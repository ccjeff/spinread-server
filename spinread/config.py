"""Runtime configuration (pydantic-settings, env prefix SPINREAD_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPINREAD_", env_file=".env", extra="ignore")

    openai_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("SPINREAD_OPENAI_API_KEY", "OPENAI_API_KEY"))

    db_url: str = "postgresql+psycopg://spinread:spinread@localhost:5432/spinread"

    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "spinread-media"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"

    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_ttl_seconds: int = 12 * 3600

    # When true the api process runs the worker claim loop in a daemon thread.
    embed_worker: bool = True

    tmp_dir: str = "/tmp/spinread"

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # Explicit opt-in; an invalid configured checkpoint fails rather than silently
    # falling back to acoustic labels. Install pingpong-training[vision].
    pose_weights: str = ""
    pose_fps: int = 12

    blurball_weights: str = ""
    blurball_device: str = "auto"
    blurball_batch_size: int = 8

    # Optional private upstream checkout. No models or upstream source are bundled.
    # This bridge runs local GPU work only; it never provisions cloud instances.
    pingpong3d_repo: str = ""
    pingpong3d_python: str = ""
    pingpong3d_model_dir: str = ""
    pingpong3d_timeout_seconds: int = Field(default=7200, ge=30, le=86400)
    pingpong3d_max_rallies: int = Field(default=80, ge=1, le=200)
    nlf_weights: str = ""
    nlf_faces: str = ""
    nlf_fps: int = Field(default=12, ge=1, le=30)
    nlf_batch_size: int = Field(default=4, ge=1, le=16)

    max_upload_bytes: int = 4 * 1024 * 1024 * 1024  # 4 GiB
    upload_part_size: int = 16 * 1024 * 1024  # 16 MiB
    presign_expiry_seconds: int = 24 * 3600

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    demo_user_email: str = "demo@spinread.local"
    demo_user_password: str = "spinread-demo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
