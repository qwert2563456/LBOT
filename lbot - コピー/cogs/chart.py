"""LTC/JPY ライブチャート（公開常時更新 + ボタンで個人閲覧）"""
import asyncio
import datetime
import json
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.chart import generate_chart, TIMEFRAMES

CHART_CONFIG_FILE = "chart_config.json"

def load_chart_config():
    if os.path.exists(CHART_CONFIG_FILE):
        try:
            with open(CHART_CONFIG_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_chart_config(channel_id, message_id):
    with open(CHART_CONFIG_FILE, "w") as f:
        json.dump({"channel_id": channel_id, "message_id": message_id}, f)

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
        self.chart_message = None

    async def cog_load(self):
        """Cogが読み込まれた時に実行"""
        self.bot.add_view(ChartView())  # 再起動後もボタン機能維持
        self.auto_update_task.start()   # 自動更新タスクを開始

    async def cog_unload(self):
        """Cogがアンロードされる時に実行"""
        self.auto_update_task.cancel()

    @app_commands.command(name="chart", description="【管理者】LTC/JPYライブチャートパネルを設置します。")
    @app_commands.default_permissions(administrator=True)
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
        save_chart_config(msg.channel.id, msg.id)

    @tasks.loop(minutes=1.0)
    async def auto_update_task(self):
        """公開チャートを1分ごとに自動更新"""
        config = load_chart_config()
        channel_id = config.get("channel_id")
        message_id = config.get("message_id")

        if not channel_id or not message_id:
            return

        # メッセージオブジェクトがメモリにない場合はFetchする
        if self.chart_message is None:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not channel:
                return
            try:
                self.chart_message = await channel.fetch_message(message_id)
            except discord.NotFound:
                # メッセージが消されていた場合は設定をクリア
                save_chart_config(None, None)
                return
            except Exception:
                return

        # 実際の更新処理
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
            save_chart_config(None, None)
        except Exception as e:
            print(f"Chart update failed: {e}")

    @auto_update_task.before_loop
    async def before_auto_update(self):
        """Botの準備が完了するまで待機"""
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ChartCog(bot))
