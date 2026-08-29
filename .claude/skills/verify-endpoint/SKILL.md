---
name: verify-endpoint
description: Prove an endpoint actually works by running it live against the running container - mint a token, seed throwaway data, curl the real routes, inspect the rows, clean up. Use in step 4 of any subtask that adds or changes an API endpoint, before writing the permanent test.
allowed-tools: Bash, Read, Write, Grep
---

# Verifying an endpoint for real

Automated tests call service functions or `TestClient`. This is the other half:
hitting the **actually running container** over HTTP, the way the mentor will
during a demo. Both Day 4 bugs in this repo were found here, not by pytest.

Do this **before** finalising the permanent test — write the test from what the
live check actually showed, rather than from what you assumed it would show.

## The loop

1. Check the containers are up.
2. Mint a token for each role the endpoint cares about, and seed the minimum rows.
3. `curl` the real routes: happy path, wrong role, missing id, and whatever this
   endpoint's specific failure mode is.
4. Inspect the resulting rows directly when the response alone doesn't prove it
   (timezone conversion, generated row counts, a status that must *not* have moved).
5. Delete the throwaway data.

## 1. Containers up

```bash
docker compose ps
```

If Docker itself is unreachable (`failed to connect to the docker API`), Docker
Desktop has stopped — ask the user to restart it rather than working around it.

## 2. Seed + mint tokens

Write the script to the scratchpad, copy it in, run it as a module-path-safe exec.
Three environment quirks this repo has already been bitten by:

- **`MSYS_NO_PATHCONV=1`** — without it, Git Bash rewrites `/tmp/x.py` into a
  Windows path and `docker compose cp` fails.
- **`PYTHONPATH=/app`** — `python /tmp/script.py` puts */tmp* on `sys.path[0]`, not
  the working directory, so `import app.*` fails without this.
- Prefer `python -m scripts.name` for anything that lives in the repo.

```bash
docker compose cp <scratchpad>/seed_x.py api:/tmp/seed_x.py
docker compose exec -T -e PYTHONPATH=/app api python /tmp/seed_x.py
```

A seed script that prints what the curls will need:

```python
import sys
sys.path.insert(0, "/app")
from app.db.session import SessionLocal
from app.models import User, Clinic, Department, Specialty, Provider
from app.models.enums import UserRole
from app.core.security import hash_password, create_access_token

db = SessionLocal()

# get-or-create everything, so a re-run after a failure doesn't collide
admin = db.query(User).filter(User.email == "verify-admin@example.com").first()
if admin is None:
    admin = User(email="verify-admin@example.com",
                 password_hash=hash_password("whatever123"), role=UserRole.ADMIN)
    db.add(admin); db.flush()
db.commit()

print("ADMIN_TOKEN=", create_access_token(admin.id))
```

Prefix every throwaway row with `Verify ` / `verify-` so cleanup can find it and
it is obvious in the database that it isn't seed data.

## 3. Curl the real routes

Always print the status code — a wrong-but-200 response is the thing to catch.

```bash
ADMIN="<token>"
BASE="http://localhost:8000/api/v1/<resource>"

echo "--- happy path (expect 201) ---"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE" \
  -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
  -d '{...}'

echo "--- wrong role (expect 403) ---"
curl -s -w "\nHTTP %{http_code}\n" -X POST "$BASE" \
  -H "Authorization: Bearer $PATIENT" -H "Content-Type: application/json" -d '{...}'

echo "--- missing id (expect 404) ---"
curl -s -w "\nHTTP %{http_code}\n" "$BASE/999999" -H "Authorization: Bearer $ADMIN"
```

Cover, every time:

- happy path
- **no auth header at all** (401 — or 200 if the route is deliberately public)
- **wrong role** (403)
- missing id (404)
- the conflict this endpoint can produce (409)
- **a field the schema deliberately refuses to accept** — send it anyway and prove
  it was ignored (e.g. `"status": "PUBLISHED"` on a service PATCH)
- if the route is idempotent, **run it twice** and check the second response

## 4. Inspect rows when the response isn't proof

```bash
docker compose exec -T api python -c "
from app.db.session import SessionLocal
from app.models import Slot
db = SessionLocal()
for s in db.query(Slot).filter(Slot.provider_id == 3).order_by(Slot.start_time).all():
    print(s.start_time.isoformat(), '->', s.end_time.isoformat(), s.status)
"
```

Do this whenever the endpoint's correctness is about something the JSON doesn't
show: UTC conversion, how many rows were generated, or a value that must have
stayed unchanged.

## 5. Clean up

Always. Leftover verification rows get mistaken for seed data later.

```python
db.query(Slot).filter(Slot.provider_id.in_(ids)).delete(synchronize_session=False)
...
db.query(User).filter(User.email.like("verify-%")).delete(synchronize_session=False)
db.commit()
```

Delete children before parents — every FK in this schema is `ON DELETE RESTRICT`.

## Reporting the result

Give the user a table of check → expected → got, say plainly whether it passed,
and if something failed, **name the exact bug and the exact fix** rather than
quietly correcting it. If the failure is in the *test or the check itself* rather
than the code, say so — that has happened more than once here.
