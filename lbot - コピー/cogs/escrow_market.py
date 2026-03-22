"""
cogs/escrow_market.py
仲介型取引の広告マーケットパネル。
既存の cogs/market.py と完全に独立したチャンネルで動作する。
"""
import discord
from discord.ext import commands
import asyncio
import math
import os
import datetime
import traceback
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import func
from utils.price import fetch_ltc_jpy_price
from database import AsyncSessionLocal
from models import EscrowAd, EscrowOrder, User, get_or_create_user, EscrowAddress, get_escrow_fee_rate
from utils.electrum import call_electrum_rpc
import json
from utils.decimal_utils import quantize_ltc
from decimal import Decimal

ESCROW_MARKET_CHANNEL_ID = int(os.getenv("ESCROW_MARKET_CHANNEL_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))

escrow_market_refresh_lock = asyncio.Lock()


async def refresh_escrow_market_panels(bot: commands.Bot, channel_id: int = None):
    """仲介型マーケットチャンネルのパネルを再生成する"""
    target_id = channel_id or ESCROW_MARKET_CHANNEL_ID
    if not target_id:
        return

    async with escrow_market_refresh_lock:
        channel = bot.get_channel(target_id)
        if not isinstance(channel, discord.TextChannel):
            return

        price_jpy = await fetch_ltc_jpy_price()
        if price_jpy <= 0:
            price_jpy = 10000

        panels_to_show = []

        async with AsyncSessionLocal() as session:
            stmt = (
                select(EscrowAd)
                .options(joinedload(EscrowAd.user))
                .filter(EscrowAd.is_active == True)
                .order_by(EscrowAd.margin_percent.asc())  # 仲介型は買い手有利（低い%が良い）な順
            )
            result = await session.execute(stmt)
            active_ads = result.scalars().all()
            fee_rate = await get_escrow_fee_rate(session)

            if not active_ads:
                embed = discord.Embed(
                    title="LTC 仲介型マーケット",
                    description=(
                        "現在、仲介型広告はありません。\n"
                        "下のボタンから広告を作成できます。\n\n"
                        "**仲介型とは？**\n"
                        "運営ボットが取引を仲介します。\n"
                        "売り手はLTCを専用アドレスに送金→買い手がJPYを支払い→ボットがLTCを送金。"
                    ),
                    color=discord.Color.light_grey()
                )
                panels_to_show.append({"embed": embed, "view": EscrowMarketCommandView()})
            else:
                for ad in active_ads:
                    seller = ad.user
                    if not seller.is_online:
                        continue

                    # マージン適用後の価格計算
                    applied_price = Decimal(str(price_jpy)) * (ad.margin_percent / Decimal("100"))

                    total_trades = seller.total_trades
                    completed = seller.completed_trades
                    success_rate = (completed / total_trades * 100) if total_trades > 0 else 0
                    status_emoji = "🟢 Online"

                    member = channel.guild.get_member(int(seller.discord_id))
                    display_name = member.display_name if member else f"User {seller.discord_id[:4]}..."
                    avatar_url = member.display_avatar.url if member and member.display_avatar else None

                    embed = discord.Embed(
                        title=f"SELL LTC (仲介型) - {ad.margin_percent.normalize()}%",
                        color=discord.Color.orange()
                    )
                    if avatar_url:
                        embed.set_thumbnail(url=avatar_url)

                    embed.description = (
                        f"**User:** {display_name}\n"
                        f"**Status:** {status_emoji}\n"
                        f"**月間取引数:** {total_trades}\n"
                        f"**取引成功率:** {success_rate:.1f}%\n"
                    )

                    if ad.terms:
                        embed.add_field(name="取引条件・決済方法", value=f"```\n{ad.terms}\n```", inline=False)

                    # 仲介型の価格表示
                    embed.add_field(
                        name="Details",
                        value=(
                            f"**換金率:** `{ad.margin_percent.normalize()}%`\n"
                            f"**最小購入額:** `¥{ad.min_amount_jpy:,}`\n"
                            f"**最大購入額:** `¥{ad.max_amount_jpy:,}`\n"
                            f"**支払いタイムアウト:** `{ad.timeout_mins} 分`\n"
                            f"**売り手LTCタイムアウト:** `30 分`\n"
                            f"**プラットフォーム手数料:** `{(fee_rate * Decimal('100')).normalize()}%` (仲介型)"
                        ),
                        inline=False
                    )

                    view = EscrowBuyAdView(ad_id=ad.id, min_jpy=ad.min_amount_jpy, max_jpy=ad.max_amount_jpy)
                    panels_to_show.append({"embed": embed, "view": view})

                # 広告作成ボタン
                embed_create = discord.Embed(
                    description="⬇️ 仲介型でLTCを売りたい場合は広告を作成してください",
                    color=discord.Color.orange()
                )
                panels_to_show.append({"embed": embed_create, "view": EscrowMarketCommandView()})

        # ── 既存メッセージの書き換え(edit)または追加/削除 ──
        try:
            history = [m async for m in channel.history(limit=100) if m.author == bot.user]
            history.reverse()

            for i, panel in enumerate(panels_to_show):
                if i < len(history):
                    msg = history[i]
                    await msg.edit(embed=panel["embed"], view=panel["view"])
                else:
                    await channel.send(embed=panel["embed"], view=panel["view"])
            
            if len(history) > len(panels_to_show):
                for msg in history[len(panels_to_show):]:
                    await msg.delete()

        except discord.DiscordException as e:
            print(f"[escrow_market] メッセージの更新中にエラーが発生しました: {e}")


# ─────────────────────────────────────────────
# 購入モーダル
# ─────────────────────────────────────────────

class EscrowBuyAmountModal(discord.ui.Modal, title="仲介型LTC購入申請"):
    amount_jpy = discord.ui.TextInput(
        label="購入希望額 (JPY)",
        placeholder="例: 10000",
        required=True,
    )

    def __init__(self, ad_id: int, min_jpy: int, max_jpy: int):
        super().__init__()
        self.ad_id = ad_id
        self.min_jpy = min_jpy
        self.max_jpy = max_jpy

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            jpy = int(self.amount_jpy.value)
            if jpy <= 0:
                raise ValueError("金額は0より大きくなければなりません。")
            if jpy < self.min_jpy or jpy > self.max_jpy:
                raise ValueError(f"金額は ¥{self.min_jpy:,} から ¥{self.max_jpy:,} の範囲で入力してください。")

            price_jpy = await fetch_ltc_jpy_price()
            if price_jpy <= 0:
                raise ValueError("現在レートが取得できません。少し時間をおいて再試行してください。")

            async with AsyncSessionLocal() as session:
                async with session.begin():
                    ad = await session.get(EscrowAd, self.ad_id)
                    if not ad or not ad.is_active:
                        raise ValueError("この広告は既に無効になっています。")

                    applied_price = Decimal(str(price_jpy)) * (ad.margin_percent / Decimal("100"))

                    # 仲介専用手数料率の取得
                    fee_rate = await get_escrow_fee_rate(session)

                    # 手数料を含めた売り手の送金必要額を計算
                    net_ltc = quantize_ltc(Decimal(str(jpy)) / Decimal(str(applied_price)))
                    fee_ltc = quantize_ltc(net_ltc * fee_rate)
                    amount_ltc = net_ltc + fee_ltc  # 売り手が送る総額

                    seller_id_str = ad.user_id
                    buyer_id_str = str(interaction.user.id)

                    if seller_id_str == buyer_id_str:
                        raise ValueError("自分自身の広告から購入することはできません。")

                    # ── アドレスプールの管理 ──
                    stmt = select(EscrowAddress).where(EscrowAddress.is_in_use == False).limit(1).with_for_update()
                    res = await session.execute(stmt)
                    pool_addr = res.scalar_one_or_none()

                    if pool_addr:
                        # プールのアドレスを使用
                        escrow_address = pool_addr.address
                        pool_addr.is_in_use = True
                        pool_addr_id = pool_addr.id
                        pool_model = pool_addr
                    else:
                        # プールに空きがないので新規作成
                        escrow_address = await call_electrum_rpc("createnewaddress")
                        if not escrow_address:
                            raise ValueError("エスクローアドレスの生成に失敗しました。管理者に連絡してください。")

                        # プールに追加
                        new_pool_addr = EscrowAddress(
                            address=escrow_address,
                            label="Escrow_temp",
                            is_in_use=True
                        )
                        session.add(new_pool_addr)
                        await session.flush()
                        pool_addr_id = new_pool_addr.id
                        pool_model = new_pool_addr


                    # EscrowOrder 作成
                    seller_ltc_deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)

                    new_order = EscrowOrder(
                        ad_id=ad.id,
                        seller_id=seller_id_str,
                        buyer_id=buyer_id_str,
                        amount_jpy=jpy,
                        amount_ltc=amount_ltc,
                        fee_ltc=fee_ltc,
                        net_ltc=net_ltc,
                        lock_price_jpy=applied_price,
                        escrow_address_id=pool_addr_id,
                        escrow_address=escrow_address,
                        escrow_label="",
                        status='WAITING_SELLER_LTC',
                        seller_ltc_deadline=seller_ltc_deadline,
                    )
                    session.add(new_order)
                    await session.flush()  # IDを取得するため

                    # ラベルを更新してElectrumにも設定
                    escrow_label = f"EscrowOrder_{new_order.id}"
                    new_order.escrow_label = escrow_label
                    pool_model.label = escrow_label
                    await call_electrum_rpc("setlabel", {"key": escrow_address, "label": escrow_label})

                    # 取引実績のtotal_trades加算
                    seller_user = await session.get(User, seller_id_str, with_for_update=True)
                    if seller_user:
                        seller_user.total_trades += 1
                    buyer_user = await get_or_create_user(session, interaction.user.id)
                    buyer_user.total_trades += 1

                    order_id = new_order.id
                    seller_ltc_deadline_unix = int(seller_ltc_deadline.timestamp())
                    timeout_mins = ad.timeout_mins
                    terms = ad.terms or ""
                    welcome_msg = ad.welcome_message or ""
                    margin_percent = ad.margin_percent

            # チケットチャンネル作成
            from cogs.escrow_ticket import create_escrow_ticket_channel
            buyer = await interaction.client.fetch_user(int(buyer_id_str))
            seller_member = await interaction.guild.fetch_member(int(seller_id_str))

            ticket_channel = await create_escrow_ticket_channel(
                guild=interaction.guild,
                seller_member=seller_member,
                buyer=buyer,
                order_id=order_id,
                amount_jpy=jpy,
                amount_ltc=amount_ltc,
                net_ltc=net_ltc,
                fee_ltc=fee_ltc,
                escrow_address=escrow_address,
                margin_percent=margin_percent,
                terms=terms,
                timeout_mins=timeout_mins,
                welcome_message=welcome_msg,
                seller_ltc_deadline_unix=seller_ltc_deadline_unix,
            )

            if ticket_channel:
                async with AsyncSessionLocal() as session:
                    db_order = await session.get(EscrowOrder, order_id)
                    if db_order:
                        db_order.ticket_channel_id = str(ticket_channel.id)
                        await session.commit()

                await interaction.followup.send(
                    f"仲介型取引を開始しました！\nチケットへ移動してください: {ticket_channel.mention}",
                    ephemeral=True
                )
            else:
                await interaction.followup.send("チケット作成に失敗しました。管理者に連絡してください。", ephemeral=True)

            await refresh_escrow_market_panels(interaction.client)

        except ValueError as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send("処理中にエラーが発生しました。", ephemeral=True)


class EscrowBuyAdView(discord.ui.View):
    def __init__(self, ad_id: int, min_jpy: int, max_jpy: int):
        super().__init__(timeout=None)
        self.ad_id = ad_id
        self.min_jpy = min_jpy
        self.max_jpy = max_jpy

    @discord.ui.button(label="購入する (Escrow Buy)", style=discord.ButtonStyle.success, custom_id="escrow_buy_ad_dynamic")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EscrowBuyAmountModal(self.ad_id, self.min_jpy, self.max_jpy))


# ─────────────────────────────────────────────
# 広告作成モーダル
# ─────────────────────────────────────────────

class CreateEscrowAdModal(discord.ui.Modal, title="仲介型広告を作成する"):
    margin = discord.ui.TextInput(
        label="市場価格に対する% (例: 105 → 5%割高=買い手不利)",
        placeholder="105.00",
        required=True,
        max_length=6
    )
    amount_range = discord.ui.TextInput(
        label="購入額範囲 (最小-最大) JPY 例: 1000-50000",
        placeholder="1000-50000",
        required=True,
        max_length=20,
    )
    terms = discord.ui.TextInput(
        label="決済条件（銀行・PayPay等）や注意事項",
        style=discord.TextStyle.paragraph,
        placeholder="PAYPAYマネーのみ,マネラ+10%...",
        required=False
    )
    timeout_input = discord.ui.TextInput(
        label="支払い期限 (15 または 30) ※空欄で15分",
        placeholder="15",
        required=False,
        max_length=2
    )
    welcome_message = discord.ui.TextInput(
        label="ウェルカムメッセージ（任意）",
        style=discord.TextStyle.paragraph,
        placeholder="PAYPAYID: ○○○○○○, 振込先: ○○銀行 ○○支店...",
        required=False,
        max_length=2000
    )

    def __init__(self, existing_ad: EscrowAd = None):
        super().__init__()
        if existing_ad:
            margin_str = str(existing_ad.margin_percent.normalize())
            self.margin.default = margin_str
            self.amount_range.default = f"{existing_ad.min_amount_jpy}-{existing_ad.max_amount_jpy}"
            if existing_ad.terms:
                self.terms.default = existing_ad.terms
            self.timeout_input.default = str(existing_ad.timeout_mins)
            if existing_ad.welcome_message:
                self.welcome_message.default = existing_ad.welcome_message

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            margin_val = Decimal(self.margin.value.strip())
            if margin_val <= Decimal("0"):
                raise ValueError("マージンは0より大きい値にしてください。")

            try:
                min_str, max_str = self.amount_range.value.split('-')
                min_val = int(min_str.strip())
                max_val = int(max_str.strip())
            except ValueError:
                raise ValueError("購入額範囲はハイフン区切りで入力してください（例: 1000-50000）")

            if min_val <= 0 or max_val <= 0 or min_val > max_val:
                raise ValueError("購入額の設定が無効です。")

            timeout_val = 30
            if self.timeout_input.value:
                try:
                    parsed_timeout = int(self.timeout_input.value)
                    if parsed_timeout not in [15, 30]:
                        raise ValueError("時間（Timeout）は 15 または 30 を指定してください。")
                    timeout_val = parsed_timeout
                except ValueError as e:
                    if "時間" not in str(e):
                        raise ValueError("時間（Timeout）には数字を入力してください。")
                    raise e

            async with AsyncSessionLocal() as session:
                # 既存アクティブ広告を無効化
                stmt = select(EscrowAd).where(
                    EscrowAd.user_id == str(interaction.user.id),
                    EscrowAd.is_active == True
                )
                res = await session.execute(stmt)
                for existing in res.scalars().all():
                    existing.is_active = False

                new_ad = EscrowAd(
                    user_id=str(interaction.user.id),
                    margin_percent=margin_val,
                    min_amount_jpy=min_val,
                    max_amount_jpy=max_val,
                    terms=self.terms.value,
                    timeout_mins=timeout_val,
                    welcome_message=self.welcome_message.value,
                    is_active=True
                )
                session.add(new_ad)
                await session.commit()

            await interaction.followup.send(
                f"**仲介型広告を作成しました**\n"
                f"マージン: `{margin_val}%`\n"
                f"限度額: `¥{min_val:,} 〜 ¥{max_val:,}`\n"
                f"JPY支払い期限: `{timeout_val}分` / LTC送金期限: `30分`",
                ephemeral=True
            )

            await refresh_escrow_market_panels(interaction.client)

        except ValueError as e:
            await interaction.followup.send(f"入力エラー: {e}", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send("予期せぬエラーが発生しました。", ephemeral=True)


class EscrowMarketCommandView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="仲介型広告を作成/更新", style=discord.ButtonStyle.primary, custom_id="escrow_market_create_ad_cmd")
    async def create_ad(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            stmt = select(EscrowAd).where(
                EscrowAd.user_id == str(interaction.user.id),
                EscrowAd.is_active == True,
            )
            result = await session.execute(stmt)
            existing_ad = result.scalars().first()
            
        await interaction.response.send_modal(CreateEscrowAdModal(existing_ad=existing_ad))

    @discord.ui.button(label="広告削除", style=discord.ButtonStyle.danger, custom_id="escrow_market_delete_ad_cmd")
    async def delete_ad(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            stmt = select(EscrowAd).where(
                EscrowAd.user_id == str(interaction.user.id),
                EscrowAd.is_active == True,
            )
            result = await session.execute(stmt)
            existing_ads = result.scalars().all()
            
            if not existing_ads:
                await interaction.response.send_message("有効な仲介型広告が見つかりません。", ephemeral=True)
                return

            for e_ad in existing_ads:
                e_ad.is_active = False
            
            await session.commit()
            
        await interaction.response.send_message("仲介型広告を削除しました。", ephemeral=True)
        await refresh_escrow_market_panels(interaction.client)


class EscrowMarketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(EscrowMarketCommandView())
        self.bot.add_view(EscrowBuyAdView(0, 0, 0))

    @discord.app_commands.command(name="setup_escrow_market", description="【管理者専用】仲介型マーケットパネルを設置します。")
    @discord.app_commands.default_permissions(administrator=True)
    async def setup_escrow_market(self, interaction: discord.Interaction):
        """仲介型マーケットを設置する（スラッシュコマンド）"""
        await interaction.response.defer(ephemeral=True)
        await refresh_escrow_market_panels(self.bot, interaction.channel_id)
        await interaction.followup.send("仲介型マーケットパネルを設置しました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EscrowMarketCog(bot))
