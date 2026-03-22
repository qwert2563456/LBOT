import os
import asyncio
import logging
import json
from typing import Tuple, Optional, Any
import math
from decimal import Decimal
from utils.decimal_utils import quantize_ltc
import aiohttp

logger = logging.getLogger(__name__)

ELECTRUM_RPC_URL = os.getenv("ELECTRUM_RPC_URL", "http://127.0.0.1:7777")
ELECTRUM_RPC_USER = os.getenv("ELECTRUM_RPC_USER", "electrum")
ELECTRUM_RPC_PASSWORD = os.getenv("ELECTRUM_RPC_PASSWORD", "password")
ELECTRUM_WALLET_PASSWORD = os.getenv("ELECTRUM_WALLET_PASSWORD", "")

# satoshi → LTC の変換定数
SATOSHI = Decimal("100000000")

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
    """HTTP JSON-RPC を用いて Electrum デーモンにコマンドを送信する。"""
    url = ELECTRUM_RPC_URL
    payload = {
        "jsonrpc": "2.0",
        "id": "lbot",
        "method": method,
        "params": params or []
    }
    session = get_rpc_session()
    logger.info(f"Electrum RPC request: {method}")
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
    """ユーザー用の入金アドレスを取得または生成してDBに保存する。"""
    if user.deposit_address:
        return user.deposit_address

    address = await call_electrum_rpc("createnewaddress")
    if address:
        await call_electrum_rpc("setlabel", {"key": address, "label": f"User_{user.hd_index}"})
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
    """ネットワークの現在ブロック高を取得する（キャッシュ付き・60秒）"""
    global _cached_network_height, _last_height_fetch_time
    import time

    now = time.time()
    if now - _last_height_fetch_time < 60 and _cached_network_height > 0:
        return _cached_network_height

    is_testnet = address.startswith(('m', 'n', 'Q', 'tltc'))
    url = ("https://litecoinspace.org/testnet/api/blocks/tip/height"
           if is_testnet else "https://litecoinspace.org/api/blocks/tip/height")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    _cached_network_height = int(text.strip())
                    _last_height_fetch_time = now
                    return _cached_network_height
                else:
                    logger.warning(f"fetch_network_height: status {resp.status}")
    except Exception as e:
        logger.warning(f"fetch_network_height error: {e}")

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

    # JSON文字列でラップされて返ってくる場合をデコード
    if txid:
        try:
            parsed = json.loads(txid)
            if isinstance(parsed, str):
                txid = parsed
        except Exception:
            pass

    logger.info(f"送金ブロードキャスト完了: txid={txid}")
    return txid


async def get_wallet_balance() -> Optional[dict]:
    """ウォレット全体の残高を取得"""
    return await call_electrum_rpc("getbalance")


# 必要な承認数
REQUIRED_CONFIRMATIONS = int(os.getenv("REQUIRED_CONFIRMATIONS", "3"))


def _satoshi_to_ltc(value) -> Decimal:
    """
    getaddressunspent はsatoshi単位(整数)で返すため LTC に変換する。
    例: 10666533 satoshi → 0.10666533 LTC
    """
    return quantize_ltc(Decimal(str(value)) / SATOSHI)


async def monitor_deposits_loop(bot):
    """
    バックグラウンドタスク: getaddressunspent を用いた入金監視。
    ユーザーごとにアドレスを個別ポーリングすることで tx_hash=None 問題を回避する。
    """
    from database import AsyncSessionLocal
    from models import User, Transaction, Ledger
    from sqlalchemy import select
    from utils.price import fetch_ltc_jpy_price

    logger.info("入金監視ループ（getaddressunspent）を開始します...")
    await asyncio.sleep(15)

    while True:
        try:
            # 全入金アドレスを持つユーザーを取得
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(User).where(User.deposit_address.isnot(None))
                )
                all_users = result.scalars().all()

            if not all_users:
                await asyncio.sleep(30)
                continue

            # ネットワーク高さを1回だけ取得（全ユーザー共通）
            network_height = await fetch_network_height(all_users[0].deposit_address)

            for user in all_users:
                address = user.deposit_address
                if not address:
                    continue

                # getaddressunspent: 値は satoshi 単位で返る
                unspent = await call_electrum_rpc("getaddressunspent", {"address": address})
                if not unspent or not isinstance(unspent, list):
                    continue

                for utxo in unspent:
                    txid = utxo.get("tx_hash")
                    value_raw = utxo.get("value")
                    tx_height = utxo.get("height", 0)

                    if not txid or value_raw is None:
                        continue

                    # ★ satoshi → LTC 変換
                    try:
                        value_ltc = _satoshi_to_ltc(value_raw)
                    except Exception:
                        continue

                    if value_ltc <= 0:
                        continue

                    # 承認数の計算
                    confirmations = 0
                    if tx_height > 0 and network_height > 0:
                        confirmations = max(1, network_height - tx_height + 1)
                    elif tx_height > 0:
                        confirmations = 1

                    async with AsyncSessionLocal() as session:
                        stmt = select(Transaction).where(
                            Transaction.txid == txid,
                            Transaction.user_id == user.discord_id,
                            Transaction.tx_type == "DEPOSIT"
                        )
                        existing_tx = (await session.execute(stmt)).scalar_one_or_none()

                        if existing_tx:
                            if (existing_tx.confirmations < REQUIRED_CONFIRMATIONS
                                    and confirmations >= REQUIRED_CONFIRMATIONS):
                                # ★ 残高反映処理
                                lock_stmt = select(User).where(
                                    User.discord_id == user.discord_id
                                ).with_for_update()
                                lock_user = (await session.execute(lock_stmt)).scalar_one()

                                current_unconf = quantize_ltc(
                                    lock_user.unconfirmed_balance or Decimal("0")
                                )
                                lock_user.unconfirmed_balance = max(
                                    Decimal("0"), current_unconf - value_ltc
                                )
                                lock_user.available_balance = quantize_ltc(
                                    (lock_user.available_balance or Decimal("0")) + value_ltc
                                )
                                existing_tx.confirmations = confirmations

                                ledger = Ledger(
                                    user_id=lock_user.discord_id,
                                    type="DEPOSIT",
                                    amount_ltc=value_ltc,
                                    reference_id=txid,
                                    note="Confirmed User Deposit"
                                )
                                session.add(ledger)
                                await session.commit()

                                logger.info(
                                    f"✅ 入金確定: User {lock_user.discord_id} "
                                    f"{value_ltc:.8f} LTC (txid: {txid[:16]}, conf: {confirmations})"
                                )

                                # DM通知
                                try:
                                    ltc_price_jpy = await fetch_ltc_jpy_price()
                                    discord_user = await bot.fetch_user(int(lock_user.discord_id))
                                    msg = (
                                        f"**{REQUIRED_CONFIRMATIONS} 承認に達し、"
                                        f"LTCが利用可能残高に反映されました！**\n"
                                        f"**反映額:** `{value_ltc:.8f} LTC`\n"
                                    )
                                    if ltc_price_jpy:
                                        jpy_val = int(float(value_ltc) * ltc_price_jpy)
                                        msg += f"(日本円換算: 約 `¥{jpy_val:,}`)\n"
                                    msg += f"*TxID: {txid}*"
                                    await discord_user.send(msg)
                                except Exception as e:
                                    logger.error(f"DM送信失敗 {user.discord_id}: {e}")

                            elif existing_tx.confirmations < confirmations:
                                # 承認数のインクリメントのみ
                                existing_tx.confirmations = confirmations
                                await session.commit()

                        else:
                            # 新規TX検出
                            lock_stmt = select(User).where(
                                User.discord_id == user.discord_id
                            ).with_for_update()
                            lock_user = (await session.execute(lock_stmt)).scalar_one()

                            new_tx = Transaction(
                                user_id=lock_user.discord_id,
                                txid=txid,
                                tx_type="DEPOSIT",
                                amount_ltc=value_ltc,
                                confirmations=confirmations,
                            )
                            session.add(new_tx)

                            if confirmations >= REQUIRED_CONFIRMATIONS:
                                # いきなり規定承認数以上（bot再起動後等）
                                lock_user.available_balance = quantize_ltc(
                                    (lock_user.available_balance or Decimal("0")) + value_ltc
                                )
                                ledger = Ledger(
                                    user_id=lock_user.discord_id,
                                    type="DEPOSIT",
                                    amount_ltc=value_ltc,
                                    reference_id=txid,
                                    note="Confirmed User Deposit (Direct)"
                                )
                                session.add(ledger)
                                is_confirmed = True
                                logger.info(
                                    f"✅ 入金確定(ダイレクト): User {lock_user.discord_id} "
                                    f"{value_ltc:.8f} LTC (txid: {txid[:16]})"
                                )
                            else:
                                # 未確定残高へ
                                lock_user.unconfirmed_balance = quantize_ltc(
                                    (lock_user.unconfirmed_balance or Decimal("0")) + value_ltc
                                )
                                is_confirmed = False
                                logger.info(
                                    f"⏳ 入金検知(未確定): User {lock_user.discord_id} "
                                    f"{value_ltc:.8f} LTC (txid: {txid[:16]}, conf: {confirmations})"
                                )

                            await session.commit()

                            # DM通知
                            try:
                                ltc_price_jpy = await fetch_ltc_jpy_price()
                                discord_user = await bot.fetch_user(int(lock_user.discord_id))
                                if is_confirmed:
                                    msg = "**入金が完了し、LTCが利用可能残高に反映されました！**\n"
                                else:
                                    msg = (
                                        f"**ブロックチェーン上で入金を検知しました（承認待ち）**\n"
                                        f"*{REQUIRED_CONFIRMATIONS} 承認で利用可能残高に移行します。*\n"
                                    )
                                msg += f"**金額:** `{value_ltc:.8f} LTC`\n"
                                if ltc_price_jpy:
                                    jpy_val = int(float(value_ltc) * ltc_price_jpy)
                                    msg += f"(日本円換算: 約 `¥{jpy_val:,}`)\n"
                                msg += f"*TxID: {txid}*"
                                await discord_user.send(msg)
                            except Exception as e:
                                logger.error(f"DM送信失敗 {user.discord_id}: {e}")

        except Exception as e:
            logger.error(f"Deposit monitor error: {e}", exc_info=True)

        await asyncio.sleep(30)