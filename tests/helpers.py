"""Shared test helpers: a fake database session scope and a lightweight settings object."""

from types import SimpleNamespace
from unittest.mock import MagicMock


def fake_scope(db=None):
    """Replacement for ``session_scope``: ``with scope() as db`` yields one shared mock session."""
    if db is None:
        db = MagicMock()
    scope = MagicMock()
    scope.return_value.__enter__.return_value = db
    scope.return_value.__exit__.return_value = False
    return scope, db


def make_settings(**overrides):
    """Only the attributes the code under test reads; avoids needing real secrets or env vars."""
    values = dict(
        max_attempts=5,
        retry_base_s=10,
        retry_cap_s=300,
        maintenance_batch=200,
        stuck_uploaded_s=120,
        stuck_queued_s=900,
        stuck_processing_s=1200,
        pending_upload_ttl_s=86400,
        orphan_grace_s=3600,
        retention_originals_days=7,
        retention_failed_days=30,
        size_class_small_max_mb=25,
        tenant_defer_s=15,
        reconcile_per_tenant_batch=20,
    )
    values.update(overrides)
    return SimpleNamespace(**values)