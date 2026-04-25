"""Wave scheduler + critical-path computation.

Scheduler is pure — given the graph, produce a plan. The orchestrator
Runner consumes the plan and invokes processors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from taskflow.model import Node, NodeStatus, TERMINAL_STATUSES
from taskflow.project import Project


@dataclass
class Wave:
    n: int
    nodes: List[str]


@dataclass
class Plan:
    waves: List[Wave] = field(default_factory=list)
    critical_path: List[str] = field(default_factory=list)
    # Nodes skipped because they're already terminal (done/archived/cancelled)
    already_done: List[str] = field(default_factory=list)
    # Nodes excluded because their deps can't be satisfied (missing upstream, cycle)
    excluded: List[str] = field(default_factory=list)
    # Max parallelism in any single wave
    max_width: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "waves": [{"n": w.n, "nodes": w.nodes} for w in self.waves],
            "critical_path": self.critical_path,
            "already_done": self.already_done,
            "excluded": self.excluded,
            "max_width": self.max_width,
            "depth": len(self.waves),
        }


class Scheduler:
    def __init__(self, project: Project) -> None:
        self.project = project

    def plan(self, *, max_parallel: Optional[int] = None) -> Plan:
        mp = max_parallel or self.project.cfg.orchestrator.max_parallel
        all_nodes = {n.id: n for n in self.project.all_nodes()}

        # Bucket: terminal vs schedulable
        terminal: Set[str] = set()
        schedulable: Dict[str, Node] = {}
        excluded: List[str] = []
        for nid, node in all_nodes.items():
            s = node.status if isinstance(node.status, NodeStatus) else NodeStatus(node.status)
            if s in TERMINAL_STATUSES:
                terminal.add(nid)
            else:
                schedulable[nid] = node

        # Build effective upstream sets = blocked_by ∪ {i.from_node}
        upstream: Dict[str, Set[str]] = {}
        for nid, node in schedulable.items():
            ups = set(node.blocked_by)
            for inp in node.inputs:
                if inp.from_node:
                    ups.add(inp.from_node)
            # Drop upstreams that don't exist (cycle detector elsewhere).
            ups = {u for u in ups if u in all_nodes}
            upstream[nid] = ups

        # Wave assignment via Kahn-ish layering: wave N = nodes whose deps are in 0..N-1 ∪ terminal
        remaining = dict(upstream)
        placed: Dict[str, int] = {nid: -1 for nid in terminal}    # terminals at "wave -1"
        waves: List[List[str]] = []

        while remaining:
            this_wave: List[str] = []
            for nid, ups in list(remaining.items()):
                if all((u in terminal) or (u in placed and placed[u] >= 0) for u in ups):
                    this_wave.append(nid)
            if not this_wave:
                # Cycle or unsatisfiable deps — bail, mark excluded
                for nid in remaining:
                    excluded.append(nid)
                break
            # enforce max_parallel
            this_wave.sort(key=lambda x: (schedulable[x].priority, x))
            wave_n = len(waves)
            capped = this_wave[:mp] if mp and mp > 0 else this_wave
            for nid in capped:
                placed[nid] = wave_n
                remaining.pop(nid, None)
            waves.append(capped)
            # Nodes not capped this round become candidates next round (their deps are
            # now satisfied but they were held by capacity). Leave them in `remaining`.

        # Critical path: longest chain through scheduled nodes
        critical = _critical_path(placed, upstream)

        plan = Plan(
            waves=[Wave(i, nodes) for i, nodes in enumerate(waves)],
            critical_path=critical,
            already_done=sorted(terminal),
            excluded=sorted(excluded),
            max_width=max((len(w) for w in waves), default=0),
        )
        return plan


def _critical_path(placed: Dict[str, int], upstream: Dict[str, Set[str]]) -> List[str]:
    """Compute longest path through scheduled (non-terminal) nodes by wave."""
    scheduled = {nid: w for nid, w in placed.items() if w >= 0}
    if not scheduled:
        return []

    # DP: longest path ending at each scheduled node
    best_prev: Dict[str, Optional[str]] = {nid: None for nid in scheduled}
    best_len: Dict[str, int] = {nid: 1 for nid in scheduled}
    # iterate in wave order
    for nid in sorted(scheduled, key=lambda x: scheduled[x]):
        for u in upstream.get(nid, set()):
            if u in scheduled and best_len[u] + 1 > best_len[nid]:
                best_len[nid] = best_len[u] + 1
                best_prev[nid] = u

    # find max
    end = max(best_len, key=lambda x: best_len[x])
    path: List[str] = []
    cur: Optional[str] = end
    while cur:
        path.append(cur)
        cur = best_prev[cur]
    return list(reversed(path))
