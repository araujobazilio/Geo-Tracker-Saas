#!/usr/bin/env python3
"""Provision a closed-beta user with a workspace and ADMIN billing grant.

Creates (or reuses) a User + Workspace + primary BillingAccount with
BillingSource.ADMIN and the specified internal plan code.

Idempotency cases:
    A. User does not exist → create user + workspace + OWNER membership + ADMIN billing.
    B. User exists, owns one workspace with the requested ADMIN plan already active
       → exact no-op success.
    C. User exists, has one workspace but no primary billing
       → create ADMIN billing safely.
    D. User exists, has no workspace → create workspace + membership + billing
       (DO NOT create a duplicate User).
    E. User belongs to multiple workspaces and target is ambiguous
       → fail safely, require --workspace-id.

Password handling:
    The script does NOT accept passwords on the command line (shell history
    risk). It generates a cryptographically secure one-time temporary
    credential, prints it once to stdout, and instructs the operator to
    share it securely. The user should change it after first login.

Usage:
    python -m scripts.provision_beta_user --email user@example.com --plan-code beta_internal
    python -m scripts.provision_beta_user --email user@example.com --plan-code beta_internal --workspace-id <uuid>

Requirements:
    DATABASE_URL must point to the production database.
    APP_ENV must be production (or staging).
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys
import uuid

# Exit codes.
EXIT_OK = 0
EXIT_FAIL = 1


def _generate_temp_password(length: int = 24) -> str:
    """Generate a cryptographically secure temporary password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision a closed-beta user with ADMIN billing.")
    parser.add_argument("--email", required=True, help="User email address")
    parser.add_argument(
        "--plan-code",
        required=True,
        help="Internal PlanDefinition code (e.g. beta_internal)",
    )
    parser.add_argument(
        "--workspace-name",
        default="My Workspace",
        help="Workspace name (default: My Workspace)",
    )
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Explicit workspace UUID for multi-workspace users (required if ambiguous)",
    )
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from app.core.enums import BillingAccountStatus, BillingSource, WorkspaceRole, WorkspaceType
    from app.core.security import hash_password, normalize_email
    from app.db.session import get_session_factory
    from app.models.billing import BillingAccount
    from app.models.plan_definition import PlanDefinition
    from app.models.user import User
    from app.models.workspace import Workspace
    from app.models.workspace_member import WorkspaceMember

    factory = get_session_factory()
    normalized = normalize_email(args.email)

    # Parse --workspace-id if provided.
    target_workspace_id: uuid.UUID | None = None
    if args.workspace_id:
        try:
            target_workspace_id = uuid.UUID(args.workspace_id)
        except ValueError:
            print(f"ERROR: --workspace-id is not a valid UUID: {args.workspace_id}")
            return EXIT_FAIL

    # Use a single transaction for atomicity.
    with factory() as session:
        # 1. Verify plan exists and is active.
        plan = session.execute(
            select(PlanDefinition).where(PlanDefinition.code == args.plan_code)
        ).scalar_one_or_none()
        if plan is None:
            print(f"ERROR: PlanDefinition '{args.plan_code}' not found.")
            return EXIT_FAIL
        if not plan.is_active:
            print(f"ERROR: PlanDefinition '{args.plan_code}' is inactive.")
            return EXIT_FAIL

        # 2. Check if user already exists.
        existing = session.execute(
            select(User).where(User.email == normalized)
        ).scalar_one_or_none()

        if existing is not None:
            # User exists — find their workspaces.
            memberships = (
                session.execute(
                    select(WorkspaceMember).where(WorkspaceMember.user_id == existing.id)
                )
                .scalars()
                .all()
            )

            if not memberships:
                # CASE D: User exists, no workspace → create workspace + membership + billing.
                password = _generate_temp_password()
                # Update password for existing user (one-time credential).
                existing.password_hash = hash_password(password)
                session.flush()

                workspace = Workspace(
                    name=args.workspace_name,
                    workspace_type=WorkspaceType.PERSONAL,
                )
                session.add(workspace)
                session.flush()

                membership = WorkspaceMember(
                    workspace_id=workspace.id,
                    user_id=existing.id,
                    role=WorkspaceRole.OWNER,
                )
                session.add(membership)
                session.flush()

                billing = BillingAccount(
                    workspace_id=workspace.id,
                    source=BillingSource.ADMIN,
                    status=BillingAccountStatus.ACTIVE,
                    plan_code=args.plan_code,
                    is_primary=True,
                )
                session.add(billing)
                session.commit()

                print("=" * 60)
                print("Beta user provisioned (existing user, new workspace).")
                print("=" * 60)
                print(f"  Email:      {normalized}")
                print(f"  User ID:    {existing.id}")
                print(f"  Workspace:  {workspace.name} ({workspace.id})")
                print(f"  Plan:       {args.plan_code}")
                print("  Billing:    ADMIN (complimentary)")
                print()
                print("  Temporary password (share securely, user should change it):")
                print(f"    {password}")
                print("=" * 60)
                return EXIT_OK

            # User has one or more workspaces.
            if target_workspace_id is not None:
                # Validate the user is a member of the target workspace.
                target_membership = next(
                    (m for m in memberships if m.workspace_id == target_workspace_id),
                    None,
                )
                if target_membership is None:
                    print(
                        f"ERROR: User {normalized} is not a member of "
                        f"workspace {target_workspace_id}."
                    )
                    return EXIT_FAIL

                # Check if target workspace already has primary billing with this plan.
                billing = session.execute(
                    select(BillingAccount).where(
                        BillingAccount.workspace_id == target_workspace_id,
                        BillingAccount.is_primary.is_(True),
                    )
                ).scalar_one_or_none()

                if billing is not None:
                    if (
                        billing.source == BillingSource.ADMIN
                        and billing.plan_code == args.plan_code
                    ):
                        # CASE B: exact no-op.
                        print(f"User already provisioned: {normalized}")
                        print(f"  User ID: {existing.id}")
                        print(f"  Workspace: {target_workspace_id}")
                        print(f"  Plan: {billing.plan_code}")
                        print("No changes made (idempotent).")
                        return EXIT_OK
                    print(
                        f"ERROR: Workspace {target_workspace_id} already has "
                        f"primary billing (source={billing.source}, "
                        f"plan={billing.plan_code})."
                    )
                    return EXIT_FAIL

                # CASE C: Create billing for the specified workspace.
                billing = BillingAccount(
                    workspace_id=target_workspace_id,
                    source=BillingSource.ADMIN,
                    status=BillingAccountStatus.ACTIVE,
                    plan_code=args.plan_code,
                    is_primary=True,
                )
                session.add(billing)
                session.commit()
                print(f"Created ADMIN billing for existing user: {normalized}")
                print(f"  User ID: {existing.id}")
                print(f"  Workspace: {target_workspace_id}")
                print(f"  Plan: {args.plan_code}")
                return EXIT_OK

            # No --workspace-id specified.
            if len(memberships) == 1:
                # Single workspace — check billing.
                ws_id = memberships[0].workspace_id
                billing = session.execute(
                    select(BillingAccount).where(
                        BillingAccount.workspace_id == ws_id,
                        BillingAccount.is_primary.is_(True),
                    )
                ).scalar_one_or_none()

                if billing is not None:
                    if (
                        billing.source == BillingSource.ADMIN
                        and billing.plan_code == args.plan_code
                    ):
                        # CASE B: exact no-op.
                        print(f"User already provisioned: {normalized}")
                        print(f"  User ID: {existing.id}")
                        print(f"  Workspace: {ws_id}")
                        print(f"  Plan: {billing.plan_code}")
                        print("No changes made (idempotent).")
                        return EXIT_OK
                    print(
                        f"ERROR: Workspace {ws_id} already has primary billing "
                        f"(source={billing.source}, plan={billing.plan_code})."
                    )
                    return EXIT_FAIL

                # CASE C: Create billing for the single workspace.
                billing = BillingAccount(
                    workspace_id=ws_id,
                    source=BillingSource.ADMIN,
                    status=BillingAccountStatus.ACTIVE,
                    plan_code=args.plan_code,
                    is_primary=True,
                )
                session.add(billing)
                session.commit()
                print(f"Created ADMIN billing for existing user: {normalized}")
                print(f"  User ID: {existing.id}")
                print(f"  Workspace: {ws_id}")
                print(f"  Plan: {args.plan_code}")
                return EXIT_OK

            # CASE E: Multiple workspaces, ambiguous.
            ws_ids = [str(m.workspace_id) for m in memberships]
            print(
                f"ERROR: User {normalized} belongs to multiple workspaces: "
                f"{', '.join(ws_ids)}. "
                f"Specify --workspace-id to select the target."
            )
            return EXIT_FAIL

        # CASE A: User does not exist → create everything.
        password = _generate_temp_password()
        user = User(
            email=normalized,
            password_hash=hash_password(password),
            is_active=True,
            is_admin=False,
        )
        session.add(user)
        session.flush()

        workspace = Workspace(
            name=args.workspace_name,
            workspace_type=WorkspaceType.PERSONAL,
        )
        session.add(workspace)
        session.flush()

        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)
        session.flush()

        billing = BillingAccount(
            workspace_id=workspace.id,
            source=BillingSource.ADMIN,
            status=BillingAccountStatus.ACTIVE,
            plan_code=args.plan_code,
            is_primary=True,
        )
        session.add(billing)
        session.commit()

        print("=" * 60)
        print("Beta user provisioned successfully.")
        print("=" * 60)
        print(f"  Email:      {normalized}")
        print(f"  User ID:    {user.id}")
        print(f"  Workspace:  {workspace.name} ({workspace.id})")
        print(f"  Plan:       {args.plan_code}")
        print("  Billing:    ADMIN (complimentary)")
        print()
        print("  Temporary password (share securely, user should change it):")
        print(f"    {password}")
        print("=" * 60)
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
