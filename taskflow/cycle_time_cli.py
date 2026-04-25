"""CLI handler functions for `taskflow query cycle-time / quality / queue-staleness` (HW-0038).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser / `cmd_query` dispatch after
this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success
    1 — unknown executor / unknown node / malformed input

Suggested subparser wiring for `cmd_query` in `cli.py` (add three more
`elif` branches, then extend the `sp.add_argument` block for the
`query` parser):

    # inside cmd_query(args):
    elif args.subject == "cycle-time":
        from taskflow import cycle_time_cli as ct_cli
        return ct_cli.cmd_query_cycle_time(args)
    elif args.subject == "quality":
        from taskflow import cycle_time_cli as ct_cli
        return ct_cli.cmd_query_quality(args)
    elif args.subject == "queue-staleness":
        from taskflow import cycle_time_cli as ct_cli
        return ct_cli.cmd_query_queue_staleness(args)

And extend the `query` subparser (the one already built around
cli.py:1201) with the per-subject switches:

    sp = sub.add_parser("query", help="Read-only JSON queries")
    sp.add_argument("subject", choices=[..., "cycle-time", "quality",
                                        "queue-staleness"])
    sp.add_argument("name", nargs="?",
                    help="node id (cycle-time), executor id (quality), "
                         "or unused (queue-staleness)")
    sp.add_argument("--component", default=None,
                    help="cycle-time: filter aggregated cycle-time to "
                         "nodes carrying this component")
    sp.add_argument("--done-since", default=None,
                    help="cycle-time: only include nodes done at/after "
                         "this ISO ts (e.g. 2026-04-01T00:00:00Z)")
    sp.add_argument("--since", default=None,
                    help="quality: only include work items with "
                         "updated >= ts")
    sp.add_argument("--all", dest="scope_all", action="store_true",
                    help="quality: tabulate across all executors")
    sp.add_argument("--threshold", default=None,
                    help="queue-staleness: default threshold (e.g. 24h, "
                         "30m, 2d). Per-queue overrides respected.")
    sp.add_argument("--format", choices=["text", "json"], default="json",
                    help="output format (default: json for scripts; "
                         "cycle-time/quality/queue-staleness default to "
                         "text when called without --format via our CLI)")
    sp.set_defaults(func=cmd_query)

Routing note: the cycle-time handlers default to **text** format if
the caller didn't pass `--format`, because they're meant to be read
in a terminal. The existing `cmd_query` always JSON-prints — we
bypass that by having these handlers print directly and return before
`_print_json` runs. (Cleaner long-term: pick format in `cmd_query`.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from taskflow import cycle_time as ct_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project(args):
    from taskflow.project import Project
    start = Path(args.project_root).resolve() if getattr(args, "project_root", None) else None
    return Project.load(start)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _format(args) -> str:
    # Default the three cycle-time-family subjects to text; callers can
    # still pass --format=json. (Rest of `query` defaults to json.)
    fmt = getattr(args, "format", None)
    return fmt or "text"


def _pct(x: float) -> str:
    return f"{int(round(x * 100))}%"


# ---------------------------------------------------------------------------
# query cycle-time
# ---------------------------------------------------------------------------


def cmd_query_cycle_time(args) -> int:
    """`taskflow query cycle-time [<node_id>] [--component C] [--done-since ISO]`.

    With `name` -> per-item. Without `name` -> aggregate.
    """
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    node_id = getattr(args, "name", None)
    component = getattr(args, "component", None)
    done_since = getattr(args, "done_since", None)

    try:
        if node_id:
            data = ct_mod.item_cycle_time(project, node_id)
        else:
            data = ct_mod.aggregate_cycle_time(
                project, component=component, done_since=done_since,
            )
    except FileNotFoundError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1
    except KeyError as e:
        print(f"taskflow: unknown node: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json(data)
        return 0

    if node_id:
        _print_item_cycle_time(data)
    else:
        _print_aggregate_cycle_time(data)
    return 0


def _print_item_cycle_time(d: Dict[str, Any]) -> None:
    open_bit = "  [open]" if d.get("open") else ""
    print(f"{d['node']} cycle time{open_bit}")
    print(f"  total:        {d['total']}")
    active = d["active_seconds"]
    print(f"  active:       {d['active']}")
    if active > 0:
        fp_pct = _pct(d["first_pass_seconds"] / active)
        rw_pct = _pct(d["rework_seconds"] / active)
        print(f"    first-pass: {d['first_pass']}  ({fp_pct})")
        print(f"    rework:     {d['rework']}  ({rw_pct})")
    else:
        print(f"    first-pass: 0s")
        print(f"    rework:     0s")
    print(f"  wait:         {d['wait']}   (human gates + queues + approvals; inbox dwell excluded)")
    if d.get("transient_seconds", 0) > 0:
        print(f"  transient:    {d['transient']}   (not attributed)")
    if d.get("by_executor"):
        print(f"  by executor:")
        for row in d["by_executor"]:
            cls = row["class"]
            if cls == "active":
                rr = f"   [{_pct(row['rework_ratio'])} rework]" if row["rework_seconds"] > 0 else ""
                open_bit = " [open]" if row.get("open") else ""
                print(
                    f"    {row['executor']:22} active  {row['total']:>8}  "
                    f"first-pass {row['first_pass']}, rework {row['rework']}{rr}{open_bit}"
                )
            else:
                open_bit = " [open]" if row.get("open") else ""
                print(f"    {row['executor']:22} {cls:6} {row['total']:>8}{open_bit}")


def _print_aggregate_cycle_time(d: Dict[str, Any]) -> None:
    filt = d.get("filters") or {}
    bits = []
    if filt.get("component"):
        bits.append(f"component={filt['component']}")
    if filt.get("done_since"):
        bits.append(f"done-since={filt['done_since']}")
    filt_str = f"  ({', '.join(bits)})" if bits else ""
    print(f"cycle time across {d['count']} node(s){filt_str}")
    if d["count"] == 0:
        return
    active = d["active_seconds"]
    print(f"  total (sum):   {d['total']}")
    print(f"  active:        {d['active']}")
    if active > 0:
        print(f"    first-pass:  {d['first_pass']}  ({_pct(d['first_pass_seconds']/active)})")
        print(f"    rework:      {d['rework']}  ({_pct(d['rework_seconds']/active)})")
        print(f"    rework-ratio overall: {_pct(d['rework_ratio'])}")
    print(f"  wait:          {d['wait']}   (human gates + queues + approvals; inbox dwell excluded)")
    if d.get("transient_seconds", 0) > 0:
        print(f"  transient:     {d['transient']}")
    if d.get("by_executor"):
        print(f"  by executor:")
        for row in d["by_executor"]:
            if row["class"] == "active":
                rr = f"   [{_pct(row['rework_ratio'])} rework]" if row["rework_seconds"] > 0 else ""
                print(
                    f"    {row['executor']:22} active  {row['total']:>8}  "
                    f"first-pass {row['first_pass']}, rework {row['rework']}{rr}"
                )
            else:
                print(f"    {row['executor']:22} {row['class']:6} {row['total']:>8}")


# ---------------------------------------------------------------------------
# query quality
# ---------------------------------------------------------------------------


def cmd_query_quality(args) -> int:
    """`taskflow query quality <executor_id> [--since] [--all]`."""
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1
    executor_id = getattr(args, "name", None)
    since = getattr(args, "since", None)
    all_flag = bool(getattr(args, "scope_all", False) or getattr(args, "all", False))

    if not executor_id and not all_flag:
        print("taskflow: quality requires an executor id or --all", file=sys.stderr)
        return 1

    data = ct_mod.quality(project, executor_id, since=since, all_executors=all_flag)

    if _format(args) == "json":
        _print_json(data)
        return 0

    if all_flag:
        rows = data.get("executors", [])
        print(f"quality across {len(rows)} executor(s){'  (since ' + since + ')' if since else ''}")
        if not rows:
            print("  (no active-time observations)")
            return 0
        hdr = f"  {'executor':22}  {'active':>9}  {'first-pass':>10}  {'rework':>8}  ratio"
        print(hdr)
        for r in rows:
            print(
                f"  {r['executor']:22}  {r['total']:>9}  {r['first_pass']:>10}  "
                f"{r['rework']:>8}  {_pct(r['rework_ratio'])}"
            )
        return 0

    if not data.get("found", True):
        print(data.get("message", "no data"))
        return 0
    print(f"quality for {data['executor']}{'  (since ' + since + ')' if since else ''}")
    print(f"  total active: {data['total']}  (across {data.get('nodes', 0)} node(s), {data['visits']} visits)")
    print(f"  first-pass:   {data['first_pass']}")
    print(f"  rework:       {data['rework']}")
    print(f"  rework-ratio: {_pct(data['rework_ratio'])}")
    return 0


# ---------------------------------------------------------------------------
# query queue-staleness
# ---------------------------------------------------------------------------


def cmd_query_queue_staleness(args) -> int:
    """`taskflow query queue-staleness [--threshold DURATION]`."""
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1
    threshold = getattr(args, "threshold", None)

    try:
        data = ct_mod.queue_staleness(project, threshold=threshold)
    except ValueError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json(data)
        return 0

    print(
        f"queue staleness across {data['count']} queue(s)  "
        f"(default threshold {data['threshold_default']}; "
        f"{data['stale_count']} stale)"
    )
    if data["count"] == 0:
        print("  (no queue-component executors declared)")
        return 0
    for row in data["queues"]:
        badge = "STALE " if row["stale"] else "      "
        pending_bit = f"pending={row['pending']}"
        oldest_bit = f"oldest={row['oldest_age']}" if row["pending"] else "idle"
        thr = f"threshold={row['threshold']}"
        print(f"  {badge}{row['executor']:22}  {pending_bit:14}  {oldest_bit:18}  {thr}")
    return 0
