"""Integration tests for billing, AppSumo license, webhook, usage, audit."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    AppSumoLicenseStatus,
    BillingAccountStatus,
    BillingSource,
    UsageEventType,
    WebhookEventStatus,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import (
    AppSumoLicense,
    AuditLog,
    BillingAccount,
    ProviderWebhookEvent,
    UsageEvent,
    User,
    Workspace,
)
from app.models.workspace import WorkspaceMember


def _make_workspace(db_session, name: str = "Billing WS") -> Workspace:  # type: ignore[no-untyped-def]
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db_session.flush()
    return ws


@pytest.mark.integration
def test_billing_account_independent_of_user(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ba = BillingAccount(
        workspace_id=ws.id, source=BillingSource.APPSUMO, status=BillingAccountStatus.ACTIVE
    )
    db_session.add(ba)
    db_session.flush()
    assert ba.id is not None
    assert ba.source == BillingSource.APPSUMO


@pytest.mark.integration
def test_billing_source_values(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    for src in (BillingSource.APPSUMO, BillingSource.STRIPE, BillingSource.ADMIN):
        db_session.add(BillingAccount(workspace_id=ws.id, source=src))
    db_session.flush()


@pytest.mark.integration
def test_appsumo_license_relationship(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ba = BillingAccount(workspace_id=ws.id, source=BillingSource.APPSUMO)
    db_session.add(ba)
    db_session.flush()
    lic = AppSumoLicense(
        workspace_id=ws.id,
        billing_account_id=ba.id,
        external_license_id="abc-123",
        appsumo_plan="tier_2_pro",
        status=AppSumoLicenseStatus.ACTIVE,
    )
    db_session.add(lic)
    db_session.flush()
    assert lic.id is not None
    assert lic.status == AppSumoLicenseStatus.ACTIVE


@pytest.mark.integration
def test_webhook_event_idempotency(db_session) -> None:  # type: ignore[no-untyped-def]
    db_session.add(
        ProviderWebhookEvent(
            provider="appsumo",
            external_event_id="evt-1",
            event_type="purchase",
            payload={"foo": "bar"},
            status=WebhookEventStatus.RECEIVED,
        )
    )
    db_session.flush()
    # Replaying the same (provider, external_event_id) must violate uniqueness.
    db_session.add(
        ProviderWebhookEvent(
            provider="appsumo",
            external_event_id="evt-1",
            event_type="purchase",
            payload={},
            status=WebhookEventStatus.RECEIVED,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.integration
def test_webhook_event_different_providers_same_external_id(db_session) -> None:  # type: ignore[no-untyped-def]
    """Same external_event_id is allowed for different providers."""
    db_session.add(ProviderWebhookEvent(provider="appsumo", external_event_id="evt-x", payload={}))
    db_session.add(ProviderWebhookEvent(provider="stripe", external_event_id="evt-x", payload={}))
    db_session.flush()


@pytest.mark.integration
def test_usage_event_decimal_cost(db_session) -> None:  # type: ignore[no-untyped-def]
    ws = _make_workspace(db_session)
    ev = UsageEvent(
        workspace_id=ws.id,
        event_type=UsageEventType.AI_CHECK,
        provider="OPENAI",
        model="gpt-4o",
        ai_checks=1,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cost_usd=Decimal("0.002340"),
    )
    db_session.add(ev)
    db_session.flush()
    assert ev.id is not None
    assert isinstance(ev.cost_usd, Decimal)
    assert ev.cost_usd == Decimal("0.002340")


@pytest.mark.integration
def test_audit_log_persists(db_session) -> None:  # type: ignore[no-untyped-def]
    log = AuditLog(action="PROJECT_CREATED", entity_type="project", metadata_={"name": "x"})
    db_session.add(log)
    db_session.flush()
    assert log.id is not None
    assert log.action == "PROJECT_CREATED"
    assert log.metadata_ == {"name": "x"}
    assert log.created_at is not None


@pytest.mark.integration
def test_audit_log_no_user_workspace_ok(db_session) -> None:  # type: ignore[no-untyped-def]
    """AuditLog user_id / workspace_id are nullable (no FK cascade)."""
    log = AuditLog(action="USER_REGISTERED")
    db_session.add(log)
    db_session.flush()
    assert log.user_id is None
    assert log.workspace_id is None
