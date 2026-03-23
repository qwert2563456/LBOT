import datetime
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Boolean, Numeric, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from database import Base, AsyncSessionLocal


class User(Base):
    """一般・管理ユーザーの情報"""
    __tablename__ = 'users'

    discord_id          = Column(String(20), primary_key=True)
    available_balance   = Column(Numeric(16, 8), default=Decimal("0"))
    locked_balance      = Column(Numeric(16, 8), default=Decimal("0"))
    unconfirmed_balance = Column(Numeric(16, 8), default=Decimal("0"))
    # hd_index: 内部的な連番ユーザーID（アドレス派生には使わない）
    hd_index            = Column(Integer, unique=True, nullable=False)
    # deposit_address: 現在ユーザーに表示中の「最新」入金アドレス（キャッシュ）
    deposit_address     = Column(String(100), nullable=True, unique=True)
    total_trades        = Column(Integer, default=0)
    completed_trades    = Column(Integer, default=0)
    is_online           = Column(Boolean, default=True)
    has_agreed_tos      = Column(Boolean, default=False)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())

    # 全入金アドレス履歴（使い捨てアドレス管理）
    deposit_addresses   = relationship('UserDepositAddress', back_populates='user',
                                       lazy='dynamic')

    @property
    def total_balance(self):
        avail  = self.available_balance   or Decimal("0")
        locked = self.locked_balance      or Decimal("0")
        unconf = self.unconfirmed_balance or Decimal("0")
        return avail + locked + unconf


class UserDepositAddress(Base):
    """
    使い捨て入金アドレス履歴テーブル。
    ユーザーが「入金する」を押すたびに新しいアドレスを生成してここに記録する。
    監視ループはこのテーブルを参照して全アドレスを監視する。
    is_active=True が現在ユーザーに表示されているアドレス（1ユーザーにつき1件）。
    """
    __tablename__ = 'user_deposit_addresses'

    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    address    = Column(String(100), unique=True, nullable=False)
    is_active  = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship('User', back_populates='deposit_addresses')


Index('idx_uda_user_id', UserDepositAddress.user_id)
Index('idx_uda_address',  UserDepositAddress.address)
Index('idx_uda_active',   UserDepositAddress.is_active)


class Ledger(Base):
    """
    全残高変動の完全な履歴（複式簿記的台帳）。
    すべての残高操作（DEPOSIT / WITHDRAW / ESCROW_LOCK / P2P_SELL / P2P_BUY /
    FEE / REFUND / ADMIN_ADJUST / ESCROW_RELEASE）はここに記録される。
    """
    __tablename__ = 'ledger'

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    type         = Column(String(20), nullable=False)
    amount_ltc   = Column(Numeric(16, 8), nullable=False)
    reference_id = Column(String(64), nullable=True)   # txid / order_id 等
    note         = Column(String(255), nullable=True)
    created_at   = Column(DateTime(timezone=True), server_default=func.now())


Index('idx_ledger_user_id', Ledger.user_id)


class Ad(Base):
    """P2P売買広告"""
    __tablename__ = 'ads'

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    user            = relationship('User', foreign_keys=[user_id], lazy='joined')
    margin_percent  = Column(Numeric(5, 2), nullable=False)
    min_amount_jpy  = Column(Integer, nullable=False)
    max_amount_jpy  = Column(Integer, nullable=False)
    terms           = Column(Text, nullable=True)
    timeout_mins    = Column(Integer, default=15)
    welcome_message = Column(Text, nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


Index('idx_ads_is_active', Ad.is_active)


class Order(Base):
    """P2P取引注文"""
    __tablename__ = 'orders'

    id                = Column(Integer, primary_key=True, autoincrement=True)
    ad_id             = Column(Integer, ForeignKey('ads.id'), nullable=False)
    seller_id         = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    buyer_id          = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    amount_jpy        = Column(Integer, nullable=False)
    amount_ltc        = Column(Numeric(16, 8), nullable=False)
    fee_ltc           = Column(Numeric(16, 8), nullable=False)
    lock_price_jpy    = Column(Numeric(10, 2), nullable=False)
    status            = Column(String(20), default='PENDING')
    ticket_channel_id = Column(String(20), nullable=True)
    appeal_deadline   = Column(DateTime(timezone=True), nullable=True)
    expires_at        = Column(DateTime(timezone=True), nullable=True)
    warned_timeout    = Column(Boolean, default=False)
    paid_at           = Column(DateTime(timezone=True), nullable=True)
    ticket_delete_at  = Column(DateTime(timezone=True), nullable=True)
    created_at        = Column(DateTime(timezone=True), server_default=func.now())
    updated_at        = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index('idx_orders_status', Order.status)


class Transaction(Base):
    """オンチェーントランザクションログ"""
    __tablename__ = 'transactions'

    id            = Column(Integer, primary_key=True, autoincrement=True)
    user_id       = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    txid          = Column(String(64), nullable=True)
    tx_type       = Column(String(20), nullable=False)
    amount_ltc    = Column(Numeric(16, 8), nullable=False)
    fee_ltc       = Column(Numeric(16, 8), default=Decimal("0"))
    confirmations = Column(Integer, default=0)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    """システム全体の設定・手数料プール（シングルトン行）"""
    __tablename__ = 'system_config'

    id                  = Column(Integer, primary_key=True, default=1)
    collected_fees_ltc  = Column(Numeric(16, 8), default=Decimal("0"))
    p2p_fee_percent     = Column(Numeric(5, 4), default=Decimal("0.002"))
    escrow_fee_percent  = Column(Numeric(5, 4), default=Decimal("0.002"))
    panel_message_ids   = Column(Text, default='{}')


class EscrowAddress(Base):
    """仲介型用エスクローアドレスプール"""
    __tablename__ = 'escrow_addresses'

    id         = Column(Integer, primary_key=True, autoincrement=True)
    address    = Column(String(100), unique=True, nullable=False)
    label      = Column(String(50), nullable=False)
    is_in_use  = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EscrowAd(Base):
    """仲介型取引広告"""
    __tablename__ = 'escrow_ads'

    id              = Column(Integer, primary_key=True, autoincrement=True)
    user_id         = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    user            = relationship('User', foreign_keys=[user_id], lazy='joined')
    margin_percent  = Column(Numeric(5, 2), nullable=False)
    min_amount_jpy  = Column(Integer, nullable=False)
    max_amount_jpy  = Column(Integer, nullable=False)
    terms           = Column(Text, nullable=True)
    timeout_mins    = Column(Integer, default=30)
    welcome_message = Column(Text, nullable=True)
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())


Index('idx_escrow_ads_is_active', EscrowAd.is_active)


class EscrowOrder(Base):
    """仲介型取引注文（オンチェーンエスクロー）"""
    __tablename__ = 'escrow_orders'

    id                        = Column(Integer, primary_key=True, autoincrement=True)
    ad_id                     = Column(Integer, ForeignKey('escrow_ads.id'), nullable=False)
    seller_id                 = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    buyer_id                  = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    amount_jpy                = Column(Integer, nullable=False)
    amount_ltc                = Column(Numeric(16, 8), nullable=False)
    fee_ltc                   = Column(Numeric(16, 8), nullable=False)
    net_ltc                   = Column(Numeric(16, 8), nullable=False)
    lock_price_jpy            = Column(Numeric(10, 2), nullable=False)
    escrow_address_id         = Column(Integer, ForeignKey('escrow_addresses.id'), nullable=True)
    escrow_address            = Column(String(100), nullable=False)
    escrow_label              = Column(String(50), nullable=False)
    seller_sent_txid          = Column(String(64), nullable=True)
    buyer_ltc_address         = Column(String(100), nullable=True)
    release_txid              = Column(String(64), nullable=True)
    status                    = Column(String(30), default='WAITING_SELLER_LTC')
    ticket_channel_id         = Column(String(20), nullable=True)
    escrow_status_message_id  = Column(String(20), nullable=True)
    expires_at                = Column(DateTime(timezone=True), nullable=True)
    seller_ltc_deadline       = Column(DateTime(timezone=True), nullable=True)
    warned_timeout            = Column(Boolean, default=False)
    ticket_delete_at          = Column(DateTime(timezone=True), nullable=True)
    paid_at                   = Column(DateTime(timezone=True), nullable=True)
    created_at                = Column(DateTime(timezone=True), server_default=func.now())
    updated_at                = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index('idx_escrow_orders_status',          EscrowOrder.status)
Index('idx_escrow_orders_escrow_address',  EscrowOrder.escrow_address)


# ─────────────────────────────────────────────────────────────────────────────
# ユーティリティ関数
# ─────────────────────────────────────────────────────────────────────────────

async def get_or_create_user(session, discord_id: int) -> User:
    """ユーザーが存在しなければ作成し、存在すれば取得して返す"""
    str_id = str(discord_id)
    user = await session.get(User, str_id)
    if user:
        return user

    try:
        result = await session.execute(
            select(func.coalesce(func.max(User.hd_index), 0))
        )
        max_idx = result.scalar()
        new_user = User(
            discord_id=str_id,
            hd_index=max_idx + 1,
            available_balance=Decimal("0"),
            locked_balance=Decimal("0"),
            unconfirmed_balance=Decimal("0"),
        )
        session.add(new_user)
        await session.flush()
        return new_user
    except IntegrityError:
        await session.rollback()
        user = await session.get(User, str_id)
        if user:
            return user
        raise


async def get_p2p_fee_rate(session=None) -> Decimal:
    """DBからP2P手数料率を取得する"""
    DEFAULT_FEE = Decimal("0.002")

    async def _fetch(s):
        res = await s.execute(select(SystemConfig))
        config = res.scalar_one_or_none()
        if config and config.p2p_fee_percent is not None:
            return Decimal(str(config.p2p_fee_percent))
        return DEFAULT_FEE

    if session is not None:
        return await _fetch(session)
    async with AsyncSessionLocal() as s:
        return await _fetch(s)


async def get_escrow_fee_rate(session=None) -> Decimal:
    """DBからエスクロー手数料率を取得する"""
    DEFAULT_FEE = Decimal("0.002")

    async def _fetch(s):
        res = await s.execute(select(SystemConfig))
        config = res.scalar_one_or_none()
        if config and config.escrow_fee_percent is not None:
            return Decimal(str(config.escrow_fee_percent))
        return DEFAULT_FEE

    if session is not None:
        return await _fetch(session)
    async with AsyncSessionLocal() as s:
        return await _fetch(s)