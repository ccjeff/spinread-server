"""Auth: POST /api/auth/login."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_db
from spinread.api.errors import unauthorized
from spinread.api.schemas import LoginRequest, LoginResponse, UserOut
from spinread.core.models import User
from spinread.core.security import issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise unauthorized("invalid email or password")
    return LoginResponse(
        access_token=issue_token(user.id),
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name),
    )
