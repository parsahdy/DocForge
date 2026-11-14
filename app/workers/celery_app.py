"""Celery configuration. Stage 2 adds: late acknowledgement, time limits, dead letter queue."""

from __future__ import annotations

from typing import Any

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.routing import DEAD_QUEUE, DLX_EXCHANGE, QUEUE_SMALL, QUEUES


def make_celery() -> Celery:
    settings = get_settings()
    app = Celery("docforge", broker=settings.celery_broker_url, include=["app.workers.tasks"])

    queue_arguments: dict[str, Any] = {
        "x-dead-letter-exchange": DLX_EXCHANGE,  # rejected or over-delivered messages go here
        "x-dead-letter-routing-key": DEAD_QUEUE,
    }
    if settings.queue_type == "quorum":
        queue_arguments["x-queue-type"] = "quorum"
        # A message redelivered more than this many times (for example because it kills the worker
        # every time) is dead-lettered instead of looping forever.
        queue_arguments["x-delivery-limit"] = settings.queue_delivery_limit

    app.conf.update(
        task_queues=[
            Queue(name, Exchange(name, type="direct"), routing_key=name, durable=True, queue_arguments=queue_arguments)
            for name in QUEUES
        ],
        task_default_queue=QUEUE_SMALL,
        task_default_queue_type=settings.queue_type,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        # Acknowledge AFTER the work is done: if the worker dies, the broker redelivers the message.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        # Timeouts: soft raises an exception inside the task (a retryable failure),
        # hard kills the worker process (the reconciler recovers the file).
        task_soft_time_limit=settings.task_soft_time_limit_s,
        task_time_limit=settings.task_hard_time_limit_s,
        broker_transport_options={"confirm_publish": True},
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
    )
    return app


celery_app = make_celery()


@celery_setup_logging.connect
def _configure_logging(**_: object) -> None:
    setup_logging()


@worker_process_init.connect
def _reset_connections(**_: object) -> None:
    from app.db.session import reset_engine

    reset_engine()