import discord
from discord.ext import commands, tasks
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from database import AsyncSessionLocal
from models import Order, Ad, User
from utils.ticket_system import generate_ticket_html
import os
import os

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
        
        async with AsyncSessionLocal() as session:
            now = datetime.datetime.now(datetime.timezone.utc)

            # --- 1. キャンセル処理 (期限切れ) ---
            # DB負荷を軽減するため、サーバー時刻ベースで直接検索
            stmt_timeout = select(Order).where(
                Order.status == 'PENDING',
                Order.expires_at < func.now()
            )
            expired_orders = (await session.execute(stmt_timeout)).scalars().all()

            for order in expired_orders:
                async with session.begin_nested():
                    order_to_cancel = await session.get(Order, order.id, with_for_update=True)
                    if order_to_cancel and order_to_cancel.status == 'PENDING':
                        order_to_cancel.status = 'CANCELLED'
                        order_to_cancel.ticket_delete_at = func.now() + datetime.timedelta(minutes=10) # 10分後に自動削除
                        
                        # 売り手のロック残高を解除
                        seller = await session.get(User, order_to_cancel.seller_id, with_for_update=True)
                        if seller:
                            total_refund = float(order_to_cancel.amount_ltc) + float(order_to_cancel.fee_ltc)
                            seller.locked_balance = float(seller.locked_balance) - total_refund
                            seller.available_balance = float(seller.available_balance) + total_refund

                        # チケットチャンネルに通知
                        if order_to_cancel.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_cancel.ticket_channel_id))
                            if ticket_channel:
                                await ticket_channel.send("**時間切れのため、この取引は自動キャンセルされました。**\n売り手のLTCロックは解除されました。このチャンネルは10分後に削除され、ログがDMに送信されます。")
                        
                        if self.MARKET_CHANNEL_ID:
                            from cogs.market import refresh_market_panels
                            await refresh_market_panels(self.bot)

            # --- 2. 警告処理 (残り5分以下で未警告) ---
            stmt_warning = select(Order).where(
                Order.status == 'PENDING',
                Order.warned_timeout == False,
                Order.expires_at < func.now() + datetime.timedelta(minutes=5)
            )
            warning_orders = (await session.execute(stmt_warning)).scalars().all()

            for order in warning_orders:
                async with session.begin_nested():
                    order_to_warn = await session.get(Order, order.id, with_for_update=True)
                    if order_to_warn and not order_to_warn.warned_timeout:
                        order_to_warn.warned_timeout = True
                        
                        # チケットチャンネルへメンション
                        if order_to_warn.ticket_channel_id:
                            ticket_channel = self.bot.get_channel(int(order_to_warn.ticket_channel_id))
                            if ticket_channel:
                                time_remaining = (order_to_warn.expires_at - now).total_seconds() / 60.0
                                await ticket_channel.send(f"<@{order_to_warn.buyer_id}> **支払期限まで残り約{max(1, int(time_remaining))}分です！**\n支払いを済ませて「支払いました」ボタンを押してください。")

            # --- 3. チケット自動削除処理 (ticket_delete_at到達) ---
            stmt_delete = select(Order).where(
                Order.ticket_delete_at.is_not(None),
                Order.ticket_delete_at < func.now()
            )
            delete_orders = (await session.execute(stmt_delete)).scalars().all()

            for order in delete_orders:
                async with session.begin_nested():
                    order_to_delete = await session.get(Order, order.id, with_for_update=True)
                    if not order_to_delete or not order_to_delete.ticket_delete_at:
                        continue
                        
                    order_to_delete.ticket_delete_at = None # 重複実行を防ぐ
                    
                    if order_to_delete.ticket_channel_id:
                        channel = self.bot.get_channel(int(order_to_delete.ticket_channel_id))
                        if isinstance(channel, discord.TextChannel):
                            try:
                                # メッセージ履歴取得 (制限なし、古い順)
                                messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
                                
                                # HTML生成
                                html_content = await generate_ticket_html(
                                    messages,
                                    channel.guild,
                                    channel,
                                    int(order_to_delete.buyer_id),
                                    self.bot.user.id if self.bot.user else 0, # Closer ID
                                    self.bot
                                )
                                
                                filename = f"ticket-{channel.name}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
                                filepath = f"logs/{filename}"
                                os.makedirs("logs", exist_ok=True)
                                
                                with open(filepath, "w", encoding="utf-8") as f:
                                    f.write(html_content)
                                    
                                # 送信
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
                                    
                                # ファイル削除
                                if os.path.exists(filepath):
                                    os.remove(filepath)
                                    
                                # チャンネル削除
                                await channel.delete()
                            except Exception as e:
                                print(f"Failed to delete channel or make log: {e}")

            await session.commit()

async def setup(bot: commands.Bot):
    await bot.add_cog(TasksCog(bot))
