import discord
from discord.ext import commands
from discord import app_commands
import os
from database import AsyncSessionLocal
from models import SystemConfig
from sqlalchemy import select

ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))

class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def is_admin(self, interaction: discord.Interaction) -> bool:
        """ユーザーが管理者ロールを持っているか確認する"""
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles)

    @app_commands.command(name="setup_market", description="【管理者専用】P2P取引マーケットを設置・デプロイします。")
    async def setup_market(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        from cogs.market import refresh_market_panels
        
        # MARKET_CHANNEL_IDが設定されている場合はそちらに、
        # 未設定なら現在のチャンネルにマーケットを展開
        target_channel = MARKET_CHANNEL_ID or interaction.channel_id
        await refresh_market_panels(self.bot, target_channel)
        
        await interaction.followup.send(f"✅ マーケットパネルを設置しました。(ch: <#{target_channel}>)", ephemeral=True)

    @app_commands.command(name="setup_dashboard", description="【管理者専用】ユーザーダッシュボードを設置します。")
    async def setup_dashboard(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
            
        embed = discord.Embed(
            title="ユーザーダッシュボード",
            description="残高の確認、LTCの入出金、自分の広告管理が行えます。\n下のボタンから操作してください。",
            color=discord.Color.green()
        )
        
        from cogs.dashboard import DashboardView
        await interaction.channel.send(embed=embed, view=DashboardView()) # type: ignore
        await interaction.followup.send("✅ ダッシュボードパネルを設置しました。", ephemeral=True)

    @app_commands.command(name="admin_fee", description="【管理者専用】プールされた手数料額を確認します。")
    async def admin_fee(self, interaction: discord.Interaction):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        from models import Transaction
        from utils.price import fetch_ltc_jpy_price
        from sqlalchemy import func
            
        collected_fee = 0.0
        withdraw_fee_revenue = 0.0
        total_withdrawals = 0
        total_deposits = 0
        
        async with AsyncSessionLocal() as session:
            # 取引手数料（SystemConfig）
            stmt = select(SystemConfig)
            res = await session.execute(stmt)
            config = res.scalar_one_or_none()
            if config:
                collected_fee = float(config.collected_fees_ltc)

            # 出金手数料の正確な合計
            w_fee_sum = await session.execute(
                select(func.coalesce(func.sum(Transaction.fee_ltc), 0)).where(Transaction.tx_type == "WITHDRAW")
            )
            withdraw_fee_revenue = float(w_fee_sum.scalar() or 0)

            # 出金件数
            w_count = await session.execute(
                select(func.count()).select_from(Transaction).where(Transaction.tx_type == "WITHDRAW")
            )
            total_withdrawals = w_count.scalar() or 0

            # 入金件数
            d_count = await session.execute(
                select(func.count()).select_from(Transaction).where(Transaction.tx_type == "DEPOSIT")
            )
            total_deposits = d_count.scalar() or 0

        total_revenue = collected_fee + withdraw_fee_revenue

        price_jpy = await fetch_ltc_jpy_price()
        trade_jpy = int(collected_fee * price_jpy) if price_jpy > 0 else 0
        withdraw_jpy = int(withdraw_fee_revenue * price_jpy) if price_jpy > 0 else 0
        total_jpy = int(total_revenue * price_jpy) if price_jpy > 0 else 0

        embed = discord.Embed(
            title="📊 運営収益レポート",
            color=discord.Color.gold()
        )
        embed.add_field(
            name="💱 取引手数料 (0.2%)",
            value=f"`{collected_fee:.8f}` LTC (≈ ¥{trade_jpy:,})",
            inline=False
        )
        embed.add_field(
            name="📤 出金手数料収益",
            value=f"`{withdraw_fee_revenue:.8f}` LTC (≈ ¥{withdraw_jpy:,})\n({total_withdrawals}件の出金)",
            inline=False
        )
        embed.add_field(
            name="💰 合計収益",
            value=f"**`{total_revenue:.8f}` LTC** (≈ ¥{total_jpy:,})",
            inline=False
        )
        embed.add_field(
            name="📈 統計",
            value=f"入金: {total_deposits}件 / 出金: {total_withdrawals}件",
            inline=False
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="set_balance", description="【管理者専用】ユーザーの残高を直接操作します。")
    @app_commands.describe(
        user="対象ユーザー",
        action="操作の種類",
        amount="金額（LTC操作はLTC単位、JPY操作はJPY単位）"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="残高をゼロにする", value="zero"),
        app_commands.Choice(name="LTC残高をセット", value="set_ltc"),
        app_commands.Choice(name="JPY分のLTCを増やす", value="add_jpy"),
        app_commands.Choice(name="JPY分のLTCを減らす", value="sub_jpy"),
        app_commands.Choice(name="LTCを直接増やす", value="add_ltc"),
        app_commands.Choice(name="LTCを直接減らす", value="sub_ltc"),
    ])
    async def set_balance(self, interaction: discord.Interaction, user: discord.Member, action: str, amount: float = 0.0):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from models import get_or_create_user
        from utils.price import fetch_ltc_jpy_price

        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, user.id)
            old_balance = float(db_user.available_balance)

            if action == "zero":
                db_user.available_balance = 0.0
                db_user.locked_balance = 0.0
            elif action == "set_ltc":
                db_user.available_balance = amount
            elif action == "add_ltc":
                db_user.available_balance = old_balance + amount
            elif action == "sub_ltc":
                db_user.available_balance = max(0, old_balance - amount)
            elif action in ("add_jpy", "sub_jpy"):
                price_jpy = await fetch_ltc_jpy_price()
                if price_jpy <= 0:
                    await interaction.followup.send("レートが取得できません。", ephemeral=True)
                    return
                ltc_amount = amount / price_jpy
                if action == "add_jpy":
                    db_user.available_balance = old_balance + ltc_amount
                else:
                    db_user.available_balance = max(0, old_balance - ltc_amount)

            new_balance = float(db_user.available_balance)
            await session.commit()

        embed = discord.Embed(
            title="残高操作完了",
            description=(
                f"**ユーザー:** {user.mention}\n"
                f"**操作:** `{action}`\n"
                f"**変更前:** `{old_balance:.8f}` LTC\n"
                f"**変更後:** `{new_balance:.8f}` LTC"
            ),
            color=discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="view_user", description="【管理者専用】ユーザーの詳細情報を確認します。")
    @app_commands.describe(user="対象ユーザー")
    async def view_user(self, interaction: discord.Interaction, user: discord.Member):
        if not self.is_admin(interaction):
            await interaction.response.send_message("権限がありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        from models import get_or_create_user
        from utils.price import fetch_ltc_jpy_price

        async with AsyncSessionLocal() as session:
            db_user = await get_or_create_user(session, user.id)
            available = float(db_user.available_balance)
            locked = float(db_user.locked_balance)
            total = available + locked
            hd_index = db_user.hd_index
            deposit_addr = db_user.deposit_address or "未生成"
            total_trades = db_user.total_trades
            completed = db_user.completed_trades

        price_jpy = await fetch_ltc_jpy_price()
        jpy_est = int(total * price_jpy) if price_jpy > 0 else 0

        embed = discord.Embed(
            title=f"ユーザー情報: {user.display_name}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Discord ID", value=f"`{user.id}`", inline=True)
        embed.add_field(name="HD Index", value=f"`{hd_index}`", inline=True)
        embed.add_field(name="入金アドレス", value=f"`{deposit_addr}`", inline=False)
        embed.add_field(name="使用可能 LTC", value=f"`{available:.8f}`", inline=True)
        embed.add_field(name="ロック中 LTC", value=f"`{locked:.8f}`", inline=True)
        embed.add_field(name="総残高 LTC", value=f"`{total:.8f}` (≈ ¥{jpy_est:,})", inline=False)
        embed.add_field(name="取引実績", value=f"{completed}/{total_trades} 完了", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))

