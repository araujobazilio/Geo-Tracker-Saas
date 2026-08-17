"""Database session / engine management.

Provides:
- `engine` — global SQLAlchemy engine (created lazily from settings)
- `SessionLocal` — session factory
- `get_db` — FastAPI dependency yielding a scoped session
- `check_database` — lightweight connectivity probe for /ready
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _build_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        echo=settings.db_echo,
        future=True,
    )


def get_engine() -> Engine:
    """Return the lazily-initialized global engine."""
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the lazily-initialized session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yield a database session and ensure cleanup."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def check_database() -> bool:
    """Return True if a single round-trip SELECT succeeds."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def reset_engine() -> None:
    """Reset cached engine/session (used in tests with overridden settings)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
