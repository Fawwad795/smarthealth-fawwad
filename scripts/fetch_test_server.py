"""Download the Temporal test-server binary at image build time.

Left alone, the SDK fetches this lazily inside whichever test first asks
for a WorkflowEnvironment: 83MB over the network on every container
start, since a fresh container's /tmp is empty. Measured at 7s on one run
and 65s on the next -- so that test's duration reported the speed of the
network, not of the code. It also made the suite need internet access,
which tests/ rules require it not to from Week 4 onward.

Starting an environment and immediately shutting it down is the only way
to ask for the download. The fetch lives inside the compiled Rust bridge
and exposes no download-only entry point.
"""

import asyncio
import sys
from pathlib import Path

from temporalio.testing import WorkflowEnvironment

DEST = Path("/opt/temporal")

# A stable name for the tests to point at. The SDK writes a
# version-stamped filename (temporal-test-server-sdk-python-1.9.0), which
# would otherwise have to be retyped everywhere it is referenced each
# time temporalio is upgraded.
STABLE_PATH = DEST / "temporal-test-server"


async def main() -> None:
    """Fetch the binary, then rename it to a version-independent path."""
    DEST.mkdir(parents=True, exist_ok=True)

    env = await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(DEST))
    await env.shutdown()

    downloaded = [path for path in DEST.iterdir() if path != STABLE_PATH]
    if len(downloaded) != 1:
        # Fail the build loudly rather than renaming the wrong file and
        # leaving the tests pointing at something that is not a server.
        sys.exit(f"expected exactly one downloaded binary, found: {downloaded}")

    downloaded[0].rename(STABLE_PATH)
    print(f"Temporal test server ready at {STABLE_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
