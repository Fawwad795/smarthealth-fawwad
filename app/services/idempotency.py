"""Client-driven idempotency: an Idempotency-Key header maps to the
result of the request that first used it.

This is the *client-driven* mechanism (CLAUDE.md non-negotiable #2) --
distinct from the atomic slot UPDATE (data-driven, slot.py) and from
Appointment.idempotency_key's unique constraint, which is a database-level
backstop for this same key if Redis is ever unavailable or evicts it
early. A booking endpoint checks here first: a hit means "don't do the
work again, just return what happened last time."
"""

import json

from redis import Redis

from app.core.config import settings

_KEY_PREFIX = "idempotency:appointment:"


def get_cached_result(redis_client: Redis, idempotency_key: str) -> dict | None:
    """Return the stored result for this key, or None if it's unseen.

    The stored shape is {"status_code": int, "appointment_id": int} --
    everything a repeat request needs to reproduce the original response
    without touching the database.
    """
    raw = redis_client.get(_KEY_PREFIX + idempotency_key)
    if raw is None:
        return None
    return json.loads(raw)


def store_result(
    redis_client: Redis, idempotency_key: str, status_code: int, appointment_id: int
) -> None:
    """Remember a first-time request's result under its key.

    Only ever called after the real work succeeds -- never on a cache
    hit, or a retried request would keep resetting its own TTL.
    """
    redis_client.set(
        _KEY_PREFIX + idempotency_key,
        json.dumps({"status_code": status_code, "appointment_id": appointment_id}),
        ex=settings.idempotency_key_ttl_seconds,
    )
