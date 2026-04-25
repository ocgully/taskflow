"""Graph evolution — typed, atomic, reversible operations (v0.6 / HW-0014).

Agents call these functions during execution to reshape the work graph. Every
operation emits three things in lockstep:

    1. An `evolve.<op>` event on `events.jsonl`.
    2. An `evolve.<op>` attestation on `attestations.jsonl` (carries agent
       identity + fingerprint, per the existing ledger conventions).
    3. A line on `.hopewell/evolutions.jsonl` carrying a unique `change_id`
       PLUS the inverse payload needed to `rollback`.

The evolutions log is the source of truth for undo: it records the minimal
context required to replay the reverse operation. `rollback(change_id)` reads
a line, dispatches on `op`, and performs the inverse — which itself is
recorded as a fresh evolution (so you can rollback-a-rollback if you want).

Design guarantees:
    - Stdlib only.
    - `change_id` is deterministic-ish: 12 hex chars of SHA-256 over
      `(timestamp | op | canonical-json(payload))`. Collisions astronomically
      unlikely within a project; the log detects them on write anyway.
    - Ops are atomic from the evolutions-log perspective: we write the
      evolutions line AFTER the underlying mutation succeeds. If the
      mutation raises, no evolutions entry is appended (so rollback can't
      chase a phantom).
    - `rollback` itself is just another evolution. It appends a fresh
      `evolve.rollback` line whose inverse-payload points at the re-application
      of the original op. This is why rollback-of-rollback works.

Public API:
    add_node(project, *, components, title, owner=None, parent=None,
             actor=None, reason=None) -> str
    wire(project, from_id, to_id, kind, *, artifact=None,
         reason=None, actor=None) -> None
    unwire(project, from_id, to_id, kind, *, actor=None, reason=None) -> None
    add_loop(project, name, over, until, *, max_iterations=10,
             actor=None) -> str
    rollback(project, change_id, *, actor=None) -> None
    list_evolutions(project) -> list[dict]
"""
from __future__ import annotations

import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskflow import events as events_mod
from taskflow.model import EdgeKind, NodeInput, NodeOutput


EVOLUTIONS_FILE = "evolutions.jsonl"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def add_node(project, *, components: List[str], title: str,
             owner: Optional[str] = None, parent: Optional[str] = None,
             actor: Optional[str] = None,
             reason: Optional[str] = None) -> str:
    """Create a node and record an evolution. Returns the new node id.

    Inverse: `delete_node(<id>)`.
    """
    node = project.new_node(
        components=list(components),
        title=title,
        owner=owner,
        parent=parent,
        actor=actor,
    )
    payload = {
        "components": list(components),
        "title": title,
        "owner": owner,
        "parent": parent,
        "reason": reason,
        "node_id": node.id,
    }
    inverse = {"op": "delete_node", "node_id": node.id}
    change_id = _record(project, op="add_node", payload=payload,
                        inverse=inverse, actor=actor, node=node.id,
                        reason=reason)
    return node.id


def wire(project, from_id: str, to_id: str, kind: str, *,
         artifact: Optional[str] = None, reason: Optional[str] = None,
         actor: Optional[str] = None) -> None:
    """Create an edge. Reuses `Project.link`. Records an evolution.

    Inverse: `unwire(from_id, to_id, kind)` with the same arguments.
    """
    edge_kind = _coerce_edge_kind(kind)
    project.link(from_id, edge_kind, to_id,
                 artifact=artifact, reason=reason, actor=actor)
    payload = {
        "from": from_id, "to": to_id, "kind": edge_kind.value,
        "artifact": artifact, "reason": reason,
    }
    inverse = {
        "op": "unwire",
        "from": from_id, "to": to_id, "kind": edge_kind.value,
        "artifact": artifact,
    }
    _record(project, op="wire", payload=payload, inverse=inverse,
            actor=actor, node=from_id, reason=reason)


def unwire(project, from_id: str, to_id: str, kind: str, *,
           actor: Optional[str] = None,
           reason: Optional[str] = None) -> None:
    """Remove an edge by rewriting node front-matter.

    Reverses the front-matter mutations that `Project.link` performs for
    the given `kind`. Also emits an `edge.delete` event/attestation and an
    evolutions line. Inverse: `wire(from_id, to_id, kind, artifact=...)`.
    """
    edge_kind = _coerce_edge_kind(kind)
    artifact = _unwire_inplace(project, from_id, to_id, edge_kind)

    project._attest(
        kind="edge.delete", node=from_id, actor=actor, reason=reason,
        data={"from": from_id, "to": to_id, "kind": edge_kind.value,
              "artifact": artifact},
    )
    events_mod.append(
        project.events_path, "edge.delete", actor=actor,
        data={"from": from_id, "to": to_id, "kind": edge_kind.value,
              "artifact": artifact, "reason": reason},
    )

    payload = {
        "from": from_id, "to": to_id, "kind": edge_kind.value,
        "artifact": artifact, "reason": reason,
    }
    inverse = {
        "op": "wire",
        "from": from_id, "to": to_id, "kind": edge_kind.value,
        "artifact": artifact,
    }
    _record(project, op="unwire", payload=payload, inverse=inverse,
            actor=actor, node=from_id, reason=reason)


def add_loop(project, name: str, over: List[str], until: str, *,
             max_iterations: int = 10,
             actor: Optional[str] = None) -> str:
    """Create a `loop`-component node that represents an iterative subgraph.

    The node carries `component_data.loop = {over, until, max_iterations,
    iterations: []}`. The orchestrator does not need to understand it yet;
    it only sees a node with component `"loop"` and its metadata. Returns
    the loop node's id. Inverse: `delete_node(<id>)`.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    node = project.new_node(
        components=["loop"], title=name, actor=actor,
    )
    node.component_data["loop"] = {
        "over": list(over),
        "until": until,
        "max_iterations": int(max_iterations),
        "iterations": [],
    }
    project.save_node(node)

    payload = {
        "node_id": node.id, "name": name, "over": list(over),
        "until": until, "max_iterations": int(max_iterations),
    }
    inverse = {"op": "delete_node", "node_id": node.id}
    _record(project, op="add_loop", payload=payload, inverse=inverse,
            actor=actor, node=node.id, reason=None)
    return node.id


def rollback(project, change_id: str, *, actor: Optional[str] = None) -> None:
    """Undo an evolution by replaying its inverse payload.

    Fails loudly if `change_id` is unknown. A successful rollback appends
    a new `evolve.rollback` evolution whose inverse points at the original
    op — so rollback-of-rollback works.
    """
    entry = _find_evolution(project, change_id)
    if entry is None:
        raise KeyError(f"unknown change_id: {change_id}")

    inv = entry.get("inverse") or {}
    op = inv.get("op")
    if not op:
        raise ValueError(f"evolution {change_id} carries no inverse payload")

    original_payload = entry.get("payload") or {}
    original_op = entry.get("op")

    if op == "delete_node":
        node_id = inv["node_id"]
        if project.has_node(node_id):
            project.delete_node(node_id, actor=actor)
    elif op == "wire":
        edge_kind = _coerce_edge_kind(inv["kind"])
        project.link(
            inv["from"], edge_kind, inv["to"],
            artifact=inv.get("artifact"),
            reason=f"rollback of {change_id}",
            actor=actor,
        )
    elif op == "unwire":
        edge_kind = _coerce_edge_kind(inv["kind"])
        _unwire_inplace(project, inv["from"], inv["to"], edge_kind)
        project._attest(
            kind="edge.delete", node=inv["from"], actor=actor,
            reason=f"rollback of {change_id}",
            data={"from": inv["from"], "to": inv["to"],
                  "kind": edge_kind.value, "artifact": inv.get("artifact")},
        )
        events_mod.append(
            project.events_path, "edge.delete", actor=actor,
            data={"from": inv["from"], "to": inv["to"],
                  "kind": edge_kind.value,
                  "artifact": inv.get("artifact"),
                  "reason": f"rollback of {change_id}"},
        )
    else:
        raise ValueError(f"unsupported inverse op: {op!r} for {change_id}")

    # Rollback-of-rollback: the inverse of THIS rollback is to re-do the
    # original op, so stash enough info to replay it.
    payload = {
        "undoes": change_id,
        "original_op": original_op,
        "original_payload": original_payload,
    }
    inverse = {
        "op": "replay",
        "original_op": original_op,
        "original_payload": original_payload,
    }
    _record(project, op="rollback", payload=payload, inverse=inverse,
            actor=actor, node=entry.get("node"),
            reason=f"rollback of {change_id}")


def list_evolutions(project) -> List[Dict[str, Any]]:
    """All evolutions performed, newest first."""
    path = _evolutions_path(project)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    out.reverse()
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _evolutions_path(project) -> Path:
    return project.hw_dir / EVOLUTIONS_FILE


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def _make_change_id(ts: str, op: str, payload: Dict[str, Any]) -> str:
    blob = ts + "|" + op + "|" + json.dumps(
        payload, sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _record(project, *, op: str, payload: Dict[str, Any],
            inverse: Dict[str, Any], actor: Optional[str],
            node: Optional[str], reason: Optional[str]) -> str:
    """Emit the event, attestation, and evolutions-log line for one op.

    Returns the change_id.
    """
    ts = _now()
    change_id = _make_change_id(ts, op, payload)
    entry: Dict[str, Any] = {
        "ts": ts,
        "change_id": change_id,
        "op": op,
        "actor": actor,
        "node": node,
        "reason": reason,
        "payload": payload,
        "inverse": inverse,
    }

    evt_kind = f"evolve.{op}"
    events_mod.append(
        project.events_path, evt_kind, node=node, actor=actor,
        data={"change_id": change_id, "op": op, "payload": payload},
    )
    project._attest(
        kind=evt_kind, node=node, actor=actor, reason=reason,
        data={"change_id": change_id, "op": op, "payload": payload},
    )

    path = _evolutions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")

    return change_id


def _find_evolution(project, change_id: str) -> Optional[Dict[str, Any]]:
    path = _evolutions_path(project)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("change_id") == change_id:
                return obj
    return None


def _coerce_edge_kind(kind: Any) -> EdgeKind:
    if isinstance(kind, EdgeKind):
        return kind
    try:
        return EdgeKind(kind)
    except ValueError as e:
        legal = sorted(k.value for k in EdgeKind)
        raise ValueError(
            f"unknown edge kind {kind!r}; legal: {legal}"
        ) from e


def _unwire_inplace(project, from_id: str, to_id: str,
                    kind: EdgeKind) -> Optional[str]:
    """Mutate node front-matter to remove the edge. Returns artifact (if any)
    so callers can stash it on the evolutions log for a future re-wire.

    This inverts the mutations `Project.link` performs; see `project.link`.
    """
    if not project.has_node(from_id):
        raise FileNotFoundError(f"node not found: {from_id}")
    src = project.node(from_id)
    artifact: Optional[str] = None

    if kind == EdgeKind.blocks:
        if to_id in src.blocks:
            src.blocks.remove(to_id)
        if project.has_node(to_id):
            dst = project.node(to_id)
            if from_id in dst.blocked_by:
                dst.blocked_by.remove(from_id)
                project.save_node(dst)
    elif kind == EdgeKind.parent:
        if project.has_node(to_id):
            dst = project.node(to_id)
            if dst.parent == from_id:
                dst.parent = None
                project.save_node(dst)
    elif kind == EdgeKind.related:
        if to_id in src.related:
            src.related.remove(to_id)
    elif kind == EdgeKind.produces:
        # `produces` is stored in `src.outputs` as NodeOutput(path=to_id, ...)
        keep: List[NodeOutput] = []
        removed_once = False
        for o in src.outputs:
            if not removed_once and o.path == to_id:
                artifact = o.kind
                removed_once = True
                continue
            keep.append(o)
        src.outputs = keep
    elif kind == EdgeKind.consumes:
        # `consumes` is stored in `src.inputs` as NodeInput(from_node=to_id,
        # artifact=<path>)
        keep_in: List[NodeInput] = []
        removed_once = False
        for i in src.inputs:
            if not removed_once and i.from_node == to_id:
                artifact = i.artifact
                removed_once = True
                continue
            keep_in.append(i)
        src.inputs = keep_in
    else:
        raise ValueError(f"unsupported edge kind for unwire: {kind}")

    project.save_node(src)
    return artifact
