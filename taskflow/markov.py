"""Markov / rework analytics (HW-0036).

Aggregates per-edge transition probabilities across all work items'
observed traversals of the flow network. For every directed edge
`(A -> B)` we compute::

    P(B | leaving A) = count(A->B) / count(total departures from A)

plus raw counts, mean dwell at source, and a forward-vs-back
classification used to highlight rework loops in the UI.

Semantics
---------

**Transitions come from `Node.locations`.** Each work-item node stores
an ordered list of `(executor_id, entered_at, left_at)` records. We
sort by `entered_at` and read successive pairs as transitions:
`(loc[i], loc[i+1])` means "the item left loc[i] and next appeared at
loc[i+1]". This matches reality better than reading `flow.push` events
directly — pushes without a matching enter are "offered but never
accepted" noise, and we want the actual path walked.

**Base rate (single-traversal items).** Items with only one
`locations` entry contribute zero transitions but ARE counted in
`total_items` so the UI can render "12 items total, 4 rework events"
alongside the graph. This keeps the probability view honest when most
items do happy-path one-shots.

**Time window.** Each transition's timestamp is `loc[i].left_at` (with
`entered_at` of loc[i+1] as fallback). Filtering:

* ``all``         — no filter
* ``30d``         — transition_ts >= now - 30d
* ``release-tag`` — transition_ts >= the committer-date of the most
                    recent git tag matching `v*` (falls back to `all`
                    if no such tag exists or git is unavailable)

Named windows accept an optional suffix after a slash, e.g.
``30d/include_open`` — ignored today; reserved for future knobs.

**Forward vs back classification.**

The declared network is the source of truth for "intended" direction:

1. **Route-condition hint wins.** A declared route whose `condition`
   or `label` carries a rework keyword (`on_fail`, `on_reject`,
   `rework`, `reopen`, `retry`, `fix`) is BACK. This correctly
   handles the common `code-review -> @architect [on_fail]` edge.
2. **Any other declared route is FORWARD.** Even if it closes a loop
   in the network (e.g. `prod-deploy -> @product-manager` for live-ops
   telemetry), the declaration itself states "this is intended flow"
   — we trust the designer. This mirrors how people look at a DAG with
   explicit feedback links and still distinguish "happy path feedback"
   from "rework".
3. **Undeclared observed edges** — someone pushed a work item along a
   route the architect didn't draw — fall back to the topological-SCC
   hint computed from forward-only declared routes, and if that's
   also silent (endpoint not in the declared graph at all), to a
   "first-seen on this item" heuristic: if at the moment of the
   transition, `B` has already been visited by this item, it's back.
   We record per-edge "backness" as the MAJORITY across its
   observations plus a `classification_confidence` (share of
   majority-label observations).

**Mean dwell at source.** For each transition `(A -> B)` we know the
dwell at A = `loc_A.left_at - loc_A.entered_at`. We aggregate mean
dwell per (A -> B) AND per source-A over all its outgoing edges. The
time-weighted overlay multiplies `probability * mean_dwell_seconds(A)`
to surface "expensive loops" rather than just "frequent loops".

Stdlib only.
"""
from __future__ import annotations

import datetime
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from taskflow import network as network_mod
from taskflow.cycle_time import _parse_ts, _now, _delta_seconds, format_duration


# ---------------------------------------------------------------------------
# window resolution
# ---------------------------------------------------------------------------


def resolve_window(
    window: str,
    *,
    project_root: Optional[Path] = None,
    now: Optional[datetime.datetime] = None,
) -> Tuple[Optional[datetime.datetime], str]:
    """Translate a window spec into (since_dt, resolved_label).

    `resolved_label` names the actual cutoff used — useful when
    `release-tag` is requested but no tag exists and we fall back to
    `all`. Returned `since_dt` is None for `all` and for unrecognised
    windows (graceful-degrade).
    """
    if not window or window == "all":
        return None, "all"

    now = now or _now()

    # 30d, 7d, 1d, 12h — optional extension point; keep it simple now.
    if window == "30d":
        return now - datetime.timedelta(days=30), "30d"
    if window == "7d":
        return now - datetime.timedelta(days=7), "7d"
    if window == "1d":
        return now - datetime.timedelta(days=1), "1d"

    if window == "release-tag":
        ts = _latest_release_tag_time(project_root)
        if ts is None:
            return None, "all(no-release-tag)"
        return ts, "release-tag"

    # Unknown window spec — be permissive, mirror cycle_time.py style.
    return None, f"all({window})"


def _latest_release_tag_time(project_root: Optional[Path]) -> Optional[datetime.datetime]:
    """Return the committer-date of the most recent `v*` tag, or None."""
    root = project_root if project_root is not None else Path.cwd()
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--sort=-committerdate",
             "--format=%(committerdate:iso-strict)", "refs/tags/v*"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    first = (out.stdout or "").strip().splitlines()
    if not first:
        return None
    return _parse_ts(first[0].replace(" ", "T"))


# ---------------------------------------------------------------------------
# topological classification (forward vs back)
# ---------------------------------------------------------------------------


def _tarjan_sccs(nodes: Iterable[str], edges: Iterable[Tuple[str, str]]) -> Dict[str, int]:
    """Return a mapping node_id -> scc_id. Iterative Tarjan (avoid recursion
    limits on large networks)."""
    adj: Dict[str, List[str]] = defaultdict(list)
    node_set = set(nodes)
    for a, b in edges:
        if a in node_set and b in node_set:
            adj[a].append(b)

    index_of: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    scc_of: Dict[str, int] = {}
    counter = [0]
    scc_counter = [0]

    for start in sorted(node_set):
        if start in index_of:
            continue
        # Iterative DFS
        work: List[Tuple[str, int]] = [(start, 0)]
        call_stack: List[str] = []
        while work:
            v, i = work[-1]
            if i == 0:
                index_of[v] = counter[0]
                lowlink[v] = counter[0]
                counter[0] += 1
                stack.append(v)
                on_stack.add(v)
                call_stack.append(v)
            nbrs = adj[v]
            if i < len(nbrs):
                work[-1] = (v, i + 1)
                w = nbrs[i]
                if w not in index_of:
                    work.append((w, 0))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index_of[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
                if lowlink[v] == index_of[v]:
                    # Emit SCC
                    scc_id = scc_counter[0]
                    scc_counter[0] += 1
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        scc_of[w] = scc_id
                        if w == v:
                            break
                call_stack.pop() if call_stack and call_stack[-1] == v else None
    return scc_of


def _scc_topo_rank(
    scc_of: Dict[str, int],
    edges: Iterable[Tuple[str, str]],
) -> Dict[int, int]:
    """Topologically rank the SCC condensation. Lower rank = earlier."""
    sccs = set(scc_of.values())
    condensed: Dict[int, Set[int]] = {s: set() for s in sccs}
    indeg: Dict[int, int] = {s: 0 for s in sccs}
    for a, b in edges:
        if a not in scc_of or b not in scc_of:
            continue
        sa, sb = scc_of[a], scc_of[b]
        if sa == sb:
            continue
        if sb not in condensed[sa]:
            condensed[sa].add(sb)
            indeg[sb] += 1

    rank: Dict[int, int] = {}
    frontier: List[int] = sorted(s for s, d in indeg.items() if d == 0)
    cur = 0
    while frontier:
        next_frontier: List[int] = []
        for s in frontier:
            rank[s] = cur
            for t in sorted(condensed[s]):
                indeg[t] -= 1
                if indeg[t] == 0:
                    next_frontier.append(t)
        frontier = next_frontier
        cur += 1
    # Any nodes not ranked (shouldn't happen for a DAG of SCCs) get max+1.
    for s in sccs:
        rank.setdefault(s, cur)
    return rank


_REWORK_KEYWORDS = (
    "on_fail", "on_reject", "rework", "reopen", "retry", "fix",
    "failure", "rejected",
)


def _is_rework_route(route) -> bool:
    """True if a declared route's condition/label marks it as explicit rework."""
    bits = []
    cond = getattr(route, "condition", None)
    if cond:
        bits.append(str(cond).lower())
    lbl = getattr(route, "label", None)
    if lbl:
        bits.append(str(lbl).lower())
    blob = " ".join(bits)
    return any(kw in blob for kw in _REWORK_KEYWORDS)


def classify_edges_topologically(
    network: network_mod.Network,
) -> Tuple[Dict[int, int], Dict[str, int], Set[Tuple[str, str]]]:
    """Return (scc_rank_by_scc_id, scc_id_by_executor, forced_back_edges).

    Uses declared FORWARD routes (those whose condition/label does not
    carry a rework keyword) to build the topology graph. Rework routes
    are surfaced separately so the caller can short-circuit their
    classification without warping the condensation.

    Caller compares `rank[scc[A]]` vs `rank[scc[B]]` — equal -> same
    SCC (back), A>B -> back, A<B -> forward. Edges in
    `forced_back_edges` skip the comparison entirely (they're back).
    """
    node_ids = list(network.executors.keys())
    forward_edges: List[Tuple[str, str]] = []
    forced_back: Set[Tuple[str, str]] = set()
    for r in network.routes:
        if _is_rework_route(r):
            forced_back.add((r.from_id, r.to_id))
        else:
            forward_edges.append((r.from_id, r.to_id))
    scc_of = _tarjan_sccs(node_ids, forward_edges)
    rank = _scc_topo_rank(scc_of, forward_edges)
    return rank, scc_of, forced_back


# ---------------------------------------------------------------------------
# transition extraction
# ---------------------------------------------------------------------------


def _item_transitions(
    node,
) -> List[Dict[str, Any]]:
    """Return chronological transitions for one work item.

    Each transition is a dict:
        {"from": eid_a, "to": eid_b,
         "ts": <str left_at-of-A or entered_at-of-B>,
         "dwell_seconds": float (at A)}

    Items with <2 locations contribute [].
    """
    locs = [loc for loc in node.locations
            if getattr(loc, "entered_at", None)]
    locs.sort(key=lambda loc: (loc.entered_at or "", loc.executor_id))
    if len(locs) < 2:
        return []
    out: List[Dict[str, Any]] = []
    for i in range(len(locs) - 1):
        a = locs[i]
        b = locs[i + 1]
        a_entered = _parse_ts(a.entered_at)
        a_left = _parse_ts(a.left_at) if getattr(a, "left_at", None) else None
        b_entered = _parse_ts(b.entered_at)
        if a_entered is None or b_entered is None:
            continue
        dwell_end = a_left if a_left is not None else b_entered
        dwell = _delta_seconds(a_entered, dwell_end)
        ts = a.left_at or b.entered_at
        out.append({
            "from": a.executor_id,
            "to": b.executor_id,
            "ts": ts,
            "dwell_seconds": dwell,
        })
    return out


# ---------------------------------------------------------------------------
# main aggregation
# ---------------------------------------------------------------------------


def compute(
    project,
    *,
    window: str = "30d",
    include_singletons: bool = True,
    now: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Aggregate per-edge transition probabilities.

    Parameters
    ----------
    window : str
        ``all`` | ``30d`` | ``7d`` | ``1d`` | ``release-tag``.
    include_singletons : bool
        If True, count single-traversal items in `total_items` (so the
        base-rate badge can read "N items / M rework events"). They
        contribute no transitions either way.
    now : datetime, optional
        Override clock for tests.

    Returns
    -------
    dict shaped for the `/api/markov` endpoint (see module docstring).
    """
    network = network_mod.load_network(project.root)

    # classification: declared routes are forward by default; routes with
    # rework-keyword conditions are back; undeclared observed edges fall
    # back to topological rank (forward-only graph) or observation votes.
    scc_rank, scc_of, forced_back = classify_edges_topologically(network)
    declared_forward: Set[Tuple[str, str]] = set()
    for r in network.routes:
        if not _is_rework_route(r):
            declared_forward.add((r.from_id, r.to_id))

    def _topo_is_back(a: str, b: str) -> Optional[bool]:
        """Return True (back), False (forward), or None (undecided).

        Rule priority:
          1. Declared rework route (condition/label contains rework kw).
          2. Declared non-rework route -> forward, always.
          3. Undeclared edge -> SCC-rank comparison if both endpoints are
             in the declared graph.
          4. Otherwise undecided (None — caller falls back to observation).
        """
        if (a, b) in forced_back:
            return True
        if (a, b) in declared_forward:
            return False
        if a not in scc_of or b not in scc_of:
            return None
        sa, sb = scc_of[a], scc_of[b]
        if sa == sb:
            return True
        return scc_rank[sb] <= scc_rank[sa]

    since_dt, resolved_window = resolve_window(
        window, project_root=project.root, now=now,
    )

    # per-edge counters
    counts: Dict[Tuple[str, str], int] = defaultdict(int)
    dwell_sum: Dict[Tuple[str, str], float] = defaultdict(float)
    # per-source total departures (for denominator)
    dep_counts: Dict[str, int] = defaultdict(int)
    dep_dwell_sum: Dict[str, float] = defaultdict(float)
    # per-edge "backness" votes — used when the declared topology is
    # silent (None from _topo_is_back). We vote via per-item revisit.
    back_votes: Dict[Tuple[str, str], int] = defaultdict(int)
    forward_votes: Dict[Tuple[str, str], int] = defaultdict(int)

    total_items = 0
    contributing_items = 0
    total_transitions = 0
    rework_events = 0
    singleton_items = 0

    for node in project.all_nodes():
        # Only count work items — otherwise epic parents skew stats.
        if not node.has_component("work-item") and not getattr(node, "locations", None):
            continue
        transitions = _item_transitions(node)
        if not transitions:
            # May still be in-flight (0 or 1 location). Count in base rate.
            if include_singletons and (node.locations or []):
                total_items += 1
                singleton_items += 1
            continue

        # Per-item: has it "contributed" after window filtering?
        kept_any = False
        seen_before_in_item: Set[str] = set()
        for t in transitions:
            trans_ts = _parse_ts(t["ts"])
            if since_dt is not None:
                if trans_ts is None or trans_ts < since_dt:
                    # Still track revisit state so later in-window transitions
                    # classify correctly.
                    seen_before_in_item.add(t["from"])
                    continue
            kept_any = True
            a, b = t["from"], t["to"]
            edge = (a, b)
            counts[edge] += 1
            dwell_sum[edge] += t["dwell_seconds"]
            dep_counts[a] += 1
            dep_dwell_sum[a] += t["dwell_seconds"]
            total_transitions += 1

            # Per-item fallback vote: is B already seen on this item?
            if b in seen_before_in_item:
                back_votes[edge] += 1
            else:
                forward_votes[edge] += 1
            seen_before_in_item.add(a)

        if kept_any:
            total_items += 1
            contributing_items += 1

    # Emit edge rows
    edges_out: List[Dict[str, Any]] = []
    for edge, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        a, b = edge
        dep_total = dep_counts.get(a) or c
        prob = c / dep_total if dep_total else 0.0
        mean_dwell = dwell_sum[edge] / c if c else 0.0

        topo = _topo_is_back(a, b)
        obs_votes = back_votes[edge] + forward_votes[edge]
        if topo is None:
            # Edge isn't declared and endpoints aren't in the declared
            # graph — vote via observations; default forward if no votes.
            is_back = back_votes[edge] > forward_votes[edge]
            confidence = (
                max(back_votes[edge], forward_votes[edge]) / obs_votes
                if obs_votes else 0.0
            )
            source = "observed"
        elif (a, b) in forced_back:
            is_back = True
            source = "declared-rework"
            confidence = 1.0
        elif (a, b) in declared_forward:
            is_back = False
            source = "declared-forward"
            confidence = 1.0
        else:
            is_back = topo
            source = "topology"
            confidence = 1.0

        if is_back:
            rework_events += c

        edges_out.append({
            "from": a,
            "to": b,
            "count": c,
            "probability": prob,
            "is_back": is_back,
            "classification_source": source,
            "classification_confidence": round(confidence, 3),
            "mean_dwell_seconds": mean_dwell,
            "mean_dwell": format_duration(mean_dwell),
            "time_weight_seconds": prob * mean_dwell,
            "time_weight": format_duration(prob * mean_dwell),
            "source_departures": dep_total,
            "declared_route": (a, b) in {(r.from_id, r.to_id) for r in network.routes},
        })

    # Per-source aggregate (used for UI sidebar — "where does work leave @engineer?")
    sources_out: List[Dict[str, Any]] = []
    for a, dep in sorted(dep_counts.items(), key=lambda kv: -kv[1]):
        mean = dep_dwell_sum[a] / dep if dep else 0.0
        sources_out.append({
            "executor": a,
            "departures": dep,
            "mean_dwell_seconds": mean,
            "mean_dwell": format_duration(mean),
        })

    return {
        "query": "markov",
        "window": resolved_window,
        "window_requested": window,
        "since": since_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if since_dt else None,
        "include_singletons": include_singletons,
        "total_items": total_items,
        "contributing_items": contributing_items,
        "singleton_items": singleton_items,
        "total_transitions": total_transitions,
        "rework_events": rework_events,
        "rework_ratio": (rework_events / total_transitions) if total_transitions else 0.0,
        "edges": edges_out,
        "sources": sources_out,
    }


def top_rework_edges(
    data: Dict[str, Any],
    *,
    n: int = 10,
    by: str = "probability",
) -> List[Dict[str, Any]]:
    """Return the top-N back-edges by probability, count, or time_weight.

    Convenience for the CLI/UI table.
    """
    keyfn = {
        "probability": lambda e: e["probability"],
        "count": lambda e: e["count"],
        "time_weight": lambda e: e["time_weight_seconds"],
    }.get(by, lambda e: e["probability"])
    backs = [e for e in data.get("edges", []) if e.get("is_back")]
    backs.sort(key=keyfn, reverse=True)
    return backs[:n]
