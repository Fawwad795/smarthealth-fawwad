# Events

Six events. Every one says something that **already happened**, carries **ids
only** (never names, contacts or free text), and is published **after** the
transaction that caused it committed.

## The envelope

Every event has the same shape:

```json
{
  "event_id": "b2f0c8e4-...",
  "event_type": "appointment.confirmed",
  "version": 1,
  "occurred_at": "2026-09-09T10:15:00+00:00",
  "correlation_id": "req-9f3a...",
  "data": { "appointment_id": 812, "slot_id": 5 }
}
```

- `event_id` — a UUID. The consumer uses it to spot a repeat.
- `version` — bumped only if the shape of `data` changes incompatibly.
- `correlation_id` — the id of the request that caused this. Grep it to follow
  one booking across the API, the workers and the consumer.
- `data` — ids only. That is rule 6.6, and `record_event()` types it
  `dict[str, int]` as the reminder.

## The catalogue

| Event | Published by | `data` | Consumer does |
|---|---|---|---|
| `appointment.booked` | `services/appointment_scheduling.py` | `appointment_id`, `patient_id`, `provider_id`, `service_id`, `slot_id` | `appointments_booked` +1 |
| `appointment.confirmed` | `temporal/activities.py` (scheduling saga) | `appointment_id`, `slot_id` | ignored |
| `appointment.cancelled` | `services/appointment_scheduling.py`, and `temporal/activities.py` when the saga compensates | `appointment_id`, `slot_id` | `cancellations` +1 |
| `visit.completed` | `services/visit.py` | `visit_id`, `appointment_id` | `completed_visits` +1, plus the wait time |
| `service.published` | `temporal/activities.py` (publish workflow) | `service_id` | ignored |
| `billing.updated` | `services/billing.py` | `billing_id`, `appointment_id` | ignored |

"Ignored" is not a gap. Topics are per aggregate, so the consumer receives more
than it acts on — three of the six move a number, the other three are there for
anyone who subscribes later.

## Topics

One topic per aggregate, not per event type: `app.appointments`, `app.visits`,
`app.services`, `app.billings`.

The message key is the aggregate id, so all of one appointment's events land on
the same partition and arrive in order — `booked` is always seen before
`confirmed`. Separate topics per event type would give no ordering guarantee
between them at all.

## How an event gets out (the outbox)

```
service commits business change + outbox row  (one transaction)
        ↓
Celery Beat runs the relay every 5s
        ↓
relay publishes to Kafka, then stamps published_at
```

The event row is written by `record_event()` **inside the caller's own
transaction**, and that function deliberately never commits. If the booking
rolls back, the event rolls back with it.

This exists to solve the dual-write problem. Committing and then publishing
loses the event if the process dies in between; publishing and then committing
announces something that never happened. Writing both in one transaction makes
that impossible.

`published_at` is stamped only after the broker acknowledges. A batch that
fails part-way rolls back entirely and is re-sent next run — so a message can
arrive twice. That is at-least-once delivery, and it is why the next section
exists.

## Idempotency: how a repeat changes nothing

Consumer group `app-analytics`. For each message:

1. `INSERT` the `event_id` into `processed_events` with
   `ON CONFLICT DO NOTHING ... RETURNING`.
2. No row came back → already handled. Skip it, but **still commit the Kafka
   offset**, or the consumer re-reads that duplicate forever.
3. A row came back → run the handler, and commit the claim and the aggregate
   update **in the same transaction**. They can never disagree about whether
   the event was handled.

`ON CONFLICT` rather than catching the error: in Postgres a violated constraint
aborts the whole transaction, which would poison the very transaction the
handler is about to use.

Offsets are committed only after the handler returns. A transient failure
rewinds with `seek()` — not committing is not enough on its own, because
`poll()` moves the client's own position regardless.

Proven by `tests/integration/test_event_replay.py`: the same `visit.completed`
delivered twice leaves `completed_visits` at 1.

## Known limitation

Daily buckets are **UTC calendar days**, not clinic-local ones. A clinic far
from UTC sees its evening appointments counted on the next day. Handlers and
the reconciliation read the same column, so the two always agree — the boundary
is simply in the wrong place for that clinic. See `docs/design.md`.
