"""Guards the PRD traceability table against silent rot.

docs/prd.md#6 maps each requirement to the test that proves it --
rules/docs.md calls this table "easy marks, don't skip it" because it's
graded. Nothing stops a cited test from being renamed or deleted later;
without this, that drift is invisible until a mentor opens the file. This
resolves every `test_x.py` and `test_x.py::test_y` reference in the table
against the actual suite, so a stale citation fails the build instead.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRD_PATH = REPO_ROOT / "docs" / "prd.md"
TESTS_DIR = REPO_ROOT / "tests"

REFERENCE_PATTERN = re.compile(r"(test_[a-z0-9_]+\.py)(?:::(test_[a-z0-9_]+))?")


def _traceability_section() -> str:
    """Return the text of docs/prd.md's '## 6. Traceability' section only.

    Scoped to that one section, not the whole file, so a test file name
    mentioned in unrelated prose elsewhere in the PRD can never be mistaken
    for a traceability citation.
    """
    text = PRD_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^## 6\. Traceability$(.*?)^## 7\.", text, re.MULTILINE | re.DOTALL
    )
    assert match, "docs/prd.md's '## 6. Traceability' section moved or was renamed"
    return match.group(1)


def test_every_traceability_reference_resolves() -> None:
    """Every test_x.py[::test_y] cited in the traceability table still exists.

    A renamed or deleted test would leave the PRD claiming coverage that no
    longer exists -- this fails loudly instead of letting the docs rot.
    """
    section = _traceability_section()
    references = REFERENCE_PATTERN.findall(section)
    assert references, "no test references found in the traceability table"

    test_files = {path.name: path for path in TESTS_DIR.glob("**/*.py")}
    missing = []
    for filename, function_name in references:
        test_file = test_files.get(filename)
        if test_file is None:
            missing.append(f"{filename}: file not found under tests/")
            continue
        if function_name and f"def {function_name}(" not in test_file.read_text(
            encoding="utf-8"
        ):
            missing.append(f"{filename}::{function_name}: function not found")

    assert not missing, (
        "docs/prd.md traceability table cites tests that no longer exist:\n"
        + "\n".join(missing)
    )
