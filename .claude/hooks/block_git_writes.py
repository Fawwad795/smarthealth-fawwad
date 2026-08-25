"""PreToolUse hook: block Claude from directly running git commit, git push,
or broad git add (-A / . / --all) via the Bash tool.

CLAUDE.md already instructs Claude to draft these commands for the user to
run themselves. This hook makes that a hard guarantee instead of relying on
the instruction being followed every turn. Read-only git commands (status,
log, diff, branch, add of a specific file) are unaffected.
"""

import json
import re
import sys

PATTERN = re.compile(
    r"(?:^|[;&|]|\bthen\b)\s*git\s+"
    r"(?:commit\b|push\b|add\s+(?:-A\b|--all\b|\.\s*(?:$|[;&|])))"
)


def main() -> int:
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
                            "git commit / git push / broad git add (-A, ., "
                            "--all) must be drafted for the user to run "
                            "themselves, per this project's CLAUDE.md."
                        ),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
