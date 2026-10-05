"""Celery package: the app-side task-queue code.

Mirrors app/temporal/ -- a small, self-contained package with its own
client-ish object (celery_app) and its own registered units of work
(tasks/). Named for the tool rather than "workers", because there are
three other kinds of worker process in this system; the Kafka consumer,
which used to live here, is now app/kafka/consumer.py.
"""
