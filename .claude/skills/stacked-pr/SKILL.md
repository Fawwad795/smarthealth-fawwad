---
name: stacked-pr
description: Start a day's branch, open its PR against the previous day's branch, and cascade the retargets after a merge. Use whenever starting a new day's work, opening a daily PR, or after a PR in the stack merges. From Week 2 onward each day gets its own branch stacked on the previous day's.
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

```powershell
git push -u origin week-<n>-<theme>-day-<d>
gh pr create --base <parent-branch> --head week-<n>-<theme>-day-<d> --title 'week <n> day <d>: <what landed>' --body @'
What this day covers, and why.

Verified: <tests run, live checks done>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
'@
```

PowerShell here-strings: the closing `'@` must be at column 0 on its own line.

## Cascading after a merge — the part that goes wrong

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
