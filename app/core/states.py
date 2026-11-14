"""The file status state machine. The database, not the queue, is the source of truth."""

from __future__ import annotations

from enum import Enum


class Status(str, Enum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY",
    FAILED = "FAILED"


ALLOWED: dict[Status, frozenset[Status]] = {
    Status.PENDING_UPLOAD: frozenset({Status.UPLOADED}),
    Status.UPLOADED: frozenset({Status.QUEUED, Status.FAILED}),
    Status.QUEUED: frozenset({Status.PROCESSING, Status.FAILED}),
    Status.PROCESSING: frozenset({Status.READY, Status.FAILED, Status.QUEUED}),  # QUEUED = retry or reaper
    Status.READY: frozenset(),
    Status.FAILED: frozenset({Status.QUEUED}),  # manual retry (FR4)
}

class IllegalTransition(Exception):
    pass


def check_transition(src: Status, dst: Status) -> None:
    if dst not in ALLOWED[src]:
        raise IllegalTransition(f"{src.value} -> {dst.value} is not allowed.")