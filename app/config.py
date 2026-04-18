from pydantic_settings import BaseSettings


class Settings(BaseSettings):
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

    model_config = {"env_file": ".env", "extra": "ignore"}
