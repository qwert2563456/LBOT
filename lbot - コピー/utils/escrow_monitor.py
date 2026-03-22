"""
utils/escrow_monitor.py
仲介型取引のオンチェーンエスクロー入金を監視するバックグラウンドタスク。
既存の monitor_deposits_loop とは独立して動作する。
"""
import asyncio
import json
import logging
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from database import AsyncSessionLocal
from models import EscrowOrder, SystemConfig, get_or_create_user, EscrowAd
from utils.electrum import run_electrum_cmd, broadcast_withdrawal
from utils.price import fetch_ltc_jpy_price

logger = logging.getLogger(__name__)

REQUIRED_CONFIRMATIONS = 1  # 仲介型は1承認で進める（既存P2Pと同じ）
SELLER_LTC_TIMEOUT_MINS = 15  # 売り手のLTC送金期限（分）


async def monitor_escrow_loop(bot):
    """
    バックグラウンドタスク: エスクローアドレスへの入金を監視する。
    30秒ごとに WAITING_SELLER_LTC / SELLER_LTC_DETECTED の注文を確認する。
    """
    logger.info("仲介型エスクロー監視ループを開始します。")
    await asyncio.sleep(20)  # ボット起動完了を待つ

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
    エスクローアドレスへの入金を getaddressbalance で厳密にチェックする。
    要求額 (order.amount_ltc) に達しているか確認する。
    """
    async with AsyncSessionLocal() as session:
        stmt = select(EscrowOrder).where(
            EscrowOrder.status.in_(['WAITING_SELLER_LTC', 'SELLER_LTC_DETECTED'])
        )
        result = await session.execute(stmt)
        orders = result.scalars().all()

        if not orders:
            return

        for order in orders:
            addr = order.escrow_address
            if not addr:
                continue

            # アドレスの残高を取得
            raw_bal = await run_electrum_cmd("getaddressbalance", addr)
            if not raw_bal:
                continue

            try:
                bal_data = json.loads(raw_bal)
                if isinstance(bal_data, str):
                    bal_data = json.loads(bal_data)
                
                confirmed = float(bal_data.get("confirmed", 0))
                unconfirmed = float(bal_data.get("unconfirmed", 0))
                total_balance = confirmed + unconfirmed
            except Exception as e:
                logger.error(f"Failed to parse balance for {addr}: {e}")
                continue

            required_ltc = float(order.amount_ltc)

            # 要求額に達していない場合
            if total_balance < required_ltc:
                if total_balance > 0:
                    # 一部入金の検知と通知（新しいトランザクション時のみ）
                    txid = "N/A"
                    raw_hist = await run_electrum_cmd("getaddresshistory", addr)
                    if raw_hist:
                        try:
                            hist = json.loads(raw_hist)
                            if isinstance(hist, str):
                                hist = json.loads(hist)
                            if hist and isinstance(hist, list):
                                txid = hist[-1].get("tx_hash", "")
                        except Exception:
                            pass
                    
                    if txid != "N/A" and order.seller_sent_txid != txid:
                        order.seller_sent_txid = txid
                        await session.commit()
                        shortage = required_ltc - total_balance
                        logger.info(f"EscrowOrder #{order.id}: 一部入金検知 ({total_balance:.8f} LTC, 不足 {shortage:.8f} LTC)")
                        await _on_seller_ltc_partial(bot, order.id, total_balance, shortage, txid)
                continue

            # 達している場合、TxIDを取得するために履歴を参照
            txid = "N/A"
            raw_hist = await run_electrum_cmd("getaddresshistory", addr)
            if raw_hist:
                try:
                    hist = json.loads(raw_hist)
                    if isinstance(hist, str):
                        hist = json.loads(hist)
                    if hist and isinstance(hist, list):
                        # 最新のトランザクションハッシュを取得
                        txid = hist[-1].get("tx_hash", "")
                except Exception:
                    pass

            # REQUIRED_CONFIRMATIONS (通常1) を満たすのは、confirmed が required_ltc 以上の場合とする
            # (※仲介型は1承認で進めるため、confirmed残高で判断する)
            if confirmed >= required_ltc:
                if order.status != 'SELLER_LTC_CONFIRMED':
                    order.status = 'SELLER_LTC_CONFIRMED'
                    order.seller_sent_txid = txid
                    await session.commit()
                    logger.info(f"EscrowOrder #{order.id}: LTC着金確認完了 ({total_balance:.8f} LTC)")
                    await _on_seller_ltc_confirmed(bot, order.id)
            else:
                if order.status != 'SELLER_LTC_DETECTED':
                    order.status = 'SELLER_LTC_DETECTED'
                    order.seller_sent_txid = txid
                    await session.commit()
                    logger.info(f"EscrowOrder #{order.id}: LTC入金検知（承認待ち）({total_balance:.8f} LTC)")
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

async def _on_seller_ltc_partial(bot, order_id: int, detected: float, shortage: float, txid: str):
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
        amount_ltc = float(order.amount_ltc)
        net_ltc = float(order.net_ltc)
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
