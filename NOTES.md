# NOTES

Working log: what I tried, what confused me, decisions I made and why.
Kept as I go, not written up at the end of the week.

---

## Tracking tables

### Part A (Weeks 1-3)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 1 | Foundation & core domain | Auth + roles + patient-data protection, providers/services/slots CRUD, migrations, seed, 80% coverage | ☐ |
| 2 | Temporal, scheduling & slots | No double-booking, no duplicate booking, publish workflow + scheduling saga with compensation, chunks produced, 80% coverage | ☐ |
| 3 | Async, events, observability | Celery reminders/rollup with DLQ, events consumed idempotently, accurate analytics, correlation IDs (no PHI), `/metrics`, 80% coverage | ☐ |

### Part B (Weeks 4-5)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 4 | Chunking, embeddings, retrieval | Published-filtered + PHI-scoped semantic search with threshold, clean re-indexing, eval results documented | ☐ |
| 5 | AI assistant & streaming | Medical-advice refusal, grounded cited recommendations, PHI scoping, report validated, SSE streaming, AI analytics, final demo | ☐ |

---

## Week 1 - Foundation

### Day 1 - 2026-08-18

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
| Config only through one `settings` object | One place knows how the app is configured; a missing variable fails loudly at boot rather than mid-request | - |
| `extra="ignore"` on Settings | `.env` already carries Weeks 2-5 keys the Week 1 class doesn't declare | A genuinely misspelled variable is silently ignored |
| `DATABASE_URL` kept out of `alembic.ini`, injected from settings in `env.py` | A connection string is a secret and `alembic.ini` is committed; it also lets the same migrations run against a test database | `alembic.ini` alone is not enough to run Alembic |
| First migration enables pgvector | The assignment requires the extension be enabled by a migration, not by hand, so a clean clone needs no manual step | Ships a Week 4 concern in Week 1 |
| `/health` checks nothing but the process | Liveness must stay fast and must not fail because a dependency is slow - that is what `/health/ready` is for in Week 3 | Two health endpoints instead of one |
| Bind-mount source + `--reload` | Edits on Windows restart the server in the container immediately | Development only; a production-shaped image must run without the mount |

**What confused me / cost time**

- `docker` was not on PATH - Docker Desktop needed to be running, and PowerShell
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
  (task 1.2) - the guidelines call skipping this the mistake that forces a
  schema rewrite in Week 2.
- Then models: `User`, `Patient`, `Provider`, `Department`, `Service`, `Slot`.

**Open questions for my mentor**

- Confirm a single clinic is fine for `departments.clinic` in Week 1.
- Confirm the expected slot granularity for the seed data (15 / 30 minutes?).

---

### Day 2 - 2026-08-22

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
- `tests/conftest.py` - test database built by `alembic upgrade head`, per-test
  transaction rollback - and 12 tests, all passing.

**Decisions and why**

| Decision | Why | Tradeoff |
|---|---|---|
| Naming convention before the first table | Constraint names must be derivable from a rule, not discovered by inspecting my own database, or a migration that drops one breaks on someone else's | Retrofitting later means renaming ~30 constraints by hand |
| Enums as `VARCHAR` + `CHECK`, not native `ENUM` | Postgres has no `DROP VALUE`, so downgrading a value addition means recreating the type and re-casting the column | More storage; alphabetical sorting; no type shared across tables |
| `StrEnum`, names equal to values | These strings are written into rows and event payloads; an f-string must render `AVAILABLE`, not `SlotStatus.AVAILABLE` | Slightly redundant to look at |
| Timestamps generated by Postgres | By Week 3 four processes write rows and ordering them needs one clock; seed scripts and psql inserts get stamped too | `onupdate` only fires on ORM updates - see below |
| `EXCLUDE` for slot overlap | The unique constraint permits overlapping rows with different start times; checking in the generator is a check-then-act gap | Needs `btree_gist`; GiST serves ordered range scans poorly, so the unique constraint stays for its B-tree index |
| Test DB dropped and recreated every run | Alembic will not re-apply a revision it has recorded, so a reused database keeps an old schema after a migration is edited | A couple of seconds per run |
| Tests split by infrastructure needed, not by scope | Weeks 4-5 require the suite to pass with no network; the boundary is cheaper to establish now than across 25+ tests | Two directories to keep straight |

**What confused me / cost time**

- `Enum(..., native_enum=False)` alone produces an **unconstrained VARCHAR** -
  `create_constraint=True` is not the SQLAlchemy default. Compiled both to see
  it. This is why every enum column goes through one helper.
- `UNIQUE (provider_id, start_time)` looks like it prevents double-booking and
  does not: 09:00-09:30 and 09:15-09:45 have different start times.
- `CHECK (end_time > start_time)` is not cosmetic. An empty range overlaps
  nothing in Postgres, so a zero-length slot would slip past the exclusion
  constraint entirely. The two constraints cover for each other.
- `migrations/env.py` unconditionally overwrote `sqlalchemy.url` with the dev
  database, so the test harness could not point migrations anywhere else.

**Things I want to be able to explain out loud**

- Why `EXCLUDE` closes a gap the atomic `UPDATE` cannot: that statement
  protects one row from concurrent access to itself and says nothing about two
  different rows overlapping in time.
- Why `onupdate` is a SQLAlchemy mechanic, not a database default - proved it:
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

### Day 3 - 2026-08-23

**Goal:** auth done end-to-end - one JSON error shape, password hashing, JWT,
register + login, a protected endpoint, and role/patient-data checks - so
Day 4's CRUD endpoints have something real to sit behind from their first line.

**Done**

- Two Day 2 open questions closed first: dropped the redundant
  `ix_services_department_id` index; added `provider_services` as an explicit
  many-to-many table rather than inferring "who offers what" from a shared
  `department_id`.
- `core/exceptions.py` (`AppError`) + `core/error_handlers.py`: one JSON shape
  for every failure. `AppError`, a validation error, a missing route, and an
  unhandled exception all resolve through the same
  `{"error": {"code", "message"}}` envelope - verified live against all four
  origins, plus confirmed the existing `/health` routes were untouched.
- `core/security.py`: `hash_password`/`verify_password` (bcrypt via passlib)
  and `create_access_token`/`decode_access_token` (python-jose). Proved,
  rather than assumed, that the pinned bcrypt version silently truncates
  anything past 72 bytes instead of raising.
- `core/dependencies.py`: `get_current_user`
  (`HTTPBearer(auto_error=False)`, because FastAPI's own default response to
  a *missing* header is 403, not 401), plus `require_role` and
  `ensure_patient_self_or_staff` - built and verified by direct function
  call, not yet wired into a real endpoint.
- `schemas/auth.py`, `services/auth.py`, `api/v1/auth.py`:
  `POST /auth/register` (patient-only), `POST /auth/login` (one identical 401
  whether the email doesn't exist, the password is wrong, or the account is
  deactivated), `GET /auth/me`.
- Suite grew from 12 tests to 40 across the day; every subtask landed as its
  own commit (eight total), each verified before the next started.

**Decisions and why**

| Decision | Why | Tradeoff |
|---|---|---|
| Registration is patient-only - no `role` field on the request | Task 1.10's seed script is what provisions provider/front_desk/admin accounts; a public endpoint that could mint an ADMIN account is a real hole, not an edge case | Staff accounts need a separate provisioning path later |
| Login returns one generic 401 for a wrong password, an unknown email, *and* a deactivated account | Distinguishing any of them lets the endpoint be used to discover which emails have real accounts - a leak on its own | A genuine typo gets the same unhelpful message as an attack attempt |
| `HTTPBearer(auto_error=False)` | The brief requires missing, invalid and expired tokens to all fail as 401; FastAPI's own default turns a missing header into 403 instead | One extra `if credentials is None` check that `get_current_user` has to own itself |
| `require_role` and `ensure_patient_self_or_staff` kept as two separate functions | They answer different questions - PATIENT is a role every patient account holds and says nothing about *which* patient's data they may see | Two call sites in any endpoint that needs both, instead of one |
| `ensure_patient_self_or_staff` is a plain function, not a `Depends()` | It needs an already-loaded `Patient` row, which only exists once a router has fetched one from a path parameter - a dependency resolves too early for that | Every router using it has to remember to call it explicitly |
| Password capped at 72 characters in `RegisterRequest` | bcrypt silently ignores anything past byte 72 - rejecting an over-length password up front beats quietly accepting one that's only ever partially checked | None found |

**What confused me / cost time**

- `db.add(User)` instead of `db.add(user)` - passed the class instead of the
  instance. Caught in verification before it ever touched the real database;
  a reminder that a single wrong capital letter compiles fine and fails at
  runtime.
- `docker compose exec` mangled an absolute `/tmp/...` path on Windows Git
  Bash - MSYS rewrites it as if it were a Windows path. Fixed with
  `MSYS_NO_PATHCONV=1` on the command.
- `python /tmp/script.py` inside the container couldn't `import app.*` -
  Python puts the *script's own* directory on `sys.path[0]`, not the working
  directory, so `/app` was never on the path. Fixed with `PYTHONPATH=/app` set
  explicitly on `docker compose exec`.

**Things I want to be able to explain out loud**

- Why a JWT's payload is readable by anyone yet still trustworthy: the
  signature is what can't be forged, not the payload, which is only
  base64-encoded - nothing secret ever belongs in a token's claims.
- Why `get_current_user` re-checks `user.is_active` even though `login()`
  already does: a token stays cryptographically valid for its whole lifetime
  after issue, even if the account is deactivated an hour later. Only a
  per-request check catches that.
- Why `ix_services_department_id` was pure write cost with no read benefit:
  `uq_services_department_id_name` already builds a composite index whose
  leading column serves any query that filters on `department_id` alone.

**Carrying into Day 4**

- Provider / service / department CRUD (task 1.7) is what finally puts
  `require_role` and `ensure_patient_self_or_staff` to real use.
- Public listing - pagination, filter by specialty/department/available-slots,
  search by service name (task 1.8) - is the first place `provider_services`
  actually gets queried.
- Seed script (task 1.10).

**Open questions for my mentor**

- None carried from Day 3 - both of Day 2's were resolved and committed
  before today's subtasks started.

---

### Day 4 - 2026-08-24

**Goal:** everything task 1.7 asks for - department/service/provider CRUD
and provider schedules with slot generation - plus task 1.8's public
service search and task 1.10's seed script, so there is a real,
demoable dataset sitting behind real endpoints by the end of the week.

**Done**

- Split task 1.7 into four reviewable pieces rather than one giant change:
  - **1.7a** Department CRUD (`app/services/department.py`,
    `app/api/v1/departments.py`) - the first use of the router → service →
    schema pattern beyond auth, and the first use of `require_role`.
  - **1.7b** Service CRUD, created `DRAFT` and staying there - `status`
    and `published_at` are deliberately absent from every writable schema,
    proven live by trying to inject `"status": "PUBLISHED"` through both
    `POST` and `PATCH` and watching it get silently ignored.
  - **1.7c** Provider CRUD - `create_provider` validates that a given
    `user_id` both exists and already has `role == PROVIDER` before
    attaching a profile to it; `ProviderUpdate` has no `user_id` field at
    all, since moving a profile to a different account isn't an edit.
  - **1.7d** Provider schedules + slot generation
    (`app/services/provider_schedule.py`) - the piece the whole week was
    building toward. `generate_slots` combines a bare clinic-local `time`,
    a calendar date and `Clinic.timezone` into the UTC-aware `Slot` rows
    Week 2's atomic reservation depends on. Exposed as its own action
    endpoint (`POST /providers/{id}/schedules/generate-slots`), idempotent
    by querying existing slots first and only inserting what's missing.
  - Introduced `app/core/pagination.py` on the first piece (1.7a) so every
    list endpoint this week - and 1.8 - shares one `{items, total, limit,
    offset}` shape instead of four slightly different ones.
- **Task 1.8**: `GET /services/search` - the patient-facing counterpart to
  1.7b's staff-only listing. No auth required (a prospective patient
  browsing before registering is the point), filters by name/department/
  specialty/available-slots, all enforced as SQL `WHERE`/`EXISTS` clauses,
  never in application code. Had to be added to the *same* router as the
  staff CRUD and declared *before* `GET /{service_id}` - Starlette matches
  routes in declaration order, and the wildcard route would otherwise
  swallow `"/search"` as an invalid `service_id` and 422 first.
- **Task 1.10**: `scripts/seed.py` - a clinic, 3 departments, 3
  specialties, 3 provider profiles with Mon/Wed/Fri schedules, 2 weeks of
  generated slots, 3 published services, and 3 synthetic patients, all in
  one idempotent command. Appointments are not seeded - that model doesn't
  exist until Week 2.
- Every piece verified two ways before being called done: the automated
  test file, and a live `curl` round-trip against the actually-running
  `api` container (not just the test database) - including, for the seed
  script, running it twice against the real dev database and inspecting
  the actual row counts and generated UTC slot timestamps directly.
- Suite grew from 40 tests to 82 across the day (7 + 8 + 7 + 11 + 7 + 2),
  each subtask landing as its own pair of commits (schemas+service, then
  router+tests), verified before the next one started.

**Decisions and why**

| Decision | Why | Tradeoff |
|---|---|---|
| `PaginationParams` (plain dataclass) split from `pagination_params` (the FastAPI dependency) | `Query(...)` objects only resolve to real values inside a request FastAPI is handling - `PaginationParams()` built directly, as every test does, would get the `Query` object itself as `limit` otherwise | One extra small function per shared concern |
| Every uniqueness check is a `SELECT` before the `INSERT`, never a bare `except IntegrityError` | A single `IntegrityError` can't distinguish "duplicate name" from "the foreign key doesn't exist," and those need different status codes (409 vs 404) | An extra query on every create |
| `has_available_slots` filters at the *provider* level | `Slot` has no `service_id` by design (a slot is provider time, chosen at booking) - a provider's one open slot legitimately makes every service they offer count as "available." Learned this the hard way in Step 4, see below | Can't express "this exact slot is held for this exact service" - not a real requirement, since the data model doesn't have that concept |
| Search filters (`EXISTS` subqueries) instead of `JOIN`s | A service offered by three providers would appear three times in a joined result set; correlated `EXISTS` avoids the fanout entirely, so no `DISTINCT` is needed anywhere | Slightly less obvious to a reader than a plain join |
| Seed script run as `python -m scripts.seed`, never `python scripts/seed.py` | Same "script's own directory lands on `sys.path[0]`" problem Day 3 already hit - `-m` puts the working directory (`/app`) on the path instead, so no `PYTHONPATH` juggling needed | One more thing to remember when documenting how to run it |
| Seed script uses `get_or_create_*` (query first, insert only if missing) everywhere, never catches the service layer's own `AppError` | Re-running it against a database that already has seed data must be a no-op, not a crash - matters for demo day | More boilerplate than "just insert and let it fail" |

**What confused me / cost time**

- Docker Desktop stopped responding mid-session (`failed to connect to the
  docker API`) - same class of problem as Day 1's PATH issue, just later.
  Restarting Docker Desktop fixed it; no data was lost since Postgres's
  volume persists across the daemon restart.
- **Two real bugs, both in tests I wrote, not in the code under test** -
  caught during Step 4 verification, not left for later:
  - The overlap test for `generate_slots` set two schedules to the same
    `start_time` before forcing them onto the same weekday, which hit
    `ProviderSchedule`'s own unique constraint *before* the test ever
    reached the `Slot` exclusion constraint it was meant to exercise.
    Fixed by giving the second schedule a different `start_time`.
  - The `has_available_slots` test gave *one* provider two services and
    only one slot, expecting the filter to distinguish between the two
    services. It can't - see the decisions table above. Fixed by using
    two separate providers, one with a slot and one without.
  Both are a good reminder that a failing test needs the same "what
  exactly does this prove" scrutiny as the code it's testing.

**Things I want to be able to explain out loud**

- Why `datetime.combine(date, time, tzinfo=clinic_tz).astimezone(utc)`
  is the whole slot-generation trick: the first call describes a moment
  using the clinic's local clock face; `.astimezone(utc)` describes the
  exact same moment using a different clock face. Nothing about *when*
  changes, only how it's written down.
- Why route *declaration order* matters in FastAPI: Starlette checks a
  router's routes top-to-bottom and stops at the first path pattern that
  matches, so a literal path (`/search`) must be declared before a
  wildcard one (`/{service_id}`) that would otherwise swallow it.
- Why `EXISTS` beats `JOIN` for an optional filter here specifically:
  a join fans out one row per match, which is invisible until someone
  notices the *count* is wrong, not just the list.
- Why the seed script has to bypass its own CRUD endpoint's rule (setting
  `service.status = PUBLISHED` directly): a maintenance script operating
  on the database directly is a different trust boundary than the API -
  the *endpoint* still can't do it, which is the actual requirement.

**Carrying into Day 5**

- **Coverage is at 78%, just under the 80% MUST** - checked with
  `pytest --cov=app`. The gap is structural: every `app/api/v1/*.py`
  router file shows **0%** coverage, because every integration test this
  week calls the service-layer functions directly rather than going
  through a `TestClient` against the real routes. Manual `curl`
  verification proved the routes work, but pytest coverage can't see
  manual verification - task 1.11 (auth flow, role + patient-data
  enforcement, CRUD happy path + 2 failure cases) needs at least a
  handful of real `TestClient` tests to close this, not more
  service-layer tests.
- Task 1.11 (the rest of it) and 1.12 (README v1, confirm the ERD is
  current, finish `docs/design.md`) are Friday's work per the guidelines'
  own hour estimates - today stayed on 1.7/1.8/1.10 only.
- Friday's loop: self-check against the Definition of Done, close the
  coverage gap above, update docs, open the `week-1` → `main` PR, demo.
- **Mentor confirmed: full route-level coverage is expected**, not a
  representative sample. So Day 5's coverage work is a `TestClient`-based
  test file per router - auth header / wrong-role / happy-path / 404 /
  409 - for every endpoint in `app/api/v1/`: auth, departments, services
  (+ the public search route), providers, provider schedules
  (+ generate-slots). That's six route files, none of them tested at the
  HTTP layer yet.

**Open questions for my mentor**

- None outstanding - the one open question above was answered before
  Day 5 started.

---

## Weekly self-check

Answered honestly every Friday.

### Week 1 - 2026-08-24

1. **Finished / broken:** Auth, department/service/provider CRUD, provider
   schedules + slot generation, public search, seed script - all route-tested,
   142 tests, 98% coverage. Nothing known broken.
2. **Not fully understood yet:** Temporal's guarantees under a mid-workflow
   crash - read about it, haven't built or broken one yet.
3. **Most time spent:** Friday's route-level test sweep (six files). Worth it
   - it caught the flaky JWT test and closed the coverage gap for real.
4. **Carrying into Week 2:** the atomic slot-reservation UPDATE, the publish
   workflow, and the scheduling saga with compensation.
