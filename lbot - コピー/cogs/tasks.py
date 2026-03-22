import discord
from discord.ext import commands, tasks
import datetime
import os
from sqlalchemy import select
from sqlalchemy.sql import func
from database import AsyncSessionLocal
from models import Order, Ad, User, EscrowOrder, EscrowAddress
from utils.ticket_system import generate_ticket_html
from utils.decimal_utils import quantize_ltc

class TasksCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))
        self.check_expired_orders.start()

    def cog_unload(self):
        self.check_expired_orders.cancel()

    @tasks.loop(seconds=60)
    async def check_expired_orders(self):
        await self.bot.wait_until_ready()
        now = datetime.datetime.now(datetime.timezone.utc)

        # --- 1. キャンセル処理 (期限切れ) ---
        async with AsyncSessionLocal() as session:
            stmt_timeout = select(Order.id).where(
                Order.status == 'PENDING',
                Order.expires_at < func.now()
            )
            expired_order_ids = (await session.execute(stmt_timeout)).scalars().all()

        for order_id in expired_order_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_cancel = await session.get(Order, order_id, with_for_update=True)
                    if order_to_cancel and order_to_cancel.status == 'PENDING':
                        order_to_cancel.status = 'CANCELLED'
                        order_to_cancel.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)
                        
                        seller = await session.get(User, order_to_cancel.seller_id, with_for_update=True)
                        if seller:
                            total_refund = quantize_ltc(order_to_cancel.amount_ltc) + quantize_ltc(order_to_cancel.fee_ltc)
                            seller.locked_balance = quantize_ltc(seller.locked_balance) - total_refund
                            seller.available_balance = quantize_ltc(seller.available_balance) + total_refund

                        if order_to_cancel.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_cancel.ticket_channel_id))
                            if ticket_channel:
                                await ticket_channel.send("**時間切れのため、この取引は自動キャンセルされました。**\n売り手のLTCロックは解除されました。このチャンネルは10分後に削除され、ログがDMに送信されます。")
                        
                        if self.MARKET_CHANNEL_ID:
                            from cogs.market import refresh_market_panels
                            await refresh_market_panels(self.bot)

        # --- 2. 警告処理 (残り5分以下で未警告) ---
        async with AsyncSessionLocal() as session:
            stmt_warning = select(Order.id).where(
                Order.status == 'PENDING',
                Order.warned_timeout == False,
                Order.expires_at < func.now() + datetime.timedelta(minutes=5)
            )
            warning_order_ids = (await session.execute(stmt_warning)).scalars().all()

        for order_id in warning_order_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_warn = await session.get(Order, order_id, with_for_update=True)
                    if order_to_warn and not order_to_warn.warned_timeout:
                        order_to_warn.warned_timeout = True
                        if order_to_warn.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_warn.ticket_channel_id))
                            if ticket_channel:
                                time_remaining = (order_to_warn.expires_at - now).total_seconds() / 60.0
                                await ticket_channel.send(f"<@{order_to_warn.buyer_id}> **支払期限まで残り約{max(1, int(time_remaining))}分です！**\n支払いを済ませて「支払いました」ボタンを押してください。")

        # --- 3. チケット自動削除処理 (ticket_delete_at到達) ---
        async with AsyncSessionLocal() as session:
            stmt_delete = select(Order.id).where(
                Order.ticket_delete_at.is_not(None),
                Order.ticket_delete_at < func.now()
            )
            delete_order_ids = (await session.execute(stmt_delete)).scalars().all()

        for order_id in delete_order_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_delete = await session.get(Order, order_id, with_for_update=True)
                    if not order_to_delete or not order_to_delete.ticket_delete_at:
                        continue
                        
                    order_to_delete.ticket_delete_at = None
                    
                    if order_to_delete.ticket_channel_id:
                        channel = self.bot.get_channel(int(order_to_delete.ticket_channel_id))
                        if isinstance(channel, discord.TextChannel):
                            try:
                                messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
                                html_content = await generate_ticket_html(
                                    messages, channel.guild, channel, int(order_to_delete.buyer_id),
                                    self.bot.user.id if self.bot.user else 0, self.bot
                                )
                                filename = f"ticket-{channel.name}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
                                filepath = f"logs/{filename}"
                                os.makedirs("logs", exist_ok=True)
                                with open(filepath, "w", encoding="utf-8") as f:
                                    f.write(html_content)
                                    
                                try:
                                    buyer = await self.bot.fetch_user(int(order_to_delete.buyer_id))
                                    await buyer.send(f"取引「{channel.name}」のログです。", file=discord.File(filepath, filename=filename))
                                except Exception as e:
                                    print(f"Failed to send log to buyer: {e}")
                                    
                                try:
                                    seller = await self.bot.fetch_user(int(order_to_delete.seller_id))
                                    await seller.send(f"取引「{channel.name}」のログです。", file=discord.File(filepath, filename=filename))
                                except Exception as e:
                                    print(f"Failed to send log to seller: {e}")
                                    
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                                    
                                await channel.delete()
                            except Exception as e:
                                print(f"Failed to delete channel or make log: {e}")

        # --- 4. 仲介型エスクロー取引のキャンセル処理 (WAITING_PAYMENT期限切れ) ---
        async with AsyncSessionLocal() as session:
            stmt_escrow_timeout = select(EscrowOrder.id).where(
                EscrowOrder.status == 'WAITING_PAYMENT',
                EscrowOrder.expires_at < func.now()
            )
            expired_escrow_ids = (await session.execute(stmt_escrow_timeout)).scalars().all()

        for order_id in expired_escrow_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_cancel = await session.get(EscrowOrder, order_id, with_for_update=True)
                    if order_to_cancel and order_to_cancel.status == 'WAITING_PAYMENT':
                        order_to_cancel.status = 'CANCELLED'
                        order_to_cancel.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

                        if order_to_cancel.escrow_address_id:
                            addr_row = await session.get(EscrowAddress, order_to_cancel.escrow_address_id)
                            if addr_row:
                                addr_row.is_in_use = False

                        if order_to_cancel.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_cancel.ticket_channel_id))
                            if ticket_channel:
                                await ticket_channel.send("**時間切れのため、この取引は自動キャンセルされました。**\nこのチャンネルは10分後に削除されます。LTCの返金が必要な場合は「Appeal」ボタン等から管理者に連絡してください。")

                        try:
                            from cogs.escrow_market import refresh_escrow_market_panels
                            await refresh_escrow_market_panels(self.bot)
                        except Exception as e:
                            print(f"Failed to refresh escrow market: {e}")

        # --- 5. 仲介型エスクロー取引の警告処理 ---
        async with AsyncSessionLocal() as session:
            stmt_escrow_warning = select(EscrowOrder.id).where(
                EscrowOrder.status == 'WAITING_PAYMENT',
                EscrowOrder.warned_timeout == False,
                EscrowOrder.expires_at < func.now() + datetime.timedelta(minutes=5)
            )
            warning_escrow_ids = (await session.execute(stmt_escrow_warning)).scalars().all()

        for order_id in warning_escrow_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_warn = await session.get(EscrowOrder, order_id, with_for_update=True)
                    if order_to_warn and not order_to_warn.warned_timeout:
                        order_to_warn.warned_timeout = True
                        if order_to_warn.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_warn.ticket_channel_id))
                            if ticket_channel:
                                time_remaining = (order_to_warn.expires_at - now).total_seconds() / 60.0
                                await ticket_channel.send(f"<@{order_to_warn.buyer_id}> **支払期限まで残り約{max(1, int(time_remaining))}分です！**\n支払いを済ませて「支払いました」ボタンを押してください。")

        # --- 6. 仲介型エスクローチケット自動削除 ---
        async with AsyncSessionLocal() as session:
            stmt_escrow_delete = select(EscrowOrder.id).where(
                EscrowOrder.ticket_delete_at.is_not(None),
                EscrowOrder.ticket_delete_at < func.now()
            )
            delete_escrow_ids = (await session.execute(stmt_escrow_delete)).scalars().all()

        for order_id in delete_escrow_ids:
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    order_to_delete = await session.get(EscrowOrder, order_id, with_for_update=True)
                    if not order_to_delete or not order_to_delete.ticket_delete_at:
                        continue
                        
                    order_to_delete.ticket_delete_at = None
                    if order_to_delete.ticket_channel_id:
                        channel = self.bot.get_channel(int(order_to_delete.ticket_channel_id))
                        if isinstance(channel, discord.TextChannel):
                            try:
                                await channel.delete()
                            except Exception as e:
                                print(f"Failed to delete escrow channel: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(TasksCog(bot))
