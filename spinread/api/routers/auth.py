"""Auth: POST /api/auth/login."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_db, get_current_user
from spinread.api.errors import unauthorized, ApiError
from spinread.api.schemas import LoginRequest, LoginResponse, UserOut
from spinread.core.models import User
from spinread.core.security import issue_token, verify_password, hash_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise unauthorized("invalid email or password")
    return LoginResponse(
        access_token=issue_token(user.id),
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role),
    )


from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy.exc import IntegrityError


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role)


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["USER", "COACH"] = "USER"


@router.post("/register", response_model=LoginResponse, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    # Coach is a workflow role, not a credential or access to any player.
    # Every video still requires the player's explicit relationship + consent.
    if not body.display_name.strip():
        raise ApiError(422, "INVALID_NAME", "显示名称不能为空")
    user = User(email=body.email.strip().lower(), password_hash=hash_password(body.password),
        display_name=body.display_name.strip(), role=body.role)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "EMAIL_EXISTS", "该邮箱已注册")
    return LoginResponse(access_token=issue_token(user.id),
        user=UserOut(id=user.id, email=user.email, display_name=user.display_name, role=user.role))
