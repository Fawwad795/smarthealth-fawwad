"""The Celery application shared by every task and the worker process.

Both sides -- whoever queues a task (the API, a script) and the process that
executes it (the celery-worker container) -- import this same instance, so
they always agree on the broker, the result backend, and which task names
exist. Same reasoning as app/temporal/client.py being one function everyone
shares.
"""

from celery import Celery
from celery.signals import setup_logging

from app.core.config import settings
from app.core.logging import configure_logging

celery_app = Celery(
    "app",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # Importing celery_app alone does not register tasks defined elsewhere --
    # each module has to be explicitly listed here, same as worker.py
    # explicitly lists every Workflow/Activity rather than auto-discovering.
    include=[
        "app.celery.tasks.reminders",
        "app.celery.tasks.events",
    ],
)

# Celery 5.4 warns on startup that this default changes in 6.0 -- pin the
# current behaviour (retry connecting to the broker at startup) explicitly
# rather than silently inheriting whatever 6.0 changes it to.
celery_app.conf.broker_connection_retry_on_startup = True

# Celery Beat reads this to know what to enqueue and when. celery-beat is
# the only process that acts on it -- celery-worker just executes whatever
# lands in the queue, the same as if a person's request had enqueued it.
celery_app.conf.beat_schedule = {
    "outbox-relay": {
        "task": "app.celery.tasks.events.publish_outbox_events",
        # Short, because this is the delay between a booking committing
        # and its event reaching Kafka. The HTTP response never waits on
        # it either way -- this only sets how stale the analytics can be.
        "schedule": 5.0,
    },
}


@setup_logging.connect
def configure_celery_logging(**kwargs: object) -> None:
    """Install our JSON handler instead of Celery's own.

    Celery rips out the root logger's handlers on worker startup and
    installs its prose format -- unless something is connected to this
    signal, which it reads as "the application handles logging". So this
    function existing at all is the real fix; the body just points the
    worker at the same configuration the API uses.
    """
    configure_logging()
