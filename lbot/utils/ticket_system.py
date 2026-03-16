import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime
from typing import Optional
import html

# 一時的な設定データを保存する辞書
temp_settings = {}

class PresetNameModal(discord.ui.Modal, title="プリセット名入力"):
    preset_name = discord.ui.TextInput(
        label="プリセット名",
        placeholder="例: サポート用",
        required=True,
        max_length=32
    )
    
    def __init__(self, bot, guild_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
    
    async def on_submit(self, interaction: discord.Interaction):
        name = self.preset_name.value.strip()
        settings = load_ticket_settings()
        guild_id = str(self.guild_id)
        
        if guild_id in settings and name in settings[guild_id].get("presets", {}):
            await interaction.response.send_message(
                f"プリセット「{name}」は既に存在します。",
                ephemeral=True
            )
            return
        
        # 一時データを初期化
        temp_key = f"{self.guild_id}_{name}"
        temp_settings[temp_key] = {
            "panel_title": "",
            "panel_description": "",
            "button_text": "チケットを開く",
            "welcome_message": "",
            "max_tickets": 3,
            "category_id": 0,
            "log_channel_id": 0,
            "staff_role_id": None,
            "viewer_role_id": None
        }
        
        # 編集パネルを表示
        view = PresetEditView(self.bot, name, self.guild_id)
        embed = create_preset_status_embed(name, temp_settings[temp_key])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class PresetSelectView(discord.ui.View):
    """既存プリセット選択用ビュー"""
    def __init__(self, bot, guild_id: int, mode: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.mode = mode
        
        settings = load_ticket_settings()
        guild_id_str = str(guild_id)
        
        if guild_id_str in settings and settings[guild_id_str].get("presets"):
            options = []
            for preset_name, preset_data in settings[guild_id_str]["presets"].items():
                options.append(
                    discord.SelectOption(
                        label=preset_name,
                        description=preset_data["panel_title"][:50],
                        value=preset_name
                    )
                )
            
            select = discord.ui.Select(
                placeholder="プリセットを選択してください",
                options=options
            )
            select.callback = self.select_callback
            self.add_item(select)
    
    async def select_callback(self, interaction: discord.Interaction):
        selected_preset = interaction.data["values"][0]
        
        if self.mode == "edit":
            # 既存データを一時データにコピー
            settings = load_ticket_settings()
            guild_id_str = str(self.guild_id)
            preset = settings[guild_id_str]["presets"][selected_preset]
            
            temp_key = f"{self.guild_id}_{selected_preset}"
            temp_settings[temp_key] = preset.copy()
            
            view = PresetEditView(self.bot, selected_preset, self.guild_id)
            embed = create_preset_status_embed(selected_preset, temp_settings[temp_key])
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        
        elif self.mode == "delete":
            view = ConfirmDeleteView(self.bot, selected_preset, self.guild_id)
            await interaction.response.send_message(
                f"プリセット「{selected_preset}」を削除しますか？\nこの操作は取り消せません。",
                view=view,
                ephemeral=True
            )

class ConfirmDeleteView(discord.ui.View):
    """削除確認ビュー"""
    def __init__(self, bot, preset_name: str, guild_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.preset_name = preset_name
        self.guild_id = guild_id
    
    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_ticket_settings()
        guild_id_str = str(self.guild_id)
        
        if guild_id_str in settings and self.preset_name in settings[guild_id_str]["presets"]:
            del settings[guild_id_str]["presets"][self.preset_name]
            save_ticket_settings(settings)
            
            await interaction.response.send_message(
                f"プリセット「{self.preset_name}」を削除しました。",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"プリセット「{self.preset_name}」が見つかりません。",
                ephemeral=True
            )
        self.stop()
    
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

def create_preset_status_embed(preset_name: str, data: dict) -> discord.Embed:
    """プリセットの現在の設定状態を表示するEmbedを生成"""
    embed = discord.Embed(
        title=f"プリセット設定: {preset_name}",
        description="各項目のボタンをクリックして設定を入力してください",
        color=discord.Color.blue()
    )
    
    # テキスト設定
    embed.add_field(
        name="パネルタイトル",
        value=data["panel_title"] if data["panel_title"] else "❌ 未設定",
        inline=False
    )
    embed.add_field(
        name="パネル説明文",
        value=data["panel_description"][:100] if data["panel_description"] else "❌ 未設定",
        inline=False
    )
    embed.add_field(
        name="ボタンの文字",
        value=data["button_text"] if data["button_text"] else "❌ 未設定",
        inline=False
    )
    embed.add_field(
        name="ウェルカムメッセージ",
        value=data["welcome_message"][:100] if data["welcome_message"] else "❌ 未設定",
        inline=False
    )
    
    # 数値・ID設定
    embed.add_field(
        name="同時チケット上限",
        value=f"{data['max_tickets']}件/人" if data["max_tickets"] else "❌ 未設定",
        inline=True
    )
    embed.add_field(
        name="カテゴリーID",
        value=str(data["category_id"]) if data["category_id"] else "❌ 未設定",
        inline=True
    )
    embed.add_field(
        name="ログチャンネルID",
        value=str(data["log_channel_id"]) if data["log_channel_id"] else "❌ 未設定",
        inline=True
    )
    embed.add_field(
        name="スタッフロールID",
        value=str(data["staff_role_id"]) if data.get("staff_role_id") else "⚪ 未設定（任意）",
        inline=True
    )
    embed.add_field(
        name="閲覧ロールID",
        value=str(data["viewer_role_id"]) if data.get("viewer_role_id") else "⚪ 未設定（任意）",
        inline=True
    )
    
    return embed

class PresetEditView(discord.ui.View):
    """プリセット編集ビュー"""
    def __init__(self, bot, preset_name: str, guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.preset_name = preset_name
        self.guild_id = guild_id
        self.temp_key = f"{guild_id}_{preset_name}"
    
    @discord.ui.button(label="パネルタイトル", style=discord.ButtonStyle.primary, row=0)
    async def edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="パネルタイトル設定",
            label="パネルタイトル",
            placeholder="例: サポートチケット",
            field_key="panel_title",
            temp_key=self.temp_key,
            max_length=256
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="パネル説明文", style=discord.ButtonStyle.primary, row=0)
    async def edit_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="パネル説明文設定",
            label="説明文",
            placeholder="例: 問題がある場合はチケットを開いてください",
            field_key="panel_description",
            temp_key=self.temp_key,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="ボタンの文字", style=discord.ButtonStyle.primary, row=0)
    async def edit_button_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="ボタン文字設定",
            label="ボタンの文字",
            placeholder="例: チケットを開く",
            field_key="button_text",
            temp_key=self.temp_key,
            max_length=80
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="ウェルカムメッセージ", style=discord.ButtonStyle.primary, row=1)
    async def edit_welcome(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="ウェルカムメッセージ設定",
            label="メッセージ",
            placeholder="例: サポートチームがすぐに対応します",
            field_key="welcome_message",
            temp_key=self.temp_key,
            style=discord.TextStyle.paragraph,
            max_length=2000
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="チケット上限", style=discord.ButtonStyle.secondary, row=1)
    async def edit_max_tickets(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="チケット上限設定",
            label="同時チケット上限",
            placeholder="例: 3",
            field_key="max_tickets",
            temp_key=self.temp_key,
            max_length=2,
            is_number=True
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="カテゴリーID", style=discord.ButtonStyle.secondary, row=1)
    async def edit_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="カテゴリーID設定",
            label="カテゴリーID",
            placeholder="例: 1234567890123456789",
            field_key="category_id",
            temp_key=self.temp_key,
            max_length=20,
            is_number=True
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="ログチャンネルID", style=discord.ButtonStyle.secondary, row=2)
    async def edit_log_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="ログチャンネルID設定",
            label="ログチャンネルID",
            placeholder="例: 1234567890123456789",
            field_key="log_channel_id",
            temp_key=self.temp_key,
            max_length=20,
            is_number=True
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="スタッフロールID", style=discord.ButtonStyle.secondary, row=2)
    async def edit_staff_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="スタッフロールID設定",
            label="スタッフロールID（任意）",
            placeholder="例: 1234567890123456789",
            field_key="staff_role_id",
            temp_key=self.temp_key,
            max_length=20,
            is_number=True,
            required=False
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="閲覧ロールID", style=discord.ButtonStyle.secondary, row=2)
    async def edit_viewer_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = SingleFieldModal(
            title="閲覧ロールID設定",
            label="閲覧ロールID（任意）",
            placeholder="例: 1234567890123456789",
            field_key="viewer_role_id",
            temp_key=self.temp_key,
            max_length=20,
            is_number=True,
            required=False
        )
        modal.callback_view = self
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="保存", style=discord.ButtonStyle.success, row=3)
    async def save_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = temp_settings.get(self.temp_key)
        
        if not data:
            await interaction.response.send_message("データが見つかりません。", ephemeral=True)
            return
        
        # 必須項目チェック
        required_fields = ["panel_title", "panel_description", "button_text", "welcome_message"]
        missing = [f for f in required_fields if not data.get(f)]
        
        if missing or not data.get("category_id") or not data.get("log_channel_id"):
            await interaction.response.send_message(
                "❌ 必須項目が未入力です。\n"
                "パネルタイトル、説明文、ボタン文字、ウェルカムメッセージ、\n"
                "カテゴリーID、ログチャンネルIDは必須です。",
                ephemeral=True
            )
            return
        
        # 保存
        settings = load_ticket_settings()
        guild_id_str = str(self.guild_id)
        
        if guild_id_str not in settings:
            settings[guild_id_str] = {"presets": {}}
        
        settings[guild_id_str]["presets"][self.preset_name] = data.copy()
        save_ticket_settings(settings)
        
        # 一時データを削除
        if self.temp_key in temp_settings:
            del temp_settings[self.temp_key]
        
        await interaction.response.send_message(
            f"プリセット「{self.preset_name}」を保存しました！\n"
            f"`/ticket panel {self.preset_name}` でパネルを設置できます。",
            ephemeral=True
        )
        self.stop()
    
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, row=3)
    async def cancel_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 一時データを削除
        if self.temp_key in temp_settings:
            del temp_settings[self.temp_key]
        
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()

class SingleFieldModal(discord.ui.Modal):
    """単一フィールド編集用モーダル"""
    def __init__(self, title: str, label: str, placeholder: str, field_key: str, 
                 temp_key: str, style=discord.TextStyle.short, max_length: int = 100,
                 is_number: bool = False, required: bool = True):
        super().__init__(title=title)
        self.field_key = field_key
        self.temp_key = temp_key
        self.is_number = is_number
        self.callback_view = None
        
        current_value = temp_settings.get(temp_key, {}).get(field_key, "")
        if current_value and is_number:
            current_value = str(current_value)
        
        self.input_field = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            style=style,
            max_length=max_length,
            required=required,
            default=str(current_value) if current_value else ""
        )
        self.add_item(self.input_field)
    
    async def on_submit(self, interaction: discord.Interaction):
        value = self.input_field.value.strip()
        
        # 数値変換
        if self.is_number and value:
            try:
                value = int(value)
            except ValueError:
                await interaction.response.send_message(
                    "数値を入力してください。",
                    ephemeral=True
                )
                return
        elif self.is_number and not value:
            value = None
        
        # 一時データに保存
        if self.temp_key not in temp_settings:
            temp_settings[self.temp_key] = {}
        
        temp_settings[self.temp_key][self.field_key] = value
        
        # パネルを更新
        preset_name = self.temp_key.split("_", 1)[1]
        embed = create_preset_status_embed(preset_name, temp_settings[self.temp_key])
        
        await interaction.response.edit_message(embed=embed, view=self.callback_view)

class TicketSetupView(discord.ui.View):
    """メイン設定パネル"""
    def __init__(self, bot):
        super().__init__(timeout=180)
        self.bot = bot
    
    @discord.ui.button(label="新規プリセット作成", style=discord.ButtonStyle.green)
    async def create_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PresetNameModal(self.bot, interaction.guild_id))
    
    @discord.ui.button(label="既存プリセット編集", style=discord.ButtonStyle.primary)
    async def edit_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_ticket_settings()
        guild_id = str(interaction.guild_id)
        
        if guild_id not in settings or not settings[guild_id].get("presets"):
            await interaction.response.send_message(
                "プリセットが見つかりません。",
                ephemeral=True
            )
            return
        
        view = PresetSelectView(self.bot, interaction.guild_id, "edit")
        await interaction.response.send_message(
            "編集するプリセットを選択してください：",
            view=view,
            ephemeral=True
        )
    
    @discord.ui.button(label="プリセット一覧", style=discord.ButtonStyle.secondary)
    async def list_presets(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_ticket_settings()
        guild_id = str(interaction.guild_id)
        
        if guild_id not in settings or not settings[guild_id].get("presets"):
            await interaction.response.send_message(
                "プリセットが見つかりません。",
                ephemeral=True
            )
            return
        
        embed = discord.Embed(
            title="チケットプリセット一覧",
            color=discord.Color.blue()
        )
        
        for name, preset in settings[guild_id]["presets"].items():
            embed.add_field(
                name=f"{name}",
                value=f"**タイトル:** {preset['panel_title']}\n"
                      f"**上限:** {preset['max_tickets']}件/人",
                inline=False
            )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @discord.ui.button(label="プリセット削除", style=discord.ButtonStyle.danger)
    async def delete_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        settings = load_ticket_settings()
        guild_id = str(interaction.guild_id)
        
        if guild_id not in settings or not settings[guild_id].get("presets"):
            await interaction.response.send_message(
                "プリセットが見つかりません。",
                ephemeral=True
            )
            return
        
        view = PresetSelectView(self.bot, interaction.guild_id, "delete")
        await interaction.response.send_message(
            "削除するプリセットを選択してください：",
            view=view,
            ephemeral=True
        )

class TicketPanelView(discord.ui.View):
    def __init__(self, bot, preset_data):
        super().__init__(timeout=None)
        self.bot = bot
        self.preset_data = preset_data
        
        button = discord.ui.Button(
            label=preset_data["button_text"],
            style=discord.ButtonStyle.green,
            custom_id=f"ticket_open_{preset_data['panel_title']}"
        )
        button.callback = self.open_ticket
        self.add_item(button)
    
    async def open_ticket(self, interaction: discord.Interaction):
        # チケット上限チェック
        active_tickets = load_active_tickets()
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        
        if guild_id not in active_tickets:
            active_tickets[guild_id] = {}
        
        user_tickets = active_tickets[guild_id].get(user_id, [])
        
        if len(user_tickets) >= self.preset_data["max_tickets"]:
            await interaction.response.send_message(
                f"同時に開けるチケットは{self.preset_data['max_tickets']}件までです。",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # カテゴリー取得
            category = interaction.guild.get_channel(self.preset_data["category_id"])
            if not category:
                await interaction.followup.send("カテゴリーが見つかりません。", ephemeral=True)
                return
            
            # チケット番号を取得
            ticket_num = len([ch for ch in category.channels if ch.name.startswith("ticket-")]) + 1
            
            # チャンネル作成
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                interaction.user: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True
                ),
                interaction.guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    read_message_history=True
                )
            }
            
            # スタッフロール権限
            if self.preset_data.get("staff_role_id"):
                staff_role = interaction.guild.get_role(self.preset_data["staff_role_id"])
                if staff_role:
                    overwrites[staff_role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True
                    )
            
            # 閲覧ロール権限
            if self.preset_data.get("viewer_role_id"):
                viewer_role = interaction.guild.get_role(self.preset_data["viewer_role_id"])
                if viewer_role:
                    overwrites[viewer_role] = discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False
                    )
            
            channel = await category.create_text_channel(
                name=f"￤ 🎫-{interaction.user.name} ￤",
                overwrites=overwrites
            )
            
            # ウェルカムメッセージ送信
            embed = discord.Embed(
                title=self.preset_data["panel_title"],
                description=self.preset_data["welcome_message"],
                color=discord.Color.green()
            )
            
            mention_text = ""
            if self.preset_data.get("staff_role_id"):
                mention_text = f"<@&{self.preset_data['staff_role_id']}>"
            
            close_view = TicketCloseView(self.bot, self.preset_data, interaction.user.id)
            
            await channel.send(
                content=f"{interaction.user.mention} {mention_text}",
                embed=embed,
                view=close_view
            )
            
            # アクティブチケットに追加
            if user_id not in active_tickets[guild_id]:
                active_tickets[guild_id][user_id] = []
            
            active_tickets[guild_id][user_id].append({
                "channel_id": channel.id,
                "created_at": datetime.now().isoformat(),
                "preset_name": self.preset_data.get("preset_name", "unknown")
            })
            
            save_active_tickets(active_tickets)
            
            await interaction.followup.send(
                f"チケットを作成しました: {channel.mention}",
                ephemeral=True
            )
            
        except Exception as e:
            await interaction.followup.send(f"❌ エラーが発生しました: {str(e)}", ephemeral=True)

class TicketCloseView(discord.ui.View):
    def __init__(self, bot, preset_data, opener_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.preset_data = preset_data
        self.opener_id = opener_id
        
        button = discord.ui.Button(
            label="チケットを閉じる",
            style=discord.ButtonStyle.red,
            custom_id=f"ticket_close_{opener_id}"
        )
        button.callback = self.close_ticket
        self.add_item(button)
    
    async def close_ticket(self, interaction: discord.Interaction):
        # 確認ダイアログ
        confirm_view = ConfirmCloseView(self.bot, self.preset_data, self.opener_id)
        await interaction.response.send_message(
            "本当にこのチケットを閉じますか？\nログが生成され、チャンネルが削除されます。",
            view=confirm_view,
            ephemeral=True
        )

class ConfirmCloseView(discord.ui.View):
    def __init__(self, bot, preset_data, opener_id):
        super().__init__(timeout=60)
        self.bot = bot
        self.preset_data = preset_data
        self.opener_id = opener_id
    
    @discord.ui.button(label="はい、閉じます", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        
        channel = interaction.channel
        guild = interaction.guild
        
        try:
            # メッセージ履歴取得
            messages = []
            async for msg in channel.history(limit=None, oldest_first=True):
                messages.append(msg)
            
            # HTML生成
            html_content = await generate_ticket_html(
                messages,
                guild,
                channel,
                self.opener_id,
                interaction.user.id,
                self.bot
            )
            
            # ファイル保存（一時的）
            filename = f"ticket-{channel.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
            filepath = f"logs/{filename}"
            
            os.makedirs("logs", exist_ok=True)
            
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html_content)
            
            try:
                # ログチャンネルに送信
                log_channel = guild.get_channel(self.preset_data["log_channel_id"])
                if log_channel:
                    file = discord.File(filepath, filename=filename)
                    embed = discord.Embed(
                        title="🎫 チケットログ",
                        description=f"**チャンネル:** {channel.name}\n"
                                    f"**開いたユーザー:** <@{self.opener_id}>\n"
                                    f"**閉じたユーザー:** {interaction.user.mention}\n"
                                    f"**閉じた時間:** {datetime.now().strftime('%Y年%m月%d日 %H:%M')}",
                        color=discord.Color.blue()
                    )
                    await log_channel.send(embed=embed, file=file)
                
                # 開設者にDM送信
                try:
                    opener = await self.bot.fetch_user(self.opener_id)
                    file = discord.File(filepath, filename=filename)
                    await opener.send(
                        f"チケット「{channel.name}」が閉じられました。",
                        file=file
                    )
                except:
                    pass
                
            finally:
                # ファイルを削除
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except Exception as e:
                    print(f"[ログ削除失敗] {filename}: {e}")
            
            # アクティブチケットから削除
            active_tickets = load_active_tickets()
            guild_id = str(guild.id)
            user_id = str(self.opener_id)
            
            if guild_id in active_tickets and user_id in active_tickets[guild_id]:
                active_tickets[guild_id][user_id] = [
                    t for t in active_tickets[guild_id][user_id]
                    if t["channel_id"] != channel.id
                ]
                save_active_tickets(active_tickets)
            
            # チャンネル削除
            await channel.delete()
            
        except Exception as e:
            await interaction.followup.send(f"エラーが発生しました: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("キャンセルしました。", ephemeral=True)
        self.stop()


async def generate_ticket_html(messages, guild, channel, opener_id, closer_id, bot):
    """Discord風の高品質HTMLログを生成（完全一致版）"""
    
    # ユーザー取得（エラーハンドリング込み）
    try:
        opener = await guild.fetch_member(opener_id)
        opener_name = html.escape(opener.display_name)
    except:
        opener_name = "Unknown User"
        
    try:
        closer = await guild.fetch_member(closer_id)
        closer_name = html.escape(closer.display_name)
    except:
        closer_name = "Unknown User"
    
    # ユーザー情報を収集（プロフィールポップアップ用）
    users_data = {}
    for msg in messages:
        user_id = msg.author.id
        if user_id not in users_data:
            try:
                member = await guild.fetch_member(user_id)
                users_data[user_id] = {
                    'username': html.escape(msg.author.name),
                    'display_name': html.escape(msg.author.display_name),
                    'discriminator': getattr(msg.author, 'discriminator', '') if getattr(msg.author, 'discriminator', '0') != '0' else '',
                    'avatar_url': msg.author.display_avatar.url if msg.author.display_avatar else 'https://cdn.discordapp.com/embed/avatars/0.png',
                    'bot': msg.author.bot,
                    'created_at': msg.author.created_at.strftime('%Y-%m-%d'),
                    'joined_at': member.joined_at.strftime('%Y-%m-%d') if member.joined_at else 'N/A',
                    'color': f"#{member.color.value:06x}" if member.color != discord.Color.default() else '#ffffff'
                }
            except:
                users_data[user_id] = {
                    'username': html.escape(msg.author.name),
                    'display_name': html.escape(msg.author.display_name),
                    'discriminator': '',
                    'avatar_url': msg.author.display_avatar.url if msg.author.display_avatar else 'https://cdn.discordapp.com/embed/avatars/0.png',
                    'bot': msg.author.bot,
                    'created_at': 'Unknown',
                    'joined_at': 'Unknown',
                    'color': '#ffffff'
                }
    
    # メッセージHTML生成
    messages_html = ""
    prev_author_id = None
    message_count_by_user = {}
    
    for msg in messages:
        author_id = msg.author.id
        user_data = users_data[author_id]
        
        # メッセージカウント
        message_count_by_user[author_id] = message_count_by_user.get(author_id, 0) + 1
        
        author_name = user_data['display_name']
        username = user_data['username']
        content = html.escape(msg.content) if msg.content else ""
        timestamp = msg.created_at.strftime("%d-%m-%Y %H:%M")
        timestamp_full = msg.created_at.strftime("%A, %d %B %Y %H:%M")
        avatar_url = user_data['avatar_url']
        author_color = user_data['color']
        
        # BOTタグ（添付ファイルに合わせた APP / BOT 表示）
        bot_tag = '<span class="chatlog__bot-tag">APP</span>' if msg.author.bot else ""
        
        # Embedの処理
        embeds_html = ""
        for embed in msg.embeds:
            e_color = f"rgba({embed.color.r}, {embed.color.g}, {embed.color.b}, 1)" if embed.color else "rgba(79, 84, 92, 1)"
            e_title = f'<div class="chatlog__embed-title"><span class="markdown">{html.escape(embed.title)}</span></div>' if embed.title else ""
            e_desc = f'<div class="chatlog__embed-description"><span class="markdown preserve-whitespace">{html.escape(embed.description)}</span></div>' if embed.description else ""
            
            # Embedフィールド
            fields_html = ""
            for field in getattr(embed, 'fields', []):
                inline_class = " chatlog__embed-field--inline" if field.inline else ""
                fields_html += f'''
                <div class="chatlog__embed-field{inline_class}">
                    <div class="chatlog__embed-field-name">{html.escape(field.name)}</div>
                    <div class="chatlog__embed-field-value">{html.escape(field.value)}</div>
                </div>'''
            e_fields = f'<div class="chatlog__embed-fields">{fields_html}</div>' if fields_html else ""
            
            # Embedフッター
            e_footer = ""
            if embed.footer and embed.footer.text:
                f_icon = f'<img class="chatlog__embed-footer-icon" src="{embed.footer.icon_url}">' if embed.footer.icon_url else ""
                e_footer = f'<div class="chatlog__embed-footer">{f_icon}<span class="chatlog__embed-footer-text">{html.escape(embed.footer.text)}</span></div>'
                
            embeds_html += f'''
            <div class="chatlog__embed">
                <div class="chatlog__embed-color-pill" style="background-color:{e_color}"></div>
                <div class="chatlog__embed-content-container">
                    <div class="chatlog__embed-content">
                        <div class="chatlog__embed-text">{e_title}{e_desc}{e_fields}</div>
                    </div>
                    {e_footer}
                </div>
            </div>'''
            
        # コンポーネント(ボタン等)の処理
        components_html = ""
        if hasattr(msg, 'components') and msg.components:
            components_html += '<div class="chatlog__components">'
            for row in msg.components:
                for child in getattr(row, 'children', []):
                    label = html.escape(getattr(child, 'label', 'Button') or 'Button')
                    # 色の簡易判定（赤: D83C3E, 青: 5865F2, 灰: 4f545c 等）
                    btn_color = "#5865F2" if getattr(child, 'style', None) == discord.ButtonStyle.primary else "#4f545c"
                    if getattr(child, 'style', None) == discord.ButtonStyle.danger: btn_color = "#D83C3E"
                    if getattr(child, 'style', None) == discord.ButtonStyle.success: btn_color = "#2D7D46"
                    components_html += f'''
                    <div class="chatlog__component-button" style="background-color:{btn_color}">
                        <a href="javascript:;" style="text-decoration:none"><span class="chatlog__button-label">{label}</span></a>
                    </div>'''
            components_html += '</div>'

        # 連続メッセージかどうか判定
        is_followup = prev_author_id == author_id
        
        if not is_followup:
            if messages_html:
                messages_html += "</div>" # 前のグループを閉じる
            messages_html += f'''
    <div class="chatlog__message-group">
        <div id="chatlog__message-container-{msg.id}" class="chatlog__message-container" data-message-id="{msg.id}">
            <div class="chatlog__message">
                <div class="chatlog__message-aside">
                    <img class="chatlog__avatar" src="{avatar_url}" data-user-id="{author_id}" />
                </div>
                <div class="chatlog__message-primary">
                    <div class="chatlog__header">
                        <span class="chatlog__author-name" title="{username}" data-user-id="{author_id}" style="color: {author_color};">{author_name}</span>
                        {bot_tag}
                        <span class="chatlog__timestamp" data-timestamp="{timestamp_full}">{timestamp}</span>
                    </div>
                    <div class="chatlog__content chatlog__markdown" data-message-id="{msg.id}" id="message-{msg.id}">
                        <span class="chatlog__markdown-preserve">{content}</span>
                        {embeds_html}
                        {components_html}
                    </div>
                </div>
            </div>
        </div>'''
        else:
            messages_html += f'''
        <div id="chatlog__message-container-{msg.id}" class="chatlog__message-container" data-message-id="{msg.id}">
            <div class="chatlog__message">
                <div class="chatlog__message-aside">
                    <div class="chatlog__short-timestamp" data-timestamp="{timestamp_full}">{msg.created_at.strftime("%H:%M")}</div>
                </div>
                <div class="chatlog__message-primary">
                    <div class="chatlog__content chatlog__markdown" data-message-id="{msg.id}" id="message-{msg.id}">
                        <span class="chatlog__markdown-preserve">{content}</span>
                        {embeds_html}
                        {components_html}
                    </div>
                </div>
            </div>
        </div>'''
        
        prev_author_id = author_id
    
    if messages:
        messages_html += "</div>"

    # ユーザープロフィールポップアップHTML生成
    user_popouts = ""
    for user_id, user_data in users_data.items():
        discriminator_html = f'<div class="meta__discriminator">#{user_data["discriminator"]}</div>' if user_data['discriminator'] else ''
        display_name_html = f'<div class="meta__display-name">{user_data["display_name"]}</div>' if user_data['display_name'] != user_data['username'] else ''
        bot_tag_meta = '<span class="chatlog__bot-tag">APP</span>' if user_data['bot'] else ''
        
        user_popouts += f'''
<div id="meta-popout-{user_id}" class="meta-popout">
    <div class="meta__header">
         <img src="{user_data['avatar_url']}" alt="Avatar">
    </div>
    <div class="meta__description">
        {display_name_html}
        <div class="meta__details">
            <div class="meta__user">{user_data['username']}</div>
            {discriminator_html}
            {bot_tag_meta}
        </div>
        <div class="meta__divider-2"></div>
        <div class="meta__field">
            <div class="meta__title">メンバーになった日</div>
            <div class="meta__value"><img src="https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/discord-logo.svg"/> {user_data['created_at']} <div class="meta__divider"></div> <img src="{guild.icon.url if guild.icon else ''}" class="meta__img-border" style="width:16px;height:16px;"/> {user_data['joined_at']}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">ユーザーID</div>
            <div class="meta__value">{user_id}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">メッセージ合計数</div>
            <div class="meta__value">{message_count_by_user.get(user_id, 0)}</div>
        </div>
    </div>
</div>
'''
    
    close_time = datetime.now().strftime('%Y/%m/%d %H:%M:%S')
    channel_created = channel.created_at.strftime('%Y/%m/%d %H:%M:%S')
    
    # --------------------------------------------------------
    # 定数としてCSSとJavaScriptを定義 (中括弧のエスケープ地獄を避けるため)
    # --------------------------------------------------------
    CSS_CONTENT = """
        @font-face { font-family: gg sans; font-weight: 400; src: url(https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/ggsans-400.woff2) format("woff2") }
        @font-face { font-family: gg sans; font-weight: 500; src: url(https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/ggsans-500.woff2) format("woff2") }
        @font-face { font-family: gg sans; font-weight: 600; src: url(https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/ggsans-600.woff2) format("woff2") }
        @font-face { font-family: gg sans; font-weight: 700; src: url(https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/ggsans-700.woff2) format("woff2") }
        @font-face { font-family: gg sans; font-weight: 800; src: url(https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/ggsans-800.woff2) format("woff2") }
        a { text-decoration: none; color: #0096cf; }
        a:hover { text-decoration: underline; }
        img { object-fit: contain; }
        .chatlog__markdown, .markdown { max-width: 100%; line-height: 1.3; overflow-wrap: break-word; }
        .chatlog__markdown-preserve, .preserve-whitespace { white-space: pre-wrap; }
        .spoiler { display: inline-block; }
        .spoiler--hidden { cursor: pointer; background-color: #202225; }
        .spoiler-text { border-radius: 3px; background-color: rgba(255, 255, 255, 0.1); }
        .spoiler--hidden .spoiler-text { opacity: 0; background-color: #202225; }
        .spoiler--hidden:hover .spoiler-text { background-color: rgba(32, 34, 37, 0.8); }
        .mention { border-radius: 3px; padding: 0 2px; color: #dee0fc; background: rgba(88, 101, 242, .3); font-weight: 500; }
        .mention:hover { background: rgba(88, 101, 242, .6); }
        .emoji { width: 1.25em; height: 1.25em; margin: 0 0.06em; vertical-align: -0.1em; }
        .emoji--small { width: 1em; height: 1em; }
        .chatlog { padding: 1rem 5px 0.125rem 2px; border-top: 1px solid rgba(255, 255, 255, 0.1); }
        .chatlog__header { margin-bottom: 0.1rem; }
        .chatlog__message-aside { grid-column: 1; width: 72px; padding: 0.15rem 0.15rem 0 0.15rem; text-align: center; }
        .chatlog__message:hover { background-color: #32353b; }
        .chatlog__message:hover .chatlog__short-timestamp { display: block; }
        .chatlog__short-timestamp { display: none; color: #a3a6aa; font-size: 0.75rem; font-weight: 500; direction: ltr; unicode-bidi: bidi-override; }
        .chatlog__message-primary { grid-column: 2; min-width: 0; }
        .chatlog__author-name { font-weight: 500; color: #ffffff; }
        .chatlog__author-name:hover { text-decoration: underline; cursor: pointer; }
        .chatlog__timestamp { margin-left: 0.3rem; color: #9599a2; font-size: 0.75rem; font-weight: 500; direction: ltr; unicode-bidi: bidi-override; }
        .chatlog__content { padding-right: 1rem; font-size: 0.95rem; word-wrap: break-word; }
        .chatlog__message { display: grid; grid-template-columns: auto 1fr; padding: 0.15rem 0; direction: ltr; unicode-bidi: bidi-override; }
        .chatlog__avatar { width: 40px; height: 40px; border-radius: 50%; }
        .chatlog__avatar:hover { cursor: pointer; }
        
        /* Embeds & Components */
        .chatlog__embed { display: flex; margin-top: 0.3em; max-width: 520px; }
        .chatlog__embed-color-pill { flex-shrink: 0; width: 0.25em; border-top-left-radius: 3px; border-bottom-left-radius: 3px; }
        .chatlog__embed-content-container { display: flex; flex-direction: column; padding: 0.5em 0.6em; border: 1px solid rgba(46, 48, 54, 0.6); border-top-right-radius: 3px; border-bottom-right-radius: 3px; background-color: rgba(46, 48, 54, 0.3); }
        .chatlog__embed-content { display: flex; width: 100%; }
        .chatlog__embed-text { flex: 1; }
        .chatlog__embed-title { margin-bottom: 0.2em; font-size: 0.875em; font-weight: 600; color: #ffffff; }
        .chatlog__embed-description { font-weight: 500; font-size: 0.85em; color: rgba(255, 255, 255, 0.6); }
        .chatlog__embed-fields { display: flex; flex-wrap: wrap; }
        .chatlog__embed-field { flex: 0; min-width: 100%; max-width: 506px; padding-top: 0.6em; font-size: 0.875em; }
        .chatlog__embed-field--inline { flex: 1; flex-basis: auto; min-width: 150px; }
        .chatlog__embed-field-name { margin-bottom: 0.2em; font-weight: 600; color: #ffffff; }
        .chatlog__embed-field-value { font-weight: 500; color: rgba(255, 255, 255, 0.6); }
        .chatlog__embed-footer { margin-top: 0.6em; color: rgba(255, 255, 255, 0.6); }
        .chatlog__embed-footer-icon { margin-right: 0.2em; width: 20px; height: 20px; border-radius: 50%; vertical-align: middle; }
        .chatlog__embed-footer-text { font-size: 0.75em; font-weight: 500; }
        .chatlog__components { display: flex; flex-wrap: wrap; }
        .chatlog__component-button { display: flex; align-items: center; margin: 0.35em 0.1em 0.1em 0.1em; padding: 0.2em 0.35em; border-radius: 2px; cursor: pointer; }
        .chatlog__button-label { min-width: 9px; margin-left: 0.35em; margin-right: 0.35em; font-size: 0.875em; color: white; font-weight: 500; }

        .chatlog__bot-tag { background: #5865f2; color: #ffffff; padding: 0 0.2rem; margin-top: 0.5em; border-radius: 0.1875rem; margin-left: 0.07rem; position: relative; vertical-align: top; display: inline-flex; flex-shrink: 0; text-indent: 0; font-weight: 500; font-size: 10px; line-height: 15px; }
        html, body { height: 100%; width: 100%; background-color: #36393f; font-family: "gg sans", Helvetica, Arial, sans-serif; font-size: 17px; color: #fff; margin: 0; padding: 0; overflow: auto; }
        .chatlog__message-group { margin-bottom: 1rem; border-color: rgba(255, 255, 255, 0.1); }
        .chatlog__message-container { background-color: transparent; transition: background-color 0.5s ease; }
        .chatlog__message-container--highlighted { background-color: rgba(114, 137, 218, 0.2) !important; transition: none !important; }
        
        /* Layout */
        .panel { display: flex; flex-shrink: 0; align-items: center; padding: 6px 0 6px 0; user-select: none; font-weight: 700; font-size: 20px; box-shadow: 0 1px 0 rgba(4, 4, 5, 0.2), 0 1.5px 0 rgba(6, 6, 7, 0.05), 0 2px 0 rgba(4, 4, 5, 0.05); }
        .panel__hashtag-icon { width: 24px; height: 24px; margin-left: 16px; margin-right: 8px; margin-top: 1px; }
        .panel__channel-topic { border-left-style: solid; border-color: #4f545c; border-width: 1px; color: #b9bbbe; margin-left: 10px; padding-left: 10px; font-size: 14px; line-height: 18px; height: 18px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 500; }
        .panel__summary-button { display: flex; align-items: center; padding: 0.2em 0.35em; border-radius: 2px; margin-left: auto; margin-right: 50px; color: #b9bbbe; }
        .panel__summary-button:hover { color: #fff; cursor: pointer; }
        .main { display: flex; overflow-y: auto; height: calc(100% - 126px); flex-direction: column; }
        .main::-webkit-scrollbar { width: 8px; background-color: rgba(0,0,0,0); }
        .main::-webkit-scrollbar-thumb { background-color: #202225; border: 0 #36393f solid; border-left-width: 1px; border-right-width: 1px; }
        .buffer { flex: 1; }
        .info { margin: 0 16px; padding-bottom: 12px; display: flex; flex-direction: column; justify-content: flex-end; flex-shrink: 0; user-select: none; }
        .info__title { font-size: 32px; font-weight: 700; line-height: 40px; }
        .info__subject { color: #b9bbbe; font-size: 16px; line-height: 20px; }
        .footer { flex-shrink: 0; display: flex; align-items: center; height: 20px; border-radius: 5px; margin: 0 16px 16px; padding: 16px; background-color: #202225; position: relative; z-index: 10; user-select: none; }
        .footer__text { font-size: 16px; line-height: 20px; font-weight: 400; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        
        /* Popouts & Menus */
        #context-menu { position: absolute; width: 188px; border-radius: 4px; padding: 6px 8px; background-color: #18191c; box-shadow: 0 8px 16px rgba(0, 0, 0, .24); font-weight: 500; font-size: 14px; line-height: 18px; color: #b9bbbe; transform: scale(0); transform-origin: top left; z-index: 6969; }
        #context-menu.visible { transform: scale(1); transition: transform 200ms ease-in-out; }
        #context-menu .item { margin: 2px 0; padding: 0 8px; display: flex; align-items: center; border-radius: 2px; min-height: 32px; cursor: pointer; }
        #context-menu .item:hover { color: #fff; }
        .summary-popout, .meta-popout { position: absolute; z-index: 6969; background-color: #292b2f; box-shadow: 0 2px 10px 0 rgb(0 0 0 / 20%), 0 0 0 1px rgb(32 34 37 / 60%); width: 280px; border-radius: 5px; overflow: hidden; transform: scale(0); transform-origin: top left; }
        .summary-popout.visible, .meta-popout.mounted { transform: scale(1); transition: transform .3s ease-in-out; }
        .meta__header { display: flex; flex-direction: column; align-items: center; justify-content: center; background-color: #202225; padding-top: 10px; }
        .meta__header img { user-select: none; border-radius: 50%; margin-bottom: 10px; position: relative; width: 80px; height: 80px; }
        .meta__description { padding: 10px; background-color: #18191c; margin: 10px; border-radius: 5%; }
        .meta__details { display: flex; flex-wrap: wrap; padding-bottom: 7px; }
        .meta__display-name { font-weight: 500; color: #fff; opacity: 0.9; text-overflow: ellipsis; overflow: hidden; font-size: 12px; }
        .meta__user { font-weight: 500; color: #fff; text-overflow: ellipsis; overflow: hidden; }
        .meta__discriminator { font-weight: 500; color: #fff; opacity: .6; }
        .meta__divider-2 { margin: 5px 12px 10px; height: 1px; background-color: #4f545c; }
        .meta__divider { height: 4px; width: 4px; border-radius: 50%; background-color: #4f545c; }
        .meta__field { margin-bottom: 10px; }
        .meta__title { font-weight: 700; color: #72767d; text-transform: uppercase; font-size: 12px; line-height: 16px; margin-bottom: 1px; }
        .meta__value { font-size: 14px; line-height: 16px; align-items: center; display: flex; column-gap: 6px; }
    """

    JS_HEAD_CONTENT = """
    <script>
        function scrollToMessage(event, id) {
            var element = document.getElementById('chatlog__message-container-' + id);
            if (element !== null && element !== undefined) {
                event.preventDefault();
                element.classList.add('chatlog__message-container--highlighted');
                element.scrollIntoView({ behavior: 'smooth', block: 'center' });
                setTimeout(function() { element.classList.remove('chatlog__message-container--highlighted'); }, 1500);
            }
        }
        function showSpoiler(event, element) {
            if (element && element.classList.contains('spoiler--hidden')) {
                event.preventDefault();
                element.classList.remove('spoiler--hidden');
            }
        }
        document.addEventListener('DOMContentLoaded', () => {
            document.querySelectorAll('.pre--multiline').forEach((block) => {
                hljs.highlightBlock(block);
            });
        });
    </script>
    """

    JS_BODY_CONTENT = """
    <script>
        let metaPopout = undefined
        const contextMenu = document.getElementById("context-menu");
        const scope = document.querySelector("body");
        const messages = document.getElementsByClassName("chatlog__message-container")
        let messageID = ""
        const normalisePosition = (mouseX, mouseY, type) => {
            if (type == "context") { maxWidth = contextMenu.clientWidth; maxHeight = contextMenu.clientHeight; } 
            else if (type == "user") { maxWidth = metaPopout.clientWidth; maxHeight = metaPopout.clientHeight; }
            let { left: scopeOffsetX, top: scopeOffsetY } = scope.getBoundingClientRect()
            scopeOffsetX = scopeOffsetX < 0 ? 0 : scopeOffsetX; scopeOffsetY = scopeOffsetY < 0 ? 0 : scopeOffsetY
            const scopeX = mouseX - scopeOffsetX; const scopeY = mouseY - scopeOffsetY
            const outOfBoundsOnX = scopeX + maxWidth > scope.clientWidth
            const outOfBoundsOnY = scopeY + maxHeight > scope.clientHeight
            let normalizedX = mouseX; let normalizedY = mouseY
            if (outOfBoundsOnX) { normalizedX = scopeOffsetX + scope.clientWidth - maxWidth; }
            if (outOfBoundsOnY) { normalizedY = scopeOffsetY + scope.clientHeight - maxHeight; }
            return { normalizedX, normalizedY };
        }
        scope.addEventListener("contextmenu", (e) => {
            event.preventDefault()
            if (e.target.offsetParent != contextMenu) { contextMenu.classList.remove("visible"); }
        })
        var openContextMenu = function() {
            const { clientX: mouseX, clientY: mouseY } = event
            const { normalizedX, normalizedY } = normalisePosition(mouseX, mouseY, "context")
            contextMenu.classList.remove("visible")
            contextMenu.style.left = `${normalizedX}px`; contextMenu.style.top = `${normalizedY}px`
            setTimeout(() => { contextMenu.classList.add("visible"); })
            messageID = this.getAttribute("data-message-id");
        }
        for (var i = 0; i < messages.length; i++) { messages[i].addEventListener("contextmenu", openContextMenu) }
        scope.addEventListener("click", (e) => {
            if (e.target.offsetParent != contextMenu) { contextMenu.classList.remove("visible"); } 
            else { navigator.clipboard.writeText(messageID); contextMenu.classList.remove("visible"); }
            if (metaPopout && e.target.offsetParent != metaPopout) { metaPopout.classList.remove("mounted") }
            if (e.target.offsetParent != summaryPopout) { summaryPopout.classList.remove("visible") }
        })
        mainScroll = document.querySelector('.main')
        mainScroll.addEventListener('scroll', (e) => {
            if (e.target.offsetParent != contextMenu) { contextMenu.classList.remove("visible"); } 
            else { navigator.clipboard.writeText(messageID); contextMenu.classList.remove("visible"); }
            if (metaPopout && e.target.offsetParent != metaPopout) { metaPopout.classList.remove("mounted") }
        });

        const summaryPopout = document.getElementById('summary-popout')
        window.onload = function() {
            var author_name = document.getElementsByClassName('chatlog__author-name');
            var avatar = document.getElementsByClassName("chatlog__avatar");
            const element_select = [...author_name, ...avatar]
            for(var i = 0; i < element_select.length; i++) {
                var element = element_select[i];
                element.onclick = function() {
                    authorID = this.getAttribute("data-user-id");
                    if (metaPopout) { metaPopout.classList.remove('mounted'); }
                    metaPopout = document.getElementById('meta-popout-' + authorID);
                    const subtractX = document.querySelector('.main').scrollLeft
                    const subtractY = document.querySelector('.main').scrollTop
                    const elementX = this.offsetLeft + this.offsetWidth + 10 - subtractX
                    const elementY = this.offsetTop - subtractY
                    const { normalizedX, normalizedY } = normalisePosition(elementX, elementY, "user")
                    metaPopout.style.left = `${normalizedX}px`; metaPopout.style.top = `${normalizedY}px`;
                    setTimeout(() => { metaPopout.classList.add("mounted") });
                }
            }
            var summaryButton = document.getElementById('summary-button');
            summaryButton.onclick = function() {
                const elementX = this.offsetLeft - 110; const elementY = this.offsetTop + 30
                summaryPopout.style.left = `${elementX}px`; summaryPopout.style.top = `${elementY}px`;
                setTimeout(() => { summaryPopout.classList.add("visible") });
            }
        }
        
        tippy('.chatlog__timestamp', { placement: 'top', animation: 'fade', content: (reference) => reference.getAttribute('data-timestamp'), theme: 'disc' });
        tippy('.chatlog__short-timestamp', { placement: 'top', animation: 'fade', content: (reference) => reference.getAttribute('data-timestamp'), theme: 'disc' });

        dayjs.extend(window.dayjs_plugin_utc);
        dayjs.extend(window.dayjs_plugin_timezone);
        dayjs.extend(window.dayjs_plugin_customParseFormat);
        dayjs.extend(window.dayjs_plugin_isToday);
        dayjs.extend(window.dayjs_plugin_isTomorrow);
        dayjs.extend(window.dayjs_plugin_isBetween);
        dayjs.tz.setDefault("Asia/Tokyo")
        dayjs().format("DD/MM/YYYY HH:mm");
        var timeStamps = document.getElementsByClassName('chatlog__timestamp');
        for(var i = 0; i < timeStamps.length; i++) {
            const date_1 = dayjs.tz(timeStamps[i].innerText, "DD-MM-YYYY HH:mm", "Asia/Tokyo");
            const date_2 = dayjs.tz();
            if (date_1.isTomorrow()) { timeStamps[i].innerText = "Tomorrow at " + date_1.format('HH:mm') } 
            else if (date_1.isToday()) { timeStamps[i].innerText = "Today at " + date_1.format('HH:mm') } 
            else if (date_1.add(1, 'day').isToday()) { timeStamps[i].innerText = "Yesterday at " + date_1.format('HH:mm') } 
            else if (date_1.isBetween(date_2, date_2.subtract(7, 'day'))) { timeStamps[i].innerText = date_1.day(date_1.day()).format("dddd [at] HH:mm") }
        }
    </script>
    """

    # 最終的なHTMLテンプレート構築
    html_template = f'''<!DOCTYPE html>
<html lang="ja">
<head>
    <title>{html.escape(guild.name)} - {html.escape(channel.name)}</title>
    <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
    <meta name="viewport" content="width=device-width" />
    <meta name="title" content="{html.escape(guild.name)} - {html.escape(channel.name)}">
    <meta name="description" content="Transcript of channel {html.escape(channel.name)} ({channel.id}) from {html.escape(guild.name)} ({guild.id})">
    <meta name="theme-color" content="#638dfc" />

    <style>
        {CSS_CONTENT}
    </style>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/9.15.6/styles/solarized-dark.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/9.15.6/highlight.min.js"></script>
    <script src="https://unpkg.com/@popperjs/core@2.11.5/dist/umd/popper.min.js"></script>
    <script src="https://unpkg.com/tippy.js@6.3.7/dist/tippy-bundle.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1/dayjs.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1/plugin/timezone.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1/plugin/utc.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1.11.5/plugin/customParseFormat.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1.11.5/plugin/isToday.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1.11.5/plugin/isTomorrow.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dayjs@1.11.5/plugin/isBetween.js"></script>
    {JS_HEAD_CONTENT}
</head>
<body>

<div class="panel">
    <img class="panel__hashtag-icon" src="https://cdn.jsdelivr.net/gh/mahtoid/DiscordUtils@master/discord-hashtag.svg"/>
    <span>{html.escape(channel.name)}</span>
    <span class="panel__channel-topic">{channel.id}</span>
    <div class="panel__summary-button" id="summary-button">
        <span>詳細</span>
    </div>
</div>

<div class="main">
    <div class="buffer"></div>
    <div class="info">
        <span class="info__title">#{html.escape(channel.name)}へようこそ !</span>
        <span class="info__subject">これはチャンネル 「#{html.escape(channel.name)}」の始まりです。 {channel.id}</span>
    </div>
    <div class="chatlog">
        {messages_html}
    </div>
</div>

<div class="footer">
    <span class="footer__text">チケットログ - 生成日時: {close_time}</span>
</div>

<div id="context-menu">
    <div class="item">メッセージIDをコピー</div>
</div>

<div id="summary-popout" class="summary-popout">
    <div class="meta__header">
         <img src="{guild.icon.url if guild.icon else 'https://cdn.discordapp.com/embed/avatars/0.png'}" alt="Avatar">
    </div>
    <div class="meta__description">
        <div class="meta__details">
            <div class="meta__user">{html.escape(guild.name)}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">サーバーID</div>
            <div class="meta__value">{guild.id}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">チャンネルID</div>
            <div class="meta__value">{channel.id}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">チャンネル作成日</div>
            <div class="meta__value">{channel_created}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">メッセージ合計数</div>
            <div class="meta__value">{len(messages)}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">参加者数</div>
            <div class="meta__value">{len(users_data)}</div>
        </div>
        <div class="meta__field">
            <div class="meta__title">チケット情報</div>
            <div class="meta__value">
                開設者: {opener_name}<br>
                閉鎖者: {closer_name}<br>
            </div>
        </div>
    </div>
</div>

{user_popouts}

{JS_BODY_CONTENT}

</body>
</html>
'''
    return html_template

def load_ticket_settings():
    """チケット設定を読み込み"""
    if not os.path.exists("jsons/ticket_settings.json"):
        return {}
    try:
        with open("jsons/ticket_settings.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_ticket_settings(data):
    """チケット設定を保存"""
    with open("jsons/ticket_settings.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_active_tickets():
    """アクティブチケットを読み込み"""
    if not os.path.exists("jsons/active_tickets.json"):
        return {}
    try:
        with open("jsons/active_tickets.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_active_tickets(data):
    """アクティブチケットを保存"""
    with open("jsons/active_tickets.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

class TicketSystem(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="ticket", description="チケットシステムの管理")
    @app_commands.describe(
        action="実行するアクション",
        preset="使用するプリセット名（panelの場合のみ）"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="setup - 設定", value="setup"),
        app_commands.Choice(name="panel - パネル設置", value="panel")
    ])
    @app_commands.default_permissions(administrator=True)
    async def ticket(
        self,
        interaction: discord.Interaction,
        action: str,
        preset: Optional[str] = None
    ):
        """チケットシステムの管理"""

        if action == "setup":
            embed = discord.Embed(
                title="チケットシステム設定",
                description="チケット機能の設定を行います。\nプリセットは最大3件まで保存できます。",
                color=discord.Color.blue()
            )
            embed.add_field(name="新規プリセット作成", value="新しい設定プリセットを作成します", inline=False)
            embed.add_field(name="既存プリセット編集", value="既存のプリセットを編集します", inline=False)
            embed.add_field(name="プリセット一覧", value="保存されているプリセットを確認します", inline=False)
            embed.add_field(name="プリセット削除", value="既存のプリセットを削除します", inline=False)
            
            view = TicketSetupView(self.bot)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        elif action == "panel":
            if not preset:
                await interaction.response.send_message("プリセット名を指定してください。", ephemeral=True)
                return

            settings = load_ticket_settings()
            guild_id = str(interaction.guild_id)

            if guild_id not in settings or preset not in settings[guild_id].get("presets", {}):
                await interaction.response.send_message(f"プリセット「{preset}」が見つかりません。", ephemeral=True)
                return

            # ✅ コマンド実行チャンネルを使用
            target_channel = interaction.channel

            preset_data = settings[guild_id]["presets"][preset].copy()
            preset_data["preset_name"] = preset

            embed = discord.Embed(
                title=preset_data["panel_title"],
                description=preset_data["panel_description"],
                color=discord.Color.green()
            )
            embed.set_footer(text=f"プリセット: {preset}")

            view = TicketPanelView(self.bot, preset_data)
            await target_channel.send(embed=embed, view=view)

            await interaction.response.send_message(
                f"{target_channel.mention} にチケットパネルを設置しました！",
                ephemeral=True
            )

    @ticket.autocomplete("preset")
    async def preset_autocomplete(self, interaction: discord.Interaction, current: str):
        """保存されているプリセット名を予測変換で出す"""
        try:
            data = load_ticket_settings()
            guild_id = str(interaction.guild_id)
            presets = data.get(guild_id, {}).get("presets", {}).keys()

            return [
                app_commands.Choice(name=p, value=p)
                for p in presets
                if current.lower() in p.lower()
            ][:25]
        except Exception:
            return []


async def setup(bot):
    await bot.add_cog(TicketSystem(bot))