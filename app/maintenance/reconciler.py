"""Reconciler: the database is the source of truth, the queue is only a doorbell.

It enforces the "stuck-state invariants" from docs/failure-modes.md:

    UPLOADED    older than STUCK_UPLOADED_S   -> advance to QUEUED          (crash between two commits, F04)
    PROCESSING  older than STUCK_PROCESSING_S -> back to QUEUED, or FAILED  (worker died, F01)
    QUEUED      and "due"                     -> publish                    (retries, lost messages, F04)

Every change is a compare-and-set, so several reconcilers (or a reconciler and a worker) can run
at once without harming each other. Publishing is the ONLY thing that sends messages from here:
the other rules just make a file "due", and one function publishes everything that is due.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import log_context
from app.core.messages import JobMessage
from app.core.routing import route_queue
from app.core.states import Status
from app.db.models import File, utcnow
from app.db.session import session_scope
from app.db.transitions import transition
from app.services.queue import Publisher
from app.services.storage import Storage

logger = logging.getLogger("docforge.reconciler")


def reconcile_once(settings: Settings, storage: Storage, publish: Publisher) -> dict[str, int]:
    now = utcnow()
    advanced = _advance_uploaded(settings, now)
    requeued, lost = _reap_processing(settings, now)
    published, missing = _publish_due(settings, storage, publish)
    return {
        "advanced_uploaded": advanced,
        "reaped_requeued": requeued,
        "reaped_failed": lost,
        "published": published,
        "failed_original_missing": missing,
    }


def _advance_uploaded(settings: Settings, now: datetime) -> int:
    cutoff = now - timedelta(seconds=settings.stuck_uploaded_s)
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id, File.size_bytes)
            .where(File.status == Status.UPLOADED.value, File.updated_at <= cutoff)
            .order_by(File.updated_at)
            .limit(settings.maintenance_batch)
        ).all()
    moved = 0
    for tenant_id, file_id, size_bytes in rows:
        queue = route_queue(size_bytes, settings.size_class_small_max_mb)
        with session_scope() as db:
            # retry_at = now makes the file "due": _publish_due will pick it up in this same cycle.
            if transition(db, tenant_id, file_id, Status.UPLOADED, Status.QUEUED, queue=queue, retry_at=utcnow()):
                moved += 1
    return moved


def _reap_processing(settings: Settings, now: datetime) -> tuple[int, int]:
    cutoff = now - timedelta(seconds=settings.stuck_processing_s)
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id, File.attempts)
            .where(File.status == Status.PROCESSING.value, File.updated_at <= cutoff)
            .order_by(File.updated_at)
            .limit(settings.maintenance_batch)
        ).all()
    requeued = failed = 0
    for tenant_id, file_id, attempts in rows:
        with log_context(file_id=str(file_id), tenant_id=tenant_id), session_scope() as db:
            if attempts >= settings.max_attempts:
                if transition(
                    db, tenant_id, file_id, Status.PROCESSING, Status.FAILED,
                    error_code="WORKER_LOST", error_message=f"worker lost; gave up after {attempts} attempts",
                    finished_at=utcnow(),
                ):
                    failed += 1
            elif transition(
                db, tenant_id, file_id, Status.PROCESSING, Status.QUEUED,
                error_code="WORKER_LOST", error_message="worker lost; retrying", retry_at=utcnow(),
            ):
                requeued += 1
    return requeued, failed


def _publish_due(settings: Settings, storage: Storage, publish: Publisher) -> tuple[int, int]:
    now = utcnow()
    lost_cutoff = now - timedelta(seconds=settings.stuck_queued_s)
    due = or_(
        File.retry_at <= now,  # a scheduled retry whose time has come
        and_(File.retry_at.is_(None), File.updated_at <= lost_cutoff),  # queued for too long: the message may be lost
    )
    with session_scope() as db:
        rows = db.execute(
            select(File.tenant_id, File.file_id, File.queue, File.size_bytes, File.s3_key)
            .where(File.status == Status.QUEUED.value, due)
            .order_by(File.updated_at)
            .limit(settings.maintenance_batch)
        ).all()

    published = missing = 0
    for tenant_id, file_id, queue, size_bytes, s3_key in rows:
        with log_context(file_id=str(file_id), tenant_id=tenant_id):
            try:
                if storage.head(s3_key) is None:  # the record exists but the object does not
                    with session_scope() as db:
                        transition(
                            db, tenant_id, file_id, Status.QUEUED, Status.FAILED,
                            error_code="ORIGINAL_MISSING",
                            error_message="the uploaded file no longer exists in storage",
                            finished_at=utcnow(),
                        )
                    missing += 1
                    continue
                # Publish first, then mark: if we crash in between, the file stays "due" and is
                # published again (a duplicate message is harmless, a lost one is not).
                publish(JobMessage(file_id=file_id, tenant_id=tenant_id), queue or route_queue(size_bytes, settings.size_class_small_max_mb))
                with session_scope() as db:
                    _touch_queued(db, tenant_id, file_id)
                published += 1
            except Exception:  # noqa: BLE001 - one bad file must not stop the cycle
                logger.exception("could not reconcile file; will try again next cycle")
    return published, missing


def _touch_queued(db: Session, tenant_id: str, file_id) -> None:
    """Clear retry_at and refresh updated_at, so the file is not "due" again immediately."""
    db.execute(
        update(File)
        .where(File.tenant_id == tenant_id, File.file_id == file_id, File.status == Status.QUEUED.value)
        .values(retry_at=None, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )