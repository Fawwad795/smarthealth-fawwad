"""Keeps the test suite off the network (task 4.11).

The brief requires the full suite to pass with no network access. The
fakes behind the provider interfaces are what make that possible; this
guard is what makes a slip visible. tests/conftest.py installs it around
every test that is not marked `live`.

It replaces socket.getaddrinfo, the lookup Python network clients make
before they connect: httpx, requests, redis-py, and psycopg 3, which
resolves the Postgres host in Python rather than leaving it to libpq.
Kafka (librdkafka) and Temporal's test server (the SDK's Rust core)
connect from C or Rust, outside Python's socket module, so the guard never
sees them. Both are inside the compose stack.
"""

import socket
from urllib.parse import urlsplit

import pytest

from app.core.config import settings


class NetworkAccessBlocked(Exception):
    """A test tried to look up a host outside the compose stack.

    Deliberately not an OSError. httpx turns an OSError into a
    ConnectError, which the hosted embedding provider reports as "provider
    unreachable", so a test expecting a 503 would pass while it was really
    trying to reach the internet.
    """


# The hosts a test may reach through Python's sockets: the stack's
# Postgres and Redis, and this machine.
ALLOWED_HOSTS = frozenset(
    {
        urlsplit(settings.database_url).hostname,
        urlsplit(settings.test_database_url).hostname,
        urlsplit(settings.redis_url).hostname,
        urlsplit(settings.test_redis_url).hostname,
        "localhost",
        "127.0.0.1",
        "::1",
    }
)


def block_outside_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any lookup of a host outside ALLOWED_HOSTS raise
    NetworkAccessBlocked, for the rest of the current test."""
    real_getaddrinfo = socket.getaddrinfo

    def guarded_getaddrinfo(host: str | bytes | None, *args, **kwargs) -> list:
        """socket.getaddrinfo, refusing any host outside the stack."""
        if host not in ALLOWED_HOSTS:
            raise NetworkAccessBlocked(f"a test tried to reach {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
