"""The task is a thin shell: validate the message, hand over to the pipeline."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.core.messages import JobMessage
from app.pipeline.embed import FakeEmbedder
from app.pipeline.run import Deps, run_job
from app.services.storage import Storage
from app.services.vectors import VectorStore
from app.workers.celery_app import celery_app

_deps = Deps | None = None


def get_deps() -> Deps:
    """Built once per worker process, after thr fork."""
    global _deps
    if _deps is None:
        settings = get_settings()
        vectors = VectorStore.from_settings(settings)
        vectors.ensure_collection()
        _deps = Deps(
            settings=settings,
            storage=Storage(settings),
            vectors=vectors,
            embedder=FakeEmbedder(
                dim=settings.embed_dim,
                latency_s=settings.embed_latency_s,
                error_rate=settings.embed_error_rate,
            ),
        )
    return _deps


@celery_app.task(name="docforge.process_file")
def process_file(msg: dict[str, Any]) -> None:
    job = JobMessage.parse(msg)
    run_job(job, get_deps())