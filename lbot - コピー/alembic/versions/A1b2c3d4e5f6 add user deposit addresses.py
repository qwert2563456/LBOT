"""Add user_deposit_addresses table for disposable address support

Revision ID: a1b2c3d4e5f6
Revises: 4911cf6e5801
Create Date: 2026-03-23 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4911cf6e5801'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. user_deposit_addresses テーブル作成 ──────────────────
    op.create_table(
        'user_deposit_addresses',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.String(length=20), nullable=False),
        sa.Column('address', sa.String(length=100), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.discord_id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('address', name='uq_user_deposit_addresses_address'),
    )
    op.create_index('idx_uda_user_id', 'user_deposit_addresses', ['user_id'], unique=False)
    op.create_index('idx_uda_address',  'user_deposit_addresses', ['address'],  unique=False)
    op.create_index('idx_uda_active',   'user_deposit_addresses', ['is_active'], unique=False)

    # ── 2. 既存の deposit_address を新テーブルへ移行 ────────────
    # users テーブルに deposit_address が入っているユーザーを全件コピー
    op.execute("""
        INSERT INTO user_deposit_addresses (user_id, address, is_active, created_at)
        SELECT discord_id, deposit_address, true, NOW()
        FROM users
        WHERE deposit_address IS NOT NULL
        ON CONFLICT (address) DO NOTHING
    """)

    # ── 3. users.deposit_address はキャッシュとして残す（NULL化しない）──
    # 後方互換のため deposit_address カラム自体は残す
    # 新規生成時は両方更新する運用に変わる


def downgrade() -> None:
    op.drop_index('idx_uda_active',   table_name='user_deposit_addresses')
    op.drop_index('idx_uda_address',  table_name='user_deposit_addresses')
    op.drop_index('idx_uda_user_id',  table_name='user_deposit_addresses')
    op.drop_table('user_deposit_addresses')