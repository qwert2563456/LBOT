import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime
from utils.ticket_system import generate_ticket_html

class ConfirmSupportCloseView(discord.ui.View):
    """チケットクローズの確認ダイアログ（一時的Viewなので再起動時の復元は不要）"""
    def __init__(self, opener_id: int, log_channel_id: int):
        super().__init__(timeout=60)
        self.opener_id = opener_id
        self.log_channel_id = log_channel_id

    @discord.ui.button(label="はい、閉じます", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        guild = interaction.guild
        bot = interaction.client
        
        try:
            # メッセージ履歴の取得
            messages = [msg async for msg in channel.history(limit=None, oldest_first=True)]
            
            # HTMLの生成
            html_content = await generate_ticket_html(
                messages, guild, channel, self.opener_id, interaction.user.id, bot
            )
            
            # ログファイルの一時保存
            filename = f"ticket-{channel.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            filepath = f"logs/{filename}"
            os.makedirs("logs", exist_ok=True)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html_content)
            
            # ログチャンネルへ送信
            log_channel = guild.get_channel(self.log_channel_id)
            if log_channel:
                file = discord.File(filepath, filename=filename)
                embed = discord.Embed(
                    title="サポートチケットログ",
                    description=(
                        f"**チャンネル:** {channel.name}\n"
                        f"**開いたユーザー:** <@{self.opener_id}>\n"
                        f"**閉じたユーザー:** {interaction.user.mention}\n"
                        f"**閉じた時間:** <t:{int(datetime.now().timestamp())}:F>"
                    ),
                    color=discord.Color.blue()
                )
                await log_channel.send(embed=embed, file=file)
            
            # 開設者のDMへ送信
            try:
                opener = await bot.fetch_user(self.opener_id)
                file = discord.File(filepath, filename=filename)
                await opener.send(f"チケット「{channel.name}」が閉じられました。ログを添付します。", file=file)
            except Exception:
                pass
                
            # 一時ファイルの削除とチャンネルの削除
            if os.path.exists(filepath):
                os.remove(filepath)
                
            await channel.delete()
            
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {str(e)}", ephemeral=True)
            
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()


class SupportTicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="verify_panel", description="簡易認証パネルを設置します（ロール付与のみ）")
    @app_commands.describe(role="認証後に付与するロール")
    @app_commands.default_permissions(administrator=True)
    async def verify_panel(self, interaction: discord.Interaction, role: discord.Role):
        """1コマンドで完結する簡易認証パネル"""
        embed = discord.Embed(
            title="認証",
            description=f"認証を完了すると、{role.mention} が付与されます。",
            color=discord.Color.blue()
        )
        
        view = discord.ui.View(timeout=None)
        btn = discord.ui.Button(
            label="認証する",
            style=discord.ButtonStyle.green,
            custom_id=f"verify_role_{role.id}"
        )
        view.add_item(btn)
        
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("認証パネルを設置しました。", ephemeral=True)

    @app_commands.command(name="support_panel", description="簡易サポートチケットパネルを設置します")
    @app_commands.describe(
        category="チケットチャンネルが作成されるカテゴリー",
        log_channel="クローズ時にHTMLログが送信されるチャンネル",
        title="パネルのタイトル（任意）",
        description="パネルの説明文（任意）"
    )
    @app_commands.default_permissions(administrator=True)
    async def support_panel(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        log_channel: discord.TextChannel,
        title: str = "サポートチケット",
        description: str = "お問い合わせやサポートが必要な場合は、下のボタンからチケットを開いてください。"
    ):
        """1コマンドで完結する簡易チケットパネル"""
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.green()
        )
        
        view = discord.ui.View(timeout=None)
        btn = discord.ui.Button(
            label="チケットを開く",
            style=discord.ButtonStyle.success,
            custom_id=f"st_open_{category.id}_{log_channel.id}"
        )
        view.add_item(btn)
        
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("サポートチケットパネルを設置しました。", ephemeral=True)

    # 動的なカスタムID（ロールIDやチャンネルIDが埋め込まれたID）を受け取るためのイベントリスナー
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
            
        custom_id = interaction.data.get("custom_id", "")
        
        # 1. 簡易認証ボタンの処理
        if custom_id.startswith("verify_role_"):
            role_id = int(custom_id.split("_")[2])
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.response.send_message("指定されたロールがDiscord上に見当たりません。", ephemeral=True)
                return
            
            try:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"認証が完了しました！\n{role.mention} ロールを付与しました。", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("ロールを付与する権限がBotにありません。Botのロール順位を確認してください。", ephemeral=True)
                
        # 2. チケットオープンボタンの処理
        elif custom_id.startswith("st_open_"):
            parts = custom_id.split("_")
            category_id = int(parts[2])
            log_channel_id = int(parts[3])
            
            await self._handle_ticket_open(interaction, category_id, log_channel_id)

        # 3. チケットクローズボタンの処理
        elif custom_id.startswith("st_close_"):
            parts = custom_id.split("_")
            opener_id = int(parts[2])
            log_channel_id = int(parts[3])
            
            view = ConfirmSupportCloseView(opener_id, log_channel_id)
            await interaction.response.send_message(
                "本当にこのチケットを閉じますか？\nログが生成され、チャンネルが削除されます。",
                view=view,
                ephemeral=True
            )

    async def _handle_ticket_open(self, interaction: discord.Interaction, category_id: int, log_channel_id: int):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        category = guild.get_channel(category_id)
        
        if not category:
            await interaction.followup.send("指定されたカテゴリーが見つからないため、チケットを作成できません。", ephemeral=True)
            return

        # スパム防止：既に同じ名前のチャンネルがあるかチェック
        channel_name = f"ticket-{interaction.user.name.lower()}"
        existing_channel = discord.utils.get(category.text_channels, name=channel_name)
        if existing_channel:
            await interaction.followup.send(f"既にチケットが開かれています: {existing_channel.mention}", ephemeral=True)
            return

        admin_role_id = int(os.getenv("ADMIN_ROLE_ID", "0"))
        admin_role = guild.get_role(admin_role_id)
        
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, read_message_history=True, attach_files=True
            )
        }
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            )
            
        try:
            channel = await category.create_text_channel(
                name=channel_name,
                overwrites=overwrites
            )
            
            embed = discord.Embed(
                title="サポートチケット",
                description="ご用件をお知らせください。運営スタッフが確認次第、対応いたします。\n終了する場合は下のボタンを押してください。",
                color=discord.Color.green()
            )
            
            view = discord.ui.View(timeout=None)
            close_btn = discord.ui.Button(
                label="チケットを閉じる",
                style=discord.ButtonStyle.danger,
                custom_id=f"st_close_{interaction.user.id}_{log_channel_id}"
            )
            view.add_item(close_btn)
            
            mention_text = f"{interaction.user.mention}"
            if admin_role:
                mention_text += f" {admin_role.mention}"
                
            await channel.send(content=mention_text, embed=embed, view=view)
            await interaction.followup.send(f"チケットを作成しました: {channel.mention}", ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {str(e)}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(SupportTicketSystem(bot))
