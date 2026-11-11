"""Per-user rate limiting on the AI endpoints (task 5.12).

Each user may make settings.ai_rate_limit_per_minute AI requests per clock
minute, counted across the five AI routes together: the assistant's two and
staff generation's three. The limit protects the model budget, so one
count covers them all; switching routes does not reset it.

The count is a Redis counter, so it holds across API processes and
restarts:

- key: ai-rate:{user_id}:{minute}, minute being the Unix time divided by 60
- TTL: 60 seconds. The key is only read during its own minute; the TTL
  clears it away after.
- invalidated by: nothing. A new minute is a new key.

This is a fixed window: the count starts again at each new minute, so a
user can make up to twice the limit across a minute boundary.

When Redis cannot be reached the request is let through, logged and
counted in ai_rate_limit_skipped. The limit guards a budget, so a Redis
outage should not take the assistant down with it.
"""

import logging
import time

from fastapi import Depends, status
from redis import Redis, RedisError

from app.core.config import settings
from app.core.dependencies import get_current_user
from app.core.exceptions import AppError
from app.core.metrics import ai_rate_limit_skipped, ai_rate_limited
from app.core.redis import get_redis
from app.models import User

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ai-rate:"
_WINDOW_SECONDS = 60

RATE_LIMITED_CODE = "RATE_LIMITED"


def limit_ai_requests(
    current_user: User = Depends(get_current_user),
    redis_client: Redis = Depends(get_redis),
) -> None:
    """FastAPI dependency: count this request against the caller's limit.

    get_current_user is the one the route's role check already resolved;
    FastAPI resolves a dependency once per request, so the user is not
    loaded twice.
    """
    check_ai_rate_limit(redis_client, current_user.id)


def check_ai_rate_limit(redis_client: Redis, user_id: int) -> None:
    """Count one AI request for `user_id`; raise a 429 if it is over the limit.

    INCR adds one and returns the new count in a single step, so two
    requests at the same moment can never both read the same count.
    """
    now = _now()
    key = f"{_KEY_PREFIX}{user_id}:{int(now // _WINDOW_SECONDS)}"
    try:
        # One round trip, run as a single transaction.
        pipe = redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, _WINDOW_SECONDS)
        count, _ = pipe.execute()
    except RedisError as err:
        # The error's type only: its message can carry the Redis URL.
        logger.warning(
            "ai rate limit skipped for user %d: redis unavailable (%s)",
            user_id,
            type(err).__name__,
        )
        ai_rate_limit_skipped.inc()
        return

    if count > settings.ai_rate_limit_per_minute:
        # Seconds until the next minute starts the count again: 1 to 60.
        retry_after = _WINDOW_SECONDS - int(now % _WINDOW_SECONDS)
        logger.info(
            "ai rate limit reached for user %d: request %d this minute",
            user_id,
            count,
        )
        ai_rate_limited.inc()
        raise AppError(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code=RATE_LIMITED_CODE,
            message=f"Too many AI requests. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )


def _now() -> float:
    """The current Unix time. A function of its own so tests can fix it:
    a test that crossed a minute boundary would see its count reset."""
    return time.time()
