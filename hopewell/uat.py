"""UAT (user-acceptance testing) tracking.

Hopewell's "done" status means internal tests pass + the node's
definition_of_done predicates are green. That's insufficient for work
where a human has to validate against acceptance criteria. The
`needs-uat` component captures the gap:

  - `hopewell uat flag <id>` adds the component (usually at creation time)
  - `hopewell uat list` shows every flagged node whose UAT status is pending
  - `hopewell uat {pass|fail|waive} <id>` records the outcome
  - `hopewell uat backfill` retroactively flags older done nodes when a
    project realises after-the-fact that UAT tracking was missing

State lives in `component_data.needs-uat`:
    status:              pending | passed | failed | waived
    acceptance_criteria: list of strings (optional but recommended)
    verified_by:         agent or human name (@ prefix)
    verified_at:         iso timestamp
    notes:               free-form text from the verifier
    failure_reason:      set when status=failed

The node's main status (idea/ready/doing/review/done/…) is orthogonal
and unaffected. A "done" node with UAT status=pending is still considered
not-shipped to end users; a done+UAT=failed node is a bug to reopen
(handled explicitly via `hopewell set-status` — `uat fail` doesn't
auto-reopen to keep the outcome/response split clean).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


COMPONENT = "needs-uat"

STATUS_PENDING = "pending"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_WAIVED = "waived"

VALID_STATUSES = {STATUS_PENDING, STATUS_PASSED, STATUS_FAILED, STATUS_WAIVED}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _uat_block(node) -> Dict[str, Any]:
    return node.component_data.get(COMPONENT) or {}


def _save_uat_block(node, block: Dict[str, Any]) -> None:
    node.component_data[COMPONENT] = block


# ---------------------------------------------------------------------------
# Flag / unflag
# ---------------------------------------------------------------------------


def flag(project, node_id: str, *,
         acceptance_criteria: Optional[List[str]] = None,
         actor: Optional[str] = None) -> Dict[str, Any]:
    """Add the needs-uat component; set status=pending if not already set."""
    node = project.node(node_id)
    changed = False
    if COMPONENT not in node.components:
        node.components = sorted(set(node.components) | {COMPONENT})
        changed = True
    block = _uat_block(node)
    if "status" not in block:
        block["status"] = STATUS_PENDING
        changed = True
    if acceptance_criteria:
        existing = list(block.get("acceptance_criteria") or [])
        merged = existing + [c for c in acceptance_criteria if c not in existing]
        if merged != existing:
            block["acceptance_criteria"] = merged
            changed = True
    _save_uat_block(node, block)
    if changed:
        project.save_node(node)
        project.touch(node_id, f"[uat] flagged as needing UAT (status: {block['status']})",
                      actor=actor)
        project._attest(kind="uat.flag", node=node_id, actor=actor,
                        data={"status": block["status"],
                              "acceptance_criteria": block.get("acceptance_criteria", [])})
    return block


def unflag(project, node_id: str, *, actor: Optional[str] = None,
           reason: Optional[str] = None) -> None:
    """Remove the needs-uat component entirely. Rare — for genuine 'never needed UAT' cases."""
    node = project.node(node_id)
    if COMPONENT not in node.components:
        return
    node.components = [c for c in node.components if c != COMPONENT]
    node.component_data.pop(COMPONENT, None)
    project.save_node(node)
    project.touch(node_id, f"[uat] unflagged" + (f" — {reason}" if reason else ""),
                  actor=actor)
    project._attest(kind="uat.unflag", node=node_id, actor=actor,
                    data={"reason": reason})


# ---------------------------------------------------------------------------
# Record outcomes
# ---------------------------------------------------------------------------


def mark(project, node_id: str, status: str, *,
         verified_by: Optional[str] = None,
         notes: Optional[str] = None,
         failure_reason: Optional[str] = None,
         actor: Optional[str] = None) -> Dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid UAT status {status!r}; valid: {sorted(VALID_STATUSES)}")
    node = project.node(node_id)
    if COMPONENT not in node.components:
        # Auto-flag so agents don't have to run two commands
        node.components = sorted(set(node.components) | {COMPONENT})
    block = _uat_block(node)
    block["status"] = status
    block["verified_by"] = verified_by or actor or "(unknown)"
    block["verified_at"] = _now_iso()
    if notes:
        block["notes"] = notes
    if failure_reason and status == STATUS_FAILED:
        block["failure_reason"] = failure_reason
    elif status != STATUS_FAILED:
        # Clear failure_reason on non-failed transitions
        block.pop("failure_reason", None)
    _save_uat_block(node, block)
    project.save_node(node)

    note = f"[uat] {status}"
    if failure_reason:
        note += f" — {failure_reason}"
    elif notes:
        note += f" — {notes}"
    project.touch(node_id, note, actor=actor)
    project._attest(kind=f"uat.{status}", node=node_id, actor=actor,
                    data={"verified_by": block["verified_by"],
                          "notes": notes, "failure_reason": failure_reason})
    return block


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_uat(project, *, status: Optional[str] = None,
             include_unflagged: bool = False) -> List[Dict[str, Any]]:
    """Return every node carrying `needs-uat` (optionally filtered by status)."""
    out: List[Dict[str, Any]] = []
    for n in project.all_nodes():
        if COMPONENT not in n.components and not include_unflagged:
            continue
        block = _uat_block(n)
        cur_status = block.get("status", STATUS_PENDING)
        if status and status != "all" and cur_status != status:
            continue
        out.append({
            "id": n.id,
            "title": n.title,
            "node_status": n.status.value if hasattr(n.status, "value") else n.status,
            "owner": n.owner,
            "uat_status": cur_status,
            "acceptance_criteria": block.get("acceptance_criteria", []),
            "verified_by": block.get("verified_by"),
            "verified_at": block.get("verified_at"),
            "notes": block.get("notes"),
            "failure_reason": block.get("failure_reason"),
            "components": list(n.components),
        })
    # Pending first, then fails, then passed, then waived
    order = {STATUS_PENDING: 0, STATUS_FAILED: 1, STATUS_PASSED: 2, STATUS_WAIVED: 3}
    out.sort(key=lambda r: (order.get(r["uat_status"], 99), r["id"]))
    return out


# ---------------------------------------------------------------------------
# Backfill — retro-flag nodes whose UAT tracking was never set up
# ---------------------------------------------------------------------------


def backfill(project, *,
             node_status: Optional[str] = None,
             component: Optional[str] = None,
             has_all: Optional[List[str]] = None,
             since: Optional[str] = None,
             dry_run: bool = False,
             actor: Optional[str] = None) -> List[Dict[str, Any]]:
    """Add needs-uat=pending to every node matching the filters.

    Used when a project realises after-the-fact that UAT tracking was
    missing (the Gulliver case: everything got marked done with no UAT).

    Filters combine AND-style. Nodes that already carry needs-uat are
    skipped regardless of their current UAT status — backfill never
    overrides an explicit decision.
    """
    touched: List[Dict[str, Any]] = []
    for n in project.all_nodes():
        if COMPONENT in n.components:
            continue
        ns = n.status.value if hasattr(n.status, "value") else n.status
        if node_status and node_status != "any" and ns != node_status:
            continue
        if component and component not in n.components:
            continue
        if has_all and not all(c in n.components for c in has_all):
            continue
        if since:
            updated = n.updated or n.created
            if updated < since:
                continue

        touched.append({
            "id": n.id, "title": n.title, "node_status": ns,
            "owner": n.owner, "components": list(n.components),
        })
        if not dry_run:
            flag(project, n.id, actor=actor)
    return touched
