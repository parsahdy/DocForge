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
    queue_type: Literal["quorum", "classic"] = "quorum"

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

    # --- reliability (Stage 2) -----------------------------------------------
    max_attempts: int = 5  # total attempts per file, the first one included
    retry_base_s: int = 10  # the backoff ceiling doubles from this value...
    retry_cap_s: int = 300  # ...up to this value
    task_soft_time_limit_s: int = 600
    task_hard_time_limit_s: int = 900
    queue_delivery_limit: int = 5  # quorum queues: redeliveries before a message is dead-lettered

    # --- maintenance: reconciler, janitor, retention (D5) ----------------------
    reconcile_interval_s: int = 30
    maintenance_batch: int = 200  # files per rule per cycle: bounds the burst we create ourselves
    stuck_uploaded_s: int = 120
    stuck_queued_s: int = 900
    stuck_processing_s: int = 1200  # must exceed the hard time limit
    pending_upload_ttl_s: int = 86400
    orphan_grace_s: int = 3600
    retention_originals_days: int = 7
    retention_failed_days: int = 30

    # --- tenant fairness (Stage 3) -------------------------------------------
    redis_state_url: str = "redis://localhost:6379/0"
    tenant_rate_per_s: int = 5  # new uploads per second per tenant
    tenant_max_backlog: int = 500  # UPLOADED + QUEUED + PROCESSING files per tenant
    tenant_max_concurrent: int = 4  # files of one tenant processed at the same time
    tenant_defer_s: int = 15  # base wait when a tenant is at its concurrency cap
    tenant_retry_after_s: int = 30  # Retry-After for a full backlog
    reconcile_per_tenant_batch: int = 20  # files per tenant per reconcile cycle

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if self.chunk_overlap_chars * 2 >= self.chunk_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be smaller than half of CHUNK_CHARS")
        if self.size_class_small_max_mb > self.max_file_mb:
            raise ValueError("SIZE_CLASS_SMALL_MAX_MB cannot exceed MAX_FILE_MB")
        if not 0.0 <= self.embed_error_rate <= 1.0:
            raise ValueError("EMBED_ERROR_RATE must be between 0 and 1")
        if self.task_soft_time_limit_s >= self.task_hard_time_limit_s:
            raise ValueError("TASK_SOFT_TIME_LIMIT_S must be smaller than TASK_HARD_TIME_LIMIT_S")
        if self.stuck_processing_s <= self.task_hard_time_limit_s:
            raise ValueError("STUCK_PROCESSING_S must exceed TASK_HARD_TIME_LIMIT_S, or healthy tasks get reaped")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment