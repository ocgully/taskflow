"""Git-hook installer (HW-0050).

Installs the layered Hopewell git hooks into `.git/hooks/`:

* **post-commit** (always) — category A: mechanical bookkeeping. Scans
  the commit message for work-item references + `closes/fixes HW-NNNN`
  triggers, touches affected nodes, emits flow events.

* **pre-commit** (full install only) — category B: declared gates.
  Rejects commits that (a) lack a work-item reference in the message,
  or (b) leave spec-refs drifted + uncovered by a reconciliation review.

* **pre-push** (full install only) — category B: declared gate.
  On a push to trunk (`main` / `master` / `trunk`), blocks if any
  in-progress release node scores below its threshold.

Category C (context injection — Pedia, universal context) is
**deliberately NOT in this file**. Git hooks can't do it because they
don't know about the AI session. See `hopewell/claude_hooks.py`
(HW-0040) for the Claude Code hook entry points that handle C.

Uninstall semantics
-------------------

Each installed hook script starts with a `# hopewell:managed` sentinel
(see `hopewell.hook_templates.SENTINEL`). Uninstall:

  * If the file is PURE Hopewell (sentinel line present, nothing else
    of substance), `uninstall` deletes it.
  * If the file has non-Hopewell content mixed in (rare — user wrote
    their own hook, then ran `hooks install`), we strip just the
    managed block between `MARKER_BEGIN` / `MARKER_END`.

Idempotent. Safe to re-run. Never touches hooks without our sentinel.

Bypass (documented prominently in the README and in the hook scripts):

    HOPEWELL_SKIP_HOOKS=1 git commit ...
    HOPEWELL_SKIP_HOOKS=1 git push ...
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Dict, List, Optional

from taskflow.hook_templates import (
    HOOK_BODIES,
    MARKER_BEGIN,
    MARKER_END,
    SENTINEL,
    render,
)


# ---------------------------------------------------------------------------
# Hook-set profiles
# ---------------------------------------------------------------------------

#: Minimal install — only the post-commit bookkeeping hook.
MINIMAL_HOOKS = ("post-commit",)

#: Full install — post-commit + pre-commit + commit-msg + pre-push
#: (bookkeeping + declared gates). This is the default going forward
#: (HW-0050).
FULL_HOOKS = ("post-commit", "pre-commit", "commit-msg", "pre-push")


# ---------------------------------------------------------------------------
# Internal path helpers
# ---------------------------------------------------------------------------


def _git_dir(project_root: Path) -> Optional[Path]:
    """Resolve `.git` whether it's a real dir, a file pointer (worktrees,
    submodules), or missing entirely."""
    candidate = project_root / ".git"
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        content = candidate.read_text(encoding="utf-8").strip()
        if content.startswith("gitdir:"):
            return Path(content.split(":", 1)[1].strip()).resolve()
    return None


def _hooks_dir(project_root: Path) -> Path:
    gd = _git_dir(project_root)
    if gd is None:
        raise RuntimeError("not inside a git working tree (no .git directory found)")
    hooks_dir = gd / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir


def _hook_path(project_root: Path, hook_name: str) -> Path:
    return _hooks_dir(project_root) / hook_name


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _is_pure_hopewell_hook(text: str) -> bool:
    """True iff the file is just our shebang + sentinel + managed block —
    no other user code interleaved."""
    stripped = text.strip()
    if SENTINEL not in stripped:
        return False
    # Everything between the first shebang (if any) and end-marker should
    # be ours. A heuristic test: remove our block, check residue.
    lines = stripped.splitlines()
    kept: List[str] = []
    in_block = False
    for line in lines:
        if line.strip() == MARKER_BEGIN:
            in_block = True
            continue
        if line.strip() == MARKER_END:
            in_block = False
            continue
        if in_block:
            continue
        # Skip our shebang + sentinel lines outside any block.
        if line.startswith("#!/") and "bash" in line:
            continue
        if line.strip() == SENTINEL:
            continue
        kept.append(line)
    residue = "\n".join(kept).strip()
    return residue == ""


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def install(project_root: Path, *, full: bool = False) -> Dict[str, Path]:
    """Install Hopewell hooks into the project's `.git/hooks/`.

    Returns a dict mapping hook name -> installed script path. Use
    `full=True` for the pre-commit + pre-push gates in addition to the
    bookkeeping post-commit hook.

    Idempotent: re-running either leaves the file identical (if already
    managed by us) or replaces just the managed block (if the file has
    other user content). Adds execute bits on POSIX.
    """
    hook_names = FULL_HOOKS if full else MINIMAL_HOOKS
    installed: Dict[str, Path] = {}
    for name in hook_names:
        installed[name] = _install_one(project_root, name)
    return installed


def _install_one(project_root: Path, hook_name: str) -> Path:
    hp = _hook_path(project_root, hook_name)
    new_script = render(hook_name)

    if hp.is_file():
        existing = hp.read_text(encoding="utf-8")
        if SENTINEL in existing and MARKER_BEGIN in existing:
            # Replace just our managed block, preserve any user content
            # before/after (allows chaining with user-written hooks).
            start = existing.index(MARKER_BEGIN)
            end = existing.index(MARKER_END, start) + len(MARKER_END)
            before = existing[:start].rstrip()
            after = existing[end:].lstrip()
            if not before.startswith("#!"):
                before = "#!/usr/bin/env bash\n" + before.lstrip("\n")
            # Insert a fresh managed block (without the shebang — the
            # user's existing shebang takes precedence).
            body = HOOK_BODIES[hook_name].strip()
            managed = (
                f"{SENTINEL}\n"
                f"{MARKER_BEGIN}\n"
                f"{body}\n"
                f"{MARKER_END}\n"
            )
            merged = before.rstrip() + "\n\n" + managed
            if after:
                merged += "\n" + after
            hp.write_text(merged, encoding="utf-8")
            _make_executable(hp)
            return hp
        # Existing non-hopewell hook. Back up and overwrite.
        backup = hp.with_suffix(hp.suffix + ".bak.hopewell") if hp.suffix else hp.parent / f"{hp.name}.bak.hopewell"
        try:
            backup.write_bytes(hp.read_bytes())
        except OSError:
            pass

    hp.write_text(new_script, encoding="utf-8")
    _make_executable(hp)
    return hp


def uninstall(project_root: Path) -> Dict[str, bool]:
    """Remove Hopewell's managed blocks from all hooks in this project.

    Returns a dict mapping hook name -> True if we removed something, False
    if there was nothing to remove. Files that are purely ours get deleted;
    files that had user content interleaved get their managed block
    surgically stripped.
    """
    result: Dict[str, bool] = {}
    for name in FULL_HOOKS:
        result[name] = _uninstall_one(project_root, name)
    return result


def _uninstall_one(project_root: Path, hook_name: str) -> bool:
    try:
        hp = _hook_path(project_root, hook_name)
    except RuntimeError:
        return False
    if not hp.is_file():
        return False
    text = hp.read_text(encoding="utf-8")
    if SENTINEL not in text and MARKER_BEGIN not in text:
        return False

    if _is_pure_hopewell_hook(text):
        hp.unlink()
        return True

    # Surgical strip of the managed block.
    if MARKER_BEGIN in text and MARKER_END in text:
        start = text.index(MARKER_BEGIN)
        end = text.index(MARKER_END, start) + len(MARKER_END)
        new = (text[:start].rstrip() + "\n" + text[end:].lstrip()).strip()
        # Also drop orphaned sentinel line if present.
        new_lines = [ln for ln in new.splitlines() if ln.strip() != SENTINEL]
        new = "\n".join(new_lines).strip()
        if new in ("", "#!/usr/bin/env bash"):
            hp.unlink()
        else:
            hp.write_text(new + "\n", encoding="utf-8")
        return True
    return False


# ---------------------------------------------------------------------------
# Status — for the CLI + tests
# ---------------------------------------------------------------------------


def status(project_root: Path) -> Dict[str, Dict[str, object]]:
    """Report which hooks are installed and whether they're hopewell-managed."""
    out: Dict[str, Dict[str, object]] = {}
    try:
        hdir = _hooks_dir(project_root)
    except RuntimeError:
        return out
    for name in FULL_HOOKS:
        hp = hdir / name
        entry: Dict[str, object] = {"installed": False, "managed": False, "path": str(hp)}
        if hp.is_file():
            entry["installed"] = True
            text = hp.read_text(encoding="utf-8", errors="replace")
            entry["managed"] = SENTINEL in text
        out[name] = entry
    return out
