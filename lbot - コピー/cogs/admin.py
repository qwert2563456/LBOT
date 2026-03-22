import discord
from discord.ext import commands
from discord import app_commands
import os
from database import AsyncSessionLocal
from models import SystemConfig, get_or_create_user, get_p2p_fee_rate, get_escrow_fee_rate
from sqlalchemy import select, func, or_
from utils.price import fetch_ltc_jpy_price
from models import Transaction
from utils.decimal_utils import quantize_ltc
from decimal import Decimal

ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))


# ── ヘルパー ──────────────────────────────────────────────

def _is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)


# ── Modals ───────────────────────────────────────────────

class SetFeeModal(discord.ui.Modal, title="P2P手数料率の設定"):
    fee_input = discord.ui.TextInput(
        label="手数料率 (%) — 例: 0.2 と入力すると 0.2% になります",
        placeholder="0.2",
        max_length=7,   # "10.000" まで許容 (最大7文字)
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            fee_pct = Decimal(self.fee_input.value.strip())
            if not (Decimal("0.0") <= fee_pct <= Decimal("10.0")):
                raise ValueError("手数料率は 0〜10% の範囲で入力してください。")

            fee_decimal = (fee_pct / Decimal("100")).quantize(Decimal("0.000000"))

            async with AsyncSessionLocal() as session:
                stmt = select(SystemConfig)
                res = await session.execute(stmt)
                config = res.scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal("0"), p2p_fee_percent=fee_decimal)
                    session.add(config)
                else:
                    config.p2p_fee_percent = fee_decimal
                await session.commit()

            await interaction.followup.send(
                f"P2P手数料率を **{fee_pct}%** に変更しました。\n"
                f"次回の取引から適用されます。",
                ephemeral=True,
            )
        except ValueError as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except Exception:
            import traceback; traceback.print_exc()
            await interaction.followup.send("予期せぬエラーが発生しました。", ephemeral=True)


class SetEscrowFeeModal(discord.ui.Modal, title="仲介型(Escrow)手数料率の設定"):
    fee_input = discord.ui.TextInput(
        label="手数料率 (%) — 例: 0.2 と入力すると 0.2% になります",
        placeholder="0.2",
        max_length=7,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            fee_pct = Decimal(self.fee_input.value.strip())
            if not (Decimal("0.0") <= fee_pct <= Decimal("10.0")):
                raise ValueError("手数料率は 0〜10% の範囲で入力してください。")

            fee_decimal = (fee_pct / Decimal("100")).quantize(Decimal("0.000000"))

            async with AsyncSessionLocal() as session:
                stmt = select(SystemConfig)
                res = await session.execute(stmt)
                config = res.scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal("0"), p2p_fee_percent=Decimal("0.002"), escrow_fee_percent=fee_decimal)
                    session.add(config)
                else:
                    config.escrow_fee_percent = fee_decimal
                await session.commit()

            await interaction.followup.send(
                f"仲介型手数料率を **{fee_pct}%** に変更しました。\n"
                f"次回の取引から適用されます。",
                ephemeral=True,
            )
        except ValueError as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except Exception:
            import traceback; traceback.print_exc()
            await interaction.followup.send("予期せぬエラーが発生しました。", ephemeral=True)


class ViewUserModal(discord.ui.Modal, title="ユーザー情報確認"):
    user_id_input = discord.ui.TextInput(
        label="Discord ユーザーID",
        placeholder="123456789012345678",
        max_length=20,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid_str = self.user_id_input.value.strip()
        try:
            uid_int = int(uid_str)
        except ValueError:
            await interaction.followup.send("無効なユーザーIDです。数字のみ入力してください。", ephemeral=True)
            return

        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, uid_int)
            available = quantize_ltc(db_user.available_balance)
            locked   = quantize_ltc(db_user.locked_balance)
            total    = available + locked
            hd_index = db_user.hd_index
            deposit_addr  = db_user.deposit_address or "未生成"
            total_trades  = db_user.total_trades
            completed     = db_user.completed_trades

        price_jpy = await fetch_ltc_jpy_price()
        jpy_est = int(total * Decimal(str(price_jpy))) if price_jpy > 0 else 0
        completion_rate = (completed / total_trades * 100) if total_trades > 0 else 0.0

        try:
            discord_user = await interaction.client.fetch_user(uid_int)
            display_name = discord_user.display_name
            avatar_url   = discord_user.display_avatar.url
        except Exception:
            display_name = f"User {uid_str[:8]}..."
            avatar_url   = None

        embed = discord.Embed(title=f"ユーザー情報: {display_name}", color=discord.Color.blue())
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        embed.add_field(name="Discord ID",  value=f"`{uid_str}`",          inline=True)
        embed.add_field(name="HD Index",    value=f"`{hd_index}`",          inline=True)
        embed.add_field(name="入金アドレス", value=f"`{deposit_addr}`",       inline=False)
        embed.add_field(name="使用可能 LTC", value=f"`{available:.8f}`",      inline=True)
        embed.add_field(name="ロック中 LTC", value=f"`{locked:.8f}`",         inline=True)
        embed.add_field(
            name="総残高 LTC",
            value=f"`{total:.8f}` (≈ ¥{jpy_est:,})",
            inline=False,
        )
        embed.add_field(
            name="取引実績",
            value=f"{completed} / {total_trades} 完了 (成功率 {completion_rate:.1f}%)",
            inline=True,
        )
        embed.add_field(
            name="オンライン状態",
            value="🟢 オンライン" if db_user.is_online else "🔴 オフライン",
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


class BalanceOpModal(discord.ui.Modal):
    """残高操作モーダル（操作種別ごとに title を変える）"""

    user_id_input = discord.ui.TextInput(
        label="Discord ユーザーID",
        placeholder="123456789012345678",
        max_length=20,
        required=True,
    )
    amount_input = discord.ui.TextInput(
        label="金額 (LTC単位) ※ ゼロリセットは空欄でOK",
        placeholder="例: 0.5",
        max_length=20,
        required=False,
    )

    def __init__(self, action: str, action_label: str):
        super().__init__(title=f"残高操作: {action_label}")
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        uid_str = self.user_id_input.value.strip()
        try:
            uid_int = int(uid_str)
        except ValueError:
            await interaction.followup.send("無効なユーザーIDです。", ephemeral=True)
            return

        amount_dec = Decimal("0")
        if self.amount_input.value.strip():
            try:
                amount_dec = quantize_ltc(Decimal(self.amount_input.value.strip()))
                if amount_dec < Decimal("0"):
                    raise ValueError("金額には 0 以上の値を入力してください。")
            except ValueError as e:
                await interaction.followup.send(f"無効な金額: {e}", ephemeral=True)
                return

        async with AsyncSessionLocal() as session:
            async with session.begin():
                from models import User, Ledger
                stmt = select(User).where(User.discord_id == str(uid_int)).with_for_update()
                db_user = (await session.execute(stmt)).scalar_one_or_none()
                if not db_user:
                    db_user = User(discord_id=str(uid_int))
                    session.add(db_user)
                    await session.flush()
                
                old_avail  = quantize_ltc(db_user.available_balance)
                old_locked = quantize_ltc(db_user.locked_balance)

                if self.action == "zero":
                    db_user.available_balance = Decimal("0")
                    db_user.locked_balance    = Decimal("0")
                elif self.action == "set_ltc":
                    db_user.available_balance = amount_dec
                elif self.action == "add_ltc":
                    db_user.available_balance = old_avail + amount_dec
                elif self.action == "sub_ltc":
                    db_user.available_balance = max(Decimal("0"), old_avail - amount_dec)

                new_avail  = quantize_ltc(db_user.available_balance)
                new_locked = quantize_ltc(db_user.locked_balance)
                
                # Ledgerに差分を記録 (ADMIN_ADJUST)
                diff_avail = new_avail - old_avail
                diff_locked = new_locked - old_locked
                
                if diff_avail != Decimal("0"):
                    lg_avail = Ledger(
                        user_id=db_user.discord_id, 
                        type="ADMIN_ADJUST", 
                        amount_ltc=diff_avail, 
                        reference_id=f"Admin: {interaction.user.id}",
                        note=f"Action: {self.action} (Available)"
                    )
                    session.add(lg_avail)
                if diff_locked != Decimal("0"):
                    lg_locked = Ledger(
                        user_id=db_user.discord_id, 
                        type="ADMIN_ADJUST", 
                        amount_ltc=diff_locked, 
                        reference_id=f"Admin: {interaction.user.id}",
                        note=f"Action: {self.action} (Locked)"
                    )
                    session.add(lg_locked)

        try:
            discord_user = await interaction.client.fetch_user(uid_int)
            display_name = discord_user.display_name
        except Exception:
            display_name = uid_str

        embed = discord.Embed(
            title="残高操作完了",
            color=discord.Color.orange(),
        )
        embed.add_field(name="対象ユーザー", value=f"{display_name}\n(`{uid_str}`)", inline=False)
        embed.add_field(name="操作", value=f"`{self.action}`", inline=True)
        embed.add_field(name="入力金額", value=f"`{amount_dec:.8f}` LTC" if amount_dec > Decimal("0") else "—", inline=True)
        embed.add_field(
            name="変更前",
            value=f"利用可能: `{old_avail:.8f}`\nロック中: `{old_locked:.8f}`",
            inline=True,
        )
        embed.add_field(
            name="変更後",
            value=f"利用可能: `{new_avail:.8f}`\nロック中: `{new_locked:.8f}`",
            inline=True,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── 残高操作の種別選択 View ──────────────────────────────

class BalanceActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        select_menu = discord.ui.Select(
            placeholder="操作の種類を選択してください",
            options=[
                discord.SelectOption(label="残高を完全リセット (ゼロにする)",    value="zero",    emoji="🔴",
                                     description="available と locked を両方 0 にします"),
                discord.SelectOption(label="LTC 残高を直接セット",               value="set_ltc", emoji="🔧",
                                     description="available を指定値に上書きします"),
                discord.SelectOption(label="LTC を増やす",                        value="add_ltc", emoji="➕",
                                     description="available に指定量を加算します"),
                discord.SelectOption(label="LTC を減らす",                        value="sub_ltc", emoji="➖",
                                     description="available から指定量を減算します（0 以下にはなりません）"),
            ],
        )
        select_menu.callback = self.on_select
        self.add_item(select_menu)

    async def on_select(self, interaction: discord.Interaction):
        action = interaction.data["values"][0]  # type: ignore
        labels = {
            "zero":    "残高リセット",
            "set_ltc": "LTC直接セット",
            "add_ltc": "LTC増加",
            "sub_ltc": "LTC減少",
        }
        modal = BalanceOpModal(action=action, action_label=labels[action])
        await interaction.response.send_modal(modal)
        self.stop()


# ── 管理者ダッシュボード メインパネル View ──────────────

class AdminDashboardView(discord.ui.View):
    """
    永続化 View。ボット再起動後もボタンが機能するよう timeout=None。
    全ボタンに管理者ロールチェックを実施。
    """

    def __init__(self):
        super().__init__(timeout=None)

    # ── Row 0: 情報系 ──

    @discord.ui.button(
        label="手数料レポート",
        style=discord.ButtonStyle.primary,
        custom_id="admindash_fee_report",
        row=0,
    )
    async def fee_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        collected_fee       = Decimal("0")
        withdraw_fee_total  = Decimal("0")
        total_withdrawals   = 0
        total_deposits      = 0
        current_fee_pct     = Decimal("0.2")  # デフォルト表示
        current_escrow_fee_pct = Decimal("0.2")  # デフォルト表示
        total_p2p_orders    = 0
        completed_p2p       = 0
        total_escrow_orders = 0
        completed_escrow    = 0

        async with AsyncSessionLocal() as session:
            stmt = select(SystemConfig)
            res  = await session.execute(stmt)
            config = res.scalar_one_or_none()
            if config:
                collected_fee   = config.collected_fees_ltc
                if config.p2p_fee_percent is not None:
                    current_fee_pct = config.p2p_fee_percent * Decimal("100")
                if config.escrow_fee_percent is not None:
                    current_escrow_fee_pct = config.escrow_fee_percent * Decimal("100")

            # 出金手数料合計
            w_fee = await session.execute(
                select(func.coalesce(func.sum(Transaction.fee_ltc), 0))
                .where(Transaction.tx_type == "WITHDRAW")
            )
            withdraw_fee_total = w_fee.scalar() or Decimal("0")

            # 件数
            w_cnt = await session.execute(
                select(func.count()).select_from(Transaction).where(Transaction.tx_type == "WITHDRAW")
            )
            total_withdrawals = int(w_cnt.scalar() or 0)

            d_cnt = await session.execute(
                select(func.count()).select_from(Transaction).where(Transaction.tx_type == "DEPOSIT")
            )
            total_deposits = int(d_cnt.scalar() or 0)

            # P2P取引数
            from models import Order
            p2p_total_q = await session.execute(select(func.count()).select_from(Order))
            total_p2p_orders = int(p2p_total_q.scalar() or 0)
            p2p_done_q = await session.execute(
                select(func.count()).select_from(Order).where(Order.status == "COMPLETED")
            )
            completed_p2p = int(p2p_done_q.scalar() or 0)

            # 仲介取引数
            from models import EscrowOrder
            esc_total_q = await session.execute(select(func.count()).select_from(EscrowOrder))
            total_escrow_orders = int(esc_total_q.scalar() or 0)
            esc_done_q = await session.execute(
                select(func.count()).select_from(EscrowOrder).where(EscrowOrder.status == "COMPLETED")
            )
            completed_escrow = int(esc_done_q.scalar() or 0)

        total_revenue = collected_fee + withdraw_fee_total
        price_jpy     = await fetch_ltc_jpy_price()

        def to_jpy(ltc: Decimal) -> str:
            return f"¥{int(ltc * Decimal(str(price_jpy))):,}" if price_jpy > 0 else "取得不可"

        embed = discord.Embed(title="運営収益レポート", color=discord.Color.gold())
        embed.add_field(
            name="現在のP2P手数料率",
            value=f"**`{current_fee_pct:.4f}%`**",
            inline=True,
        )
        embed.add_field(
            name="現在の仲介手数料率",
            value=f"**`{current_escrow_fee_pct:.4f}%`**",
            inline=True,
        )
        embed.add_field(
            name="P2P取引手数料収益",
            value=f"`{collected_fee:.8f}` LTC (≈ {to_jpy(collected_fee)})",
            inline=True,
        )
        embed.add_field(
            name="出金手数料収益",
            value=f"`{withdraw_fee_total:.8f}` LTC (≈ {to_jpy(withdraw_fee_total)})\n{total_withdrawals}件の出金",
            inline=True,
        )
        embed.add_field(
            name="合計収益",
            value=f"**`{total_revenue:.8f}` LTC** (≈ {to_jpy(total_revenue)})",
            inline=False,
        )
        embed.add_field(
            name="統計サマリー",
            value=(
                f"P2P取引: {total_p2p_orders}件 (完了 {completed_p2p}件)\n"
                f"仲介取引: {total_escrow_orders}件 (完了 {completed_escrow}件)"
            ),
            inline=False,
        )
        if price_jpy > 0:
            embed.set_footer(text=f"LTC/JPY レート: ¥{price_jpy:,.2f}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="ユーザー情報確認",
        style=discord.ButtonStyle.secondary,
        custom_id="admindash_view_user",
        row=0,
    )
    async def view_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(ViewUserModal())

    # ── Row 1: 操作系 ──

    @discord.ui.button(
        label="P2P手数料率を変更",
        style=discord.ButtonStyle.danger,
        custom_id="admindash_set_fee",
        row=1,
    )
    async def set_fee(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        # 現在の手数料を確認してからモーダルを開く
        current = await get_p2p_fee_rate()
        modal = SetFeeModal()
        modal.fee_input.default = f"{current * 100:.2f}"  # 例: "0.20" (5文字以内)
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="仲介手数料率を変更",
        style=discord.ButtonStyle.danger,
        custom_id="admindash_set_escrow_fee",
        row=1,
    )
    async def set_escrow_fee(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        current = await get_escrow_fee_rate()
        modal = SetEscrowFeeModal()
        modal.fee_input.default = f"{current * 100:.2f}"
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="ユーザー残高を操作",
        style=discord.ButtonStyle.secondary,
        custom_id="admindash_balance_op",
        row=1,
    )
    async def balance_op(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "**残高操作** — 操作の種類を選択してください:",
            view=BalanceActionView(),
            ephemeral=True,
        )


# ── AdminCog ─────────────────────────────────────────────

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(AdminDashboardView())

    def is_admin(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)

    @app_commands.command(
        name="admin_dashboard",
        description="【管理者専用】管理者ダッシュボードパネルを設置します。",
    )
    async def admin_dashboard(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        current_fee = await get_p2p_fee_rate()
        current_escrow_fee = await get_escrow_fee_rate()

        embed = discord.Embed(
            title="管理者ダッシュボード",
            description=(
                "管理者向けの操作パネルです。\n"
                "各ボタンは管理者ロールを持つメンバーのみ使用できます。\n\n"
                f"**現在のP2P手数料率:** `{current_fee * 100:.4f}%` / **仲介型:** `{current_escrow_fee * 100:.4f}%`"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="手数料レポート",
            value="収益・統計の一覧を確認します",
            inline=True,
        )
        embed.add_field(
            name="ユーザー情報確認",
            value="ユーザーIDから残高・実績を確認します",
            inline=True,
        )
        embed.add_field(
            name="P2P手数料率を変更",
            value="P2P取引にかかる手数料率(%)を変更します",
            inline=True,
        )
        embed.add_field(
            name="仲介手数料率を変更",
            value="仲介型取引にかかる手数料率(%)を変更します",
            inline=True,
        )
        embed.add_field(
            name="ユーザー残高を操作",
            value="特定ユーザーのLTC残高を直接操作します",
            inline=True,
        )

        await interaction.channel.send(embed=embed, view=AdminDashboardView())  # type: ignore
        await interaction.followup.send("管理者ダッシュボードを設置しました。", ephemeral=True)

    # ── 旧スラッシュコマンド (後方互換) ──────────────────

    @app_commands.command(name="setup_market", description="【管理者専用】P2P取引マーケットを設置します。")
    async def setup_market(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from cogs.market import refresh_market_panels
        target_channel = MARKET_CHANNEL_ID or interaction.channel_id
        await refresh_market_panels(self.bot, target_channel)
        await interaction.followup.send(
            f"マーケットパネルを設置しました。(ch: <#{target_channel}>)", ephemeral=True
        )

    @app_commands.command(name="setup_dashboard", description="【管理者専用】ユーザーダッシュボードを設置します。")
    async def setup_dashboard(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(
            title="ユーザーダッシュボード",
            description="残高の確認、LTCの入出金、自分の広告管理が行えます。",
            color=discord.Color.green(),
        )
        from cogs.dashboard import DashboardView
        await interaction.channel.send(embed=embed, view=DashboardView())  # type: ignore
        await interaction.followup.send("ダッシュボードパネルを設置しました。", ephemeral=True)


    @app_commands.command(name="audit_ledger", description="【管理者専用】全ユーザーの台帳と現在残高の整合性をチェックします。")
    async def audit_ledger(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=False)
        
        from models import User, Ledger
        
        mismatch_reports = []
        total_ledger = Decimal("0")
        total_balance = Decimal("0")
        total_users_checked = 0
        
        async with AsyncSessionLocal() as session:
            # 全ユーザー取得
            users_res = await session.execute(select(User))
            all_users = users_res.scalars().all()
            
            for user in all_users:
                # Ledger合計
                ledger_res = await session.execute(
                    select(func.coalesce(func.sum(Ledger.amount_ltc), 0)).where(Ledger.user_id == user.discord_id)
                )
                ledger_sum = quantize_ltc(Decimal(str(ledger_res.scalar())))
                
                # DB上の利用可能＋ロック中
                actual_available = quantize_ltc(user.available_balance)
                actual_locked = quantize_ltc(user.locked_balance)
                actual_sum = actual_available + actual_locked
                
                total_ledger += ledger_sum
                total_balance += actual_sum
                total_users_checked += 1
                
                if ledger_sum != actual_sum:
                    mismatch_reports.append(
                        f"ID: `{user.discord_id}`\n"
                        f"Ledger ({ledger_sum:.8f}) vs Actual ({actual_sum:.8f}) "
                        f"[Av: {actual_available:.8f}, Lk: {actual_locked:.8f}]"
                    )

        if not mismatch_reports:
            embed = discord.Embed(
                title="✅ 台帳(Ledger)監査結果: 正常",
                description=f"全 {total_users_checked} ユーザーの残高とトランザクション台帳が完全に一致しています。\n"
                            f"**総流通残高:** `{total_balance:.8f} LTC`",
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title="⚠️ 台帳(Ledger)監査結果: 不整合を検出",
                description=f"ユーザー全体の期待残高: `{total_ledger:.8f} LTC`\n"
                            f"実際のユーザー残高合計: `{total_balance:.8f} LTC`\n\n"
                            f"以下のユーザーに不整合が発生しています：",
                color=discord.Color.red()
            )
            # レポート（長くなりすぎる場合は省略表示）
            report_text = "\n\n".join(mismatch_reports[:10])
            if len(mismatch_reports) > 10:
                report_text += f"\n\n...他 {len(mismatch_reports) - 10} 件のユーザー"
            
            embed.add_field(name="不整合対象", value=report_text)
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))