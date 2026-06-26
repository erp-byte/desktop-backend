from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# Resolve .env against the project root, not the process CWD. Without this,
# pydantic-settings would treat env_file=".env" as relative to wherever
# `uvicorn` was launched from — so a fallback `Settings()` constructed
# mid-request (e.g. inside jwt_service._secret) silently fails to read .env
# and the model validator raises "DATABASE_URL: Field required". The
# computation runs at import time so the absolute path is baked in.
_ENV_PATH = str(Path(__file__).resolve().parent.parent / ".env")


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
    # Operator-stated: lock duration is 5 minutes. Auto-unlock fires
    # at the natural expiry of locked_until — the login gate checks
    # `locked_until > now` and treats a past timestamp as unlocked, so
    # the next login attempt 5+ min after lockout succeeds with no
    # admin intervention. (See auth_service.login:237-252 + 266-276
    # for the check + the success-path clear of the lockout fields.)
    LOGIN_LOCKOUT_MINUTES: int = 5

    # ── SMTP (notification mail; best-effort) ─────────────────────────────
    SMTP_HOST: str = ""                         # empty → mail send is a no-op
    SMTP_PORT: int = 587
    SMTP_EMAIL: str = ""                        # sender address + STARTTLS login (Gmail app password)
    SMTP_APP_PASSWORD: str = ""                 # app password for SMTP_EMAIL
    PUBLIC_BACKEND_URL: str = "http://65.0.86.156"          # base for email action links — hits the FastAPI backend directly (email clicks are top-level navigations, so plain HTTP is fine, not mixed-content)
    WEB_APP_URL: str = "https://erpcf.in"                    # web app base for the Hold / Reject redirects

    # ── Vendor module (S3-only + Claude extraction) ───────────────────────
    VENDOR_S3_BUCKET: str = ""                  # required for vendor doc uploads; falls back to RECEIPT_S3_BUCKET
    VENDOR_DOC_MODEL: str = "claude-sonnet-4-6"  # Claude model used for vendor document extraction

    # ── WhatsApp Cloud API (password-reset OTP delivery) ─────────────────
    # `otp_service` + `whatsapp_service` read these via os.environ at call time
    # so an ops flip of WHATSAPP_ENABLED takes effect without a restart. NOTE:
    # pydantic-settings loads .env into THIS object, NOT into os.environ — so the
    # lifespan in app/main.py copies these creds from the Settings instance into
    # os.environ at startup (a real shell / `--env-file` value wins). Without that
    # hydration the os.environ reads would be blank and sending would no-op.
    # Defaults mirror the working vms_referrence integration on the same WABA
    # (`visitor_revisit_otp` / `en_US` / Graph v21.0).
    WHATSAPP_ENABLED: bool = True
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_OTP_TEMPLATE_NAME: str = "visitor_revisit_otp"
    WHATSAPP_OTP_LANG: str = "en_US"
    WHATSAPP_GRAPH_BASE: str = "https://graph.facebook.com/v21.0"
    WHATSAPP_INTIMATION_TEMPLATE_NAME: str = "qc_inward_intimation"
    WHATSAPP_INTIMATION_LANG: str = "en"

    # ── AWS credentials (read from .env when not present in shell env) ────
    # pydantic-settings reads these from `.env` into the Settings instance;
    # boto3 does NOT consult `.env`, only os.environ. We read them here and
    # pass them explicitly into boto3.client(...) so .env-only deployments
    # work without exporting the keys in the shell.
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_SESSION_TOKEN: str = ""                 # optional, for assumed-role credentials
    AWS_DEFAULT_REGION: str = ""

    model_config = {"env_file": _ENV_PATH, "extra": "ignore"}

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
