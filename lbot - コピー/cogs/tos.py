import discord
import asyncio
from discord.ext import commands
from discord import app_commands
from database import AsyncSessionLocal
from models import get_or_create_user
from sqlalchemy import select

class ToSView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="規約に同意する", style=discord.ButtonStyle.success, custom_id="agree_tos_button")
    async def agree_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            user = await get_or_create_user(session, interaction.user.id)
            user.has_agreed_tos = True
            await session.commit()

        role = interaction.guild.get_role(1485151605249675364)
        if role:
            try:
                await interaction.user.add_roles(role)
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            "**利用規約に同意しました。**\n当サーバーの機能をご利用いただけます！", 
            ephemeral=True
        )

class ToSCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 再起動後もView（ボタン）のイベントをリッスンするために登録します
        self.bot.add_view(ToSView())

    @app_commands.command(name="setup_tos", description="【管理者専用】サーバー利用規約パネルを設置します。")
    @app_commands.default_permissions(administrator=True)
    async def setup_tos(self, interaction: discord.Interaction):
        """利用規約パネルを指定のチャンネルに送信するコマンド"""
        await interaction.response.defer(ephemeral=True)

        # パネルのベースとなるEmbedを作成
        embed = discord.Embed(
            title="サーバー利用規約",
            description=(
                "このサーバー（以下「当サーバー」）は、ユーザー間でのLitecoin（LTC）のP2P売買およびエスクロー（仲介）取引を支援するコミュニティです。\n"
                "当サーバーを利用するすべてのユーザーは、以下の規約に同意したものとみなします。"
            ),
            color=discord.Color.dark_theme() # ダークグレー系の色で厳格さを演出
        )

        # 1項目ずつフィールドとして追加し、見やすく区切る
        embed.add_field(
            name="1. Discord利用規約の遵守",
            value=(
                "当サーバーはDiscord上で運営されています。すべてのユーザーは、当サーバーの規約に加え、Discord公式の利用規約およびコミュニティガイドラインを遵守する必要があります。\n"
                "・詐欺、マネーロンダリング、その他あらゆる違法行為の温床とする行為はDiscordの規約違反となります。\n"
                "・発覚次第、当サーバーからの永久BANに加え、Discord運営への通報（アカウント凍結）措置をとります。"
            ),
            inline=False
        )

        embed.add_field(
            name="2. サービスの性質と運営の立ち位置",
            value=(
                "・当サーバーおよび稼働しているBotは、ユーザー間の取引機会を提供する「マッチングボード」および「エスクローツール」であり、暗号資産交換業者（取引所）ではありません。\n"
                "・運営はユーザー間のJPY（日本円）等の法定通貨のやり取りには一切関与しません。"
            ),
            inline=False
        )

        embed.add_field(
            name="3. 免責事項（重要）",
            value=(
                "暗号資産の性質上、以下の事象によってユーザーに損失が発生した場合、運営は一切の補償および責任を負いません。\n"
                "**・ユーザーの過失** : LTCの送金先アドレスの入力ミス、送金ネットワークの間違い。\n"
                "**・ブロックチェーンの仕様** : ネットワークの混雑による着金遅延や、Reorg（ブロックチェーンの再編成）によるトランザクションの取り消し。\n"
                "**・システム障害** : DiscordのAPI障害、Botの予期せぬダウン、クラッシュ等による一時的な取引停止。\n"
                "**・ユーザー間のトラブル** : JPYの未払い、振り込め詐欺、チャージバック等の金銭トラブル。"
            ),
            inline=False
        )

        embed.add_field(
            name="4. エスクロー取引と紛争解決",
            value=(
                "・仲介型（エスクロー）取引においてトラブルが発生した場合、当サーバーのチケットシステムを用いて運営が仲裁に入ります。\n"
                "・運営は、ブロックチェーン上のトランザクション履歴（TxID）およびチケット内のやり取りを客観的証拠として判定を下します。\n"
                "**・運営の裁定は絶対**であり、決定後の異議申し立てや返金要求には応じられません。"
            ),
            inline=False
        )

        embed.add_field(
            name="5. 禁止事項",
            value=(
                "以下の行為を行った場合、事前の警告なくアカウントの残高を凍結し、サーバーから追放します。\n"
                "・Botのバグ、システム上の隙、Rate Limit（API制限）を意図的に突く行為（連打ツール等の使用）。\n"
                "・犯罪収益の資金洗浄（マネーロンダリング）、詐欺で得た電子マネー等の換金目的での利用。\n"
                "・チケット内での暴言、脅迫、その他取引相手や運営に対するハラスメント行為。\n"
                "・Discord外への誘導による直接取引（中抜き行為）。"
            ),
            inline=False
        )

        embed.add_field(
            name="6. 手数料と残高の扱い",
            value=(
                "・Botを利用した取引には、システム維持のためのプラットフォーム手数料が発生します。手数料率は予告なく変更されることはありません。\n"
                "・長期間（例: 6ヶ月以上）ログインおよび取引がないアカウントのLTC残高は、サーバー維持費として没収される場合があります。"
            ),
            inline=False
        )

        # パネルのフッター設定
        embed.set_footer(text="同意ボタンを押すことで、上記すべての規約に同意したことになります。")

        # チャンネルに送信
        await interaction.channel.send(embed=embed, view=ToSView())
        
        # コマンド実行者（管理者）への完了通知
        await interaction.followup.send("利用規約パネルをチャンネルに設置しました。", ephemeral=True)

    @app_commands.command(name="emergency_dm", description="【管理者専用】規約同意済みユーザー全員に再建用DMを送信します。")
    @app_commands.default_permissions(administrator=True)
    async def emergency_dm(self, interaction: discord.Interaction, message: str):
        """
        message: 送信したいメッセージ内容（再建サーバーのURLなど）
        """
        await interaction.response.defer(ephemeral=True)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
    select(User).where(User.has_agreed_tos.is_(True))  # type: ignore
)
            users = result.scalars().all()

        if not users:
            await interaction.followup.send("送信対象のユーザーが見つかりません。", ephemeral=True)
            return

        await interaction.followup.send(f"**{len(users)}人**へのDM送信処理を開始しました。完了までしばらくお待ちください...", ephemeral=True)

        success_count = 0
        fail_count = 0

        for u in users:
            try:
                discord_user = self.bot.get_user(int(u.discord_id)) or await self.bot.fetch_user(int(u.discord_id))
                
                if discord_user:
                    await discord_user.send(f"【重要なお知らせ】\n{message}")
                    success_count += 1
            except Exception as e:
                fail_count += 1

            await asyncio.sleep(1.5)

        await interaction.user.send(
            f"**一斉送信が完了しました**\n"
            f"成功: {success_count}件\n"
            f"失敗: {fail_count}件（DM拒否など）"
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(ToSCog(bot))