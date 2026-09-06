"""The Celery application shared by every task and the worker process.

Both sides -- whoever queues a task (the API, a script) and the process that
executes it (the celery-worker container) -- import this same instance, so
they always agree on the broker, the result backend, and which task names
exist. Same reasoning as app/temporal/client.py being one function everyone
shares.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "app",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # Importing celery_app alone does not register tasks defined elsewhere --
    # each module has to be explicitly listed here, same as worker.py
    # explicitly lists every Workflow/Activity rather than auto-discovering.
    include=["app.workers.tasks.ping", "app.workers.tasks.reminders"],
)

# Celery 5.4 warns on startup that this default changes in 6.0 -- pin the
# current behaviour (retry connecting to the broker at startup) explicitly
# rather than silently inheriting whatever 6.0 changes it to.
celery_app.conf.broker_connection_retry_on_startup = True
