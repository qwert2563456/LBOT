import os
import asyncio
from asyncio import subprocess
import logging
import json
from typing import Tuple, Optional, Any

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

# electrum-ltc 実行ファイルのパス
ELECTRUM_BIN = os.getenv("ELECTRUM_BIN", "electrum-ltc")

# ウォレットファイルのフルパス
# デフォルト: %APPDATA%\Electrum-LTC\wallets\default_wallet
_default_wallet = os.path.join(
    os.getenv("APPDATA", ""),
    "Electrum-LTC", "wallets",
    os.getenv("ELECTRUM_WALLET", "default_wallet")
)
ELECTRUM_WALLET_PATH = os.getenv("ELECTRUM_WALLET_PATH", _default_wallet)

# ウォレットパスワード（パスワード保護されたウォレット用）
ELECTRUM_WALLET_PASSWORD = os.getenv("ELECTRUM_WALLET_PASSWORD", "")

# ウォレットロード済みフラグ（無限リトライ防止）
_wallet_loaded = False


async def _load_wallet() -> bool:
    """デーモンにウォレットをロードさせる"""
    global _wallet_loaded
    if _wallet_loaded:
        return True

    logger.info(f"Loading wallet: {ELECTRUM_WALLET_PATH}")
    try:
        proc = await asyncio.create_subprocess_exec(
            ELECTRUM_BIN, "daemon", "load_wallet", "-w", ELECTRUM_WALLET_PATH,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            logger.info(f"Wallet loaded successfully: {out}")
            _wallet_loaded = True
            return True
        else:
            logger.warning(f"load_wallet returned code {proc.returncode}: {err}")
            # 既にロード済みの場合もある
            if "already loaded" in err.lower() or "already loaded" in out.lower():
                _wallet_loaded = True
                return True
            return False
    except Exception as e:
        logger.error(f"load_wallet failed: {e}")
        return False


async def run_electrum_cmd(*args: str, _is_retry: bool = False) -> Optional[str]:
    """
    electrum-ltc CLI を実行。
    自動的に -w (wallet path) オプションを付与する。
    """
    cmd = [ELECTRUM_BIN, "-w", ELECTRUM_WALLET_PATH] + list(args)
    logger.info(f"Electrum CMD: {' '.join(cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()

            # ウォレット未ロード → 1回だけリトライ
            if not _is_retry and "wallet not loaded" in err_msg.lower():
                loaded = await _load_wallet()
                if loaded:
                    await asyncio.sleep(1)
                    return await run_electrum_cmd(*args, _is_retry=True)

            logger.error(f"Electrum error (code {proc.returncode}): {err_msg}")
            return None

        result = stdout.decode("utf-8", errors="replace").strip()
        return result if result else None

    except asyncio.TimeoutError:
        logger.error("Electrum command timed out (30s)")
        return None
    except FileNotFoundError:
        logger.error(f"Electrum binary not found: {ELECTRUM_BIN}")
        return None
    except Exception as e:
        logger.error(f"Electrum subprocess error: {e}")
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

    # もし無ければ、新規でアドレスを生成する (getunusedaddressだと全員同じになる問題を回避)
    address = await run_electrum_cmd("createnewaddress")

    if address:
        # JSON形式で返ってくる場合を処理
        try:
            parsed = json.loads(address)
            if isinstance(parsed, str):
                address = parsed
        except (json.JSONDecodeError, TypeError):
            pass

        # ラベルでユーザー(HD Index)と紐付け
        await run_electrum_cmd("setlabel", address, f"User_{user.hd_index}")
        
        # データベースに保存
        async with AsyncSessionLocal() as session:
            # セッションにマージするか再度取得
            db_user = await session.merge(user)
            db_user.deposit_address = address
            await session.commit()
            user.deposit_address = address # 現在のインスタンスも更新

    return address


async def broadcast_withdrawal(to_address: str, amount_ltc: float, feerate: int = 1) -> Optional[str]:
    """
    指定アドレスへLTCを送金する。
    feerate: sat/byte 単位のトランザクション手数料率
    パスワード保護されたウォレットの場合、--password オプションを付与する。
    """
    logger.info(f"送金処理: {to_address} へ {amount_ltc} LTC (feerate: {feerate} sat/byte)")

    # payto でトランザクション作成
    payto_args = ["payto", to_address, str(amount_ltc), "--feerate", str(feerate)]
    if ELECTRUM_WALLET_PASSWORD:
        payto_args += ["--password", ELECTRUM_WALLET_PASSWORD]

    tx_hex = await run_electrum_cmd(*payto_args)
    if not tx_hex:
        logger.error("payto コマンドが失敗しました。")
        return None

    # JSON形式で返ってくる場合 {"hex": "...", "complete": true}
    try:
        data = json.loads(tx_hex)
        if isinstance(data, dict) and "hex" in data:
            tx_hex = data["hex"]
    except (json.JSONDecodeError, TypeError):
        pass

    txid = await run_electrum_cmd("broadcast", tx_hex)
    return txid


async def get_wallet_balance() -> Optional[dict]:
    """ウォレット全体の残高を取得"""
    result = await run_electrum_cmd("getbalance")
    if result:
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            pass
    return None


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
            # onchain_history を取得（JSON形式）
            result = await run_electrum_cmd("onchain_history")

            if not result:
                await asyncio.sleep(30)
                continue

            try:
                history_data = json.loads(result)
            except json.JSONDecodeError:
                logger.warning(f"history JSON parse failed: {result[:200]}")
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
                # hd_index → User のマッピングを構築
                all_users_stmt = select(User)
                all_users_res = await session.execute(all_users_stmt)
                all_users = all_users_res.scalars().all()

                hd_index_to_user = {}
                for u in all_users:
                    hd_index_to_user[u.hd_index] = u

                if not hd_index_to_user:
                    await asyncio.sleep(30)
                    continue

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
                        value_ltc = float(value)
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
                            matched_user = hd_index_to_user.get(hd_idx)
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
                    matched_user.available_balance = float(matched_user.available_balance) + value_ltc

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


