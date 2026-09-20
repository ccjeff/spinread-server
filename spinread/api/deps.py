"""Shared FastAPI dependencies: DB session, object store, JWT auth, ownership."""

from __future__ import annotations

import jwt
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.config import Settings, get_settings
from spinread.core.db import make_engine, make_session_factory
from spinread.core.models import User, Video
from spinread.core.security import decode_token
from spinread.core.storage import S3ObjectStore
from spinread.api.errors import forbidden, not_found, unauthorized

# Process-wide singletons (sync SQLAlchemy engine is thread-safe via pooling).
_engine = None
_session_factory = None
_s3: S3ObjectStore | None = None


def init_singletons(settings: Settings) -> None:
    global _engine, _session_factory, _s3
    _engine = make_engine(settings.db_url)
    _session_factory = make_session_factory(_engine)
    _s3 = S3ObjectStore(settings)


def get_db():
    if _session_factory is None:
        init_singletons(get_settings())
    session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_s3() -> S3ObjectStore:
    if _s3 is None:
        init_singletons(get_settings())
    return _s3  # type: ignore[return-value]


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("missing bearer token")
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise unauthorized("invalid token")
    user = db.get(User, payload.get("sub", ""))
    if user is None:
        raise unauthorized("unknown user")
    return user


def get_owned_video(video_id: str, db: Session, user: User) -> Video:
    video = db.get(Video, video_id)
    if video is None or video.deleted_at is not None:
        raise not_found("video not found")
    if video.owner_id != user.id:
        raise forbidden("not the video owner")
    return video
