"""Runtime configuration (pydantic-settings, env prefix SPINREAD_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SPINREAD_", env_file=".env", extra="ignore", hide_input_in_errors=True)

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
    runtime_environment: str = "local"
    bootstrap_storage: bool = True
    seed_demo: bool = True
    remote_provider: str = "disabled"  # disabled | local (tests only) | modal
    remote_app: str = "rallyloop-staging-na-compute"
    remote_timeout_seconds: int = Field(default=7200, ge=60, le=7200)
    remote_poll_seconds: float = Field(default=5, ge=0.01, le=60)
    remote_max_bundle_bytes: int = Field(default=2 * 1024**3, ge=1024, le=4 * 1024**3)
    remote_replay_engines: list[str] = []  # Enable only after model preflight.
    remote_vision_enabled: bool = False
    remote_model_revision: str = ""

    @model_validator(mode="after")
    def cloud_safety(self):
        if self.remote_provider not in {"disabled", "local", "modal"}:
            raise ValueError("unsupported remote provider")
        if set(self.remote_replay_engines) - {"local", "nlf", "pingpong3d"}:
            raise ValueError("unsupported replay engine")
        if (self.remote_replay_engines or self.remote_vision_enabled) and not self.remote_model_revision:
            raise ValueError("remote models require an explicit versioned manifest hash")
        if self.runtime_environment not in {"local", "staging", "production"}:
            raise ValueError("unsupported runtime environment")
        if self.runtime_environment != "local":
            from urllib.parse import urlsplit, parse_qs
            if self.embed_worker or self.bootstrap_storage or self.seed_demo:
                raise ValueError("cloud requires embedded worker, bucket bootstrap and demo seed disabled")
            if self.remote_provider == "local":
                raise ValueError("local compute provider is forbidden in cloud")
            if len(self.jwt_secret) < 32 or self.jwt_secret.startswith("dev-only"):
                raise ValueError("cloud requires a unique JWT secret of at least 32 characters")
            if (urlsplit(self.s3_endpoint).scheme != "https" or not self.s3_secret_key
                    or self.s3_secret_key == "minioadmin" or self.s3_access_key == "minioadmin"):
                raise ValueError("cloud requires HTTPS object storage and non-default credentials")
            if urlsplit(self.db_url).hostname in {None, "localhost", "127.0.0.1"}:
                raise ValueError("cloud requires an explicit database endpoint")
            if parse_qs(urlsplit(self.db_url).query).get("sslmode", [None])[0] not in {"require", "verify-full"}:
                raise ValueError("cloud requires database TLS")
            if not self.cors_origins or any(not v.startswith("https://") for v in self.cors_origins):
                raise ValueError("cloud requires explicit HTTPS browser origins")
        return self

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
