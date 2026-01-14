from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EduHub API"
    environment: Literal["local", "test", "staging", "production"] = "local"

    secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    database_url: str = "postgresql+asyncpg://eduhub:eduhub@localhost:5432/eduhub"
    db_pool_size: int = 5
    db_max_overflow: int = 10

    cors_origins: list[str] = []

    # AI: Bedrock's OpenAI-compatible endpoint. A Bedrock API key (ABSK...) is the bearer token.
    llm_provider: Literal["openai", "fake"] = "openai"
    openai_base_url: str = "https://bedrock-mantle.us-east-1.api.aws/v1"
    openai_api_key: str | None = None
    aws_bearer_token_bedrock: str | None = None
    llm_chat_model: str = "openai.gpt-oss-120b"
    llm_timeout_seconds: float = 60
    ai_school_monthly_token_budget: int = 2_000_000
    # One-time AI tutor trial for schools without a plan, shared by all their students.
    # Measured on Bedrock: one tutor message with tool lookups costs ~8,000 tokens, so 10,000 is
    # about one message. 50,000-100,000 gives a school a real try-out.
    ai_free_trial_tokens: int = 10_000
    ai_user_hourly_message_limit: int = 30

    # Embeddings for semantic search/RAG. "hashing" is a free, deterministic local fallback (tests, offline).
    embedding_provider: Literal["bedrock", "hashing"] = "hashing"
    aws_region: str = "us-east-1"
    bedrock_embedding_model: str = "amazon.titan-embed-text-v2:0"
    # Optional Amazon Bedrock Guardrail applied to tutor input on top of the built-in checks.
    bedrock_guardrail_id: str | None = None
    bedrock_guardrail_version: str = "DRAFT"

    # Uploaded files. "local" needs no AWS account, so dev, CI and tests use it.
    storage_provider: Literal["local", "s3"] = "local"
    local_storage_dir: str = "var/uploads"
    s3_bucket: str | None = None
    s3_region: str | None = None

    # Past-paper ingestion runs in a worker, not on the web process.
    paper_max_upload_bytes: int = 20 * 1024 * 1024
    paper_uploads_per_hour: int = 10
    # Parallel parse jobs per worker. OCR is CPU- and memory-hungry, so keep this near core count.
    worker_max_jobs: int = 4
    worker_job_timeout_seconds: int = 900
    # A paper left in "processing" this long, by a worker that died, is claimed again.
    paper_stuck_after_minutes: int = 20
    # OCR for scanned pages: Tesseract is local and free; Textract is billed per page.
    ocr_provider: Literal["tesseract", "textract"] = "tesseract"
    textract_monthly_page_budget: int = 5000

    # Server-to-server secret shared with the web app, which sends password-reset emails. It can mint
    # reset tokens, so it lives only in the two servers' environments.
    internal_api_key: str | None = None
    password_reset_ttl_minutes: int = 30
    password_resets_per_email_per_hour: int = 3
    password_resets_per_ip_per_hour: int = 10

    # Chapa payment gateway.
    chapa_secret_key: str | None = None
    chapa_webhook_secret: str | None = None
    api_public_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:3000"

    # Real-time contest leaderboards. Without Redis, clients fall back to polling.
    redis_url: str | None = None
    ws_ticket_ttl_seconds: int = 60

    @property
    def llm_api_key(self) -> str | None:
        return self.openai_api_key or self.aws_bearer_token_bedrock


@lru_cache
def get_settings() -> Settings:
    return Settings()
