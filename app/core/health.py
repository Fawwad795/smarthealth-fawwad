"""Readiness: can this process actually do its job right now?

/health answers "is this process alive". This module answers the harder
question -- whether the things it depends on can be reached. An API that is
running but cannot see Postgres is up and useless, and only one of those two
facts is worth waking someone for.

Every check here has a deadline, and that is the part that matters. A
dependency that is down refuses a connection immediately; one that is hung
accepts it and then says nothing, forever. Without a timeout this endpoint
hangs too -- and a health check that hangs is worse than one that fails,
because a monitoring tool cannot tell it apart from a slow network, so it
waits politely instead of raising the alarm.

Two of the four are not really this process's own dependencies: the API
writes events to the outbox table rather than to Kafka, and only touches
Temporal when starting a workflow. They are checked anyway because the
question being answered is "can a booking get all the way through", and the
answer is no if either is down.
"""

import asyncio
import logging

from confluent_kafka.admin import AdminClient
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.temporal.client import get_temporal_client

logger = logging.getLogger(__name__)

# How long any single check may take before it counts as a failure. Short
# enough that a monitor polling every 10 seconds never overlaps itself,
# long enough not to trip over an ordinary slow moment.
CHECK_TIMEOUT_SECONDS = 2.0

# The order they are reported in. Named once so the checks and their labels
# cannot drift apart.
DEPENDENCIES = ("database", "redis", "kafka", "temporal")


def check_database(db: Session) -> bool:
    """Ask Postgres to run the simplest statement there is.

    SELECT 1 rather than reading a table, so this cannot fail because a
    migration has not run or because some table is locked.

    statement_timeout is set first: a database that accepted the connection
    and then stopped answering would otherwise hang here. SET LOCAL lasts
    only for the current transaction, so it cannot leak into anything else.
    """
    db.execute(
        text(f"SET LOCAL statement_timeout = {int(CHECK_TIMEOUT_SECONDS * 1000)}")
    )
    db.execute(text("SELECT 1"))
    return True


def check_redis(client: Redis) -> bool:
    """Send Redis a real command and wait for the reply.

    ping(), rather than just holding the client object: redis-py connects
    lazily, so a client pointed at a server that has never existed looks
    perfectly healthy right up until you ask it something.
    """
    return bool(client.ping())


def check_kafka() -> bool:
    """Ask the broker to describe its topics.

    The cheapest call that proves a real round trip. An AdminClient rather
    than a Producer, because a Producer would allocate send buffers we have
    no use for -- and list_topics takes the deadline directly, which is
    exactly what this needs.
    """
    admin = AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers})
    admin.list_topics(timeout=CHECK_TIMEOUT_SECONDS)
    return True


async def check_temporal() -> bool:
    """Connect to the Temporal server and ask it how it is.

    check_health() is a real request, so this fails if Temporal is
    listening but unwell rather than only if the port is shut. wait_for
    supplies the deadline both times, because connecting to an address that
    accepts and then goes silent would otherwise never come back.
    """
    client = await asyncio.wait_for(get_temporal_client(), CHECK_TIMEOUT_SECONDS)
    await asyncio.wait_for(client.service_client.check_health(), CHECK_TIMEOUT_SECONDS)
    return True


async def run_checks(db: Session, redis_client: Redis) -> dict[str, str]:
    """Run all four checks together and report a verdict for each.

    Concurrently rather than one after another, so the endpoint costs as
    long as the slowest check instead of the sum of all four -- two seconds
    rather than eight on the day everything is broken at once.

    The three synchronous checks go to worker threads because they block.
    Run straight on the event loop they would freeze every other request
    being served while Postgres thinks.

    Every exception becomes "down" rather than propagating. An endpoint that
    raises tells the caller nothing about *which* dependency failed, and
    that is the only thing it exists to say.
    """
    results = await asyncio.gather(
        run_in_threadpool(check_database, db),
        run_in_threadpool(check_redis, redis_client),
        run_in_threadpool(check_kafka),
        check_temporal(),
        return_exceptions=True,
    )

    verdicts: dict[str, str] = {}
    for name, result in zip(DEPENDENCIES, results, strict=True):
        if isinstance(result, BaseException):
            # The real reason goes to the log, where it is safe. The
            # response gets a bare "down" -- a driver's error message can
            # carry the host and password it was dialling with.
            logger.warning(
                "readiness check failed dependency=%s error=%s", name, result
            )
            verdicts[name] = "down"
        else:
            verdicts[name] = "ok"
    return verdicts
