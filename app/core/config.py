"""
Application settings (decision D12): every limit and endpoint is configuration.

Secrets have no default on purpose: a missing secret should stop the process at
startup, not at the first request.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MB = 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- connections ---------------------------------------------------------
    database_url: str
    celery_broker_url: str
    s3_access_key: str
    s3_secret_key: str

    # Two endpoints on purpose: a presigned URL is signed for one hostname.
    # Workers reach storage by its internal name, clients by the public one.
    s3_endpoint_internal: str = "http://localhost:8333"
    s3_endpoint_public: str = "http://localhost:8333"
    s3_bucket: str = "docforge-files"
    s3_region: str = "us-east-1"
    qdrant_url: str = "http://localhost:6333"

    # --- limits and routing (D2, D12) ---------------------------------------
    max_file_mb: int = 200
    size_class_small_max_mb: int = 25
    presign_expires_s: int = 900

    # --- queue (W10): the queue type is fixed when a queue is declared --------
    queue_type = Literal["quorum", "classic"] = "quorum"

    # --- pipeline ------------------------------------------------------------
    chunker_version: str = "c1"
    chunk_chars: int = 1800  # about 450 tokens at 4 characters per token (A7)
    chunk_overlap_chars: int = 200
    embed_batch_size: int = 64

    # --- fake embedder (scale-down of A8: the design models 1536 dimensions) --
    embed_dim: int = 128
    embed_latency_s: float = 0.0  # per batch
    embed_error_rate: float = 0.0  # probability that a batch fails with a "429"

    # --- vector collection (W7): a versioned collection read through an alias
    embed_collection: str = "chunks_v1"
    embed_alias: str = "chunks"

    # --- database pool -------------------------------------------------------
    db_pool_size: int = 5
    db_max_overflow: int = 5

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.chunk_overlap_chars * 2 >= self.chunk_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be smaller than half of CHUNK_CHARS")
        if self.size_class_small_max_mb > self.max_file_mb:
            raise ValueError("SIZE_CLASS_SMALL_MAX_MB cannot exceed MAX_FILE_MB")
        if not 0.0 <= self.embed_error_rate <= 1.0:
            raise ValueError("EMBED_ERROR_RATE must be between 0 and 1")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment