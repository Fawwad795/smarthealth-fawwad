<!-- Converted from part-b.md.docx by scripts/convert_briefs.py.
     Source of truth for this project; .claude/CLAUDE.md is the
     distilled working summary. Re-run the script after the
     briefs are edited. Do not hand-edit this file. -->

## SmartHealth — Intelligent Healthcare Operations & Patient Engagement Platform [Part B]

Duration: Weeks 4–5 of 5 Prerequisite: Part A Weeks 1–2 complete, and content_chunks is being populated by your service publishing workflow. If chunking isn't working yet, fix that first — Part B has nothing to search otherwise.

### 1. The Scenario

Part A gave SmartHealth a reliable operations backend. It did nothing for the second problem in the brief: patients can't find the right service, and staff answer the same questions all day.

Today a patient with a question ("which specialist do I need?", "what do I do before my scan?") has to call the front desk, and staff write every appointment summary and follow-up by hand. Meanwhile the platform holds well-structured service, provider and scheduling data that nobody is using intelligently.

Part B adds an AI layer on top of your existing data:

- Patients ask natural questions and get guidance grounded in the clinic's real services and availability — routed to the right specialty, with preparation steps.
- Staff generate appointment summaries, follow-up drafts, FAQ answers and operational reports.
- Long AI responses stream to the client instead of making them wait.

Two safety rules define this layer. (1) The assistant is a navigation and logistics helper, not a clinician — it must never diagnose, prescribe or give clinical advice. (2) Patient data is PHI — the assistant must never surface one patient's information to another. Getting these two right matters more than the number of endpoints.

### 2. What "Good" Looks Like

By the end of Week 5:

- A patient asks "which specialist should I see for knee pain?" and gets a real, available specialty/ service at the clinic (e.g. orthopaedics), with preparation guidance and a clear "this is not medical advice — consult a professional" note — not a diagnosis.
- A patient asks "what's wrong with me?" or "what medication should I take?" and the assistant refuses to diagnose or prescribe, and instead routes them to the right service (or urgent care). This refusal is the single most important behaviour in Part B.
- A patient asks "what prep do I need before my MRI?" and gets an answer taken from that service's real preparation instructions, cited — nothing invented.
- A patient asks about someone else's appointment and is refused — patient data never crosses patients.
- A request for a service the clinic doesn't offer returns "we don't offer that here" — not an invented service.
- A staff member generates an appointment summary, a follow-up draft or a department utilisation report, and it streams in progressively.
- An analytics endpoint shows AI usage: questions asked, answered, refused, booking conversion after an AI interaction, average latency, token usage.
- You can show a small evaluation table proving retrieval surfaces the right services.

### 3. Functional Requirements

Labels as in Part A: [MUST], [SHOULD], [STRETCH].

#### 3.1 Indexing Pipeline (Week 4)

- [MUST] Extend the Part A service-publishing workflow with an embedding Activity: after a service's content is chunked, generate a vector embedding per chunk and store it in a vector store.
- [MUST] Chunking must be sensible and documented: combine the service name, department, specialty and preparation instructions so each chunk carries enough context to be found. State your approach and why. Blind fixed-length character splitting loses marks.
- [MUST] Every stored vector carries metadata: service_id, department, specialty, and a published flag. Retrieval must filter on published + offered — recommending a service the clinic doesn't offer is both a relevance and a correctness failure.
- [MUST] Re-publishing a service (an edit, a withdrawn service) re-indexes it and removes stale vectors. No orphaned or duplicated vectors after an update. Demonstrate this.
- [MUST] Embedding runs inside the publish workflow (a Temporal Activity), is retried on provider failure, and its progress is visible via the workflow status.
- [MUST] Batch your embedding calls (e.g. 32–100 chunks per request) and explain why.
- [SHOULD] Skip re-embedding chunks whose text hash is unchanged.
- [STRETCH] Hybrid search (keyword/BM25 + vector) with a documented comparison against pure vector.

#### 3.2 Retrieval

- [MUST] POST /search — semantic search over the clinic's services/providers, returning top-k with similarity scores and their department/specialty.
- [MUST] Configurable k (default 5) and a minimum similarity threshold below which results are discarded. If nothing passes, that is a valid "we don't offer that" outcome — the basis of the refusal path in 3.3.
- [MUST] Results are filtered to published, offered services. Draft or withdrawn services never appear to a patient. Enforced in the query, not in the prompt.
- [MUST] PHI scoping. Any retrieval that can touch patient-specific data (a patient's own appointments) is scoped to the authenticated patient in the query. A patient's context never includes another patient's data. This is a security requirement; test it explicitly.
- [SHOULD] A short retrieval evaluation set — at least 10 query/expected-service pairs in a JSON/CSV file — plus a script reporting how often the expected service appears in the top-k. Put the table in your docs.
- [STRETCH] Filter by department/availability extracted from the query; reranking.

#### 3.3 Patient Assistant (Week 5)

- [MUST] POST /assistant/ask — retrieval-augmented answering across these intents:
- Service navigation — "which specialist for knee pain?": recommend a real, offered specialty/ service, not a diagnosis.
- Preparation & steps — "what prep before my MRI?", "explain my appointment steps": answer from the real service's preparation instructions and the appointment's real data.
- Availability & booking guidance — "show available appointments this week": from real slots.
- [MUST] No medical advice — the top-weighted behaviour. The system prompt must forbid diagnosis, prescription and clinical advice; on any such request the assistant refuses and routes the patient to the appropriate service (or urgent care), always with a "not medical advice — consult a professional" disclaimer. Verify with a deliberate "diagnose me" request and include the transcript.
- [MUST] Grounding is mandatory. Recommend only services the clinic actually offers; if nothing fits, say so. Preparation answers come from the real service data, cited — never invented.
- [MUST] PHI access control. A patient may only ask about their own appointments. Enforced in the endpoint, never in the prompt. Never surface another patient's data.
- [MUST] Handle bad input gracefully: empty question, one word, gibberish, extremely long question (enforce a max length), and an in-scope-but-unanswerable request.
- [MUST] Never let retrieved content override your instructions (basic prompt-injection awareness: clearly delimit context and question, treat retrieved content as data, not instructions).
- [MUST] Store every interaction: question, retrieved ids, answer, model, token counts, latency, and whether it was refused — without logging PHI bodies. This table powers 3.6.
- [SHOULD] Multi-turn conversations: a conversation_id with the last N turns as history.
- [STRETCH] Orchestrate the flow with LangGraph as an explicit graph (route → safety-check → retrieve → generate → check grounding) and write up what the graph buys you.

#### 3.4 Communication & Report Generation (staff)

- [MUST] Endpoints for front_desk / admin, scoped appropriately:
- POST /appointments/{id}/generate/summary (a patient-facing appointment summary)
- POST /appointments/{id}/generate/followup (a follow-up communication draft)
- POST /reports/generate/utilisation (a department utilisation report over a date range)
- [MUST] Reports/summaries are grounded in real appointment and analytics data — no invented numbers. A utilisation report's figures must match the analytics tables.
- [MUST] Structured outputs where applicable (e.g. a utilisation report as a validated Pydantic model). If the model returns malformed JSON, retry once with a repair instruction, then fail cleanly.
- [MUST] Generated content is saved (a generated_content table) with the prompt version and model used — so results are reproducible and reviewable.
- [SHOULD] Staff can accept/reject a generated draft before it's used.
- [STRETCH] Patient-engagement campaign copy from a cohort definition.

#### 3.5 Streaming & Non-Blocking Behaviour

- [MUST] Streaming responses via Server-Sent Events (FastAPI StreamingResponse with text/event-stream) for the assistant and generation endpoints. Tokens must appear progressively — demo it with curl -N.
- [MUST] Send citations/metadata (which services/appointments were used) as a final event after the answer text, and a terminal done event.
- [MUST] Handle client disconnect mid-stream without leaving the server hanging, and still persist what was generated.
- [MUST] No blocking calls on the event loop: use async clients, or run sync SDK calls in a thread. One slow LLM call must not stall appointment booking — prove it by hitting the API concurrently.
- [MUST] Timeouts and retries on the LLM provider, with a clear error response when it's down. The AI layer failing must never block a patient from booking.
- [SHOULD] Per-user rate limiting on AI endpoints (Redis counter).
- [STRETCH] Long report generation offloaded to Celery with a job id + polling endpoint.

#### 3.6 AI Analytics & Observability

- [MUST] Extend your analytics with: questions asked, answered vs. refused, breakdown by intent (navigation / preparation / availability / staff_generation), booking conversion after an AI interaction, average + p95 latency, and total tokens used.
- [MUST] Every AI interaction is logged with the correlation ID from Part A, plus retrieved ids and token counts — never PHI or clinical text. When an answer is wrong, you must be able to reconstruct what context produced it from ids alone.
- [MUST] Prometheus counters/histograms for AI calls, failures and latency, plus a refusal counter (diagnosis requests refused).
- [SHOULD] A cost estimate per interaction based on token counts.
- [STRETCH] An answer-quality flag (thumbs up/down) stored for later analysis.

### 4. Explicitly Out of Scope

- Fine-tuning or training any model.
- Self-hosting large models on your laptop (use a hosted API; see section 5).
- Agents with tool use, web browsing, or multi-agent systems.
- Any clinical reasoning — diagnosis, triage severity scoring, treatment/medication suggestions. The assistant navigates services and logistics only.
- Voice, image or medical-image understanding.
- A UI for the assistant (curl / Swagger / Postman is fine).
- Building your own vector database or ANN index.
- Sophisticated eval frameworks — the 10-query set in 3.2 is enough.

### 5. Tech Stack & Practical Setup

Required

- LangChain / LangGraph (or a direct provider SDK) for LLM + embedding calls. Keep it thin.
- LLM provider: OpenAI / Groq / Anthropic. Your mentor will provide a key or a budget.
- Vector store: pgvector in your existing PostgreSQL is recommended (one less service, and metadata filtering — published/offered, department — is trivial with SQL). Qdrant or Chroma via Docker are acceptable alternatives. Put it behind a small interface so it can be swapped.

Optional / stretch: LangGraph, hybrid search, reranking models.

Practical rules — read these before you write code

- Safety first. The assistant must never diagnose or prescribe. Bake the refusal into the system prompt and test it. A "helpful" assistant that offers clinical advice is a failing assistant.
- PHI discipline. Patient data is protected. Never log it, never return it to anyone but its owner, and scope every retrieval by the authenticated patient. Use synthetic patient data throughout.
- Budget discipline. Log token usage from day one. Batch embeddings; never re-embed the whole catalog twice by accident.
- Keys in the environment only. A committed API key is an automatic fail and must be rotated. Add .env to .gitignore on day one.
- Determinism for tests. Temperature 0 for anything you assert on; a FakeLLM / FakeEmbeddings behind your interface so tests never call a real API. This is a requirement, not a nicety.

### 6. Deliverables

Added to your existing repo and docs:

- Working endpoints for search, the patient assistant (navigation + preparation + availability), and the staff generation types — all streaming.
- docs/ai-layer.md covering:
- the indexing pipeline (what a chunk is, what metadata it carries, why),
- the retrieval strategy (k, threshold, published/offered filter, PHI scoping),
- your prompts, verbatim, with version numbers — including the safety/refusal prompt,
- the retrieval evaluation table,
- transcripts of: a grounded service recommendation, a refused "diagnose me" request, a real preparation answer, and a malformed-input case,
- failure modes and how you handled them,
- cost/latency observations.
- Updated architecture diagram showing the AI layer and where it plugs into Part A.
- PRD update — use cases, non-functional requirements (latency, cost, groundedness, safety, PHI), and traceability from requirement → implementation → test.
- Tests — including retrieval filtering by published/offered, PHI scoping (a patient can't reach another's data), the medical-advice refusal, malformed input handling, structured report validation, and streaming response shape (with a fake LLM).
- Final 20-minute demo covering Part A + Part B end-to-end.

### 7. How You Will Be Assessed

| Area | Weight | What we look for |
|---|---|---|
| Safety & groundedness | 30% | No diagnosis/prescription ever; recommendations are real and offered; PHI never crosses patients; citations are real |
| Retrieval quality | 20% | Sensible chunking, published/offered filtering, threshold — and evidence it works |
| Engineering quality | 20% | Streaming done properly, non-blocking, retries, tests with a fake provider, no leaked keys |
| Documentation | 15% | Prompts (incl. the safety prompt), decisions, eval results, failure modes |
| Understanding | 15% | You can explain embeddings, similarity, RAG, and where your system would break |

Warning: the two ways to fail Part B are (1) an assistant that answers a clinical question instead of refusing, and (2) a retrieval that returns another patient's data. A cautious assistant that stays in its lane — navigation and logistics, grounded, patient-scoped — scores far higher than a fluent one that oversteps.
