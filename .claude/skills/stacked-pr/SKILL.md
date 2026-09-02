---
name: stacked-pr
description: Start a day's branch, open its PR against the previous day's branch, and cascade the retargets after a merge. Use whenever starting a new day's work, opening a daily PR, or after a PR in the stack merges. From Week 2 onward each day gets its own branch stacked on the previous day's.
allowed-tools: Bash, Read
---

# Stacked daily PRs

From Week 2 onward, **each day's work goes on its own branch, branched off the
previous day's**, and each day's PR targets that previous branch. Every PR then
shows only that day's diff instead of a growing pile.

```
main
 └── week-1-foundation                    (PR #1, still open)
      └── week-2-workflows-day-1          PR → week-1-foundation
           └── week-2-workflows-day-2     PR → week-2-workflows-day-1
                └── week-2-workflows-day-3  …and so on
```

Branch naming: `week-<n>-<theme>-day-<d>`, e.g. `week-2-workflows-day-1`.

**A parent does not have to be merged before you stack on it.** PR #1 is still
open while Week 2 proceeds — branch off `week-1-foundation` anyway and target its
PR there. The retargets happen later, when the parent actually merges (see the
cascade section). Never branch Week 2 off `main` while Week 1 is unmerged: `main`
does not contain Week 1's code, so the day's diff would be wrong.

## Never run git writes directly

Per `.claude/CLAUDE.md` rule 10.9 (and the `block_git_writes.py` hook), **draft
these commands in the chat for the user to run.** Read-only checks — `git status`,
`git log`, `git branch`, `gh pr list`, `gh pr view` — are fine to run directly, and
are how you confirm what actually happened.

## Starting a day

Day 1 of a week branches off the previous week's branch; every later day branches
off the day before.

```powershell
git checkout <parent-branch>
git pull
git checkout -b week-<n>-<theme>-day-<d>
```

Confirm afterwards with `git branch --show-current` and `git log --oneline -3`.

## Opening the day's PR

The `--base` is the parent branch, **not `main`** — that is the whole point.

Title is Title Case: `Week <N> Day <D>: <What Landed>`. Body is Markdown, one
`## Task <N.M> - <Short Title>` section per task the day covered (matching the
week's task numbering, not generic labels like "Summary"), ending in a `##
Verified` section. No `🤖 Generated with [Claude Code]` trailer in the body —
that convention applies to commit messages, not PR descriptions. Before
drafting a later day's PR, read the previous day's actual PR
(`gh pr view <number> --json body -q '.body'`) rather than relying on memory
of the shape, since which task sections appear depends on what that day did.

```powershell
git push -u origin week-<n>-<theme>-day-<d>
gh pr create --base <parent-branch> --head week-<n>-<theme>-day-<d> --title 'Week <N> Day <D>: <What Landed>' --body @'
## Task <N.M> - <Short Title>

<a few terse bullets on what landed, including any sub-decisions or fixes
worth naming -- this is a PR body, not NOTES.md or the Friday docs pass,
so keep it as short as those keep their own entries>

## Task <N.M2> - <Short Title>

...one section per task...

## Verified

- `<old count>` → `<new count>` tests (all passing)
- Lint clean, no hardcoded status codes
- <live checks actually run, one bullet each>
'@
```

PowerShell here-strings: the closing `'@` must be at column 0 on its own line.

## Merging a stacked PR — GitHub does the cascade for you

**`gh pr merge` does not work on these.** GitHub detects the chain as a stack and
refuses both the GraphQL path (`gh pr merge`) and the plain REST `/merge` endpoint
(403). Use the async endpoint:

```powershell
gh api --method PUT repos/<owner>/<repo>/pulls/<n>/merge-async -f merge_method=merge -f sha=<head-sha>
gh api repos/<owner>/<repo>/pulls/<n>/merge-async/<uuid>   # poll until status=merged
```

`sha` is a safety interlock — it refuses if the head moved. Use `merge_method=merge`,
never `squash` or `rebase`: those put *different* commits on `main` than the branches
above are built on, so every stacked PR's diff replays the merged work.

**What GitHub then does automatically**, server-side, in that one call:

- rebases *every* branch above the merged one onto the new `main`
- retargets the next PR's base (e.g. day-1 from `week-1-foundation` to `main`)

So the manual cascade below is usually unnecessary. Confirmed on the Week 1 merge:
all seven Week 2 branches were rewritten and PR #2 retargeted, with no content lost.

**Local branches go stale afterwards.** Every remote got a new hash, so reset each
one or a later push will force the old history back:

```powershell
git fetch origin
git checkout <branch>; git reset --hard origin/<branch>   # per branch
```

Verify the trees match first (`git diff --stat <branch> origin/<branch>` → empty),
and tell the mentor the churn is rebase noise, not new work.

## Cascading after a merge — the manual fallback

When a PR in the stack merges, every PR **below** it is still based on a branch
that may no longer exist. GitHub often retargets an open PR automatically when its
base is deleted on merge, but **do not assume it did** — check, then fix.

1. Check what each open PR is actually based on:

   ```powershell
   gh pr list --state open --json number,headRefName,baseRefName
   ```

2. Retarget any PR still pointing at the merged branch:

   ```powershell
   gh pr edit <number> --base main
   ```

3. Rebase that branch onto its new base so the diff stays honest:

   ```powershell
   git checkout week-<n>-<theme>-day-<d>
   git fetch origin
   git rebase origin/main
   git push --force-with-lease
   ```

   `--force-with-lease`, never plain `--force`: it refuses if someone else pushed
   in the meantime.

4. Repeat down the chain — day-2 onto day-1's new base, day-3 onto day-2's, and so
   on. Re-run the `gh pr list` check afterwards and confirm every base is what you
   expect.

## Checklist before opening any daily PR

- Full suite passes: `make test`
- The day's NOTES.md entry is written (see the `day-log` skill if present)
- Commits are small and meaningful, not one end-of-day dump
- The PR base is the parent branch, not `main`
