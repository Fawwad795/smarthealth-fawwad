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

## The event pipeline (established Week 3 — keep it)

**Layout.** `app/events/` names an event and writes it to `outbox_events`, and
imports no broker client. `app/kafka/` is everything that talks to a broker.
`app/celery/` is the task queue. Keep that boundary.

**Producing**
- Emit with `record_event()` inside the transaction that caused the change; it
  never commits. Nothing outside `app/kafka/` imports the producer.
- The relay claims rows `FOR UPDATE SKIP LOCKED` and stamps `published_at` only
  after the broker acks. At-least-once is the accepted trade.
- `publish()` waits on the delivery callback — `produce()` alone proves nothing.
  `message.timeout.ms` stays inside the flush window: the outbox is the only
  retry layer.
- One topic per aggregate (`app.appointments`), never per event type.
- **Message key = `<aggregate>-<id>`** (`appointment-51`), built by `key_for()`.
  Name the aggregate, never the event: `booked-51` would split one
  appointment's events across partitions. `aggregate_id` is always the id of
  the topic's own aggregate.

**Consuming**
- `enable.auto.commit: False`, `auto.offset.reset: earliest`.
- `claim_event()` (`ON CONFLICT DO NOTHING ... RETURNING`) shares the handler's
  transaction; one commit covers the claim and the aggregate update.
- `PermanentEventError` → dead-letter to `failed_jobs`, then commit the offset.
  Anything else → `seek()` back and retry, never commit.
- A handler only runs on an event whose row is guaranteed complete:
  `appointments_booked` comes from `appointment.confirmed`, because
  `appointment.booked` is queued before `booked_at` exists.
- Handlers bucket by the raw column the reconciliation reads (`booked_at`, the
  CANCELLED history row, `completed_at`), never the envelope's `occurred_at`.
- Keep "row missing" and "column empty" apart (`one_or_none()`, not
  `scalar_one_or_none()`), so a dead-letter says which anomaly it was.
- Dead-letters record coordinates (topic/partition/offset), never the body.

**Analytics and observability**
- The consumer is the sole writer of `analytics_daily`. Averages are stored as
  a sum and a count.
- Reconciliation is read-only and exits 1 on drift; `--repair` is separate and
  manual.
- Metrics are per process — query with `sum by (job)`. HTTP labels use the
  route template, never the raw URL.
- `/health/ready` checks every dependency with a deadline; 503, not 500.
- Identifiers people read are self-describing: `schedule-appointment-51`,
  `publish-service-3`, `appointment-51`, `req-...`.
