"""Flow runtime (HW-0028) — push-to-inbox semantics.

Layered on top of the HW-0027 flow network. A WorkItem (`Node`) carries
one or more `NodeLocation` entries recording which executors it is
currently *at*. An executor's **inbox** is a computed projection over
`events.jsonl`:

    inbox(X) = {flow.push events where data.to_executor == X}
             - {flow.ack events where data.executor == X, same node}

There are no inbox files, no daemons, no polling. The only runtime
primitive is `events.append` + the event log order.

Orthogonality (design locked in HW-0026):

* **Status vs location**: the `idea->doing->review->done->archived`
  state machine is untouched. Locations are a separate axis.
* **Claim vs location**: a claim is a branch-scoped mutex; independent
  of where the work physically sits.
* **Done auto-fires** when every `required=True` route out of the work
  item's visited executors eventually reaches a `target` executor.
  (See `all_required_terminals_reached`.)

Public surface — functions only, no classes (keep the module thin
enough for agents to reason about a slice at a time):

    inbox(project, executor_id) -> [entry, ...]
    where(project, node_id)     -> [active_location_dict, ...]
    enter(project, node_id, executor_id, ...)
    leave(project, node_id, executor_id, ...)
    push(project, node_id, to_executor, ...)
    ack(project, node_id, executor_id, ...)
    all_required_terminals_reached(project, node_id) -> bool
    maybe_auto_done(project, node_id, ...)

All mutating functions are idempotent where stated; all emit events via
`hopewell.events.append`.

Stdlib only.
"""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from hopewell import events as events_mod
from hopewell.model import Node, NodeLocation, NodeStatus


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# lazy network loader (avoid hard dep at import time)
# ---------------------------------------------------------------------------


def _load_network(project):
    from hopewell import network as net_mod
    return net_mod.load_network(project.root)


def _executor_ids(project) -> Set[str]:
    return set(_load_network(project).executors.keys())


# ---------------------------------------------------------------------------
# inbox projection
# ---------------------------------------------------------------------------


def inbox(project, executor_id: str) -> List[Dict[str, Any]]:
    """Compute the current inbox for an executor.

    Walks `events.jsonl`:
      * Collect `flow.push` where `data.to_executor == executor_id`
      * Subtract any matching `flow.ack` where `data.executor == executor_id`
        for the same work item (same `node` id).

    An ack is matched to the OLDEST outstanding push for that node.
    (Enables the natural case: two pushes to @architect, one ack leaves
    one pending.)

    Returns the remaining pushes, **oldest first**. Each entry:

        {
          "node":          "<work-item-id>",
          "pushed_at":     "<iso ts>",
          "from_executor": "<id or None>",
          "artifact":      "<path or None>",
          "reason":        "<text or None>",
        }
    """
    events = events_mod.read_all(project.events_path)
    # Per-node FIFO queue of pending pushes; drained by acks.
    pending: Dict[str, List[Dict[str, Any]]] = {}
    for ev in events:
        kind = ev.get("kind")
        if kind == "flow.push":
            data = ev.get("data") or {}
            if data.get("to_executor") != executor_id:
                continue
            nid = ev.get("node")
            if not nid:
                continue
            pending.setdefault(nid, []).append({
                "node": nid,
                "pushed_at": ev.get("ts"),
                "from_executor": data.get("from_executor"),
                "artifact": data.get("artifact"),
                "reason": data.get("reason"),
            })
        elif kind == "flow.ack":
            data = ev.get("data") or {}
            if data.get("executor") != executor_id:
                continue
            nid = ev.get("node")
            if not nid:
                continue
            q = pending.get(nid)
            if q:
                q.pop(0)
    # Flatten and sort oldest-first by pushed_at.
    out: List[Dict[str, Any]] = []
    for q in pending.values():
        out.extend(q)
    out.sort(key=lambda e: (e.get("pushed_at") or "", e.get("node") or ""))
    return out


def where(project, node_id: str) -> List[Dict[str, Any]]:
    """Return the node's ACTIVE locations as dicts (oldest entry first)."""
    node = project.node(node_id)
    return [loc.to_dict() for loc in node.active_locations()]


def history(project, node_id: str) -> List[Dict[str, Any]]:
    """Full location history (including closed). Useful for UI timelines."""
    node = project.node(node_id)
    return [loc.to_dict() for loc in node.locations]


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def _require_executor(project, executor_id: str) -> None:
    known = _executor_ids(project)
    if executor_id not in known:
        raise ValueError(
            f"unknown executor: {executor_id!r} "
            f"(known: {sorted(known) if known else '<none — run `hopewell network init`>'})"
        )


def enter(project, node_id: str, executor_id: str, *,
          artifact: Optional[str] = None,
          actor: Optional[str] = None,
          reason: Optional[str] = None) -> bool:
    """Add a NodeLocation at `executor_id` for the work item.

    Idempotent: if the node already has an active location at this
    executor, returns False and does NOT emit an event (quiet by
    default — less noise in the event log).

    HW-0034 (reconciliation gate): when `executor_id` is an `agent`
    component executor and the work item declares `spec-input`
    references, we run a pre-flight drift check via
    `reconciliation.check_drift_gate`. If a referenced slice has
    drifted and there is no resolved `downstream-review` covering it,
    the gate auto-creates a review node and raises
    `ReconciliationRequired` — propagated to the caller. Set
    `HOPEWELL_SKIP_RECONCILIATION=1` to disable.

    Returns True on first-time enter, False on no-op.
    """
    _require_executor(project, executor_id)
    # HW-0034: reconciliation pre-flight. Imported lazily to keep flow.py
    # standalone-importable (and to avoid a circular import — reconciliation
    # itself touches `events`/`spec_input`/`model`/`project` modules).
    from hopewell import reconciliation as recon_mod
    recon_mod.check_drift_gate(project, node_id, executor_id, actor=actor)

    node = project.node(node_id)
    existing = node.location_at(executor_id)
    if existing is not None:
        # Refresh artifact hint quietly if new one is provided.
        if artifact and existing.last_artifact != artifact:
            existing.last_artifact = artifact
            project.save_node(node)
        return False

    loc = NodeLocation(
        executor_id=executor_id,
        entered_at=_now(),
        last_artifact=artifact,
    )
    node.locations.append(loc)
    project.save_node(node)
    data: Dict[str, Any] = {"executor": executor_id}
    if artifact:
        data["artifact"] = artifact
    if reason:
        data["reason"] = reason
    events_mod.append(project.events_path, "flow.enter",
                      node=node_id, actor=actor, data=data)
    # After adding a location we may have reached a terminal — check.
    maybe_auto_done(project, node_id, actor=actor)
    return True


def leave(project, node_id: str, executor_id: str, *,
          actor: Optional[str] = None,
          reason: Optional[str] = None) -> bool:
    """Close the active location at `executor_id` (set `left_at`).

    Idempotent: if there is no active location at this executor,
    returns False and does nothing. Returns True on first-time leave.
    """
    node = project.node(node_id)
    loc = node.location_at(executor_id)
    if loc is None:
        return False
    loc.left_at = _now()
    project.save_node(node)
    data: Dict[str, Any] = {"executor": executor_id}
    if reason:
        data["reason"] = reason
    events_mod.append(project.events_path, "flow.leave",
                      node=node_id, actor=actor, data=data)
    return True


def push(project, node_id: str, to_executor: str, *,
         from_executor: Optional[str] = None,
         artifact: Optional[str] = None,
         reason: Optional[str] = None,
         actor: Optional[str] = None) -> Dict[str, Any]:
    """Offer the work item to a target executor's inbox.

    Does **not** modify `Node.locations` — the target decides whether
    to accept (via `ack` + `enter`). This keeps "offered" and
    "accepted" as distinct states.

    Raises `ValueError` if `to_executor` is unknown. The `from_executor`
    (if provided) is also validated; pass None to model "external /
    manual push" cases.
    """
    _require_executor(project, to_executor)
    if from_executor is not None:
        _require_executor(project, from_executor)
    # Target must be known to the project as a work item.
    if not project.has_node(node_id):
        raise FileNotFoundError(f"node not found: {node_id}")

    data: Dict[str, Any] = {"to_executor": to_executor}
    if from_executor:
        data["from_executor"] = from_executor
    if artifact:
        data["artifact"] = artifact
    if reason:
        data["reason"] = reason
    ev = events_mod.append(project.events_path, "flow.push",
                           node=node_id, actor=actor, data=data)
    return ev


def ack(project, node_id: str, executor_id: str, *,
        outcome: str = "processed",
        note: Optional[str] = None,
        actor: Optional[str] = None) -> Dict[str, Any]:
    """Target acks a pending push. Emits `flow.ack`.

    `outcome` is free-form (e.g. "accepted" / "rejected" / "processed").
    Orchestrator integration uses "success" / "failure" — see
    `orchestrator.py`.

    Does NOT by itself modify `Node.locations`. Typically paired with
    `enter(node_id, executor_id)` when the target actually takes the
    work.
    """
    _require_executor(project, executor_id)
    data: Dict[str, Any] = {"executor": executor_id, "outcome": outcome}
    if note:
        data["note"] = note
    ev = events_mod.append(project.events_path, "flow.ack",
                           node=node_id, actor=actor, data=data)
    return ev


# ---------------------------------------------------------------------------
# "done" auto-fire via required-terminal reachability
# ---------------------------------------------------------------------------


def _visited_executors(project, node_id: str) -> Set[str]:
    """Every executor the work item has ever been at (active OR closed).

    We use full history so that pushing past a terminal still counts as
    "reached" even after `leave`.
    """
    node = project.node(node_id)
    return {loc.executor_id for loc in node.locations}


def all_required_terminals_reached(project, node_id: str) -> bool:
    """True iff every `required=True` route chain out of the work item's
    starting source reaches at least one `target` executor it has
    visited.

    Intuition: the work item is "done" when every required exit edge
    from every required-path executor has been honored all the way to a
    `target` that the work item actually landed on. In practice this
    reduces to: **every `target` marked required-reachable from any
    visited executor has been visited**.

    Implementation: BFS from visited executors through required routes
    only; if no unreached required target exists, we're done.
    """
    net = _load_network(project)
    visited = _visited_executors(project, node_id)
    if not visited:
        return False

    # Adjacency by required routes only.
    adj: Dict[str, List[str]] = {}
    for r in net.routes:
        if r.required:
            adj.setdefault(r.from_id, []).append(r.to_id)

    # All required targets reachable from any visited executor (walking
    # required edges only).
    required_targets: Set[str] = set()
    frontier = list(visited)
    seen: Set[str] = set()
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        ex = net.executors.get(cur)
        if ex is not None and ex.has_component("target"):
            required_targets.add(cur)
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                frontier.append(nxt)

    if not required_targets:
        # No required targets reachable from anywhere visited —
        # nothing to finish. Don't auto-fire done.
        return False

    return required_targets.issubset(visited)


def maybe_auto_done(project, node_id: str, *,
                    actor: Optional[str] = None) -> bool:
    """If the work item satisfies `all_required_terminals_reached`,
    walk its status toward `done` (via the legal transition sequence).

    No-op if already done/archived/cancelled, or if not all required
    terminals are reached.

    Returns True iff a status change was made.
    """
    node = project.node(node_id)
    cur = node.status if isinstance(node.status, NodeStatus) else NodeStatus(node.status)
    if cur in (NodeStatus.done, NodeStatus.archived, NodeStatus.cancelled):
        return False
    if not all_required_terminals_reached(project, node_id):
        return False

    # Walk via the legal path: cur -> ... -> done.
    sequence_from = {
        NodeStatus.idea:     [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done],
        NodeStatus.blocked:  [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done],
        NodeStatus.ready:    [NodeStatus.doing, NodeStatus.review, NodeStatus.done],
        NodeStatus.doing:    [NodeStatus.review, NodeStatus.done],
        NodeStatus.review:   [NodeStatus.done],
    }
    steps = sequence_from.get(cur, [])
    if not steps:
        return False
    for s in steps:
        try:
            project.set_status(node_id, s, actor=actor,
                               reason="flow: all required terminals reached")
        except ValueError:
            # Transition blocked for some reason — bail out quietly; the
            # caller can inspect the node.
            return False
    return True


# ---------------------------------------------------------------------------
# convenience queries
# ---------------------------------------------------------------------------


def pending_pushes(project, executor_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Flat list of all pending pushes (optionally for one executor)."""
    net = _load_network(project)
    executors = [executor_id] if executor_id else list(net.executors.keys())
    out: List[Dict[str, Any]] = []
    for eid in executors:
        for entry in inbox(project, eid):
            entry = dict(entry)
            entry["to_executor"] = eid
            out.append(entry)
    out.sort(key=lambda e: e.get("pushed_at") or "")
    return out
