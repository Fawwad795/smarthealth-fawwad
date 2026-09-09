"""Shared Task base class for every Celery task in this project.

A task's last, permanent failure should never just vanish into worker log
output -- DeadLetterTask writes it to failed_jobs instead. Built once, on
the first task that needs it, same reasoning as PublishActivities and
BillingChecker: a shared class beats repeating the same on_failure body in
every task module later.
"""

from celery import Task

from app.db.session import SessionLocal
from app.models import FailedJob


class DeadLetterTask(Task):
    """Writes a failed_jobs row once Celery has given up retrying a task.

    Celery calls on_failure automatically when a task's exception won't be
    retried again -- either autoretry_for exhausted max_retries, or the
    exception wasn't a retryable one to begin with. Runs inside the worker
    process, so it opens its own session the same way Temporal Activities
    do -- there is no Depends(get_db) here.
    """

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Write the failed_jobs row. Called by Celery, never directly."""
        with SessionLocal() as db:
            db.add(
                FailedJob(
                    job_type=self.name,
                    payload={"args": list(args), "kwargs": kwargs, "task_id": task_id},
                    error=str(exc),
                    attempts=self.request.retries + 1,
                )
            )
            db.commit()
