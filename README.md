# SmartHealth

A backend for healthcare **operations** and patient engagement: appointments,
provider schedules, services and simulated billing pre-checks — plus (from Week 4)
an AI layer that helps patients find the right service.

This is deliberately **not** a clinical system. There are no diagnoses,
prescriptions, lab results or medical records anywhere in it, and the AI
assistant refuses to give medical advice by design.

It is also **API only**. There is no UI; Swagger UI at `/docs`, curl or Postman
is the interface.

> Status: **Week 1 — foundation, self-check complete**. See
> [Project status](#project-status) for what works today.

---

## What problem it solves

| # | Problem | Addressed by |
|---|---|---|
| 1 | Bookings are slow, and two patients can hold the same slot | Atomic slot reservation + a Temporal scheduling saga (Week 2) |
| 2 | Patients can't find the right service | Retrieval-augmented assistant (Weeks 4–5) |
| 3 | Dashboards disagree with the tables | Pre-aggregated analytics maintained by an event consumer, plus a reconciliation script (Week 3) |
| 4 | Peak-window timeouts | Slot reservation, billing and reminders moved out of the HTTP request (Weeks 2–3) |
| 5 | Background failures are invisible | Structured logs with a correlation ID, `/metrics`, and a dead-letter table (Week 3) |

---

## Architecture

```
                    ┌──────────────┐
   curl / Postman ──▶│  FastAPI API │
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  ┌───────────┐      ┌───────────┐      ┌───────────┐
  │ PostgreSQL│      │   Redis   │      │  Temporal │   (Week 2)
  │ +pgvector │      │ cache /   │      │  server   │
  └───────────┘      │ broker /  │      └─────┬─────┘
                     │ idem keys │            │
                     └─────┬─────┘            ▼
                           │            ┌──────────────┐
                           ▼            │ Temporal     │
                    ┌─────────────┐     │ worker       │
                    │ Celery      │     │ (workflows + │
                    │ worker      │     │  activities) │
                    │ (Week 3)    │     └──────────────┘
                    └─────────────┘
                           │
                           ▼
                    ┌─────────────┐     ┌──────────────┐
                    │   Kafka     │────▶│  Consumer    │──▶ analytics tables
                    │  (Week 3)   │     │  (idempotent)│
                    └─────────────┘     └──────────────┘
```

**Division of labour.** Temporal owns the multi-step durable workflows (service
publishing, the appointment scheduling saga). Celery owns fire-and-forget work
(reminders, the analytics rollup). The visit lifecycle is neither — it is a plain
validated status flow driven by human actions.

A proper diagram and the ERD land in `docs/design.md` at the end of Week 1.

---

## Running it

**Prerequisites:** Docker Desktop. Nothing else — no local Python needed.

```bash
# 1. configure
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into JWT_SECRET

# 2. start (Week 1 needs postgres + redis + api)
docker compose up -d --build api

# 3. build the schema
docker compose exec api alembic upgrade head

# 4. load a synthetic demo dataset (clinic, departments, providers + schedules,
#    published services, patients) -- idempotent, safe to re-run
docker compose exec api python -m scripts.seed

# 5. check
curl http://localhost:8000/health
```

Then open <http://localhost:8000/docs>.

| Command | What it does |
|---|---|
| `docker compose up -d --build api` | API + Postgres + Redis |
| `docker compose up -d` | the above + Temporal and its UI (Week 2) |
| `docker compose --profile week3 up -d` | the above + Kafka, Kafka UI, Prometheus (Week 3) |
| `docker compose logs -f api` | tail the API logs |
| `docker compose down` | stop, keep data |
| `docker compose down -v` | stop and wipe all data |

### Where things listen

Ports differ depending on whether you are inside the Compose network or on your
own machine. Using `localhost` from inside a container is the most common mistake
in this project.

| Service | From a container | From your machine |
|---|---|---|
| API | `api:8000` | <http://localhost:8000> |
| Postgres | `postgres:5432` | `localhost:5432` |
| Redis | `redis:6379` | `localhost:6379` |
| Temporal | `temporal:7233` | `localhost:7233` |
| Temporal UI | — | <http://localhost:8233> |
| Kafka | `kafka:9092` | `localhost:29092` |
| Kafka UI | — | <http://localhost:8080> |
| Prometheus | `prometheus:9090` | <http://localhost:9090> |

---

## Database migrations

Alembic owns the schema. There is no `create_all()` in the codebase — a clean
clone builds the whole database with `alembic upgrade head` and nothing else.

Run Alembic **inside the container**, because `DATABASE_URL` uses the hostname
`postgres`, which only resolves on the Compose network:

```bash
docker compose exec api alembic upgrade head                        # apply
docker compose exec api alembic current                             # where am I?
docker compose exec api alembic history                             # the chain
docker compose exec api alembic revision --autogenerate -m "add X"  # draft a migration
docker compose exec api alembic downgrade -1                        # undo one
```

Always read an autogenerated migration before applying it, and always write a
working `downgrade()`.

---

## Tests

```bash
docker compose exec api pytest
docker compose exec api pytest --cov=app --cov-report=term-missing
```

Target is ≥25 meaningful tests and ≥80% coverage; Week 1 stands at 142 tests and
98% coverage, no network access required. Later weeks add the concurrency/saga/
event hard cases (slot double-booking, saga compensation, idempotent event
handling) that Week 1's domain doesn't have yet.

---

## API overview

| Method | Path | Purpose | Access |
|---|---|---|---|
| GET | `/health` | Liveness | Public |
| GET | `/health/db` | Temporary: confirms the API can reach Postgres | Public |
| GET | `/docs` | Swagger UI | Public |
| POST | `/api/v1/auth/register` | Patient self-registration | Public |
| POST | `/api/v1/auth/login` | Issue a JWT | Public |
| GET | `/api/v1/auth/me` | Current authenticated user | Any role |
| POST/GET/PATCH | `/api/v1/departments`, `/departments/{id}` | Department CRUD | Admin write, any staff read |
| POST/GET/PATCH | `/api/v1/services`, `/services/{id}` | Service CRUD (always created `DRAFT`) | Admin write, any staff read |
| GET | `/api/v1/services/search` | Public catalog: published + offered, filterable | Public |
| POST/GET/PATCH | `/api/v1/providers`, `/providers/{id}` | Provider profile CRUD | Admin write, any staff read |
| POST/GET/PATCH | `/api/v1/providers/{id}/schedules`, `/schedules/{id}` | Weekly working-hours templates | Admin write, any staff read |
| POST | `/api/v1/providers/{id}/schedules/generate-slots` | Turn templates into bookable `Slot` rows, idempotent | Admin |

Slots, appointments, analytics, and the AI assistant are added week by week and
documented here as they land.

---

## Configuration

All configuration comes from environment variables. `.env.example` is committed
and kept accurate; the real `.env` is git-ignored and never committed.

| Variable | Purpose |
|---|---|
| `APP_ENV`, `LOG_LEVEL`, `LOG_FORMAT` | Runtime behaviour and logging |
| `DATABASE_URL` | Postgres connection string |
| `REDIS_URL` | Cache, idempotency keys, rate limiting |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Celery (Week 3) |
| `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | Auth |
| `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE` | Temporal (Week 2) |
| `KAFKA_BOOTSTRAP_SERVERS`, `KAFKA_CONSUMER_GROUP`, `KAFKA_TOPIC_PREFIX` | Events (Week 3) |
| `LLM_*`, `EMBEDDING_*`, `RETRIEVAL_*`, `AI_*` | AI layer (Weeks 4–5) |

See `.env.example` for the full list with defaults and comments.

---

## Project layout

```
app/
  main.py         app factory, middleware, routers
  core/           config, security, logging, dependencies, errors, metrics
  db/             declarative Base, engine, session
  models/         SQLAlchemy ORM — database shape only
  schemas/        Pydantic request/response models
  api/v1/         routers: parse input, call a service, shape the response
  services/       business rules
migrations/       Alembic revisions
tests/            unit/ and integration/
docs/             design, events, runbook, ai-layer, prd
scripts/          seed, reconcile_analytics, eval_retrieval
```

The layering is the point: routers hold no business logic and no SQL, services
hold the rules, and an ORM object is never returned from an endpoint (it would
leak `password_hash` and patient data).

---

## Documentation

| Document | Contents |
|---|---|
| `docs/design.md` | Data model / ERD, module breakdown, publish workflow and scheduling saga, the slot concurrency approach, decisions and tradeoffs |
| `docs/events.md` | Every event, its schema, producer, consumer, idempotency guarantee |
| `docs/runbook.md` | Diagnosing the most likely failures |
| `docs/ai-layer.md` | Chunking, retrieval, prompts, evaluation, transcripts |
| `docs/prd.md` | PRD with the requirement → implementation → test traceability table |
| `NOTES.md` | Working log and weekly tracking tables |

---

## Project status

| Week | Theme | Status |
|---|---|---|
| 1 | Foundation and core domain | Done |
| 2 | Temporal workflows, scheduling, slots | Not started |
| 3 | Celery, Kafka, observability | Not started |
| 4 | Chunking, embeddings, retrieval | Not started |
| 5 | AI assistant, streaming, demo | Not started |

**Working today:** the full Week 1 domain — auth (register/login/roles),
department/service/provider CRUD, provider schedules + idempotent slot
generation, the public service search, and a synthetic seed script — all
behind real routes, all role/ownership-checked, 142 tests at 98% coverage.

**Not built yet:** appointments, the Temporal publish workflow and scheduling
saga, Celery/Kafka, analytics, and the AI layer — Weeks 2–5.
