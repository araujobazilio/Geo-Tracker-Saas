"""Shared SQLAlchemy type utilities (UUID primary keys, JSONB, etc.)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# PostgreSQL is the only supported database. Use native JSONB directly so
# queries against JSON columns are first-class. On non-PG dialects (only
# used incidentally in tests) SQLAlchemy will fall back via the dialect.
JSONBType: Any = JSONB()

# Re-export the PostgreSQL UUID type for use across models.
UUIDType = PG_UUID(as_uuid=True)


class StringList(TypeDecorator[list[str]]):
    """Store a Python list of strings as a JSONB array.

    Used for small, schemaless collections like brand aliases where a
    full child table would be overkill but plain text would lose structure.
    On PostgreSQL this maps to JSONB; the impl falls back to JSON on
    dialects that lack JSONB.
    """

    impl = JSONB
    cache_ok = True

    def process_bind_param(self, value: list[str] | None, dialect: object) -> list[str] | None:
        if value is None:
            return None
        return list(value)

    def process_result_value(self, value: object, dialect: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return []


__all__ = ["JSONBType", "UUIDType", "StringList", "String"]
