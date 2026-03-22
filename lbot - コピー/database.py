import os
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

# PostgreSQLのURLを環境変数から取得
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://user:password@localhost/ltcp2p")

# 非同期エンジンとセッションの作成
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"ssl": False},
    pool_size=20,
    max_overflow=10,
    pool_timeout=30
)

Base = declarative_base()

async def get_session() -> AsyncSession:
    """非同期データベースセッションを提供するジェネレータ（依存注入用や単純呼び出し用）"""
    async with AsyncSessionLocal() as session:
        yield session

async def init_db():
    """データベーステーブルの初期化"""
    async with engine.begin() as conn:
        # 必要なテーブルが存在しない場合は作成する
        await conn.run_sync(Base.metadata.create_all)
        
        # SystemConfigの初期値設定は、アプリケーション側（必要時）またはマイグレーションスクリプトで行います。

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)
