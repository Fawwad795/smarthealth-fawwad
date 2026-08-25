---
paths:
  - "app/ai/**"
  - "scripts/eval_retrieval.py"
---

# Part B — the AI layer (Weeks 4–5)

**Prerequisite:** `content_chunks` must actually be populated by the publish
workflow. If chunking isn't working, fix Week 2 first — Part B has nothing to search
otherwise.

## The two safety rules that define this layer

1. The assistant is a **navigation and logistics helper, not a clinician** — never
   diagnose, prescribe, suggest a cause of symptoms, or give clinical advice.
2. **Patient data is PHI** — never surface one patient's information to another.

The two ways to fail Part B: an assistant that answers a clinical question instead of
refusing, and retrieval that returns another patient's data. A cautious assistant that
stays in its lane scores far higher than a fluent one that oversteps.

## Indexing

- Embedding is an **Activity inside the Part A publish workflow**, retried on provider
  failure, progress visible via workflow status.
- **Chunking is deliberate and documented.** A service is small → the natural unit is
  one enriched chunk per service:
  `"{department} · {specialty}: {service name}. {description}. Preparation: {prep}"`.
  Blind fixed-length character splitting loses marks.
- Every vector carries metadata: `service_id`, `department`, `specialty`, `published`.
- **Batch 32–100 chunks per embedding call** and explain why. [SHOULD] skip
  re-embedding chunks whose `text_hash` is unchanged.
- Re-publishing **re-indexes and removes stale vectors** — no orphans, no duplicates.
  Never re-index by inserting again. Demonstrate it.

## Retrieval — `POST /search`

Top-k (configurable, default 5) with similarity scores + department/specialty. A
minimum **similarity threshold** discards weak results; "nothing above threshold" is a
valid *"we don't offer that"* outcome and is the basis of the refusal path. Pick the
threshold empirically from the eval set and document it.

Filtering is **correctness and safety**, enforced **in the SQL query, never in the
prompt**: `WHERE published` (never recommend an unoffered service) and patient-scoping
on any patient-specific retrieval (never leak PHI). Both are graded; test both.

Week 1's `search_services` in `app/services/service_search.py` already establishes the
published-only + offered-only pattern in SQL — reuse that shape.

[SHOULD] An eval set of ≥10 query → expected-service pairs plus a script reporting
top-k hit rate; before/after table in the docs.

## Patient assistant — `POST /assistant/ask`

Intents: **service navigation**, **preparation & steps**, **availability & booking
guidance** — all grounded in real rows.

Flow: safety-check → embed query → retrieve top-k offered services above threshold
(plus only the caller's own appointment data if relevant) → if nothing survives, say
"we don't offer that" → build a prompt containing only that context → **stream** →
append citations → persist the interaction (ids only).

- **Refusal is the top-weighted behaviour.** Diagnosis/prescription/treatment/
  symptom-cause requests are refused, the patient routed to the right service (or
  urgent care if acute), always with *"This is not medical advice — please consult a
  professional."* Save the "diagnose me" transcript in the docs — it is the
  top-weighted artefact.
- **PHI access control enforced in the endpoint, never in the prompt.**
- Handle bad input without a 500: empty, one word, gibberish, over-max-length,
  in-scope-but-unanswerable.
- **Prompt injection:** clearly delimit context and question; treat retrieved content
  as data, never as instructions.
- Version prompts in code and commit them: `PROMPT_NAV_V1`, `PROMPT_SAFETY_V1`.
- Persist every interaction to `ai_interactions`: question, retrieved ids, answer,
  model, token counts, latency, refused flag — **no PHI bodies**.

## Staff generation

`POST /appointments/{id}/generate/summary` · `.../generate/followup` ·
`POST /reports/generate/utilisation` — for front_desk/admin, scoped appropriately.
Grounded in real appointment/analytics data — **never invent numbers**; a utilisation
report's figures must match the analytics tables. Structured output as a validated
Pydantic model; on malformed JSON, **retry once with a repair instruction, then fail
cleanly**. Save to `generated_content` with prompt version + model.

## Streaming & non-blocking

SSE via `StreamingResponse` with `text/event-stream`: token events → a `citations`
event → a terminal `done` event. Test with `curl -N`. **Stream from the provider** —
generating the whole answer then yielding it in pieces is fake streaming and is called
out as a mistake. Handle client disconnect without hanging the server, still
persisting what was generated. **Never block the event loop**: async clients, or
`await asyncio.to_thread(...)` for a sync SDK. Timeouts + retries on the provider —
**the AI layer failing must never block a patient from booking.**

## AI analytics

Questions asked; answered vs. refused; breakdown by intent; booking conversion after
an AI interaction; average + p95 latency; total tokens. Prometheus counters/histograms
for AI calls, failures, latency, plus a **refusal counter**. Every AI interaction
logged with the Part A correlation ID plus retrieved ids and token counts — never PHI
or clinical text. When an answer is wrong, it must be reconstructable from ids alone.

## Determinism for tests (a requirement, not a nicety)

`EmbeddingProvider` and `LLMProvider` interfaces with real implementations **and**
`FakeEmbeddings` / `FakeLLM`. Temperature 0 for anything asserted on. **The full suite
must pass with no network access.** Real-API tests are integration tests, marked and
skippable. API keys live in the environment only — a committed key is an automatic
fail and must be rotated.
