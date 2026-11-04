"""create files table

Revision ID: 0001
Revises:
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("s3_key", sa.String(512), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("queue", sa.String(20), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "file_id"),
    )
    op.create_index("ix_files_tenant_status", "files", ["tenant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_files_tenant_status", table_name="files")
    op.drop_table("files")