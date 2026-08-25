---
paths:
  - "app/temporal/**"
  - "app/services/**"
---

# Slot concurrency, Temporal workflows and the scheduling saga

## Slot reservation — the core invariant

Reserving a slot is a **single atomic conditional update**:

```sql
UPDATE slots SET status = 'RESERVED', updated_at = now()
WHERE id = :slot_id AND status = 'AVAILABLE'
RETURNING id;
```

No row back = already taken, reject cleanly. SELECT-then-check-then-UPDATE is **not
acceptable** — two bookings both read AVAILABLE and both book. The check-then-act gap
is the bug; the database must be the arbiter. Must be explained in the docs and
proven by a test firing ~50 concurrent bookings at one slot: exactly one confirmed
appointment.

Note this statement protects one row from concurrent access to *itself*. It says
nothing about two different rows overlapping in time — that is what the `EXCLUDE`
constraint on `slots` is for.

## Idempotency — four distinct mechanisms, all needed

- **Client-driven:** `Idempotency-Key: <uuid>` header on `POST /appointments`, stored
  in Redis as key → (status_code, appointment_id) with a TTL. A repeat key returns the
  original appointment. No second appointment, no second billing row.
- **Data-driven:** the atomic reservation above.
- **Activity-level:** every Temporal Activity must be safe to retry.
- **Consumer-level:** `processed_events` unique on `event_id`.

## Service publishing — a Temporal Workflow

```
DRAFT ──publish──> PUBLISHING ──success──> PUBLISHED ──unpublish──> INACTIVE
                        └──failure──> PUBLISH_FAILED ──retry──> PUBLISHING
```

Activities: validate completeness → structure the operational content → chunk it →
(Part B: embed) → mark PUBLISHED. Temporal provides durability, retries and
resumption — **no hand-rolled job table, no polling loop, no separate state-machine
module.**

- Validation runs first and returns **all** errors at once.
- A validation failure ends the workflow cleanly in PUBLISH_FAILED; a transient
  failure is retried by Temporal.
- Illegal entry transitions → **409**, checked in one place before the workflow starts.
- `POST /services/{id}/publish` → 202 + workflow id; `GET /services/{id}/publish-status`
  queries the workflow.
- Re-publishing replaces chunks **atomically**; killing the worker mid-publish and
  restarting must resume with no duplicated chunks. Demonstrate it.

**This workflow is the only path to PUBLISHED.** `POST`/`PATCH /services` deliberately
have no `status` field — do not add one.

## Appointment scheduling — a Temporal saga

```
REQUESTED ──reserve──> SLOT_RESERVED ──billing──> CONFIRMED ──(visit)──> COMPLETED
    │                       │
 (ineligible)         (billing fails)
    ▼                       ▼
REJECTED         compensate(release slot) → CANCELLED
                                                ▲
                        patient cancel / reschedule (release slot, move waitlist)
```

Activities: validate_eligibility → reserve_slot → billing_precheck →
schedule_reminders → confirm. On failure, compensating Activities run **in reverse**
and the appointment lands in a clean terminal state.

- Billing is a simulated internal `BillingChecker` with
  `precheck(appointment, idempotency_key)` — records a billing row, forcible failure
  via a config flag, itself idempotent. No real payment integration.
- **Compensation must actually restore state**: a billing failure after reservation
  releases the slot back to AVAILABLE. Cancellation releases the slot and moves the
  waitlist. **Never delete the appointment row** — transition it; preserve history.
- Rescheduling releases old + reserves new **atomically** — no window holding neither
  or both.
- `POST /appointments` → 202 + appointment id; `GET /appointments/{id}` reports state.
- Every state change → `appointment_status_history`.
- Crash-safe: kill the worker mid-booking, restart, no double reservation. Demonstrate.

## Temporal rules that will be punished if broken

- **Workflows must be deterministic**: no `datetime.now()`, no `random`, no DB calls,
  no network inside a Workflow function. All I/O lives in Activities. Use Temporal
  timers, never `time.sleep`.
- **Activities must be idempotent**: they can be retried at any time. `reserve_slot`
  uses the atomic update plus a `slot_reservations` row so a retry doesn't
  double-reserve; `billing_precheck` checks for an existing pre-check first.

## Visit lifecycle — deliberately *not* a workflow

`CHECKED_IN → IN_PROGRESS → COMPLETED`, driven by front_desk/provider actions, with
one small transition-validating function. Illegal jumps (completing a visit never
checked in) → **409**. Check-in and completion are idempotent — a retried check-in
must not create duplicate history rows or advance twice.

## Service-layer conventions established in Week 1

- **Check before insert, don't catch a bare `IntegrityError`.** One `IntegrityError`
  cannot distinguish "duplicate name" from "the foreign key doesn't exist", and those
  need different status codes. Pre-check what you can; let the `except` handle only
  what remains.
- **`None` on an update schema means "not mentioned, leave it alone"** — never "set
  this to null".
- **A `get_*` helper that raises 404 is the single entry point** for resolving an id,
  so "missing" produces one consistent error rather than a `None` every caller must
  remember to check.
- **Scope nested resources to their parent.** A `schedule_id` that exists but belongs
  to another provider returns 404, not the row.
- **Use `from fastapi import status`**, never a hardcoded integer.
