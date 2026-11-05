"""Publishing jobs. The API sends a task *by name*: it never imports worker code."""

from __future__ import annotations

from collections.abc import Callable

from app.core.messages import JobMessage
from app.workers.celery_app import celery_app

Publisher = Callable[[JobMessage, str], None]


def publish_job(job: JobMessage, queue: str) -> None:
    celery_app.send_task(
        "docforge.process_file",
        kwargs={"msg": job.to_wire()},
        queue=queue
    )