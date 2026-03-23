"""
cogs/escrow_ticket.py
仲介型取引のチケット操作UI（Views・ボタン・モーダル）を定義する。
既存 cogs/ticket.py の AppealView / AdminResolutionView を流用しつつ、
仲介型固有のフローを実装する。
"""
import discord
from discord.ext import commands
import os
import datetime
import traceback
from sqlalchemy import select
from sqlalchemy.sql import func
from database import AsyncSessionLocal
from models import EscrowOrder, EscrowAd, SystemConfig, Transaction, User, get_or_create_user, EscrowAddress
from utils.electrum import broadcast_withdrawal
from utils.price import fetch_ltc_jpy_price
from utils.decimal_utils import quantize_ltc
from decimal import Decimal

TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "0"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0"))
MARKET_CHANNEL_ID = int(os.getenv("MARKET_CHANNEL_ID", "0"))
ESCROW_MARKET_CHANNEL_ID = int(os.getenv("ESCROW_MARKET_CHANNEL_ID", "0"))  # 仲介型専用マーケットチャンネル

# ─────────────────────────────────────────────
# アドレスプールの解放ユーティリティ
# ─────────────────────────────────────────────
async def _release_escrow_address(session, order: EscrowOrder):
    if order.escrow_address_id:
        addr_row = await session.get(EscrowAddress, order.escrow_address_id)
        if addr_row:
            addr_row.is_in_use = False


# ─────────────────────────────────────────────
# 買い手: LTC受取アドレス入力
# ─────────────────────────────────────────────

class BuyerAddressModal(discord.ui.Modal, title="LTC受取アドレスの登録"):
    address = discord.ui.TextInput(
        label="あなたのLTC受取アドレス",
        placeholder="ltc1q...",
        required=True,
        min_length=26,
        max_length=100
    )

    def __init__(self, order_id: int):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        ltc_addr = self.address.value.strip()

        # 簡易バリデーション（LTCアドレスはL/M/ltc1で始まる）
        if not (ltc_addr.startswith("L") or ltc_addr.startswith("M") or ltc_addr.startswith("ltc1")):
            await interaction.followup.send(
                "正しいLTCアドレスを入力してください（L/M/ltc1 で始まるアドレス）。",
                ephemeral=True
            )
            return

        async with AsyncSessionLocal() as session:
            order = await session.get(EscrowOrder, self.order_id)
            if not order or order.status != 'WAITING_PAYMENT':
                await interaction.followup.send("この取引は現在アドレスを登録できる状態ではありません。", ephemeral=True)
                return
            if str(interaction.user.id) != order.buyer_id:
                await interaction.followup.send("このボタンは購入者のみが使用できます。", ephemeral=True)
                return

            order.buyer_ltc_address = ltc_addr
            await session.commit()

        await interaction.followup.send(
            f"LTC受取アドレスを登録しました。\n`{ltc_addr}`\n\n"
            f"次に、販売者が指定した方法でJPY支払いを行い、「**支払いました**」ボタンを押してください。",
            ephemeral=True
        )

        # チャンネルに通知
        channel = interaction.channel
        if channel:
            await channel.send(
                f"<@{interaction.user.id}> がLTC受取アドレスを登録しました。\n"
                f"JPY支払いを完了したら「支払いました」ボタンを押してください。"
            )


class BuyerAddressView(discord.ui.View):
    """
    LTC着金確認後に表示されるビュー。
    買い手: アドレス登録 + 支払い報告
    売り手/管理者: Appeal
    """
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

        # 動的に custom_id を割り当て
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "escrow_register_address_btn":
                    child.custom_id = f"escrow_reg_{order_id}"
                elif child.custom_id == "escrow_paid_btn":
                    child.custom_id = f"escrow_paid_{order_id}"
                elif child.custom_id == "escrow_appeal_btn_addr":
                    child.custom_id = f"escrow_app_buy_{order_id}"

    @discord.ui.button(label="LTC受取アドレスを登録", style=discord.ButtonStyle.primary, custom_id="escrow_register_address_btn")
    async def register_address(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("このボタンは購入者のみが使用できます。", ephemeral=True)
            return
        await interaction.response.send_modal(BuyerAddressModal(self.order_id))

    @discord.ui.button(label="支払いました", style=discord.ButtonStyle.success, custom_id="escrow_paid_btn")
    async def confirm_payment(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("このボタンは購入者のみが使用できます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            order = await session.get(EscrowOrder, self.order_id)
            if not order or order.status != 'WAITING_PAYMENT':
                await interaction.followup.send("この取引は現在支払い報告を受け付けられる状態ではありません。", ephemeral=True)
                return
            if not order.buyer_ltc_address:
                await interaction.followup.send(
                    "まずLTC受取アドレスを登録してください（「LTC受取アドレスを登録」ボタン）。",
                    ephemeral=True
                )
                return

            order.status = 'PAID'
            order.paid_at = func.now()
            await session.commit()

            seller_id = order.seller_id
            order_id = order.id
            net_ltc = float(order.net_ltc)
            buyer_addr = order.buyer_ltc_address

        # このビューのボタンを無効化
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        # 売り手向けリリースビューを表示
        release_view = EscrowReleaseView(order_id=order_id, buyer_id=self.buyer_id, seller_id=seller_id)
        await interaction.channel.send(
            f"<@{seller_id}> 購入者がJPY支払いを報告しました。\n"
            f"着金を確認後、「**LTCをリリース**」ボタンを押してください。\n"
            f"（ボットが `{net_ltc:.8f} LTC` を `{buyer_addr}` へ自動送金します）",
            view=release_view
        )

    @discord.ui.button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger, custom_id="escrow_appeal_btn_addr")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_escrow_appeal(interaction, self.order_id, self.buyer_id, self.seller_id, self)


# ─────────────────────────────────────────────
# 売り手: LTCリリースボタン
# ─────────────────────────────────────────────

class EscrowReleaseView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "escrow_release_btn":
                    child.custom_id = f"escrow_rel_{order_id}"
                elif child.custom_id == "escrow_appeal_btn_release":
                    child.custom_id = f"escrow_app_rel_{order_id}"

    @discord.ui.button(label="着金確認 & LTCをリリース", style=discord.ButtonStyle.primary, custom_id="escrow_release_btn")
    async def release_ltc(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 売り手のみ (管理者のバイパスを削除し、買い手の誤操作を防止)
        if str(interaction.user.id) != self.seller_id:
            await interaction.response.send_message(
                "詐欺防止: このボタンは**販売者**のみが押せます。",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        # フェーズ1: RELEASINGに変更しコミット
        from models import Ledger
        import json as _json

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if not order or order.status == 'COMPLETED':
                    await interaction.followup.send("この取引は既に完了しているか無効です。", ephemeral=True)
                    return
                if order.status not in ['PAID']:
                    await interaction.followup.send(
                        f"この取引は現在 `{order.status}` 状態のため、リリースできません。",
                        ephemeral=True
                    )
                    return

                buyer_addr = order.buyer_ltc_address
                net_ltc = quantize_ltc(order.net_ltc)
                fee_ltc = quantize_ltc(order.fee_ltc)

                if not buyer_addr:
                    await interaction.followup.send("買い手のLTC受取アドレスが登録されていません。", ephemeral=True)
                    return

                order.status = 'RELEASING'
        
        # ── ボットがエスクローアドレスから買い手へ送金 ──
        # feerate=10 (スタンダード) を使用。必要に応じて設定可能にする。
        txid = await broadcast_withdrawal(
            to_address=buyer_addr,
            amount_ltc=net_ltc,
            feerate=10
        )

        if not txid:
            async with AsyncSessionLocal() as session:
                order = await session.get(EscrowOrder, self.order_id)
                if order and order.status == 'RELEASING':
                    order.status = 'PAID'
                    await session.commit()
            await interaction.followup.send(
                "LTC送金に失敗しました。Electrumデーモンの状態を確認してください。元の状態に戻りました。",
                ephemeral=True
            )
            return

        # TXIDのJSON文字列処理
        try:
            parsed = _json.loads(txid)
            if isinstance(parsed, str):
                txid = parsed
        except Exception:
            pass

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                # ステータス更新
                if order:
                    order.status = 'COMPLETED'
                    order.release_txid = str(txid)
                    order.ticket_delete_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)

                    await _release_escrow_address(session, order)

                # 手数料を SystemConfig に計上
                stmt_cfg = select(SystemConfig).with_for_update()
                config = (await session.execute(stmt_cfg)).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal('0'))
                    session.add(config)
                config.collected_fees_ltc = quantize_ltc(config.collected_fees_ltc) + fee_ltc

                # 取引実績を更新（既存 User モデルを流用）
                seller_user = await session.get(User, self.seller_id, with_for_update=True)
                buyer_user = await session.get(User, self.buyer_id, with_for_update=True)
                if seller_user:
                    seller_user.completed_trades += 1
                if buyer_user:
                    buyer_user.completed_trades += 1

                # トランザクションログとLedger（台帳）
                tx_sell = Transaction(
                    user_id=self.seller_id,
                    tx_type="ESCROW_SELL",
                    txid=str(txid),
                    amount_ltc=-net_ltc,
                    fee_ltc=fee_ltc,
                    confirmations=1
                )
                tx_buy = Transaction(
                    user_id=self.buyer_id,
                    tx_type="ESCROW_BUY",
                    txid=str(txid),
                    amount_ltc=net_ltc,
                    fee_ltc=Decimal('0'),
                    confirmations=1
                )
                session.add_all([tx_sell, tx_buy])

                lg_sell = Ledger(user_id=str(self.seller_id), type="ESCROW_RELEASE", amount_ltc=-net_ltc, reference_id=str(self.order_id))
                lg_fee = Ledger(user_id=str(self.seller_id), type="FEE", amount_ltc=-fee_ltc, reference_id=str(self.order_id))
                lg_buy = Ledger(user_id=str(self.buyer_id), type="ESCROW_RELEASE", amount_ltc=net_ltc, reference_id=str(self.order_id))
                session.add_all([lg_sell, lg_fee, lg_buy])

        # ボタン無効化
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        await interaction.channel.send(
            f"**取引が完了しました！**\n"
            f"ボットが `{net_ltc:.8f} LTC` を `{buyer_addr}` へ送金しました。\n"
            f"**TxID:** `{txid}`\n\n"
            f"このチケットは10分後に削除され、ログがDMに送信されます。"
        )

        # マーケット更新
        if ESCROW_MARKET_CHANNEL_ID:
            from cogs.escrow_market import refresh_escrow_market_panels
            await refresh_escrow_market_panels(interaction.client)

    @discord.ui.button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger, custom_id="escrow_appeal_btn_release")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_escrow_appeal(interaction, self.order_id, self.buyer_id, self.seller_id, self)


# ─────────────────────────────────────────────
# 共通: Appeal処理
# ─────────────────────────────────────────────

async def _handle_escrow_appeal(interaction: discord.Interaction, order_id: int, buyer_id: str, seller_id: str, view: discord.ui.View):
    is_party = str(interaction.user.id) in [buyer_id, seller_id]
    is_admin = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', []))
    if not is_party and not is_admin:
        await interaction.response.send_message("このボタンは取引当事者または管理者のみが押せます。", ephemeral=True)
        return

    await interaction.response.defer()

    async with AsyncSessionLocal() as session:
        order = await session.get(EscrowOrder, order_id)
        if not order:
            await interaction.followup.send("取引が見つかりません。", ephemeral=True)
            return
            
        # 未入金時の制限ロジック
        if order.status in ['WAITING_SELLER_LTC', 'SELLER_LTC_DETECTED']:
            await interaction.followup.send(
                "まだボットがLTC（暗号資産）を受け取っていないため、異議申し立てを行うことはできません。\n"
                "入金が行われない等の問題がある場合は、キャンセルを進めるようにしてください。",
                ephemeral=True
            )
            return

        if order:
            order.status = 'APPEALED'
            await session.commit()

    for child in view.children:
        child.disabled = True
    await interaction.edit_original_response(view=view)

    admin_view = EscrowAdminResolutionView(order_id, buyer_id, seller_id)
    await interaction.channel.send(
        f"<@&{ADMIN_ROLE_ID}> **仲介型取引で異議申立てがありました。**\n"
        f"LTC着金済みの状態での申し立てのため、裁定処理時には取引額の1%の追加手数料が控除されます。\n"
        f"エスクローに保管されたLTCは安全です。管理者が裁定を行ってください。",
        view=admin_view
    )


# ─────────────────────────────────────────────
# キャンセルビュー（未入金キャンセル用）
# ─────────────────────────────────────────────

class EscrowCancelView(discord.ui.View):
    """WAITING_SELLER_LTC 段階でのキャンセルボタンと、エラー用Appealボタンを備えたビュー"""
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "escrow_cancel_btn":
                    child.custom_id = f"escrow_can_{order_id}"
                elif child.custom_id == "escrow_appeal_btn_waiting":
                    child.custom_id = f"escrow_app_wait_{order_id}"

    @discord.ui.button(label="キャンセル（LTC未送金の場合のみ）", style=discord.ButtonStyle.secondary, custom_id="escrow_cancel_btn")
    async def cancel_order(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_party = str(interaction.user.id) in [self.buyer_id, self.seller_id]
        is_admin = any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', []))
        if not is_party and not is_admin:
            await interaction.response.send_message("このボタンは取引当事者のみが使用できます。", ephemeral=True)
            return

        await interaction.response.defer()

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if not order:
                    await interaction.followup.send("取引が見つかりません。", ephemeral=True)
                    return
                if order.status not in ['WAITING_SELLER_LTC']:
                    await interaction.followup.send(
                        "**LTCの入金が既に検知されているため、このボタンではキャンセルできません。**\n"
                        "「Appeal (異議申立)」ボタンを使用し、管理者に対応を依頼してください。",
                        ephemeral=True
                    )
                    return

                # ペナルティ処理
                seller_user = await session.get(User, self.seller_id, with_for_update=True)
                buyer_user = await session.get(User, self.buyer_id, with_for_update=True)
                canceller_id = str(interaction.user.id)

                if canceller_id == self.seller_id:
                    if buyer_user and buyer_user.total_trades > 0:
                        buyer_user.total_trades -= 1
                elif canceller_id == self.buyer_id:
                    if seller_user and seller_user.total_trades > 0:
                        seller_user.total_trades -= 1
                else:
                    if seller_user and seller_user.total_trades > 0:
                        seller_user.total_trades -= 1
                    if buyer_user and buyer_user.total_trades > 0:
                        buyer_user.total_trades -= 1

                order.status = 'CANCELLED'
                order.ticket_delete_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
                await _release_escrow_address(session, order)

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            "**取引がキャンセルされました。**\n"
            "（LTCの入金が行われていないため、資金のリスクはありません）\n"
            "このチャンネルは10分後に削除されます。"
        )

        if ESCROW_MARKET_CHANNEL_ID:
            from cogs.escrow_market import refresh_escrow_market_panels
            await refresh_escrow_market_panels(interaction.client)

    @discord.ui.button(label="Appeal (異議申立)", style=discord.ButtonStyle.danger, custom_id="escrow_appeal_btn_waiting")
    async def appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _handle_escrow_appeal(interaction, self.order_id, self.buyer_id, self.seller_id, self)


# ─────────────────────────────────────────────
# 管理者: 強制解決ビュー
# ─────────────────────────────────────────────

class EscrowAdminResolutionView(discord.ui.View):
    def __init__(self, order_id: int, buyer_id: str, seller_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "escrow_admin_force_release":
                    child.custom_id = f"escrow_adm_rel_{order_id}"
                elif child.custom_id == "escrow_admin_refund_seller":
                    child.custom_id = f"escrow_adm_ref_{order_id}"

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return any(r.id == ADMIN_ROLE_ID for r in getattr(interaction.user, 'roles', []))

    @discord.ui.button(label="[Admin] 強制リリース機能を開く", style=discord.ButtonStyle.success, custom_id="escrow_admin_force_release")
    async def force_release(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_admin(interaction):
            await interaction.response.send_message("管理者のみが押せます。", ephemeral=True)
            return

        await interaction.response.defer()

        # フェーズ1: 中間ステータスへ移行
        from models import Ledger
        import json as _json

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if not order or order.status == 'COMPLETED':
                    await interaction.followup.send("既に完了または無効な取引です。", ephemeral=True)
                    return

                if not order.buyer_ltc_address:
                    await interaction.followup.send(
                        "買い手のLTC受取アドレスが未登録です。管理者が直接アドレスを入力して返金/リリースしてください。",
                        ephemeral=True
                    )
                    return

                net_ltc = quantize_ltc(order.net_ltc)
                fee_ltc = quantize_ltc(order.fee_ltc)
                
                # 1%の追加手数料を控除する処理
                penalty_fee_ltc = quantize_ltc(quantize_ltc(order.amount_ltc) * Decimal('0.01'))
                payout_ltc = net_ltc - penalty_fee_ltc
                
                if payout_ltc <= Decimal('0.0001'):
                    await interaction.followup.send(f"控除後のリリース額が小さすぎます。手動で処理してください。", ephemeral=True)
                    return

                buyer_addr = order.buyer_ltc_address
                order.status = 'RELEASING'

        # フェーズ2: ブロードキャスト
        txid = await broadcast_withdrawal(buyer_addr, payout_ltc, feerate=10)
        
        if not txid:
            async with AsyncSessionLocal() as session:
                order = await session.get(EscrowOrder, self.order_id)
                if order and order.status == 'RELEASING':
                    order.status = 'APPEALED' # 管理者介入中なのでAPPEALEDに戻す
                    await session.commit()
            await interaction.followup.send("LTC送金に失敗しました。Electrumデーモンを確認してください。", ephemeral=True)
            return

        try:
            parsed = _json.loads(txid)
            if isinstance(parsed, str): txid = parsed
        except Exception:
            pass

        # フェーズ3: 結果記録
        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if order:
                    order.status = 'COMPLETED'
                    order.release_txid = str(txid)
                    order.ticket_delete_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
                    await _release_escrow_address(session, order)

                stmt_cfg = select(SystemConfig).with_for_update()
                config = (await session.execute(stmt_cfg)).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal('0'))
                    session.add(config)
                # 既存の手数料 + 今回の1%のペナルティを加算
                config.collected_fees_ltc = quantize_ltc(config.collected_fees_ltc) + fee_ltc + penalty_fee_ltc

                tx_b = Transaction(user_id=self.buyer_id, tx_type="ESCROW_BUY_ADMIN", txid=str(txid), amount_ltc=payout_ltc, fee_ltc=Decimal('0'), confirmations=1)
                tx_s = Transaction(user_id=self.seller_id, tx_type="ESCROW_SELL_ADMIN", txid=str(txid), amount_ltc=-payout_ltc, fee_ltc=fee_ltc+penalty_fee_ltc, confirmations=1)
                session.add_all([tx_b, tx_s])

                lg_sell = Ledger(user_id=str(self.seller_id), type="ESCROW_RELEASE", amount_ltc=-payout_ltc, reference_id=str(self.order_id))
                lg_fee = Ledger(user_id=str(self.seller_id), type="FEE", amount_ltc=-(fee_ltc+penalty_fee_ltc), reference_id=str(self.order_id))
                lg_buy = Ledger(user_id=str(self.buyer_id), type="ESCROW_RELEASE", amount_ltc=payout_ltc, reference_id=str(self.order_id))
                session.add_all([lg_sell, lg_fee, lg_buy])

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)
        await interaction.channel.send(
            f"**[管理者] 強制リリース（買い手へ送金）を実行しました。**\n"
            f"1%のペナルティ手数料（`{penalty_fee_ltc:.8f} LTC`）が差し引かれました。\n"
            f"差し引き後 `$ {payout_ltc:.8f} LTC` を `{buyer_addr}` へ送金しました。\nTxID: `{txid}`\n"
            f"チケットは10分後に削除されます。"
        )

    @discord.ui.button(label="[Admin] 売り手アドレスへ返金", style=discord.ButtonStyle.secondary, custom_id="escrow_admin_refund_seller")
    async def refund_seller(self, interaction: discord.Interaction, button: discord.ui.Button):
        """管理者が売り手のアドレスへ直接返金するモーダル"""
        if not self._is_admin(interaction):
            await interaction.response.send_message("管理者のみが押せます。", ephemeral=True)
            return
        await interaction.response.send_modal(AdminRefundModal(self.order_id, self.buyer_id, self.seller_id, self))


class AdminRefundModal(discord.ui.Modal, title="売り手への返金アドレス入力"):
    seller_address = discord.ui.TextInput(
        label="売り手のLTC返金アドレス",
        placeholder="ltc1q...",
        required=True,
        min_length=26,
        max_length=100
    )

    def __init__(self, order_id: int, buyer_id: str, seller_id: str, parent_view: discord.ui.View):
        super().__init__()
        self.order_id = order_id
        self.buyer_id = buyer_id
        self.seller_id = seller_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        from models import Ledger
        import json as _json

        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if not order or order.status == 'COMPLETED':
                    await interaction.followup.send("この取引は既に完了または無効です。", ephemeral=True)
                    return

                # 1%のペナルティ手数料を控除する
                amount_ltc = quantize_ltc(order.amount_ltc)
                penalty_fee_ltc = quantize_ltc(amount_ltc * Decimal('0.01'))
                refund_ltc = amount_ltc - penalty_fee_ltc
                refund_addr = self.seller_address.value.strip()

                if refund_ltc <= Decimal('0.0001'):
                    await interaction.followup.send(f"控除後の返金額が小さすぎます。手動で対応してください。", ephemeral=True)
                    return

                order.status = 'RELEASING'

        # フェーズ2: ブロードキャスト
        txid = await broadcast_withdrawal(refund_addr, refund_ltc, feerate=10)
        
        if not txid:
            async with AsyncSessionLocal() as session:
                order = await session.get(EscrowOrder, self.order_id)
                if order and order.status == 'RELEASING':
                    order.status = 'APPEALED' # 管理者介入中なのでAPPEALEDに戻す
                    await session.commit()
            await interaction.followup.send("返金送金に失敗しました。", ephemeral=True)
            return

        try:
            parsed = _json.loads(txid)
            if isinstance(parsed, str): txid = parsed
        except Exception:
            pass

        # フェーズ3: 結果の記録
        async with AsyncSessionLocal() as session:
            async with session.begin():
                order = await session.get(EscrowOrder, self.order_id, with_for_update=True)
                if order:
                    order.status = 'CANCELLED' # 返金の場合はキャンセル完了扱い
                    order.release_txid = str(txid)
                    order.ticket_delete_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)
                    await _release_escrow_address(session, order)

                stmt_cfg = select(SystemConfig).with_for_update()
                config = (await session.execute(stmt_cfg)).scalar_one_or_none()
                if not config:
                    config = SystemConfig(collected_fees_ltc=Decimal('0'))
                    session.add(config)
                # システムの収益として加算
                config.collected_fees_ltc = quantize_ltc(config.collected_fees_ltc) + penalty_fee_ltc

                tx_refund = Transaction(
                    user_id=self.seller_id,
                    tx_type="ESCROW_REFUND_ADMIN",
                    txid=str(txid),
                    amount_ltc=refund_ltc,
                    fee_ltc=penalty_fee_ltc,
                    confirmations=1
                )
                session.add(tx_refund)

                # 台帳
                lg_refund = Ledger(user_id=str(self.seller_id), type="REFUND", amount_ltc=refund_ltc, reference_id=str(self.order_id))
                lg_fee = Ledger(user_id=str(self.seller_id), type="FEE", amount_ltc=-penalty_fee_ltc, reference_id=str(self.order_id))
                session.add_all([lg_refund, lg_fee])

        # 親ビューのボタンを無効化
        if hasattr(self.parent_view, "children"):
            for child in self.parent_view.children:
                child.disabled = True

        await interaction.followup.send(
            f"返金を実行しました。\n"
            f"1%のペナルティ手数料（`{penalty_fee_ltc:.8f} LTC`）が差し引かれました。\n"
            f"`{refund_ltc:.8f} LTC` → `{refund_addr}`\nTxID: `{txid}`",
            ephemeral=True
        )

        if interaction.channel:
            await interaction.channel.send(
                f"**[管理者] 売り手への返金を実行しました。**\n"
                f"1%の手数料控除後の額が返送されています。\n"
                f"TxID: `{txid}`\nチケットは10分後に削除されます。"
            )


# ─────────────────────────────────────────────
# チケットチャンネル作成
# ─────────────────────────────────────────────

async def create_escrow_ticket_channel(
    guild: discord.Guild,
    seller_member: discord.Member,
    buyer: discord.User,
    order_id: int,
    amount_jpy: int,
    amount_ltc: Decimal,
    net_ltc: Decimal,
    fee_ltc: Decimal,
    escrow_address: str,
    margin_percent: Decimal,
    terms: str,
    timeout_mins: int,
    welcome_message: str,
    seller_ltc_deadline_unix: int,
) -> discord.TextChannel | None:

    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None

    seller_id = seller_member.id

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        buyer: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        seller_member: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
    }
    admin_role = guild.get_role(ADMIN_ROLE_ID)
    if admin_role:
        overwrites[admin_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)

    try:
        channel = await guild.create_text_channel(
            name=f"escrow-{order_id}",
            category=category,
            overwrites=overwrites
        )

        terms_display = terms if terms else "特になし"
        fee_pct = round(fee_ltc / amount_ltc * 100, 2) if amount_ltc > 0 else 0.2

        embed = discord.Embed(
            title="仲介型取引が開始されました",
            description=(
                f"**購入者:** {buyer.mention}\n"
                f"**販売者:** <@{seller_id}>\n\n"
                f"**取引内容:**\n"
                f"JPY支払い額: `¥{amount_jpy:,}`\n"
                f"エスクロー必要LTC: `{amount_ltc:.8f} LTC`（手数料 {fee_pct}% 込み）\n"
                f"買い手への送金額: `{net_ltc:.8f} LTC`\n\n"
                f"**決済条件・注意事項:**\n```\n{terms_display}\n```\n"
            ),
            color=discord.Color.orange()
        )

        # ステップ説明
        embed.add_field(
            name="取引フロー",
            value=(
                "**①** 販売者が下記エスクローアドレスにLTCを送金する\n"
                "**②** ボットが入金を自動検知・承認\n"
                "**③** 購入者がLTC受取アドレスを登録し、JPYを支払う\n"
                "**④** 購入者が「支払いました」ボタンを押す\n"
                "**⑤** 販売者が着金確認後「LTCをリリース」ボタンを押す\n"
                "**⑥** ボットが購入者のアドレスへLTCを自動送金"
            ),
            inline=False
        )

        embed.add_field(
            name=" ①: 販売者のLTC送金先（専用エスクローアドレス）",
            value=(
                f"**アドレス:**\n"
                f"```\n{escrow_address}\n```\n"
                f"**送金額 (コピー用):**\n"
                f"```\n{amount_ltc:.8f}\n```\n"
                f"※この金額を厳守して送金してください\n"
                f"**送金期限: <t:{seller_ltc_deadline_unix}:R>**（期限超過で自動キャンセル）"
            ),
            inline=False
        )

        cancel_view = EscrowCancelView(order_id, str(buyer.id), str(seller_id))
        await channel.send(f"{buyer.mention} <@{seller_id}>", embed=embed, view=cancel_view)

        if welcome_message and str(welcome_message).strip():
            welcome_embed = discord.Embed(
                title="販売者からのメッセージ",
                description=str(welcome_message),
                color=discord.Color.blue()
            )
            await channel.send(embed=welcome_embed)

        return channel

    except Exception as e:
        print(f"Failed to create escrow ticket: {e}")
        return None


class EscrowTicketCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        """ボット起動時にアクティブなエスクロー注文の永続ビューを再登録する"""
        async with AsyncSessionLocal() as session:
            stmt = select(EscrowOrder).where(
                EscrowOrder.status.in_([
                    "WAITING_SELLER_LTC", "SELLER_LTC_DETECTED", "SELLER_LTC_CONFIRMED", 
                    "WAITING_PAYMENT", "PAID", "APPEALED"
                ])
            )
            result = await session.execute(stmt)
            active_orders = result.scalars().all()

        registered = 0
        for order in active_orders:
            oid = order.id
            bid = str(order.buyer_id)
            sid = str(order.seller_id)
            
            self.bot.add_view(BuyerAddressView(oid, bid, sid))
            self.bot.add_view(EscrowReleaseView(oid, bid, sid))
            self.bot.add_view(EscrowAdminResolutionView(oid, bid, sid))
            self.bot.add_view(EscrowCancelView(oid, bid, sid))
            registered += 1

        if registered > 0:
            print(f"[escrow_ticket] {registered} 件のアクティブな仲介注文の View を再登録しました。")


async def setup(bot: commands.Bot):
    await bot.add_cog(EscrowTicketCog(bot))
