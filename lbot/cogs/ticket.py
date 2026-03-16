import discord
from discord.ext import commands
import os
import asyncio
from typing import Optional
from database import AsyncSessionLocal
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from decouple import config
from models import Order, User, SystemConfig, Transaction

TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))

class TicketView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

    @discord.ui.button(label="支払いました", style=discord.ButtonStyle.success, custom_id="ticket_paid_btn")
    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("このボタンは購入者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()
        
        async with AsyncSessionLocal() as session:
             order = await session.get(Order, self.order_id)
             if not order or order.status != 'PENDING':
                 await interaction.followup.send("注文が見つからないか、既に決済処理されています。", ephemeral=True)
                 return
                 
             order.status = 'PAID'
             order.paid_at = func.now()
             await session.commit()
        
        button.disabled = True
        button.label = "支払い報告済み"
        button.style = discord.ButtonStyle.secondary
        
        # 販売者向けリリースボタンを有効化する新しいView
        release_view = ReleaseView(self.order_id, self.buyer_id, self.seller_id)
        
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(f"<@{self.seller_id}> 購入者が支払いを報告しました。着金を確認後、LTCをリリースしてください。", view=release_view) # type: ignore

    @discord.ui.button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger, custom_id="ticket_appeal_btn")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) not in [self.buyer_id, self.seller_id] and not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if not order:
                 await interaction.followup.send("注文が見つかりません。", ephemeral=True)
                 return
            order.status = 'APPEALED'
            await session.commit()
            
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
            
        admin_view = AdminResolutionView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.channel.send(f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。取引は一時停止されボタンがロックされました。**\n関係者は直ちに詳細を報告し、管理者の裁定をお待ちください。", view=admin_view) # type: ignore

    @discord.ui.button(label="キャンセル (Cancel)", style=discord.ButtonStyle.secondary, custom_id="ticket_cancel_btn")
    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) not in [self.buyer_id, self.seller_id] and not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        async with AsyncSessionLocal() as session:
            async with session.begin(): # トランザクション
                 # For update をかけて2重処理防止
                 stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                 res = await session.execute(stmt_order)
                 order = res.scalar_one_or_none()
                 
                 if not order or order.status in ['COMPLETED', 'CANCELLED', 'APPEALED']:
                     await interaction.followup.send("この注文はキャンセル処理の対象外（完了/異議あり等）です。", ephemeral=True)
                     return
                     
                 if order.status == 'PAID' and str(interaction.user.id) == self.seller_id:
                     await interaction.followup.send("⚠️ 詐欺防止: 購入者がすでに「支払いました」と報告しているため、販売者が一方的にキャンセルすることはできません。異議がある場合は『Appeal (異議申立)』を押してください。", ephemeral=True)
                     return
                 
                 order.status = 'CANCELLED'
                 # 削除予約 (10分後)
                 order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)
                 
                 # 販売者のロックを解除
                 stmt_seller = select(User).where(User.discord_id == self.seller_id).with_for_update()
                 total_refund = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                 seller.locked_balance = round(float(seller.locked_balance) - total_refund, 8)
                 seller.available_balance = round(float(seller.available_balance) + total_refund, 8)

                 # キャンセルした側のみペナルティ（total_tradesは維持、完了しないため勝手に%が下がる）
                 # キャンセルしていない側はtotal_tradesを-1して母数を減らし、%が下がらないように救済する
                 stmt_buyer = select(User).where(User.discord_id == self.buyer_id).with_for_update()
                 buyer = (await session.execute(stmt_buyer)).scalar_one()

                 if str(interaction.user.id) == self.seller_id:
                     # 販売者がキャンセル -> 購入者を救済
                     if buyer.total_trades > 0:
                         buyer.total_trades -= 1
                 elif str(interaction.user.id) == self.buyer_id:
                     # 購入者がキャンセル -> 販売者を救済
                     if seller.total_trades > 0:
                         seller.total_trades -= 1
                 else:
                     # 管理者がキャンセル -> どっちも悪くないかもしれないので一応両方救済する
                     if seller.total_trades > 0:
                         seller.total_trades -= 1
                     if buyer.total_trades > 0:
                         buyer.total_trades -= 1
                 
        await interaction.channel.send("🚫 **取引がキャンセルされました。**\nロックされていたLTCは販売者に返還されました。このチャンネルは後ほど削除されます。") # type: ignore
        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        
        # マーケット更新（正しいチャンネルIDを使用）
        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)

class ReleaseView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

    @discord.ui.button(label="着金確認 ＆ LTCリリース", style=discord.ButtonStyle.primary, custom_id="ticket_release_btn")
    async def release_ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 重大なセキュリティ修正: 販売者(または管理者)のみがリリースできるように厳格化
        if str(interaction.user.id) != self.seller_id and not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("⚠️ 詐欺防止: このボタンは**販売者(または管理者)**のみが押せます。購入者が押すことはできません。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
             async with session.begin():
                 # 1. オーダーを取得 (FOR UPDATE)
                 stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                 res = await session.execute(stmt_order)
                 order = res.scalar_one_or_none()
                 
                 if not order or order.status == 'COMPLETED':
                     await interaction.followup.send("この取引は既に完了しているか無効です。", ephemeral=True)
                     return
                     
                 # 2. ユーザー情報の取得 (FOR UPDATE)
                 stmt_seller = select(User).where(User.discord_id == self.seller_id).with_for_update()
                 seller = (await session.execute(stmt_seller)).scalar_one()
                 
                 stmt_buyer = select(User).where(User.discord_id == self.buyer_id).with_for_update()
                 buyer = (await session.execute(stmt_buyer)).scalar_one()
                 
                 # 3. リリース処理
                 # Sellerからロック分を引く
                 total_locked = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                 seller.locked_balance = round(float(seller.locked_balance) - total_locked, 8)
                 seller.completed_trades += 1
                 
                 # Buyerへ追加 (+ Transaction履歴生成)
                 buyer.available_balance = round(float(buyer.available_balance) + float(order.amount_ltc), 8)
                 buyer.completed_trades += 1
                 
                 tx_buyer = Transaction(
                     user_id=self.buyer_id,
                     tx_type="P2P_BUY",
                     amount_ltc=float(order.amount_ltc),
                     confirmations=1
                 )
                 session.add(tx_buyer)
                 
                 tx_seller = Transaction(
                     user_id=self.seller_id,
                     tx_type="P2P_SELL",
                     amount_ltc=-float(order.amount_ltc),
                     confirmations=1
                 )
                 session.add(tx_seller)

                 # 4. 手数料(0.2%)の計上
                 stmt_config = select(SystemConfig).with_for_update() # Only 1 row usually
                 config = (await session.execute(stmt_config)).scalar_one_or_none()
                 if not config:
                     config = SystemConfig(collected_fees_ltc=0.0)
                     session.add(config)
                 config.collected_fees_ltc = round(float(config.collected_fees_ltc) + float(order.fee_ltc), 8)
                 
                 order.status = 'COMPLETED'
                 # 削除予約 (10分後)
                 order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)
                 
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send("✅ **取引が完了しました！**\nLTCが購入者にリリースされました。このチケットは10分後に削除され、ログがDMに送信されます。") # type: ignore
        
        # マーケット更新（正しいチャンネルIDを使用）
        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)

    @discord.ui.button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger, custom_id="ticket_appeal_btn2")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) not in [self.buyer_id, self.seller_id] and not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if order:
                order.status = 'APPEALED'
                await session.commit()
            
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
            
        admin_view = AdminResolutionView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.channel.send(f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。取引は一時停止されボタンがロックされました。**\n関係者は直ちに詳細を報告し、管理者の裁定をお待ちください。", view=admin_view) # type: ignore

class AdminResolutionView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

    @discord.ui.button(label="[Admin] 強制リリース", style=discord.ButtonStyle.success, custom_id="admin_force_release")
    async def force_release(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("このボタンは管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
             async with session.begin():
                 stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                 res = await session.execute(stmt_order)
                 order = res.scalar_one_or_none()
                 
                 if not order or order.status == 'COMPLETED':
                     await interaction.followup.send("この取引は既に完了しているか無効です。", ephemeral=True)
                     return
                     
                 stmt_seller = select(User).where(User.discord_id == self.seller_id).with_for_update()
                 seller = (await session.execute(stmt_seller)).scalar_one()
                 
                 stmt_buyer = select(User).where(User.discord_id == self.buyer_id).with_for_update()
                 buyer = (await session.execute(stmt_buyer)).scalar_one()
                 
                 total_locked = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                 seller.locked_balance = round(float(seller.locked_balance) - total_locked, 8)
                 
                 buyer.available_balance = round(float(buyer.available_balance) + float(order.amount_ltc), 8)
                 
                 tx_buyer = Transaction(user_id=self.buyer_id, tx_type="P2P_BUY_ADMIN", amount_ltc=float(order.amount_ltc), confirmations=1)
                 session.add(tx_buyer)
                 tx_seller = Transaction(user_id=self.seller_id, tx_type="P2P_SELL_ADMIN", amount_ltc=-float(order.amount_ltc), confirmations=1)
                 session.add(tx_seller)

                 stmt_config = select(SystemConfig).with_for_update()
                 config = (await session.execute(stmt_config)).scalar_one_or_none()
                 if not config:
                     config = SystemConfig(collected_fees_ltc=0.0)
                     session.add(config)
                 config.collected_fees_ltc = round(float(config.collected_fees_ltc) + float(order.fee_ltc), 8)
                 
                 order.status = 'COMPLETED'
                 order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)
                 
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send("**管理者が【強制リリース】を実行しました。**\nLTCは購入者に引き渡されました。チケットは10分後に削除され、ログがDMに送信されます。") # type: ignore

    @discord.ui.button(label="[Admin] 強制キャンセル", style=discord.ButtonStyle.danger, custom_id="admin_force_cancel")
    async def force_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', [])):
            await interaction.response.send_message("このボタンは管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                 stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                 res = await session.execute(stmt_order)
                 order = res.scalar_one_or_none()
                 
                 if not order or order.status in ['COMPLETED', 'CANCELLED']:
                     await interaction.followup.send("この注文はすでにキャンセルまたは完了しています。", ephemeral=True)
                     return
                 
                 order.status = 'CANCELLED'
                 order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)
                 
                 stmt_seller = select(User).where(User.discord_id == self.seller_id).with_for_update()
                 res_seller = await session.execute(stmt_seller)
                 seller = res_seller.scalar_one()
                 
                 total_refund = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                 seller.locked_balance = round(float(seller.locked_balance) - total_refund, 8)
                 seller.available_balance = round(float(seller.available_balance) + total_refund, 8)
                 
                 # キャンセルした側のみペナルティ（完了扱いにならないため勝手に%が下がる）
                 # 相手側はtotal_tradesを-1して母数を減らし、%が維持されるように救済
                 stmt_buyer = select(User).where(User.discord_id == self.buyer_id).with_for_update()
                 buyer = (await session.execute(stmt_buyer)).scalar_one()

                 if str(interaction.user.id) == self.seller_id:
                     # 販売者がキャンセル -> 購入者を救済
                     if buyer.total_trades > 0:
                         buyer.total_trades -= 1
                 elif str(interaction.user.id) == self.buyer_id:
                     # 購入者がキャンセル -> 販売者を救済
                     if seller.total_trades > 0:
                         seller.total_trades -= 1
                 else:
                     # 第三者（管理者等）がキャンセル -> 双方救済する
                     if seller.total_trades > 0:
                         seller.total_trades -= 1
                     if buyer.total_trades > 0:
                         buyer.total_trades -= 1
                 
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send("**管理者が【強制キャンセル】を実行しました。**\nロックされていたLTCは販売者に返還されました。チケットは10分後に削除され、ログがDMに送信されます。") # type: ignore


async def create_ticket_channel(
    guild: discord.Guild,
    seller_member: discord.Member,
    buyer: discord.User,
    order_id: int,
    amount_jpy: int,
    amount_ltc: float,
    margin_percent: float,
    min_amount_jpy: int,
    max_amount_jpy: int,
    terms: str,
    timeout_mins: int,
    welcome_message: str,
    expires_at_unix: int
) -> Optional[discord.TextChannel]:
    """購入ボタン押下時に呼び出され、チケットチャンネルを作成する"""
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    seller_id = seller_member.id
    
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        buyer: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        seller_member: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        guild.get_role(ADMIN_ROLE_ID): discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True) # type: ignore
    }

    # remove None types
    overwrites = {k: v for k, v in overwrites.items() if k is not None}
    
    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{order_id}",
            category=category,
            overwrites=overwrites
        )
        
        terms_display = terms if terms else "特になし"
        
        embed = discord.Embed(
            title="P2P 取引が開始されました",
            description=(
                f"**購入者:** {buyer.mention}\n"
                f"**販売者:** <@{seller_id}>\n\n"
                f"**取引内容:**\n"
                f"支払い額: `{amount_jpy:,.0f} JPY`\n"
                f"受取額: `{amount_ltc:.8f} LTC`\n\n"
                f"**決済条件・注意事項:**\n"
                f"```\n{terms_display}\n```\n"
                f"⚠️ 購入者は指定された方法で送金を行い、完了したら「支払いました」ボタンを押してください。\n"
                f"🕒 **支払いは <t:{expires_at_unix}:R> までに完了させてください。**"
            ),
            color=discord.Color.gold()
        )
        
        view = TicketView(order_id, str(buyer.id), str(seller_id))
        # Store view globally if we want it to persist, or reconstruct dynamically
        await channel.send(f"{buyer.mention} <@{seller_id}>", embed=embed, view=view)
        
        if welcome_message and str(welcome_message).strip():
            welcome_embed = discord.Embed(
                title="販売者からのメッセージ",
                description=str(welcome_message),
                color=discord.Color.blue()
            )
            await channel.send(embed=welcome_embed)
            
        return channel
        
    except Exception as e:
        print(f"Failed to create ticket: {e}")
        return None

class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # self.bot.add_view(TicketView(...)) 

async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
