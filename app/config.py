from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_ENV: str = "dev"                        # dev | staging | prod — gates safety fallbacks

    DATABASE_URL: str
    ANTHROPIC_API_KEY: str = ""
    STORAGE_BACKEND: str = "local"
    STORAGE_LOCAL_BASE_DIR: str = "./so_pdfs"
    QUEUE_BACKEND: str = "memory"
    POPPLER_PATH: str | None = None
    SYSTEM_USER_ID: int = 0
    MAX_PDF_SIZE_MB: int = 20
    EXTRACTION_MAX_RETRIES: int = 3
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    INTERNAL_WEBHOOK_TOKEN: str = ""
    WS_TOKEN_SECRET: str = ""
    WS_TOKEN_EXPIRY_MINUTES: int = 5

    # ── Auth (JWT + lockout + rate limit) ─────────────────────────────────
    AUTH_ENCRYPTION_KEY: str = ""               # Fernet key for legacy password encryption; also dev fallback for JWT_SECRET
    JWT_SECRET: str = ""                        # required in non-dev; dev may derive from AUTH_ENCRYPTION_KEY
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "candor-consumption"
    ACCESS_TOKEN_TTL_SECONDS: int = 900         # 15 min
    REFRESH_TOKEN_TTL_SECONDS: int = 28800      # 8 h
    LOGIN_RATE_LIMIT_MAX: int = 10              # attempts per window per (ip, phone)
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: int = 60
    LOGIN_LOCKOUT_THRESHOLD: int = 5            # failed_login_count → lock
    LOGIN_LOCKOUT_MINUTES: int = 15

    # ── SMTP (notification mail; best-effort) ─────────────────────────────
    SMTP_HOST: str = ""                         # empty → mail send is a no-op
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SMTP_FROM: str = "noreply@candorfoods.in"

    # ── Vendor module (S3-only + Claude extraction) ───────────────────────
    VENDOR_S3_BUCKET: str = ""                  # required for vendor doc uploads; falls back to RECEIPT_S3_BUCKET
    VENDOR_DOC_MODEL: str = "claude-sonnet-4-6"  # Claude model used for vendor document extraction

    # ── AWS credentials (read from .env when not present in shell env) ────
    # pydantic-settings reads these from `.env` into the Settings instance;
    # boto3 does NOT consult `.env`, only os.environ. We read them here and
    # pass them explicitly into boto3.client(...) so .env-only deployments
    # work without exporting the keys in the shell.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""                 # optional, for assumed-role credentials
    AWS_DEFAULT_REGION: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}

    @field_validator("APP_ENV")
    @classmethod
    def _normalise_env(cls, v: str) -> str:
        v = (v or "dev").strip().lower()
        if v not in {"dev", "staging", "prod"}:
            raise ValueError(f"APP_ENV must be dev|staging|prod, got {v!r}")
        return v

    @model_validator(mode="after")
    def _require_jwt_secret_in_prod(self):
        # CR-02: outside dev, JWT_SECRET MUST be set explicitly. Refuse to
        # boot rather than silently signing tokens with a derived key.
        if self.APP_ENV != "dev" and not self.JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is required when APP_ENV != 'dev'. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
            )
        return self
