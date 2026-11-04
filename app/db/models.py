"""Database model. ``tenant_id`` leads the primary key and every index (W1)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class File(Base):
    __tablename__ = "files"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)

    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    s3_key: Mapped[str] = mapped_column(String(512))

    status: Mapped[str] = mapped_column(String(20))
    queue: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_files_tenant_status", "tenant_id", "status"),)