"""Shared gate logic invoked by git hooks (HW-0050).

These are the `declared gates` (Category B in the hooks-vs-orchestrator
analysis — see AgentFactory `patterns/drafts/hooks-vs-orchestrator.md`).
Each gate takes minimal context (commit message, branch name, project
root) and returns a `GateResult`.

The hook scripts in `hopewell/hook_templates.py` shell out to the
`hopewell` CLI, which dispatches here. Keeping the logic in-library
(rather than inline in the shell script) means:

  * Python test coverage is possible without invoking git.
  * Hooks remain tiny shell stubs — easier to audit + uninstall.
  * Any Hopewell release that updates gate logic updates hook behaviour
    transparently (no need to re-run `taskflow hooks install`).

All gates obey the following contract:

  * Exit-code semantics are enforced by the CLI wrapper, not here.
    Gates return `GateResult(ok=True|False, ...)` — infrastructure
    failures (project not found, CLI not on PATH, etc.) become `ok=True`
    with a `skipped` reason so hooks never block on tooling errors.
  * The `HOPEWELL_SKIP_HOOKS=1` bypass is checked in the shell script
    before we're even invoked. We don't need to check it here — but we
    do respect `HOPEWELL_GATE_*` env-var overrides for individual gates
    so testing is surgical.

Gate catalogue:

  * `check_hw_reference(msg, prefix)` — deny commits without an HW-NNNN
    reference in the message (pre-commit).
  * `check_drift(project_root)` — deny commits while spec-refs are
    drifted AND no active reconciliation review covers them (pre-commit).
  * `check_release_readiness(project_root, branch)` — on a push to the
    trunk branch, deny if an in-progress release node scores below its
    threshold (pre-push).

This module is stdlib-only. It imports other hopewell modules lazily so
it works even if a stale hook script survives a partial uninstall.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    """Outcome of a gate check.

    `ok=True` means "let the git operation proceed". `ok=False` means
    "block". `skipped` is a non-empty string when the gate couldn't run
    (tooling missing, no project, env override). A skipped gate is
    always `ok=True` — we never block on infrastructure failure.
    """

    ok: bool
    gate: str
    message: str = ""
    detail: List[str] = field(default_factory=list)
    skipped: Optional[str] = None

    def format_for_hook(self) -> str:
        """Human-readable multi-line banner for the hook to print."""
        head = f"taskflow: [{self.gate}] {'OK' if self.ok else 'BLOCKED'} — {self.message}".rstrip(" —")
        lines = [head]
        for d in self.detail:
            lines.append(f"  {d}")
        if self.skipped:
            lines.append(f"  (skipped: {self.skipped})")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# A) HW-NNNN reference in commit message
# ---------------------------------------------------------------------------


def check_hw_reference(commit_message: str, prefix: str = "HW") -> GateResult:
    """Reject commit messages that lack any `<PREFIX>-NNNN` reference.

    Accepts case-insensitive matches (`hw-0050` works too) but reports
    the canonical uppercased form. Merge-commit headers and empty
    messages are allowed (we can't reasonably require an id on a merge
    commit created by `git merge --no-ff`).
    """
    if os.environ.get("HOPEWELL_GATE_SKIP_HW_REF") == "1":
        return GateResult(ok=True, gate="hw-ref", skipped="HOPEWELL_GATE_SKIP_HW_REF=1")

    msg = (commit_message or "").strip()
    if not msg:
        return GateResult(ok=True, gate="hw-ref", message="empty message (allowed)")

    first_line = msg.splitlines()[0].lower()
    if first_line.startswith(("merge ", "revert ", "fixup!", "squash!", "amend!")):
        return GateResult(ok=True, gate="hw-ref", message=f"{first_line.split()[0]} commit (allowed)")

    pat = re.compile(rf"\b({re.escape(prefix)}-\d+)\b", re.IGNORECASE)
    found = pat.findall(msg)
    if found:
        canonical = sorted({f.upper() for f in found})
        return GateResult(
            ok=True,
            gate="hw-ref",
            message=f"found {', '.join(canonical)}",
        )
    return GateResult(
        ok=False,
        gate="hw-ref",
        message=f"commit message must reference a work-item id ({prefix}-NNNN)",
        detail=[
            "Add a reference like `[HW-0050]` or `fixes HW-0050` to the message.",
            "To bypass for this one commit:  HOPEWELL_SKIP_HOOKS=1 git commit ...",
        ],
    )


# ---------------------------------------------------------------------------
# B) Spec-ref drift gate
# ---------------------------------------------------------------------------


def check_drift(project_root: Path) -> GateResult:
    """Block the commit if any spec-ref has drifted AND no active
    reconciliation review is already tracking it.

    Gracefully degrades: if spec-input isn't configured, returns ok.
    """
    if os.environ.get("HOPEWELL_GATE_SKIP_DRIFT") == "1":
        return GateResult(ok=True, gate="drift", skipped="HOPEWELL_GATE_SKIP_DRIFT=1")

    try:
        from taskflow.project import Project
        from taskflow import spec_input as spec_mod
        from taskflow import reconciliation as recon_mod
    except ImportError as e:
        return GateResult(ok=True, gate="drift", skipped=f"imports unavailable: {e}")

    try:
        project = Project.load(project_root)
    except Exception as e:  # noqa: BLE001
        return GateResult(ok=True, gate="drift", skipped=f"no project: {e}")

    # If there are no spec-refs at all, nothing to gate.
    try:
        entries = spec_mod.drift_all(project)
    except FileNotFoundError:
        return GateResult(ok=True, gate="drift", skipped="no spec-refs configured")
    except Exception as e:  # noqa: BLE001
        return GateResult(ok=True, gate="drift", skipped=f"drift check failed: {e}")

    drifted = [e for e in entries if e.get("state") != "clean"]
    if not drifted:
        return GateResult(ok=True, gate="drift", message=f"{len(entries)} spec-refs clean")

    # Check active reconciliation reviews — if they cover every drifted
    # ref, we let the commit through (the agent is already dealing with it).
    covered_nodes: set = set()
    try:
        reviews = recon_mod.list_active_reviews(project)
        for review in reviews or []:
            for nid in review.get("nodes", []) or []:
                covered_nodes.add(nid)
    except AttributeError:
        # `list_active_reviews` may not exist in every Hopewell version; be forgiving.
        pass
    except Exception:  # noqa: BLE001
        pass

    uncovered = [e for e in drifted if e.get("node") not in covered_nodes]
    if not uncovered:
        return GateResult(
            ok=True,
            gate="drift",
            message=f"{len(drifted)} drifted but all covered by active reconciliation review(s)",
        )

    detail = [f"{e.get('node','?')}  {e.get('path','?')}  state={e.get('state')}"
              for e in uncovered[:10]]
    if len(uncovered) > 10:
        detail.append(f"...and {len(uncovered) - 10} more")
    detail.append("Resolve by: (a) updating code or spec so drift clears,")
    detail.append("            (b) `taskflow reconcile start <node>` to track a review,")
    detail.append("            (c) HOPEWELL_SKIP_HOOKS=1 to force through this one commit.")
    return GateResult(
        ok=False,
        gate="drift",
        message=f"{len(uncovered)} drifted spec-ref(s) not covered by a reconciliation review",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# B) Release-readiness on push to trunk
# ---------------------------------------------------------------------------


TRUNK_BRANCHES = ("refs/heads/main", "refs/heads/master", "refs/heads/trunk")


def _trunk_from_ref(ref: str) -> Optional[str]:
    for b in TRUNK_BRANCHES:
        if ref == b or ref.endswith(b[len("refs/heads/"):]):
            return ref.rsplit("/", 1)[-1]
    return None


def _current_branch(project_root: Path) -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(project_root), capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def check_release_readiness(project_root: Path, *,
                            branch: Optional[str] = None) -> GateResult:
    """On push to trunk, block if an in-progress release is under threshold.

    Gracefully degrades: if no in-progress release exists, returns ok.
    """
    if os.environ.get("HOPEWELL_GATE_SKIP_RELEASE") == "1":
        return GateResult(ok=True, gate="release", skipped="HOPEWELL_GATE_SKIP_RELEASE=1")

    branch = branch or _current_branch(project_root) or ""
    is_trunk = branch in ("main", "master", "trunk")
    if not is_trunk:
        return GateResult(ok=True, gate="release",
                          skipped=f"branch '{branch}' is not trunk (main/master/trunk)")

    try:
        from taskflow.project import Project
        from taskflow import release as release_mod
    except ImportError as e:
        return GateResult(ok=True, gate="release", skipped=f"imports unavailable: {e}")

    try:
        project = Project.load(project_root)
    except Exception as e:  # noqa: BLE001
        return GateResult(ok=True, gate="release", skipped=f"no project: {e}")

    try:
        in_progress = release_mod.list_releases(project, status="draft") or []
        held = release_mod.list_releases(project, status="held") or []
    except Exception as e:  # noqa: BLE001
        return GateResult(ok=True, gate="release", skipped=f"release lookup failed: {e}")

    candidates = in_progress + held
    if not candidates:
        return GateResult(ok=True, gate="release", message="no in-progress release nodes")

    # Score each candidate; block if ANY is below threshold.
    blocked = []
    for rel in candidates:
        version = rel.get("version") or rel.get("id")
        if not version:
            continue
        try:
            sc = release_mod.score(project, version)
        except Exception:  # noqa: BLE001
            continue
        total = sc.get("total", 0)
        threshold = sc.get("threshold", 0)
        outcome = sc.get("outcome", "")
        if outcome == "below-threshold" or (threshold and total < threshold):
            blocked.append((version, total, threshold, sc))

    if not blocked:
        return GateResult(
            ok=True,
            gate="release",
            message=f"{len(candidates)} in-progress release(s) all at/above threshold",
        )

    detail: List[str] = []
    for version, total, threshold, sc in blocked:
        detail.append(f"{version}: {total}/100 (threshold {threshold}) -> {sc.get('outcome','?')}")
        for signal in sc.get("signals", [])[:3]:
            detail.append(f"    {signal.get('name')}: {signal.get('score')}/{signal.get('weight')} — {signal.get('justification','')[:60]}")
    detail.append("Resolve by improving release signals, or HOPEWELL_SKIP_HOOKS=1 to bypass.")
    return GateResult(
        ok=False,
        gate="release",
        message=f"{len(blocked)} in-progress release(s) below threshold; push to trunk blocked",
        detail=detail,
    )
