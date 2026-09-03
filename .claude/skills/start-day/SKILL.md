---
name: start-day
description: Pick up a new day's work with full context - read where yesterday left off, pull this week's task list from the assignment brief, load the rules for this week's theme, start the day's branch, and propose a plan. Use at the beginning of any working session ("Week 2 Day 1", "start day 3", "let's begin today").
allowed-tools: Bash, Read, Grep
---

# Starting a day

A fresh session loads `.claude/CLAUDE.md` and nothing else. Everything below is
what that summary deliberately leaves out. Do all of it **before** proposing a
plan, and do not write code until the user approves the plan.

Argument is the week and day, e.g. `Week 2 Day 1`. If it isn't given, work it out
from `NOTES.md` and `git log` and confirm with the user rather than guessing.

## 1. Where things left off

```
Read NOTES.md          # go to the END: the last "### Day N" entry and the
                       # weekly self-check. "Carrying into..." and "Open
                       # questions for my mentor" are the handoff.
git log --oneline -12
git branch --show-current
git status --short
```

`NOTES.md` is a repo file, not a memory file — it is never auto-loaded, and it is
the single best record of what was decided and why. Read the tail of it every time.

If the previous day's entry is missing or has no "Carrying into" section, say so —
the handoff is broken and the user should know before planning on top of it.

## 2. This week's actual task list

Do not plan from memory or from the CLAUDE.md summary. Get the real table. **The
brief's chapter number is not the week number** — it runs four ahead:

| Week | Chapter | Tasks | Definition of Done |
|---|---|---|---|
| 1 | 5 | `### 5.1 Tasks` | `### 5.3 Definition of Done` |
| 2 | 6 | `### 6.1 Tasks` | `### 6.3 Definition of Done` |
| 3 | 7 | `### 7.1 Tasks` | `### 7.3 Definition of Done` |
| 4 | 8 | `### 8.1 Tasks` | `### 8.3 Definition of Done` |
| 5 | 9 | `### 9.1 Tasks` | `### 9.3 Definition of Done` |

```
Grep "### 7.1 Tasks" .claude/reference/execution-guidelines.md -A 25          # e.g. Week 3
Grep "### 7.3 Definition of Done" .claude/reference/execution-guidelines.md -A 15
```

Task rows carry hour estimates and `(Done)` markers, which is how to tell what is
genuinely left. See the `assignment-brief` skill for the other landmarks.

## 3. Load the rules this week actually needs

**This step matters more than it looks.** `.claude/rules/*.md` are path-scoped and
load when Claude *reads* a matching file — so a week that starts by **creating**
`app/temporal/` files, which don't exist yet, may never trigger them.

Read the relevant file(s) explicitly:

| Week | Theme | Read |
|---|---|---|
| 2 | Temporal, scheduling, slots | `.claude/rules/workflows-and-sagas.md`, `.claude/rules/data-model.md` |
| 3 | Celery, Kafka, observability | `.claude/rules/events-observability.md` |
| 4 | Chunking, embeddings, retrieval | `.claude/rules/ai-layer.md`, `.claude/rules/data-model.md` |
| 5 | Assistant, streaming, demo | `.claude/rules/ai-layer.md` |

Add `.claude/rules/testing.md` on any day that writes tests — which is every day.

## 4. Start the day's branch

Use the `stacked-pr` skill: each day branches off the previous day's branch, and
its PR targets that branch, not `main`. Draft the commands for the user to run —
never run git writes.

## 5. Propose the plan, then stop

State, briefly:

- which numbered tasks today covers, and their hour estimates
- what carried over from yesterday
- anything in "Open questions for my mentor" that blocks today's work
- the order, and where the risk is

Then **ask for approval before writing any code**, and follow the five-step
protocol in `.claude/CLAUDE.md` §11 for each subtask unless the user waives it.

## Ending the day

The next `start-day` is only as good as this. Append a `### Day N — <date>` entry
to `NOTES.md` under the current week, matching the existing structure exactly:

- **Goal** — one or two sentences
- **Done** — what actually landed, with file references
- **Decisions and why** — a table: Decision | Why | Tradeoff
- **What confused me / cost time** — real dead ends, including bugs found in
  tests rather than in the code
- **Things I want to be able to explain out loud** — the mentor asks these
- **Carrying into Day N+1** — the handoff step 1 depends on
- **Open questions for my mentor**

Write it from what actually happened, not a summary of the plan. Then draft the
`docs:` commit for the user.
