"""TODO.md / ROADMAP.md scanner for `taskflow backfill`.

Parses bullet-list work items from TODO-like markdown files. Each bullet
becomes a BackfillCandidate; checked items (`- [x]`) open as done,
unchecked (`- [ ]`) or plain (`- item`) open as idea.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


# Candidate filenames, scanned case-insensitively at project root.
TODO_FILENAMES = ("TODO.md", "ROADMAP.md", "BACKLOG.md", "FEATURE_BACKLOG.md")

# GFM-ish task list pattern + plain bullet
_TASK_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+?)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass
class TodoItem:
    source_path: str
    line_no: int
    title: str
    checked: bool
    section: str = ""              # most recent heading above the bullet


def discover(root: Path) -> List[Path]:
    """Return existing TODO-like files at project root (case-insensitive)."""
    found: List[Path] = []
    lowered = {name.lower() for name in TODO_FILENAMES}
    for child in root.iterdir() if root.is_dir() else []:
        if child.is_file() and child.name.lower() in lowered:
            found.append(child)
    return sorted(found)


def parse_file(path: Path) -> List[TodoItem]:
    """Parse one TODO-style file into bullet-level items.

    Handles both `- [ ] item` / `- [x] done` and bare `- item`. Tracks the
    nearest preceding heading so callers can report context.
    """
    items: List[TodoItem] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return items

    section = ""
    for i, line in enumerate(text.splitlines(), start=1):
        h = _HEADING_RE.match(line)
        if h:
            section = h.group(2).strip()
            continue
        m = _TASK_RE.match(line)
        if m:
            checked = m.group(1).lower() == "x"
            title = m.group(2).strip()
            if title:
                items.append(TodoItem(
                    source_path=str(path),
                    line_no=i, title=title,
                    checked=checked, section=section,
                ))
            continue
        b = _BULLET_RE.match(line)
        if b:
            title = b.group(1).strip()
            # Skip bullets that look like sub-prose or links-only entries? keep
            # everything — users put real work here.
            if title:
                items.append(TodoItem(
                    source_path=str(path),
                    line_no=i, title=title,
                    checked=False, section=section,
                ))

    return items


def scan(root: Path) -> List[TodoItem]:
    """Top-level entry: discover + parse every TODO-like file at root."""
    out: List[TodoItem] = []
    for f in discover(root):
        out.extend(parse_file(f))
    return out
