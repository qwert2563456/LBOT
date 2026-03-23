import discord
from discord.ext import commands
import os
from typing import Optional
from database import AsyncSessionLocal
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from models import Order, User, SystemConfig, Transaction, Ledger
from utils.decimal_utils import quantize_ltc
from decimal import Decimal

TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
ADMIN_ROLE_ID      = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID  = int(os.getenv("MARKET_CHANNEL_ID", "0"))


class TicketView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        btn_paid = discord.ui.Button(label="支払いました", style=discord.ButtonStyle.success,
                                     custom_id=f"ticket_paid_{order_id}")
        btn_paid.callback = self.confirm_payment
        self.add_item(btn_paid)

        btn_appeal = discord.ui.Button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger,
                                       custom_id=f"ticket_appeal_{order_id}")
        btn_appeal.callback = self.appeal
        self.add_item(btn_appeal)

        btn_cancel = discord.ui.Button(label="キャンセル (Cancel)", style=discord.ButtonStyle.secondary,
                                       custom_id=f"ticket_cancel_{order_id}")
        btn_cancel.callback = self.cancel_order
        self.add_item(btn_cancel)

    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("このボタンは購入者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if not order or order.status != "PENDING":
                await interaction.followup.send("注文が見つからないか、既に決済処理されています。", ephemeral=True)
                return
            order.status  = "PAID"
            order.paid_at = func.now()
            await session.commit()

        button.disabled = True
        button.label    = "支払い報告済み"
        button.style    = discord.ButtonStyle.secondary

        release_view = ReleaseView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            f"<@{self.seller_id}> 購入者が支払いを報告しました。着金を確認後、LTCをリリースしてください。",
            view=release_view,
        )

    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()
        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if not order:
                await interaction.followup.send("注文が見つかりません。", ephemeral=True)
                return
            order.status = "APPEALED"
            await session.commit()

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。取引は一時停止されました。**",
            view=AdminResolutionView(self.order_id, self.buyer_id, self.seller_id),
        )

    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = (await session.execute(
                    select(Order).where(Order.id == self.order_id).with_for_update()
                )).scalar_one_or_none()

                if not order or order.status in ["COMPLETED", "CANCELLED", "APPEALED"]:
                    await interaction.followup.send("この注文はキャンセル処理の対象外です。", ephemeral=True)
                    return

                if order.status == "PAID" and str(interaction.user.id) == self.seller_id:
                    await interaction.followup.send(
                        "詐欺防止: 購入者がすでに支払い報告済みのため、販売者が一方的にキャンセルできません。\n"
                        "異議がある場合は「Appeal」を押してください。", ephemeral=True,
                    )
                    return

                order.status          = "CANCELLED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

                seller = (await session.execute(
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )).scalar_one()
                buyer = (await session.execute(
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )).scalar_one()

                amount_ltc   = quantize_ltc(order.amount_ltc)
                fee_ltc      = quantize_ltc(order.fee_ltc)
                total_refund = amount_ltc + fee_ltc

                seller.locked_balance    = quantize_ltc(seller.locked_balance or Decimal("0")) - total_refund
                seller.available_balance = quantize_ltc(seller.available_balance or Decimal("0")) + total_refund

                # ★ Ledger: ロック解除（キャンセル返金）
                session.add(Ledger(
                    user_id=self.seller_id, type="REFUND",
                    amount_ltc=total_refund,
                    reference_id=str(self.order_id),
                    note="P2P Order Cancelled"
                ))

                # total_trades の救済処理
                canceller = str(interaction.user.id)
                if canceller == self.seller_id:
                    if buyer.total_trades > 0:
                        buyer.total_trades -= 1
                elif canceller == self.buyer_id:
                    if seller.total_trades > 0:
                        seller.total_trades -= 1
                else:
                    if seller.total_trades > 0:
                        seller.total_trades -= 1
                    if buyer.total_trades > 0:
                        buyer.total_trades -= 1

        await interaction.channel.send(
            "**取引がキャンセルされました。**\nロックされていたLTCは販売者に返還されました。"
        )
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)


class ReleaseView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        btn_release = discord.ui.Button(label="着金確認 ＆ LTCリリース", style=discord.ButtonStyle.primary,
                                        custom_id=f"ticket_release_{order_id}")
        btn_release.callback = self.release_ltc
        self.add_item(btn_release)

        btn_appeal = discord.ui.Button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger,
                                       custom_id=f"ticket_release_appeal_{order_id}")
        btn_appeal.callback = self.appeal
        self.add_item(btn_appeal)

    async def release_ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_seller = str(interaction.user.id) == self.seller_id
        is_admin  = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_seller or is_admin):
            await interaction.response.send_message(
                "詐欺防止: このボタンは**販売者（または管理者）**のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = (await session.execute(
                    select(Order).where(Order.id == self.order_id).with_for_update()
                )).scalar_one_or_none()

                if not order or order.status == "COMPLETED":
                    await interaction.followup.send("この取引は既に完了しているか無効です。", ephemeral=True)
                    return

                seller = (await session.execute(
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )).scalar_one()
                buyer = (await session.execute(
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )).scalar_one()

                amount_ltc   = quantize_ltc(order.amount_ltc)
                fee_ltc      = quantize_ltc(order.fee_ltc)
                total_locked = amount_ltc + fee_ltc

                # 売り手: ロック解除（売却完了）
                seller.locked_balance = quantize_ltc(seller.locked_balance or Decimal("0")) - total_locked
                seller.completed_trades += 1

                # 買い手: LTC付与
                buyer.available_balance = quantize_ltc(buyer.available_balance or Decimal("0")) + amount_ltc
                buyer.completed_trades += 1

                # トランザクションログ
                session.add(Transaction(user_id=self.buyer_id,  tx_type="P2P_BUY",
                                        amount_ltc=amount_ltc,  confirmations=1))
                session.add(Transaction(user_id=self.seller_id, tx_type="P2P_SELL",
                                        amount_ltc=-amount_ltc, confirmations=1))

                # ★ Ledger: 売り手（売却 + 手数料）・買い手（購入）
                session.add(Ledger(user_id=self.seller_id, type="P2P_SELL",
                                   amount_ltc=-amount_ltc, reference_id=str(self.order_id),
                                   note="P2P Sale"))
                session.add(Ledger(user_id=self.seller_id, type="FEE",
                                   amount_ltc=-fee_ltc, reference_id=str(self.order_id),
                                   note="P2P Platform Fee"))
                session.add(Ledger(user_id=self.buyer_id, type="P2P_BUY",
                                   amount_ltc=amount_ltc, reference_id=str(self.order_id),
                                   note="P2P Purchase"))

                # 手数料計上
                config = (await session.execute(
                    select(SystemConfig).with_for_update()
                )).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal("0"))
                    session.add(config)
                config.collected_fees_ltc = quantize_ltc(
                    (config.collected_fees_ltc or Decimal("0")) + fee_ltc
                )

                order.status           = "COMPLETED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            "**取引が完了しました！** LTCが購入者にリリースされました。\n"
            "このチケットは10分後に削除されます。"
        )

        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)

    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()
        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if order:
                order.status = "APPEALED"
                await session.commit()

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。**",
            view=AdminResolutionView(self.order_id, self.buyer_id, self.seller_id),
        )


class AdminResolutionView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        btn_release = discord.ui.Button(label="[Admin] 強制リリース", style=discord.ButtonStyle.success,
                                        custom_id=f"admin_force_release_{order_id}")
        btn_release.callback = self.force_release
        self.add_item(btn_release)

        btn_cancel = discord.ui.Button(label="[Admin] 強制キャンセル", style=discord.ButtonStyle.danger,
                                       custom_id=f"admin_force_cancel_{order_id}")
        btn_cancel.callback = self.force_cancel
        self.add_item(btn_cancel)

    async def force_release(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("このボタンは管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = (await session.execute(
                    select(Order).where(Order.id == self.order_id).with_for_update()
                )).scalar_one_or_none()

                if not order or order.status == "COMPLETED":
                    await interaction.followup.send("この取引は既に完了しているか無効です。", ephemeral=True)
                    return

                seller = (await session.execute(
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )).scalar_one()
                buyer = (await session.execute(
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )).scalar_one()

                amount_ltc   = quantize_ltc(order.amount_ltc)
                fee_ltc      = quantize_ltc(order.fee_ltc)
                total_locked = amount_ltc + fee_ltc

                seller.locked_balance   = quantize_ltc(seller.locked_balance or Decimal("0")) - total_locked
                buyer.available_balance = quantize_ltc(buyer.available_balance or Decimal("0")) + amount_ltc

                session.add(Transaction(user_id=self.buyer_id,  tx_type="P2P_BUY_ADMIN",
                                        amount_ltc=amount_ltc,  confirmations=1))
                session.add(Transaction(user_id=self.seller_id, tx_type="P2P_SELL_ADMIN",
                                        amount_ltc=-amount_ltc, confirmations=1))

                # ★ Ledger
                session.add(Ledger(user_id=self.seller_id, type="P2P_SELL",
                                   amount_ltc=-amount_ltc, reference_id=str(self.order_id),
                                   note="P2P Sale (Admin Force Release)"))
                session.add(Ledger(user_id=self.seller_id, type="FEE",
                                   amount_ltc=-fee_ltc, reference_id=str(self.order_id),
                                   note="P2P Platform Fee (Admin)"))
                session.add(Ledger(user_id=self.buyer_id, type="P2P_BUY",
                                   amount_ltc=amount_ltc, reference_id=str(self.order_id),
                                   note="P2P Purchase (Admin Force Release)"))

                config = (await session.execute(
                    select(SystemConfig).with_for_update()
                )).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal("0"))
                    session.add(config)
                config.collected_fees_ltc = quantize_ltc(
                    (config.collected_fees_ltc or Decimal("0")) + fee_ltc
                )

                order.status           = "COMPLETED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            "**管理者が【強制リリース】を実行しました。** LTCは購入者に引き渡されました。"
        )

    async def force_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message("このボタンは管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = (await session.execute(
                    select(Order).where(Order.id == self.order_id).with_for_update()
                )).scalar_one_or_none()

                if not order or order.status in ["COMPLETED", "CANCELLED"]:
                    await interaction.followup.send("この注文はすでにキャンセルまたは完了しています。", ephemeral=True)
                    return

                order.status           = "CANCELLED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

                seller = (await session.execute(
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )).scalar_one()
                buyer = (await session.execute(
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )).scalar_one()

                amount_ltc   = quantize_ltc(order.amount_ltc)
                fee_ltc      = quantize_ltc(order.fee_ltc)
                total_refund = amount_ltc + fee_ltc

                seller.locked_balance    = quantize_ltc(seller.locked_balance or Decimal("0")) - total_refund
                seller.available_balance = quantize_ltc(seller.available_balance or Decimal("0")) + total_refund

                # ★ Ledger
                session.add(Ledger(user_id=self.seller_id, type="REFUND",
                                   amount_ltc=total_refund, reference_id=str(self.order_id),
                                   note="P2P Order Force Cancelled (Admin)"))

                if seller.total_trades > 0:
                    seller.total_trades -= 1
                if buyer.total_trades > 0:
                    buyer.total_trades -= 1

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            "**管理者が【強制キャンセル】を実行しました。** ロックされていたLTCは販売者に返還されました。"
        )


# ── チケットチャンネル作成 ────────────────────────────────

async def create_ticket_channel(
    guild: discord.Guild,
    seller_member,
    buyer: discord.User,
    order_id: int,
    amount_jpy: int,
    amount_ltc: Decimal,
    margin_percent: Decimal,
    min_amount_jpy: int,
    max_amount_jpy: int,
    terms: str,
    timeout_mins: int,
    welcome_message: str,
    expires_at_unix: int,
) -> Optional[discord.TextChannel]:
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    seller_id = seller_member.id
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        buyer: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        seller_member: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        )

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{order_id}", category=category, overwrites=overwrites
        )

        embed = discord.Embed(
            title="P2P 取引が開始されました",
            description=(
                f"**購入者:** {buyer.mention}\n**販売者:** <@{seller_id}>\n\n"
                f"**取引内容:**\n"
                f"支払い額: `{amount_jpy:,.0f} JPY`\n"
                f"受取額: `{amount_ltc:.8f} LTC`\n\n"
                f"**決済条件:**\n```\n{terms or '特になし'}\n```\n"
                f"購入者は指定方法で送金後「支払いました」を押してください。\n"
                f"**支払い期限: <t:{expires_at_unix}:R>**"
            ),
            color=discord.Color.gold(),
        )

        await channel.send(
            f"{buyer.mention} <@{seller_id}>",
            embed=embed,
            view=TicketView(order_id, str(buyer.id), str(seller_id))
        )

        if welcome_message and str(welcome_message).strip():
            await channel.send(embed=discord.Embed(
                title="販売者からのメッセージ",
                description=str(welcome_message),
                color=discord.Color.blue(),
            ))

        return channel
    except Exception as e:
        print(f"[ticket] チャンネル作成失敗: {e}")
        return None


# ── TicketCog ────────────────────────────────────────────

class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Order).where(Order.status.in_(["PENDING", "PAID", "APPEALED"]))
            )
            active_orders = result.scalars().all()

        for order in active_orders:
            oid, bid, sid = order.id, str(order.buyer_id), str(order.seller_id)
            self.bot.add_view(TicketView(oid, bid, sid))
            self.bot.add_view(ReleaseView(oid, bid, sid))
            self.bot.add_view(AdminResolutionView(oid, bid, sid))

        if active_orders:
            print(f"[ticket] {len(active_orders)} 件のアクティブ注文のViewを再登録しました。")


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))