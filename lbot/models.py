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
    available_balance = Column(Numeric(16, 8), default=0.0) # 使用可能LTC
    locked_balance = Column(Numeric(16, 8), default=0.0)    # Escrow等でロック中のLTC
    hd_index = Column(Integer, unique=True, nullable=False) # アドレス生成用ID
    deposit_address = Column(String(100), nullable=True, unique=True)     # HD管理: ユーザー固定の入金アドレス
    total_trades = Column(Integer, default=0) # 総取引数
    completed_trades = Column(Integer, default=0) # 完了取引数
    is_online = Column(Boolean, default=True) # 状態
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
    margin_percent = Column(Numeric(5, 2), nullable=False) # %指定 (例:95.00)
    min_amount_jpy = Column(Integer, nullable=False)
    max_amount_jpy = Column(Integer, nullable=False)
    terms = Column(Text, nullable=True) # 決済方法・注意事項など
    timeout_mins = Column(Integer, default=15)
    welcome_message = Column(Text, nullable=True) # 追加: チケット開設時に送信する挨拶・注意事項 # 取引の自動キャンセルまでの時間（分）
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
    amount_ltc = Column(Numeric(16, 8), nullable=False) # 取引額
    fee_ltc = Column(Numeric(16, 8), nullable=False)    # 0.2%手数料
    lock_price_jpy = Column(Numeric(10, 2), nullable=False) # ロック時の1LTC価格
    status = Column(String(20), default='PENDING') # PENDING, PAID, COMPLETED, CANCELLED, APPEALED
    ticket_channel_id = Column(String(20), nullable=True) # 専用DiscordチャンネルID
    appeal_deadline = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True) # 支払い期限
    warned_timeout = Column(Boolean, default=False) # 期限警告メンション送信済みフラグ
    paid_at = Column(DateTime(timezone=True), nullable=True) # 支払い報告日時
    ticket_delete_at = Column(DateTime(timezone=True), nullable=True) # チケット自動削除予定日時
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

Index('idx_orders_status', Order.status)

class Transaction(Base):
    """入出金やトラブル時のトランザクションログ"""
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(20), ForeignKey('users.discord_id'), nullable=False)
    txid = Column(String(64), nullable=True)
    tx_type = Column(String(20), nullable=False) # DEPOSIT, WITHDRAW
    amount_ltc = Column(Numeric(16, 8), nullable=False)
    fee_ltc = Column(Numeric(16, 8), default=0.0)  # 出金手数料（ユーザー課金分）
    confirmations = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class SystemConfig(Base):
    """システム全体の設定・手数料プール（シングルトン行）"""
    __tablename__ = 'system_config'
    
    id = Column(Integer, primary_key=True, default=1)
    collected_fees_ltc = Column(Numeric(16, 8), default=0.0)

async def get_or_create_user(session, discord_id: int):
    """ユーザーが存在しなければ作成し、存在すれば取得して返す（自動登録用）"""
    str_id = str(discord_id)
    user = await session.get(User, str_id)
    if user:
        return user
        
    try:
        # 新しいhd_indexを決定する (最も大きいhd_index + 1)
        # 初期ユーザーは1からスタート
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
        # flush等で確定
        await session.flush()
        return new_user
    except IntegrityError:
        # 他のセッションによる同時作成の場合はリトライして返す
        await session.rollback()
        user = await session.get(User, str_id)
        if user:
            return user
        raise
