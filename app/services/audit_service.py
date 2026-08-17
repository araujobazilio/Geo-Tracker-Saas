"""Audit service — centralized audit event recording.

AuditService.record() is the single entry point for writing audit logs.
Routers/services should never instantiate AuditLog directly.

Transaction behavior:
    AuditService.record() uses its own session and commits independently.
    This means audit logging failure does NOT roll back the core business
    transaction. If audit logging fails, the error is logged but does not
    propagate — core operations must still succeed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.db.session import get_session_factory
from app.models.audit import AuditLog

logger = get_logger("app.audit")


class AuditService:
    """Centralized audit event recording."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._factory = session_factory

    @property
    def factory(self) -> sessionmaker[Session]:
        if self._factory is None:
            self._factory = get_session_factory()
        return self._factory

    def record(
        self,
        *,
        action: str,
        user_id: uuid.UUID | None = None,
        workspace_id: uuid.UUID | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record an audit event in a separate session.

        Never stores passwords, session tokens, CSRF tokens, or secrets.
        Failures are logged but do not propagate.
        """
        try:
            session = self.factory()
            try:
                log = AuditLog(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    metadata_=metadata or {},
                )
                session.add(log)
                session.commit()
            finally:
                session.close()
        except Exception:
            logger.error("audit_record_failed", action=action)
