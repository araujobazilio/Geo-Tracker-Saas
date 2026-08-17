"""Workspace and WorkspaceMember models."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import WorkspaceRole, WorkspaceType
from app.db.base import Base, TimestampMixin
from app.db.mixins import UUIDPrimaryKey
from app.db.types import UUIDType


class Workspace(UUIDPrimaryKey, TimestampMixin, Base):
    """Tenant boundary. A Workspace owns Projects and memberships."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    workspace_type: Mapped[WorkspaceType] = mapped_column(
        String(20), nullable=False, default=WorkspaceType.PERSONAL
    )

    members: Mapped[list[WorkspaceMember]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Workspace id={self.id} name={self.name!r}>"


class WorkspaceMember(UUIDPrimaryKey, TimestampMixin, Base):
    """Membership linking a User to a Workspace with a role."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member_workspace_user"),
    )

    workspace_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[WorkspaceRole] = mapped_column(
        String(20), nullable=False, default=WorkspaceRole.MEMBER
    )

    workspace: Mapped[Workspace] = relationship(back_populates="members")

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<WorkspaceMember workspace={self.workspace_id} user={self.user_id} role={self.role}>"
        )
