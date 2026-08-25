---
paths:
  - "tests/**"
  - "conftest.py"
---

# Testing rules

**Targets: ≥25 meaningful tests, ≥80% coverage.** Week 1 finished at 142 tests / 98%.

Run them with `make test` (`docker compose run --rm test`), which works from cold
without the API container running.

## What must be covered

Part A: slot double-booking under concurrency · duplicate booking · saga compensation
(slot released on billing failure) · illegal state transitions · unauthorized PHI
access · idempotent event handling.

Part B: published/offered retrieval filtering · PHI scoping · medical-advice refusal ·
malformed input · structured report validation · streaming shape.

**Testing only happy paths is called out as a mistake.** Write the test with the
behaviour, not "at the end".

## Structure established in Week 1 — follow it

- **`tests/unit/`** — no infrastructure needed. **`tests/integration/`** — needs the
  database. The split exists because Weeks 4–5 require the suite to pass with no
  network access.
- **The test schema is built by running the Alembic migrations**, never
  `Base.metadata.create_all()`. `create_all()` builds from the models, so a migration
  that disagreed with them could never fail a test — and migrations are what actually
  run on any machine that is not mine.
- **The test database is dropped and recreated every run.** Alembic will not re-apply
  a revision it has already recorded, so a reused database keeps an old schema after a
  migration is edited.
- **`db_session` wraps each test in a transaction that is rolled back**, using
  `join_transaction_mode="create_savepoint"` so a `commit()` inside a test commits a
  SAVEPOINT — constraints fire exactly as in production, but the database is left
  untouched.
- **Domain fixtures compose** (`clinic` → `department` → `provider`): a test names
  only what it needs and pytest builds the chain. Prefer them to a `make_x()` helper.
- **Route-level tests use `TestClient`** with auth-header fixtures. Service-layer
  tests call service functions directly. Both are needed — service tests alone leave
  every router at 0% coverage, which is how Week 1 nearly missed the 80% bar.

## Assertions

- **Assert constraint *names*** when testing an `IntegrityError` (`match=`). Without
  it, any `IntegrityError` passes — including one caused by a typo in the test data.
- **Assert the `code`, not just the status.** Two different 409s mean different things.
- When a test fails, **check the test's own premise before the code's.** Both bugs
  found on Day 4 were in tests that couldn't prove what they claimed: one tripped a
  different constraint than intended, the other assumed slots were service-scoped when
  the model deliberately makes them provider-scoped.

## Never

- Real network calls. Fakes behind the provider interfaces.
- Real patient data. Synthetic only.
- `CELERY_TASK_ALWAYS_EAGER=False` in tests.
