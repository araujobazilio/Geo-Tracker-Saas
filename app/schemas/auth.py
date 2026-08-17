"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import WorkspaceType

# --- Auth request schemas ---


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


# --- Auth response schemas ---


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceBrief(BaseModel):
    id: uuid.UUID
    name: str
    workspace_type: WorkspaceType
    role: str

    model_config = {"from_attributes": True}


class UserWithWorkspacesResponse(BaseModel):
    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime
    workspaces: list[WorkspaceBrief] = []

    model_config = {"from_attributes": True}


class CsrfResponse(BaseModel):
    csrf_token: str


class MessageResponse(BaseModel):
    message: str


# --- Workspace schemas ---


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    workspace_type: WorkspaceType = WorkspaceType.PERSONAL


class WorkspaceUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    workspace_type: WorkspaceType
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
