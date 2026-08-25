# CLAUDE.md — SmartHealth

Working context for this repo. Derived from the three assignment documents
(`part-a.md`, `part-b.md`, `execution-guidelines.md`). **Those docs are the source
of truth; this file is the working summary.** Read them at
`.claude/reference/*.md` — converted from the `.docx` originals by
`scripts/convert_briefs.py`. Grep them for exact wording rather than reading them
whole; the `assignment-brief` skill has the landmarks.

Detail that only matters in one part of the codebase lives in `.claude/rules/`
and loads on demand:

| Rule file | Loads when working on |
|---|---|
| `data-model.md` | `app/models/**`, `migrations/**` |
| `api-and-schemas.md` | `app/api/**`, `app/schemas/**` |
| `workflows-and-sagas.md` | `app/temporal/**`, `app/services/**` |
| `events-observability.md` | `app/events/**`, `app/workers/**`, `app/core/**` |
| `ai-layer.md` | `app/ai/**` |
| `testing.md` | `tests/**` |
| `docs.md` | `docs/**`, `README.md`, `NOTES.md` |

Path-scoped rules load when a **matching file is read**, so a day that starts by
*creating* files in a new directory may not trigger them — read the relevant rule
explicitly (the `start-day` skill does this).

**At the end of each week, write newly-established conventions back into the
matching rule file.** These files started as a distillation of the brief; they stay
useful only if what each week actually settles gets added.

---

## 1. What this is

**SmartHealth** — an intelligent healthcare *operations* and patient-engagement
platform for MediNova. A 5-week individual assignment, own repo, weekly mentor
review.

- **Backend API only.** No patient portal, no front-desk UI, no web page. Postman /
  curl / Swagger UI is the interface. Building any UI actively costs marks.
- **Operations, not clinical.** Appointments, provider schedules, services, billing
  pre-checks. Never diagnoses, prescriptions, lab results, medical records or EMR.

The five problems being fixed: slow/manual scheduling where two patients can hold
one slot; patients unable to find the right service; dashboards disagreeing with
tables; peak-window timeouts; invisible background failures.

## 2. Timeline

| Week | Theme | Part |
|---|---|---|
| 1 | Foundation & core domain (patients, providers, schedules) | A |
| 2 | Temporal workflows, scheduling, slots | A |
| 3 | Celery, Kafka, observability | A |
| 4 | Chunking, embeddings, retrieval | B |
| 5 | AI assistant, streaming, polish, demo | B |

Weekly loop: Mon plan with mentor → Mon–Thu build, commit daily → Fri AM self-check
+ update docs → Fri PM PR + demo. Fix review feedback before starting the next week.

**[MUST]** required · **[SHOULD]** expected · **[STRETCH]** only if genuinely ahead.
Drop order: STRETCH → SHOULD → *never* MUST. And tell the mentor.

## 3. Tech stack (required — do not substitute)

Python 3.11+ · FastAPI · Pydantic v2 · PostgreSQL + SQLAlchemy 2.x + Alembic ·
Redis · Celery · **Temporal** · **Kafka** (single node, JSON) · prometheus-client ·
structlog or stdlib JSON logging · Docker Compose · pytest.

Part B: LangChain/LangGraph or a thin direct SDK; OpenAI / Groq / Anthropic;
**pgvector in the existing Postgres**, behind a small swappable interface.

Hostnames — inside a container vs. from the laptop:
`postgres:5432` / `localhost:5432` · `redis:6379` / `localhost:6379` ·
`temporal:7233` / `localhost:7233` · `kafka:9092` / `localhost:29092` ·
Kafka UI `localhost:8080` · Temporal UI `localhost:8233`.
**Using `localhost` from inside a container is the single most common mistake.**

## 4. Out of scope (building these costs marks)

Any UI. Microservices / Kubernetes / cloud deploy. A second (NoSQL) database — use
Postgres JSONB. Any clinical data. Real payment/insurance, SMS/email, calendar sync.
OCR / PDF parsing. Multi-clinic sync. RabbitMQ, Schema Registry, Avro/Protobuf.
GraphQL, WebSockets (Part B uses SSE). Fine-tuning. Agents with tool use / browsing.
Clinical reasoning or triage scoring. Voice/image understanding. Building a vector DB.

## 5. Architecture and layering

```
api/       FastAPI routers. Parse input, call a service, shape the response.
           No business logic. No SQL. >~20 lines = logic has leaked in.
services/  Business rules. "Is this slot free?" "Can this patient see this?"
models/    SQLAlchemy ORM. Database shape only.
schemas/   Pydantic request/response. Never return an ORM object from an endpoint.
core/      Config, security, logging, exceptions, dependencies, metrics.
```

**Division of labour:** Temporal owns multi-step durable workflows (service
publishing, appointment scheduling saga). Celery owns fire-and-forget work
(reminders, analytics rollup). The visit lifecycle is neither — a plain validated
status flow driven by human actions.

## 6. The non-negotiables

Each is expanded in the matching file under `.claude/rules/`.

1. **Slot reservation is a single atomic conditional UPDATE** guarded on
   `status = 'AVAILABLE'`. Never SELECT-then-check-then-UPDATE.
2. **Idempotency at four levels**: client `Idempotency-Key` header, the atomic
   reservation, retry-safe Temporal Activities, `processed_events` for consumers.
3. **Temporal Workflows are deterministic**: no `datetime.now()`, no `random`, no
   DB calls, no network inside a Workflow function. All I/O lives in Activities.
4. **Compensation must actually restore state.** Never delete an appointment row —
   transition it.
5. **Migrations, not `create_all()`.** Every model change gets an Alembic revision.
6. **PHI discipline**: ids in logs, events and `ai_interactions` — never names,
   contacts or patient text. Scope every patient-specific query to the caller.
7. **No clinical content** anywhere — code, prompts, seed data or docs.
8. **Synthetic data only**, in seeds and tests.
9. **No secrets in git.** `.env` stays ignored; `.env.example` stays current.
10. **The AI assistant refuses medical advice** and never surfaces one patient's
    data to another.

## 7. Engineering requirements

- `docker compose up` starts everything. A mentor installs nothing locally.
- `alembic upgrade head` builds the whole schema from an empty DB.
- Seed script → synthetic demo dataset in one command (`make seed`).
- Config via env vars; `.env.example` committed and accurate.
- **pytest, ≥25 meaningful tests, ≥80% coverage.** See `rules/testing.md`.
- Meaningful commits, one branch per week, one PR per week. `main` stays working.
  Commit daily — one giant Friday commit is unreviewable.
- `make help` lists every task target.

## 8. Deliverables

`README.md` · `docs/design.md` · `docs/events.md` · `docs/runbook.md` ·
`docs/ai-layer.md` · `docs/prd.md` (with the traceability table) · `NOTES.md`
(working log + weekly tracking tables).

Update `README.md`, `docs/design.md` and the PRD traceability table **at the end of
every week**. Docs written on the last afternoon are visible and penalised.

## 9. Assessment

Working software 30% · correctness under stress 20% · code quality 20% ·
documentation 15% · understanding & communication 15%.

Raises the score: *"I chose X over Y because Z, and the tradeoff is W"*; tests that
prove the hard cases; an honest list of known limitations. Lowers it: a large
half-working feature set; code that can't be explained; last-minute docs.

> A working, well-understood smaller system beats a half-finished ambitious one.

## 10. Working rules for Claude in this repo

1. **Explainability is graded.** The mentor will point at a random block and ask
   what it does and why; *"the AI wrote it"* ends the review. Generate code that is
   straightforward, conventional and commented where non-obvious — never clever.
2. **Every function, class and module gets a docstring.** Say *why*, not just what.
3. **Never hardcode HTTP status codes.** Use `from fastapi import status` →
   `status.HTTP_404_NOT_FOUND`.
4. **Never violate a MUST to deliver a STRETCH.**
5. **No UI. Ever.** If asked for one, flag that it is explicitly out of scope.
6. **Never return an ORM object from an endpoint.**
7. **Write the test with the behaviour**, especially edge cases — not "at the end".
8. Keep `docs/` and `NOTES.md` current as work lands, not at the end of the week.
9. **Never run state-changing git commands** (`add`, `commit`, `push`, `checkout`).
   Draft them in the chat for me to run. Read-only (`status`, `log`, `diff`) is fine.
   PowerShell: single-quoted strings, a literal apostrophe as `''`, never backslash
   escapes.

## 11. How to walk me through work

Subtasks are delivered in **five steps**, each needing my explicit approval before
the next: **1** what the task is, in beginner-friendly language · **2** why it
matters · **3** the code in the chat for me to type myself (never written to files
unless I ask), with a test when the behaviour is worth pinning down · **4**
verification — a live check (throwaway script, curl, TestClient) proving the real
behaviour, naming the exact bug and fix if anything is wrong · **5** the
`git add`/`git commit` commands printed for me to run.

Announce which subtask and step at the top of each response; end by asking to
advance. **If I say to skip steps or just do the work myself, that overrides this
for that piece of work** — don't re-litigate it.
