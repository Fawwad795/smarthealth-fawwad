---
paths:
  - "docs/**"
  - "README.md"
  - "NOTES.md"
---

# Documentation

**Documentation is 15% of the grade**, and "docs written on the last afternoon are
visible and penalised". Update `README.md`, `docs/design.md` and the PRD
traceability table **at the end of every week**, not at the end of the project.

The bar: *someone else could pick this up, and every decision is justified.* A
decision recorded without its tradeoff is half-recorded.

## What each file must contain

| File | Must contain |
|---|---|
| `README.md` | What the project is, architecture diagram, one-command run, how to run tests, API overview, env vars |
| `docs/design.md` | Data model / ERD, module breakdown, the publish workflow and scheduling saga, **the slot concurrency approach**, decisions with tradeoffs |
| `docs/events.md` | Every event: schema, producer, consumer, and how idempotency is guaranteed |
| `docs/runbook.md` | Diagnosing the three most likely failures — "a booking saga is stuck" (including the Temporal UI), "reminders aren't going out" — with **exact queries and commands** |
| `docs/ai-layer.md` | Chunking approach + why, retrieval strategy (k, threshold, published filter, PHI scoping), **prompts verbatim with version numbers including the safety prompt**, the retrieval eval table, transcripts, failure modes, cost/latency |
| `docs/prd.md` | 2–3 pages: use cases, functional + non-functional requirements, milestones, and a **traceability table mapping each requirement → implementation → test** |
| `NOTES.md` | Working log: what was tried, what confused, decisions and why. Plus the weekly tracking tables and Friday self-check |

## The traceability table is easy marks — don't skip it

One row per requirement: the requirement, the file that implements it, the test that
proves it. Add rows as work lands, not in Week 5.

## docs/ai-layer.md specifics (Weeks 4–5)

- **Prompts verbatim, with version identifiers** (`PROMPT_NAV_V1`,
  `PROMPT_SAFETY_V1`) — copied from the code, matching it exactly.
- **The refused "diagnose me" transcript is the single top-weighted artefact** in
  Part B. Save it in full.
- Also save: a grounded recommendation, a real preparation answer, and a
  malformed-input response.
- The retrieval eval table, before and after whatever tuning happened.
- An honest failure-modes section. **An honest list of known limitations raises the
  score**; a claim the code doesn't support lowers it.

## Never in the docs

**No PHI, no real patient data, no clinical content.** Transcripts and examples use
synthetic data only — the same rule as seeds and tests. A saved assistant transcript
must not contain a real person's details even in the question.

## NOTES.md structure

Append `### Day N — <date>` under the current week, matching the existing entries:
Goal · Done · Decisions (a table: Decision | Why — two columns, not three) · Cost
time · Explain out loud · Carrying into Day N+1 · Open questions.

**These are notes, not documentation.** One line per bullet, no sub-explanations,
no restating what the code comment already says. Goal is 1–2 sentences. Done is
~5–8 bullets naming the thing, not narrating it. The Decisions table is short
phrases, not paragraphs — the *why* in a few words, not the full reasoning (that
belongs in code comments or the Friday docs pass, not here). If a day's entry runs
noticeably longer than a typical prior day's, it has drifted from notes into
documentation — cut it back before appending.

Write it from what actually happened — including bugs found in tests rather than in
the code, and time lost to environment problems. That record is what the Friday
self-check and the mentor review are built from.

## Diagrams

Mermaid in fenced ```mermaid blocks, not ASCII art — GitHub renders it, and it stays
editable. The architecture diagram is updated when the architecture changes, which
includes adding the AI layer in Week 4.
