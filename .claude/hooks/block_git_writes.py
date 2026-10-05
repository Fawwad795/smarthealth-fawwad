"""PreToolUse hook: block Claude from directly running state-changing git
commands, or merging a PR, via the Bash or PowerShell tools.

CLAUDE.md already instructs Claude to draft these commands for the user to
run themselves. This hook makes that a hard guarantee instead of relying on
the instruction being followed every turn. Read-only git commands (status,
log, diff, branch, add of a specific file) are unaffected.

Matches past a leading `git -C <path>` / `git -c <key>=<value>` global
option, and across `;`, `&`, `|`, `then` and newline-separated commands --
the previous version, scoped to only commit/push/broad-add, was found to
miss all of these plus checkout, reset, restore and rebase (checkout is
named explicitly in CLAUDE.md #10.9; the others are the same class of
state-changing write).
"""

import json
import re
import sys

GIT_GLOBAL_OPTION = r"(?:-c\s+\S+\s+|-C\s+\S+\s+)*"

PATTERN = re.compile(
    r"(?:^|[;&|]|\bthen\b)\s*"
    r"(?:git\s+" + GIT_GLOBAL_OPTION + r"(?:commit\b|push\b|checkout\b|reset\b|"
    r"restore\b|rebase\b|"
    r"add\s+(?:-A\b|--all\b|\.\s*(?:$|[;&|])))"
    r"|gh\s+pr\s+merge\b)",
    re.MULTILINE,
)


def main() -> int:
    """Read the pending Bash/PowerShell command and deny it if it's a git write."""
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")

    if PATTERN.search(command):
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Blocked by .claude/hooks/block_git_writes.py: "
                            "state-changing git commands (commit, push, "
                            "checkout, reset, restore, rebase, broad add) "
                            "and gh pr merge must be drafted for the user "
                            "to run themselves, per this project's "
                            "CLAUDE.md."
                        ),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
