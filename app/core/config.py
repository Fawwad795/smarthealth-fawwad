"""Application settings, loaded from environment variables.

Nothing in the codebase reads os.environ directly. Everything goes through
the single `settings` object below, so there is exactly one place that
knows how the app is configured.
"""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings fields are added as each week needs them."""

    model_config = SettingsConfigDict(
        env_file=".env",
        # .env holds keys for Weeks 4-5 (the LLM, retrieval tuning) that this
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
    # How long any single Redis command may take. Without a limit, a Redis
    # that accepts the connection and then goes quiet blocks the caller
    # forever -- a booking, not just a health check.
    redis_timeout_seconds: float = 2.0

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

    # --- Celery (Week 3+) ---
    # Broker and result backend both live in Redis, on separate DB indices
    # from redis_url (DB 0) so Celery's queue and the idempotency cache
    # never collide.
    celery_broker_url: str
    celery_result_backend: str

    # --- Kafka (Week 3+) ---
    # `kafka:9092` is the in-container address. From the laptop it is
    # localhost:29092 -- see the two listeners in docker-compose.yml.
    kafka_bootstrap_servers: str = "kafka:9092"
    kafka_consumer_group: str = "app-analytics"
    # Topics are <prefix>.<aggregate>: app.appointments, app.visits, ...
    kafka_topic_prefix: str = "app"

    # --- Domain rules ---
    # How long an Idempotency-Key is remembered before a retried request
    # would be treated as brand new. Read by app/services/idempotency.py.
    idempotency_key_ttl_seconds: int = 86400
    # Simulated billing failure switch, for exercising the Week 2 saga's
    # compensation path (task 2.9) on demand. Never true outside a demo
    # or a test that deliberately flips it.
    billing_force_fail: bool = False

    # --- Embeddings (Week 4+) ---
    # Which EmbeddingProvider app/ai/embeddings.py builds. "local" runs the
    # model in-process from the image: no key, no quota, no network.
    # "huggingface" calls the hosted API with EMBEDDING_API_KEY. "fake" is the
    # hashing stand-in the tests use, which knows nothing about meaning.
    embedding_provider: Literal["local", "huggingface", "fake"] = "local"
    # Only the huggingface provider uses this. SecretStr so the key prints as
    # '**********' in any repr or log line; reading it takes an explicit
    # .get_secret_value().
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Fixed by the model, and by the vector column built for it: changing
    # the model means a migration and re-embedding every service.
    embedding_dimensions: int = 384
    # Texts per batch. The brief asks for 32-100: for the hosted API,
    # per-request overhead and the rate limit are paid once per batch, not
    # per text; the local model runs each batch as one ONNX call.
    embedding_batch_size: int = 64
    # huggingface provider only. Above the ~8s measured for a cold Hugging
    # Face model, and below the publish Activities' 30s timeout, so the HTTP
    # call fails and is classified before Temporal abandons the attempt.
    embedding_timeout_seconds: float = 20.0

    # --- Retrieval (Week 4+) ---
    # How many services POST /search returns when the request names no
    # top_k. The brief's default.
    retrieval_top_k: int = 5
    # The lowest cosine similarity a service may score and still be
    # returned. Below it a match is noise, and an empty result is the
    # "we don't offer that" answer. Read on every search, never sent by the
    # client: a client that could lower it could ask for noise.
    # 0.31 comes from the eval set (scripts/eval_retrieval.py, task 4.10):
    # unrelated queries scored at most 0.298, and every offered query's
    # service at least 0.332. It holds for all-MiniLM-L6-v2 only; a new
    # model needs `make eval` run again.
    retrieval_min_similarity: float = 0.31
    # The longest search query, in characters. Shared with the Week 5
    # assistant's question limit.
    ai_max_question_chars: int = 1000

    # --- LLM (Week 5) ---
    # Which LLMProvider app/ai/llm.py builds. "groq" and "openai" take the
    # same request format at different addresses. "fake" is the tests'
    # stand-in and makes no call. Anthropic is not offered: its API has a
    # different shape and would need a second client.
    llm_provider: Literal["groq", "openai", "fake"] = "groq"
    # SecretStr for the same reason as embedding_api_key.
    llm_api_key: SecretStr = SecretStr("")
    # Chosen on Week 5 Day 1 from the chat models Groq offers, once
    # llama-3.1-8b-instant had gone. It answers without a hidden reasoning
    # pass, so every token of max_completion_tokens goes to the answer
    # (docs/ai-layer.md section 9).
    llm_model: str = "qwen/qwen3.8-27b"
    # The name the assistant gives the clinic in its prompts and fixed
    # replies. A setting, not a read of the clinics table: the assistant
    # speaks for the organisation, and a clinics row is one site of it.
    assistant_clinic_name: str = "MediNova"
    # Seconds httpx waits on any one step of a model call: connecting, or
    # the gap between two pieces of a reply. A per-step limit rather than a
    # total, so a long reply that keeps arriving is never cut off.
    ai_request_timeout_seconds: float = 60.0


# Imported everywhere as: from app.core.config import settings
settings = Settings()
