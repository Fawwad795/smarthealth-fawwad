"""Redis client, shared across the process.

A plain sync client, not redis.asyncio: the rest of the app is sync
SQLAlchemy, and this also has to be callable from inside a Temporal
Activity -- which Day 2 established runs synchronously, inside a
ThreadPoolExecutor, not on the async event loop.
"""

import redis

from app.core.config import settings

redis_client = redis.from_url(settings.redis_url, decode_responses=True)
