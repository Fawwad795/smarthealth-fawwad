# PRD — SmartHealth

Healthcare **operations** backend for MediNova: appointments, provider schedules,
services, simulated billing pre-checks, and (Weeks 4–5) a retrieval-backed
assistant. API only — no UI. No clinical data anywhere.

---

## 1. Problems

| # | Problem | Approach |
|---|---|---|
| 1 | Scheduling is manual, and two patients can hold one slot | Atomic conditional slot UPDATE + a Temporal saga |
| 2 | Patients can't find the right service | Published-catalogue search (Week 1); semantic search over published services (Week 4) |
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
- A patient describes a service in their own words and gets the nearest published services, or "we don't offer that".

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
| FR-19a | One enriched chunk per published service: department, specialties, name, description, preparation | 4 |
| FR-19b | Embedding as a retried Activity in the publish workflow; vectors stored with their metadata; progress visible | 4 |
| FR-19c | Re-publishing re-indexes: no stale vectors, no duplicates, unchanged chunks not re-embedded | 4 |
| FR-19d | `POST /search`: top-k with scores, a threshold giving "we don't offer that", published filter in SQL, PHI scoping | 4 |
| FR-19e | An eval set with a hit-rate script, which chose the threshold | 4 |
| FR-20 | Assistant refuses medical advice; grounded, cited answers; preparation and availability answers from the caller's own appointments and real open slots, never another patient's; bad input is a 422, never a 500; SSE streaming | 5 |
| FR-21 | Staff generation for front desk and admin: an appointment summary, a follow-up draft and a utilisation report over a date range; every number from the database, the report validated with one repair retry; every run saved with its prompt version and model; SSE streaming | 5 |

## 4. Non-functional requirements

| ID | Requirement | Status |
|---|---|---|
| NFR-1 | No double-booking under ~50 concurrent attempts | Met |
| NFR-2 | ≥25 meaningful tests, ≥80% coverage | Met — 749 tests, 99% |
| NFR-3 | Schema built solely by Alembic; every revision round-tripped | Met |
| NFR-4 | No PHI in logs, events or AI records — ids only | Met for events (`test_outbox.py::test_every_queued_event_carries_ids_only`) and search logs (`test_search_routes.py::test_search_logs_result_ids_but_never_the_query`); other log lines by review; AI records ids, labels and counts only (`test_ai_interaction_model.py`) |
| NFR-5 | No clinical content in code, prompts, seeds or docs | Met |
| NFR-6 | Config from env vars; `.env` never committed | Met |
| NFR-7 | Workflows deterministic; Activities idempotent | Met |
| NFR-8 | Crash mid-workflow resumes without duplicate side effects | Demonstrated live (Weeks 2–3) |
| NFR-9 | One command to run; tests runnable from cold | Met |
| NFR-10 | The suite passes with no network access; tests embed with `FakeEmbeddings` and answer with `FakeLLM` unless they ask otherwise | Met — enforced by the `no_network`, `fake_embeddings_by_default` and `fake_llm_by_default` fixtures (`tests/network_guard.py`, `test_offline_guards.py`) |
| NFR-11 | A long AI call never blocks booking | Demonstrated live: `POST /appointments` took 101 ms with five streamed answers running, 137 ms with five reports generating, and 99 ms with neither (`docs/ai-layer.md` sections 13 and 14) |

## 5. Milestones

| Week | Deliverable | Status |
|---|---|---|
| 1 | Auth, core domain, schedules/slots, search, seed | Done |
| 2 | Temporal publishing + scheduling saga, slot concurrency, visits | Done |
| 3 | Celery, Kafka, analytics, observability | Done |
| 4 | Chunking, embeddings, retrieval | Done |
| 5 | Assistant, streaming, demo | In progress |

## 6. Traceability

| Requirement | Implementation | Test |
|---|---|---|
| FR-1 | `app/services/auth.py`, `app/core/security.py` | `test_auth_routes.py`, `test_auth_service.py` |
| FR-2 | `app/core/dependencies.py` (`ensure_patient_self_or_staff`) | `test_dependencies.py`, `test_appointment_routes.py::test_get_appointment_state_forbidden_for_a_different_patient` |
| FR-3 | `app/services/{department,service,provider}.py` | `test_department_routes.py`, `test_service_routes.py`, `test_provider_routes.py` |
| FR-4 | `app/services/provider_schedule.py` | `test_provider_schedule_service.py`, `test_provider_schedule_routes.py` |
| FR-5 | `app/services/service_search.py` | `test_service_search_service.py` |
| FR-6 | `app/temporal/workflows.py::PublishServiceWorkflow`, `activities.py::PublishActivities` | `test_publish_workflow.py`, `test_publish_activities.py` |
| FR-7 | `app/services/service_publish.py::ensure_can_publish`, `::ensure_can_unpublish`, `::_lock` (row lock, so two requests cannot both pass the check) | `test_service_publish_guard.py`, `test_service_publish_routes.py`, `test_service_publish_concurrency.py` |
| FR-8 | `app/services/slot.py::reserve_slot_uncommitted` | `test_slot_service.py::test_concurrent_reservations_exactly_one_wins` |
| FR-9 | `app/services/idempotency.py`, `appointments.idempotency_key` | `test_appointment_routes.py::test_create_appointment_repeated_idempotency_key_returns_original`, `::test_create_appointment_falls_back_to_the_database_when_redis_loses_the_key` |
| FR-10 | `app/services/billing.py::BillingChecker` | `test_billing_service.py` |
| FR-11 | `workflows.py::AppointmentSchedulingWorkflow`, `activities.py::SchedulingActivities` | `test_scheduling_workflow.py::test_scheduling_workflow_compensates_on_billing_failure`, `test_scheduling_activities.py::test_release_slot_compensates_correctly` |
| FR-12 | `app/models/appointment_status_history.py` | `test_schema_constraints.py`, `test_visit_routes.py::test_complete_finishes_the_visit_and_the_appointment` |
| FR-13 | `appointment_scheduling.py::cancel_appointment`, `waitlist.py::promote_next_waiting` | `test_appointment_cancel.py::test_cancel_promotes_the_oldest_waiting_entry` |
| FR-14 | `appointment_scheduling.py::reschedule_appointment` | `test_appointment_reschedule.py::test_a_failed_reschedule_leaves_the_original_slot_untouched` |
| FR-15 | `app/services/visit.py` | `test_visit_routes.py` (16 tests) |
| FR-16 | `app/celery/tasks/reminders.py`, `app/celery/base.py` (`DeadLetterTask`) | `test_celery_tasks.py::test_a_transient_failure_retries_with_backoff_then_dead_letters`, `::test_reminder_task_dead_letters_a_permanent_failure` |
| FR-17 | `app/events/` (envelope, outbox), `app/kafka/` (relay, producer, consumer, dedupe, handlers) | `test_event_replay.py`, `test_dedupe.py`, `test_consumer_loop.py`, `test_outbox_relay.py`, `test_event_handlers.py`, `test_event_envelope.py` |
| FR-18 | `app/services/analytics.py`, `app/api/v1/analytics.py`, `scripts/reconcile_analytics.py` | `test_analytics_service.py`, `test_analytics_routes.py`, `test_reconciliation.py` |
| FR-19a | `app/ai/chunking.py`, `activities.py::PublishActivities.structure_content` | `test_chunking.py`, `test_publish_activities.py::test_structure_content_lists_each_linked_providers_specialty_once`, `::test_structure_content_without_linked_providers_has_no_specialty`, `test_seed_script.py::test_publish_services_publishes_each_service_with_its_chunk` |
| FR-19b | `app/ai/embeddings.py` (`LocalEmbeddings` by default), `scripts/fetch_embedding_model.py`, `activities.py::embed_chunks`, `::mark_published`, `workflows.py::PublishServiceWorkflow` (embedding retry policy), `service_publish.py::get_publish_progress`, `app/ai/vector_store.py`, `app/models/chunk_embedding.py` | `test_embeddings.py`, `test_embeddings_local.py`, `test_embeddings_live.py` (skipped unless `RUN_LIVE_TESTS=1`), `test_vector_store.py`, `test_publish_activities.py::test_embed_chunks_stores_an_unpublished_vector_with_the_services_metadata`, `::test_embed_chunks_raises_a_transient_failure_retryable_without_its_chain`, `::test_mark_published_makes_the_services_vectors_searchable`, `test_publish_workflow.py::test_publish_workflow_gives_up_on_embedding_after_five_attempts`, `test_service_publish_routes.py::test_get_publish_status_shows_a_retrying_step_while_publishing`, `::test_get_publish_status_says_why_a_publish_failed` |
| FR-19c | `activities.py::chunk_content` (keeps unchanged chunks), `::embed_chunks` (skips chunks already embedded), `vector_store.py::PgVectorStore.store_service_vectors`, `service_publish.py::start_publish` (re-publish unpublishes the old vectors), `::unpublish_service` | `test_publish_activities.py::test_republishing_unchanged_text_embeds_nothing`, `::test_republishing_changed_text_embeds_it_and_leaves_one_vector`, `::test_embed_chunks_re_embeds_a_vector_made_by_another_model`, `test_vector_store.py::test_store_keeps_the_vector_of_a_current_chunk_it_was_not_given`, `test_service_reindex.py`, `test_service_publish_routes.py::test_publish_a_service_again_returns_202_and_takes_its_vectors_out_of_search`, `::test_unpublish_service_returns_200_inactive_and_takes_it_out_of_search` |
| FR-19d | `app/api/v1/search.py`, `app/ai/retrieval.py`, `vector_store.py::PgVectorStore.search` | `test_vector_store.py::test_search_never_returns_an_unpublished_vector`, `::test_search_drops_services_below_the_threshold`, `test_search_routes.py` (published filter, threshold message, client threshold ignored, validation, auth, 503); PHI: `test_search_routes.py::test_a_patients_details_cannot_be_found_through_search`, `::test_search_logs_result_ids_but_never_the_query`, `::test_a_failed_search_logs_the_reason_but_never_the_query` |
| FR-19e | `scripts/eval_retrieval.py` (`make eval`), `RETRIEVAL_MIN_SIMILARITY=0.31` | `test_eval_retrieval.py` (hit rate and sweep scoring; the set expects exactly the seeded catalogue), `test_eval_ranking.py::test_evaluate_ranks_every_published_service_whatever_its_score` |
| FR-20 | `app/ai/llm.py`, `app/ai/prompts.py`, `app/models/ai_interaction.py`, `app/services/ai_interaction.py`, `app/schemas/assistant.py`, `app/services/assistant.py`, `app/api/v1/assistant.py`, `app/core/sse.py` | `test_llm.py`, `test_llm_live.py` (skipped unless `RUN_LIVE_TESTS=1`), `test_prompts.py`, `test_ai_interaction_model.py`, `test_ai_interaction_service.py`, `test_schema_constraints.py::test_an_ai_interaction_outcome_must_be_one_the_enum_knows`, `test_assistant_schemas.py`, `test_assistant_refusal.py`, `test_assistant_routes.py` (answer and citations, refusal, not offered, 422s, roles, 503s, no transaction held during the model call, question never logged, open times, slots recorded once); PHI: `test_assistant_routes.py::test_another_patients_appointment_for_the_same_service_is_never_shown`, `::test_a_preparation_question_is_answered_from_the_callers_own_appointments`, `::test_a_navigation_question_never_loads_appointments`; streaming: `test_assistant_routes.py::test_the_stream_sends_the_answer_piece_by_piece_then_citations_then_done`, `::test_a_provider_failure_mid_answer_ends_the_stream_with_an_error_event`, `::test_a_patient_who_leaves_mid_answer_is_recorded_as_interrupted`, `::test_an_interrupted_answer_is_recorded_though_the_request_is_cancelled`, `test_sse.py` |
| FR-21 | `app/services/generation.py`, `app/api/v1/generation.py`, `app/schemas/generation.py`, `app/models/generated_content.py`, `app/ai/prompts.py` (`PROMPT_SUMMARY_V1`, `PROMPT_FOLLOWUP_V1`, `PROMPT_REPORT_V1`, `REPORT_REPAIR_V1`) | `test_generation_routes.py::test_the_report_figures_match_the_analytics_endpoint_and_count_each_departments_slots`, `::test_an_unusable_report_reply_is_retried_once_with_a_repair_instruction`, `::test_a_report_reply_with_a_digit_is_retried`, `::test_two_unusable_report_replies_fail_cleanly`, `::test_a_summary_is_saved_with_its_prompt_version_model_and_interaction`, `::test_a_staff_member_who_leaves_mid_generation_still_has_the_text_saved`, `::test_appointment_generation_is_for_front_desk_and_admin_only`; PHI: `::test_the_summary_prompt_holds_the_appointment_and_nothing_about_the_patient`, `test_prompts.py::test_appointment_facts_carry_nothing_that_identifies_the_patient`; `test_report_validation.py`, `test_schema_constraints.py::test_generated_content_must_match_its_kinds_scope` |

## 7. Known gaps

- **FR-16's analytics rollup was retired.** The Celery Beat rollup recomputed
  `analytics_daily` every five minutes and overwrote the Kafka consumer's
  increments, so the consumer is now its only writer. Celery keeps the
  reminders and gained the outbox relay. The requirement is met by a
  different mechanism than it names.
- Cancel/reschedule are proven; **the saga's compensation has no single end-to-end
  test** — it is covered as two halves (orchestration + Activity DB writes) plus a
  live demonstration.
- A booking whose workflow never starts (Temporal unreachable) stays `REQUESTED`
  with nothing to retry it.
- Overlapping *schedule* windows for one provider are not prevented; the invariant
  that matters is enforced on the slots they generate.
- **Booking accepts a slot in the past.** Nothing in the booking path compares a
  slot's `start_time` with now. Found during the Week 3 review's live check.
- **Check-in is accepted days before the appointment.** The wait is check-in
  minus the slot's start, so an early check-in records a negative wait and
  pulls the average down. Found on Week 5 Day 3, when the utilisation report's
  commentary called a negative average wait an anomaly.
- **A consumer down for longer than Kafka's 7-day retention skips events
  silently** -- its offset falls below the log start and `earliest` jumps the
  gap. Reconciliation reports the drift, the outbox still holds every event, and
  `--repair` restores the numbers. Diagnosis in `docs/runbook.md` section 4.
- **Search gives a near miss a weak match, not "we don't offer that".** "chest
  x-ray" finds Knee X-Ray at 0.428, above the 0.332 of a real service's
  weakest query, so no threshold refuses it. `docs/ai-layer.md` section 7.
- **A re-publish takes the service out of search and booking while it runs**,
  and a failed one keeps it out until a publish succeeds.
- **`/search` is PHI-safe by construction, not by a per-patient filter**: the
  index holds service text only and the query is never logged. Retrieval
  scoped to the caller's own data starts with the Week 5 assistant.
