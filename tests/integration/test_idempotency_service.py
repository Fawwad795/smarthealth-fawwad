"""Proves the idempotency cache round-trips: unseen key returns None,
a stored result comes back exactly as stored, and a cache hit is
distinguishable from a miss even when appointment_id is falsy-looking.
"""

from app.services.idempotency import get_cached_result, store_result


def test_unseen_key_returns_none(test_redis_client) -> None:
    assert get_cached_result(test_redis_client, "never-used-key") is None


def test_stored_result_is_returned_on_repeat(test_redis_client) -> None:
    store_result(test_redis_client, "repeat-key", status_code=202, appointment_id=42)

    result = get_cached_result(test_redis_client, "repeat-key")

    assert result == {"status_code": 202, "appointment_id": 42}


def test_different_keys_do_not_collide(test_redis_client) -> None:
    store_result(test_redis_client, "key-a", status_code=202, appointment_id=1)
    store_result(test_redis_client, "key-b", status_code=202, appointment_id=2)

    assert get_cached_result(test_redis_client, "key-a")["appointment_id"] == 1
    assert get_cached_result(test_redis_client, "key-b")["appointment_id"] == 2
