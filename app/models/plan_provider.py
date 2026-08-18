"""Plan-provider association model.

Defines which AI providers are allowed for a given plan. An empty
provider set means NO providers are allowed — it must NOT implicitly
mean "all providers".
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import LLMProvider
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.plan_definition import PlanDefinition


class PlanProvider(UUIDPrimaryKey, TimestampMixin, Base):
    """Allowed AI provider for a plan.

    Unique constraint on (plan_id, provider) prevents duplicates.
    An empty set of PlanProvider rows for a plan means no providers
    are allowed.
    """

    __tablename__ = "plan_providers"
    __table_args__ = (
        UniqueConstraint("plan_id", "provider", name="uq_plan_providers_plan_provider"),
    )

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("plan_definitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[LLMProvider] = mapped_column(String(30), nullable=False)

    plan: Mapped[PlanDefinition] = relationship(back_populates="providers")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PlanProvider plan={self.plan_id} provider={self.provider}>"
