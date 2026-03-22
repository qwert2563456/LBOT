"""
utils/escrow_monitor.py
仲介型取引のオンチェーンエスクロー入金を監視するバックグラウンドタスク。
既存の monitor_deposits_loop とは独立して動作する。
"""
import asyncio
import os
import json
import logging
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from database import AsyncSessionLocal
from models import EscrowOrder, SystemConfig, get_or_create_user, EscrowAd
from utils.electrum import call_electrum_rpc, broadcast_withdrawal
from utils.price import fetch_ltc_jpy_price
from utils.decimal_utils import quantize_ltc
from decimal import Decimal

logger = logging.getLogger(__name__)

REQUIRED_CONFIRMATIONS = int(os.getenv("REQUIRED_CONFIRMATIONS", "3"))
SELLER_LTC_TIMEOUT_MINS = 15


_cached_network_height = 0
_last_height_fetch_time = 0

async def fetch_network_height(address: str) -> int:
    global _cached_network_height, _last_height_fetch_time
    import time

    now = time.time()
    if now - _last_height_fetch_time < 60 and _cached_network_height > 0:
        return _cached_network_height

    is_testnet = address.startswith(('m', 'n', 'Q', 'tltc'))
    url = "https://litecoinspace.org/testnet/api/blocks/tip/height" if is_testnet else "https://litecoinspace.org/api/blocks/tip/height"
    import aiohttp
    try:
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


async def monitor_escrow_loop(bot):
    """
    バックグラウンドタスク: エスクローアドレスへの入金を監視する。
    30秒ごとに WAITING_SELLER_LTC / SELLER_LTC_DETECTED の注文を確認する。
    """
    logger.info("仲介型エスクロー監視ループを開始します。")
    await asyncio.sleep(20)

    while True:
        try:
            await _check_escrow_deposits(bot)
            await _check_seller_ltc_timeouts(bot)
        except Exception as e:
            logger.error(f"Escrow monitor error: {e}", exc_info=True)
        await asyncio.sleep(30)


async def _check_escrow_deposits(bot):
    """
    WAITING_SELLER_LTC または SELLER_LTC_DETECTED のオーダーについて
    エスクローアドレスへの入金を履歴（getaddresshistory）から厳密にチェックする。
    """
    async with AsyncSessionLocal() as session:
        stmt = select(EscrowOrder).where(
            EscrowOrder.status.in_(['WAITING_SELLER_LTC', 'SELLER_LTC_DETECTED'])
        )
        result = await session.execute(stmt)
        orders = result.scalars().all()

        if not orders:
            return

        network_height = 0
        sample_addr = next((o.escrow_address for o in orders if o.escrow_address), None)
        if sample_addr:
            network_height = await fetch_network_height(sample_addr)

        for order in orders:
            addr = order.escrow_address
            if not addr:
                continue

            hist = await call_electrum_rpc("getaddresshistory", {"address": addr})
            if not hist or not isinstance(hist, list):
                continue

            total_ltc = Decimal("0")
            confirmed_ltc = Decimal("0")
            latest_txid = "N/A"

            # 特定のアドレスに対する受信額を計算する
            for item in hist:
                # getaddresshistory では金額が直接出ないことがあるため、リスト済みのtxからUTXOを探すには getaddressunspent が一番確実
                # 念のため getaddressunspent も取得して額を割り出す
                pass
            
            # 安全のため額は getaddressbalance と listunspent 両方で確認がよいが、
            # Electrumの getaddressbalance は確認済み・未確認を合計して出せる。
            unspent = await call_electrum_rpc("getaddressunspent", {"address": addr})
            if not unspent or not isinstance(unspent, list):
                continue
            
            for utxo in unspent:
                val = Decimal(str(utxo.get("value", 0)))
                h = utxo.get("height", 0)
                tx_hash = utxo.get("tx_hash", "")
                
                total_ltc += val
                latest_txid = tx_hash
                
                conf = 0
                if h > 0 and network_height > 0:
                    conf = max(1, network_height - h + 1)
                elif h > 0:
                    conf = 1
                
                if conf >= REQUIRED_CONFIRMATIONS:
                    confirmed_ltc += val

            required_ltc = quantize_ltc(Decimal(str(order.amount_ltc)))

            if total_ltc < required_ltc:
                if total_ltc > 0 and latest_txid != "N/A" and order.seller_sent_txid != latest_txid:
                    order.seller_sent_txid = latest_txid
                    await session.commit()
                    shortage = required_ltc - total_ltc
                    logger.info(f"EscrowOrder #{order.id}: 一部入金検知 ({total_ltc:.8f} LTC, 不足 {shortage:.8f} LTC)")
                    await _on_seller_ltc_partial(bot, order.id, total_ltc, shortage, latest_txid)
                continue

            # 総額は満たしているが、承認数が足りているか？
            if confirmed_ltc >= required_ltc:
                if order.status != 'SELLER_LTC_CONFIRMED':
                    order.status = 'SELLER_LTC_CONFIRMED'
                    order.seller_sent_txid = latest_txid
                    await session.commit()
                    logger.info(f"EscrowOrder #{order.id}: LTC着金確認完了 ({confirmed_ltc:.8f} LTC, {REQUIRED_CONFIRMATIONS}承認)")
                    await _on_seller_ltc_confirmed(bot, order.id)
            else:
                if order.status != 'SELLER_LTC_DETECTED':
                    order.status = 'SELLER_LTC_DETECTED'
                    order.seller_sent_txid = latest_txid
                    await session.commit()
                    logger.info(f"EscrowOrder #{order.id}: LTC入金検知（承認待ち）({total_ltc:.8f} LTC)")
                    await _on_seller_ltc_detected(bot, order.id)


async def _on_seller_ltc_detected(bot, order_id: int):
    """入金検知時: チケットチャンネルに「承認待ち」通知を送る"""
    async with AsyncSessionLocal() as session:
        order = await session.get(EscrowOrder, order_id)
        if not order or not order.ticket_channel_id:
            return

    channel = bot.get_channel(int(order.ticket_channel_id))
    if not channel:
        return

    await channel.send(
        f"🔍 **LTCの全額入金を検知しました。ブロックチェーンの承認を待っています...**\n"
        f"必要なLTCが全額送金されたため、送金期限は気にしなくて大丈夫です。\n"
        f"TxID: `{order.seller_sent_txid}`\n"
        f"（通常1〜数分で着金完了となります）"
    )

async def _on_seller_ltc_partial(bot, order_id: int, detected: Decimal, shortage: Decimal, txid: str):
    """一部入金検知時: チケットチャンネルに不足額通知を送る"""
    async with AsyncSessionLocal() as session:
        order = await session.get(EscrowOrder, order_id)
        if not order or not order.ticket_channel_id:
            return

    channel = bot.get_channel(int(order.ticket_channel_id))
    if not channel:
        return

    await channel.send(
        f"⚠️ **LTCの一部入金を検知しましたが、金額が足りません。**\n"
        f"検知額: `{detected:.8f} LTC` / 不足額: **`{shortage:.8f} LTC`**\n"
        f"TxID: `{txid}`\n"
        f"表示されている専用エスクローアドレスへ、残りの不足額を追加で送金してください。\n"
        f"全額揃うまで取引は進行しません。"
    )


async def _on_seller_ltc_confirmed(bot, order_id: int):
    """
    LTC着金承認完了時の処理:
    1. チケットに確認通知
    2. 買い手にLTC受取アドレス入力を促すビューを表示
    3. ステータスを WAITING_PAYMENT へ
    """
    from cogs.escrow_ticket import BuyerAddressView

    async with AsyncSessionLocal() as session:
        order = await session.get(EscrowOrder, order_id)
        if not order:
            return

        # WAITING_PAYMENT に移行
        order.status = 'WAITING_PAYMENT'
        
        # adを直接取得
        ad = await session.get(EscrowAd, order.ad_id)
        timeout_mins = ad.timeout_mins if ad else 30
        
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=timeout_mins)
        order.expires_at = expires_at
        await session.commit()

        channel_id = order.ticket_channel_id
        buyer_id = order.buyer_id
        seller_id = order.seller_id
        amount_ltc = quantize_ltc(order.amount_ltc)
        net_ltc = quantize_ltc(order.net_ltc)
        expires_unix = int(expires_at.timestamp())

    if not channel_id:
        return

    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    import discord
    embed = discord.Embed(
        title="LTCの着金が確認されました",
        description=(
            f"売り手からのLTCが確認されました。\n\n"
            f"**エスクロー受領額:** `{amount_ltc:.8f} LTC`\n"
            f"**買い手への送金額:** `{net_ltc:.8f} LTC`（手数料差し引き後）\n\n"
            f"**<@{buyer_id}> は下のボタンから LTC受取アドレスを登録し、**\n"
            f"**<@{seller_id}> が指定した方法でJPY支払いを行ってください。**\n\n"
            f"支払い期限: <t:{expires_unix}:R>"
        ),
        color=discord.Color.green()
    )

    view = BuyerAddressView(order_id=order_id, buyer_id=buyer_id, seller_id=seller_id)
    await channel.send(f"<@{buyer_id}> <@{seller_id}>", embed=embed, view=view)


async def _check_seller_ltc_timeouts(bot):
    """
    WAITING_SELLER_LTC のまま seller_ltc_deadline を過ぎた注文を自動キャンセルする。
    """
    # EscrowAddressテーブルへの戻し処理もここで行う
    from models import EscrowAddress
    
    async with AsyncSessionLocal() as session:
        stmt = select(EscrowOrder).where(
            EscrowOrder.status == 'WAITING_SELLER_LTC',
            EscrowOrder.seller_ltc_deadline < func.now()
        )
        result = await session.execute(stmt)
        expired = result.scalars().all()

        for order in expired:
            order.status = 'CANCELLED'
            order.ticket_delete_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
            
            # 使用したアドレスを未使用に戻す
            if order.escrow_address_id:
                addr_row = await session.get(EscrowAddress, order.escrow_address_id)
                if addr_row:
                    addr_row.is_in_use = False

            await session.commit()

            if order.ticket_channel_id:
                channel = bot.get_channel(int(order.ticket_channel_id))
                if channel:
                    await channel.send(
                        f"**売り手のLTC送金期限が切れたため、この取引は自動キャンセルされました。**\n"
                        f"このチャンネルは10分後に削除されます。"
                    )
