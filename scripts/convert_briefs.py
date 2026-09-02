"""Convert the three assignment .docx briefs into readable markdown.

    python scripts/convert_briefs.py

Runs on the HOST, not inside a container: the .docx files live outside the
repo and are not mounted into any service. Needs no third-party packages --
a .docx is a zip of XML, and the standard library can read both.

Why this exists at all: Claude Code's Read tool refuses binary files, so
without a converted copy every session that needs to check the source of
truth has to re-extract the XML by hand. The briefs are also living
documents (task rows get "(Done)" appended as work lands), so this is
re-run rather than done once.

Override the source directory with SMARTHEALTH_BRIEFS_DIR if the originals
move.
"""

import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# The WordprocessingML namespace every element below is qualified with.
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

DEFAULT_SOURCE_DIR = r"D:\eMumba\SmartHealth-20260817T073706Z-1-001\SmartHealth"
DEST_DIR = Path(__file__).resolve().parents[1] / ".claude" / "reference"

BRIEFS = {
    "part-a.md.docx": "part-a.md",
    "part-b.md.docx": "part-b.md",
    "execution-guidelines.md.docx": "execution-guidelines.md",
}


def _paragraph_text(p: ET.Element) -> str:
    """Every scrap of text in one paragraph.

    Word splits a sentence into multiple runs whenever formatting changes
    mid-way, so "the **slot** is free" is three runs -- joining them is what
    reassembles the sentence.
    """
    parts = []
    for node in p.iter():
        if node.tag == f"{W}t":
            parts.append(node.text or "")
        elif node.tag == f"{W}tab":
            parts.append("\t")
        elif node.tag == f"{W}br":
            parts.append("\n")
    return "".join(parts).strip()


def _paragraph_style(p: ET.Element) -> str:
    """The named Word style ("Heading2", "Title", ...), or "" if unstyled."""
    pr = p.find(f"{W}pPr")
    if pr is None:
        return ""
    style = pr.find(f"{W}pStyle")
    return style.get(f"{W}val", "") if style is not None else ""


def _is_list_item(p: ET.Element) -> bool:
    """Bullets and numbers live in numbering properties, not in the text."""
    pr = p.find(f"{W}pPr")
    return pr is not None and pr.find(f"{W}numPr") is not None


def _render_paragraph(p: ET.Element) -> str:
    text = _paragraph_text(p)
    if not text:
        return ""

    style = _paragraph_style(p)
    heading = re.match(r"Heading(\d)", style, re.IGNORECASE)
    if heading:
        # Word's Heading1 becomes "##": the document title takes "#", so
        # every file has exactly one top-level heading.
        level = min(int(heading.group(1)) + 1, 6)
        return "#" * level + " " + text
    if style.lower() == "title":
        return "# " + text
    if _is_list_item(p):
        return "- " + text
    return text


def _render_table(tbl: ET.Element) -> str:
    """A Word table as a markdown table, first row treated as the header."""
    rows = []
    for tr in tbl.findall(f"{W}tr"):
        cells = []
        for tc in tr.findall(f"{W}tc"):
            # A cell holds paragraphs, not raw text. Escape any pipe so it
            # cannot break out of the markdown column it belongs to.
            cell = " ".join(
                t for t in (_paragraph_text(p) for p in tc.findall(f"{W}p")) if t
            )
            cells.append(cell.replace("|", "\\|") or " ")
        rows.append(cells)

    if not rows:
        return ""

    # Ragged rows (merged cells) would produce a malformed table, so pad
    # every row out to the widest one.
    width = max(len(r) for r in rows)
    rows = [r + [" "] * (width - len(r)) for r in rows]

    out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join(["---"] * width) + "|"]
    out.extend("| " + " | ".join(r) + " |" for r in rows[1:])
    return "\n".join(out)


def convert(path: Path) -> str:
    """Read one .docx and return its markdown.

    Iterates the body's direct children rather than findall()-ing
    paragraphs and tables separately, so a table stays where it appears in
    the document instead of being collected at the end.
    """
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    body = ET.fromstring(xml).find(f"{W}body")

    blocks = []
    for child in body:
        if child.tag == f"{W}p":
            blocks.append(_render_paragraph(child))
        elif child.tag == f"{W}tbl":
            blocks.append(_render_table(child))

    markdown = "\n\n".join(b for b in blocks if b)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    # Consecutive bullets read as separate lists with a blank line between
    # them; pull them back together.
    markdown = re.sub(r"(?m)^- (.*)\n\n(?=- )", r"- \1\n", markdown)
    return markdown.strip() + "\n"


def main() -> int:
    """Convert every brief found in the source directory.

    Returns a shell exit code: 1 if the source directory is missing, 0
    otherwise. A brief that is individually absent is skipped with a
    warning rather than failing the whole run.
    """
    source_dir = Path(os.environ.get("SMARTHEALTH_BRIEFS_DIR", DEFAULT_SOURCE_DIR))
    if not source_dir.is_dir():
        print(
            f"Source directory not found: {source_dir}\n"
            "Set SMARTHEALTH_BRIEFS_DIR to where the .docx briefs live.",
            file=sys.stderr,
        )
        return 1

    DEST_DIR.mkdir(parents=True, exist_ok=True)

    for docx_name, md_name in BRIEFS.items():
        source = source_dir / docx_name
        if not source.is_file():
            print(f"  skipped (missing): {docx_name}", file=sys.stderr)
            continue

        header = (
            f"<!-- Converted from {docx_name} by scripts/convert_briefs.py.\n"
            f"     Source of truth for this project; .claude/CLAUDE.md is the\n"
            f"     distilled working summary. Re-run the script after the\n"
            f"     briefs are edited. Do not hand-edit this file. -->\n\n"
        )
        destination = DEST_DIR / md_name
        destination.write_text(header + convert(source), encoding="utf-8")
        print(f"  {docx_name} -> {destination.relative_to(DEST_DIR.parents[1])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
