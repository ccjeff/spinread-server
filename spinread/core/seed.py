"""Seed data: idempotent demo user."""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.config import get_settings
from spinread.core.models import User
from spinread.core.security import hash_password

log = logging.getLogger(__name__)


def seed_demo_user(session: Session) -> User:
    s = get_settings()
    user = session.scalar(select(User).where(User.email == s.demo_user_email))
    if user is not None:
        return user
    user = User(
        email=s.demo_user_email,
        password_hash=hash_password(s.demo_user_password),
        display_name="Demo Player",
        role="USER",
    )
    session.add(user)
    session.flush()
    log.info("seeded demo user %s", s.demo_user_email)
    return user
