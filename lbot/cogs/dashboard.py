import discord
from discord.ext import commands
import traceback
import os
from utils.electrum import async_generate_address_for_user, broadcast_withdrawal
from utils.price import fetch_ltc_jpy_price
from database import AsyncSessionLocal
from models import get_or_create_user, Transaction

MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))

# feerate=sat/byte, user_fee=ユーザーに課金するLTC額
WITHDRAW_FEE_TIERS = [
    {"label": "エコノミー (遅い)",  "feerate": 1,  "user_fee": 0.00012,  "desc": "手数料: ≈1円 / 着金目安5〜10分"},
    {"label": "スタンダード (普通)", "feerate": 10, "user_fee": 0.0006,   "desc": "手数料: ≈5円 / 着金目安1〜5分"},
    {"label": "エクスプレス (速い)", "feerate": 50, "user_fee": 0.0012,   "desc": "手数料: ≈10円 / 着金目安1〜3分"},
]


class FeeSelectView(discord.ui.View):
    """出金手数料ティアを選択するビュー"""
    def __init__(self, max_jpy: int):
        super().__init__(timeout=120)
        self.max_jpy = max_jpy

        select = discord.ui.Select(
            placeholder="送金速度を選択してください",
            options=[
                discord.SelectOption(
                    label=tier["label"],
                    description=tier["desc"],
                    value=str(i)
                )
                for i, tier in enumerate(WITHDRAW_FEE_TIERS)
            ]
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        idx = int(interaction.data["values"][0])  # type: ignore
        tier = WITHDRAW_FEE_TIERS[idx]
        modal = WithdrawModal(
            max_jpy=self.max_jpy,
            feerate=tier["feerate"],
            user_fee=tier["user_fee"],
            tier_label=tier["label"]
        )
        await interaction.response.send_modal(modal)
        self.stop()


class WithdrawModal(discord.ui.Modal, title="LTCの出金"):
    address = discord.ui.TextInput(
        label="出金先LTCアドレス",
        placeholder="ltc1q...",
        required=True,
    )
    amount_jpy_input = discord.ui.TextInput(
        label="出金額 (JPY) ※最低500円",
        placeholder="例: 5000",
        required=True,
    )

    def __init__(self, max_jpy: int, feerate: int = 1, user_fee: float = 0.0002, tier_label: str = ""):
        super().__init__()
        self.amount_jpy_input.default = str(max_jpy)
        self.max_jpy = max_jpy
        self.feerate = feerate
        self.user_fee = user_fee
        self.tier_label = tier_label

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            jpy = int(self.amount_jpy_input.value)
            if jpy < 500:
                raise ValueError("最低出金額は 500円 です。")
            if jpy > self.max_jpy:
                raise ValueError(f"出金可能額を超えています。(上限: ¥{self.max_jpy:,})")

            price_jpy = await fetch_ltc_jpy_price()
            if price_jpy <= 0:
                raise ValueError("現在レートが取得できません。しばらくお待ちください。")
            amt_ltc = jpy / price_jpy

            # 手数料は送金額から差し引く（ユーザー残高からの引き落とし = amt_ltc のまま）
            send_ltc = amt_ltc - self.user_fee
            fee_jpy = int(self.user_fee * price_jpy)

            if send_ltc <= 0:
                raise ValueError(f"送金額が手数料({self.user_fee:.4f} LTC ≈ ¥{fee_jpy})より少ないため出金できません。")

            async with AsyncSessionLocal() as session:
                user = await get_or_create_user(session, interaction.user.id)

                if float(user.available_balance) < amt_ltc:
                    raise ValueError(
                        f"残高不足です。`{amt_ltc:.8f}` LTC が必要です。"
                        f"\n(利用可能: `{float(user.available_balance):.8f}` LTC)"
                    )

                txid = await broadcast_withdrawal(self.address.value, send_ltc, feerate=self.feerate)
                if not txid:
                    raise ValueError("送金処理に失敗しました。Electrumデーモンを確認してください。")

                new_balance = float(user.available_balance) - amt_ltc

                # 残高が1円未満なら自動で0にする
                if new_balance > 0 and (new_balance * price_jpy) < 1.0:
                    new_balance = 0.0

                user.available_balance = max(0.0, new_balance)

                tx_log = Transaction(
                    user_id=str(interaction.user.id),
                    txid=txid if isinstance(txid, str) else str(txid),
                    tx_type="WITHDRAW",
                    amount_ltc=-send_ltc,
                    fee_ltc=self.user_fee,
                    confirmations=0
                )
                session.add(tx_log)
                await session.commit()

            await interaction.followup.send(
                f"✅ 出金処理を受け付けました。\n"
                f"**送金先:** `{self.address.value}`\n"
                f"**入力額:** `¥{jpy:,}` (≈ `{amt_ltc:.8f} LTC`)\n"
                f"**手数料:** `{self.user_fee:.4f} LTC` (≈ ¥{fee_jpy}) — {self.tier_label}\n"
                f"**実送金額:** `{send_ltc:.8f} LTC`\n"
                f"**TxID:** `{txid}`",
                ephemeral=True
            )

        except ValueError as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send("出金処理中にエラーが発生しました。", ephemeral=True)


class DashboardView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None) # Persistent View

    @discord.ui.button(label="残高・実績の確認", style=discord.ButtonStyle.primary, custom_id="dash_balance")
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        async with AsyncSessionLocal() as session:
            user = await get_or_create_user(session, interaction.user.id)
            
            available = float(user.available_balance)
            locked = float(user.locked_balance)
            total = user.total_balance
            
            total_trades = user.total_trades
            completed_trades = user.completed_trades
            completion_rate = (completed_trades / total_trades) * 100 if total_trades > 0 else 0
            
            # 価格情報を取得して見積もり計算
            price_jpy = await fetch_ltc_jpy_price()
            jpy_estimate = int(float(total) * price_jpy)
        
        embed = discord.Embed(
            title=f"{interaction.user.display_name} のダッシュボード",
            color=discord.Color.blue()
        )
        embed.add_field(name="使用可能 LTC", value=f"**{available:.8f}** LTC", inline=True)
        embed.add_field(name="ロック中 (取引中)", value=f"**{locked:.8f}** LTC", inline=True)
        embed.add_field(name="総残高", value=f"**{float(total):.8f}** LTC", inline=False)
        embed.add_field(name="JPY換算(目安)", value=f"約 **{jpy_estimate:,}** 円", inline=True)
        embed.add_field(name="取引実績", value=f"{completed_trades}回 完了 (成功率 {completion_rate:.1f}%)", inline=False)
        embed.add_field(name="ステータス", value="🟢 オンライン" if user.is_online else "🔴 オフライン", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="LTCを入金する", style=discord.ButtonStyle.success, custom_id="dash_deposit")
    async def deposit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        async with AsyncSessionLocal() as session:
            user = await get_or_create_user(session, interaction.user.id)
            
            # 入金アドレスの取得 (DBから、または新規生成してDBに保存)
            address = await async_generate_address_for_user(user)
            if not address:
                await interaction.followup.send("アドレスの生成に失敗しました。時間をおいて再度お試しください。", ephemeral=True)
                return
            
        if not address:
             await interaction.followup.send("入金アドレスの生成に失敗しました。ウォレットが起動していない可能性があります。", ephemeral=True)
             return
        
        embed = discord.Embed(
            title="LTC 入金アドレス",
            description=f"以下のLTCアドレスにご入金ください。\n(ネットワークで数回の承認後に自動的に「使用可能LTC」に反映されます)\n\n`{address}`",
            color=discord.Color.green()
        )
        embed.set_footer(text="※ このアドレスはあなた専用です。他の人に共有しないでください。")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="LTCを出金する", style=discord.ButtonStyle.danger, custom_id="dash_withdraw")
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 先に残高を取得
        async with AsyncSessionLocal() as session:
            user = await get_or_create_user(session, interaction.user.id)
            available = float(user.available_balance)
        
        price_jpy = await fetch_ltc_jpy_price()
        max_jpy = int(available * price_jpy) if price_jpy > 0 else 0
        
        if max_jpy < 500:
            await interaction.response.send_message("出金可能残高が最低出金額(500円)に満たないため、出金できません。", ephemeral=True)
            return
        
        # 手数料ティア選択を表示 → 選択後にモーダルが開く
        await interaction.response.send_message(
            "📤 **出金速度を選択してください**\n速度によって手数料が異なります。",
            view=FeeSelectView(max_jpy=max_jpy),
            ephemeral=True
        )

    @discord.ui.button(label="オンライン状態 切替", style=discord.ButtonStyle.secondary, custom_id="dash_toggle_online")
    async def toggle_online(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        async with AsyncSessionLocal() as session:
            # 先にユーザー情報を取得
            user = await get_or_create_user(session, interaction.user.id)
            
            from models import Ad
            from sqlalchemy import select
            
            stmt = select(Ad).where(
                Ad.user_id == str(interaction.user.id),
                Ad.is_active == True
            )
            res = await session.execute(stmt)
            active_ad = res.scalars().first()
            
            # ★ 修正: アクティブな広告がない、または「残高が0以下（=マーケットに表示されていない）」場合は弾く
            if not active_ad or float(user.available_balance) <= 0:
                await interaction.followup.send("現在マーケットに掲示されている有効な広告がありません。（未作成、または残高不足です）\n広告を掲示しているユーザーのみ状態を切り替えることができます。", ephemeral=True)
                return

            # 条件を満たしている場合のみステータスを変更
            user.is_online = not user.is_online
            new_status = user.is_online
            await session.commit()
            
        status_str = "🟢 オンライン" if new_status else "🔴 オフライン"
        await interaction.followup.send(f"あなたの状態を {status_str} に変更しました。\n※オフライン時は他ユーザーに広告が表示されなくなります。", ephemeral=True)
        
        # ★ ここに到達するのは「広告を掲示しているユーザーがステータスを変更したときのみ」になります
        if MARKET_CHANNEL_ID:
            # インポートパスはご自身の環境に合わせてください
            from cogs.market import refresh_market_panels 
            await refresh_market_panels(interaction.client)

class DashboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(DashboardView()) # 再起動時にもボタンが機能するように登録

async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardCog(bot))
