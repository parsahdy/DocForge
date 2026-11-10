"""The pipeline orchestrator. Independent of Celery:

    a worker task is only a thin shell around it (D11).

Each stage reads its input from storage and writes its output to storage (W6):

    original -> parse -> text.jsonl.gz -> chunk -> chunks.<ver>.jsonl.gz -> embed -> Qdrant

A stage whose artifact already exists is skipped, so a retry never repeats finished work.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qdrant_client import models as qm

from app.core.config import Settings
from app.core.errors import PermanentError, TransientError
from app.core.ids import chunk_id
from app.core.keys import chunks_key, text_key
from app.core.logging import log_context
from app.core.messages import JobMessage
from app.core.states import Status
from app.db.models import File, utcnow
from app.db.session import session_scope
from app.db.transitions import transition
from app.pipeline.chunk import chunk_texts
from app.pipeline.embed import Embedder
from app.pipeline.parse import parse_file
from app.services.storage import Storage
from app.services.vectors import VectorStore

logger = logging.getLogger("docforge.pipeline")


@dataclass(frozen=True)
class Deps:
    settings: Settings
    storage: Storage
    vectors: VectorStore
    embedder: Embedder


@dataclass(frozen=True)
class FileInfo:
    tenant_id: str
    file_id: uuid.UUID
    filename: str
    s3_key: str


def run_job(job: JobMessage, deps: Deps) -> None:
    with log_context(file_id=str(job.file_id), tenant_id=job.tenant_id):
        info = _claim(job)
        if info is None:
            return  # duplicate or stale message: someone else owns this file, nothing to do
        started = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="docforge-") as tmp:  # no state survives the task (W9)
                work = Path(tmp)
                sha256 = stage_parse(deps, info, work)
                stage_chunk(deps, info, work)
                chunk_count = stage_embed(deps, info, work)
            _finish(job, sha256, chunk_count)
            logger.info(
                "file ready",
                extra={"fields": {"chunks": chunk_count, "duration_s": round(time.perf_counter() - started, 2)}},
            )
        except Exception as exc:  # noqa: BLE001 - every failure must end in a recorded state
            _fail(job, exc)


def _claim(job: JobMessage) -> FileInfo | None:
    """QUEUED -> PROCESSING as a compare-and-set. Exactly one worker can win."""
    with session_scope() as db:
        won = transition(
            job,
            job.tenant_id,
            job.file_id,
            Status.QUEUED,
            Status.PROCESSING,
            attempts=File.attempts + 1,
            started_at=utcnow(),
            error_code=None,
            error_message=None,
        )
        row = db.get(File, (job.tenant_id, job.file_id))
        if not won or row is None:
            logger.info(
                "job skipped",
                extra={"fields": {"reason": "not_queued", "status": row.status if row else "missing"}}
            )
            return None
        return FileInfo(job.tenant_id, job.file_id, row.filename, row.s3_key)


def _finish(job: JobMessage, sha256: str, chunk_count: int) -> None:
    with session_scope() as db:
        done = transition(
            db,
            job.tenant_id,
            job.file_id,
            Status.PROCESSING,
            Status.READY,
            sha256=sha256 or None,
            chunk_count=chunk_count,
            finished_at=utcnow(),
        )
        if not done:
            logger.warning("could not mark READY: state changed underneath us")


def _fail(job: JobMessage, exc: Exception) -> None:
    if isinstance(exc, (PermanentError, TransientError)):
        code, message = exc.code, str(exc)
    else:
        code, message = "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}"
    if isinstance(exc, PermanentError):
        logger.warning("file failed", extra={"fields": {"error_code": code}})
    else:
        logger.error("file failed", exc_info=exc, extra={"fields": {"error_code": code}})
    with session_scope() as db:
        transition(
            db,
            job.tenant_id,
            job.file_id,
            Status.PROCESSING,
            Status.FAILED,
            error_code=code,
            error_message=message[:500],
            finished_at=utcnow(),
        )


def stage_parse(deps: Deps, info: FileInfo, work: Path) -> str:
    """original -> text artifact. Returns the SHA-256 of the original."""
    key = text_key(info.tenant_id, info.file_id)
    existing = deps.storage.head(key)
    if existing is not None:
        logger.info("stage skipped", extra={"fields": {"stage": "parse"}})
        return existing.get("Metadata", {}).get("sha256", "")

    original = work / "original"
    deps.storage.download(info.s3_key, original)
    sha256 = _sha256_file(original)

    text_path = work / "text.jsonl.gz"
    blocks = 0
    with gzip.open(text_path, "wt", encoding="utf-8") as out:
        for block in parse_file(original, info.filename):
            out.write(json.dumps({"loc": block.loc, "text": block.text}, ensure_ascii=False) + "\n")
            blocks += 1
    if blocks == 0:
        raise PermanentError("NO_TEXT", "no extractable text (empty document, or scanned images)")

    deps.storage.upload(text_path, key, metadata={"sha256": sha256, "blocks": str(blocks)})
    return sha256


def stage_chunk(deps: Deps, info: FileInfo, work: Path) -> int:
    """text artifact -> chunks artifact. Returns the number of chunks."""
    version = deps.settings.chunker_version
    key = chunks_key(info.tenant_id, info.file_id, version)
    existing = deps.storage.head(key)
    if existing is not None:
        logger.info("stage skipped", extra={"fields": {"stage": "chunk"}})
        return int(existing.get("Metadata", {}).get("chunks", "0"))

    text_path = work / "text.in.jsonl.gz"
    deps.storage.download(text_key(info.tenant_id, info.file_id), text_path)  # stages talk through storage only

    chunks_path = work / "chunks.out.jsonl.gz"
    count = 0
    texts = (record["text"] for record in _read_jsonl(text_path))
    with gzip.open(chunks_path, "wt", encoding="utf-8") as out:
        for chunk in chunk_texts(texts, deps.settings.chunk_chars, deps.settings.chunk_overlap_chars):
            out.write(json.dumps({"i": chunk.index, "text": chunk.text}, ensure_ascii=False) + "\n")
            count += 1
    if count == 0:
        raise PermanentError("NO_TEXT", "no text left after chunking")

    deps.storage.upload(chunks_path, key, metadata={"chunks": str(count)})
    return count


def stage_embed(deps: Deps, info: FileInfo, work: Path) -> int:
    """chunks artifact -> vectors in Qdrant, in batches. Returns the number of points written."""
    chunks_path = work / "chunks.in.jsonl.gz"
    deps.storage.download(chunks_key(info.tenant_id, info.file_id, deps.settings.chunker_version), chunks_path)

    total = 0
    batch: list[dict[str, Any]] = []
    for record in _read_jsonl(chunks_path):
        batch.append(record)
        if len(batch) >= deps.settings.embed_batch_size:
            total += _embed_batch(deps, info, batch)
            batch = []
    if batch:
        total += _embed_batch(deps, info, batch)
    return total


def _embed_batch(deps: Deps, info: FileInfo, batch: list[dict[str, Any]]) -> int:
    version = deps.settings.chunker_version
    vectors = deps.embedder.embed([record["text"] for record in batch])
    points = [
        qm.PointStruct(
            id=str(chunk_id(info.tenant_id, info.file_id, version, record["i"])),  # deterministic (W3)
            vector=vector,
            payload={
                "tenant_id": info.tenant_id,
                "file_id": str(info.file_id),
                "chunk_index": record["i"],
                "chunker_version": version,
                "text": record["text"],
            },
        )
        for record, vector in zip(batch, vectors, strict=True)
    ]
    deps.vectors.upsert(points)
    return len(points)


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            yield json.loads(line)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

