# PRD — SmartHealth

Healthcare **operations** backend for MediNova: appointments, provider schedules,
services, simulated billing pre-checks, and (Weeks 4–5) a retrieval-backed
assistant. API only — no UI. No clinical data anywhere.

---

## 1. Problems

| # | Problem | Approach |
|---|---|---|
| 1 | Scheduling is manual, and two patients can hold one slot | Atomic conditional slot UPDATE + a Temporal saga |
| 2 | Patients can't find the right service | Published-catalogue search (Week 1); semantic search over published services (Week 4); an assistant built on it, which refuses medical advice (Week 5) |
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
- A patient asks the assistant to diagnose them; it refuses, says where to go, and adds the disclaimer.
- A patient asks how to prepare for their appointment and is answered from their own booking, never another patient's.
- A patient picks one of a service's open slots and books it.
- Front desk streams a utilisation report whose figures match the analytics endpoint.

## 3. Functional requirements

| ID | Requirement | Week |
|---|---|---|
| FR-1 | Registration, login, four roles, JWT auth | 1 |
| FR-2 | Patients see only their own data; staff scoping enforced in the endpoint | 1 |
| FR-3 | Department / service / provider CRUD, admin-gated | 1 |
| FR-4 | Provider schedule templates generating discrete `Slot` rows, idempotently | 1 |
| FR-5 | Public search over published, offered services, and each one's open slots | 1, 5 |
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
| FR-22 | AI analytics: questions asked, answered against refused, by intent, booking conversion, average and p95 latency, tokens; Prometheus metrics for AI interactions, failures, latency and refusals | 5 |
| FR-23 | A per-user limit on AI requests per minute, across the AI routes, kept in Redis; a 429 with `Retry-After` over it | 5 |
| FR-24 | Repeated assistant questions answered from a Redis cache, with a documented key, TTL and invalidation; nothing about one patient ever served to another | 5 |

## 4. Non-functional requirements

| ID | Requirement | Status |
|---|---|---|
| NFR-1 | No double-booking under ~50 concurrent attempts | Met |
| NFR-2 | ≥25 meaningful tests, ≥80% coverage | Met — 813 tests, 98.66% |
| NFR-3 | Schema built solely by Alembic; every revision round-tripped | Met |
| NFR-4 | No PHI in logs, events or AI records — ids only | Met for events (`test_outbox.py::test_every_queued_event_carries_ids_only`), search logs (`test_search_routes.py::test_search_logs_result_ids_but_never_the_query`) and assistant logs on both routes (`test_assistant_routes.py::test_neither_route_logs_the_question_or_the_answer`); other log lines by review; AI records ids, labels and counts only (`test_ai_interaction_model.py`) |
| NFR-5 | No clinical content in code, prompts, seeds or docs | Met |
| NFR-6 | Config from env vars; `.env` never committed | Met |
| NFR-7 | Workflows deterministic; Activities idempotent | Met |
| NFR-8 | Crash mid-workflow resumes without duplicate side effects | Demonstrated live (Weeks 2–3) |
| NFR-9 | One command to run; tests runnable from cold | Met — on Week 5 Day 5 a fresh clone, on fresh volumes, followed only the README: build 403 s, migrations 12 s, seed 40 s, then search, the assistant, a booking to `CONFIRMED`, analytics, a streamed report and the suite (804 passed) all worked |
| NFR-10 | The suite passes with no network access; tests embed with `FakeEmbeddings` and answer with `FakeLLM` unless they ask otherwise | Met — enforced by the `no_network`, `fake_embeddings_by_default` and `fake_llm_by_default` fixtures (`tests/network_guard.py`, `test_offline_guards.py`) |
| NFR-11 | A long AI call never blocks booking | Demonstrated live: `POST /appointments` took 101 ms with five streamed answers running, 137 ms with five reports generating, and 99 ms with neither (`docs/ai-layer.md` sections 13 and 14). On Day 5's clean stack, 203 to 656 ms with eight AI streams running against 47 to 94 ms idle: slower, never blocked (section 7) |
| NFR-12 | AI latency is measured, and a stalled provider cannot hold a patient indefinitely | Met — answers p95 616 to 636 ms, cache hits 36 ms; a provider that stops answering ends in a 503 after 41 s (`docs/ai-layer.md` sections 9 and 18) |
| NFR-13 | AI cost is known per request and bounded | Met — tokens measured per request type; ten AI requests a minute per user; a cache hit costs nothing; Groq's free tier allows 8,000 tokens a minute and 1,000 requests a day (`docs/ai-layer.md` section 18) |
| NFR-14 | Groundedness: answers recommend only published services retrieval found, with citations; report figures come from the database | Met — `test_assistant_routes.py::test_the_answer_prompt_holds_only_the_services_found`, `::test_an_unpublished_service_is_never_shown_to_the_model_or_cited`, `test_generation_routes.py::test_the_report_figures_match_the_analytics_endpoint_and_count_each_departments_slots` |
| NFR-15 | Safety: a question labelled medical advice gets fixed refusal text; every assistant reply ends with the disclaimer | Met — `test_assistant_routes.py::test_a_diagnose_me_question_is_refused_and_routed_with_the_disclaimer`; the answer prompts keep the rule as a second guard (`test_prompts.py::test_every_answer_prompt_forbids_medical_advice_and_says_how_to_refuse`) |

## 5. Milestones

| Week | Deliverable | Status |
|---|---|---|
| 1 | Auth, core domain, schedules/slots, search, seed | Done |
| 2 | Temporal publishing + scheduling saga, slot concurrency, visits | Done |
| 3 | Celery, Kafka, analytics, observability | Done |
| 4 | Chunking, embeddings, retrieval | Done |
| 5 | Assistant, streaming, demo | Built; the final demo is next |

## 6. Traceability

| Requirement | Implementation | Test |
|---|---|---|
| FR-1 | `app/services/auth.py`, `app/core/security.py` | `test_auth_routes.py`, `test_auth_service.py` |
| FR-2 | `app/core/dependencies.py` (`ensure_patient_self_or_staff`) | `test_dependencies.py`, `test_appointment_routes.py::test_get_appointment_state_forbidden_for_a_different_patient` |
| FR-3 | `app/services/{department,service,provider}.py` | `test_department_routes.py`, `test_service_routes.py`, `test_provider_routes.py` |
| FR-4 | `app/services/provider_schedule.py` | `test_provider_schedule_service.py`, `test_provider_schedule_routes.py` |
| FR-5 | `app/services/service_search.py`, `app/services/open_slots.py`, `app/api/v1/services.py::list_open_slots` | `test_service_search_service.py`; open slots: `test_open_slot_routes.py` (only open future slots of providers who offer the service, earliest first; a service not published is a 404 like a missing one; provider filter; paging; a shared slot under each service; search's `has_available_slots` agrees with the list) |
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
| FR-20 | `app/ai/llm.py`, `app/ai/prompts.py`, `app/models/ai_interaction.py`, `app/services/ai_interaction.py`, `app/schemas/assistant.py`, `app/services/assistant.py`, `app/api/v1/assistant.py`, `app/core/sse.py` | `test_llm.py`, `test_llm_live.py` (skipped unless `RUN_LIVE_TESTS=1`), `test_prompts.py`, `test_ai_interaction_model.py`, `test_ai_interaction_service.py`, `test_schema_constraints.py::test_an_ai_interaction_outcome_must_be_one_the_enum_knows`, `test_assistant_schemas.py`, `test_assistant_refusal.py`, `test_assistant_routes.py` (answer and citations, refusal, not offered, 422s for bad questions and broken bodies, a one-word question answered, gibberish not offered, roles, 503s, no transaction held during the model call, open times, slots recorded once); rules every prompt version must keep: `test_prompts.py::test_every_answer_prompt_forbids_medical_advice_and_says_how_to_refuse`, `::test_every_staff_prompt_keeps_to_logistics`, `::test_every_tagged_block_is_named_in_the_rule_that_says_it_is_data`; PHI: `test_assistant_routes.py::test_another_patients_appointment_for_the_same_service_is_never_shown`, `::test_a_preparation_question_is_answered_from_the_callers_own_appointments`, `::test_a_navigation_question_never_loads_appointments`, `::test_a_question_that_tries_to_break_out_of_its_tag_sees_only_the_callers_data`, `::test_neither_route_logs_the_question_or_the_answer`; streaming: `test_assistant_routes.py::test_the_stream_sends_the_answer_piece_by_piece_then_citations_then_done`, `::test_a_provider_failure_mid_answer_ends_the_stream_with_an_error_event`, `::test_a_patient_who_leaves_mid_answer_is_recorded_as_interrupted`, `::test_an_interrupted_answer_is_recorded_though_the_request_is_cancelled`, `test_sse.py` |
| FR-21 | `app/services/generation.py`, `app/api/v1/generation.py`, `app/schemas/generation.py`, `app/models/generated_content.py`, `app/ai/prompts.py` (`PROMPT_SUMMARY_V1`, `PROMPT_FOLLOWUP_V1`, `PROMPT_REPORT_V1`, `REPORT_REPAIR_V1`) | `test_generation_routes.py::test_the_report_figures_match_the_analytics_endpoint_and_count_each_departments_slots`, `::test_an_unusable_report_reply_is_retried_once_with_a_repair_instruction`, `::test_a_report_reply_with_a_digit_is_retried`, `::test_two_unusable_report_replies_fail_cleanly`, `::test_a_summary_is_saved_with_its_prompt_version_model_and_interaction`, `::test_a_staff_member_who_leaves_mid_generation_still_has_the_text_saved`, `::test_appointment_generation_is_for_front_desk_and_admin_only`; PHI: `::test_the_summary_prompt_holds_the_appointment_and_nothing_about_the_patient`, `test_prompts.py::test_appointment_facts_carry_nothing_that_identifies_the_patient`; `test_report_validation.py`, `test_schema_constraints.py::test_generated_content_must_match_its_kinds_scope` |
| FR-22 | `app/core/metrics.py` (AI metrics), `app/services/ai_interaction.py::_count`, `app/services/ai_analytics.py`, `app/api/v1/analytics.py::get_ai_summary` | Metrics: `test_ai_interaction_service.py::test_an_answer_is_counted_with_its_latency_in_seconds_and_its_tokens`, `::test_a_refusal_is_counted_and_one_without_a_label_counts_as_none`, `test_assistant_routes.py::test_a_refused_question_is_counted_on_the_metrics_page`, `test_metrics.py::test_the_ai_series_are_on_the_page_at_zero_before_any_interaction`; analytics: `test_ai_analytics_routes.py::test_the_ai_analytics_count_outcomes_intents_latency_and_tokens`, `::test_the_p95_latency_interpolates_between_the_two_slowest`, `::test_a_range_with_no_interactions_has_zeros_and_no_rates`, `::test_booking_conversion_counts_patients_who_booked_within_a_day_of_asking`, `::test_a_booking_exactly_a_day_after_the_question_still_counts`, `test_analytics_routes.py` (roles) |
| FR-23 | `app/services/rate_limit.py`, `app/core/exceptions.py` (`headers`), `app/api/v1/assistant.py`, `app/api/v1/generation.py` | `test_ai_rate_limit.py::test_the_request_over_the_limit_is_429_with_retry_after_and_no_ai_work`, `::test_the_limit_is_shared_by_the_ask_and_stream_routes`, `::test_staff_generation_is_limited_too`, `::test_each_user_has_their_own_count`, `::test_a_new_minute_starts_a_new_count`, `::test_a_request_refused_for_its_role_is_not_counted`, `::test_a_rejected_question_still_counts`, `::test_when_redis_is_down_the_request_is_let_through_logged_and_counted` |
| FR-24 | `app/services/answer_cache.py`, `app/services/assistant.py` (`_cached`, `_remember`), `ai_interactions.cache_hit` | `test_answer_cache.py` (normalising, the key changes with each input, every prompt version listed, entries round-trip, a failed store logged and never raised), `test_answer_cache_routes.py::test_the_next_patient_asking_the_same_question_gets_the_cached_answer`, `::test_a_refusal_is_cached_and_still_counted_as_a_refusal`, `::test_a_reply_cut_short_is_not_cached`, `::test_editing_a_service_misses_the_cache`, `::test_an_unpublished_service_is_never_recommended_from_the_cache`, `::test_the_stream_sends_a_cached_answer_as_one_piece`, `::test_a_hit_does_not_extend_the_entrys_time_to_live`, `::test_when_redis_is_down_the_question_is_answered_and_nothing_is_stored`; PHI: `::test_a_preparation_answer_is_never_cached` |

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
- **Staff cannot accept or reject a generated draft** (a SHOULD). Every draft
  is saved to `generated_content` with its prompt version and model, so it can
  be reviewed, but it has no status.
- **Booking slows while AI requests run.** On a clean stack, `POST
  /appointments` took 47 to 94 ms idle and 203 to 656 ms with eight AI streams
  running; reaching `CONFIRMED` took 0.52 to 0.75 s and 0.67 to 1.56 s.
  Reports, which embed nothing, slowed it as much as questions did, so the
  cost is the API serving concurrent requests, not embedding alone. Day 3's
  2.6 to 4.0 s did not come back.
- **A fresh stack can start with one dead-lettered job.** Celery Beat starts
  with the other services and runs the outbox relay every 5 s, so on a fresh
  database its first run can come before `alembic upgrade head` has made the
  tables. That run lands in `failed_jobs`, and the analytics then show one
  failed job. It happened on one of three fresh starts on Week 5 Day 5. Running
  the migration before `docker compose up -d` avoids it (`docs/demo.md`
  section 0); a one-shot migration service the workers wait for would fix it.
- **While Redis is stopped, every AI request waits 3 to 4 s** for Docker's DNS
  to fail the name `redis`, which the 2 s Redis timeout does not cover. The
  request is still answered. `docs/ai-layer.md` section 16.
