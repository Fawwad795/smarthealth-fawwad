"""A trivial task proving the Celery + Redis wiring end-to-end.

Not a real feature -- task 3.2 deletes this once the actual reminder and
rollup tasks exist. Its only job is to be queued from outside the worker
process and observably run inside it, the same role ping_workflow.py played
for Temporal on Week 2 Day 1.
"""

from app.workers.celery_app import celery_app

@celery_app.task
def add(x: int, y: int) -> int:
    """Add two numbers. Exists only to prove a task can round-trip through Redis."""
    return x + y
