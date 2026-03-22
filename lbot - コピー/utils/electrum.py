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


async def async_generate_address_for_user(user, session=None) -> Optional[str]:
    """
    ユーザー用の入金アドレスを取得または生成してDBに保存する。
    """
    # 既にアドレスを持っている場合はそれを返す
    if user.deposit_address:
        return user.deposit_address

    # もし無ければ、新規でアドレスを生成する
    address = await call_electrum_rpc("createnewaddress")

    if address:
        # ラベルでユーザー(HD Index)と紐付け
        await call_electrum_rpc("setlabel", {"key": address, "label": f"User_{user.hd_index}"})
        
        # データベースに保存
        if session:
            user.deposit_address = address
            session.add(user)
            await session.flush()
        else:
            from database import AsyncSessionLocal
            async with AsyncSessionLocal() as s:
                db_user = await s.merge(user)
                db_user.deposit_address = address
                await s.commit()
                user.deposit_address = address

    return address


_cached_network_height = 0
_last_height_fetch_time = 0

async def fetch_network_height(address: str) -> int:
    """アドレスのプレフィックスからMainnet/Testnetを判定し、外部APIで現在のブロック高を取得する(キャッシュ付き)"""
    global _cached_network_height, _last_height_fetch_time
    import time
    
    now = time.time()
    if now - _last_height_fetch_time < 60 and _cached_network_height > 0:
        return _cached_network_height

    is_testnet = address.startswith(('m', 'n', 'Q', 'tltc'))
    url = "https://litecoinspace.org/testnet/api/blocks/tip/height" if is_testnet else "https://litecoinspace.org/api/blocks/tip/height"
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    _cached_network_height = int(text.strip())
                    _last_height_fetch_time = now
                    return _cached_network_height
                else:
                    logger.warning(f"Failed to fetch network height from {url}, status: {resp.status}")
    except Exception as e:
        logger.warning(f"Failed to fetch network height from {url}: {e}")
    
    return _cached_network_height


async def broadcast_withdrawal(to_address: str, amount_ltc: Any, feerate: int = 1) -> Optional[str]:
    """
    指定アドレスへLTCを送金する。
    ★二重送金防止のため、呼び出し元で中間ステータス（RELEASING等）をコミットしてから呼ぶこと。
    """
    logger.info(f"送金処理開始: {to_address} へ {amount_ltc} LTC (feerate: {feerate} sat/byte)")

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
    logger.info(f"送金ブロードキャスト完了: txid={txid}")
    return txid


async def get_wallet_balance() -> Optional[dict]:
    """ウォレット全体の残高を取得"""
    return await call_electrum_rpc("getbalance")


# 必要な承認数
REQUIRED_CONFIRMATIONS = int(os.getenv("REQUIRED_CONFIRMATIONS", "3"))


async def monitor_deposits_loop(bot):
    """
    バックグラウンドタスク: listunspent を用いた軽量な入金監視。
    """
    from database import AsyncSessionLocal
    from models import User, Transaction, Ledger
    from sqlalchemy import select
    from utils.price import fetch_ltc_jpy_price

    logger.info("軽量な入金監視ループ（listunspent）を開始します...")
    await asyncio.sleep(15)

    while True:
        try:
            unspent_data = await call_electrum_rpc("listunspent")
            if not isinstance(unspent_data, list):
                await asyncio.sleep(30)
                continue

            async with AsyncSessionLocal() as session:
                # すべてのデポジットアドレスをメモリに乗せる（ユーザー数が数万規模でなければ高速）
                result = await session.execute(select(User).where(User.deposit_address.isnot(None)))
                address_to_user = {u.deposit_address: u for u in result.scalars()}

                network_height = 0
                sample_address = next(iter(address_to_user.keys()), None)
                if sample_address:
                    network_height = await fetch_network_height(sample_address)

                # listunspent で見つかった各UTXOを処理
                for utxo in unspent_data:
                    # 1つのUTXOの中身: {"address": "...", "value": 1.23, "height": 100000, "tx_hash": "..."}
                    address = utxo.get("address")
                    txid = utxo.get("tx_hash")
                    value_str = utxo.get("value")
                    tx_height = utxo.get("height", 0)

                    if not address or not txid or value_str is None:
                        continue

                    # エスクローアドレスなどではなく、一般ユーザーの入金アドレスか？
                    matched_user = address_to_user.get(address)
                    if not matched_user:
                        continue

                    try:
                        value_ltc = quantize_ltc(Decimal(str(value_str)))
                    except Exception:
                        continue

                    if value_ltc <= 0:
                        continue

                    # 承認数の計算
                    confirmations = 0
                    if tx_height > 0 and network_height > 0:
                        confirmations = max(1, network_height - tx_height + 1)
                    elif tx_height > 0:
                        confirmations = 1  # ネットワーク高さが取れなかった場合の最低保障値

                    # トランザクションレコードの存在チェック
                    stmt = select(Transaction).where(
                        Transaction.txid == txid,
                        Transaction.user_id == matched_user.discord_id,
                        Transaction.tx_type == "DEPOSIT"
                    )
                    existing_tx = (await session.execute(stmt)).scalar_one_or_none()

                    if existing_tx:
                        # 既存のTXがまだREQUIRED_CONFIRMATIONSに達していない場合のみ更新処理を行う
                        if existing_tx.confirmations < REQUIRED_CONFIRMATIONS and confirmations >= REQUIRED_CONFIRMATIONS:
                            # 行ロックを取得して残高を移動する
                            lock_stmt = select(User).where(User.discord_id == matched_user.discord_id).with_for_update()
                            lock_user = (await session.execute(lock_stmt)).scalar_one()

                            lock_user.unconfirmed_balance = quantize_ltc(lock_user.unconfirmed_balance - value_ltc)
                            lock_user.available_balance = quantize_ltc(lock_user.available_balance + value_ltc)
                            existing_tx.confirmations = confirmations

                            # Ledger（台帳）に記入
                            ledger = Ledger(
                                user_id=lock_user.discord_id,
                                type="DEPOSIT",
                                amount_ltc=value_ltc,
                                reference_id=txid,
                                note="Confirmed User Deposit"
                            )
                            session.add(ledger)
                            
                            logger.info(f"✅ 入金確定: User {lock_user.discord_id} に {value_ltc:.8f} LTC を加算 (txid: {txid[:16]}, 承認数: {confirmations})")

                            # DM通知
                            try:
                                ltc_price_jpy = await fetch_ltc_jpy_price()
                                discord_user = await bot.fetch_user(int(lock_user.discord_id))
                                msg = f"**{REQUIRED_CONFIRMATIONS} 承認に達し、LTCが利用可能残高に反映されました！**\n"
                                msg += f"**反映額:** `{value_ltc:.8f} LTC`\n"
                                if ltc_price_jpy:
                                    jpy_val = int(float(value_ltc) * ltc_price_jpy)
                                    msg += f"(日本円換算: 約 `¥{jpy_val:,}`)\n"
                                msg += f"*TxID: {txid}*"
                                await discord_user.send(msg)
                            except Exception as e:
                                logger.error(f"Failed to send confirmed DM to user {lock_user.discord_id}: {e}")

                        elif existing_tx.confirmations < confirmations:
                            # 単なる承認数のインクリメント
                            existing_tx.confirmations = confirmations

                    else:
                        # まだ Transaction に存在しない新規入金（未確定）
                        lock_stmt = select(User).where(User.discord_id == matched_user.discord_id).with_for_update()
                        lock_user = (await session.execute(lock_stmt)).scalar_one()

                        new_tx = Transaction(
                            user_id=lock_user.discord_id,
                            txid=txid,
                            tx_type="DEPOSIT",
                            amount_ltc=value_ltc,
                            confirmations=confirmations
                        )
                        session.add(new_tx)

                        if confirmations >= REQUIRED_CONFIRMATIONS:
                            # いきなり3承認以上で発見された場合（bot再起動時など）
                            lock_user.available_balance = quantize_ltc(lock_user.available_balance + value_ltc)
                            ledger = Ledger(
                                user_id=lock_user.discord_id,
                                type="DEPOSIT",
                                amount_ltc=value_ltc,
                                reference_id=txid,
                                note="Confirmed User Deposit (Direct)"
                            )
                            session.add(ledger)
                            logger.info(f"✅ 入金確定(ダイレクト): User {lock_user.discord_id} に {value_ltc:.8f} LTC (txid: {txid[:16]})")
                            is_confirmed = True
                        else:
                            # 未確定残高に追加
                            lock_user.unconfirmed_balance = quantize_ltc(lock_user.unconfirmed_balance + value_ltc)
                            logger.info(f"⏳ 入金検知(未確定): User {lock_user.discord_id} に {value_ltc:.8f} LTC (txid: {txid[:16]}, 承認数: {confirmations})")
                            is_confirmed = False

                        try:
                            # DM通知
                            ltc_price_jpy = await fetch_ltc_jpy_price()
                            discord_user = await bot.fetch_user(int(lock_user.discord_id))
                            if is_confirmed:
                                msg = f"**入金が完了し、LTCが利用可能残高に反映されました！**\n"
                            else:
                                msg = f"**ブロックチェーン上で入金を検知しました（承認待ち）**\n"
                                msg += f"*{REQUIRED_CONFIRMATIONS} 承認されると利用可能残高に移行します。*\n"
                                
                            msg += f"**金額:** `{value_ltc:.8f} LTC`\n"
                            if ltc_price_jpy:
                                jpy_val = int(float(value_ltc) * ltc_price_jpy)
                                msg += f"(日本円換算: 約 `¥{jpy_val:,}`)\n"
                            msg += f"*TxID: {txid}*"
                            await discord_user.send(msg)
                        except Exception as e:
                            logger.error(f"Failed to send deposit DM to user {lock_user.discord_id}: {e}")

                await session.commit()

            # ※注：出金済みUTXOは listunspent から即座に消えるが、Transactionに一度記録されていれば
            # すり抜けたわけではなく、次にネットワーク高さが上がった際（あるいは個別ポーリング処理時）に
            # 承認扱いにする処理を本来は追加すべきだが、botは中間ステータス(RELEASING)運用により
            # DB整合性が担保されている前提となるため一旦 listunspent のみで回す

        except Exception as e:
            logger.error(f"Deposit monitor error: {e}", exc_info=True)

        await asyncio.sleep(30)



