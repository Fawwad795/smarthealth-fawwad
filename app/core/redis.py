"""Redis client, shared across the process.

A plain sync client, not redis.asyncio: the rest of the app is sync
SQLAlchemy, and this also has to be callable from inside a Temporal
Activity -- which Day 2 established runs synchronously, inside a
ThreadPoolExecutor, not on the async event loop.
"""

import redis

from app.core.config import settings

redis_client = redis.from_url(settings.redis_url, decode_responses=True)


def get_redis() -> redis.Redis:
    """FastAPI dependency: hands out the shared client.

    Used as `redis_client: Redis = Depends(get_redis)` in routers. A
    dependency rather than a direct import so tests can point route-level
    code at the test database (index 15) through dependency_overrides,
    exactly as they do with get_db -- the app's real Redis must never be
    written to by the suite.
    """
    return redis_client
