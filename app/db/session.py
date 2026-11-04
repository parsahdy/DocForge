"""Engine and session handling. The engine is created lazily, once per process.

Lazy creation matters for Celery: worker processes are forked, and a database
connection must never be shared between a parent and its children.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        if settings.database_url.startswith("sqlite"):
            _engine = create_engine(
                settings.database_url, connect_args={"check_same_thread": False}
            )
        else:
            _engine = create_engine(
                settings.database_url,
                pool_pre_ping=True,
                pool_size = settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
            )

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _factory


def reset_engine() -> None:
    """
    Forget the engine (used after a fork, and in tests.)
    """
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """
    mmit on success, roll back on error. Used by the worker.
    """
    session = get_session_factory()()
    try:
        yield Session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """
    FastAPI dependency. No automatic commit: the API commits explicitly, step by step.
    """
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()