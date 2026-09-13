---
paths:
  - "app/events/**"
  - "app/kafka/**"
  - "app/celery/**"
  - "app/core/**"
  - "scripts/reconcile_analytics.py"
---

# Events, Celery, analytics and observability (Week 3)

**Order of work in Week 3: Celery first, then Kafka, then observability.**

## Events

Types: `appointment.booked`, `appointment.confirmed`, `appointment.cancelled`,
`visit.completed`, `service.published`, `billing.updated`.

Envelope — **ids only, never PHI**:

```json
{ "event_id": "b2f0…", "event_type": "appointment.confirmed", "version": 1,
  "occurred_at": "2026-07-25T10:15:00Z", "correlation_id": "req-9f3…",
  "data": { "appointment_id": 812, "provider_id": 12, "slot_id": 5 } }
```

Events describe something that **already happened**, carry **ids not objects**, and
are **never published before the causing transaction commits**. Document each in
`docs/events.md`.

**Consumer idempotency:** INSERT the `event_id` into `processed_events` first; a
unique violation means skip. Process the event and update aggregates **in the same
transaction** as that insert. Demonstrate replaying `visit.completed` twice without
moving the count.

**Outbox [SHOULD]:** insert the event into `outbox_events` in the same transaction as
the business change, publish from the outbox. Solves the dual-write problem —
`commit()` then `produce()` loses events on a crash; `produce()` then `commit()` emits
a lie on rollback. At minimum, publish after commit and explain the window.

A consumer must never die on one bad message. Never commit Kafka offsets before
processing succeeds. Never assume exactly-once delivery.

## Celery

`autoretry_for`, `retry_backoff`, `retry_jitter`, `max_retries`. Retry only transient
failures. Final failure → `failed_jobs` (the dead-letter table, which feeds the
"failed workflows" metric). `CELERY_TASK_ALWAYS_EAGER=True` in tests. Tasks are thin
wrappers over `services/`. A "notification" is a `notifications` row plus a log line —
no real SMS/email.

## Analytics — six metrics, served from pre-aggregated tables

Total patients · appointments booked over time (daily buckets, date-range filter) ·
completed visits · cancellation rate · average wait time (check-in vs. scheduled) ·
failed workflows/background jobs.

**Never `COUNT(*)` over everything per request** — aggregates are maintained by the
event consumer; endpoints read the aggregates. Ship a reconciliation endpoint/script
comparing aggregates to raw tables and reporting drift.

**Caching rule:** never cache without writing down (a) the key, (b) the TTL, (c) what
invalidates it.

## Observability

- Structured **JSON** logs: `timestamp`, `level`, `logger`, `correlation_id`,
  `message`. No bare `print()`. **Never log PHI** (names, contacts) — log ids.
- **Correlation ID per request**: middleware reads `X-Request-ID` or generates a UUID,
  stores it in a ContextVar, adds it to every log record, returns it in a response
  header, and propagates it into Temporal workflow/activity args, Celery task kwargs
  and Kafka envelopes. Success criterion: `grep <id> logs/` shows one booking's full
  story across API → worker → consumer, with no PHI.
- `GET /health` (liveness, checks nothing else so it stays fast);
  `GET /health/ready` (DB, Redis, Kafka, Temporal).
- `GET /metrics` Prometheus: request count/latency by endpoint+status, plus domain
  counters — `appointments_booked_total`, `double_booking_prevented_total`,
  `events_consumed_total`, `events_failed_total`.

## Error handling (established Week 1 — keep it)

One JSON error shape for every failure: `{"error": {"code", "message"}}`, built in
`app/core/error_handlers.py` by four handlers covering `AppError`,
`RequestValidationError`, `StarletteHTTPException` and the `Exception` catch-all.
Correct status codes. **Never leak stack traces, DB errors or PHI to clients** — the
catch-all logs the real detail and returns a flat 500.

`AppError` is for expected failures raised deliberately by `services/`. A bug is not
an `AppError` — it belongs to the catch-all.

**Never hardcode a status code**: `from fastapi import status` →
`status.HTTP_409_CONFLICT`.
