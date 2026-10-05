"""Celery worker package: the app-side task-queue code.

Mirrors app/temporal/ -- a small, self-contained package with its own
client-ish object (celery_app) and its own registered units of work (tasks/).
"""
