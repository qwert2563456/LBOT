import discord
from discord.ext import commands
import os
import asyncio
from typing import Optional
from database import AsyncSessionLocal
import datetime
from sqlalchemy import select
from sqlalchemy.sql import func
from models import Order, User, SystemConfig, Transaction

TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
ADMIN_ROLE_ID      = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID  = int(os.getenv("MARKET_CHANNEL_ID", "0"))


# ──────────────────────────────────────────────────────────────────────────────
# TicketView
#   custom_id にオーダーIDを埋め込むことで、ボット再起動後も正しいオーダーに
#   対してボタンが機能する（persistent view）。
# ──────────────────────────────────────────────────────────────────────────────

class TicketView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        # ── オーダーIDをカスタムIDに含めて永続化 ──
        btn_paid = discord.ui.Button(
            label="支払いました",
            style=discord.ButtonStyle.success,
            custom_id=f"ticket_paid_{order_id}",
        )
        btn_paid.callback = self.confirm_payment
        self.add_item(btn_paid)

        btn_appeal = discord.ui.Button(
            label="Appeal (異議申立)",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_appeal_{order_id}",
        )
        btn_appeal.callback = self.appeal
        self.add_item(btn_appeal)

        btn_cancel = discord.ui.Button(
            label="キャンセル (Cancel)",
            style=discord.ButtonStyle.secondary,
            custom_id=f"ticket_cancel_{order_id}",
        )
        btn_cancel.callback = self.cancel_order
        self.add_item(btn_cancel)

    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message(
                "このボタンは購入者のみが押せます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if not order or order.status != "PENDING":
                await interaction.followup.send(
                    "注文が見つからないか、既に決済処理されています。", ephemeral=True
                )
                return
            order.status  = "PAID"
            order.paid_at = func.now()
            await session.commit()

        button.disabled = True
        button.label    = "支払い報告済み"
        button.style    = discord.ButtonStyle.secondary

        release_view = ReleaseView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(  # type: ignore
            f"<@{self.seller_id}> 購入者が支払いを報告しました。"
            f"着金を確認後、LTCをリリースしてください。",
            view=release_view,
        )

    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin  = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message(
                "このボタンは取引当事者または管理者のみが押せます。", ephemeral=True
            )
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
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)

        admin_view = AdminResolutionView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.channel.send(  # type: ignore
            f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。取引は一時停止されました。**\n"
            f"関係者は詳細を報告し、管理者の裁定をお待ちください。",
            view=admin_view,
        )

    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin  = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message(
                "このボタンは取引当事者または管理者のみが押せます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                order = (await session.execute(stmt_order)).scalar_one_or_none()

                if not order or order.status in ["COMPLETED", "CANCELLED", "APPEALED"]:
                    await interaction.followup.send(
                        "この注文はキャンセル処理の対象外（完了 / 異議申立済み / キャンセル済み）です。",
                        ephemeral=True,
                    )
                    return

                # 購入者が支払い報告済みの場合、販売者が一方的にキャンセルできない
                if order.status == "PAID" and str(interaction.user.id) == self.seller_id:
                    await interaction.followup.send(
                        "詐欺防止: 購入者がすでに「支払いました」と報告しているため、"
                        "販売者が一方的にキャンセルすることはできません。\n"
                        "異議がある場合は『Appeal (異議申立)』を押してください。",
                        ephemeral=True,
                    )
                    return

                order.status          = "CANCELLED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

                # ── 販売者のロック残高を解除（★ seller を正しく取得）──
                stmt_seller = (
                    select(User)
                    .where(User.discord_id == self.seller_id)
                    .with_for_update()
                )
                seller = (await session.execute(stmt_seller)).scalar_one()

                total_refund = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                seller.locked_balance    = round(float(seller.locked_balance)    - total_refund, 8)
                seller.available_balance = round(float(seller.available_balance) + total_refund, 8)

                # ── 買い手を取得 ──
                stmt_buyer = (
                    select(User)
                    .where(User.discord_id == self.buyer_id)
                    .with_for_update()
                )
                buyer = (await session.execute(stmt_buyer)).scalar_one()

                # キャンセルしていない側の total_trades を -1 して救済
                canceller = str(interaction.user.id)
                if canceller == self.seller_id:
                    if buyer.total_trades > 0:
                        buyer.total_trades -= 1
                elif canceller == self.buyer_id:
                    if seller.total_trades > 0:
                        seller.total_trades -= 1
                else:  # 管理者
                    if seller.total_trades > 0:
                        seller.total_trades -= 1
                    if buyer.total_trades > 0:
                        buyer.total_trades -= 1

        await interaction.channel.send(  # type: ignore
            "**取引がキャンセルされました。**\n"
            "ロックされていたLTCは販売者に返還されました。"
            "このチャンネルは後ほど削除されます。"
        )
        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)

        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)


# ──────────────────────────────────────────────────────────────────────────────
# ReleaseView
# ──────────────────────────────────────────────────────────────────────────────

class ReleaseView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        btn_release = discord.ui.Button(
            label="着金確認 ＆ LTCリリース",
            style=discord.ButtonStyle.primary,
            custom_id=f"ticket_release_{order_id}",
        )
        btn_release.callback = self.release_ltc
        self.add_item(btn_release)

        btn_appeal = discord.ui.Button(
            label="Appeal (異議申立)",
            style=discord.ButtonStyle.danger,
            custom_id=f"ticket_release_appeal_{order_id}",
        )
        btn_appeal.callback = self.appeal
        self.add_item(btn_appeal)

    async def release_ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_seller = str(interaction.user.id) == self.seller_id
        is_admin   = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_seller or is_admin):
            await interaction.response.send_message(
                "詐欺防止: このボタンは**販売者（または管理者）**のみが押せます。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                order = (await session.execute(stmt_order)).scalar_one_or_none()

                if not order or order.status == "COMPLETED":
                    await interaction.followup.send(
                        "この取引は既に完了しているか無効です。", ephemeral=True
                    )
                    return

                stmt_seller = (
                    select(User)
                    .where(User.discord_id == self.seller_id)
                    .with_for_update()
                )
                seller = (await session.execute(stmt_seller)).scalar_one()

                stmt_buyer = (
                    select(User)
                    .where(User.discord_id == self.buyer_id)
                    .with_for_update()
                )
                buyer = (await session.execute(stmt_buyer)).scalar_one()

                # ロック解除
                total_locked = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                seller.locked_balance = round(float(seller.locked_balance) - total_locked, 8)
                seller.completed_trades += 1

                # 購入者へLTCを付与
                buyer.available_balance = round(
                    float(buyer.available_balance) + float(order.amount_ltc), 8
                )
                buyer.completed_trades += 1

                session.add(Transaction(
                    user_id=self.buyer_id,
                    tx_type="P2P_BUY",
                    amount_ltc=float(order.amount_ltc),
                    confirmations=1,
                ))
                session.add(Transaction(
                    user_id=self.seller_id,
                    tx_type="P2P_SELL",
                    amount_ltc=-float(order.amount_ltc),
                    confirmations=1,
                ))

                # 手数料計上
                stmt_config = select(SystemConfig).with_for_update()
                config = (await session.execute(stmt_config)).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=0.0)
                    session.add(config)
                config.collected_fees_ltc = round(
                    float(config.collected_fees_ltc) + float(order.fee_ltc), 8
                )

                order.status           = "COMPLETED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(  # type: ignore
            "**取引が完了しました！**\n"
            "LTCが購入者にリリースされました。"
            "このチケットは10分後に削除され、ログがDMに送信されます。"
        )

        if MARKET_CHANNEL_ID:
            from cogs.market import refresh_market_panels
            await refresh_market_panels(interaction.client)

    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin  = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
        if not (is_party or is_admin):
            await interaction.response.send_message(
                "このボタンは取引当事者または管理者のみが押せます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            order = await session.get(Order, self.order_id)
            if order:
                order.status = "APPEALED"
                await session.commit()

        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)

        admin_view = AdminResolutionView(self.order_id, self.buyer_id, self.seller_id)
        await interaction.channel.send(  # type: ignore
            f"<@&{ADMIN_ROLE_ID}> **異議申立てがありました。取引は一時停止されました。**\n"
            f"関係者は詳細を報告し、管理者の裁定をお待ちください。",
            view=admin_view,
        )


# ──────────────────────────────────────────────────────────────────────────────
# AdminResolutionView
# ──────────────────────────────────────────────────────────────────────────────

class AdminResolutionView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id  = order_id
        self.buyer_id  = buyer_id
        self.seller_id = seller_id

        btn_release = discord.ui.Button(
            label="[Admin] 強制リリース",
            style=discord.ButtonStyle.success,
            custom_id=f"admin_force_release_{order_id}",
        )
        btn_release.callback = self.force_release
        self.add_item(btn_release)

        btn_cancel = discord.ui.Button(
            label="[Admin] 強制キャンセル",
            style=discord.ButtonStyle.danger,
            custom_id=f"admin_force_cancel_{order_id}",
        )
        btn_cancel.callback = self.force_cancel
        self.add_item(btn_cancel)

    async def force_release(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message(
                "このボタンは管理者のみが押せます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                order = (await session.execute(stmt_order)).scalar_one_or_none()

                if not order or order.status == "COMPLETED":
                    await interaction.followup.send(
                        "この取引は既に完了しているか無効です。", ephemeral=True
                    )
                    return

                stmt_seller = (
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )
                seller = (await session.execute(stmt_seller)).scalar_one()

                stmt_buyer = (
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )
                buyer = (await session.execute(stmt_buyer)).scalar_one()

                total_locked = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                seller.locked_balance = round(float(seller.locked_balance) - total_locked, 8)

                buyer.available_balance = round(
                    float(buyer.available_balance) + float(order.amount_ltc), 8
                )

                session.add(Transaction(
                    user_id=self.buyer_id,
                    tx_type="P2P_BUY_ADMIN",
                    amount_ltc=float(order.amount_ltc),
                    confirmations=1,
                ))
                session.add(Transaction(
                    user_id=self.seller_id,
                    tx_type="P2P_SELL_ADMIN",
                    amount_ltc=-float(order.amount_ltc),
                    confirmations=1,
                ))

                stmt_config = select(SystemConfig).with_for_update()
                config = (await session.execute(stmt_config)).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=0.0)
                    session.add(config)
                config.collected_fees_ltc = round(
                    float(config.collected_fees_ltc) + float(order.fee_ltc), 8
                )

                order.status           = "COMPLETED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(  # type: ignore
            "**管理者が【強制リリース】を実行しました。**\n"
            "LTCは購入者に引き渡されました。チケットは10分後に削除されます。"
        )

    async def force_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
            await interaction.response.send_message(
                "このボタンは管理者のみが押せます。", ephemeral=True
            )
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                stmt_order = select(Order).where(Order.id == self.order_id).with_for_update()
                order = (await session.execute(stmt_order)).scalar_one_or_none()

                if not order or order.status in ["COMPLETED", "CANCELLED"]:
                    await interaction.followup.send(
                        "この注文はすでにキャンセルまたは完了しています。", ephemeral=True
                    )
                    return

                order.status           = "CANCELLED"
                order.ticket_delete_at = func.now() + datetime.timedelta(minutes=10)

                stmt_seller = (
                    select(User).where(User.discord_id == self.seller_id).with_for_update()
                )
                seller = (await session.execute(stmt_seller)).scalar_one()

                total_refund = round(float(order.amount_ltc) + float(order.fee_ltc), 8)
                seller.locked_balance    = round(float(seller.locked_balance)    - total_refund, 8)
                seller.available_balance = round(float(seller.available_balance) + total_refund, 8)

                stmt_buyer = (
                    select(User).where(User.discord_id == self.buyer_id).with_for_update()
                )
                buyer = (await session.execute(stmt_buyer)).scalar_one()

                # 管理者によるキャンセルは双方を救済
                if seller.total_trades > 0:
                    seller.total_trades -= 1
                if buyer.total_trades > 0:
                    buyer.total_trades -= 1

        for child in self.children:
            child.disabled = True  # type: ignore
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(  # type: ignore
            "**管理者が【強制キャンセル】を実行しました。**\n"
            "ロックされていたLTCは販売者に返還されました。"
            "チケットは10分後に削除されます。"
        )


# ──────────────────────────────────────────────────────────────────────────────
# チケットチャンネル作成
# ──────────────────────────────────────────────────────────────────────────────

async def create_ticket_channel(
    guild: discord.Guild,
    seller_member,
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
    expires_at_unix: int,
) -> Optional[discord.TextChannel]:
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    seller_id = seller_member.id

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        buyer: discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        ),
        seller_member: discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        ),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, read_message_history=True
        )

    try:
        channel = await guild.create_text_channel(
            name=f"ticket-{order_id}",
            category=category,
            overwrites=overwrites,
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
                f"購入者は指定された方法で送金を行い、完了したら「支払いました」ボタンを押してください。\n"
                f"**支払いは <t:{expires_at_unix}:R> までに完了させてください。**"
            ),
            color=discord.Color.gold(),
        )

        view = TicketView(order_id, str(buyer.id), str(seller_id))
        await channel.send(f"{buyer.mention} <@{seller_id}>", embed=embed, view=view)

        if welcome_message and str(welcome_message).strip():
            welcome_embed = discord.Embed(
                title="販売者からのメッセージ",
                description=str(welcome_message),
                color=discord.Color.blue(),
            )
            await channel.send(embed=welcome_embed)

        return channel

    except Exception as e:
        print(f"[ticket] チャンネル作成失敗: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# TicketCog
# ──────────────────────────────────────────────────────────────────────────────

class TicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """
        ボット起動時に PENDING / PAID / APPEALED のアクティブな注文を全件取得し、
        各注文の TicketView / ReleaseView / AdminResolutionView を永続 View として登録する。
        これにより、ボット再起動後もチケットボタンが機能する。
        """
        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.status.in_(["PENDING", "PAID", "APPEALED"])
            )
            result      = await session.execute(stmt)
            active_orders = result.scalars().all()

        registered = 0
        for order in active_orders:
            oid     = order.id
            bid     = str(order.buyer_id)
            sid     = str(order.seller_id)
            self.bot.add_view(TicketView(oid, bid, sid))
            self.bot.add_view(ReleaseView(oid, bid, sid))
            self.bot.add_view(AdminResolutionView(oid, bid, sid))
            registered += 1

        if registered:
            print(f"[ticket] {registered} 件のアクティブ注文の View を再登録しました。")


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))