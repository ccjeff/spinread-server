"""Engine / session factory for the sync SQLAlchemy stack."""

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spinread.config import get_settings


def make_engine(db_url: str | None = None):
    return create_engine(db_url or get_settings().db_url, pool_pre_ping=True)


def make_session_factory(engine=None) -> sessionmaker[Session]:
    return sessionmaker(engine or make_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]):
    """Transactional scope: commit on success, rollback on exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
