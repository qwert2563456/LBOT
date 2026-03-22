import discord
from discord.ext import commands
import traceback
import math
import os
import asyncio
import datetime
from utils.price import fetch_ltc_jpy_price
from database import AsyncSessionLocal
from models import get_or_create_user, Ad, Order, User, get_p2p_fee_rate
from sqlalchemy import select
from sqlalchemy.orm import joinedload

MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))

# ロック: purge〜パネル送信を含む全体を保護する（二重パネル防止）
market_refresh_lock = asyncio.Lock()


async def refresh_market_panels(bot: commands.Bot, channel_id: int = None):
    """
    マーケットチャンネル内の過去パネルを削除し、最新の広告パネルを再生成する。
    ロックで全体を囲み、並行呼び出しによる二重パネルを防止する。
    """
    target_id = channel_id or MARKET_CHANNEL_ID
    if not target_id:
        return

    # ── ロックで purge〜send 全体を保護 ──────────────────
    async with market_refresh_lock:
        channel = bot.get_channel(target_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.purge(limit=100, check=lambda m: m.author == bot.user)
        except discord.Forbidden:
            print(f"[market] purge 失敗: 権限不足 (ch: {target_id})")
            return

        price_jpy = await fetch_ltc_jpy_price()
        if price_jpy <= 0:
            price_jpy = 10000  # フォールバック

        # 現在の手数料率を取得
        fee_rate = await get_p2p_fee_rate()

        async with AsyncSessionLocal() as session:
            stmt = (
                select(Ad)
                .options(joinedload(Ad.user))
                .filter(Ad.is_active == True)
                .order_by(Ad.margin_percent.desc())
            )
            result = await session.execute(stmt)
            active_ads = result.scalars().all()

            if not active_ads:
                embed = discord.Embed(
                    title="LTC P2P マーケット",
                    description=(
                        "現在、アクティブな広告はありません。\n"
                        "下のボタンから広告を作成できます。"
                    ),
                    color=discord.Color.light_grey(),
                )
                await channel.send(embed=embed, view=MarketCommandView())
                return

            for ad in active_ads:
                seller = ad.user
                available_ltc = float(seller.available_balance)

                if available_ltc <= 0:
                    continue

                # オフラインの売り手はスキップ
                if not seller.is_online:
                    continue

                seller_available_jpy = math.floor(available_ltc * price_jpy)

                # 手数料を考慮した最大販売可能額
                max_safe_ltc = available_ltc / (1.0 + fee_rate)
                max_safe_jpy = math.floor(
                    max_safe_ltc * price_jpy * (float(ad.margin_percent) / 100.0)
                )
                actual_max_jpy = max(0, min(max_safe_jpy, ad.max_amount_jpy))

                if actual_max_jpy < ad.min_amount_jpy:
                    continue

                total_trades = seller.total_trades
                completed    = seller.completed_trades
                success_rate = (completed / total_trades * 100) if total_trades > 0 else 0

                member      = channel.guild.get_member(int(seller.discord_id))
                display_name = member.display_name if member else f"User {seller.discord_id[:4]}..."
                avatar_url   = member.display_avatar.url if member and member.display_avatar else None

                embed = discord.Embed(
                    title=f"SELL LTC  —  {float(ad.margin_percent)}% of market",
                    color=discord.Color.gold(),
                )
                if avatar_url:
                    embed.set_thumbnail(url=avatar_url)

                embed.description = (
                    f"**User:** {display_name}\n"
                    f"**Status:** 🟢 Online\n"
                    f"**Monthly Trades:** {total_trades}\n"
                    f"**Success Rate:** {success_rate:.1f}%\n"
                )

                if ad.terms:
                    embed.add_field(
                        name="取引条件・留意事項",
                        value=f"```\n{ad.terms}\n```",
                        inline=False,
                    )

                embed.add_field(
                    name="Details",
                    value=(
                        f"**換金率:** `{float(ad.margin_percent)}%`\n"
                        f"**販売可能額:** `約 ¥{seller_available_jpy:,}`\n"
                        f"**最小購入額:** `¥{ad.min_amount_jpy:,}`\n"
                        f"**最大購入額:** `¥{actual_max_jpy:,}`\n"
                        f"**支払いタイムアウト:** `{ad.timeout_mins} 分`\n"
                        f"**プラットフォーム手数料:** `{fee_rate * 100:.2f}%`"
                    ),
                    inline=False,
                )

                view = BuyAdView(ad_id=ad.id, max_jpy=actual_max_jpy, min_jpy=ad.min_amount_jpy)
                await channel.send(embed=embed, view=view)

        embed_create = discord.Embed(
            description="⬇️ 自分のLTCを販売したい場合は広告を作成してください",
            color=discord.Color.blue(),
        )
        await channel.send(embed=embed_create, view=MarketCommandView())


# ── 購入モーダル ─────────────────────────────────────────

class BuyAmountModal(discord.ui.Modal, title="LTCの購入"):
    amount_jpy = discord.ui.TextInput(
        label="購入希望額 (JPY)",
        placeholder="例: 10000",
        required=True,
    )

    def __init__(self, ad_id: int, min_jpy: int, max_jpy: int):
        super().__init__()
        self.ad_id   = ad_id
        self.min_jpy = min_jpy
        self.max_jpy = max_jpy

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            jpy = int(self.amount_jpy.value)
            if jpy <= 0:
                raise ValueError("金額は 0 より大きくなければなりません。")
            if not (self.min_jpy <= jpy <= self.max_jpy):
                raise ValueError(
                    f"金額は ¥{self.min_jpy:,} から ¥{self.max_jpy:,} の範囲で入力してください。"
                )

            price_jpy_market = await fetch_ltc_jpy_price()
            if price_jpy_market <= 0:
                raise ValueError("現在レートが取得できません。少し時間をおいて再試行してください。")

            # 手数料率を取得（DB から最新値を使用）
            fee_rate = await get_p2p_fee_rate()

            async with AsyncSessionLocal() as session:
                async with session.begin():
                    ad = await session.get(Ad, self.ad_id)
                    if not ad or not ad.is_active:
                        raise ValueError("この広告は既に無効になっています。")

                    applied_price    = price_jpy_market * (float(ad.margin_percent) / 100.0)
                    required_ltc     = round(jpy / applied_price, 8)
                    fee_ltc          = round(required_ltc * fee_rate, 8)
                    total_required   = round(required_ltc + fee_ltc, 8)

                    seller_id_str = ad.user_id
                    buyer_id_str  = str(interaction.user.id)

                    if seller_id_str == buyer_id_str:
                        raise ValueError("自分自身の広告から購入することはできません。")

                    stmt = select(User).where(User.discord_id == seller_id_str).with_for_update()
                    result = await session.execute(stmt)
                    seller = result.scalar_one_or_none()

                    if not seller:
                        raise ValueError("販売者情報が見つかりません。")
                    if float(seller.available_balance) < total_required:
                        raise ValueError(
                            "販売者の残高が不足しているため、この金額での取引は現在開始できません。"
                        )

                    # 残高をロック: available → locked
                    seller.available_balance = round(
                        float(seller.available_balance) - total_required, 8
                    )
                    seller.locked_balance = round(
                        float(seller.locked_balance) + total_required, 8
                    )
                    seller.total_trades += 1

                    buyer_user = await get_or_create_user(session, interaction.user.id)
                    buyer_user.total_trades += 1

                    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                        minutes=ad.timeout_mins
                    )
                    new_order = Order(
                        ad_id=ad.id,
                        seller_id=seller_id_str,
                        buyer_id=buyer_id_str,
                        amount_jpy=jpy,
                        amount_ltc=required_ltc,
                        fee_ltc=fee_ltc,
                        lock_price_jpy=applied_price,
                        status="PENDING",
                        expires_at=expires_at,
                    )
                    session.add(new_order)
                    await session.flush()

                    order_id       = new_order.id
                    expires_at_unix = int(expires_at.timestamp())

            # チケット作成
            buyer          = await interaction.client.fetch_user(int(buyer_id_str))
            seller_member  = await interaction.client.fetch_user(int(seller_id_str))

            from cogs.ticket import create_ticket_channel
            ticket_channel = await create_ticket_channel(
                interaction.guild,
                seller_member,
                buyer,
                order_id,
                jpy,
                required_ltc,
                ad.margin_percent,
                ad.min_amount_jpy,
                ad.max_amount_jpy,
                ad.terms,
                ad.timeout_mins,
                ad.welcome_message,
                expires_at_unix=expires_at_unix,
            )

            if ticket_channel:
                async with AsyncSessionLocal() as session:
                    db_order = await session.get(Order, order_id)
                    if db_order:
                        db_order.ticket_channel_id = str(ticket_channel.id)
                        await session.commit()
                await interaction.followup.send(
                    f"エスクローを確保し、取引を開始しました！\n"
                    f"チケットへ移動してください: {ticket_channel.mention}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    "チケット作成に失敗しました。管理者に連絡してください。",
                    ephemeral=True,
                )

            await refresh_market_panels(interaction.client)

        except ValueError as e:
            await interaction.followup.send(f"エラー: {e}", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("購入処理中にエラーが発生しました。", ephemeral=True)


class BuyAdView(discord.ui.View):
    def __init__(self, ad_id: int, max_jpy: int, min_jpy: int):
        super().__init__(timeout=None)
        self.ad_id   = ad_id
        self.max_jpy = max_jpy
        self.min_jpy = min_jpy

    @discord.ui.button(label="購入する (Buy)", style=discord.ButtonStyle.success, custom_id="buy_ad_dynamic")
    async def buy_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            BuyAmountModal(self.ad_id, self.min_jpy, self.max_jpy)
        )


# ── 広告作成モーダル ─────────────────────────────────────

class CreateAdModal(discord.ui.Modal, title="広告を作成する"):
    margin = discord.ui.TextInput(
        label="市場価格に対する% (例: 105 → 5%割高=買い手不利)",
        placeholder="105.00",
        required=True,
        max_length=6,
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
        required=False,
    )
    timeout_input = discord.ui.TextInput(
        label="支払期限 (15 または 30) ※空欄で15分",
        placeholder="15",
        required=False,
        max_length=2,
    )
    welcome_message = discord.ui.TextInput(
        label="ウェルカムメッセージ（任意）",
        style=discord.TextStyle.paragraph,
        placeholder="PAYPAYID: ○○○○○○, 振込先: ○○銀行 ○○支店...",
        required=False,
        max_length=2000,
    )

    def __init__(self, existing_ad: Ad = None):
        super().__init__()
        if existing_ad:
            # 既存の値を表示用にフォーマット
            margin_str = str(float(existing_ad.margin_percent))
            if '.' in margin_str:
                margin_str = margin_str.rstrip('0').rstrip('.')
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
            margin_val = float(self.margin.value)

            try:
                min_str, max_str = self.amount_range.value.split("-")
                min_val = int(min_str.strip())
                max_val = int(max_str.strip())
            except ValueError:
                raise ValueError("購入額範囲はハイフン区切りで入力してください（例: 1000-50000）")

            if margin_val <= 0:
                raise ValueError("マージンは 0 より大きい値にしてください。")
            if min_val <= 0 or max_val <= 0:
                raise ValueError("金額は 0 より大きい値にしてください。")
            if min_val > max_val:
                raise ValueError("最小購入額は最大購入額以下にしてください。")

            timeout_val = 15
            if self.timeout_input.value:
                try:
                    parsed_timeout = int(self.timeout_input.value)
                    if parsed_timeout not in [15, 30]:
                        raise ValueError("時間（Timeout）は 15 または 30 を指定してください。")
                    timeout_val = parsed_timeout
                except ValueError as e:
                    if "時間" not in str(e):
                        raise ValueError("時間（Timeout）には数字を入力してください。")
                    raise

            price_jpy = await fetch_ltc_jpy_price()
            if price_jpy <= 0:
                raise ValueError("現在レートが取得できません。しばらくお待ちください。")

            # 手数料率取得
            fee_rate = await get_p2p_fee_rate()

            async with AsyncSessionLocal() as session:
                user = await get_or_create_user(session, interaction.user.id)
                available_ltc = float(user.available_balance)

                if available_ltc <= 0:
                    raise ValueError("LTC残高がないため広告を作成できません。まず入金してください。")

                max_safe_ltc   = available_ltc / (1.0 + fee_rate)
                max_possible_jpy = math.floor(
                    max_safe_ltc * price_jpy * (margin_val / 100.0)
                )

                if max_possible_jpy < min_val:
                    raise ValueError(
                        f"残高が不足しています。現在の残高({available_ltc:.8f} LTC)では "
                        f"最小購入額 ¥{min_val:,} を賄えません。"
                    )

                actual_max = min(max_val, max_possible_jpy)

                # 既存のアクティブ広告を無効化（1ユーザー1広告）
                stmt = select(Ad).where(
                    Ad.user_id == str(interaction.user.id),
                    Ad.is_active == True,
                )
                res  = await session.execute(stmt)
                for e_ad in res.scalars().all():
                    e_ad.is_active = False

                new_ad = Ad(
                    user_id=str(interaction.user.id),
                    margin_percent=margin_val,
                    min_amount_jpy=min_val,
                    max_amount_jpy=actual_max,
                    terms=self.terms.value,
                    timeout_mins=timeout_val,
                    welcome_message=self.welcome_message.value,
                    is_active=True,
                )
                session.add(new_ad)
                await session.commit()

            msg = (
                f"**広告を作成しました**\n"
                f"マージン: `{margin_val}%`\n"
                f"購入限度額: `¥{min_val:,} 〜 ¥{actual_max:,}`"
            )
            if actual_max < max_val:
                msg += f"\n最大購入額は残高に合わせて ¥{actual_max:,} に自動調整されました。"

            await interaction.followup.send(msg, ephemeral=True)
            await refresh_market_panels(interaction.client)

        except ValueError as e:
            await interaction.followup.send(f"入力エラー: {e}", ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("予期せぬエラーが発生しました。", ephemeral=True)


class MarketCommandView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="広告を作成/更新",
        style=discord.ButtonStyle.primary,
        custom_id="market_create_ad_cmd",
    )
    async def create_ad(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            stmt = select(Ad).where(
                Ad.user_id == str(interaction.user.id),
                Ad.is_active == True,
            )
            result = await session.execute(stmt)
            existing_ad = result.scalars().first()
            
        await interaction.response.send_modal(CreateAdModal(existing_ad=existing_ad))

    @discord.ui.button(
        label="広告削除",
        style=discord.ButtonStyle.danger,
        custom_id="market_delete_ad_cmd",
    )
    async def delete_ad(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            stmt = select(Ad).where(
                Ad.user_id == str(interaction.user.id),
                Ad.is_active == True,
            )
            result = await session.execute(stmt)
            existing_ads = result.scalars().all()
            
            if not existing_ads:
                await interaction.response.send_message("有効な広告が見つかりません。", ephemeral=True)
                return

            for e_ad in existing_ads:
                e_ad.is_active = False
            
            await session.commit()
            
        await interaction.response.send_message("広告を削除しました。", ephemeral=True)
        await refresh_market_panels(interaction.client)


class MarketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(MarketCommandView())


async def setup(bot: commands.Bot):
    await bot.add_cog(MarketCog(bot))