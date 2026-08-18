"""Plan definition and plan-provider repositories."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.plan_definition import PlanDefinition
from app.models.plan_provider import PlanProvider


class PlanRepository:
    """Persistence layer for PlanDefinition entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_code(self, code: str) -> PlanDefinition | None:
        return self._session.execute(
            select(PlanDefinition).where(PlanDefinition.code == code)
        ).scalar_one_or_none()

    def get_by_id(self, plan_id: uuid.UUID) -> PlanDefinition | None:
        return self._session.get(PlanDefinition, plan_id)

    def create(self, plan: PlanDefinition) -> PlanDefinition:
        self._session.add(plan)
        self._session.flush()
        return plan


class PlanProviderRepository:
    """Persistence layer for PlanProvider entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_for_plan(self, plan_id: uuid.UUID) -> list[PlanProvider]:
        return list(
            self._session.execute(select(PlanProvider).where(PlanProvider.plan_id == plan_id))
            .scalars()
            .all()
        )

    def create(self, provider: PlanProvider) -> PlanProvider:
        self._session.add(provider)
        self._session.flush()
        return provider
