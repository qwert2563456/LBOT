import discord
from discord.ext import commands
from discord import app_commands
import json
import os
from datetime import datetime
from typing import Optional
import html

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