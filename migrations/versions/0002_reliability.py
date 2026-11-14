"""retry_at, original_deleted_at and the reconciler index

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("files", sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("files", sa.Column("original_deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_files_status_updated", "files", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_files_status_updated", table_name="files")
    op.drop_column("files", "original_deleted_at")
    op.drop_column("files", "retry_at")