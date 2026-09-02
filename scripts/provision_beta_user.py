#!/usr/bin/env python3
"""Provision a closed-beta user with a workspace and ADMIN billing grant.

Creates (or reuses) a User + Workspace + primary BillingAccount with
BillingSource.ADMIN and the specified internal plan code.

Idempotent: if the email already exists, the script reports and exits
without creating duplicates. If the workspace already has a primary
BillingAccount, it reports and exits without creating a duplicate.

Password handling:
    The script does NOT accept passwords on the command line (shell history
    risk). Instead, it generates a one-time temporary password, prints it
    to stdout, and instructs the operator to share it securely. The user
    should change it after first login.

Usage:
    python -m scripts.provision_beta_user --email user@example.com --plan-code beta_internal

Requirements:
    DATABASE_URL must point to the production database.
    APP_ENV must be production (or staging).
"""

from __future__ import annotations

import argparse
import secrets
import string
import sys

# Exit codes.
EXIT_OK = 0
EXIT_FAIL = 1


def _generate_temp_password(length: int = 20) -> str:
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
        "--password",
        default=None,
        help="Password (NOT recommended — use generated password instead)",
    )
    parser.add_argument(
        "--generate-password",
        action="store_true",
        default=True,
        help="Generate a one-time temporary password (default)",
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

    with factory() as session:
        # 1. Check if user already exists (idempotent).
        normalized = normalize_email(args.email)
        existing = session.execute(
            select(User).where(User.email == normalized)
        ).scalar_one_or_none()

        if existing is not None:
            print(f"User already exists: {normalized}")
            print(f"  User ID: {existing.id}")

            # Check if they already have a workspace with billing.
            membership = (
                session.execute(
                    select(WorkspaceMember).where(WorkspaceMember.user_id == existing.id)
                )
                .scalars()
                .first()
            )

            if membership is not None:
                billing = session.execute(
                    select(BillingAccount).where(
                        BillingAccount.workspace_id == membership.workspace_id,
                        BillingAccount.is_primary.is_(True),
                    )
                ).scalar_one_or_none()

                if billing is not None:
                    print(f"  Workspace already has primary billing: {billing.source}")
                    print(f"  Plan: {billing.plan_code}")
                    print("No changes made (idempotent).")
                    return EXIT_OK
                else:
                    # Create billing for existing workspace.
                    plan = session.execute(
                        select(PlanDefinition).where(PlanDefinition.code == args.plan_code)
                    ).scalar_one_or_none()
                    if plan is None:
                        print(f"ERROR: PlanDefinition '{args.plan_code}' not found.")
                        return EXIT_FAIL
                    if not plan.is_active:
                        print(f"ERROR: PlanDefinition '{args.plan_code}' is inactive.")
                        return EXIT_FAIL

                    billing = BillingAccount(
                        workspace_id=membership.workspace_id,
                        source=BillingSource.ADMIN,
                        status=BillingAccountStatus.ACTIVE,
                        plan_code=args.plan_code,
                        is_primary=True,
                    )
                    session.add(billing)
                    session.commit()
                    print(f"  Created ADMIN billing with plan '{args.plan_code}'.")
                    return EXIT_OK

        # 2. Verify plan exists.
        plan = session.execute(
            select(PlanDefinition).where(PlanDefinition.code == args.plan_code)
        ).scalar_one_or_none()
        if plan is None:
            print(f"ERROR: PlanDefinition '{args.plan_code}' not found.")
            return EXIT_FAIL
        if not plan.is_active:
            print(f"ERROR: PlanDefinition '{args.plan_code}' is inactive.")
            return EXIT_FAIL

        # 3. Generate or get password.
        password = args.password if args.password else _generate_temp_password()

        # 4. Create user.
        user = User(
            email=normalized,
            password_hash=hash_password(password),
            is_active=True,
            is_admin=False,
        )
        session.add(user)
        session.flush()

        # 5. Create workspace.
        workspace = Workspace(
            name=args.workspace_name,
            workspace_type=WorkspaceType.PERSONAL,
        )
        session.add(workspace)
        session.flush()

        # 6. Create membership.
        membership = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role=WorkspaceRole.OWNER,
        )
        session.add(membership)
        session.flush()

        # 7. Create ADMIN billing account.
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
