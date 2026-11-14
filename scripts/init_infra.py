"""One-time setup: bucket, vector collection and the dead letter queue. Run: python -m scripts.init_infra"""

from kombu import Connection, Exchange, Queue

from app.core.config import Settings, get_settings
from app.core.routing import DEAD_QUEUE, DLX_EXCHANGE
from app.services.storage import Storage
from app.services.vectors import VectorStore


def declare_dead_letter(settings: Settings) -> None:
    arguments = {"x-queue-type": "quorum"} if settings.queue_type == "quorum" else None
    with Connection(settings.celery_broker_url) as connection:
        exchange = Exchange(DLX_EXCHANGE, type="direct", durable=True)
        dead = Queue(DEAD_QUEUE, exchange, routing_key=DEAD_QUEUE, durable=True, queue_arguments=arguments)
        dead(connection.default_channel).declare()  # declares the exchange, the queue and the binding


def main() -> None:
    settings = get_settings()
    Storage(settings).ensure_bucket()
    VectorStore.from_settings(settings).ensure_collection()
    declare_dead_letter(settings)
    print("bucket, collection and dead letter queue are ready")


if __name__ == "__main__":
    main()