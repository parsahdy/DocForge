"""Celery configuration. Only what Stage 1 needs; reliability settings arrive in Stage 2."""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_process_init
from kombu import Exchange, Queue

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.routing import QUEUE_SMALL, QUEUES


def make_celery() -> Celery:
    settings = get_settings()
    app = Celery("docforge", broker=settings.celery_broker_url, include=["app.workers.tasks"])

    queue_arguments = {"x-queue-type": "quorum"} if settings.queue_type == "quorum" else None
    app.conf.update(
        task_queues=[
            Queue(name, Exchange(name, type="direct"), routing_key=name,
                  durable=True, queue_arguments=queue_arguments)
                  for name in QUEUES
        ],
        task_default_queue=QUEUE_SMALL,
        task_default_queue_type=settings.queue_type,
        task_serializer="json",
        accept_content=["json"],
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"confirm_publish": True},
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
    )
    return app

celery_app = make_celery()

@celery_setup_logging.connect
def _configure_logging(**_: object):
    setup_logging()


@worker_process_init.connect
def _reset_connections(**_: object):
    from app.db.session import reset_engine
    reset_engine()
    