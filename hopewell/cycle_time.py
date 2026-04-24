"""Cycle-time + rework segmentation + quality + queue-staleness (HW-0038).

Computes per-item cycle time broken into:

* **total**   — `node.create` -> latest `node.status.change` to `done`
                (falls back to now if open).
* **active**  — time spent at executors classified `active` (AI clock).
                Further split into **first-pass** (first visit to that
                executor by the item) and **rework** (subsequent visits
                to an executor the item has left before).
* **wait**    — time spent at executors classified `wait` (human gates,
                approvals, queues, CI, services, human agents).
* Inbox dwell (pre-`flow.enter`) is EXCLUDED entirely — orchestration
  concern, not cycle-time.

Classification — hybrid:

1. **Explicit override wins.** An executor's
   `component_data["executor"]["time_class"]` (or, as a fallback, a
   top-level `extras["time_class"]`) of `"active" | "wait" |
   "transient"` overrides anything else.
   `"transient"` buckets the duration away from both active and wait
   (still counted in total — it's just not attributed).

2. **Auto-default from components:**
   * has `agent` AND NOT `human` extra/tag  -> **active**
   * has `human` (as a component OR in the `agent` component_data
     with `kind: human`)                     -> **wait**
   * has any of {`gate`, `approval-gate`, `queue`, `ci-pipeline`,
     `deployment-target`, `service`}        -> **wait**
   * otherwise                               -> **wait** (safe default;
     anything weird shouldn't accidentally claim agent-active clock)

`approval-gate`, `ci-pipeline`, `deployment-target`, `human` are NOT
in the built-in executor-component registry today. We treat them as
soft hints — if a project declares them as custom components (via
`.hopewell/network/components/*.json`) they'll classify as wait. If
an executor simply carries `"approval-gate"` in its components list
without registration, we still respect it for classification.

Quality:

    rework_ratio = rework_active / (first_pass_active + rework_active)

per executor, across all work items (optionally `--since` filtered by
node `updated`). Executors with no active time are omitted.

Queue-staleness:

    For every executor with the `queue` component, compute
    max(now - pushed_at) across currently-pending pushes (via
    `hopewell.flow.inbox`). If >= threshold (default 24h, overridable
    per-queue via component_data["queue"]["stale_after"]), flag it.

Stdlib only.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from hopewell import events as events_mod
from hopewell import flow as flow_mod
from hopewell import network as network_mod
from hopewell.executor import Executor
from hopewell.model import Node, NodeStatus


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------


_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
)


def _parse_ts(ts) -> Optional[datetime.datetime]:
    if ts is None or ts == "":
        return None
    # YAML may hand us a datetime.datetime already (naive or aware).
    if isinstance(ts, datetime.datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=datetime.timezone.utc)
        return ts
    if isinstance(ts, datetime.date):
        return datetime.datetime(ts.year, ts.month, ts.day,
                                 tzinfo=datetime.timezone.utc)
    if not isinstance(ts, str):
        return None
    for fmt in _ISO_FORMATS:
        try:
            return datetime.datetime.strptime(ts, fmt).replace(
                tzinfo=datetime.timezone.utc
            )
        except ValueError:
            continue
    # Try fromisoformat as last resort (handles "+00:00" etc.)
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _delta_seconds(a: datetime.datetime, b: datetime.datetime) -> float:
    return max(0.0, (b - a).total_seconds())


def format_duration(seconds: float) -> str:
    """Readable short form — 14d 3h, 2d 7h, 9h, 42m, 18s.

    Picks the two most-significant nonzero units, largest first. Zero
    renders as `0s` so callers never see an empty string.
    """
    if seconds is None or seconds < 0:
        seconds = 0
    s = int(round(seconds))
    if s == 0:
        return "0s"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: List[Tuple[int, str]] = []
    if days:
        parts.append((days, "d"))
    if hours:
        parts.append((hours, "h"))
    if minutes:
        parts.append((minutes, "m"))
    if secs:
        parts.append((secs, "s"))
    # Keep top two units.
    parts = parts[:2]
    return " ".join(f"{v}{u}" for v, u in parts)


def parse_duration(text: str) -> float:
    """Parse "24h", "30m", "2d", "90s", "1d 2h" -> seconds.

    Used for CLI `--threshold` on queue-staleness. Stdlib only; we
    don't need ISO 8601 duration support here.
    """
    if text is None:
        raise ValueError("duration is required")
    t = text.strip().lower()
    if not t:
        raise ValueError("empty duration")
    # Bare number -> seconds
    try:
        return float(t)
    except ValueError:
        pass
    total = 0.0
    num = ""
    for ch in t:
        if ch.isdigit() or ch == ".":
            num += ch
            continue
        if ch.isspace():
            continue
        if not num:
            raise ValueError(f"malformed duration: {text!r}")
        n = float(num)
        num = ""
        if ch == "s":
            total += n
        elif ch == "m":
            total += n * 60
        elif ch == "h":
            total += n * 3600
        elif ch == "d":
            total += n * 86400
        elif ch == "w":
            total += n * 7 * 86400
        else:
            raise ValueError(f"unknown duration unit {ch!r} in {text!r}")
    if num:
        # trailing bare number — treat as seconds
        total += float(num)
    return total


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


ACTIVE = "active"
WAIT = "wait"
TRANSIENT = "transient"

_WAIT_COMPONENTS = {
    "gate",
    "approval-gate",
    "queue",
    "ci-pipeline",
    "deployment-target",
    "service",
}


def classify_executor(ex: Optional[Executor]) -> str:
    """Return ACTIVE / WAIT / TRANSIENT for an executor.

    Unknown executors (not registered in the network) default to WAIT.
    """
    if ex is None:
        return WAIT

    # 1. Explicit override — component_data.executor.time_class
    cd = ex.component_data or {}
    exec_data = cd.get("executor") or {}
    override = exec_data.get("time_class") if isinstance(exec_data, dict) else None
    if override in (ACTIVE, WAIT, TRANSIENT):
        return override
    # Also allow extras.time_class for convenience
    extras_override = (ex.extras or {}).get("time_class") if ex.extras else None
    if extras_override in (ACTIVE, WAIT, TRANSIENT):
        return extras_override

    comps = set(ex.components or [])

    # 2. wait-class components (any of these -> wait)
    if comps & _WAIT_COMPONENTS:
        return WAIT

    # 3. human executor (component "human" OR agent.kind == "human")
    if "human" in comps:
        return WAIT
    agent_data = cd.get("agent") or {}
    if isinstance(agent_data, dict) and agent_data.get("kind") == "human":
        return WAIT

    # 4. agent -> active
    if "agent" in comps:
        return ACTIVE

    # 5. safe default
    return WAIT


# ---------------------------------------------------------------------------
# per-item cycle time
# ---------------------------------------------------------------------------


def _node_done_ts(node: Node, events: List[Dict[str, Any]]) -> Optional[datetime.datetime]:
    """Timestamp the node transitioned to `done` (most recent, if any)."""
    last: Optional[datetime.datetime] = None
    for ev in events:
        if ev.get("kind") != "node.status.change":
            continue
        if ev.get("node") != node.id:
            continue
        data = ev.get("data") or {}
        if data.get("to") != "done":
            continue
        ts = _parse_ts(ev.get("ts"))
        if ts is not None:
            last = ts
    return last


def _attribute_visits(
    node: Node,
    network: network_mod.Network,
    upper_bound: datetime.datetime,
) -> Dict[str, Dict[str, Any]]:
    """Walk node.locations in chronological order, bucketing duration per
    executor.

    Returns a dict keyed by executor_id:
        {
          "class": "active" | "wait" | "transient",
          "first_pass": seconds,
          "rework": seconds,
          "visits": count,
          "open": bool,   # last visit is still active
        }
    """
    visits = sorted(
        (loc for loc in node.locations),
        key=lambda loc: (loc.entered_at or "", loc.executor_id),
    )
    per_exec: Dict[str, Dict[str, Any]] = {}
    seen_before: Set[str] = set()
    for loc in visits:
        entered = _parse_ts(loc.entered_at)
        if entered is None:
            continue
        left = _parse_ts(loc.left_at) if loc.left_at else None
        end = left if left is not None else upper_bound
        dur = _delta_seconds(entered, end)

        eid = loc.executor_id
        ex = network.executors.get(eid)
        cls = classify_executor(ex)
        bucket = per_exec.setdefault(eid, {
            "class": cls,
            "first_pass": 0.0,
            "rework": 0.0,
            "visits": 0,
            "open": False,
        })
        # A late-discovered override on an executor with later visits
        # could in principle disagree with an earlier read; classify
        # once per executor (first visit wins) to stay stable.
        is_rework = eid in seen_before
        if is_rework:
            bucket["rework"] += dur
        else:
            bucket["first_pass"] += dur
            seen_before.add(eid)
        bucket["visits"] += 1
        if left is None:
            bucket["open"] = True
    return per_exec


def item_cycle_time(project, node_id: str) -> Dict[str, Any]:
    """Cycle-time breakdown for a single work item."""
    node = project.node(node_id)
    events = events_mod.read_all(project.events_path)
    network = network_mod.load_network(project.root)

    created = _parse_ts(node.created) or _now()
    done_ts = _node_done_ts(node, events)
    open_flag = done_ts is None
    upper = done_ts if done_ts is not None else _now()

    total = _delta_seconds(created, upper)
    per_exec = _attribute_visits(node, network, upper)

    active_first = 0.0
    active_rework = 0.0
    wait = 0.0
    transient = 0.0
    for eid, b in per_exec.items():
        if b["class"] == ACTIVE:
            active_first += b["first_pass"]
            active_rework += b["rework"]
        elif b["class"] == WAIT:
            wait += b["first_pass"] + b["rework"]
        elif b["class"] == TRANSIENT:
            transient += b["first_pass"] + b["rework"]

    active_total = active_first + active_rework
    rework_ratio = (active_rework / active_total) if active_total > 0 else 0.0

    by_executor: List[Dict[str, Any]] = []
    for eid in sorted(per_exec.keys()):
        b = per_exec[eid]
        ex_total = b["first_pass"] + b["rework"]
        per_ratio = (b["rework"] / ex_total) if ex_total > 0 else 0.0
        by_executor.append({
            "executor": eid,
            "class": b["class"],
            "total_seconds": ex_total,
            "total": format_duration(ex_total),
            "first_pass_seconds": b["first_pass"],
            "first_pass": format_duration(b["first_pass"]),
            "rework_seconds": b["rework"],
            "rework": format_duration(b["rework"]),
            "visits": b["visits"],
            "rework_ratio": per_ratio,
            "open": b["open"],
        })

    return {
        "query": "cycle-time",
        "node": node.id,
        "title": node.title,
        "status": node.status.value if isinstance(node.status, NodeStatus) else node.status,
        "open": open_flag,
        "created": node.created,
        "done_at": done_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if done_ts else None,
        "total_seconds": total,
        "total": format_duration(total),
        "active_seconds": active_total,
        "active": format_duration(active_total),
        "first_pass_seconds": active_first,
        "first_pass": format_duration(active_first),
        "rework_seconds": active_rework,
        "rework": format_duration(active_rework),
        "rework_ratio": rework_ratio,
        "wait_seconds": wait,
        "wait": format_duration(wait),
        "transient_seconds": transient,
        "transient": format_duration(transient),
        "by_executor": by_executor,
        "notes": [
            "inbox dwell excluded from cycle-time math",
            "active = agent-component executors; wait = gates + queues + approvals + human executors",
        ],
    }


# ---------------------------------------------------------------------------
# aggregate cycle time (filter + summarise)
# ---------------------------------------------------------------------------


def _node_matches_filters(
    node: Node,
    *,
    component: Optional[str],
    done_since: Optional[datetime.datetime],
    events: List[Dict[str, Any]],
) -> Tuple[bool, Optional[datetime.datetime]]:
    if component and not node.has_component(component):
        return False, None
    done_ts = _node_done_ts(node, events)
    if done_since is not None:
        if done_ts is None or done_ts < done_since:
            return False, done_ts
    return True, done_ts


def aggregate_cycle_time(
    project,
    *,
    component: Optional[str] = None,
    done_since: Optional[str] = None,
) -> Dict[str, Any]:
    """Summarise cycle time across filtered nodes.

    Returns aggregate buckets + per-executor contributions + a list of
    per-node summaries (id, total, active, wait, rework_ratio).
    """
    since_dt = _parse_ts(done_since) if done_since else None
    events = events_mod.read_all(project.events_path)
    network = network_mod.load_network(project.root)

    nodes = project.all_nodes()
    matched: List[Tuple[Node, Optional[datetime.datetime]]] = []
    for n in nodes:
        ok, dts = _node_matches_filters(n, component=component, done_since=since_dt, events=events)
        if ok:
            matched.append((n, dts))

    total_s = 0.0
    active_first_s = 0.0
    active_rework_s = 0.0
    wait_s = 0.0
    transient_s = 0.0

    per_executor: Dict[str, Dict[str, Any]] = {}
    per_node: List[Dict[str, Any]] = []

    for node, done_ts in matched:
        created = _parse_ts(node.created) or _now()
        upper = done_ts if done_ts is not None else _now()
        total_s += _delta_seconds(created, upper)
        per_exec = _attribute_visits(node, network, upper)

        n_active_first = 0.0
        n_active_rework = 0.0
        n_wait = 0.0
        for eid, b in per_exec.items():
            agg = per_executor.setdefault(eid, {
                "class": b["class"],
                "first_pass_seconds": 0.0,
                "rework_seconds": 0.0,
                "visits": 0,
                "nodes": 0,
            })
            agg["first_pass_seconds"] += b["first_pass"]
            agg["rework_seconds"] += b["rework"]
            agg["visits"] += b["visits"]
            agg["nodes"] += 1
            if b["class"] == ACTIVE:
                active_first_s += b["first_pass"]
                active_rework_s += b["rework"]
                n_active_first += b["first_pass"]
                n_active_rework += b["rework"]
            elif b["class"] == WAIT:
                wait_s += b["first_pass"] + b["rework"]
                n_wait += b["first_pass"] + b["rework"]
            elif b["class"] == TRANSIENT:
                transient_s += b["first_pass"] + b["rework"]

        n_active = n_active_first + n_active_rework
        per_node.append({
            "id": node.id,
            "title": node.title,
            "status": node.status.value if isinstance(node.status, NodeStatus) else node.status,
            "open": done_ts is None,
            "total_seconds": _delta_seconds(created, upper),
            "total": format_duration(_delta_seconds(created, upper)),
            "active_seconds": n_active,
            "active": format_duration(n_active),
            "wait_seconds": n_wait,
            "wait": format_duration(n_wait),
            "rework_ratio": (n_active_rework / n_active) if n_active > 0 else 0.0,
        })

    executors_out: List[Dict[str, Any]] = []
    for eid in sorted(per_executor.keys()):
        agg = per_executor[eid]
        ex_total = agg["first_pass_seconds"] + agg["rework_seconds"]
        per_ratio = (agg["rework_seconds"] / ex_total) if ex_total > 0 else 0.0
        executors_out.append({
            "executor": eid,
            "class": agg["class"],
            "total_seconds": ex_total,
            "total": format_duration(ex_total),
            "first_pass_seconds": agg["first_pass_seconds"],
            "first_pass": format_duration(agg["first_pass_seconds"]),
            "rework_seconds": agg["rework_seconds"],
            "rework": format_duration(agg["rework_seconds"]),
            "visits": agg["visits"],
            "nodes": agg["nodes"],
            "rework_ratio": per_ratio,
        })

    active_total = active_first_s + active_rework_s
    rework_ratio = (active_rework_s / active_total) if active_total > 0 else 0.0

    return {
        "query": "cycle-time",
        "filters": {"component": component, "done_since": done_since},
        "count": len(matched),
        "total_seconds": total_s,
        "total": format_duration(total_s),
        "active_seconds": active_total,
        "active": format_duration(active_total),
        "first_pass_seconds": active_first_s,
        "first_pass": format_duration(active_first_s),
        "rework_seconds": active_rework_s,
        "rework": format_duration(active_rework_s),
        "rework_ratio": rework_ratio,
        "wait_seconds": wait_s,
        "wait": format_duration(wait_s),
        "transient_seconds": transient_s,
        "transient": format_duration(transient_s),
        "by_executor": executors_out,
        "nodes": per_node,
    }


# ---------------------------------------------------------------------------
# quality — rework_ratio per executor
# ---------------------------------------------------------------------------


def _node_updated_after(node: Node, since: Optional[datetime.datetime]) -> bool:
    if since is None:
        return True
    upd = _parse_ts(node.updated)
    if upd is None:
        return True
    return upd >= since


def quality(
    project,
    executor_id: Optional[str] = None,
    *,
    since: Optional[str] = None,
    all_executors: bool = False,
) -> Dict[str, Any]:
    """Per-executor rework ratio.

    Either provide `executor_id` for a single row, or set
    `all_executors=True` to get every executor that saw active time.
    """
    since_dt = _parse_ts(since) if since else None
    network = network_mod.load_network(project.root)

    # Aggregate over matching nodes
    agg: Dict[str, Dict[str, Any]] = {}
    for node in project.all_nodes():
        if not _node_updated_after(node, since_dt):
            continue
        upper = _parse_ts(node.updated) or _now()
        # If node isn't done, use now for open visits. If it is done,
        # fall back to the node's `updated` as upper bound (close
        # enough; avoids synthesising status events we don't have).
        per_exec = _attribute_visits(node, network, upper)
        for eid, b in per_exec.items():
            bucket = agg.setdefault(eid, {
                "class": b["class"],
                "first_pass_seconds": 0.0,
                "rework_seconds": 0.0,
                "visits": 0,
                "nodes": set(),
            })
            bucket["first_pass_seconds"] += b["first_pass"]
            bucket["rework_seconds"] += b["rework"]
            bucket["visits"] += b["visits"]
            bucket["nodes"].add(node.id)

    def _row(eid: str, b: Dict[str, Any]) -> Dict[str, Any]:
        total_s = b["first_pass_seconds"] + b["rework_seconds"]
        ratio = (b["rework_seconds"] / total_s) if total_s > 0 else 0.0
        return {
            "executor": eid,
            "class": b["class"],
            "first_pass_seconds": b["first_pass_seconds"],
            "first_pass": format_duration(b["first_pass_seconds"]),
            "rework_seconds": b["rework_seconds"],
            "rework": format_duration(b["rework_seconds"]),
            "total_seconds": total_s,
            "total": format_duration(total_s),
            "rework_ratio": ratio,
            "visits": b["visits"],
            "nodes": len(b["nodes"]),
        }

    if all_executors:
        rows = [
            _row(eid, b)
            for eid, b in sorted(agg.items())
            if b["class"] == ACTIVE and (b["first_pass_seconds"] + b["rework_seconds"]) > 0
        ]
        return {
            "query": "quality",
            "filters": {"since": since, "scope": "all"},
            "count": len(rows),
            "executors": rows,
        }

    if not executor_id:
        return {
            "query": "quality",
            "error": "executor_id required (or pass all_executors=True)",
        }

    b = agg.get(executor_id)
    if b is None:
        return {
            "query": "quality",
            "filters": {"executor": executor_id, "since": since},
            "found": False,
            "message": f"no active-time observations for {executor_id!r}",
        }
    row = _row(executor_id, b)
    row["found"] = True
    row["filters"] = {"executor": executor_id, "since": since}
    row["query"] = "quality"
    return row


# ---------------------------------------------------------------------------
# queue staleness
# ---------------------------------------------------------------------------


def _queue_threshold(ex: Executor, default_seconds: float) -> float:
    cd = ex.component_data or {}
    q = cd.get("queue") or {}
    if isinstance(q, dict):
        raw = q.get("stale_after")
        if isinstance(raw, str):
            try:
                return parse_duration(raw)
            except ValueError:
                return default_seconds
        if isinstance(raw, (int, float)):
            return float(raw)
    return default_seconds


def queue_staleness(
    project,
    *,
    threshold: Optional[str] = None,
) -> Dict[str, Any]:
    """For every `queue`-component executor, report max pending age.

    Separate from cycle-time math: purely a health signal. An idle
    queue with nothing pending isn't "stale" in this model (no packets,
    no problem) — we flag it with `pending: 0`.
    """
    default = parse_duration(threshold) if threshold else 24 * 3600.0
    network = network_mod.load_network(project.root)
    now = _now()

    queues: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    for eid, ex in sorted(network.executors.items()):
        if not ex.has_component("queue"):
            continue
        thr = _queue_threshold(ex, default)
        pending = flow_mod.inbox(project, eid)
        oldest_ts: Optional[str] = None
        oldest_age = 0.0
        for entry in pending:
            pt = _parse_ts(entry.get("pushed_at"))
            if pt is None:
                continue
            age = _delta_seconds(pt, now)
            if age > oldest_age:
                oldest_age = age
                oldest_ts = entry.get("pushed_at")
        row = {
            "executor": eid,
            "pending": len(pending),
            "oldest_pushed_at": oldest_ts,
            "oldest_age_seconds": oldest_age,
            "oldest_age": format_duration(oldest_age),
            "threshold_seconds": thr,
            "threshold": format_duration(thr),
            "stale": oldest_age >= thr and len(pending) > 0,
        }
        queues.append(row)
        if row["stale"]:
            stale.append(row)

    return {
        "query": "queue-staleness",
        "threshold_default_seconds": default,
        "threshold_default": format_duration(default),
        "count": len(queues),
        "stale_count": len(stale),
        "queues": queues,
        "stale": stale,
    }
