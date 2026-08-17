"""Reusable ORM mixins."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import UUIDType


class UUIDPrimaryKey:
    """UUID primary key (Python-generated uuid4 on insert).

    Using a Python-side default keeps the model dialect-agnostic and makes
    the id available on the object immediately after insert (no extra
    round-trip to fetch a server-generated value).
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
