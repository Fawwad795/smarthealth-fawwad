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

### Day 2 — 2026-08-22

**Goal:** the full Week 1 schema exists as models and as a migration, with a
test harness proving the migration builds it. No endpoints, no auth.

**Done**

- ERD on paper, reviewed and sent to mentor. Grew from six entities to nine:
  `Specialty` and `Clinic` became tables, and `ProviderSchedule` was added.
  Written up in `docs/design.md` with what each table is *and is not*.
- Constraint naming convention on `Base.metadata`; `TimestampMixin`;
  `UserRole` / `ServiceStatus` / `SlotStatus` plus the `enum_column()` helper;
  the model registry in `app/models/__init__.py`.
- The nine models, with 16 relationships and every FK `ON DELETE RESTRICT`.
- Two migrations: `btree_gist`, then the nine tables (34 constraints).
  Round-tripped `downgrade base` → `upgrade head` twice.
- `tests/conftest.py` — test database built by `alembic upgrade head`, per-test
  transaction rollback — and 12 tests, all passing.

**Decisions and why**

| Decision | Why | Tradeoff |
|---|---|---|
| Naming convention before the first table | Constraint names must be derivable from a rule, not discovered by inspecting my own database, or a migration that drops one breaks on someone else's | Retrofitting later means renaming ~30 constraints by hand |
| Enums as `VARCHAR` + `CHECK`, not native `ENUM` | Postgres has no `DROP VALUE`, so downgrading a value addition means recreating the type and re-casting the column | More storage; alphabetical sorting; no type shared across tables |
| `StrEnum`, names equal to values | These strings are written into rows and event payloads; an f-string must render `AVAILABLE`, not `SlotStatus.AVAILABLE` | Slightly redundant to look at |
| Timestamps generated by Postgres | By Week 3 four processes write rows and ordering them needs one clock; seed scripts and psql inserts get stamped too | `onupdate` only fires on ORM updates — see below |
| `EXCLUDE` for slot overlap | The unique constraint permits overlapping rows with different start times; checking in the generator is a check-then-act gap | Needs `btree_gist`; GiST serves ordered range scans poorly, so the unique constraint stays for its B-tree index |
| Test DB dropped and recreated every run | Alembic will not re-apply a revision it has recorded, so a reused database keeps an old schema after a migration is edited | A couple of seconds per run |
| Tests split by infrastructure needed, not by scope | Weeks 4–5 require the suite to pass with no network; the boundary is cheaper to establish now than across 25+ tests | Two directories to keep straight |

**What confused me / cost time**

- `Enum(..., native_enum=False)` alone produces an **unconstrained VARCHAR** —
  `create_constraint=True` is not the SQLAlchemy default. Compiled both to see
  it. This is why every enum column goes through one helper.
- `UNIQUE (provider_id, start_time)` looks like it prevents double-booking and
  does not: 09:00–09:30 and 09:15–09:45 have different start times.
- `CHECK (end_time > start_time)` is not cosmetic. An empty range overlaps
  nothing in Postgres, so a zero-length slot would slip past the exclusion
  constraint entirely. The two constraints cover for each other.
- `migrations/env.py` unconditionally overwrote `sqlalchemy.url` with the dev
  database, so the test harness could not point migrations anywhere else.

**Things I want to be able to explain out loud**

- Why `EXCLUDE` closes a gap the atomic `UPDATE` cannot: that statement
  protects one row from concurrent access to itself and says nothing about two
  different rows overlapping in time.
- Why `onupdate` is a SQLAlchemy mechanic, not a database default — proved it:
  an ORM update moves `updated_at`, a raw `UPDATE` does not. **The Week 2
  reservation is raw SQL, so it must set `updated_at = now()` itself.**
- Why a model missing from `app/models/__init__.py` is not merely invisible:
  if its table exists, autogenerate reports "Detected removed table" and emits
  `op.drop_table()`. Confirmed on a throwaway table.
- Why the tests assert constraint *names*: without `match=`, any
  `IntegrityError` passes, including one from a typo in the test data. Verified
  by asserting the wrong name and watching the test fail.

**Carrying into Day 3**

- Auth: password hashing, JWT, role dependencies, and the PHI-scoping rule that
  `uq_patients_user_id` now makes well-defined.
- Emails must be lowercased at the service boundary; lookups must be written
  `WHERE lower(email) = :email` or the index is not used.

**Open questions for my mentor**

- `ix_services_department_id` and `ix_providers_department_id` are redundant
  with the composite uniques whose leading column they duplicate. Keep for
  clarity, or drop the write cost?
- Should `provider_services` be modelled now rather than deferred to Week 2?

---

## Weekly self-check

Answered honestly every Friday.

### Week 1 — (to complete Friday)

1. What did I finish, and what is genuinely still broken?
2. Which part of this week do I not fully understand yet?
3. What did I spend the most time on, and was that time well spent?
4. What am I carrying into next week?
