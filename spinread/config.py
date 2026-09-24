"""Runtime configuration (pydantic-settings, env prefix SPINREAD_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPINREAD_", env_file=".env", extra="ignore")

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
    blurball_weights: str = ""
    blurball_device: str = "auto"
    blurball_batch_size: int = 8

    max_upload_bytes: int = 4 * 1024 * 1024 * 1024  # 4 GiB
    upload_part_size: int = 16 * 1024 * 1024  # 16 MiB
    presign_expiry_seconds: int = 24 * 3600

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    demo_user_email: str = "demo@spinread.local"
    demo_user_password: str = "spinread-demo"


@lru_cache
def get_settings() -> Settings:
    return Settings()
