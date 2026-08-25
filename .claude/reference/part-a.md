<!-- Converted from part-a.md.docx by scripts/convert_briefs.py.
     Source of truth for this project; .claude/CLAUDE.md is the
     distilled working summary. Re-run the script after the
     briefs are edited. Do not hand-edit this file. -->

## SmartHealth — Intelligent Healthcare Operations & Patient Engagement Platform [Part A]

Duration: Weeks 1–3 of 5 Type: Individual assignment. You build this on your own, in your own repository, with a mentor reviewing your work weekly.

### 1. The Scenario

MediNova is building SmartHealth, a healthcare operations platform for clinics, hospitals and telemedicine providers. Front-desk staff register patients and manage schedules; providers define their availability; patients book appointments and attend visits.

This is an operations and scheduling system, not a clinical/EMR system. You handle appointments, provider schedules, services and billing pre-checks — never diagnoses, prescriptions or medical records.

Volume is growing fast and the current backend is breaking down in five ways:

- Scheduling is slow and manual. Booking an appointment and confirming a slot is unreliable, and two patients can end up holding the same slot.
- Patients can't find the right service. There is no intelligent guidance to the right specialty or preparation steps. (You solve this in Part B.)
- Operational data disagrees with itself. Dashboards show appointment and visit numbers that don't match the actual tables.
- Peak booking windows cause timeouts. Slot reservation, billing pre-checks and reminders run inside the HTTP request, so a spike makes the whole API slow.
- Failures are invisible. When a background step dies mid-booking, nobody knows until a patient turns up to a cancelled slot.

Your job in Part A: build the backend foundation that fixes problems 1, 3, 4 and 5.

You are building a backend API only. No patient portal, no front-desk UI. Postman/curl/Swagger UI is your interface. Do not spend a single hour on a web page.

### 2. What "Good" Looks Like

By the end of Week 3, a mentor should be able to clone your repo, run one command, and then:

- Register a provider, define a schedule with slots, and publish a service — and watch a Temporal workflow validate and chunk the service content so Part B can search it later.
- Book an appointment and watch the scheduling saga reserve the slot, run a billing pre-check, schedule reminders and mark it CONFIRMED only after every step succeeds.
- Point five concurrent bookings at the same slot and see exactly one confirmed — the slot is never double-booked.
- Book the same appointment twice with one idempotency key and see one appointment, not two.
- Force a billing pre-check failure after the slot was reserved and watch the saga compensate — release the slot, cancel the appointment cleanly — so the slot is bookable again.
- Kill the Temporal worker mid-booking, restart it, and watch the saga finish with no double reservation.
- Cancel a confirmed appointment and watch the slot release and the waitlist move.
- Check a patient in, progress the visit, and complete it.
- Call an analytics endpoint and see numbers that exactly match the raw tables.
- Read your logs and follow a single booking end-to-end using one correlation ID.

Those middle bullets — the slot race and the saga compensation — are the real assignment.

### 3. Functional Requirements

Requirements are labelled:

- [MUST] — required. Missing these means the assignment is incomplete.
- [SHOULD] — expected if Weeks 1–2 went to plan.
- [STRETCH] — only if you are genuinely ahead. Never at the cost of a MUST.

#### 3.1 Users & Roles

- [MUST] Registration and login. Passwords hashed (bcrypt via passlib — never plain text).
- [MUST] JWT-based authentication on all non-public endpoints.
- [MUST] Four roles: patient, provider, front_desk, admin.
- patient: book/reschedule/cancel their own appointments, view their booking history.
- provider: manage their own schedule and slots, view their appointments, progress visits.
- front_desk: register patients, book on their behalf, check patients in.
- admin: manage departments/services, read analytics.
- [MUST] Authorization is enforced server-side, and it protects patient data (PHI). A patient calling a provider schedule endpoint gets 403. A patient can never read another patient's profile, appointments or history — enforced server-side, not by obscurity.
- [SHOULD] Refresh tokens.
- [STRETCH] Per-clinic staff scoping (a front-desk user only sees their clinic).

#### 3.2 Providers, Services & Schedules

- [MUST] Provider profiles with a specialty and a department; departments belong to a clinic (a single clinic is fine — see Out of Scope on multi-clinic).
- [MUST] Services (e.g. "MRI scan", "cardiology consult") with a description and preparation instructions, owned by a department, with a DRAFT / PUBLISHED status.
- [MUST] Provider schedules made of discrete slots (start/end time) with a status (AVAILABLE / RESERVED / BOOKED / BLOCKED). A slot is the unit that gets booked.
- [MUST] Listing endpoints are paginated and filterable (by specialty, department, available-slots, search on service name). Patients see only PUBLISHED services and AVAILABLE slots.
- [MUST] Publishing a service produces searchable content chunks (Week 2): split the service description + preparation instructions (and the provider's specialty blurb) into a content_chunks table (source_type, source_id, chunk_index, text, token_count). This is what Part B consumes — if it isn't populated, Part B has nothing to search.
- [MUST] Audit trail for profile, schedule and service changes.
- [SHOULD] Recurring schedule templates that generate slots.
- [STRETCH] Provider time-off that blocks slots.

#### 3.3 Service Content Publishing — a Temporal workflow

The first hard requirement. Publishing a service is not a single UPDATE status = 'published'. It is a durable, multi-step Temporal workflow (the producer for Part B's search index).

- [MUST] A service moves through these states, driven by the workflow:

DRAFT ──publish──> PUBLISHING ──success──> PUBLISHED ──unpublish──> INACTIVE

│

└──failure──> PUBLISH_FAILED ──retry──> PUBLISHING



- [MUST] Publishing is implemented as a Temporal Workflow whose steps are Activities: validate completeness → structure the operational content → chunk it → mark PUBLISHED. Temporal provides the durability, retries and resumption — you do not hand-roll a job table or a polling loop, and there is no separate state-machine module.
- [MUST] Each Activity is idempotent: re-running it after a retry or a worker crash must not duplicate chunks. Re-publishing a service replaces its chunks atomically.
- [MUST] Validation runs first (missing description or preparation instructions, no owning department) and returns all errors at once. A validation failure ends the workflow cleanly in PUBLISH_FAILED; a transient failure is retried by Temporal.
- [MUST] Illegal entry transitions are rejected with 409 Conflict, checked in one place before the workflow starts. The service's status reflects where the workflow is.
- [MUST] POST /services/{id}/publish starts the workflow (202 Accepted + workflow id); GET /services/{id}/publish-status reports progress by querying the workflow.
- [MUST] The workflow is crash-safe: killing the Temporal worker mid-publish and restarting resumes it with no duplicated chunks. Demonstrate this.

#### 3.4 Appointment Scheduling & Slots — a Temporal saga

The second hard requirement, and the concurrency-critical one. It is a peak booking window: many patients hit the same open slot in the same instant. A slot must never be booked twice.

- [MUST] Booking an appointment is implemented as a Temporal Workflow (a saga) whose steps are Activities: validate eligibility → reserve the slot → billing pre-check → schedule reminders → confirm. If any step fails, the saga runs compensating Activities in reverse (release the reserved slot, cancel scheduled reminders) and lands the appointment in a clean terminal state.
- [MUST] An appointment moves through these states, driven by the saga:

REQUESTED ──reserve──> SLOT_RESERVED ──billing──> CONFIRMED ──(visit)──> COMPLETED

│                       │

(ineligible)         (billing fails)

▼                       ▼

REJECTED         compensate(release slot) → CANCELLED

▲

patient cancel / reschedule (release slot, move waitlist)



- [MUST] Slot reservation is correct under concurrency. Reserving a slot is an atomic conditional update (UPDATE slots SET status='RESERVED' WHERE id=:id AND status='AVAILABLE' RETURNING id) — no row back means "already taken". A SELECT-then-UPDATE check is not acceptable; explain in your docs why it races. Prove it: ~50 concurrent bookings at one slot yield exactly one confirmed appointment and no double-booking.
- [MUST] Bookings are idempotent. The client sends an Idempotency-Key; the same key returns the same appointment and never creates a second one or a second billing record.
- [MUST] Billing pre-check is simulated by an internal BillingChecker (a class you write — no real payment/insurance integration) that records a billing row and can be forced to fail.
- [MUST] Compensation actually restores state. A billing failure after reservation must release the slot so it returns to AVAILABLE. A cancellation releases the slot and moves the waitlist (the next waiting patient can be offered it). Deleting the appointment row is not acceptable — preserve the history.
- [MUST] Rescheduling releases the old slot and reserves a new one — atomically, with no window where the patient holds neither or both.
- [MUST] POST /appointments starts the saga (202 Accepted + appointment id); GET /appointments/{id} reports the current state (queried from the workflow, or the projected status).
- [MUST] Every state change is recorded in an appointment_status_history table — the audit trail and the source the analytics reads.
- [MUST] The saga is crash-safe: killing the Temporal worker mid-booking and restarting resumes it with no double reservation. Demonstrate this.
- [SHOULD] A waitlist that fills a released slot.
- [STRETCH] No-show detection that frees the slot after a grace period.

#### 3.5 Visit & Service Workflow

- [MUST] A confirmed appointment progresses through a visit lifecycle — a lightweight validated status flow (CHECKED_IN → IN_PROGRESS → COMPLETED) driven by front_desk/provider actions. Illegal jumps (completing a visit that was never checked in) return 409.
- [MUST] Check-in and completion are idempotent: a retried "check in" doesn't create duplicate history rows or advance the visit twice.
- [MUST] Completing a visit triggers (outside the request, Week 3) a billing update and a follow-up reminder, and updates analytics.
- [SHOULD] Average wait time captured from check-in vs. appointment time.
- [STRETCH] Follow-up appointment suggestion on completion.

#### 3.6 Background Processing & Events (Week 3)

- [MUST] The content-publishing workflow and the scheduling saga run on Temporal (see §3.3, §3.4). Separately, Celery workers (Redis as broker) handle the remaining background work:
- reminder/notification tasks (a "notification" is a row in a notifications table plus a log line — do not integrate real SMS/email),
- a periodic analytics rollup.
- [MUST] Celery tasks are retried with backoff on transient failure, with a maximum attempt count. After the final failure the task lands in a dead-letter table with the error and payload.
- [MUST] Publish domain events to Kafka (plain JSON, one topic per type or one topic with a type field — your choice, documented):
- appointment.booked, appointment.confirmed, appointment.cancelled, visit.completed, service.published, billing.updated.
- [MUST] Every event has an envelope: event_id (UUID), event_type, occurred_at, version, correlation_id, and a data object (carry ids, never patient PHI). Document each in docs/events.md.
- [MUST] A consumer reads these events and updates the analytics tables.
- [MUST] The consumer is idempotent: a processed_events table with a unique event_id, checked before processing. Replaying visit.completed twice must not double-count. Demonstrate this.
- [SHOULD] Write events to an outbox table inside the same DB transaction as the business change, and publish from the outbox. Explain the dual-write problem it solves.
- [STRETCH] Consumer lag metric; partition-by-provider_id; Schema Registry + Avro.

#### 3.7 Analytics

- [MUST] These six metrics, exposed via API and matching the source tables exactly:
- Total patients
- Appointments booked over time (daily buckets, date-range filter)
- Completed visits
- Cancellation rate (cancelled vs. booked)
- Average wait time (check-in vs. scheduled time)
- Failed workflows / background jobs (from your dead-letter table)
- [MUST] Metrics are served from pre-aggregated tables maintained by the event consumer — not by running COUNT(*) over everything on each request. Add a reconciliation endpoint or script that compares aggregates against raw tables and reports drift.
- [SHOULD] Redis caching on the analytics endpoints with a documented TTL and invalidation rule.
- [STRETCH] Provider utilisation rate; no-show rate.

#### 3.8 Observability & Reliability

- [MUST] Structured JSON logs. Every log line carries timestamp, level, logger, correlation_id, and message. No bare print(). Never log patient PHI (names, contact details) — log ids.
- [MUST] A correlation ID per request, propagated into Temporal workflows/activities, Celery tasks and Kafka envelopes. One ID must trace a booking "requested → slot reserved → confirmed → analytics updated" across API, worker and consumer logs.
- [MUST] GET /health (liveness) and GET /health/ready (checks DB, Redis, Kafka, Temporal).
- [MUST] GET /metrics in Prometheus format: request count/latency by endpoint and status, plus at least two domain counters (e.g. appointments_booked_total, double_booking_prevented_total).
- [MUST] Consistent error responses: one JSON error shape, correct status codes, and no stack traces, DB errors, or PHI leaked to clients.
- [SHOULD] A docs/runbook.md: "a booking saga is stuck — how do I diagnose it?" (including the Temporal UI), "reminders aren't going out — where do I look?" with exact queries/commands.
- [STRETCH] OpenTelemetry tracing with Jaeger; a Grafana dashboard.

#### 3.9 Engineering Requirements

- [MUST] docker compose up starts everything: API, Temporal worker, Celery worker, consumer, Postgres, Redis, Kafka, Temporal. A mentor must not have to install anything locally.
- [MUST] Alembic migrations. No create_all() in application code.
- [MUST] A seed script that creates a realistic demo dataset (providers with schedules, services, patients, a few appointments) — using synthetic patient data only.
- [MUST] Config via environment variables and a committed .env.example. No secrets in git.
- [MUST] Tests with pytest: minimum 25 meaningful tests, specifically covering slot double-booking under concurrency, duplicate booking, saga compensation (slot released on billing failure), illegal state transitions, unauthorized PHI access, and idempotent event handling.
- [MUST] Meaningful commits. One branch per week, merged via a PR your mentor reviews.
- [SHOULD] ruff + black clean; type hints on service functions; a Makefile or justfile.
- [STRETCH] CI running lint + tests on every PR.

### 4. Explicitly Out of Scope

Do not build these. They will not earn you marks and they will cost you the assignment.

- Any patient/front-desk/provider UI.
- Microservices, service mesh, Kubernetes, cloud deployment.
- A second (NoSQL) database. Use PostgreSQL JSONB where a flexible schema helps, and say so.
- Any clinical data: diagnoses, prescriptions, lab results, medical records / EMR. This is an operations system — appointments, schedules, services and billing pre-checks only.
- Real payment/insurance integration; real SMS/email; real calendar (Google/Outlook) sync.
- Parsing real PDF/scanned documents (OCR). Operational content is text.
- Multi-clinic / cross-facility inventory or schedule synchronisation. A single clinic is enough.
- RabbitMQ, Schema Registry, Avro/Protobuf (Kafka with JSON is enough).
- GraphQL, WebSockets (Part B uses simple SSE streaming).

### 5. Tech Stack

Required

- Python 3.11+, FastAPI, Pydantic v2
- PostgreSQL + SQLAlchemy 2.x + Alembic
- Redis — Celery broker + caching + idempotency keys
- Celery — background tasks (reminders/notifications, analytics rollup)
- Temporal (provided in the starter compose file) — durable orchestration of the service-publishing workflow and the appointment-scheduling saga
- Kafka (single node, provided) — domain events, JSON payloads
- Prometheus client for /metrics; structured logging (structlog or stdlib logging with a JSON formatter)
- Docker + Docker Compose
- pytest

Optional / stretch: Schema Registry, OpenTelemetry + Jaeger, Grafana, RabbitMQ.

Infrastructure is provided in starter-kit/docker-compose.yml. Use it — do not spend Week 1 debugging a Kafka or Temporal config.

### 6. Deliverables

Everything lives in your GitHub repo:

- Source code — organised by domain module (see execution-guidelines.md for the suggested layout).
- README.md — what the project is, architecture diagram, how to run it in one command, how to run tests, API overview, environment variables.
- docs/design.md — data model / ERD, module breakdown, the publishing workflow and the scheduling saga (Temporal), the slot concurrency approach, and key decisions with tradeoffs.
- docs/events.md — every event, its schema, producer, consumer, and how idempotency is guaranteed.
- docs/runbook.md — how to diagnose the three most likely failures.
- A short PRD (2–3 pages) — key use cases, functional + non-functional requirements, milestones, and a traceability table mapping each requirement to its implementation and test.
- Tests — passing, and runnable with one command.
- A 15-minute demo in Week 3, walking through the scenarios in section 2.

### 7. How You Will Be Assessed

| Area | Weight | What we look for |
|---|---|---|
| It works end-to-end | 30% | Mentor can run it and complete every scenario in section 2 |
| Correctness under stress | 20% | Slot double-booking prevented, duplicates, saga compensation, retries, idempotency |
| Code quality & structure | 20% | Clear layering, no copy-paste, readable naming, tests that mean something |
| Documentation | 15% | Someone else could pick this up; decisions are justified |
| Understanding | 15% | You can explain why, and what you'd change with more time |

A working, well-understood smaller system beats a half-finished ambitious one. If you must drop something, drop a STRETCH, then a SHOULD. Never a MUST. And tell your mentor when you do.
