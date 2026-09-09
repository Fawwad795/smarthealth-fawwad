"""Proves app.core.redis builds its client from settings.redis_url.

Pure unit test -- redis.from_url only connects lazily on the first real
command, so inspecting the constructed client's config needs no running
Redis server.
"""

from app.core.redis import redis_client


def test_redis_client_is_configured_for_the_app_database() -> None:
    kwargs = redis_client.connection_pool.connection_kwargs
    assert kwargs["host"] == "redis"
    assert kwargs["db"] == 0
    assert kwargs["decode_responses"] is True
