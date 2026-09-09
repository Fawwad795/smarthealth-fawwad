# NOTES

Working log: what I tried, what confused me, decisions I made and why.
Kept as I go, not written up at the end of the week.

---

## Tracking tables

### Part A (Weeks 1-3)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 1 | Foundation & core domain | Auth + roles + patient-data protection, providers/services/slots CRUD, migrations, seed, 80% coverage | ☑ 142 tests, 98% - PR #1 approved, deliberately left unmerged |
| 2 | Temporal, scheduling & slots | No double-booking, no duplicate booking, publish workflow + scheduling saga with compensation, chunks produced, 80% coverage | ☑ 264 tests, 98% - 2.1-2.13 complete |
| 3 | Async, events, observability | Celery reminders/rollup with DLQ, events consumed idempotently, accurate analytics, correlation IDs (no PHI), `/metrics`, 80% coverage | ☐ |

### Part B (Weeks 4-5)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 4 | Chunking, embeddings, retrieval | Published-filtered + PHI-scoped semantic search with threshold, clean re-indexing, eval results documented | ☐ |
| 5 | AI assistant & streaming | Medical-advice refusal, grounded cited recommendations, PHI scoping, report validated, SSE streaming, AI analytics, final demo | ☐ |

---

## Week 1 - Foundation

### Day 1 - 2026-08-18

**Goal:** `docker compose up` + `alembic upgrade head` clean. No domain features.

**Done**

- Read the 3 assignment docs, summarised into `CLAUDE.md`.
- Project skeleton (Appendix A layout), pinned `requirements.txt`, `Dockerfile`.
- `api` service in Compose; `core/config.py`, `db/session.py`, `db/base.py`,
  `main.py` with `/health` and a temporary `/health/db`.
- First Alembic revision enables pgvector. Round-tripped upgrade/downgrade.

**Decisions**

| Decision | Why |
|---|---|
| Pin every dependency version | Reproducible build |
| One image, per-service `command:` | Shared code/deps across API/worker/consumer |
| `COPY requirements.txt` before `COPY . .` | Docker layer caching |
| Config only via one `settings` object | Single source of truth, fails loudly if missing |
| `extra="ignore"` on Settings | `.env` carries later-week keys not yet declared |
| `DATABASE_URL` injected in `env.py`, not `alembic.ini` | Secret, and lets migrations target a test DB |
| pgvector enabled in first migration | No manual step on a clean clone |
| `/health` checks nothing but the process | Stays fast; `/health/ready` is Week 3 |
| Bind-mount + `--reload` | Fast local iteration |

**Cost time**

- `docker` not on PATH - needed a restart.
- `alembic upgrade` `KeyError: 'url'` - `sqlalchemy.url` never set in `env.py`.

**Explain out loud**

- `postgres:5432` (container) vs `localhost:5432` (host) - Compose DNS.
- Kafka's two listeners - container and host clients need different addresses.
- `service_healthy` vs plain `depends_on` - running is not the same as ready.
- Migrations vs `create_all()` - the latter never alters an existing table.

**Carrying into Day 2**

- Data model on paper first (task 1.2).
- Models: User, Patient, Provider, Department, Service, Slot.

**Open questions**

- Single clinic OK for Week 1?
- Slot granularity for seed data (15 / 30 min)?

---

### Day 2 - 2026-08-22

**Goal:** Full Week 1 schema as models + migration, with a test harness. No
endpoints, no auth.

**Done**

- ERD reviewed with mentor; grew to 9 tables (added `Specialty`, `Clinic`,
  `ProviderSchedule`). Documented in `docs/design.md`.
- Naming convention on `Base.metadata`; `TimestampMixin`; enums via
  `enum_column()`.
- 9 models, 16 relationships, all FKs `ON DELETE RESTRICT`.
- 2 migrations (`btree_gist`, then 9 tables / 34 constraints). Round-tripped
  twice.
- `tests/conftest.py` (migration-built DB, per-test rollback) + 12 passing
  tests.

**Decisions**

| Decision | Why |
|---|---|
| Naming convention before the first table | Constraint names derivable, portable |
| Enums as VARCHAR + CHECK, not native ENUM | Postgres has no `DROP VALUE` |
| `StrEnum`, names equal to values | Written into rows/events as-is |
| Timestamps via Postgres `now()` | One clock across future processes |
| `EXCLUDE` constraint for slot overlap | A unique constraint alone allows overlaps |
| Test DB dropped/recreated each run | Alembic won't re-apply an edited revision |
| Tests split unit / integration | Weeks 4-5 need a no-network suite |

**Cost time**

- `native_enum=False` alone gives an unconstrained VARCHAR - needs
  `create_constraint=True`.
- `UNIQUE(provider_id, start_time)` does not prevent overlapping slots.
- `CHECK(end_time > start_time)` needed - an empty range bypasses `EXCLUDE`.
- `env.py` hardcoded the dev DB URL, blocking test DB use.

**Explain out loud**

- `EXCLUDE` vs the atomic `UPDATE` - different rows vs the same row.
- `onupdate` is SQLAlchemy-side; raw SQL must set `updated_at` itself.
- A model missing from `__init__.py` makes autogenerate think its table was
  deleted.
- Tests assert constraint *names*, not just `IntegrityError`.

**Carrying into Day 3**

- Auth: hashing, JWT, role deps, PHI-scoping.
- Emails lowercased at the service boundary; lookups via `lower(email)`.

**Open questions**

- Drop `ix_services_department_id` / `ix_providers_department_id` (redundant)?
- Model `provider_services` now or defer?

---

### Day 3 - 2026-08-23

**Goal:** Auth end-to-end - error shape, hashing, JWT, register/login, role
checks.

**Done**

- Resolved both Day 2 opens: dropped the redundant index, added
  `provider_services` as an explicit table.
- `AppError` + `error_handlers.py` - one JSON error shape across all 4
  failure origins.
- `security.py` - bcrypt hash/verify, JWT create/decode. Confirmed bcrypt
  truncates past 72 bytes.
- `dependencies.py` - `get_current_user` (`HTTPBearer(auto_error=False)` for
  401 not 403), `require_role`, `ensure_patient_self_or_staff`.
- `POST /auth/register` (patient-only), `POST /auth/login` (one generic
  401), `GET /auth/me`.
- 12 → 40 tests, 8 commits, each verified before the next.

**Decisions**

| Decision | Why |
|---|---|
| Registration patient-only, no `role` field | A public ADMIN-minting endpoint would be a real hole |
| Login: one generic 401 for every failure reason | Distinguishing leaks which emails exist |
| `HTTPBearer(auto_error=False)` | Default missing-header response is 403, need 401 |
| `require_role` + `ensure_patient_self_or_staff` kept separate | Role is not "which patient's data" |
| Ownership check is a plain function, not `Depends()` | Needs an already-loaded `Patient` row |
| Password capped at 72 chars | bcrypt silently truncates past that |

**Cost time**

- `db.add(User)` instead of `db.add(user)` - caught in verification.
- `docker compose exec` mangled a `/tmp/...` path on Git Bash - fixed with
  `MSYS_NO_PATHCONV=1`.
- Script couldn't `import app.*` - fixed with `PYTHONPATH=/app`.

**Explain out loud**

- JWT payload is readable but trustworthy - the signature, not secrecy, is
  the guarantee.
- `get_current_user` re-checks `is_active` - a token stays valid after
  deactivation.

**Carrying into Day 4**

- Provider/service/department CRUD (1.7) - first real use of role/ownership
  checks.
- Public listing (1.8) - first real query on `provider_services`.
- Seed script (1.10).

**Open questions**

- None.

---

### Day 4 - 2026-08-24

**Goal:** Task 1.7 (dept/service/provider CRUD + schedules/slots), 1.8
(public search), 1.10 (seed).

**Done**

- **1.7a** Department CRUD - first router→service→schema pattern beyond
  auth, first `require_role` use.
- **1.7b** Service CRUD, always created DRAFT - `status`/`published_at`
  absent from writable schemas, verified live that injecting
  `"status": "PUBLISHED"` is ignored.
- **1.7c** Provider CRUD - validates `user_id` exists and is role PROVIDER;
  `ProviderUpdate` has no `user_id`.
- **1.7d** Provider schedules + `generate_slots` - combines local time +
  date + `Clinic.timezone` into UTC `Slot` rows. Idempotent action endpoint.
- Introduced `core/pagination.py` for a shared `{items, total, limit,
  offset}` shape.
- **1.8** `GET /services/search` - public, filters via SQL WHERE/EXISTS,
  declared before `/{service_id}` (route-order collision).
- **1.10** `scripts/seed.py` - clinic, 3 depts, 3 specialties, 3 providers
  with schedules, 2 weeks of slots, 3 published services, 3 patients.
  Idempotent. No appointments (model doesn't exist yet).
- Every piece verified via test file + live curl against the running
  container.
- 40 → 82 tests (7+8+7+11+7+2).

**Decisions**

| Decision | Why |
|---|---|
| `PaginationParams` split from `pagination_params` dependency | `Query(...)` only resolves inside a real request |
| Uniqueness checked via SELECT before INSERT | One `IntegrityError` can't distinguish 409 vs 404 |
| `has_available_slots` filters at provider level | `Slot` has no `service_id` by design |
| `EXISTS` subqueries, not `JOIN`, for search filters | Avoids row fanout/duplication |
| Seed run as `python -m scripts.seed` | Avoids the `sys.path[0]` script-directory problem |
| Seed uses get-or-create everywhere | Safe to re-run, no crash on existing data |

**Cost time**

- Docker Desktop stopped responding mid-session - restarted, no data lost.
- 2 bugs found in tests, not code: an overlap test collided with the wrong
  constraint; a `has_available_slots` test assumed slots were
  service-scoped (they're provider-scoped by design).

**Explain out loud**

- `datetime.combine(...).astimezone(utc)` - same instant, different clock
  face.
- Route declaration order - a literal path must precede a wildcard.
- `EXISTS` vs `JOIN` - fanout is invisible until the count is wrong.
- Seed bypasses the PATCH restriction on `status` - a DB script is a
  different trust boundary than the API.

**Carrying into Day 5**

- Coverage at 78%, under the 80% MUST - every router file at 0%,
  service-layer tests don't cover routes.
- 1.11 (TestClient tests per router) + 1.12 (README, ERD, design.md) are
  Friday's work.
- Mentor confirmed: full route-level coverage expected, not a sample.

**Open questions**

- None.

---

## Weekly self-check

Answered honestly every Friday.

### Week 1 - 2026-08-24

1. **Finished / broken:** Everything through 1.12, route-tested, 142 tests,
   98% coverage. Nothing known broken.
2. **Not fully understood yet:** Temporal's guarantees under a mid-workflow
   crash.
3. **Most time spent:** Friday's route-level test sweep - caught a flaky
   JWT test, closed the coverage gap.
4. **Carrying into Week 2:** the atomic slot-reservation UPDATE, the publish
   workflow, the scheduling saga with compensation.

---

## Week 1 review fixes - 2026-08-26

Post-review work on PR #1: 5 mentor points, plus the tooling that came out
of them.

**Done**

- 77 docstrings added (all routers, most schemas, get/list/update service
  functions). 31 hardcoded status codes → `fastapi.status`.
- One-shot `test` compose service + Makefile - `make test` runs from cold,
  no live API needed.
- `.claude/` committed, un-ignored, split: 587 → 185 lines in CLAUDE.md,
  rest in 7 path-scoped rule files.
- `scripts/convert_briefs.py` - the 3 briefs converted from `.docx` to
  greppable markdown in `.claude/reference/`.
- 4 skills: `start-day`, `stacked-pr`, `verify-endpoint`, `assignment-brief`.
- 142 tests still passing; no production behaviour changed.

**Decisions**

| Decision | Why |
|---|---|
| `fastapi.status` over stdlib | Matches existing convention in `error_handlers.py` |
| Split CLAUDE.md by path scope | Avoids loading Week 4 rules during Week 2 work |
| Non-negotiables stay in CLAUDE.md, detail in rules | Path-scoped rules only load on a matching file *read* |
| Briefs converted + committed | Read tool refuses `.docx`; hand-extracting XML each session was wasteful |
| Only 2 skills at first (`verify-endpoint`, `stacked-pr`) | Avoid stale/unused skills |
| PR #1 left unmerged, Week 2 stacks on it | Mentor approved but said carry on |

**Cost time**

- `make` not on PATH; msys64's `make.exe` mangles Docker CLI args - not a
  Makefile bug.
- PowerShell here-strings don't need doubled apostrophes - that rule is
  only for `-m '...'`.
- Docker Desktop unresponsive again mid-session.

**Explain out loud**

- `.claude/reference/` is committed but never auto-loaded - grepped on
  demand.
- `@import` would have reloaded the same 587-line problem, in full.
- `app/api/**` / `app/schemas/**` had zero rule coverage - the
  PHI-in-endpoint rule was scoped to `app/ai/**` and wouldn't have loaded
  for the Week 5 assistant endpoint.

**Carrying into Week 2**

- PR #1 approved but unmerged - Week 2 branches off `week-1-foundation`,
  not `main`.
- `gh` installed and authenticated.
- Branch protection doesn't dismiss stale approvals - worth enabling.
- Rule files are a distillation only - write back what Week 2 actually
  settles.

**Open questions**

- None.

---

## Week 2 - Temporal Workflows & Scheduling

### Day 1 - 2026-08-29

**Goal:** 2.1 (status transition guard, 409 on illegal entry) + 2.2
(Temporal worker wired up, trivial workflow end-to-end).

**Done**

- **2.1** `service_publish.py` - `ensure_can_publish` /
  `ensure_can_unpublish`. 10 unit tests, no database.
- **2.2** `app/temporal/` - `client.py`, `ping_workflow.py` (temporary),
  `worker.py`. New `temporal-worker` compose service. `temporalio==1.9.0`.
- Verified live: worker connected, `PingWorkflow` ran end-to-end ->
  `pong, Fawwad`.
- 142 -> 152 tests, lint clean, no hardcoded status codes.

**Decisions**

| Decision | Why |
|---|---|
| Guard in its own module (`service_publish.py`) | Keeps `service.py` Temporal-unaware |
| `INACTIVE` has no path back to `PUBLISHING` | Diagram only draws `PUBLISH_FAILED -> PUBLISHING` as retry |
| Compose service `temporal-worker`, not `worker` | `worker` already reserved for Week 3's Celery worker |
| `client.py` test deferred to 2.3 | Needs an async-test-infra call 2.3 forces anyway |

**Cost time**

- Named the compose service `worker`, collided with the file's own Week 3
  plan - caught on re-read, not by me first.
- `run_worker()` missing a docstring - caught by ruff (D103).
- `docker compose exec` mangled a path on Git Bash again -
  `MSYS_NO_PATHCONV=1`.
- `ruff check .` on the whole repo flagged `migrations/` - false alarm,
  `make lint` excludes it.

**Explain out loud**

- Workflow/Activity/Worker/Client - the kitchen analogy; durable execution
  resumes mid-recipe after a crash.
- `temporal:7233` only resolves inside the compose network, not the
  laptop.
- Same `client.py` file will run inside `api` too - one image, execution
  follows the importer.
- `@activity.defn`/`@workflow.defn` - registers a function with Temporal's
  SDK.
- `Client.connect()` is a real network call - why it has to be `async`.

**Carrying into Day 2**

- 2.3 - the real publish workflow, replaces `ping_workflow.py`.
- Decide Temporal test-infra (`pytest-asyncio` + `WorkflowEnvironment` vs.
  real container).
- 2.4 follows once 2.3 exists.

**Open questions**

- Should `INACTIVE` have a re-publish path?

---

### Day 2 - 2026-08-30

**Goal:** 2.3 (service publishing as a real Temporal Workflow) end to
end, then 2.4 (publish endpoints).

**Done**

- Decided Day 1's test-infra question: `pytest-asyncio` +
  `WorkflowEnvironment.start_time_skipping()`.
- `content_chunks` model + migration.
- `PublishActivities` - validate, structure, chunk, mark_published,
  mark_publish_failed. Class with an injectable `session_factory`.
- `PublishServiceWorkflow` - validate -> structure -> chunk ->
  mark_published, with a clean-failure branch for `SERVICE_INCOMPLETE`.
- Wired `worker.py` to the real workflow/activities; deleted
  `ping_workflow.py`.
- **2.4** `POST /services/{id}/publish` (202) + `GET publish-status`.
- Verified live: full publish, duplicate-publish 409, worker-restart
  resumption, validation failure -> `PUBLISH_FAILED`, retry after fix.
- 152 -> 167 tests.

**Decisions**

| Decision | Why |
|---|---|
| `WorkflowEnvironment`, not a real container | Proves the real Workflow definition, not a mock; time-skipping fast-forwards timers |
| `content_chunks` uses `source_type`/`source_id`, no FK | Matches the brief's generic schema |
| `PublishActivities` as a class + injectable `session_factory` | Activities have no `Depends(get_db)`; Temporal's documented DI pattern |
| `imports_passed_through()` around the activities import | Importing them pulls in the DB engine setup, which the sandbox rejects |
| Deterministic workflow id (`publish-service-{id}`) | Second guard against a concurrent double-publish |
| `GET publish-status` reads the DB row, not a live Temporal query | Simpler; Activities keep it in sync |

**Cost time**

- Sandbox rejected the workflow at startup (`RestrictedWorkflowAccessError`
  on `pathlib.Path.expanduser`) - importing `PublishActivities` pulled in
  the DB engine setup. Fixed with `imports_passed_through()`.
- `api`'s image was stale (built before `temporalio`) -
  `ModuleNotFoundError` until rebuilt.
- Broke the "code in chat, not written directly" rule once, writing a
  test file straight to disk - caught immediately.

**Explain out loud**

- Deterministic (Workflows) vs. idempotent (Activities) - different
  questions.
- Sync Activities need a `ThreadPoolExecutor` - one blocking call would
  freeze Temporal's whole event loop, not just itself.
- `session_factory` is dependency injection by hand - same idea as
  `Depends(get_db)`.
- Two independent guards against a double-publish: the status check
  (small race window) and Temporal's workflow-id uniqueness.

**Carrying into Day 3**

- 2.5 - concurrency-safe slot reservation (atomic conditional UPDATE).
- 2.6 - Appointment model, status enum, `appointment_status_history`
  migration.
- Weekly docs pass (2.13): publish race-window tradeoff,
  publish-status's known limitation.

**Open questions**

- Should `INACTIVE` have a re-publish path? (carried from Day 1)
- Should `GET publish-status` cross-check Temporal's own execution status
  instead of relying on the DB column alone?
- Is `POST /services/{id}/unpublish` expected this week, or does it wait
  until scheduled? Only `/publish` was named in 2.4.

---

### Day 3 - 2026-08-31

**Goal:** 2.5 (concurrency-safe slot reservation) + 2.6 (Appointment
model, status enum, appointment_status_history migration).

**Done**

- **2.5** `reserve_slot()` in `app/services/slot.py` - one conditional
  `UPDATE ... WHERE status = AVAILABLE ... RETURNING id`. Concurrency
  test: 50 threads race one slot, exactly one wins.
- **2.6** `AppointmentStatus` enum, `Appointment` model,
  `AppointmentStatusHistory` model (append-only), migration.
  `patient`/`service` fixtures added to `conftest.py`. 4 new
  schema-constraint tests (status enums, idempotency uniqueness, FK
  protecting history from a deleted appointment).
- 168 -> 172 tests, lint clean, migration round-tripped
  (upgrade/downgrade/upgrade).

**Decisions**

| Decision | Why |
|---|---|
| `reserve_slot` via Core `update().returning()`, not raw SQL | Matches the `select()` style used everywhere else in the codebase |
| `updated_at` set explicitly in that UPDATE | `onupdate` isn't guaranteed applied to a hand-written Core statement |
| Concurrency test opens its own engine sized for 50 connections | Shared `engine` fixture's default pool (5+10) would serialize most attempts before they reach Postgres |
| `idempotency_key` unique + NOT NULL on `Appointment` | DB-level backstop alongside Redis (2.7) |
| `AppointmentStatusHistory` a separate append-only table, not JSONB | Queryable, and matches "never delete, transition" (non-negotiable #4) |
| `actor` a plain string, not an enum/FK to users | Not every actor is a user - the saga itself causes transitions |

**Cost time**

- `reserve_slot` first draft dropped `.returning(Slot.id)` and
  `db.commit()` when typed by hand - `ResourceClosedError`, caught by
  the concurrency test.
- `Appointment` model typed with `service_id` missing entirely and
  `slot_id`'s FK pointing at `services.id` instead of `slots.id` - not
  visible from reading the file, only surfaced in the autogenerated
  migration diff.
- `book_at` typo for `booked_at`; a `TYPE_CHECKING` import referenced
  `app.models.appointments` (plural, doesn't exist).
- A multi-paragraph commit message drafted as one `-m` with embedded
  blank lines didn't paragraph correctly - switched convention to one
  `-m` per paragraph going forward.

**Explain out loud**

- Why SELECT-then-UPDATE is a race condition and a single conditional
  UPDATE closes it - compare-and-swap, the database as sole arbiter.
- Current-state column + append-only history table - balance vs.
  statement, same shape as light event sourcing.
- Why the concurrency test can't use `db_session` - its transaction is
  never actually committed, so a second connection can't see the row.

**Carrying into Day 4**

- 2.7 - Booking idempotency (`Idempotency-Key` header, Redis).
- 2.8 - Simulated `BillingChecker` + billing table.

**Open questions**

- Should `INACTIVE` have a re-publish path? (carried from Day 1)
- Should `GET publish-status` cross-check Temporal's own execution status
  instead of relying on the DB column alone? (carried from Day 2)
- Is `POST /services/{id}/unpublish` expected this week, or does it wait
  until scheduled? (carried from Day 2)
- `AppointmentStatusHistory.actor` is a free string for now - worth a
  fixed vocabulary before the saga starts writing to it in 2.9?

---

### Day 4 - 2026-08-31

**Goal:** 2.7 (booking idempotency via Redis) + 2.8 (simulated
`BillingChecker` + billing table). Both standalone, unwired -- tomorrow's
saga (2.9) is what calls them.

**Done**

- **2.7** `app/core/redis.py` (client) + `app/services/idempotency.py`
  (`get_cached_result`/`store_result`), TTL from
  `settings.idempotency_key_ttl_seconds`.
- **2.8** `BillingStatus` enum, `Billing` model + migration (unique
  `appointment_id` and `idempotency_key`, FK RESTRICT),
  `BillingChecker.precheck()` - idempotent, forceable failure via
  `billing_force_fail`.
- Fixed pre-existing `black` drift in 3 files untouched since before Day
  3 (`temporal/client.py`, two test files) - separate commit.
- 172 -> 179 tests (7 new + 1 coverage fix), lint clean, no hardcoded
  status codes, migration round-tripped, everything also verified live
  against the real containers, not just pytest.

**Decisions**

| Decision | Why |
|---|---|
| Redis check-then-remember, DB unique constraint as backstop | Redis answers "seen this key" before a row exists; the DB is the last-resort guarantee |
| TTL read from `settings.idempotency_key_ttl_seconds` (86400s) | `.env.example` already declared this env var; my first draft hardcoded 3600s and ignored it |
| `BillingChecker` is a class with one method today | `REFUNDED` is already in the vocabulary - a natural second method later, same reasoning as `PublishActivities` |
| `billing.amount` is a fixed placeholder (`100.00`) | No pricing model exists anywhere in the domain; billing is explicitly simulated |
| `billing_force_fail` is a global settings flag | Matches the brief's literal wording; 2.9's saga flips it on demand to exercise compensation |
| `BillingChecker.precheck` takes `db: Session` directly | Mirrors `reserve_slot`'s plain style; the Temporal session-factory DI is 2.9's Activities wrapper's job, not this class's |

**Cost time**

- `docker compose build` (bare) silently skips services behind
  `profiles:` - the `test` image kept running on a pre-`redis` image
  until built explicitly (`docker compose build test`). Recurred a
  second time later in the day for reasons I didn't fully pin down.
- `settings.billing.force_fail` - a nested-attribute typo for
  `settings.billing_force_fail`, caught by the test suite, not lint.
- `black` flagged 3 files never touched today - drift since before Day
  3 despite that day's notes saying "lint clean".
- `app/core/redis.py` sat at 0% coverage - nothing, not even the test
  suite, ever imported it. Caught by `/verify`, not by habit.

**Explain out loud**

- Four distinct idempotency mechanisms now exist (client/Redis,
  data/atomic UPDATE, Activity-level, consumer-level later) - why each
  is needed and none subsumes another.
- A fake billing check has to be able to fail on purpose, or it proves
  nothing about compensation.
- Check-before-insert is now the same pattern across three unrelated
  tasks (2.5, 2.7, 2.8) - one convention, not three coincidences.

**Carrying into Day 5**

- 2.9 - the scheduling saga (7h, the big one) - wires `reserve_slot`,
  `BillingChecker` and the idempotency cache together as Temporal
  Activities with compensation.
- Still open: a fixed vocabulary for `AppointmentStatusHistory.actor`
  before the saga starts writing to it.

**Open questions**

- Should `INACTIVE` have a re-publish path? (carried from Day 1)
- Should `GET publish-status` cross-check Temporal's own execution status
  instead of relying on the DB column alone? (carried from Day 2)
- Is `POST /services/{id}/unpublish` expected this week? Leaning "no" -
  the Week 2 Definition of Done never mentions it. (carried from Day 2)
- Why did the `test` image's `redis` package regress after being fixed
  once already today - worth watching for a third occurrence before
  digging into BuildKit/profile caching further.

---

### Day 5 - 2026-08-31

**Goal:** 2.9 - the appointment scheduling saga (validate -> reserve ->
billing -> reminders -> confirm) as a Temporal Workflow with
compensation, fully tested.

**Done**

- Resolved both carried opens: `INACTIVE` gets no re-publish path;
  `AppointmentStatusHistory.actor` fixed as
  `PATIENT`/`FRONT_DESK`/`PROVIDER`/`ADMIN` + `SAGA` + `SAGA_COMPENSATION`.
- `slot_reservations` table + model + enum, migration round-tripped -
  makes `reserve_slot` retry-safe.
- `reserve_slot` split into `reserve_slot_uncommitted` + `reserve_slot`
  so the saga can share one transaction with its own insert.
- `SchedulingActivities` (7 methods, all idempotent) +
  `AppointmentSchedulingWorkflow` (explicit compensation per failure
  type). Worker registers both.
- Verified live: happy path, compensation path, worker-crash resumption.
- 179 -> 202 tests, 98% coverage.

**Decisions**

| Decision | Why |
|---|---|
| `slot_reservations` table, not `Appointment.status` | Reserve must survive a crash between the UPDATE and the status write |
| `reserve_slot` split into uncommitted core + committing wrapper | One shared transaction with the saga's own insert |
| `SAGA` vs `SAGA_COMPENSATION` as separate actors | Audit trail shows a rollback apart from an ordinary step |
| `schedule_reminders` a real no-op Activity | Celery is Week 3; one-line change later |
| Compensation `try`/`except` left unabstracted | Modeling each one explicitly is the lesson |
| Crash-resumption proven live, not by a test | No reliable way to hit "mid-saga" timing in pytest |

**Cost time**

- `appointment.book_at` typo for `booked_at` - same field Day 3
  mis-typed too.
- Test file saved as `tes_scheduling_workflow.py` - pytest silently
  skipped it.
- First instinct (reuse `Appointment.status` for idempotency) would have
  missed the actual crash window.

**Explain out loud**

- Compensation isn't rollback - `reserve_slot` already committed, so
  undoing it is a new write.
- The UPDATE and the `slot_reservations` insert share one commit, not
  two.
- Postgres row-level locking, not "who commits first," stops two
  concurrent reserves both winning.

**Carrying into Day 6**

- 2.10 - `POST /appointments`, `GET` state, cancel/reschedule.
- 2.11 - Visit lifecycle status flow.
- 2.12 - remaining saga-level tests that need 2.10/2.11 to exist.
- 2.13 - docs pass, not started for 2.9.

**Open questions**

- `GET publish-status` cross-check Temporal directly? (carried from Day 2)
- Is `unpublish` expected this week? Leaning no. (carried from Day 2)
- Is a live demo enough for "demonstrate it" on crash-resumption, or does
  the mentor want an automated test too?

---

### Day 6 - 2026-09-01

**Goal:** 2.10 in full - `POST /appointments` + `GET` state, the waitlist
table and join endpoint, cancel, and reschedule. Ran long enough that
2.11 and the rest of 2.12/2.13 continue on Day 7 instead.

**Done**

- **2.10a** `POST /appointments` (202 + id, starts the Day 5 saga) and
  `GET /appointments/{id}`. `Idempotency-Key` wired to 2.7's Redis cache,
  with `appointments.idempotency_key` as the DB backstop.
- `resolve_acting_patient` in a new `app/services/patient.py`: a PATIENT
  books for themselves, body `patient_id` ignored; staff must name one.
- `get_redis` FastAPI dependency + `conftest` override, so route tests hit
  test Redis (index 15) instead of the app's DB 0.
- **2.10b** `waitlist` table, model, `WaitlistStatus`, migration with a
  partial unique index (`WHERE status = 'WAITING'`), and `POST /waitlist`.
- **2.10c** Cancel: `ensure_can_cancel` + `cancel_appointment` - releases
  the slot (if one was held), transitions to CANCELLED, promotes the
  oldest waiting entry. Same shape as `ensure_can_publish`.
- `promote_next_waiting` in `waitlist.py` - oldest `WAITING` entry to
  `OFFERED`, ordered `(created_at, id)`, doesn't commit itself.
- **2.10d** Reschedule: `ensure_can_reschedule` + `reschedule_appointment`
  - release old + reserve new in one transaction via
  `reserve_slot_uncommitted`, so a lost race for the new slot leaves
  nothing committed. `appointment.status` untouched; a CONFIRMED
  appointment's new slot goes straight to BOOKED/COMMITTED. No
  `appointment_status_history` row - `slot_reservations` is the trail.
- Verified live against the real worker throughout: booking ran REQUESTED
  -> CONFIRMED; a cancelled CONFIRMED appointment released its slot and
  promoted a real waitlist entry; one appointment rescheduled twice left
  an unbroken RELEASED/RELEASED/COMMITTED trail across three slots with
  zero extra history rows; forcing a target slot to BOOKED and
  rescheduling into it left the original completely untouched.
- 202 -> 243 tests, 97% coverage.
- **Not done:** the visit lifecycle (2.11), the rest of 2.12, `docs/prd.md`.

**Decisions**

| Decision | Why |
|---|---|
| `resolve_acting_patient` in its own service, not the router | The router had a `select()` and two business rules in it - caught by review, not by lint |
| `get_redis` a dependency, not a module-level import | Route tests otherwise write 24h-TTL keys into the app's real Redis |
| Waitlist entry points at a provider, not a slot | You don't know which slot frees up, only whose time you want |
| Partial unique index, not a plain one | An OFFERED entry is history; a plain index locks a patient out of that queue permanently |
| Queue order is `(created_at, id)` | Postgres `now()` is transaction-scoped, so two rows in one transaction tie |
| 201 for a waitlist join, 202 for a booking | Nothing runs in the background for a join - no workflow to poll |
| Two waitlist states only | Nothing can write a third until Week 3 notifications exist |
| `ensure_can_cancel`/`ensure_can_reschedule` as standalone guards | Requested explicitly - same shape as `ensure_can_publish`, not inlined |
| Cancel reuses `release_slot`'s outcome, never its code | `SAGA_COMPENSATION` is reserved for the saga's own rollback; a patient-requested cancel needs its own actor |
| Reschedule writes no status-history row | The table logs status *transitions*; a slot move at unchanged status isn't one - `slot_reservations` already carries that trail |
| Reschedule uses `reserve_slot_uncommitted`, not `reserve_slot` | A failed new-slot reservation must leave nothing committed, including the old slot's release |

**Cost time**

- `main.py` router registration never got typed - every appointment route
  404'd until verification caught it.
- After the refactor, `appointments.py` still passed `data` instead of
  `data.patient_id`. A Pydantic model is iterable, so SQLAlchemy read it as
  a composite primary key.
- Two transcription typos in reschedule: `db.get(Slot, appointment.id)`
  for `appointment.slot_id` (crashed immediately); the CONFIRMED branch
  assigning RESERVED instead of COMMITTED (silent, caught only because
  the test asserted the exact enum value).
- The reschedule atomicity test failed for a reason that wasn't a code
  bug: the `client` fixture's `db_session` override has no per-request
  rollback, unlike real `get_db()`. A live check against the real API
  proved production was already correct; fixed by testing the same claim
  at the service layer with an explicit commit-then-rollback instead.
- `black` drift on 7 files, 2 untouched since Days 3 and 5. Third
  recurrence - a pre-commit hook is the actual fix.
- The Redis test-isolation bug was caught by reading the code, not by a
  failing test: the suite would have passed once and failed on re-run.

**Explain out loud**

- Why a client idempotency key and the atomic slot UPDATE solve different
  problems - one patient's own retry vs. two patients racing.
- Why a `select()` in a router is a layering bug, and what moved to fix it.
- Why the waitlist index carries a `WHERE` clause, and what breaks without it.
- Why `now()` ties inside one transaction, and why `id` is the tiebreak.
- Compensation vs. a patient's own cancel: same slot-release outcome,
  different actor recorded, and why that distinction matters later.
- Reschedule's atomicity is a different guarantee from the slot UPDATE's:
  one writer's two changes staying together, not two writers racing.

**Carrying into Day 7**

- **Branch plan: Day 7 branches off `week-2-workflows-day-6`**, its own
  PR targeting day-6's. Day 6's PR (#8) is now complete as-is - 2.10 in
  full plus most of 2.13 - and gets no more pushes.
- 2.11 - visit lifecycle (`CHECKED_IN -> IN_PROGRESS -> COMPLETED`),
  idempotent, 409 on illegal jumps. The one Week 2 MUST still unbuilt.
- 2.12 remainder - illegal visit transitions need 2.11 to exist first.
  Concurrency, duplicate booking and saga compensation are already covered.
- 2.13 remainder - `docs/prd.md` and the traceability table.
- Uncovered in `appointment_scheduling.py`: the DB-backstop branch when
  Redis misses but the row exists, and the `IntegrityError` race.
- A pre-commit hook for black.

**Open questions**

- Should `GET publish-status` cross-check Temporal directly? (from Day 2)
- Is `unpublish` expected? Leaning no. (from Day 2)
- Live demo enough for crash-resumption, or an automated test too? (Day 5)
- A booking stuck REQUESTED because Temporal was down at `start_workflow`
  has no retry. Worth a sweeper, or is documenting it enough?

---

### Day 7 - 2026-09-02

**Goal:** close Week 2 - 2.11 (visit lifecycle), the 2.12 gaps, and
2.13's remaining `docs/prd.md`.

**Done**

- Closed all three carried opens: `publish-status` does not cross-check
  Temporal, `unpublish` is not expected for now, and a live demo is
  enough for crash-resumption.
- **2.11** `visits` table, model, enum, migration (round-tripped);
  `unique(appointment_id)` makes it a real 1:1.
- `app/services/visit.py` - `check_in`/`start_visit`/`complete_visit`,
  each with its own `ensure_can_*` guard. `complete_visit` is the only
  one that also moves `Appointment.status` and writes history.
- `app/api/v1/visits.py` under `/appointments/{id}/visit` - transitions
  are staff-only, a patient can read their own.
- **2.12** audited: 4 of 5 cases already covered. Closed
  `mark_publish_failed`, the Redis-evicted idempotency backstop and
  three 404s, into their existing test files.
- **2.13** `docs/prd.md` with the traceability table; ERD extended to
  all 17 tables.
- 243 -> 264 tests, 98% coverage.

**Decisions**

| Decision | Why |
|---|---|
| Visit lifecycle is not a Temporal workflow | Each move is a human action at its own pace; nothing is mid-flight to resume |
| No `visit_status_history` table | `status` plus the two timestamps are the whole trail for a linear flow |
| Idempotent retry and illegal jump handled separately | Repeating a transition is a no-op; skipping or reversing one is a 409 |
| Check-in returns an existing visit whatever state it reached | A retry must not create a second row *or* drag a started visit back |
| No end-to-end saga compensation test | Real Activities under `WorkflowEnvironment` share the test session across the worker's threads; two halves plus a live demo instead |

**Cost time**

- `visit.status - VisitStatus.COMPLETED` - a `-` where `=` belonged.
  Third transcription typo on this branch; all three caught by tests,
  none by review.

**Explain out loud**

- Why the visit lifecycle is a status column and a guard rather than a
  workflow - who owes the next step, the system or a human.
- Why a retried check-in is a 200 no-op but completing an unstarted
  visit is a 409.
- What `from_attributes=True` does, and why `AppointmentResponse`
  deliberately does not have it.

**Carrying into Week 3**

- Celery first, then Kafka, then observability - do not start Kafka on
  Monday.
- Still uncovered: the `IntegrityError` race in `request_appointment`.
- A booking stuck REQUESTED if Temporal was down at `start_workflow`.
- A pre-commit hook for black.

**Open questions**

- None.

---

## Weekly self-check

### Week 2 - 2026-09-02

1. **Finished / broken:** 2.1-2.13 all complete. 264 tests, 98%
   coverage. Nothing known broken; three known gaps recorded in
   `docs/prd.md` rather than left implicit.
2. **Not fully understood yet:** what should happen to a booking whose
   workflow never started because Temporal was down - the row sits
   REQUESTED and nothing retries it.
3. **Most time spent:** writing the code, and working out Temporal's
   model - what belongs in an Activity vs. a Workflow, why Workflows
   must be deterministic, and the reasoning behind each piece before
   typing it.
4. **Carrying into Week 3:** nothing from Week 2's task list. Celery,
   then Kafka, then observability.

---

## Week 3 - Async, Events & Observability

### Day 1 - 2026-09-07

**Goal:** 3.1 (Celery + Redis broker, trivial task) and 3.2 (reminder
task + `failed_jobs`, periodic analytics rollup + Celery Beat).

**Done**

- **3.1** `app/workers/celery_app.py`, `celery-worker` compose service.
  Trivial `add` task proved the broker + result backend round-trip live,
  then deleted once real tasks existed.
- **3.2a** `schedule_reminders` (Week 2's deliberate no-op) now queues a
  real Celery task. `notifications` + `failed_jobs` tables, models,
  migration. `DeadLetterTask` base class writes to `failed_jobs` on a
  task's last failure.
- **3.2b** `analytics_daily` table + `app/services/analytics.py`'s
  rollup (five per-day metrics from raw tables, `INSERT ... ON CONFLICT
  DO UPDATE`, not an increment). `celery-beat` compose service, 5-minute
  schedule.
- 265 -> 273 tests, 98.15% coverage, every file touched today at 100%.

**Decisions**

| Decision | Why |
|---|---|
| `celery-worker`/`celery-beat`, not `worker` | Avoids the exact naming collision the compose file's own comment already flagged |
| Rollup recomputes-and-overwrites, not increments | Idempotent by construction - safe to run redundantly |
| `total_patients` absent from `analytics_daily` | A running count, not a per-day bucket |
| `DeadLetterTask` a shared Task base class | Any future Celery task gets dead-lettering for free |
| Idempotency lives in one check-before-insert, not a DB constraint | Two retry layers (Temporal, Celery) funnel through one guard |

**Cost time**

- Two more `=`/`:` typos (`Notification.user_id`,
  `AnalyticsDaily.appointments_booked`) - caught by Alembic's import
  crashing, not review.
- A copy-pasted `.where()` left `failed_jobs_count` filtering on
  `Visit.checked_in_at` - a silent cartesian product masked by `visits`
  being empty in dev data; caught by SQLAlchemy's own warning, not the
  number being obviously wrong.
- `temporal-worker` and the `test` image were both still on pre-Celery
  builds from before 3.1 - `ModuleNotFoundError` until rebuilt.
- Skipped the `verify` skill's lint/mypy/black pass after 3.2a - black
  later reformatted 4 already-committed 3.2a files, needing a separate
  style commit.
- `task_eager_propagates` (on so every eager-mode test can just
  `pytest.raises`) skips Celery's `on_failure` hook entirely and
  re-raises directly - silently defeated the first version of the
  dead-letter test.
- `SessionLocal` has no test-time injection point, unlike Activities'
  `session_factory` - worked around per-test via monkeypatching each
  module's own `SessionLocal` name.

**Explain out loud**

- Scheduler vs. worker: Beat only enqueues on a clock, the same
  mechanism as `.delay()` - it never executes task code itself.
- Recompute-and-overwrite is a stronger guarantee than "safe to retry":
  no run depends on a previous run's output, so redundant or concurrent
  runs converge on the same answer.
- One idempotency guard can safely sit under two unrelated retry layers
  (Temporal retrying the Activity, Celery retrying the task) because it
  lives at the point the effect actually happens.

**Carrying into Day 2**

- 3.3 (Kafka producer) next - the brief explicitly warns not to start
  Kafka first.
- `SessionLocal` needs a real injectable session factory before more
  tasks repeat today's monkeypatch workaround.
- Run `verify`'s static checks every subtask, not just before a day's
  PR - today's black drift on already-committed files is exactly what
  skipping it once caused.

**Open questions**

- None.

### Day 2 - 2026-09-08

**Goal:** 3.8 (structured JSON logging + correlation IDs across API, Temporal
and Celery) and 3.3 (Kafka producer, via the outbox).

**Done**

- Carry-over: `session_scope()` in `app/db/session.py` -- one seam for
  non-request code, replacing four per-module `SessionLocal` monkeypatches.
- **3.8a** `app/core/logging.py`: ContextVar, `CorrelationIdFilter`,
  `JsonFormatter`, `configure_logging()`. Middleware reads or mints
  `X-Request-ID` and returns it. uvicorn's own handlers cleared so its
  access log is the same JSON.
- **3.8b** The id crosses both process boundaries: Celery task kwarg, and
  Temporal input dataclasses (`AppointmentInput`, `ServiceInput`; nine
  Activities changed). Each saga step logs one line, ids only.
- **3.3a** `app/events/`: envelope, six event types, `outbox_events` table +
  migration, `record_event()` at seven emit points inside the transaction
  that already existed.
- **3.3b** `confluent-kafka`, producer with delivery confirmation, relay
  claiming rows `FOR UPDATE SKIP LOCKED`, Celery Beat every 5s.
  Kafka/kafka-ui/Prometheus off the `week3` profile.
- 273 -> 324 tests, 98.25% coverage.

**Decisions**

| Decision | Why |
|---|---|
| Correlation ID via workflow/activity args, not Temporal headers + interceptors | What the brief specifies; far less machinery for one string |
| `correlation_id` defaults to None on every input dataclass | Replay-safe for history recorded before the field existed |
| Topics per aggregate, not per event type | One appointment's events share a partition, so `booked` precedes `confirmed` |
| Aggregate derived from the event name, not stored | Two columns cannot disagree if there is only one |
| Outbox relay is a Celery Beat task, not a new container | Fire-and-forget work is Celery's half of the division of labour |
| `message.timeout.ms` inside the flush window | The outbox owns retries; librdkafka must not be a second retry layer |

**Cost time**

- Five transcription slips in the event names and emit points -- names and
  values crossed over. **ruff, black and mypy passed on all of them**; only
  the suite caught them, as an `AttributeError` from inside `enum.py`.
- The workflow tests *hung* rather than failed: an `AttributeError` in an
  Activity is not an `ApplicationError`, so Temporal retried it forever.
  Had to exclude both files to get a usable failure list.
- Wrote a test that could not fail -- comparing two lists built from the
  same run passes vacuously when both are empty.
- Third stale-image incident in two days: `api` had been crash-looping
  since yesterday's Celery change while `docker compose ps` still said
  `Up`, and `test` predated `confluent-kafka`.
- ContextVar leaked between tests -- eager Celery tasks set it in the
  test's own context. Needed an autouse reset fixture.
- Ran black over `migrations/`, reformatting 14 unrelated files; `make fmt`
  deliberately scopes to `app tests scripts`.

**Explain out loud**

- `from X import Y` binds at import time, `X.Y` at call time -- you can only
  replace what is resolved late.
- Ambient context (ContextVar) for cross-cutting values nothing acts on;
  explicit injection for dependencies code actually uses.
- The dual-write problem: no ordering of commit/produce is safe, so make the
  announcement a database write and accept duplicates instead.
- `produce()` only queues; only a delivery callback separates *sent* from
  *abandoned*.

**Carrying into Day 3**

- 3.4 (consumer container) and 3.5 (`processed_events`) -- the duplicates the
  relay can emit are absorbed there.
- Prometheus `app-api` target is DOWN (404) until 3.9 adds `/metrics`.
- Consider `pytest-timeout` so a hanging Temporal test fails instead of
  stalling the suite.
- `docs/events.md` still to write (3.12).

**Open questions**

- None.

### Day 3 - 2026-09-09

**Goal:** 3.4 (consumer container), 3.5 (consumer idempotency), 3.6 (analytics
endpoints). Two unplanned preambles first.

**Done**

- **Preamble** `pytest-timeout`, 120s. Day 2's hang would now fail with a
  traceback instead of stalling the run.
- **Preamble** Temporal test-server binary baked into the image
  (`scripts/fetch_test_server.py`). The suite no longer downloads 83MB per
  run and passes with `--network none` -- a Weeks 4-5 requirement that was
  already broken and invisible.
- **3.5a** `processed_events`, PK `(consumer, event_id)` + migration.
- **3.4a** `app/workers/consumer.py` and its container: manual offsets,
  poison message -> `failed_jobs` then commit, transient -> `seek` and retry.
- **3.5b** `claim_event()` -- `ON CONFLICT DO NOTHING ... RETURNING`, sharing
  the handler's transaction.
- **3.4b** Handlers for booked/cancelled/completed. `analytics_daily`
  reshaped; Beat rollup retired and `rollup_analytics_for_date` became the
  read-only `compute_analytics_for_date` for 3.7.
- **3.6** `GET /analytics/summary` and `GET /analytics/appointments`,
  FRONT_DESK/ADMIN only.
- 324 -> 370 tests, 97.53% coverage.

**Decisions**

| Decision | Why |
|---|---|
| Consumer is the sole writer of `analytics_daily`; the rollup becomes 3.7's check | A 5-minute recompute would overwrite increments, and a check that writes can never find drift |
| PK `(consumer, event_id)`, not `event_id` alone | Idempotency is per-consumer; a second consumer would skip everything the first had seen |
| `ON CONFLICT DO NOTHING ... RETURNING`, not a caught `IntegrityError` | A violated constraint aborts the transaction the handler still needs |
| Permanent failure dead-letters then commits; transient rewinds | An offset is a position, so refusing to move blocks the partition forever |
| `topic.metadata.refresh.interval.ms` 10s | A topic created after subscribe was invisible for five minutes |
| `avg_wait_seconds` -> `wait_seconds_total` + `wait_count` | An average cannot be incremented; its components can |
| `failed_jobs_count` dropped, counted directly | No event maintains it, and it duplicated a number `failed_jobs` already holds |
| Handlers bucket by the same raw column the reconciliation reads | Otherwise a midnight-straddling event shows as drift that was never real |

**Cost time**

- Two more transcription slips, both in typed code: `envelope[event_id]`
  missing its quotes (a `NameError` on the first real event), and a
  `FailedJob` import I had removed an hour earlier. ruff caught both; review
  would not have.
- `pytest-timeout` failed a Temporal test at 60s on its first run. Cause was
  an 83MB test-server download *inside* the test, measured at 7s and 65s on
  two runs of the same suite -- the test was reporting network speed.
- The first live event never arrived: the consumer had subscribed before
  `app.visits` existed, and librdkafka refreshes topic metadata every five
  minutes. Looked exactly like a dead consumer.
- `worker_session`'s `nullcontext` does not roll back, so the "claim must not
  outlive its work" test failed while production was already correct. Made
  the rollback explicit rather than leave it to the session closing.
- Coverage passed at 97% with `dispatch()` at 0% -- every loop test
  monkeypatches it, so the routing seam was never executed.
- Seeded patient `ayesha@example.com` no longer authenticates with
  `SEED_PASSWORD`; used a throwaway registration for the live 403 check.

**Explain out loud**

- A Kafka offset is a position, not a checklist: you cannot accept the next
  message while leaving this one outstanding.
- Not committing is not enough to retry -- `poll()` advances the client's own
  position anyway, so `seek()` is what makes a retry real inside a process.
- A violated constraint aborts the whole transaction, which is why the
  duplicate guard is an upsert rather than a `try/except`.
- An aggregate is incrementally maintainable only if the new answer needs
  just the old answer and the new item. Averages fail that and decompose into
  a sum and a count, which do not.

**Carrying into Day 4**

- 3.7 reconciliation -- `compute_analytics_for_date` is ready and writes
  nothing.
- Stale `analytics_daily` rows written by the retired rollup will show as
  real drift in 3.7. Decide whether to clear them or demo them.
- 3.9 `/metrics` -- the Prometheus `app-api` target is still DOWN.
- 3.10 `/health/ready`, 3.12 docs, 3.13 crash-recovery demo.
- Buckets are UTC calendar days, not clinic-local. Known limitation, written
  down, not fixed.
- Dev seed has drifted -- some seeded users no longer match `SEED_PASSWORD`.

**Open questions**

- The DoD says all six metrics are "served from aggregates", but total
  patients and failed jobs have no event that could maintain one, so both are
  counted directly. Is that acceptable, or should a seventh event type exist?

### Day 4 - 2026-09-10

**Goal:** 3.9 (`/metrics`), 3.10 (`/health/ready`), 3.7 (reconciliation).
One unplanned preamble first.

**Done**

- **Preamble** Seed now repairs a drifted login instead of skipping it, and
  `main()` prints the accounts it owns. The account that "stopped
  authenticating" was never seeded.
- **3.9** `app/core/metrics.py`; HTTP counter + latency histogram from
  middleware; four domain counters; worker and consumer each publish on
  8001/8002. All four Prometheus targets UP -- `app-api` had been DOWN
  since Day 2.
- **3.10** `/health/ready` checks Postgres, Redis, Kafka, Temporal
  concurrently, each with a deadline. `/health/db` retired as its Week 1
  docstring promised.
- **3.7** `reconcile_date`/`reconcile_range`/`repair_date`,
  `scripts/reconcile_analytics.py`, `GET /analytics/reconciliation`
  (ADMIN only).
- Stale rollup rows cleared via `--repair`; drift then injected by hand to
  watch the check fail.
- Mentor answered Day 3's open question: counting total patients and failed
  jobs directly is acceptable. No seventh event type.
- 378 -> 387 tests, 97.66% coverage.

**Decisions**

| Decision | Why |
|---|---|
| Metrics from three processes, not one | A process can only count what it saw; `reserve_slot` runs in the worker, not the API |
| Accept that every process registers all four domain counters | Importing the module registers them; the API imports activities anyway. Names stay honest, `sum` by job is the query |
| Label HTTP metrics by route template, never the URL | One metric per appointment id is how you exhaust your own Prometheus |
| Unmatched paths share one label | Otherwise anyone mints unlimited labels by requesting nonsense |
| `appointments_booked` counted at CONFIRMED, not at reservation | A reserved slot can still be compensated back |
| `/health/ready` returns the per-dependency breakdown, not the error envelope | Reader is a monitor, not an API client; "which one" is the whole payload |
| 503, not 500 | Not broken, just not ready -- a load balancer treats them differently |
| Socket timeouts on the shared Redis client, not just the check | A hung Redis should not block a booking either |
| Reconciliation writes nothing; `--repair` is separate and manual | A check that fixes what it finds can never report anything |
| A missing aggregate row counts as zeros, not "skip" | A consumer that died before writing a day would otherwise look healthy |
| Reconciliation endpoint is ADMIN only | The other analytics routes answer clinic questions; this one answers whether our pipeline works |
| Script exits 1 on unrepaired drift | A check that always exits 0 cannot be alerted on |

**Cost time**

- The seed printed "Seed complete" while repairing nothing. `_get_or_create_patient`
  returns early when the profile exists, before its own commit, so the new hash
  was flushed and discarded. Staff accounts had been riding on a later helper's
  commit by luck.
- The suite cannot catch that: `db_session` rolls back and the assertion re-reads
  the same session, so a flush is indistinguishable from a commit. Five green
  tests against a script that did nothing.
- `verify_password` raises on a hash too corrupt to parse, so the first repair
  crashed on exactly the row it existed to fix.
- Two transcription slips again -- `app.middlewayre`, `from redis import redis`.
- My own test bug: assumed the `appointment` fixture had a `booked_at`. It stops
  at REQUESTED, which is correct -- `booked_at` is the saga's to write.
- Redis and Postgres readiness checks take ~3.9s, not the 2s designed for: both
  drivers retry a refused connection once, so the timeout is per attempt, not
  per check. Comment corrected to say so.

**Explain out loud**

- A metric is a tally in one process's memory. Three programs, three tills.
- Labelling by URL instead of route template is unbounded cardinality -- the
  standard way people take down their own monitoring.
- A dependency that is *down* refuses instantly; one that is *hung* accepts and
  says nothing. The timeout is the whole game.
- Detection and repair must be separate actions, or the evidence is gone before
  anyone reads the report.

**Carrying into Day 5**

- 3.11 tests, 3.12 `docs/events.md` + `docs/runbook.md` + architecture diagram,
  3.13 crash-recovery demo.
- Runbook must document `sum by (job)` for the domain counters, and
  `--repair` as the drift response.
- Buckets are UTC calendar days, not clinic-local. Deliberately deferred:
  ~2.5-4h (4 handler sites, 3 boundary computations, 8 test files, and a
  full backfill since every stored row is bucketed the old way), it is in
  neither 7.1 nor the DoD, and Day 5 already holds 12h of estimates. Write
  it up as a known limitation in `docs/design.md` during 3.12 instead --
  what the boundary is, why UTC, what it costs a clinic far from UTC.

**Open questions**

- None.

### Day 5 - 2026-09-10

**Goal:** 3.11 (tests), 3.12 (`docs/events.md`, `docs/runbook.md`,
architecture diagram), 3.13 (crash-recovery demo).

**Done**

- **3.11** Retry covered both ways: a transient `OperationalError` retries
  five times and dead-letters at `attempts=6`; a blip that clears on the
  third attempt writes nothing.
- **3.11** `test_event_replay.py` -- one `visit.completed` through the real
  loop twice, count stays 1. Mutation-checked by forcing `claim_event` to
  return True.
- **3.11** The forward reconciliation test found a real bug: the wait-time
  recompute counted every visit checked in that day, the handler only
  completed ones. Fixed.
- **3.12** `docs/events.md` and `docs/runbook.md` written. README diagram
  redrawn (outbox, Prometheus, consumer -> Postgres); two stale lines fixed.
- **3.13** 30 bookings and 20 publishes fired as a trickle, worker SIGKILLed
  mid-flight both times: 11 sagas frozen at SLOT_RESERVED, 4 services at
  PUBLISHING, 29 workflows Running with no worker. All resumed on restart,
  0 left running, exactly 1 chunk per service.
- Live replay against real Kafka: same `event_id` twice -> "event processed"
  then "duplicate event skipped", count +1.
- 387 -> 393 tests, 97.66% coverage.

**Decisions**

| Decision | Why |
|---|---|
| Backoff asserted as the ceiling, not the delay | `retry_jitter` picks randomly inside `min(max, factor * 2**retries)` |
| The crash demo fires a trickle, not a burst | A burst makes "mid-flight" a race; a trickle guarantees done, part-done and not-started at once |
| Wait-time recompute filters on COMPLETED | It has to read what the handler writes, or it reports drift that was never real |
| The `appointment.booked` fix is deferred | It changes which event drives a graded metric -- not a Friday-afternoon change |
| Demo order: analytics before crash recovery | Zero-cost mitigation for that bug, and the reconciliation catching real drift is the better story anyway |

**Cost time**

- `docker compose cp`/`exec` needed `MSYS_NO_PATHCONV=1` *and* a Windows-form
  source path -- `//tmp` for exec arguments, `/tmp` for cp destinations.
- Ruff never flagged a duplicated `_book_on` in `test_reconciliation.py`:
  F811 ignores names starting with an underscore, which is every test helper
  in this repo.
- The crash demo's drift looked like a demo artefact and was a real bug --
  28 consumer dead-letters reading "appointment 51 does not exist" about an
  appointment that exists.
- `jq` is not installed here; caught before it reached the runbook.

**Explain out loud**

- Celery's eager mode runs retries inline, so the retry count is observable
  in a test even though the nesting is not production's shape.
- A test that has only ever been seen passing proves nothing -- breaking the
  dedupe guard on purpose is what turns the replay test into evidence.
- 29 workflows Running with no worker alive *is* the durability guarantee,
  made visible.
- "Row missing" and "not ready yet" collapsing into the same `None` is how a
  permanent-error branch quietly swallows a transient one.

**Carrying into Week 4**

- The `appointment.booked` -> `appointment.confirmed` handler fix. ~1h15m
  including a crash-demo re-run, which is the only thing that proves it.
- 28 dead-lettered events are unrecoverable; the numbers themselves were
  repaired.
- UTC bucket limitation, unchanged.
- `docs/diagrams/architecture.svg` needs re-exporting from the new mermaid.

**Open questions**

- None.

---

## Weekly self-check

### Week 3 - 2026-09-10

1. **Finished / broken:** 3.1-3.13 all complete. 393 tests, 97.66%
   coverage. One real bug found by the crash demo and deliberately
   deferred rather than rushed: a Temporal worker outage dead-letters
   `appointment.booked` events, so booking counts need `--repair` until
   the handler moves to `appointment.confirmed`. Recorded in
   `docs/design.md`, `docs/prd.md` 7 and the runbook.
2. **Not fully understood yet:** how the consumer behaves with more than
   one instance -- `processed_events` is keyed per consumer group and the
   rebalance path has never been exercised, only reasoned about.
3. **Most time spent:** making "mid-flight" deterministic for the crash
   demo. Both workflows finish in ~200ms, so killing the worker at the
   right moment was a coin toss until the load was spread into a trickle.
   After that the demo was decisive rather than suggestive.
4. **Carrying into Week 4:** the handler fix above, and re-exporting the
   architecture diagram. Nothing from the 3.x task list.
