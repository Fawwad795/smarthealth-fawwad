---
paths:
  - "app/api/**"
  - "app/schemas/**"
  - "app/main.py"
---

# Routers and schemas

## The layering rule

Routers **parse input, call a service, shape the response**. No business logic, no
SQL. **A router function longer than ~20 lines means logic has leaked in** — move it
to `services/`.

The body of almost every endpoint here is three lines: call the service, validate the
result into a response schema, return it.

## Never return an ORM object

Always `SomeResponse.model_validate(row)`, never the row. A `User` carries
`password_hash`; a `Patient` carries `contact` (PHI). Response schemas name their
fields explicitly, which is what makes leaking one impossible rather than unlikely.

## Status codes

`from fastapi import status` → `status.HTTP_404_NOT_FOUND`. **Never a bare integer**,
including on the decorator (`status_code=status.HTTP_201_CREATED`).

Which code means what, in this project:

| Code | Used for |
|---|---|
| 201 | A resource was created |
| 202 | Accepted — a Temporal workflow was started (`POST /appointments`, `POST /services/{id}/publish`) |
| 401 | Missing, invalid or expired token — never distinguished from each other |
| 403 | Authenticated but not allowed (wrong role, or another patient's data) |
| 404 | No such row — also what a row belonging to a different parent returns |
| 409 | A legal request that conflicts with current state: duplicate name, slot taken, illegal status transition |
| 422 | The request body failed validation (FastAPI raises this itself) |

## Access control — three separate checks

1. **Role** — `Depends(require_role(UserRole.ADMIN))`. "Is this a provider?"
2. **Ownership** — "does this provider own this schedule?" Check the path's parent id
   against the row; a mismatch is a **404**, not a 403, so the response cannot confirm
   that an id exists.
3. **Patient data** — `ensure_patient_self_or_staff(current_user, patient)`. A plain
   function, not a `Depends()`, because it needs a `Patient` row a router has already
   loaded.

These are different questions. `PATIENT` is a role every patient holds and says
nothing about *which* patient's data they may see.

**From Week 4 on: PHI scoping is enforced here, in the endpoint — never in a prompt.**
An LLM instructed to "only show this patient's data" is not an access control.
Retrieval for a patient-specific question must be scoped in the SQL query by the
authenticated caller's patient id before anything reaches the model.

## Request schemas gate what is settable

A field a client must not control **simply does not exist** on the request schema.
Pydantic drops unknown keys silently (no `extra="forbid"` anywhere here), so
`{"status": "PUBLISHED"}` on a `ServiceUpdate` vanishes rather than erroring.

Established cases — do not "fix" them by adding the field:

- `ServiceCreate` / `ServiceUpdate` have no `status` or `published_at`. Week 2's
  Temporal workflow is the only path to PUBLISHED.
- `ProviderUpdate` has no `user_id`.
- `ProviderScheduleCreate` has no `provider_id` — it comes from the URL path, so path
  and body cannot disagree.

**On an update schema, `None` means "not mentioned, leave it alone"** — never "set
this to null".

## Pagination

Every list endpoint takes `Depends(pagination_params)` and returns
`{items, total, limit, offset}`. `total` is the unpaginated count. `limit` is capped
at 100 in `app/core/pagination.py`, not per-endpoint.

## Route declaration order

Starlette matches routes **in declaration order**. A literal path must be declared
before a wildcard that would swallow it:

```python
@router.get("/search")        # must come first
@router.get("/{service_id}")  # would otherwise match "search" and 422
```

This bit once already, in `app/api/v1/services.py`. Watch for it with
`/appointments/{id}` vs any literal sub-route, and anything under `/reports/`.

## Docstrings

Every route function gets one saying **what it does, who may call it, and what the
non-200 outcomes are**. `"""Create a department. ADMIN only. 409 if the name is
taken, 404 if the clinic doesn't exist."""` — one line each is enough.

## Streaming endpoints (Week 5)

SSE via `StreamingResponse` with `media_type="text/event-stream"`. **Stream from the
provider** — generating the whole answer then chunking it is fake streaming and is
explicitly called out as a mistake. Never block the event loop: async clients, or
`await asyncio.to_thread(...)` for a sync SDK. Handle client disconnect without
hanging, and still persist what was generated.
