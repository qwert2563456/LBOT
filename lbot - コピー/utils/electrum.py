import os
import asyncio
import logging
import json
from typing import Optional, Any
from decimal import Decimal
from utils.decimal_utils import quantize_ltc
import aiohttp

logger = logging.getLogger(__name__)

ELECTRUM_RPC_URL         = os.getenv("ELECTRUM_RPC_URL", "http://127.0.0.1:7777")
ELECTRUM_RPC_USER        = os.getenv("ELECTRUM_RPC_USER", "electrum")
ELECTRUM_RPC_PASSWORD    = os.getenv("ELECTRUM_RPC_PASSWORD", "password")
ELECTRUM_WALLET_PASSWORD = os.getenv("ELECTRUM_WALLET_PASSWORD", "")

SATOSHI = Decimal("100000000")

_rpc_session: Optional[aiohttp.ClientSession] = None


def get_rpc_session() -> aiohttp.ClientSession:
    global _rpc_session
    if _rpc_session is None or _rpc_session.closed:
        auth = aiohttp.BasicAuth(ELECTRUM_RPC_USER, ELECTRUM_RPC_PASSWORD) if ELECTRUM_RPC_USER else None
        _rpc_session = aiohttp.ClientSession(auth=auth)
    return _rpc_session


async def call_electrum_rpc(method: str, params: Any = None) -> Any:
    payload = {"jsonrpc": "2.0", "id": "lbot", "method": method, "params": params or []}
    try:
        async with get_rpc_session().post(
            ELECTRUM_RPC_URL, json=payload,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if "error" in data and data["error"]:
                logger.error(f"RPC error: {data['error']} ({method})")
                return None
            return data.get("result")
    except Exception as e:
        logger.error(f"RPC HTTP error ({method}): {e}", exc_info=True)
        return None


def _satoshi_to_ltc(value) -> Decimal:
    """satoshi単位(整数) → LTC(Decimal)  例: 10666533 → 0.10666533"""
    return quantize_ltc(Decimal(str(value)) / SATOSHI)


# ── 使い捨てアドレス生成 ─────────────────────────────────

async def generate_new_deposit_address(user, session) -> Optional[str]:
    """
    使い捨て入金アドレスを返す。
    ルール:
      - 現在のアクティブアドレスに対してDBのTransactionテーブルにDEPOSITレコードが
        1件も存在しない（= 一度も使われていない）場合 → そのまま使い回す
      - 1件でもDEPOSITがある（= 使用済み）場合 → 新しいアドレスを生成する
    これにより「入金ボタンを何度押しても未使用なら同じアドレスを返す」が実現できる。
    """
    from models import UserDepositAddress, Transaction
    from sqlalchemy import select

    # 現在のアクティブアドレスをDBから取得
    stmt = select(UserDepositAddress).where(
        UserDepositAddress.user_id == user.discord_id,
        UserDepositAddress.is_active == True,
    )
    current = (await session.execute(stmt)).scalar_one_or_none()

    if current:
        # ★ Electrum RPCではなく自前のTransactionテーブルで「使用済み」を判定
        used_check = await session.execute(
            select(Transaction).where(
                Transaction.user_id == user.discord_id,
                Transaction.tx_type == "DEPOSIT",
                # txidにアドレスは入っていないので、
                # 監視ループが記録した時点でuser_idと紐づいているため
                # 「このユーザーへの入金TX」が1件でもあれば使用済みとみなす。
                # ただし複数アドレス持ちの場合に誤判定しないよう
                # user_deposit_addressesのaddressと突合する。
            )
        )
        deposit_txs = used_check.scalars().all()

        # 現在アクティブなアドレス宛のTXがあるかを確認
        # TransactionにはアドレスカラムがないのでUTXOで確認するが、
        # それも不安定なため「このアドレスのUserDepositAddressレコードのcreated_at
        # 以降にDEPOSIT TXが存在するか」で判定する
        addr_created_at = current.created_at
        used_stmt = select(Transaction).where(
            Transaction.user_id == user.discord_id,
            Transaction.tx_type == "DEPOSIT",
            Transaction.created_at >= addr_created_at,
        )
        used_tx = (await session.execute(used_stmt)).scalar_one_or_none()

        if used_tx is None:
            # 未使用 → 同じアドレスを返す
            user.deposit_address = current.address
            logger.info(f"既存アドレス再利用: user={user.discord_id} addr={current.address}")
            return current.address
        # else: 使用済みなので以降で新規生成

    # 新規アドレスを生成
    address = await call_electrum_rpc("createnewaddress")
    if not address:
        # 生成失敗時は既存アドレスをそのまま返す（フォールバック）
        if current:
            logger.warning(f"createnewaddress 失敗、既存アドレスを返します: user={user.discord_id}")
            return current.address
        logger.error(f"createnewaddress 失敗: user={user.discord_id}")
        return None

    await call_electrum_rpc("setlabel", {"key": address, "label": f"User_{user.discord_id}"})

    # 古いアクティブを無効化
    if current:
        current.is_active = False

    session.add(UserDepositAddress(user_id=user.discord_id, address=address, is_active=True))

    # UNIQUE制約のため一度NULLにしてから更新
    user.deposit_address = None
    await session.flush()
    user.deposit_address = address
    await session.flush()

    logger.info(f"新規入金アドレス生成: user={user.discord_id} addr={address}")
    return address


async def async_generate_address_for_user(user, session=None) -> Optional[str]:
    """後方互換用。常に新しいアドレスを生成する（使い捨て）。"""
    if session:
        return await generate_new_deposit_address(user, session)

    from database import AsyncSessionLocal
    from models import User
    async with AsyncSessionLocal() as s:
        db_user = await s.get(User, user.discord_id)
        if not db_user:
            return None
        addr = await generate_new_deposit_address(db_user, s)
        await s.commit()
        user.deposit_address = addr
        return addr


# ── ネットワーク高さ ─────────────────────────────────────

_cached_network_height = 0
_last_height_fetch_time = 0


async def fetch_network_height(address: str) -> int:
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
                    _cached_network_height = int((await resp.text()).strip())
                    _last_height_fetch_time = now
                    return _cached_network_height
    except Exception as e:
        logger.warning(f"fetch_network_height error: {e}")
    return _cached_network_height


# ── 送金 ─────────────────────────────────────────────────

async def broadcast_withdrawal(to_address: str, amount_ltc: Any, feerate: int = 1) -> Optional[str]:
    """指定アドレスへLTCを送金する。呼び出し前にRELEASINGステータスをコミットすること。"""
    logger.info(f"送金: {to_address} {amount_ltc} LTC (feerate={feerate})")

    params = {"destination": to_address, "amount": str(amount_ltc), "feerate": feerate}
    if ELECTRUM_WALLET_PASSWORD:
        params["password"] = ELECTRUM_WALLET_PASSWORD

    tx_hex_resp = await call_electrum_rpc("payto", params)
    if not tx_hex_resp:
        return None

    tx_hex = tx_hex_resp["hex"] if isinstance(tx_hex_resp, dict) and "hex" in tx_hex_resp else str(tx_hex_resp)
    txid = await call_electrum_rpc("broadcast", {"tx": tx_hex})

    if txid:
        try:
            parsed = json.loads(txid)
            if isinstance(parsed, str):
                txid = parsed
        except Exception:
            pass

    logger.info(f"送金完了: txid={txid}")
    return txid


async def get_wallet_balance() -> Optional[dict]:
    return await call_electrum_rpc("getbalance")


# ── 入金監視 ─────────────────────────────────────────────

REQUIRED_CONFIRMATIONS = int(os.getenv("REQUIRED_CONFIRMATIONS", "3"))


async def monitor_deposits_loop(bot):
    """
    入金監視ループ。
    user_deposit_addresses テーブルの全アドレス（is_active新旧問わず）を監視するため、
    過去の使い捨てアドレスへの入金も正しく検出できる。
    """
    from database import AsyncSessionLocal
    from models import User, UserDepositAddress, Transaction, Ledger
    from sqlalchemy import select
    from utils.price import fetch_ltc_jpy_price

    logger.info("入金監視ループ（全アドレス追跡）を開始します...")
    await asyncio.sleep(15)

    while True:
        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(select(UserDepositAddress))).scalars().all()

            if not rows:
                await asyncio.sleep(30)
                continue

            addr_to_user_id = {row.address: row.user_id for row in rows}
            network_height = await fetch_network_height(next(iter(addr_to_user_id)))

            for address, user_id in addr_to_user_id.items():
                unspent = await call_electrum_rpc("getaddressunspent", {"address": address})
                if not unspent or not isinstance(unspent, list):
                    continue

                for utxo in unspent:
                    txid      = utxo.get("tx_hash")
                    value_raw = utxo.get("value")
                    tx_height = utxo.get("height", 0)

                    if not txid or value_raw is None:
                        continue

                    try:
                        value_ltc = _satoshi_to_ltc(value_raw)
                    except Exception:
                        continue

                    if value_ltc <= 0:
                        continue

                    confirmations = 0
                    if tx_height > 0 and network_height > 0:
                        confirmations = max(1, network_height - tx_height + 1)
                    elif tx_height > 0:
                        confirmations = 1

                    async with AsyncSessionLocal() as session:
                        existing_tx = (await session.execute(
                            select(Transaction).where(
                                Transaction.txid == txid,
                                Transaction.user_id == user_id,
                                Transaction.tx_type == "DEPOSIT"
                            )
                        )).scalar_one_or_none()

                        if existing_tx:
                            if (existing_tx.confirmations < REQUIRED_CONFIRMATIONS
                                    and confirmations >= REQUIRED_CONFIRMATIONS):
                                lock_user = (await session.execute(
                                    select(User).where(User.discord_id == user_id).with_for_update()
                                )).scalar_one()

                                unconf = quantize_ltc(lock_user.unconfirmed_balance or Decimal("0"))
                                lock_user.unconfirmed_balance = max(Decimal("0"), unconf - value_ltc)
                                lock_user.available_balance   = quantize_ltc(
                                    (lock_user.available_balance or Decimal("0")) + value_ltc
                                )
                                existing_tx.confirmations = confirmations
                                session.add(Ledger(
                                    user_id=user_id, type="DEPOSIT",
                                    amount_ltc=value_ltc, reference_id=txid,
                                    note="Confirmed User Deposit"
                                ))
                                await session.commit()

                                logger.info(f"✅ 入金確定: {user_id} {value_ltc:.8f} LTC")

                                try:
                                    price = await fetch_ltc_jpy_price()
                                    du = await bot.fetch_user(int(user_id))
                                    jpy_val = int(float(value_ltc) * price) if price else 0
                                    msg = (f"**{REQUIRED_CONFIRMATIONS}承認に達し、残高に反映されました！**\n"
                                           f"**反映額:** `{value_ltc:.8f} LTC`")
                                    if jpy_val:
                                        msg += f"\n(≈ `¥{jpy_val:,}`)"
                                    msg += f"\n*TxID: {txid}*"
                                    await du.send(msg)
                                except Exception as e:
                                    logger.error(f"DM失敗 {user_id}: {e}")

                            elif existing_tx.confirmations < confirmations:
                                existing_tx.confirmations = confirmations
                                await session.commit()

                        else:
                            lock_user = (await session.execute(
                                select(User).where(User.discord_id == user_id).with_for_update()
                            )).scalar_one()

                            session.add(Transaction(
                                user_id=user_id, txid=txid,
                                tx_type="DEPOSIT", amount_ltc=value_ltc,
                                confirmations=confirmations,
                            ))

                            if confirmations >= REQUIRED_CONFIRMATIONS:
                                lock_user.available_balance = quantize_ltc(
                                    (lock_user.available_balance or Decimal("0")) + value_ltc
                                )
                                session.add(Ledger(
                                    user_id=user_id, type="DEPOSIT",
                                    amount_ltc=value_ltc, reference_id=txid,
                                    note="Confirmed User Deposit (Direct)"
                                ))
                                is_confirmed = True
                                logger.info(f"✅ 入金確定(ダイレクト): {user_id} {value_ltc:.8f} LTC")
                            else:
                                lock_user.unconfirmed_balance = quantize_ltc(
                                    (lock_user.unconfirmed_balance or Decimal("0")) + value_ltc
                                )
                                is_confirmed = False
                                logger.info(f"⏳ 入金検知(未確定): {user_id} {value_ltc:.8f} LTC conf={confirmations}")

                            await session.commit()

                            try:
                                price = await fetch_ltc_jpy_price()
                                du = await bot.fetch_user(int(user_id))
                                msg = ("**入金が完了し、残高に反映されました！**\n" if is_confirmed
                                       else f"**入金を検知しました（承認待ち）**\n"
                                            f"*{REQUIRED_CONFIRMATIONS}承認で利用可能になります。*\n")
                                jpy_val = int(float(value_ltc) * price) if price else 0
                                msg += f"**金額:** `{value_ltc:.8f} LTC`"
                                if jpy_val:
                                    msg += f"\n(≈ `¥{jpy_val:,}`)"
                                msg += f"\n*TxID: {txid}*"
                                await du.send(msg)
                            except Exception as e:
                                logger.error(f"DM失敗 {user_id}: {e}")

        except Exception as e:
            logger.error(f"Deposit monitor error: {e}", exc_info=True)

        await asyncio.sleep(30)