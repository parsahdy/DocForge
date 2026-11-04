"""Object key layout (W1, W6): the tenant comes first, and keys are built from IDs only.

The user's file name is never part of a key. Each pipeline stage writes its own
artifact, so a stage can be skipped on retry and later run in another worker pool.
"""

from __future__ import annotations

import uuid


def _base(tenant_id: str, file_id: uuid.UUID) -> str:
    return f"t/{tenant_id}/f/{file_id}"


def original_key(tenant_id: str, file_id: uuid.UUID) -> str:
    return f"{_base(tenant_id, file_id)}/original"


def text_key(tenant_id: str, file_id: uuid.UUID) -> str:
    return f"{_base(tenant_id, file_id)}/text.jsonl.gz"


def chunks_key(tenant_id: str, file_id: uuid.UUID, chunker_version: str) -> str:
    return f"{_base(tenant_id, file_id)}/chunks.{chunker_version}.jsonl.gz"