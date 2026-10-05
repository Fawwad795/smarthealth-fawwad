"""SessionStart hook: inject the live handoff so a fresh session doesn't
have to guess where yesterday left off.

CLAUDE.md is the only file loaded automatically at session start; it says
nothing about the current branch, uncommitted changes, or what "Carrying
into Day N+1" recorded yesterday. Without this, Claude either asks or
assumes -- this hands over the same three things the start-day skill reads
by hand, so a session that skips start-day still isn't guessing blind.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NOTES_TAIL_LIMIT = 2500


def run_git(*args: str) -> str:
    """Run a read-only git command in ROOT, returning its stdout or a placeholder."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() or "(none)"
    except Exception as exc:  # never let a git hiccup block the session starting
        return f"(unavailable: {exc})"


def last_notes_entry() -> str:
    """Return the most recent '### Day N' section of NOTES.md, or a warning."""
    notes_path = ROOT / "NOTES.md"
    if not notes_path.exists():
        return "(NOTES.md not found)"
    text = notes_path.read_text(encoding="utf-8")
    days = list(re.finditer(r"^### Day .*$", text, re.MULTILINE))
    if not days:
        return "(no '### Day N' entries yet)"
    tail = text[days[-1].start() :].strip()
    if len(tail) > NOTES_TAIL_LIMIT:
        tail = tail[:NOTES_TAIL_LIMIT] + "\n...(truncated -- read NOTES.md for the rest)"
    return tail


def main() -> int:
    """Print the SessionStart hook payload with the handoff as additionalContext."""
    context = f"""<session-handoff source=".claude/hooks/session_context.py">
Branch: {run_git("branch", "--show-current")}
Uncommitted changes: {run_git("status", "--short")}

Recent commits:
{run_git("log", "--oneline", "-8")}

Last NOTES.md entry -- "Carrying into" and "Open questions" are today's starting point:
{last_notes_entry()}
</session-handoff>"""

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
