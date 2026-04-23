"""Read-only query API. CLI subcommands delegate to these; scripts import them.

Every query returns a plain JSON-serialisable dict.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from hopewell.model import Node, NodeStatus, TERMINAL_STATUSES
from hopewell.project import Project


# ---------------------------------------------------------------------------
# systems / list
# ---------------------------------------------------------------------------


def list_nodes(project: Project, *, status: Optional[str] = None,
               component: Optional[str] = None, has_all: Optional[List[str]] = None,
               owner: Optional[str] = None) -> Dict[str, Any]:
    nodes = project.all_nodes()
    if status:
        nodes = [n for n in nodes if (n.status.value if isinstance(n.status, NodeStatus) else n.status) == status]
    if component:
        nodes = [n for n in nodes if n.has_component(component)]
    if has_all:
        nodes = [n for n in nodes if n.has_all(has_all)]
    if owner:
        nodes = [n for n in nodes if n.owner == owner]
    return {
        "query": "list",
        "filters": {"status": status, "component": component, "has_all": has_all, "owner": owner},
        "count": len(nodes),
        "nodes": [_node_summary(n) for n in nodes],
    }


def show(project: Project, node_id: str) -> Dict[str, Any]:
    node = project.node(node_id)
    return {
        "query": "show",
        "node": _node_full(node),
    }


# ---------------------------------------------------------------------------
# ready — nodes whose inputs are all satisfied
# ---------------------------------------------------------------------------


def ready(project: Project, *, owner: Optional[str] = None,
          include_claimed: bool = False) -> Dict[str, Any]:
    by_id = {n.id: n for n in project.all_nodes()}

    # v0.5: filter out nodes that are currently claimed (remote branch + unreleased local claim).
    claimed_ids: set = set()
    if not include_claimed:
        try:
            from hopewell import claim as claim_mod
            for c in claim_mod.query_claims(project):
                claimed_ids.add(c.node_id)
        except Exception:
            pass

    out: List[Node] = []
    for n in by_id.values():
        s = n.status if isinstance(n.status, NodeStatus) else NodeStatus(n.status)
        if s in TERMINAL_STATUSES or s == NodeStatus.doing:
            continue
        if owner and n.owner != owner:
            continue
        if n.id in claimed_ids:
            continue
        if _all_blockers_done(n, by_id) and _all_inputs_satisfied(n, by_id):
            out.append(n)
    return {
        "query": "ready",
        "filters": {"owner": owner, "include_claimed": include_claimed},
        "count": len(out),
        "excluded_claimed": sorted(claimed_ids),
        "nodes": [_node_summary(n) for n in sorted(out, key=lambda x: (x.priority, x.id))],
    }


def _all_blockers_done(n: Node, by_id: Dict[str, Node]) -> bool:
    for bid in n.blocked_by:
        b = by_id.get(bid)
        if not b:
            return False
        bs = b.status if isinstance(b.status, NodeStatus) else NodeStatus(b.status)
        if bs not in TERMINAL_STATUSES:
            return False
    return True


def _all_inputs_satisfied(n: Node, by_id: Dict[str, Node]) -> bool:
    for i in n.inputs:
        if not i.required:
            continue
        if i.from_node:
            up = by_id.get(i.from_node)
            if not up:
                return False
            us = up.status if isinstance(up.status, NodeStatus) else NodeStatus(up.status)
            if us != NodeStatus.done:
                return False
    return True


# ---------------------------------------------------------------------------
# deps — forward + reverse (+ transitive)
# ---------------------------------------------------------------------------


def deps(project: Project, node_id: str, *, transitive: bool = False) -> Dict[str, Any]:
    by_id = {n.id: n for n in project.all_nodes()}
    if node_id not in by_id:
        return {"query": "deps", "target": node_id, "found": False,
                "known": sorted(by_id.keys())}
    target = by_id[node_id]

    if transitive:
        blocks_reach = _reachable(by_id, node_id, lambda nd: nd.blocks)
        blocked_by_reach = _reachable(by_id, node_id, lambda nd: nd.blocked_by)
    else:
        blocks_reach = set(target.blocks)
        blocked_by_reach = set(target.blocked_by)

    return {
        "query": "deps",
        "target": node_id,
        "found": True,
        "transitive": transitive,
        "blocks": sorted(blocks_reach),
        "blocked_by": sorted(blocked_by_reach),
        "consumes_from": sorted({i.from_node for i in target.inputs if i.from_node}),
        "produces": [{"path": o.path, "kind": o.kind, "signal": o.signal} for o in target.outputs],
    }


def _reachable(by_id: Dict[str, Node], start: str, next_fn) -> Set[str]:
    visited: Set[str] = set()
    stack = [start]
    while stack:
        nid = stack.pop()
        for m in next_fn(by_id.get(nid)) if by_id.get(nid) else []:
            if m not in visited:
                visited.add(m)
                stack.append(m)
    visited.discard(start)
    return visited


# ---------------------------------------------------------------------------
# waves + critical path (scheduler read-only views)
# ---------------------------------------------------------------------------


def waves(project: Project) -> Dict[str, Any]:
    from hopewell.scheduler import Scheduler
    plan = Scheduler(project).plan()
    return {
        "query": "waves",
        "stack": plan.to_dict(),
    }


def critical_path(project: Project) -> Dict[str, Any]:
    from hopewell.scheduler import Scheduler
    plan = Scheduler(project).plan()
    return {
        "query": "critical-path",
        "path": plan.critical_path,
        "depth": len(plan.waves),
    }


# ---------------------------------------------------------------------------
# metrics — component distribution, status counts, owner loads
# ---------------------------------------------------------------------------


def metrics(project: Project, *, by: str = "component") -> Dict[str, Any]:
    nodes = project.all_nodes()
    result: Dict[str, Dict[str, int]] = {}
    if by == "component":
        for n in nodes:
            for c in n.components:
                result.setdefault(c, {}).setdefault(_status_str(n.status), 0)
                result[c][_status_str(n.status)] += 1
    elif by == "status":
        for n in nodes:
            result.setdefault(_status_str(n.status), {"count": 0})["count"] += 1
    elif by == "owner":
        for n in nodes:
            key = n.owner or "(unassigned)"
            result.setdefault(key, {}).setdefault(_status_str(n.status), 0)
            result[key][_status_str(n.status)] += 1
    else:
        return {"query": "metrics", "error": f"unknown --by: {by!r}"}
    return {
        "query": "metrics",
        "by": by,
        "total_nodes": len(nodes),
        "breakdown": result,
    }


def _status_str(s) -> str:
    return s.value if isinstance(s, NodeStatus) else s


# ---------------------------------------------------------------------------
# component listing
# ---------------------------------------------------------------------------


def component_nodes(project: Project, component: str) -> Dict[str, Any]:
    nodes = [n for n in project.all_nodes() if n.has_component(component)]
    return {
        "query": "component",
        "component": component,
        "count": len(nodes),
        "nodes": [_node_summary(n) for n in nodes],
    }


# ---------------------------------------------------------------------------
# full graph export (for web UI later; useful now too)
# ---------------------------------------------------------------------------


def claims(project: Project, node_id: Optional[str] = None) -> Dict[str, Any]:
    """Return active claims (remote branches + unreleased local claims)."""
    from hopewell import claim as claim_mod
    active = claim_mod.query_claims(project, node_id=node_id)
    return {
        "query": "claims",
        "filters": {"node_id": node_id},
        "count": len(active),
        "claims": [c.to_dict() for c in active],
    }


def graph(project: Project) -> Dict[str, Any]:
    nodes = project.all_nodes()
    edges: List[Dict[str, Any]] = []
    for n in nodes:
        for b in n.blocks:
            edges.append({"from": n.id, "to": b, "kind": "blocks"})
        for r in n.related:
            edges.append({"from": n.id, "to": r, "kind": "related"})
        for i in n.inputs:
            if i.from_node:
                edges.append({"from": i.from_node, "to": n.id, "kind": "consumes",
                              "artifact": i.artifact})
        if n.parent:
            edges.append({"from": n.parent, "to": n.id, "kind": "parent"})
    return {
        "query": "graph",
        "nodes": [_node_summary(n) for n in nodes],
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# helpers: summary vs full
# ---------------------------------------------------------------------------


def _node_summary(n: Node) -> Dict[str, Any]:
    return {
        "id": n.id,
        "title": n.title,
        "status": _status_str(n.status),
        "priority": n.priority,
        "owner": n.owner,
        "components": list(n.components),
        "blocks": list(n.blocks),
        "blocked_by": list(n.blocked_by),
        "parent": n.parent,
    }


def _node_full(n: Node) -> Dict[str, Any]:
    s = _node_summary(n)
    s.update({
        "project": n.project,
        "created": n.created,
        "updated": n.updated,
        "inputs": [
            {"from_node": i.from_node, "artifact": i.artifact, "kind": i.kind,
             "description": i.description, "required": i.required}
            for i in n.inputs
        ],
        "outputs": [
            {"path": o.path, "kind": o.kind, "signal": o.signal}
            for o in n.outputs
        ],
        "related": list(n.related),
        "component_data": dict(n.component_data),
        "body": n.body,
        "notes": list(n.notes),
    })
    return s
