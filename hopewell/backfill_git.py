"""git-log spider for `hopewell backfill`.

Scans `git log` for ticket-like references (HW-NNNN explicit ids, plus
`closes|fixes|implements #N` GitHub-issue style). Each unique reference
becomes a BackfillCandidate the orchestrator can dedupe + materialise.

Stdlib only; shells out to `git` directly.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


# HW-NNNN style explicit references (prefix may be any capitalised id prefix,
# but we default to the project's id_prefix at call time).
_EXPLICIT_RE_TMPL = r"\b{prefix}-(\d{{1,6}})\b"

# GitHub-issue-style closing keywords. Captured repeat: `closes #42`,
# `fix #3`, `implements #17`. Matches are case-insensitive.
_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|implement[sd]?|resolve[sd]?)\s+#(\d{1,6})\b",
    re.IGNORECASE,
)


@dataclass
class GitRef:
    """One reference discovered in git history."""
    kind: str                                # "hw-id" | "issue-number"
    ref: str                                 # e.g. "HW-0042" or "42"
    commit_sha: str
    commit_ts: str                           # ISO-8601 UTC
    subject: str
    body: str
    author: str
    # Whether the commit looks like "closing" the ref (fixes/closes keywords
    # found). Non-closing references (e.g. "refactor HW-0001 into module")
    # still attach as activity but don't drive the node to done.
    is_closing: bool = False


def spider(
    root: Path,
    *,
    prefix: str,
    since_iso: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[GitRef]:
    """Walk `git log` and return every ticket-like reference found.

    - `prefix` is the project's id_prefix (e.g. "HW"), used to match
      explicit ids. Case-sensitive; matches the config.
    - `since_iso` is passed to `git log --since=` directly.
    - `limit` caps commits scanned (safety valve for huge histories).
    """
    cmd = ["git", "log", "--pretty=format:%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1e"]
    if since_iso:
        cmd.append(f"--since={since_iso}")
    if limit:
        cmd.append(f"-n{limit}")

    try:
        cp = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []

    out: List[GitRef] = []
    blob = cp.stdout
    # Split on record separator (\x1e); each record is
    # sha \x1f author-date \x1f author \x1f subject \x1f body
    for raw in blob.split("\x1e"):
        raw = raw.strip("\n")
        if not raw:
            continue
        parts = raw.split("\x1f")
        if len(parts) < 4:
            continue
        sha, ts, author, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        haystack = subject + "\n" + body

        # Explicit HW-NNNN
        explicit_re = re.compile(_EXPLICIT_RE_TMPL.format(prefix=re.escape(prefix)))
        for m in explicit_re.finditer(haystack):
            n = int(m.group(1))
            ref = f"{prefix}-{n:04d}"
            # Closing keyword near this ref?
            closing = _is_closing_near(haystack, m.start(), m.end()) or _has_any_close_keyword(haystack)
            out.append(GitRef(
                kind="hw-id", ref=ref, commit_sha=sha, commit_ts=ts,
                subject=subject, body=body, author=author,
                is_closing=closing,
            ))

        # GitHub-issue-style #N with keyword
        for m in _KEYWORD_RE.finditer(haystack):
            out.append(GitRef(
                kind="issue-number", ref=m.group(1),
                commit_sha=sha, commit_ts=ts,
                subject=subject, body=body, author=author,
                is_closing=True,
            ))

    return out


def _is_closing_near(text: str, start: int, end: int) -> bool:
    """Heuristic: closing keyword within ~30 chars before the match."""
    window = text[max(0, start - 32): end]
    return bool(re.search(
        r"\b(?:close[sd]?|fix(?:e[sd])?|implement[sd]?|resolve[sd]?)\b",
        window, re.IGNORECASE,
    ))


def _has_any_close_keyword(text: str) -> bool:
    return bool(re.search(
        r"\b(?:close[sd]?|fix(?:e[sd])?|implement[sd]?|resolve[sd]?)\b",
        text, re.IGNORECASE,
    ))


def aggregate(refs: List[GitRef]) -> Dict[str, List[GitRef]]:
    """Group refs by canonical ref string (preserves discovery order)."""
    groups: Dict[str, List[GitRef]] = {}
    for r in refs:
        groups.setdefault(r.ref, []).append(r)
    return groups


def all_commit_subjects(
    root: Path, *, since_iso: Optional[str] = None, limit: Optional[int] = None,
) -> List[str]:
    """Return subject+body for every commit in the window (no ref filtering).

    Used by the spec scanner for git-evidence correlation — we need to see
    ALL commits, not just ones that mention a ticket.
    """
    cmd = ["git", "log", "--pretty=format:%s%n%b%x1e"]
    if since_iso:
        cmd.append(f"--since={since_iso}")
    if limit:
        cmd.append(f"-n{limit}")
    try:
        cp = subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out: List[str] = []
    for raw in cp.stdout.split("\x1e"):
        raw = raw.strip("\n")
        if raw:
            out.append(raw)
    return out


def has_git_history(root: Path) -> bool:
    """True if `root` is inside a git worktree with at least one commit."""
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=5,
        )
        return cp.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
