"""Application settings, loaded from environment variables.

Nothing in the codebase reads os.environ directly. Everything goes through
the single `settings` object below, so there is exactly one place that
knows how the app is configured.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Week 1 settings. Fields are added as each week needs them."""

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

    # --- Redis ---
    redis_url: str

    # --- Auth ---
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60


# Imported everywhere as: from app.core.config import settings
settings = Settings()
