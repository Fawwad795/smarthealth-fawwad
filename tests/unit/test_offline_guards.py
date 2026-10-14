"""The suite's own guards: no network, and fake embeddings unless a test
asks for another provider (tests/conftest.py, task 4.11).

The brief requires the suite to pass with no network access. Without these
tests, a guard that silently stopped working would look exactly like a
suite that never needed it.
"""

import socket
from urllib.parse import urlsplit

import pytest

from app.ai.embeddings import (
    FakeEmbeddings,
    HuggingFaceEmbeddings,
    get_embedding_provider,
)
from app.core.config import settings
from tests.network_guard import NetworkAccessBlocked


def test_a_test_cannot_look_up_a_host_outside_the_stack() -> None:
    with pytest.raises(NetworkAccessBlocked, match="router.huggingface.co"):
        socket.getaddrinfo("router.huggingface.co", 443)


def test_the_hosted_provider_is_stopped_before_it_reaches_its_api() -> None:
    """The real HuggingFaceEmbeddings, with its real httpx client and a
    made-up key. The guard's error comes through as itself, not as the
    provider's "unreachable" error a 503 test would accept."""
    provider = HuggingFaceEmbeddings(
        api_key="hf_not_a_real_key",
        model="sentence-transformers/all-MiniLM-L6-v2",
        dimensions=384,
        batch_size=64,
        timeout_seconds=5,
    )

    with pytest.raises(NetworkAccessBlocked):
        provider.embed(["knee x-ray"])


@pytest.mark.parametrize(
    ("url", "port"), [(settings.test_database_url, 5432), (settings.redis_url, 6379)]
)
def test_a_lookup_inside_the_stack_is_allowed(url: str, port: int) -> None:
    """The guard must not block what the suite really uses: psycopg 3 and
    redis-py both look their host up through Python's sockets."""
    assert socket.getaddrinfo(urlsplit(url).hostname, port)


def test_the_embedding_provider_is_the_fake_unless_a_test_asks_otherwise() -> None:
    """Whatever EMBEDDING_PROVIDER says in the .env the test container
    reads."""
    assert isinstance(get_embedding_provider(), FakeEmbeddings)
