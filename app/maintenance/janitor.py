"""Janitor: lifecycle policy (D5) and consistency between storage and the database.

Three jobs:
    expire_pending_uploads  uploads that never completed (F03): delete the object, then the record
    apply_retention         delete originals of old READY / FAILED files (the text artifact stays,
                            so the vectors can still be rebuilt without the original, W6)
    find_orphan_objects     objects in storage that have NO database record (a slow audit, dry run by default)

The opposite direction (a record whose object is missing) is checked by the reconciler,
right before it would publish a file.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select, tuple_, update

from app.core.config import Settings
from app.core.states import Status
from app.db.models import File, utcnow
from app.db.session import session_scope
from app.services.storage import Storage

logger = logging.getLogger("docforge.janitor")


def expire_pending_uploads(settings: Settings, storage: Storage) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.pending_upload_ttl_s)
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id, File.s3_key)
            .where(File.status == Status.PENDING_UPLOAD.value, File.updated_at <= cutoff)
            .limit(settings.maintenance_batch)
        ).all()
    for tenant_id, file_id, s3_key in rows:
        storage.delete(s3_key)  # object first, record second: a crash in between is repaired by the next run
        with session_scope() as db:
            db.execute(
                delete(File).where(
                    File.tenant_id == tenant_id,
                    File.file_id == file_id,
                    File.status == Status.PENDING_UPLOAD.value,
                )
            )
    return len(rows)


def apply_retention(settings: Settings, storage: Storage) -> int:
    now = utcnow()
    ready_cutoff = now - timedelta(days=settings.retention_originals_days)
    failed_cutoff = now - timedelta(days=settings.retention_failed_days)
    expired = or_(
        and_(File.status == Status.READY.value, File.finished_at <= ready_cutoff),
        and_(File.status == Status.FAILED.value, File.finished_at <= failed_cutoff),
    )
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id, File.s3_key)
            .where(expired, File.original_deleted_at.is_(None))
            .limit(settings.maintenance_batch)
        ).all()
    for tenant_id, file_id, s3_key in rows:
        storage.delete(s3_key)
        with session_scope() as db:
            db.execute(
                update(File)
                .where(File.tenant_id == tenant_id, File.file_id == file_id)
                .values(original_deleted_at=utcnow())
                .execution_options(synchronize_session=False)
            )
    return len(rows)


def find_orphan_objects(settings: Settings, storage: Storage, delete_found: bool = False) -> list[str]:
    """Slow: lists the whole bucket. Run it rarely, and look at the report before enabling deletion."""
    grace_cutoff = utcnow() - timedelta(seconds=settings.orphan_grace_s)
    orphans: list[str] = []
    page: list[tuple[str, uuid.UUID, str]] = []

    def flush() -> None:
        keys = _keys_without_record(page)
        if delete_found:
            for key in keys:
                storage.delete(key)
        orphans.extend(keys)
        page.clear()

    for obj in storage.iter_objects(prefix="t/"):
        parsed = _parse_key(obj["Key"])
        if parsed is None or obj["LastModified"] > grace_cutoff:  # skip foreign keys and very fresh objects
            continue
        page.append((parsed[0], parsed[1], obj["Key"]))
        if len(page) >= 500:
            flush()
    if page:
        flush()
    return orphans


def _parse_key(key: str) -> tuple[str, uuid.UUID] | None:
    parts = key.split("/")  # t/{tenant}/f/{file_id}/{name}
    if len(parts) < 5 or parts[0] != "t" or parts[2] != "f":
        return None
    try:
        return parts[1], uuid.UUID(parts[3])
    except ValueError:
        return None


def _keys_without_record(page: list[tuple[str, uuid.UUID, str]]) -> list[str]:
    pairs = list({(tenant, file_id) for tenant, file_id, _ in page})
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id).where(tuple_(File.tenant_id, File.file_id).in_(pairs))
        ).all()
    known = {(row[0], row[1]) for row in rows}
    return [key for tenant, file_id, key in page if (tenant, file_id) not in known]