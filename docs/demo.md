# Final demo script

The 20-minute final demo, in the brief's order: 2 minutes on the problem and
architecture, 6 on Part A, 7 on Part B, 3 on what I would do differently, 2 for
questions. Every command was rehearsed on a fresh stack on 2026-10-22, and the
outputs below come from that run.

Run it from Git Bash. The only fixed id is service 4, Cardiology Consultation;
the helpers look slots up through `GET /services/4/slots`.

**Do not debug live.** If Groq answers 503, its free tier has used its 8,000
tokens for the minute: wait a minute, or show the saved transcripts in
`docs/ai-layer.md` section 12.

---

## 0. Before the meeting (10 min)

Reset to the seed data, building the schema before any worker starts, then
check every dependency:

```bash
docker compose down -v
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up -d
docker compose exec api python -m scripts.seed
docker compose ps                        # 12 services, all running
curl -s localhost:8000/health/ready      # all four "ok"
```

This is `make reset` with the migration moved first. In `make reset`, Celery
Beat starts with everything else, and its first outbox relay can run before
the tables exist; that run is dead-lettered, and `failed_jobs` then reads 1 in
the analytics and the report (`docs/prd.md` section 7).

Open the README's architecture diagram, the Temporal UI at
<http://localhost:8233>, and `docs/ai-layer.md` section 12. Then paste the
helpers:

```bash
export PYTHONIOENCODING=utf-8
B=http://localhost:8000/api/v1
tok() { curl -s -X POST $B/auth/login -H 'Content-Type: application/json' -d '{"email":"'"$1"'","password":"ChangeMe123!"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'; }
ADMIN=$(tok admin@medinova.example); FRONT=$(tok frontdesk@medinova.example)
P1=$(tok patient.one@example.com); P2=$(tok patient.two@example.com); P3=$(tok patient.three@example.com)
sql() { docker compose exec -T postgres psql -U app -d app -c "$1"; }
open_slot() { curl -s "$B/services/$1/slots?limit=1" | python -c 'import sys,json;i=json.load(sys.stdin)["items"][0];print(i["slot_id"],i["provider_id"])'; }
book() { curl -s -X POST $B/appointments -H "Authorization: Bearer $P1" -H 'Content-Type: application/json' -H "Idempotency-Key: $4" -d '{"service_id":'$1',"slot_id":'$2',"provider_id":'$3'}'; echo; }
status() { curl -s $B/appointments/$1 -H "Authorization: Bearer $ADMIN" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])'; }
ask() { curl -s -X POST $B/assistant/ask -H "Authorization: Bearer ${2:-$P1}" -H 'Content-Type: application/json' -d '{"question":"'"$1"'"}' | python -c 'import sys,json;d=json.load(sys.stdin);print(d["outcome"],[c["name"] for c in d["citations"]]);print(d["answer"])'; }
publish_status() { curl -s $B/services/$1/publish-status -H "Authorization: Bearer $ADMIN" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])'; }
trace() { docker compose logs --no-log-prefix api temporal-worker consumer celery-worker | grep "$1" | python -c 'import sys,json
rows = [json.loads(l) for l in sys.stdin if l.startswith("{")]
for d in sorted(rows, key=lambda d: d["timestamp"]): print(d["timestamp"][11:19], d["logger"][:28].ljust(28), d["message"][:80])'; }
```

---

## 1. Problem and architecture (2 min)

Show the README's problem table and architecture diagram.

**Say:** MediNova had five problems: two patients could hold one slot, patients
could not find the right service, dashboards disagreed with the tables, peak
hours timed out, and background failures were invisible. SmartHealth is an API
for the operations side only, never clinical. Temporal runs the multi-step
work that must survive a crash (publishing, the booking saga), Celery the
fire-and-forget work, and Kafka carries events from an outbox to a consumer
that keeps the analytics. The AI layer adds no container: the embedding model
runs in the API and the worker, vectors live in Postgres, and only the
language model is hosted.

---

## 2. Part A (6 min)

### 2.1 A publish survives the worker going down (1.5 min)

```bash
SID=$(curl -s -X POST $B/services -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"department_id": 2, "name": "Walking Boot Fitting", "description": "Fitting and adjustment of a walking boot or ankle brace.", "prep_instructions": "Wear a loose sock and bring the boot or brace if you already have one."}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo $SID
docker compose stop temporal-worker
curl -s -X POST $B/services/$SID/publish -H "Authorization: Bearer $ADMIN"; echo
publish_status $SID                                   # PUBLISHING, and nothing runs it
docker compose start temporal-worker
until [ "$(publish_status $SID)" = PUBLISHED ]; do sleep 1; done; publish_status $SID
sql "SELECT c.id AS chunk, e.id AS vector, e.published FROM content_chunks c LEFT JOIN chunk_embeddings e ON e.chunk_id = c.id WHERE c.source_type = 'SERVICE' AND c.source_id = $SID"
```

The publish answers 202 with `PUBLISHING` and stays there while the worker is
down. About 9 s after the worker starts it reaches `PUBLISHED`, with one chunk
and one vector, published. Show the workflow's history in the Temporal UI.

**Say:** Temporal stored the workflow when it started. The worker picked it up
from that history and ran each activity once; every activity is safe to run
again, so a crash part-way leaves no duplicate chunk.

### 2.2 Book, then send the same booking again (1 min)

```bash
read SLOT PROV <<< "$(open_slot 4)"; echo $SLOT
curl -s -X POST $B/appointments -H "Authorization: Bearer $P1" -H 'Content-Type: application/json' -H "Idempotency-Key: demo-1" -H "X-Request-ID: demo-booking-1" -d '{"service_id":4,"slot_id":'$SLOT',"provider_id":'$PROV'}'; echo
sleep 1; status 1                                     # CONFIRMED
book 4 $SLOT $PROV demo-1                             # the same key: appointment 1 again
sql "SELECT (SELECT count(*) FROM appointments) AS appointments, (SELECT count(*) FROM billing) AS billing_rows"
```

The first call answers 202 `REQUESTED` and is `CONFIRMED` a second later. The
repeat returns appointment 1, now `CONFIRMED`: one appointment, one billing
row.

**Say:** the client sends an `Idempotency-Key`, and a repeat returns the
original appointment instead of starting a second saga.

### 2.3 Fifty bookings for one slot (1 min)

```bash
read SLOT PROV <<< "$(open_slot 4)"; echo $SLOT
for i in $(seq 50); do book 4 $SLOT $PROV race-$i > /dev/null & done; wait
sleep 8; sql "SELECT status, count(*) FROM appointments WHERE slot_id = $SLOT GROUP BY status"
```

`CONFIRMED 1`, `REJECTED 49`, all 50 sent in about 6 s.

**Say:** reserving is one conditional `UPDATE ... WHERE status = 'AVAILABLE'
RETURNING id`. Postgres lets one request change the row; the other 49 get no
row back and are rejected. A SELECT, then a check, then an UPDATE would let
several read `AVAILABLE` at once (`docs/design.md` section 7).

### 2.4 Billing fails, and the slot comes back (1.5 min)

Swap in a worker whose billing check always fails:

```bash
docker compose stop temporal-worker
docker compose run -d --rm --name demo-billing-fails -e BILLING_FORCE_FAIL=true temporal-worker
read SLOT PROV <<< "$(open_slot 4)"; echo $SLOT
AID=$(book 4 $SLOT $PROV comp-1 | python -c 'import sys,json;print(json.load(sys.stdin)["id"])'); echo $AID
sleep 10; status $AID                                 # CANCELLED
sql "SELECT from_status, to_status, actor, reason FROM appointment_status_history WHERE appointment_id = $AID ORDER BY id"
open_slot 4                                           # the same slot is open again
docker stop demo-billing-fails; docker compose start temporal-worker
```

The booking goes `REQUESTED`, `SLOT_RESERVED`, then `CANCELLED` by
`SAGA_COMPENSATION` with the reason "billing pre-check failed", 9 s after it
was sent. The slot is `AVAILABLE` and first in the open-slots list again.

**Say:** compensation restores real state: it releases the slot it reserved.
The appointment row is moved to `CANCELLED`, never deleted, so its history
stays.

### 2.5 The analytics agree with the tables (30 s)

```bash
curl -s "$B/analytics/summary" -H "Authorization: Bearer $FRONT"; echo
curl -s "$B/analytics/reconciliation" -H "Authorization: Bearer $ADMIN"; echo
```

`appointments_booked` 2, `cancellations` 1, `cancellation_rate` 0.5,
`failed_jobs` 0, and reconciliation `"in_sync": true` with no drifted days.

**Say:** the consumer is the only writer of the aggregates, and it claims each
event's id first, so a replay changes nothing. Reconciliation counts the raw
tables and compares.

### 2.6 One booking's story, by request id (30 s)

```bash
trace demo-booking-1
```

The saga's four steps in the worker (slot reserved, billing passed, reminder
queued, confirmed), the reminder sent by Celery, and three events processed by
the consumer, in time order.

**Say:** the middleware takes the `X-Request-ID`, and it travels into the
Temporal workflow, the Celery task and each Kafka event. The log lines carry
ids, never names.

---

## 3. Part B (7 min)

### 3.1 Search by meaning (45 s)

```bash
curl -s -X POST $B/search -H "Authorization: Bearer $P1" -H 'Content-Type: application/json' -d '{"query":"I need a scan of my knee","top_k":3}'; echo
curl -s -X POST $B/search -H "Authorization: Bearer $P1" -H 'Content-Type: application/json' -d '{"query":"laser eye surgery","top_k":3}'; echo
```

Knee MRI Scan 0.694, Knee X-Ray 0.614, Orthopaedic Consultation 0.386. Laser
eye surgery returns nothing, with "We don't offer a service that matches this
search".

**Say:** the published filter and the 0.31 threshold are in the SQL, so an
unpublished service can never take a place in the results. The threshold was
chosen with an eval set (`docs/ai-layer.md` section 7).

### 3.2 A grounded recommendation (45 s)

```bash
ask "Do you offer knee scans?"
```

`ANSWERED`, citing Knee MRI Scan, Knee X-Ray and Orthopaedic Consultation: "Yes,
MediNova offers a Knee MRI Scan and a Knee X-Ray. You can also book an
Orthopaedic Consultation...", then the disclaimer.

**Say:** the model sees only the services retrieval found, and may recommend
nothing else.

### 3.3 "Diagnose me" (1 min)

```bash
ask "Can you diagnose me? I have had a headache for three days."
ask "Can you diagnose my rash?"
```

Both `REFUSED` with "I can't give medical advice...", urgent-care advice and
the disclaimer. The first matches no service, so it suggests a consultation;
the second routes to Full Skin Check (Dermatology).

**Say:** a safety check labels the question first. A refusal is fixed text,
and the code adds the disclaimer, so the behaviour graded highest never
depends on the model's wording. The answer prompts forbid diagnosis too, as a
second guard.

### 3.4 A preparation answer from the patient's own booking (1 min)

```bash
ask "How should I prepare for my appointment?"
```

"You have two confirmed Cardiology Consultations on Friday 23 October 2026.
For both, please bring any letters or reports from earlier tests and arrive
15 minutes early to register."

### 3.5 The same question from another patient (1 min)

```bash
ask "How should I prepare for my appointment?" $P2
sql "SELECT i.id, u.email, i.intent, i.retrieved_ids->'appointments' AS appointments FROM ai_interactions i JOIN users u ON u.id = i.user_id ORDER BY i.id DESC LIMIT 2"
```

"I don't see any appointments listed for you." The rows show patient one's
answer built from appointments `[1, 2]` (sections 2.2 and 2.3) and patient
two's from `[]`.

**Say:** the appointments are loaded by a SQL query filtered on the token's
user, before the prompt is built. Nothing in the question can widen it, and
the prompt is not what keeps one patient's data from another.

### 3.6 A streamed utilisation report (1.5 min)

```bash
FROM=$(date -u +%F); TO=$(date -u -d '+7 days' +%F)
curl -sN -X POST $B/reports/generate/utilisation -H "Authorization: Bearer $FRONT" -H 'Content-Type: application/json' -d '{"start_date":"'$FROM'","end_date":"'$TO'"}'
curl -s "$B/analytics/summary?start_date=$FROM&end_date=$TO" -H "Authorization: Bearer $FRONT"; echo
```

About 130 `token` events as the model writes its commentary, then one
`report` event, then `done`. The report's analytics figures equal the summary
(2 booked, 1 cancelled), and Cardiology shows 2 of 64 slots booked.

**Say:** every number is read by the code; the model writes the commentary
only, and a digit in it fails validation and gets one repair retry. The range
covers the coming week because department figures count slots that take place
in it, while the analytics count bookings made in it.

### 3.7 Optional: booking while AI runs, and the rate limit (1 min)

```bash
read SLOT PROV <<< "$(open_slot 4)"
for i in 1 2 3; do curl -sN -o /dev/null -w "report $i: %{time_total} s\n" -X POST $B/reports/generate/utilisation -H "Authorization: Bearer $FRONT" -H 'Content-Type: application/json' -d '{}' & done
curl -s -o /dev/null -w 'booking: %{http_code} in %{time_total} s\n' -X POST $B/appointments -H "Authorization: Bearer $P1" -H 'Content-Type: application/json' -H "Idempotency-Key: during-ai-1" -d '{"service_id":4,"slot_id":'$SLOT',"provider_id":'$PROV'}'; wait
for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} ' -X POST $B/assistant/ask -H "Authorization: Bearer $P3" -H 'Content-Type: application/json' -d '{"question":""}'; done; echo
curl -s -i -X POST $B/assistant/ask -H "Authorization: Bearer $P3" -H 'Content-Type: application/json' -d '{"question":"Do you offer physiotherapy?"}' | grep -i -E "^HTTP|^retry-after|RATE_LIMITED"
```

The booking answers 202 in 0.14 s while three reports stream for 0.72 to
0.82 s. Ten empty questions get 422 and still count, so the eleventh request
gets 429 `RATE_LIMITED` with a `Retry-After` header, without spending a token.

---

## 4. What I would do differently (3 min)

- **Check the model provider's limits on day one.** Groq's free tier, 8,000
  tokens a minute, caps the whole clinic at about 11 questions a minute. I
  measured it on the last day.
- **Run a clean clone every Friday.** On Week 5 Day 5 it found, in an hour,
  four out-of-date README steps and no way to find a slot to book. Both had
  been there for weeks.
- **Run the embedding model outside the API.** Booking's response went from
  under 100 ms to up to 656 ms while AI requests ran, because they share the
  API's CPU.
- **Count analytics by the clinic's day, not UTC, from the start.** Changing it
  now touches every event handler and needs a backfill.

## 5. Questions (2 min)

Likely questions, with where the full answer lives:

| Question | Short answer | Where |
|---|---|---|
| Why a conditional UPDATE, not SELECT then UPDATE? | Two requests can both read `AVAILABLE`; the UPDATE's `WHERE` lets only one change the row | `docs/design.md` section 7 |
| Why is booking a Temporal saga but visits are not? | Booking has steps that must be undone on failure; a visit is a person changing a status | README, "Division of labour" |
| Why is the refusal fixed text? | It never depends on the model's wording, and the disclaimer is never missing | `docs/design.md`, Week 5 decisions |
| What keeps one patient's data from another? | The SQL filter on the token's user, applied before the prompt is built | `docs/ai-layer.md` section 12 |
| Why does `ai_interactions` hold no question text? | The question is the patient's own words; a wrong answer is traced from ids and prompt versions | `docs/ai-layer.md` section 11 |
| What happens when Groq is down? | A 503 after one retry, at most 41 s, recorded `FAILED`; booking is unaffected | `docs/ai-layer.md` section 19 |
