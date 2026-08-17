"""Integration tests for AppSumo license uniqueness constraint."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    AppSumoLicenseStatus,
    BillingSource,
    WorkspaceRole,
    WorkspaceType,
)
from app.models import AppSumoLicense, BillingAccount, User, Workspace, WorkspaceMember


def _make_workspace(db_session, name: str = "AppSumo Uniq WS") -> Workspace:  # type: ignore[no-untyped-def]
    user = User(email=f"{name.lower().replace(' ', '.')}@example.com", password_hash="h")
    ws = Workspace(name=name, workspace_type=WorkspaceType.AGENCY)
    db_session.add_all([user, ws])
    db_session.flush()
    db_session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=WorkspaceRole.OWNER))
    db_session.flush()
    return ws


@pytest.mark.integration
def test_duplicate_external_license_id_rejected(db_session) -> None:  # type: ignore[no-untyped-def]
    """Two AppSumo licenses with the same external_license_id must be rejected."""
    ws = _make_workspace(db_session)
    ba = BillingAccount(workspace_id=ws.id, source=BillingSource.APPSUMO)
    db_session.add(ba)
    db_session.flush()

    db_session.add(
        AppSumoLicense(
            workspace_id=ws.id,
            billing_account_id=ba.id,
            external_license_id="license-dup-001",
            status=AppSumoLicenseStatus.ACTIVE,
        )
    )
    db_session.flush()

    db_session.add(
        AppSumoLicense(
            workspace_id=ws.id,
            billing_account_id=ba.id,
            external_license_id="license-dup-001",
            status=AppSumoLicenseStatus.ACTIVE,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
