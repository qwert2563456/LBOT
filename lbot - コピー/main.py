import os
from dotenv import load_dotenv

load_dotenv()

import discord
from discord.ext import commands
import asyncio

from database import init_db
import models

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

class LTCP2PBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # データベースの初期化
        await init_db()
        print("Database initialized.")

        # Cogsの読み込み
        initial_extensions = [
            "cogs.admin",
            "cogs.dashboard",
            "cogs.market",
            "cogs.ticket",
            "cogs.tasks",
            "cogs.chart",
            "cogs.escrow_market",
            "cogs.escrow_ticket",
            "cogs.support_ticket",
            "cogs.tos"
        ]
        out_str = []
        for ext in initial_extensions:
            try:
                await self.load_extension(ext)
                out_str.append(f"Loaded extension {ext}")
            except Exception as e:
                out_str.append(f"Failed to load extension {ext}: {e}")
        print("\n".join(out_str))

        # スラッシュコマンドの同期 (開発時は指定ギルドで行うと即時反映される)
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Synced commands to guild {GUILD_ID}.")
        else:
            await self.tree.sync()
            print("Synced global commands.")


    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id if self.user else 'Unknown'})")
        print("Bot is ready and operational.")

        # 再起動時の送金リカバリーを実行
        from utils.recovery import run_startup_recovery
        try:
            await run_startup_recovery()
        except Exception as e:
            print(f"Failed to run startup recovery: {e}")

        # 入金監視タスクを起動
        from utils.electrum import monitor_deposits_loop
        from utils.escrow_monitor import monitor_escrow_loop
        if not hasattr(self, '_deposit_monitor_started'):
            self._deposit_monitor_started = True
            self.loop.create_task(monitor_deposits_loop(self))
            self.loop.create_task(monitor_escrow_loop(self))
            print("Deposit and Escrow monitors started.")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        print("ERROR: DISCORD_BOT_TOKEN is not set in .env")
    else:
        bot = LTCP2PBot()
        bot.run(TOKEN)
