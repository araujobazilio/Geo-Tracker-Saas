"""SQLAlchemy declarative base.

All ORM models inherit from `Base`. A common timestamp mixin is provided
to keep `created_at` / `updated_at` consistent across entities.

Timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now (avoid naive server-local time)."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TimestampMixin:
    """Provides `created_at` and `updated_at` columns.

    `created_at` is server-defaulted to NOW at insert time.
    `updated_at` is server-defaulted to NOW and updated on every UPDATE.
    Both are timezone-aware UTC.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
