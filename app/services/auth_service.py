"""Authentication service — register, login, logout.

Handles:
  - email normalization + validation
  - password policy validation
  - Argon2id hashing + rehash on login
  - atomic registration (User + Workspace + WorkspaceMember)
  - session creation/revocation via SessionService
  - audit event recording via AuditService
  - timing-safe login (dummy hash for nonexistent users)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.enums import WorkspaceRole, WorkspaceType
from app.core.exceptions import AuthenticationError, ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.security import (
    check_needs_rehash,
    hash_password,
    normalize_email,
    validate_email_format,
    validate_password,
    verify_password,
)
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.repositories.user_repository import UserRepository
from app.services.audit_service import AuditService
from app.services.session_service import SessionData, SessionService

logger = get_logger("app.auth")

# A dummy hash used for timing-attack mitigation on nonexistent users.
# Generated once at import time so we always have a valid Argon2id hash
# to verify against, keeping login timing roughly constant.
_DUMMY_HASH = hash_password("dummy-password-for-timing-mitigation")


@dataclass
class AuthResult:
    """Result of a successful authentication operation."""

    user: User
    session_token: str
    session: SessionData


class AuthService:
    """Authentication business logic."""

    def __init__(
        self,
        session: Session,
        session_service: SessionService,
        audit_service: AuditService | None = None,
    ) -> None:
        self._session = session
        self._user_repo = UserRepository(session)
        self._session_service = session_service
        self._audit = audit_service or AuditService()

    def register(self, email: str, password: str) -> AuthResult:
        """Register a new user with a default personal workspace.

        Atomic: if workspace or membership creation fails, the user
        creation is rolled back.
        """
        normalized = normalize_email(email)
        if not validate_email_format(normalized):
            raise ValidationError("Invalid email address.")
        if not validate_password(password):
            raise ValidationError("Password must be between 12 and 128 characters.")
        if self._user_repo.email_exists(normalized):
            raise ConflictError("An account with this email already exists.")

        user = User(
            email=normalized,
            password_hash=hash_password(password),
            is_active=True,
            is_admin=False,
        )
        self._user_repo.create(user)

        workspace = Workspace(
            name="My Workspace",
            workspace_type=WorkspaceType.PERSONAL,
        )
        self._session.add(workspace)
        self._session.flush()

        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        self._session.add(membership)
        self._session.flush()
        self._session.commit()

        token, sess = self._session_service.create_session(str(user.id))
        self._audit.record(
            action="USER_REGISTERED",
            user_id=user.id,
            workspace_id=workspace.id,
            entity_type="user",
            entity_id=user.id,
        )
        return AuthResult(user=user, session_token=token, session=sess)

    def login(self, email: str, password: str) -> AuthResult:
        """Authenticate a user and create a new session.

        Returns generic error for nonexistent user or wrong password.
        Performs a dummy hash verification for nonexistent users to
        reduce timing side-channels.
        """
        normalized = normalize_email(email)
        user = self._user_repo.get_by_email(normalized)

        if user is None:
            # Dummy verify to keep timing roughly constant.
            verify_password(password, _DUMMY_HASH)
            self._audit.record(action="LOGIN_FAILED", metadata={"email": normalized})
            raise AuthenticationError("Invalid email or password.")

        if not verify_password(password, user.password_hash):
            self._audit.record(
                action="LOGIN_FAILED",
                user_id=user.id,
                metadata={"email": normalized},
            )
            raise AuthenticationError("Invalid email or password.")

        if not user.is_active:
            self._audit.record(
                action="LOGIN_FAILED",
                user_id=user.id,
                metadata={"reason": "inactive"},
            )
            raise AuthenticationError("Invalid email or password.")

        # Rehash if Argon2 parameters are outdated.
        if check_needs_rehash(user.password_hash):
            self._user_repo.update_password_hash(user.id, hash_password(password))
            self._session.commit()

        token, sess = self._session_service.create_session(str(user.id))
        self._audit.record(
            action="LOGIN_SUCCEEDED",
            user_id=user.id,
        )
        return AuthResult(user=user, session_token=token, session=sess)

    def logout(self, session_token: str, user_id: uuid.UUID | None = None) -> None:
        """Revoke a session and record the audit event. Idempotent."""
        self._session_service.revoke_session(session_token)
        if user_id is not None:
            self._audit.record(
                action="LOGOUT",
                user_id=user_id,
            )
