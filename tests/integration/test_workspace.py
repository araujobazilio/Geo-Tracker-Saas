"""Integration tests for User, Workspace, and membership models."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import WorkspaceRole, WorkspaceType
from app.models import User, Workspace, WorkspaceMember


@pytest.mark.integration
def test_create_user(db_session) -> None:  # type: ignore[no-untyped-def]
    user = User(email="alice@example.com", password_hash="hashed")
    db_session.add(user)
    db_session.flush()
    assert user.id is not None
    assert user.is_active is True
    assert user.is_admin is False
    assert user.created_at is not None


@pytest.mark.integration
def test_user_email_unique(db_session) -> None:  # type: ignore[no-untyped-def]
    db_session.add(User(email="bob@example.com", password_hash="h1"))
    db_session.flush()
    db_session.add(User(email="bob@example.com", password_hash="h2"))
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_create_workspace(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = Workspace(name="Acme Agency", workspace_type=WorkspaceType.AGENCY)
    db_session.add(ws)
    db_session.flush()
    assert ws.id is not None
    assert ws.workspace_type == WorkspaceType.AGENCY


@pytest.mark.integration
def test_workspace_membership(db_session) -> None:  # type: ignore[no-untyped-def]
    user = User(email="carol@example.com", password_hash="h")
    ws = Workspace(name="Carol WS", workspace_type=WorkspaceType.PERSONAL)
    db_session.add_all([user, ws])
    db_session.flush()
    m = WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER)
    db_session.add(m)
    db_session.flush()
    assert m.id is not None
    assert m.role == WorkspaceRole.OWNER


@pytest.mark.integration
def test_duplicate_membership_prevented(db_session) -> None:  # type: ignore[no-untyped-def]
    user = User(email="dave@example.com", password_hash="h")
    ws = Workspace(name="Dave WS", workspace_type=WorkspaceType.PERSONAL)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.MEMBER))
    with pytest.raises(IntegrityError):
        db_session.flush()
