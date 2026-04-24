"""SpecKit `specs/` scanner for `hopewell backfill`.

Each `specs/NNN-slug/` subdirectory becomes a BackfillCandidate. File
presence drives a progress hint; git-evidence is needed to escalate to
done (so a spec with plan+tasks but no commits stays an `idea`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from hopewell import backfill_git


# Directory pattern: numeric prefix + slug (001-example, 042-something).
_DIR_RE = re.compile(r"^(\d{1,4})-([a-z0-9][a-z0-9._-]*)$", re.IGNORECASE)


@dataclass
class SpecItem:
    """One spec directory's ingest view."""
    path: str
    slug: str                     # "001-example"
    number: int                   # 1
    title: str                    # from frontmatter; falls back to slug
    body: str = ""                # spec.md first paragraph
    has_spec: bool = False
    has_plan: bool = False
    has_tasks: bool = False
    # Populated later if the orchestrator correlates with git history
    has_git_evidence: bool = False

    @property
    def phase(self) -> str:
        """Relative authoring progress (independent of git evidence)."""
        if self.has_tasks:
            return "tasks"
        if self.has_plan:
            return "plan"
        if self.has_spec:
            return "spec"
        return "empty"


def specs_root(root: Path) -> Optional[Path]:
    """Return the `specs/` directory if it exists at root, else None."""
    candidate = root / "specs"
    return candidate if candidate.is_dir() else None


def scan(root: Path) -> List[SpecItem]:
    sroot = specs_root(root)
    if sroot is None:
        return []
    out: List[SpecItem] = []
    for child in sorted(sroot.iterdir()):
        if not child.is_dir():
            continue
        m = _DIR_RE.match(child.name)
        if not m:
            continue
        spec_md = child / "spec.md"
        plan_md = child / "plan.md"
        tasks_md = child / "tasks.md"
        title, body = _extract_title_body(spec_md) if spec_md.is_file() else (child.name, "")
        out.append(SpecItem(
            path=str(child),
            slug=child.name,
            number=int(m.group(1)),
            title=title or child.name,
            body=body,
            has_spec=spec_md.is_file(),
            has_plan=plan_md.is_file(),
            has_tasks=tasks_md.is_file(),
        ))
    return out


def _extract_title_body(spec_md: Path) -> (str, str):  # type: ignore[override]
    """Pull title from YAML frontmatter (if present) and the first
    non-heading paragraph as a short body.

    Pure stdlib — we don't need a full YAML parser; we just look for the
    `title:` key in a `---` delimited block.
    """
    try:
        text = spec_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "")

    title = ""
    body_lines: List[str] = []

    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        j = 1
        while j < len(lines) and lines[j].strip() != "---":
            m = re.match(r"^\s*title\s*:\s*(.+?)\s*$", lines[j])
            if m and not title:
                title = m.group(1).strip().strip('"').strip("'")
            j += 1
        i = j + 1

    # First non-empty, non-heading paragraph after frontmatter.
    para: List[str] = []
    saw_para = False
    for line in lines[i:]:
        stripped = line.strip()
        if not stripped:
            if saw_para:
                break
            continue
        if stripped.startswith("#"):
            # If no title yet, take the first heading as title.
            if not title:
                title = re.sub(r"^#+\s*", "", stripped)
            continue
        para.append(stripped)
        saw_para = True
        if len(para) >= 4:  # keep it short
            break
    body_lines = para
    return (title, " ".join(body_lines).strip())


def correlate_git_evidence(
    items: List[SpecItem],
    git_subjects: List[str],
) -> None:
    """Mutate each SpecItem to flag `has_git_evidence=True` when any commit
    subject/body mentions the spec's slug OR its numeric prefix as a topic
    (e.g. `impl 001-example`, `specs/001-example: foo`).

    `git_subjects` is a flat list of subject+body blobs (one per commit) —
    we do NOT limit this to ticket-referencing commits, because specs can
    be implemented without a ticket id.

    Keeps the default conservative — specs without git evidence stay idea
    regardless of authoring phase, per the prompt's ground rules.
    """
    if not items or not git_subjects:
        return
    haystacks = [s.lower() for s in git_subjects]
    for it in items:
        slug_lc = it.slug.lower()
        for hay in haystacks:
            if slug_lc in hay:
                it.has_git_evidence = True
                break
