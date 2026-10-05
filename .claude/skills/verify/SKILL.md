---
name: verify
description: Check the work before calling it done - lint, docstrings, hardcoded status codes, tests, and coverage - then fix what fails and re-run. Use at the end of any subtask that changed code, before drafting a commit, and before opening a daily PR.
allowed-tools: Bash, Read, Edit, Grep
---

# Verify before claiming done

Run every check, fix what fails, **re-run until clean**. A check that was
fixed but not re-run has not passed.

Report a table of check → expected → actual. If something cannot be fixed,
say so plainly rather than quietly narrowing what was claimed.

## 1. Lint

```bash
docker compose run --rm test ruff check app scripts tests
```

Configured in `pyproject.toml` to enforce more than ruff's defaults:

- **D100–D103** — every module, class, method and function has a docstring.
  This exists because a mentor review found 77 missing; the default rule set
  does not check docstrings at all, so `make lint` used to pass while they
  were absent. Test functions are exempt (`per-file-ignores`) — their names
  are the description.
- **F** — pyflakes. Catches unused imports and undefined names.
- **E501 is deliberately off**: black owns line length and will not split an
  unbreakable string, so enforcing it here only flags what black cannot fix.

`--fix` resolves the mechanical ones. Read what it changed.

## 2. No hardcoded HTTP status codes

```bash
grep -rn "status_code=[0-9]" app/ && echo "FAIL: use fastapi.status" || echo "OK"
```

Must return nothing. `from fastapi import status` → `status.HTTP_404_NOT_FOUND`.
Ruff has no rule for this, so it is a grep — but it is the other half of the
same review finding as the docstrings.

## 3. Tests

```bash
make test        # or: docker compose run --rm test
```

Runs from cold; does not need the API container up. **Every test must pass** —
not "all but one known failure".

## 4. Coverage

```bash
make test-cov    # or: docker compose run --rm test pytest --cov=app --cov-report=term-missing --cov-fail-under=80
```

`--cov-fail-under=80` makes this a real gate — pytest exits non-zero below
80%, not just a number left for a human to notice. **The number alone still
isn't the whole check** — look at *which* lines are missing even when it
passes. Week 1 sat at 78% with every router file at 0%, because
the tests called service functions directly and never went through a route.
The percentage looked nearly fine; the coverage was structurally wrong.

If a whole file reads 0%, that layer is untested regardless of the total.

## 5. When the endpoint is new or changed

Lint and tests do not prove an endpoint behaves correctly over HTTP. Use the
`verify-endpoint` skill for the live curl round-trip as well.

## Reading a failure

When a check fails, **question the check's own premise before the code's**.
Two bugs in Week 1 were in tests that could not prove what they claimed: one
tripped a different constraint than intended, the other assumed slots were
service-scoped when the model deliberately makes them provider-scoped. Both
looked like code failures and were not.
