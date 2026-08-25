# NOTES

Working log: what I tried, what confused me, decisions I made and why.
Kept as I go, not written up at the end of the week.

---

## Tracking tables

### Part A (Weeks 1-3)

| Week | Focus | Must be true by Friday | Status |
|---|---|---|---|
| 1 | Foundation & core domain | Auth + roles + patient-data protection, providers/services/slots CRUD, migrations, seed, 80% coverage | ☑ 142 tests, 98% - PR #1 approved, deliberately left unmerged |
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
