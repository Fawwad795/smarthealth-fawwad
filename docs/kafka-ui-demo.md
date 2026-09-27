# Kafka UI demo runbook

Four pipelines that exercise the event path from Kafka UI: a good event, a
replay, three kinds of faulty event, and a transient outage. Every command
here has been run against this stack.

Companion to `docs/runbook.md`, which diagnoses failures. This one *causes*
them on purpose, to show the consumer's two opposite error branches.

**Why Kafka UI and not the API?** Producing by hand bypasses `outbox_events`,
so Kafka UI stands in for the relay and the consumer side becomes observable
in isolation. To demo the full chain instead, book through the API and watch
the outbox row appear, the relay drain it within 5s, and the message land here.

---

## 0. Setup

Two terminals. **A** tails the consumer -- this is where the demo happens:

```bash
docker compose logs -f consumer
```

**B** runs the checks below. Kafka UI is at <http://localhost:8080>.

| Screen | Path | Shows |
|---|---|---|
| Topic list | `Topics` | The four topics and message counts |
| Messages | `Topics` then `app.appointments` then **Messages** | The records in the log |
| Produce | same page, **Produce Message** button | Hand-inject an event |
| Lag | `Consumers` then `app-analytics` | Current offset, end offset, lag |

**Gotcha:** the Messages tab defaults *Seek Type* to newest, so an idle topic
looks empty. Set it to **Oldest**.

### Demo data

`visits` is empty in dev data, so `visit.completed` has nothing real behind it
(which is also why `completed_visits` and `wait_count` are 0 on every row).
Use `appointment.booked` -- 39 appointments carry a `booked_at`.

| Field | Value |
|---|---|
| Appointment | `51` -- patient 19, provider 6, slot 153, CONFIRMED |
| `booked_at` date | **2026-09-09**, the bucket that moves |
| Topic / key | `app.appointments` / `51` |

Baseline. Re-run after every pipeline:

```bash
docker compose exec postgres psql -U app -d app -c 'SELECT "date", appointments_booked, cancellations FROM analytics_daily ORDER BY "date" DESC;'
```

Expected progression: **31** to 32 (P1), 32 (P2), 33 (P3), 34 (P4), back to 31
after repair.

A fresh event id:

```bash
python -c "import uuid; print(uuid.uuid4())"
```

---

## 1. Happy path -- published, brokered, consumed

Produce to `app.appointments`, partition `0`, key `51`. Keep the `event_id`;
pipeline 2 reuses it.

```json
{
  "event_id": "PASTE-FRESH-UUID",
  "event_type": "appointment.booked",
  "version": 1,
  "occurred_at": "2026-09-14T10:00:00+00:00",
  "correlation_id": "req-mentor-demo-1",
  "data": {"appointment_id": 51, "patient_id": 19, "provider_id": 6, "slot_id": 153}
}
```

1. **Messages tab** (Seek Type: Oldest) -- the record, with the offset the
   broker assigned to it.
2. **Terminal A** -- `event processed event_id=... type=appointment.booked`,
   carrying `correlation_id=req-mentor-demo-1`.
3. The claim row was written:

```bash
docker compose exec postgres psql -U app -d app -c "SELECT * FROM processed_events WHERE event_id = 'PASTE-FRESH-UUID';"
```

4. Baseline query -- `2026-09-09` moves **31 to 32**.
5. **Lag screen** -- lag `0`, offset past the message.

**Say this:** the count moved on 2026-09-09, not today, even though
`occurred_at` says today. The handler reads `appointments.booked_at` and
ignores the envelope's timestamp, because the aggregate and the reconciliation
must bucket off the same raw column -- otherwise a booking committed either
side of midnight shows as drift that was never real.

---

## 2. Replay -- the same event twice moves nothing

Produce the **identical** message again, same `event_id`. Easiest route: find
it on the Messages tab, copy it from the row menu, paste into Produce.

1. **Terminal A** -- `duplicate event skipped event_id=...`.
2. Baseline query -- still **32**. That is the whole point.
3. **Lag is `0` again** -- the offset still moved. A duplicate is not a
   failure; refusing to commit it would block the partition forever.
4. Duplicates are counted, not swallowed:

```bash
curl -s localhost:8002/metrics | grep events_consumed_total
```

`result="processed"` and `result="duplicate"` are separate label values,
because a producer stuck in a retry loop must not be invisible.

**If asked how you know the test is not vacuous:** `test_event_replay.py` is
mutation-checked -- forcing `claim_event` to return True makes it fail.

---

## 3. Faulty events -- dead-lettered, and the partition keeps moving

Three variants, each rejected at a different point, all to `app.appointments`.

**3a. Missing a required field.** Rejected by `parse_message`, before any DB work:

```json
{"event_id": "FRESH-UUID-A", "event_type": "appointment.booked", "version": 1}
```

Gives `envelope is missing fields: ['data']`.

**3b. Unknown event type.** Rejected at the `EventType(...)` lookup:

```json
{"event_id": "FRESH-UUID-B", "event_type": "appointment.exploded", "version": 1, "data": {"appointment_id": 51}}
```

Gives `unknown event type: appointment.exploded`.

**3c. Well-formed, absent row.** Reaches the handler:

```json
{"event_id": "FRESH-UUID-C", "event_type": "appointment.booked", "version": 1, "data": {"appointment_id": 999999}}
```

Gives `appointment 999999 does not exist`.

Then:

1. **Terminal A** -- `dead-lettering message topic=... partition=... offset=...
   reason=...`. Coordinates only, never the body: a message that failed to
   parse is of unknown shape, and `failed_jobs` must not become the place PHI
   arrives by accident.
2. The dead-letter rows:

```bash
docker compose exec postgres psql -U app -d app -c "SELECT id, error, payload, attempts FROM failed_jobs WHERE job_type='app.kafka.consumer' ORDER BY id DESC LIMIT 5;"
```

`attempts = 1` -- one attempt, no retry. Re-attempting what can never succeed
only blocks the partition behind it.

3. **The point of the pipeline:** produce a *good* message (pipeline 1's
   payload, fresh `event_id`) and watch it process. **32 to 33.** Three bad
   messages killed nothing, stalled nothing, and were all recorded.

**On 3c:** that error string is verbatim the one the Day 5 crash demo produced
28 times -- except there it said "appointment 51 does not exist" about an
appointment that existed, because `appointment.booked` is queued before the
saga writes `booked_at`. Deferred bug, recorded in `docs/design.md`.

---

## 4. Transient outage -- the offset does *not* move

The opposite branch from pipeline 3, and the clearest thing to show in Kafka UI
because lag becomes visible.

`temporal`, `api` and `celery-worker` also depend on Postgres, so expect noisy
errors in their logs while it is down. Expected, not a bug.

```bash
docker compose stop postgres
```

Produce a **valid** pipeline 1 payload with a fresh `event_id`.

1. **Terminal A** -- `event handling failed; offset left for redelivery`, then
   retrying every few seconds. Alive and looping, not dead.
2. **Lag screen -- lag sits at `1` and stays there.** The message was read but
   not accepted.

```bash
docker compose start postgres
```

3. It processes on the next retry, lag returns to `0`, **33 to 34**.

**Say this:** a permanent error commits the offset *after* dead-lettering,
because the message can never succeed. A transient error rewinds with `seek()`
and commits nothing, because it will succeed later. Get them backwards and you
either block the partition forever or lose events silently.

And `seek()` is required because *not committing is not enough* -- `poll()`
advances the client's own position regardless, so without the rewind the retry
would never re-read the message inside that process.

**Harder variant:** `docker compose pause postgres` instead of `stop`. A
stopped dependency refuses instantly; a paused one accepts and says nothing.
That is the "down versus hung" distinction the readiness timeouts exist for.

---

## 5. Cleanup

The demo added three bookings to 2026-09-09 that no appointment backs, so the
aggregate reads 34 against a real 31. Show the reconciliation finding it:

```bash
docker compose exec api python -m scripts.reconcile_analytics --days 7
```

It reports the drift and **exits 1** -- a check that always exits 0 cannot be
alerted on. Repair is a separate, manual action:

```bash
docker compose exec api python -m scripts.reconcile_analytics --days 7 --repair
```

Re-run without `--repair` to confirm it is clean and back to **31**. Then drop
the demo's dead-letter rows. Filter on the job type, not the error text --
which variants you ran decides which messages are there:

```bash
docker compose exec postgres psql -U app -d app -c "SELECT id, job_type, left(error, 45) AS error FROM failed_jobs ORDER BY id DESC LIMIT 10;"
docker compose exec postgres psql -U app -d app -c "DELETE FROM failed_jobs WHERE job_type='app.kafka.consumer';"
```

The 28 crash-demo rows from Day 5 are safe: they were written under the old
`app.workers.consumer` job type, before the package rename.

Pipeline 4 also dead-letters one `publish_outbox_events` row, because the relay
cannot reach Postgres either while it is stopped. Harmless -- the outbox rows
stay `published_at IS NULL` and the next Beat tick drains them -- but confirm
that before deleting it:

```bash
docker compose exec postgres psql -U app -d app -c "SELECT count(*) AS unpublished FROM outbox_events WHERE published_at IS NULL;"
```

The Kafka records stay in the log -- you cannot delete a record from a
partition, only let it age out. That immutability is what makes replay possible
in the first place.
