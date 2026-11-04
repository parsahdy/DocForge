"""The router (D2, W4): one function maps file metadata to a queue.

Producers never name a queue themselves, so adding a class later (for example
``ocr``) changes this function and the worker deployment, and nothing else.
"""

from __future__ import annotations

from app.core.config import MB


QUEUE_SMALL = "small"
QUEUE_LARGE = "large"
QUEUES = (QUEUE_SMALL, QUEUE_LARGE)


def route_queue(size_bytes: int, small_max_mb: int) -> str:
    return QUEUE_SMALL if size_bytes < small_max_mb * MB else QUEUE_LARGE