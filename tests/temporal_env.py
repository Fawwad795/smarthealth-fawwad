"""One place that knows how to start a Temporal test environment.

Three tests need a real server to run against. The image already carries
the binary (scripts/fetch_test_server.py fetches it at build time), so
tests point at that copy instead of downloading their own -- which is
what keeps the suite off the network.
"""

import os

from temporalio.testing import WorkflowEnvironment


async def start_test_env() -> WorkflowEnvironment:
    """A time-skipping environment, using the image's binary when there is one.

    Read at call time, not import time, so a test can monkeypatch the
    variable. When it is unset -- running pytest on the laptop rather
    than in the container -- the SDK downloads its own copy exactly as
    before. Passing None is the SDK's own default, so the fallback needs
    no branch: it is slow, not broken.

    `or None` folds an empty value in with an absent one. Without it,
    TEMPORAL_TEST_SERVER_PATH= in a .env would send "" through as a real
    path and fail on a missing file rather than falling back.
    """
    return await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=os.getenv("TEMPORAL_TEST_SERVER_PATH") or None
    )
