---
name: explain
description: Re-explain recent work in short, plain, beginner-level English. Invoked as "/explain 2" or "/explain 4", where the number is how many of the most recent Claude responses to cover. Use whenever the user wants to understand what just happened without the jargon.
allowed-tools: Read, Grep
disallowed-tools: Edit, Write, NotebookEdit, Bash
---

# Explain what just happened, simply

The argument is a number: how many of **your own most recent responses** to
explain. `/explain 2` covers the last two, `/explain 4` the last four. With
no number, explain the last one.

Count *your* responses, not the user's messages, and count backwards from the
most recent — not including this one.

## What to produce

For each response, in order (oldest first), a short block:

**What I did** — one or two plain sentences.
**Why it mattered** — one sentence. The problem it solved, not the mechanism.
**The one thing worth remembering** — a single line, only if there is one.

Then close with a single sentence tying them together, if they were part of
one larger piece of work.

## Rules

- **Short.** Aim for 3–5 lines per response. The whole answer should be
  readable in under a minute. If the original response was long, that is
  exactly why the summary must not be.
- **Plain English at a beginner level.** Assume someone who writes Python but
  has not met this tool or pattern before.
- **Every technical term gets a five-word gloss the first time**, inline:
  "an idempotent script (safe to run twice)". Do not use a term and move on.
- **No code blocks.** Name a file or function if it helps, but do not re-paste
  code. If the code itself needs explaining, describe what it does in words.
- **Do not re-do the work, re-verify, or run tests.** This skill only explains
  what already happened. Reading a file to remind yourself what was written is
  fine.
- **Do not restate what you already said.** If the original response was
  already plain and short, say so in a line and move on.
- **Be honest about failures.** If something did not work, was left unfinished,
  or was a mistake, say that in the same plain language — do not smooth it over
  into sounding successful.

## Analogies

One analogy is often worth a paragraph of precision — use it when a concept is
genuinely unfamiliar (what a saga is, why a workflow must be deterministic).
Say plainly that it is an analogy, and do not stretch it past the point where
it stops being true.

## Example shape

> **What I did:** Added a config file that makes the linter check every
> function has a docstring.
>
> **Why it mattered:** The linter was passing while 77 docstrings were
> missing — it was never told to look for them. Your mentor found them
> instead, which is a slow and expensive way to catch that.
>
> **Worth remembering:** A tool that reports "all checks passed" is only
> checking what you configured it to check.
