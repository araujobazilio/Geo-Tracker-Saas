"""User repository — persistence/query concerns for User."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User


class UserRepository:
    """Persistence layer for User entities."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return self._session.execute(stmt).scalar_one_or_none()

    def email_exists(self, email: str) -> bool:
        return self.get_by_email(email) is not None

    def create(self, user: User) -> User:
        self._session.add(user)
        self._session.flush()
        return user

    def update_password_hash(self, user_id: uuid.UUID, password_hash: str) -> None:
        user = self.get_by_id(user_id)
        if user is not None:
            user.password_hash = password_hash
            self._session.flush()
