"""LTC/JPY ライブチャート（公開常時更新 + ボタンで個人閲覧）"""
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands

from utils.chart import generate_chart, TIMEFRAMES


class ChartView(discord.ui.View):
    """チャート期間切替ボタン（ボタン押下はephemeral応答）"""

    def __init__(self):
        super().__init__(timeout=None)  # 永続ビュー

    async def _send_personal_chart(self, interaction: discord.Interaction, tf: str):
        """ボタンを押した人だけにチャートをephemeralで送信"""
        await interaction.response.defer(ephemeral=True)
        buf = await generate_chart(tf)
        tf_info = TIMEFRAMES.get(tf, TIMEFRAMES["1h"])

        if buf is None:
            await interaction.followup.send(
                f"{tf_info['label']} のデータ取得に失敗しました。",
                ephemeral=True
            )
            return

        file = discord.File(fp=buf, filename="ltc_chart.png")
        await interaction.followup.send(
            content=f"**LTC/JPY** — {tf_info['label']}",
            file=file,
            ephemeral=True
        )

    @discord.ui.button(label="24h", style=discord.ButtonStyle.primary, custom_id="chart_live", row=0)
    async def btn_live(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_personal_chart(interaction, "live")
        
    @discord.ui.button(label="7d", style=discord.ButtonStyle.secondary, custom_id="chart_6h", row=0)
    async def btn_6h(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_personal_chart(interaction, "6h")

    @discord.ui.button(label="14d", style=discord.ButtonStyle.secondary, custom_id="chart_1d", row=0)
    async def btn_1d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_personal_chart(interaction, "1d")

    @discord.ui.button(label="30d", style=discord.ButtonStyle.secondary, custom_id="chart_7d", row=0)
    async def btn_7d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send_personal_chart(interaction, "7d")


class ChartCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.chart_message: discord.Message | None = None
        self.bot.add_view(ChartView())  # 再起動後もボタン機能維持

    @app_commands.command(name="chart", description="【管理者】LTC/JPYライブチャートパネルを設置します。")
    async def chart(self, interaction: discord.Interaction):
        await interaction.response.defer()

        buf = await generate_chart("live")

        if buf:
            file = discord.File(fp=buf, filename="ltc_chart.png")
            now = datetime.datetime.now().strftime("%H:%M:%S")
            msg = await interaction.followup.send(
                content=f"**LTC/JPY ライブチャート**  🔴\n最終更新: {now}",
                file=file,
                view=ChartView(),
                wait=True
            )
        else:
            msg = await interaction.followup.send(
                content="**LTC/JPY ライブチャート**\n⏳ データ取得に失敗しました...",
                view=ChartView(),
                wait=True
            )

        self.chart_message = msg

        # 自動更新ループ開始
        if not hasattr(self, '_update_running') or not self._update_running:
            self._update_running = True
            self.bot.loop.create_task(self._auto_update_loop())

    async def _auto_update_loop(self):
        """公開チャートを30秒ごとに自動更新"""
        while True:
            await asyncio.sleep(60)
            if not self.chart_message:
                continue
            try:
                buf = await generate_chart("live")
                if buf:
                    file = discord.File(fp=buf, filename="ltc_chart.png")
                    now = datetime.datetime.now().strftime("%H:%M:%S")
                    await self.chart_message.edit(
                        content=f"**LTC/JPY ライブチャート**  🔴\n最終更新: {now}",
                        attachments=[file]
                    )
            except discord.NotFound:
                self.chart_message = None
            except Exception:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ChartCog(bot))
