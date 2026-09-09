# PRD — SmartHealth

Healthcare **operations** backend for MediNova: appointments, provider schedules,
services, simulated billing pre-checks, and (Weeks 4–5) a retrieval-backed
assistant. API only — no UI. No clinical data anywhere.

---

## 1. Problems

| # | Problem | Approach |
|---|---|---|
| 1 | Scheduling is manual, and two patients can hold one slot | Atomic conditional slot UPDATE + a Temporal saga |
| 2 | Patients can't find the right service | Published-catalogue search now; semantic retrieval in Week 4 |
| 3 | Dashboards disagree with the tables | Aggregates maintained by an event consumer + a reconciliation script (Week 3) |
| 4 | Peak-window timeouts | Reservation, billing and reminders moved out of the request (Weeks 2–3) |
| 5 | Background failures are invisible | Correlation IDs, `/metrics`, dead-letter table (Week 3) |

## 2. Use cases

- A patient registers, searches the published catalogue, and books a slot.
- Two patients race for the last slot; exactly one gets it.
- A patient retries a booking after a dropped connection; one appointment results.
- Billing fails after the slot was held; the saga releases it and the slot is bookable again.
- A patient cancels or reschedules; the slot is released and the next waiting patient is offered it.
- Front desk checks a patient in; the provider starts and completes the visit.
- An admin publishes a service; it becomes searchable, and re-publishing replaces its chunks cleanly.

## 3. Functional requirements

| ID | Requirement | Week |
|---|---|---|
| FR-1 | Registration, login, four roles, JWT auth | 1 |
| FR-2 | Patients see only their own data; staff scoping enforced in the endpoint | 1 |
| FR-3 | Department / service / provider CRUD, admin-gated | 1 |
| FR-4 | Provider schedule templates generating discrete `Slot` rows, idempotently | 1 |
| FR-5 | Public search over published, offered services | 1 |
| FR-6 | Service publishing as a durable Temporal workflow writing `content_chunks` | 2 |
| FR-7 | Illegal publish entry actions rejected with 409 | 2 |
| FR-8 | Slot reservation is a single atomic conditional UPDATE | 2 |
| FR-9 | Booking idempotency: `Idempotency-Key` → the original appointment | 2 |
| FR-10 | Simulated, idempotent billing pre-check, forcible failure | 2 |
| FR-11 | Scheduling saga with compensation releasing the slot | 2 |
| FR-12 | Every appointment status change recorded with an actor | 2 |
| FR-13 | Cancel releases the slot and moves the waitlist | 2 |
| FR-14 | Reschedule releases old + reserves new atomically | 2 |
| FR-15 | Visit lifecycle `CHECKED_IN → IN_PROGRESS → COMPLETED`, idempotent, 409 on illegal jumps | 2 |
| FR-16 | Celery reminders + analytics rollup, retries, dead-letter table | 3 |
| FR-17 | Kafka events (ids only) consumed idempotently into aggregates | 3 |
| FR-18 | Analytics endpoints served from aggregates + reconciliation | 3 |
| FR-19 | Chunking, embeddings, PHI-scoped semantic retrieval | 4 |
| FR-20 | Assistant refuses medical advice; grounded, cited answers; SSE streaming | 5 |

## 4. Non-functional requirements

| ID | Requirement | Status |
|---|---|---|
| NFR-1 | No double-booking under ~50 concurrent attempts | Met |
| NFR-2 | ≥25 meaningful tests, ≥80% coverage | Met — 264 tests, 98% |
| NFR-3 | Schema built solely by Alembic; every revision round-tripped | Met |
| NFR-4 | No PHI in logs, events or AI records — ids only | Enforced by review; Week 3 adds logging |
| NFR-5 | No clinical content in code, prompts, seeds or docs | Met |
| NFR-6 | Config from env vars; `.env` never committed | Met |
| NFR-7 | Workflows deterministic; Activities idempotent | Met |
| NFR-8 | Crash mid-workflow resumes without duplicate side effects | Demonstrated live (Weeks 2–3) |
| NFR-9 | One command to run; tests runnable from cold | Met |

## 5. Milestones

| Week | Deliverable | Status |
|---|---|---|
| 1 | Auth, core domain, schedules/slots, search, seed | Done |
| 2 | Temporal publishing + scheduling saga, slot concurrency, visits | Done |
| 3 | Celery, Kafka, analytics, observability | Not started |
| 4 | Chunking, embeddings, retrieval | Not started |
| 5 | Assistant, streaming, demo | Not started |

## 6. Traceability

| Requirement | Implementation | Test |
|---|---|---|
| FR-1 | `app/services/auth.py`, `app/core/security.py` | `test_auth_routes.py`, `test_auth_service.py` |
| FR-2 | `app/core/dependencies.py` (`ensure_patient_self_or_staff`) | `test_dependencies.py`, `test_appointment_routes.py::test_get_appointment_state_forbidden_for_a_different_patient` |
| FR-3 | `app/services/{department,service,provider}.py` | `test_department_routes.py`, `test_service_routes.py`, `test_provider_routes.py` |
| FR-4 | `app/services/provider_schedule.py` | `test_provider_schedule_service.py`, `test_provider_schedule_routes.py` |
| FR-5 | `app/services/service_search.py` | `test_service_search_service.py` |
| FR-6 | `app/temporal/workflows.py::PublishServiceWorkflow`, `activities.py::PublishActivities` | `test_publish_workflow.py`, `test_publish_activities.py` |
| FR-7 | `app/services/service_publish.py::ensure_can_publish` | `test_service_publish_guard.py`, `test_service_publish_routes.py` |
| FR-8 | `app/services/slot.py::reserve_slot_uncommitted` | `test_slot_service.py::test_concurrent_reservations_exactly_one_wins` |
| FR-9 | `app/services/idempotency.py`, `appointments.idempotency_key` | `test_appointment_routes.py::test_create_appointment_repeated_idempotency_key_returns_original`, `::test_create_appointment_falls_back_to_the_database_when_redis_loses_the_key` |
| FR-10 | `app/services/billing.py::BillingChecker` | `test_billing_service.py` |
| FR-11 | `workflows.py::AppointmentSchedulingWorkflow`, `activities.py::SchedulingActivities` | `test_scheduling_workflow.py::test_scheduling_workflow_compensates_on_billing_failure`, `test_scheduling_activities.py::test_release_slot_compensates_correctly` |
| FR-12 | `app/models/appointment_status_history.py` | `test_schema_constraints.py`, `test_visit_routes.py::test_complete_finishes_the_visit_and_the_appointment` |
| FR-13 | `appointment_scheduling.py::cancel_appointment`, `waitlist.py::promote_next_waiting` | `test_appointment_cancel.py::test_cancel_promotes_the_oldest_waiting_entry` |
| FR-14 | `appointment_scheduling.py::reschedule_appointment` | `test_appointment_reschedule.py::test_a_failed_reschedule_leaves_the_original_slot_untouched` |
| FR-15 | `app/services/visit.py` | `test_visit_routes.py` (16 tests) |
| FR-16–FR-20 | Not built | — |

## 7. Known gaps

- Cancel/reschedule are proven; **the saga's compensation has no single end-to-end
  test** — it is covered as two halves (orchestration + Activity DB writes) plus a
  live demonstration.
- A booking whose workflow never starts (Temporal unreachable) stays `REQUESTED`
  with nothing to retry it.
- Overlapping *schedule* windows for one provider are not prevented; the invariant
  that matters is enforced on the slots they generate.
