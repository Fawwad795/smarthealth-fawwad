"""Application settings, loaded from environment variables.

Nothing in the codebase reads os.environ directly. Everything goes through
the single `settings` object below, so there is exactly one place that
knows how the app is configured.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings fields are added as each week needs them."""

    model_config = SettingsConfigDict(
        env_file=".env",
        # .env holds keys for Weeks 2-5 (Temporal, Kafka, the LLM) that this
        # class does not declare yet. Ignore them instead of crashing.
        extra="ignore",
    )

    # --- App ---
    app_env: str = "local"
    log_level: str = "INFO"

    # --- Database ---
    # Host is `postgres` (the compose service name), not `localhost`,
    # because the API runs inside a container on the compose network.
    database_url: str

    # --- Tests ---
    # A separate database, dropped and recreated by the test suite on every
    # run. It must never be the development database: the fixtures issue
    # DROP DATABASE, and conftest refuses to start if the two match.
    test_database_url: str = "postgresql+psycopg://app:app@postgres:5432/app_test"

    # --- Redis ---
    redis_url: str
    # A separate Redis DB index, flushed by the test suite before/after
    # every run. Same reasoning as test_database_url: tests must never
    # share state with the app's real one.
    test_redis_url: str = "redis://redis:6379/15"

    # --- Auth ---
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # --- Temporal ---
    # Host is `temporal` (the compose service name), matching the same
    # container-vs-localhost rule as database_url.
    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "app-workflow"


# Imported everywhere as: from app.core.config import settings
settings = Settings()
