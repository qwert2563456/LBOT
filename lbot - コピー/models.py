import datetime
from sqlalchemy import Column, Integer, String, Boolean, Numeric, DateTime, ForeignKey, Text, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from database import Base, AsyncSessionLocal

class User(Base):
    """一般・管理ユーザーの情報"""
    __tablename__ = 'users'

    discord_id = Column(String(20), primary_key=True)
    available_balance = Column(Numeric(16, 8), default=0.0)
    locked_balance = Column(Numeric(16, 8), default=0.0)
    hd_index = Column(Integer, unique=True, nullable=False)
    deposit_address = Column(String(100), nullable=True, unique=True)
    total_trades = Column(Integer, default=0)
    completed_trades = Column(Integer, default=0)
    is_online = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def total_balance(self):
        return self.available_balance + self.locked_balance


class Ad(Base):
    """P2P売買広告"""
    __tablename__ = 'ads'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    user = relationship('User', foreign_keys=[user_id], lazy='joined')
    margin_percent = Column(Numeric(5, 2), nullable=False)
    min_amount_jpy = Column(Integer, nullable=False)
    max_amount_jpy = Column(Integer, nullable=False)
    terms = Column(Text, nullable=True)
    timeout_mins = Column(Integer, default=15)
    welcome_message = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

Index('idx_ads_is_active', Ad.is_active)


class Order(Base):
    """取引（エスクローロック等）"""
    __tablename__ = 'orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_id = Column(Integer, ForeignKey('ads.id'), nullable=False)
    seller_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    buyer_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    amount_jpy = Column(Integer, nullable=False)
    amount_ltc = Column(Numeric(16, 8), nullable=False)
    fee_ltc = Column(Numeric(16, 8), nullable=False)
    lock_price_jpy = Column(Numeric(10, 2), nullable=False)
    status = Column(String(20), default='PENDING')  # PENDING, PAID, COMPLETED, CANCELLED, APPEALED
    ticket_channel_id = Column(String(20), nullable=True)
    appeal_deadline = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    warned_timeout = Column(Boolean, default=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    ticket_delete_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

Index('idx_orders_status', Order.status)


class Transaction(Base):
    """入出金やトラブル時のトランザクションログ"""
    __tablename__ = 'transactions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    txid = Column(String(64), nullable=True)
    tx_type = Column(String(20), nullable=False)
    amount_ltc = Column(Numeric(16, 8), nullable=False)
    fee_ltc = Column(Numeric(16, 8), default=0.0)
    confirmations = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    """システム全体の設定・手数料プール（シングルトン行）"""
    __tablename__ = 'system_config'

    id = Column(Integer, primary_key=True, default=1)
    collected_fees_ltc = Column(Numeric(16, 8), default=0.0)
    # P2P取引手数料率（小数表現: 0.002 = 0.2%）
    # 既存DBへの追加: ALTER TABLE system_config ADD COLUMN p2p_fee_percent NUMERIC(5,4) DEFAULT 0.002;
    p2p_fee_percent = Column(Numeric(5, 4), default=0.002)
    # 仲介（エスクロー）用手数料率
    escrow_fee_percent = Column(Numeric(5, 4), default=0.002)


class EscrowAddress(Base):
    """仲介型用アドレスプール"""
    __tablename__ = 'escrow_addresses'

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(100), unique=True, nullable=False)
    label = Column(String(50), nullable=False)
    is_in_use = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EscrowAd(Base):
    """仲介型取引広告"""
    __tablename__ = 'escrow_ads'

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    user = relationship('User', foreign_keys=[user_id], lazy='joined')

    # 価格設定
    margin_percent = Column(Numeric(5, 2), nullable=False)
    min_amount_jpy = Column(Integer, nullable=False)
    max_amount_jpy = Column(Integer, nullable=False)

    # 取引条件
    terms = Column(Text, nullable=True)            # 決済方法・注意事項
    timeout_mins = Column(Integer, default=30)     # 支払い期限（分）
    welcome_message = Column(Text, nullable=True)  # チケット開設時メッセージ

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

Index('idx_escrow_ads_is_active', EscrowAd.is_active)


class EscrowOrder(Base):
    """仲介型取引注文（オンチェーンエスクロー）"""
    __tablename__ = 'escrow_orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    ad_id = Column(Integer, ForeignKey('escrow_ads.id'), nullable=False)
    seller_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    buyer_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)

    # 取引金額
    amount_jpy = Column(Integer, nullable=False)           # 買い手が支払うJPY
    amount_ltc = Column(Numeric(16, 8), nullable=False)    # 売り手が送るLTC（手数料込み）
    fee_ltc = Column(Numeric(16, 8), nullable=False)       # 運営手数料(0.2%)
    net_ltc = Column(Numeric(16, 8), nullable=False)       # 買い手が受け取るLTC
    lock_price_jpy = Column(Numeric(10, 2), nullable=False)  

    # オンチェーン管理
    escrow_address_id = Column(Integer, ForeignKey('escrow_addresses.id'), nullable=True)
    escrow_address = Column(String(100), nullable=False)
    escrow_label = Column(String(50), nullable=False)
    seller_sent_txid = Column(String(64), nullable=True)
    buyer_ltc_address = Column(String(100), nullable=True)
    release_txid = Column(String(64), nullable=True)

    # ステータス管理
    status = Column(String(30), default='WAITING_SELLER_LTC')
    ticket_channel_id = Column(String(20), nullable=True)
    escrow_status_message_id = Column(String(20), nullable=True)

    # タイムアウト
    expires_at = Column(DateTime(timezone=True), nullable=True)
    seller_ltc_deadline = Column(DateTime(timezone=True), nullable=True)
    warned_timeout = Column(Boolean, default=False)
    ticket_delete_at = Column(DateTime(timezone=True), nullable=True)

    # 取引統計
    paid_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

Index('idx_escrow_orders_status', EscrowOrder.status)
Index('idx_escrow_orders_escrow_address', EscrowOrder.escrow_address)


async def get_or_create_user(session, discord_id: int):
    """ユーザーが存在しなければ作成し、存在すれば取得して返す"""
    str_id = str(discord_id)
    user = await session.get(User, str_id)
    if user:
        return user

    try:
        result = await session.execute(select(func.coalesce(func.max(User.hd_index), 0)))
        max_idx = result.scalar()
        new_hd_index = max_idx + 1

        new_user = User(
            discord_id=str_id,
            hd_index=new_hd_index,
            available_balance=0.0,
            locked_balance=0.0
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


async def get_p2p_fee_rate(session=None) -> float:
    """
    DBからP2P手数料率を取得する。
    セッションを渡すと既存セッションを再利用し、渡さない場合は新規セッションを作成する。
    """
    DEFAULT_FEE = 0.002  # フォールバック: 0.2%

    async def _fetch(s):
        stmt = select(SystemConfig)
        res = await s.execute(stmt)
        config = res.scalar_one_or_none()
        if config and config.p2p_fee_percent is not None:
            return float(config.p2p_fee_percent)
        return DEFAULT_FEE

    if session is not None:
        return await _fetch(session)

    async with AsyncSessionLocal() as s:
        return await _fetch(s)


async def get_escrow_fee_rate(session=None) -> float:
    """
    DBからエスクロー手数料率を取得する。
    """
    DEFAULT_FEE = 0.002

    async def _fetch(s):
        stmt = select(SystemConfig)
        res = await s.execute(stmt)
        config = res.scalar_one_or_none()
        if config and hasattr(config, "escrow_fee_percent") and config.escrow_fee_percent is not None:
            return float(config.escrow_fee_percent)
        return DEFAULT_FEE

    if session is not None:
        return await _fetch(session)

    async with AsyncSessionLocal() as s:
        return await _fetch(s)