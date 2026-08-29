"""A trivial workflow that proves the worker <-> Temporal server wiring
works end-to-end. Temporary, the same way health_db proved the API <->
Postgres wiring in Week 1 (app/main.py) -- deleted once 2.3 adds the real
publish workflow, not extended into it.
"""

from datetime import timedelta
from temporalio import activity, workflow


@activity.defn
async def ping_activity(name: str) -> str:
    """The one piece of work PingWorkflow performs. Activities are where
    I/O belongs; this one does none -- the point is proving an Activity
    can be scheduled and executed by a real worker, not what it computes.
    """
    return f"pong, {name}"


@workflow.defn
class PingWorkflow:
    """Calls ping_activity once and returns its result.

    No I/O, no datetime.now(), no random here -- Workflow code must be
    deterministic because Temporal replays it on recovery.
    """

    @workflow.run
    async def run(self, name: str) -> str:
        """Run the workflow: schedule ping_activity and return its result."""
        return await workflow.execute_activity(
            ping_activity, name, start_to_close_timeout=timedelta(seconds=10)
        )