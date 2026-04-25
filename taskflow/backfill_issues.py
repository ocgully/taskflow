"""GitHub issue sync for `taskflow backfill` (opt-in via --github).

Shells out to the `gh` CLI. Open issues → status=idea, closed issues →
status=done. Comments populate `notes`.

Strictly opt-in: never auto-fires on `taskflow init`; callers must pass
`--source issues` or `--github` / `--github-repo owner/name` explicitly.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Issue:
    number: int
    title: str
    body: str
    state: str                           # "open" | "closed"
    created_at: str
    updated_at: str
    author: str
    comments: List[str] = field(default_factory=list)
    url: str = ""


def gh_available() -> bool:
    try:
        cp = subprocess.run(
            ["gh", "--version"], capture_output=True, text=True, timeout=5,
        )
        return cp.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def fetch(
    repo: Optional[str] = None,
    *,
    limit: int = 200,
    cwd: Optional[Path] = None,
) -> List[Issue]:
    """Fetch issues via `gh issue list --json ...`.

    If `repo` is None, `gh` uses the current working directory's git remote.
    Returns an empty list on any failure (network, auth, not-a-repo, etc.).
    """
    if not gh_available():
        return []

    fields = "number,title,body,state,createdAt,updatedAt,author,url,comments"
    cmd = [
        "gh", "issue", "list",
        "--state", "all",
        "--limit", str(limit),
        "--json", fields,
    ]
    if repo:
        cmd.extend(["--repo", repo])

    try:
        cp = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(cwd) if cwd else None, timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if cp.returncode != 0:
        return []

    try:
        rows = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return []

    out: List[Issue] = []
    for r in rows:
        author = ""
        a = r.get("author") or {}
        if isinstance(a, dict):
            author = a.get("login") or ""
        comments_raw = r.get("comments") or []
        comments: List[str] = []
        for c in comments_raw:
            if not isinstance(c, dict):
                continue
            who = ""
            auth = c.get("author") or {}
            if isinstance(auth, dict):
                who = auth.get("login") or ""
            ts = c.get("createdAt") or ""
            body = (c.get("body") or "").strip()
            if body:
                comments.append(f"{ts} [{who}] {body}")
        out.append(Issue(
            number=int(r.get("number") or 0),
            title=r.get("title") or "",
            body=r.get("body") or "",
            state=(r.get("state") or "").lower(),
            created_at=r.get("createdAt") or "",
            updated_at=r.get("updatedAt") or "",
            author=author,
            comments=comments,
            url=r.get("url") or "",
        ))
    return out
