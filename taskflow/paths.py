"""Project-root and `.taskflow/` discovery.

Detects either `.taskflow/` (preferred, post-rebrand) or `.hopewell/`
(legacy) — walks up from the cwd. New init creates `.taskflow/`. The
`taskflow migrate-from-hopewell` CLI command renames an existing
`.hopewell/` to `.taskflow/` in place.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


MARKER = ".taskflow"            # preferred
LEGACY_MARKER = ".hopewell"     # pre-rebrand
# Order matters — first match wins.
MARKERS = (MARKER, LEGACY_MARKER)


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from `start` (or cwd) until a storage-dir marker is
    found. Returns None if not inside an initialised project.

    Prefers `.taskflow/`; falls back to `.hopewell/` for backwards compat.
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        for m in MARKERS:
            if (candidate / m).is_dir():
                return candidate
    return None


def require_project_root(start: Optional[Path] = None) -> Path:
    root = find_project_root(start)
    if root is None:
        raise FileNotFoundError(
            f"not inside a TaskFlow project — no `{MARKER}/` (or legacy "
            f"`{LEGACY_MARKER}/`) directory found walking up from "
            f"{(start or Path.cwd()).resolve()}. Run `taskflow init` first."
        )
    return root


def hw_dir(project_root: Path) -> Path:
    """Return the on-disk storage dir, preferring `.taskflow/` but
    falling back to `.hopewell/` if only the legacy dir exists."""
    new = project_root / MARKER
    if new.is_dir():
        return new
    legacy = project_root / LEGACY_MARKER
    if legacy.is_dir():
        return legacy
    return new


# Alias under the new preferred name.
tf_dir = hw_dir


def ensure_hw_dir(project_root: Path) -> Path:
    d = hw_dir(project_root)
    # If we landed on a legacy `.hopewell/`, keep writing there so reads
    # stay consistent. The user runs `taskflow migrate-from-hopewell`
    # to rename explicitly.
    d.mkdir(parents=True, exist_ok=True)
    (d / "nodes").mkdir(exist_ok=True)
    (d / "views").mkdir(exist_ok=True)
    (d / "orchestrator").mkdir(exist_ok=True)
    (d / "orchestrator" / "runs").mkdir(exist_ok=True)
    return d


# Alias under the new preferred name.
ensure_tf_dir = ensure_hw_dir
