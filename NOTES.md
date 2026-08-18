# NOTES

Working log: what I tried, what confused me, decisions I made and why.
Kept as I go, not written up at the end of the week.

---

## Tracking tables

### Part A (Weeks 1–3)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 1 | Foundation & core domain | Auth + roles + patient-data protection, providers/services/slots CRUD, migrations, seed, 80% coverage | ☐ |
| 2 | Temporal, scheduling & slots | No double-booking, no duplicate booking, publish workflow + scheduling saga with compensation, chunks produced, 80% coverage | ☐ |
| 3 | Async, events, observability | Celery reminders/rollup with DLQ, events consumed idempotently, accurate analytics, correlation IDs (no PHI), `/metrics`, 80% coverage | ☐ |

### Part B (Weeks 4–5)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 4 | Chunking, embeddings, retrieval | Published-filtered + PHI-scoped semantic search with threshold, clean re-indexing, eval results documented | ☐ |
| 5 | AI assistant & streaming | Medical-advice refusal, grounded cited recommendations, PHI scoping, report validated, SSE streaming, AI analytics, final demo | ☐ |

---

## Week 1 — Foundation

### Day 1 — 2026-08-18

**Goal:** `docker compose up` boots a FastAPI app talking to Postgres, and
`alembic upgrade head` runs clean. No domain features.

**Done**

- Read the three assignment documents; summarised the requirements into
  `CLAUDE.md` (git-ignored working context).
- Read the starter `docker-compose.yml` line by line.
- Project skeleton from Appendix A: `app/{core,db,models,schemas,api/v1,services}`,
  `tests/`, `docs/`, `scripts/`.
- `requirements.txt` with pinned versions; `Dockerfile`; `.dockerignore`.
- Added the `api` service to Compose (build from our Dockerfile, `.env` injected,
  source bind-mounted, `depends_on` Postgres and Redis being *healthy*).
- `app/core/config.py` (pydantic-settings), `app/db/session.py`, `app/db/base.py`,
  `app/main.py` with `GET /health` and a temporary `GET /health/db`.
- Alembic wired up; first revision `a589f9dbe585` enables the pgvector extension.
- Verified `upgrade head` → `downgrade base` → `upgrade head` round-trips cleanly.

**Decisions and why**

| Decision | Why | Tradeoff |
|---|---|---|
| Pin every dependency version | The image must build identically for me today and for my mentor next month | Manual bumps later |
| One image, several roles (`command:` differs per service) | The API, Temporal worker, Celery worker and consumer share the same code and dependencies | The image carries libraries a given role may not use |
| `COPY requirements.txt` before `COPY . .` | Docker caches layers; editing a `.py` file then doesn't reinstall every dependency | Slightly non-obvious to a reader |
| Config only through one `settings` object | One place knows how the app is configured; a missing variable fails loudly at boot rather than mid-request | — |
| `extra="ignore"` on Settings | `.env` already carries Weeks 2–5 keys the Week 1 class doesn't declare | A genuinely misspelled variable is silently ignored |
| `DATABASE_URL` kept out of `alembic.ini`, injected from settings in `env.py` | A connection string is a secret and `alembic.ini` is committed; it also lets the same migrations run against a test database | `alembic.ini` alone is not enough to run Alembic |
| First migration enables pgvector | The assignment requires the extension be enabled by a migration, not by hand, so a clean clone needs no manual step | Ships a Week 4 concern in Week 1 |
| `/health` checks nothing but the process | Liveness must stay fast and must not fail because a dependency is slow — that is what `/health/ready` is for in Week 3 | Two health endpoints instead of one |
| Bind-mount source + `--reload` | Edits on Windows restart the server in the container immediately | Development only; a production-shaped image must run without the mount |

**What confused me / cost time**

- `docker` was not on PATH — Docker Desktop needed to be running, and PowerShell
  needed restarting to pick up the environment.
- `alembic upgrade` failed with `KeyError: 'url'`. Cause: I commented
  `sqlalchemy.url` out of `alembic.ini` but had not yet added
  `config.set_main_option("sqlalchemy.url", settings.database_url)` to
  `migrations/env.py`, so nothing supplied the URL. Lesson: `engine_from_config`
  reads the `[alembic]` ini section, and `set_main_option` writes into that same
  section at runtime.

**Things I want to be able to explain out loud**

- Why `postgres:5432` inside a container and `localhost:5432` from the laptop:
  inside a container `localhost` means the container itself; Compose gives every
  service a DNS name equal to its service name on a shared private network.
- Why Kafka advertises two listeners (`kafka:9092` and `localhost:29092`): a
  broker tells clients the address to reach it on, and containers and the host
  need different answers, so one broker opens two doors.
- Why `depends_on: condition: service_healthy` rather than plain `depends_on`:
  a container can be *running* but not yet *accepting connections*.
- Why migrations rather than `create_all()`: `create_all()` only creates missing
  tables and can never alter an existing one, so the schema silently drifts.

**Carrying into Day 2**

- Design the data model on paper first and get it reviewed *before* coding it
  (task 1.2) — the guidelines call skipping this the mistake that forces a
  schema rewrite in Week 2.
- Then models: `User`, `Patient`, `Provider`, `Department`, `Service`, `Slot`.

**Open questions for my mentor**

- Confirm a single clinic is fine for `departments.clinic` in Week 1.
- Confirm the expected slot granularity for the seed data (15 / 30 minutes?).

---

## Weekly self-check

Answered honestly every Friday.

### Week 1 — (to complete Friday)

1. What did I finish, and what is genuinely still broken?
2. Which part of this week do I not fully understand yet?
3. What did I spend the most time on, and was that time well spent?
4. What am I carrying into next week?
