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
section you need:

```
Grep "5.1 Tasks" .claude/reference/execution-guidelines.md      -A 20
Grep "Definition of Done" .claude/reference/execution-guidelines.md
Grep -i "idempoten|outbox" .claude/reference/part-a.md
```

Useful landmarks in `execution-guidelines.md`:

- `## <n>. Week <n> —` — each week's chapter
- `### <n>.1 Tasks` — the numbered task table with hour estimates
- `### <n>.2 Guidance` · `### <n>.3 Definition of Done` · `### <n>.4 Common mistakes`
- `Appendix A` — suggested project layout · `Appendix B` — suggested data model

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
