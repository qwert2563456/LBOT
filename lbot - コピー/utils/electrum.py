import os
import asyncio
import logging
import json
from typing import Tuple, Optional, Any
import math
from decimal import Decimal
from utils.decimal_utils import quantize_ltc
import aiohttp
import math
from decimal import Decimal
from utils.decimal_utils import quantize_ltc

logger = logging.getLogger(__name__)

CHECKPOINT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "last_txid.txt")

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Failed to load checkpoint: {e}")
    return None

def save_checkpoint(txid):
    try:
        with open(CHECKPOINT_FILE, "w") as f:
            f.write(txid)
    except Exception as e:
        logger.error(f"Failed to save checkpoint: {e}")

ELECTRUM_RPC_URL = os.getenv("ELECTRUM_RPC_URL", "http://127.0.0.1:7777")
ELECTRUM_RPC_USER = os.getenv("ELECTRUM_RPC_USER", "electrum")
ELECTRUM_RPC_PASSWORD = os.getenv("ELECTRUM_RPC_PASSWORD", "password")

ELECTRUM_WALLET_PASSWORD = os.getenv("ELECTRUM_WALLET_PASSWORD", "")

_rpc_session: Optional[aiohttp.ClientSession] = None

def get_rpc_session() -> aiohttp.ClientSession:
    global _rpc_session
    if _rpc_session is None or _rpc_session.closed:
        auth = None
        if ELECTRUM_RPC_USER and ELECTRUM_RPC_PASSWORD:
            auth = aiohttp.BasicAuth(ELECTRUM_RPC_USER, ELECTRUM_RPC_PASSWORD)
        _rpc_session = aiohttp.ClientSession(auth=auth)
    return _rpc_session

async def call_electrum_rpc(method: str, params: Any = None) -> Any:
    """
    HTTP JSON-RPC を用いて Electrum デーモンにコマンドを送信する。
    """
    url = ELECTRUM_RPC_URL
    payload = {
        "jsonrpc": "2.0",
        "id": "lbot",
        "method": method,
        "params": params or []
    }
    
    session = get_rpc_session()
    logger.info(f"Electrum RPC request: {method} (params length: {len(str(params))})")
    try:
        async with session.post(url, json=payload, timeout=30) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "error" in data and data["error"]:
                logger.error(f"Electrum RPC error: {data['error']} (method: {method})")
                return None
            return data.get("result")
    except Exception as e:
        logger.error(f"Electrum RPC HTTP error (method: {method}): {e}", exc_info=True)
        return None


def generate_address_for_user(hd_index: int) -> Tuple[str, str]:
    """同期インターフェース（後方互換用）"""
    return f"pending_async_{hd_index}", ""


async def async_generate_address_for_user(user) -> Optional[str]:
    """
    ユーザー用の入金アドレスを取得または生成してDBに保存する。
    """
    from database import AsyncSessionLocal
    
    # 既にアドレスを持っている場合はそれを返す
    if user.deposit_address:
        return user.deposit_address

    # もし無ければ、新規でアドレスを生成する
    address = await call_electrum_rpc("createnewaddress")

    if address:
        # ラベルでユーザー(HD Index)と紐付け
        await call_electrum_rpc("setlabel", {"key": address, "label": f"User_{user.hd_index}"})
        
        # データベースに保存
        async with AsyncSessionLocal() as session:
            db_user = await session.merge(user)
            db_user.deposit_address = address
            await session.commit()
            user.deposit_address = address

    return address


async def broadcast_withdrawal(to_address: str, amount_ltc: Any, feerate: int = 1) -> Optional[str]:
    """
    指定アドレスへLTCを送金する。
    feerate: sat/byte 単位のトランザクション手数料率
    """
    logger.info(f"送金処理: {to_address} へ {amount_ltc} LTC (feerate: {feerate} sat/byte)")

    params = {
        "destination": to_address,
        "amount": str(amount_ltc),
        "feerate": feerate,
    }
    if ELECTRUM_WALLET_PASSWORD:
        params["password"] = ELECTRUM_WALLET_PASSWORD

    tx_hex_resp = await call_electrum_rpc("payto", params)
    if not tx_hex_resp:
        logger.error("payto コマンドが失敗しました。")
        return None

    if isinstance(tx_hex_resp, dict) and "hex" in tx_hex_resp:
        tx_hex = tx_hex_resp["hex"]
    else:
        tx_hex = str(tx_hex_resp)

    txid = await call_electrum_rpc("broadcast", {"tx": tx_hex})
    return txid


async def get_wallet_balance() -> Optional[dict]:
    """ウォレット全体の残高を取得"""
    return await call_electrum_rpc("getbalance")


# 必要最小承認数（この回数以上で残高に反映する）
REQUIRED_CONFIRMATIONS = 1


async def monitor_deposits_loop(bot):
    """
    バックグラウンドタスク: 入金を監視する。
    Electrum-LTC の onchain_history をチェックし、
    新しい受信TXがあればユーザーの available_balance に反映する。
    
    マッチング方式:
      各TXの "label" フィールド (例: "User_1") から hd_index を抽出し、
      DBの User.hd_index でユーザーを特定する。
    """
    from database import AsyncSessionLocal
    from models import User, Transaction
    from sqlalchemy import select
    from utils.price import fetch_ltc_jpy_price # Added import

    logger.info("入金監視ループを開始します。")

    last_txid = load_checkpoint()
    if last_txid:
        logger.info(f"Loaded checkpoint TxID: {last_txid}")

    # 初回は少し待つ（ボット起動完了を待つ）
    await asyncio.sleep(15)

    while True:
        try:
            # onchain_history を取得
            history_data = await call_electrum_rpc("onchain_history")

            if not history_data:
                await asyncio.sleep(30)
                continue

            # "transactions" キーからリストを取得
            transactions = []
            if isinstance(history_data, dict):
                transactions = history_data.get("transactions", [])
            elif isinstance(history_data, list):
                transactions = history_data

            if not transactions:
                await asyncio.sleep(30)
                continue

            # --- CHECKPOINT LOGIC ---
            if last_txid is None:
                first_unconf_idx = -1
                for i, tx in enumerate(transactions):
                    if not isinstance(tx, dict): continue
                    try: conf = int(tx.get("confirmations", 0))
                    except: conf = 0
                    if conf < REQUIRED_CONFIRMATIONS:
                        first_unconf_idx = i
                        break
                
                if first_unconf_idx == -1:
                    if transactions and isinstance(transactions[-1], dict):
                        last_txid = transactions[-1].get("txid")
                elif first_unconf_idx > 0:
                    prev_tx = transactions[first_unconf_idx - 1]
                    if isinstance(prev_tx, dict):
                        last_txid = prev_tx.get("txid")
                
                if last_txid:
                    save_checkpoint(last_txid)
                    logger.info(f"初回起動: チェックポイントを {last_txid} に設定し、過去の履歴をスキップします。")
                await asyncio.sleep(30)
                continue

            start_idx = 0
            if last_txid:
                for i, tx in enumerate(transactions):
                    if isinstance(tx, dict) and tx.get("txid") == last_txid:
                        start_idx = i
                        break
            
            transactions_to_process = transactions[start_idx:]

            async with AsyncSessionLocal() as session:
                credited_any = False

                for tx in transactions_to_process:
                    if not isinstance(tx, dict):
                        continue

                    # 受信TXのみ処理 (incoming == true)
                    if not tx.get("incoming", False):
                        continue

                    txid = tx.get("txid", "")
                    if not txid:
                        continue

                    confirmations = tx.get("confirmations", 0)
                    try:
                        confirmations = int(confirmations)
                    except (ValueError, TypeError):
                        confirmations = 0

                    # 承認数が足りなければスキップ
                    if confirmations < REQUIRED_CONFIRMATIONS:
                        continue

                    # 受信額を取得
                    value = tx.get("bc_value")
                    if value is None:
                        continue

                    try:
                        value_ltc = quantize_ltc(Decimal(str(value)))
                    except (ValueError, TypeError):
                        continue

                    if value_ltc <= 0:
                        continue

                    # ★ label からユーザーを特定 (形式: "User_1", "User_2", ...)
                    label = str(tx.get("label", ""))
                    matched_user = None

                    if label.startswith("User_"):
                        try:
                            hd_idx = int(label.split("_", 1)[1])
                            stmt = select(User).where(User.hd_index == hd_idx).with_for_update()
                            matched_user = (await session.execute(stmt)).scalar_one_or_none()
                        except (ValueError, IndexError):
                            pass

                    if not matched_user:
                        continue

                    # このTXIDが既に処理済みか確認
                    existing_tx = await session.execute(
                        select(Transaction).where(
                            Transaction.txid == txid,
                            Transaction.user_id == matched_user.discord_id,
                            Transaction.tx_type == "DEPOSIT"
                        )
                    )
                    if existing_tx.scalar_one_or_none():
                        continue  # 既に処理済み

                    # ★ 残高を加算
                    matched_user.available_balance = quantize_ltc(matched_user.available_balance) + value_ltc

                    # トランザクション記録を作成
                    new_tx = Transaction(
                        user_id=matched_user.discord_id,
                        txid=txid,
                        tx_type="DEPOSIT",
                        amount_ltc=value_ltc,
                        confirmations=confirmations
                    )
                    session.add(new_tx)
                    credited_any = True
                    logger.info(
                        f"💰 入金検出: User {matched_user.discord_id} に "
                        f"{value_ltc:.8f} LTC を加算 (txid: {txid[:16]}...)"
                    )

                    # HD Indexから取得したユーザーにDMを送信
                    try:
                        # fetch_ltc_jpy_priceを呼び出してJPY価格を取得
                        ltc_price_jpy = await fetch_ltc_jpy_price()
                        # Botのインスタンスを使ってDiscord Userをフェッチ
                        discord_user = await bot.fetch_user(int(matched_user.discord_id))
                        
                        # 送信メッセージの構築
                        msg = f"**入金が完了し、アカウントに反映されました！**\n\n"
                        msg += f"**入金額:** `{value_ltc:.8f} LTC`\n"
                        if ltc_price_jpy:
                            jpy_val = float(value_ltc) * ltc_price_jpy
                            msg += f"(日本円換算: 約 `¥{jpy_val:,.0f}`)\n"
                        msg += f"\n*TxID: {txid}*"
                        
                        await discord_user.send(msg)
                    except Exception as e:
                        logger.error(f"Failed to send deposit DM to user {matched_user.discord_id}: {e}")


                if credited_any:
                    await session.commit()
                    logger.info("入金処理をコミットしました。")

            # --- UPDATE CHECKPOINT ---
            first_unconf_idx = -1
            for i, tx in enumerate(transactions):
                if not isinstance(tx, dict): continue
                try: conf = int(tx.get("confirmations", 0))
                except: conf = 0
                if conf < REQUIRED_CONFIRMATIONS:
                    first_unconf_idx = i
                    break
            
            new_anchor = None
            if first_unconf_idx == -1:
                if transactions and isinstance(transactions[-1], dict):
                    new_anchor = transactions[-1].get("txid")
            elif first_unconf_idx > 0:
                prev_tx = transactions[first_unconf_idx - 1]
                if isinstance(prev_tx, dict):
                    new_anchor = prev_tx.get("txid")
            
            if new_anchor and new_anchor != last_txid:
                last_txid = new_anchor
                save_checkpoint(last_txid)
                logger.info(f"チェックポイントを更新しました: {last_txid}")

        except Exception as e:
            logger.error(f"Deposit monitor error: {e}", exc_info=True)

        await asyncio.sleep(30)  # 30秒ごとにチェック


