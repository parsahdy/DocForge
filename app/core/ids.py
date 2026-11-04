"""Identifier generation (W2, W3)."""

from __future__ import annotations

import os
import time
import uuid

# Fixed forever. Changing it would change every chunk ID ever produced.
_CHUNK_NAMESPACE = uuid.UUID("6f1c2b0e-8d57-4c1a-9a53-3f6f0c2d7b11")


def uuid7() -> uuid.UUID:
    """UUIDv7 (RFC 9562), created in the application: no database sequence.

    Layout, from the most significant bit:
    48 bits timestamp | 4 bits version (7) | 12 bits random | 2 bits variant | 62 bits random
    IDs created in different milliseconds sort by creation time.
    """
    ts_ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")  # 80 random bits
    rand_a = rand >> 68  # top 12 bits
    rand_b = rand & ((1 << 62) - 1)  # low 62 bits
    value = (ts_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def chunk_id(tenant_id: str, file_id: uuid.UUID, chunker_version: str, chunk_index: int) -> uuid.UUID:
    """Deterministic point ID: processing the same file twice overwrites, never duplicates.

    Tenant and file are part of the ID on purpose. With only a content hash, two
    tenants uploading the same document would overwrite each other's points.
    """
    return uuid.uuid5(_CHUNK_NAMESPACE, f"{tenant_id}:{file_id}:{chunker_version}:{chunk_index}")