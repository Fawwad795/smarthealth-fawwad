---
name: assignment-brief
description: Look up what the assignment documents actually say - the week's task list and hour estimates, a Definition of Done, an exact MUST/SHOULD/STRETCH wording, or the grading weights. Use whenever a question needs the source of truth rather than the summary in CLAUDE.md, or when the mentor's briefs have been edited and need re-converting.
allowed-tools: Read, Grep, Bash
---

# The assignment briefs

`.claude/CLAUDE.md` is a **distilled summary**. When a question needs the exact
wording — what a week's task list actually is, what a Definition of Done requires,
whether something is MUST or SHOULD — the source of truth is here:

| File | Lines | Covers |
|---|---|---|
| `.claude/reference/part-a.md` | ~240 | Weeks 1–3: scenario, functional requirements by area, out of scope, tech stack, deliverables, grading |
| `.claude/reference/part-b.md` | ~160 | Weeks 4–5: the AI layer, safety rules, retrieval, assistant, streaming, grading |
| `.claude/reference/execution-guidelines.md` | ~840 | Per-week task tables with hour estimates, guidance, Definitions of Done, common mistakes, Appendix A (layout) and B (data model) |

## Grep, don't read whole

`execution-guidelines.md` is ~840 lines. Reading it end to end costs more context
than the summary it was distilled into, which defeats the point. Search for the
section you need. **The chapter number is not the week number** — the brief
front-loads three general chapters (what this assignment is, the 5-week
structure, ground rules) before Week 1, so every week's chapter is four ahead
of its week number:

| Week | Chapter | Tasks | Guidance | Definition of Done | Common mistakes |
|---|---|---|---|---|---|
| 1 | 5 | `### 5.1 Tasks` | `### 5.2 Guidance` | `### 5.3 Definition of Done` | `### 5.4 Common mistakes this week` |
| 2 | 6 | `### 6.1 Tasks` | `### 6.2 Guidance` | `### 6.3 Definition of Done` | `### 6.4 Common mistakes this week` |
| 3 | 7 | `### 7.1 Tasks` | `### 7.2 Guidance` | `### 7.3 Definition of Done` | `### 7.4 Common mistakes this week` |
| 4 | 8 | `### 8.1 Tasks` | `### 8.2 Guidance` | `### 8.3 Definition of Done` | `### 8.4 Common mistakes this week` |
| 5 | 9 | `### 9.1 Tasks` | `### 9.2 Guidance` | `### 9.3 Definition of Done` | `### 9.4 Common mistakes this week` |

```
Grep "### 7.1 Tasks" .claude/reference/execution-guidelines.md  -A 20   # e.g. Week 3
Grep "Definition of Done" .claude/reference/execution-guidelines.md
Grep -i "idempoten|outbox" .claude/reference/part-a.md
```

`Appendix A` — suggested project layout · `Appendix B` — suggested data model.
If this table ever stops matching (a mentor revision could re-order chapters),
re-derive it with `Grep "^## [0-9]" .claude/reference/execution-guidelines.md`
rather than trusting it blindly.

## These are living documents

Task rows get `(Done)` appended as work lands, and the mentor may revise them. The
converted markdown is generated, **never hand-edited**. After the `.docx` files
change, regenerate:

```bash
python scripts/convert_briefs.py
```

Runs on the host with stdlib only — the `.docx` originals live outside the repo at
`D:\eMumba\SmartHealth-20260817T073706Z-1-001\SmartHealth` and are not mounted into
any container. Override with `SMARTHEALTH_BRIEFS_DIR` if they move.

Never read the `.docx` files directly: the Read tool refuses binary files, and
hand-extracting the XML per session is exactly what this conversion exists to stop.

## When the brief and the summary disagree

The brief wins — say so plainly, and update `.claude/CLAUDE.md` (or the relevant
`.claude/rules/` file) so the next session doesn't rediscover the same gap.
