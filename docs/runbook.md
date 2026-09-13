# Runbook

Diagnosing the four things most likely to go wrong. Every command here has
been run against this stack.

**Start here.** Is anything actually down?

```bash
docker compose ps
curl -s localhost:8000/health/ready
```

`/health/ready` names the dependency, not just "unhealthy":

```json
{"status":"ready","checks":{"database":"ok","redis":"ok","kafka":"ok","temporal":"ok"}}
```

503 with one check `"error"` means that dependency is down or hung. `/health`
(liveness) checks nothing else on purpose, so it stays fast.

---

## 1. A booking is stuck

**Symptom:** an appointment sits at `REQUESTED` or `SLOT_RESERVED` and never
reaches `CONFIRMED`.

```bash
# what state are they in?
docker compose exec postgres psql -U app -d app \
  -c "SELECT status, count(*) FROM appointments GROUP BY status;"
```

`REQUESTED` = the saga never started. `SLOT_RESERVED` = it started, took the
slot, and stopped before confirming — that slot is held.

```bash
# is anything running, and is a worker alive to run it?
docker compose exec temporal temporal workflow count \
  --address temporal:7233 --query "ExecutionStatus='Running'"
docker compose exec temporal temporal workflow list --address temporal:7233 --limit 10
docker compose ps temporal-worker
```

Temporal UI: **http://localhost:8233** — the workflow id is
`schedule-appointment-<id>`. Its History tab shows the exact activity it
stopped on.

**Running workflows + no worker = the worker died.** Nothing is lost; Temporal
holds the state. Restart it and they resume from where they stopped:

```bash
docker compose up -d temporal-worker
docker compose logs -f temporal-worker
```

Verified: 30 sagas killed mid-flight with `SIGKILL` (11 frozen at
`SLOT_RESERVED`) all reached `CONFIRMED` after a restart, with no slot left
stranded.

**Workflows running *and* a worker alive** is different — read the worker logs
for the failing activity. A workflow that has genuinely failed shows
`ExecutionStatus='Failed'` and needs a human, not a restart.

---

## 2. Reminders aren't going out

Reminders are Celery, not Temporal.

```bash
docker compose ps celery-worker celery-beat
docker compose exec redis redis-cli LLEN celery      # queue depth; 0 = nothing waiting
docker compose logs celery-worker --tail 50
```

A task that failed every retry is in the dead-letter table:

```bash
docker compose exec postgres psql -U app -d app -c \
  "SELECT job_type, attempts, error, created_at FROM failed_jobs ORDER BY id DESC LIMIT 10;"
```

`attempts = 6` means it retried five times with backoff and then gave up —
that is the policy working, not a bug. `attempts = 1` means the error was
never retryable (a missing row, say).

---

## 3. The analytics numbers look wrong

The endpoints read pre-aggregated rows, so "wrong" means the aggregate and the
raw tables disagree. **Check before you fix** — repairing first destroys the
evidence:

```bash
docker compose exec api python -m scripts.reconcile_analytics --days 7
```

Exits 1 if it finds drift, so it can be alerted on. To fix, once you have read
the report:

```bash
docker compose exec api python -m scripts.reconcile_analytics --days 7 --repair
```

Repair recomputes from the raw tables, so it is safe to run twice.

**Where drift comes from.** Almost always a consumer that dead-lettered an
event — the aggregate silently under-counts until repaired:

```bash
docker compose exec postgres psql -U app -d app -c \
  "SELECT error, payload FROM failed_jobs WHERE job_type='app.kafka.consumer'
   ORDER BY id DESC LIMIT 10;"
```

> **Known issue.** A Temporal worker outage makes `appointment.booked` events
> arrive before the saga has set `booked_at`, and the handler treats that as
> permanent and dead-letters them. Booking counts are then lost until you run
> `--repair`. Fix is to drive the aggregate from `appointment.confirmed`
> instead — see `NOTES.md`. **When demoing, show the analytics before any
> crash-recovery scenario.**

---

## 4. Events aren't reaching the consumer

```bash
# still queued in the outbox? (should drain within ~5s)
docker compose exec postgres psql -U app -d app -c \
  "SELECT count(*) FROM outbox_events WHERE published_at IS NULL;"
```

Not draining → the relay isn't running: check `celery-beat` and `celery-worker`.

Draining but nothing changes → the consumer is the problem:

```bash
docker compose ps consumer
docker compose logs consumer --tail 50
```

Look for `duplicate event skipped` (normal — that is idempotency working) versus
`dead-lettering message`.

Kafka UI: **http://localhost:8080** — consumer group `app-analytics`. A growing
lag with a live consumer means it is stuck on one message.

**A consumer that just started sees nothing for up to 5 minutes** if its topics
did not exist when it subscribed; librdkafka refreshes topic metadata on that
interval. It looks exactly like a dead consumer.

---

## Metrics

Prometheus: **http://localhost:9090** — targets `app-api`, `app-worker`,
`app-consumer`.

Every process keeps its own tally, so a domain counter must be summed across
them or you are reading one process's view:

```promql
sum by (job) (appointments_booked_total)
sum(rate(events_failed_total[5m]))
sum by (endpoint, status) (rate(http_requests_total[5m]))
```

`prometheus_client` appends `_total` to counter names, so the metric is
`appointments_booked_total` even though the code says `appointments_booked`.
