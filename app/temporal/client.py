"""Connects to the Temporal server.

One function, used by both API endpoints that start/query workflows and
by scripts -- so there is exactly one place that knows the server address.
"""

from temporalio.client import Client

from app.core.config import settings


async def get_temporal_client() -> Client:
    """Connect to the Temporal server at settings.temporal_host.

    A new connection per call is cheap -- the SDK multiplexes over one
    underlying gRPC channel -- so nothing here needs to be a singleton.
    """
    return await Client.connect(
        settings.temporal_host, namespace=settings.temporal_namespace
    )
