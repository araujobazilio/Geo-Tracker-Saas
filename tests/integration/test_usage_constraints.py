"""Integration tests for UsageEvent non-negative CHECK constraints."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import UsageEventType, WorkspaceRole, WorkspaceType
from app.models import UsageEvent, User, Workspace, WorkspaceMember


def _make_workspace(db_session, name: str = "Usage WS") -> Workspace:  # type: ignore[no-untyped-def]
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.PERSONAL)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db_session.flush()
    return ws


@pytest.mark.integration
def test_negative_ai_checks_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=-1,
        cost_usd=Decimal("0"),
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_negative_cost_usd_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=1,
        cost_usd=Decimal("-0.001"),
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_negative_input_tokens_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=1,
        input_tokens=-10,
        cost_usd=Decimal("0"),
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_negative_output_tokens_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=1,
        output_tokens=-5,
        cost_usd=Decimal("0"),
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_negative_total_tokens_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=1,
        total_tokens=-1,
        cost_usd=Decimal("0"),
    )
    db_session.add(ev)
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_zero_values_accepted(db_session) -> None:  # type: ignore[no-untyped-def]
    """Zero is non-negative and must be accepted."""
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=0,
        cost_usd=Decimal("0"),
    )
    db_session.add(ev)
    db_session.flush()
    assert ev.id is not None


@pytest.mark.integration
def test_null_tokens_accepted(db_session) -> None:  # type: ignore[no-untyped-def]
    """Null token counts must be accepted (CHECK allows NULL)."""
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        ai_checks=1,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_usd=Decimal("0.002"),
    )
    db_session.add(ev)
    db_session.flush()
    assert ev.id is not None
