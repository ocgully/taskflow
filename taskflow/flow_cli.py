"""CLI handler functions for `taskflow flow ...` (HW-0028).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success
    1 — unknown executor / unknown node / malformed input
    2 — `flow enter` blocked by reconciliation gate (HW-0034). Caller
        should resolve the referenced downstream-review node before
        retrying. See `taskflow reconcile resolve --help`.

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    from taskflow import flow_cli as flow_cli_mod

    sp = sub.add_parser("flow", help="Flow runtime: push/ack/enter/leave/inbox")
    fsub = sp.add_subparsers(dest="flow_cmd", required=True)

    fp = fsub.add_parser("enter", help="Add a location on a work item")
    fp.add_argument("node_id")
    fp.add_argument("--executor", required=True)
    fp.add_argument("--artifact", default=None)
    fp.add_argument("--reason", default=None)
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_enter(a))

    fp = fsub.add_parser("leave", help="Close a location on a work item")
    fp.add_argument("node_id")
    fp.add_argument("--executor", required=True)
    fp.add_argument("--reason", default=None)
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_leave(a))

    fp = fsub.add_parser("where", help="Show active locations for a work item")
    fp.add_argument("node_id")
    fp.add_argument("--history", action="store_true",
                    help="include closed (historical) locations")
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_where(a))

    fp = fsub.add_parser("inbox", help="Show pending pushes for an executor")
    fp.add_argument("executor_id")
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_inbox(a))

    fp = fsub.add_parser("push", help="Offer a work item to a target inbox")
    fp.add_argument("node_id")
    fp.add_argument("--to", required=True, dest="to_executor")
    fp.add_argument("--from", default=None, dest="from_executor")
    fp.add_argument("--artifact", default=None)
    fp.add_argument("--reason", default=None)
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_push(a))

    fp = fsub.add_parser("ack", help="Ack a pending push")
    fp.add_argument("node_id")
    fp.add_argument("--executor", required=True)
    fp.add_argument("--outcome", default="processed")
    fp.add_argument("--note", default=None)
    fp.add_argument("--format", choices=["text", "json"], default="text")
    fp.set_defaults(func=lambda a: flow_cli_mod.cmd_flow_ack(a))
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from taskflow import flow as flow_mod
from taskflow import paths as paths_mod


def _project(args):
    from taskflow.project import Project
    start = Path(args.project_root).resolve() if getattr(args, "project_root", None) else None
    return Project.load(start)


def _actor_from_env():
    return os.environ.get("HOPEWELL_ACTOR") or os.environ.get("GIT_AUTHOR_NAME")


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _format(args) -> str:
    return getattr(args, "format", "text") or "text"


# ---------------------------------------------------------------------------
# enter / leave
# ---------------------------------------------------------------------------


def cmd_flow_enter(args) -> int:
    from taskflow import reconciliation as recon_mod
    try:
        project = _project(args)
        added = flow_mod.enter(
            project, args.node_id, args.executor,
            artifact=getattr(args, "artifact", None),
            reason=getattr(args, "reason", None),
            actor=_actor_from_env(),
        )
    except recon_mod.ReconciliationRequired as e:
        # HW-0034: gate fired — flow.enter is blocked until the named
        # review node resolves. Exit 2 so scripts can distinguish "blocked"
        # from "bad input" (1) and "ok" (0).
        if _format(args) == "json":
            _print_json({
                "op": "flow.enter",
                "blocked": True,
                "review_node": e.review_node_id,
                "drifted_slices": e.drifted_slices,
                "node": args.node_id,
                "executor": args.executor,
            })
        else:
            print(f"taskflow: {e}", file=sys.stderr)
            print(
                f"  resolve via:  taskflow reconcile resolve {e.review_node_id} "
                f"--outcome <no-impact|update-in-scope|update-out-of-scope|spec-revert>",
                file=sys.stderr,
            )
            print(
                f"  bypass (scripts only):  HOPEWELL_SKIP_RECONCILIATION=1 "
                f"taskflow flow enter {args.node_id} --executor {args.executor}",
                file=sys.stderr,
            )
        return 2
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    node = project.node(args.node_id)
    status_str = node.status.value if hasattr(node.status, "value") else node.status
    if _format(args) == "json":
        _print_json({
            "op": "flow.enter",
            "node": args.node_id,
            "executor": args.executor,
            "added": added,
            "active_locations": [loc.to_dict() for loc in node.active_locations()],
            "status": status_str,
        })
    else:
        if added:
            print(f"entered {args.node_id} -> {args.executor}")
        else:
            print(f"{args.node_id} already at {args.executor} (no-op)")
        active = [loc.executor_id for loc in node.active_locations()]
        print(f"  active locations: {', '.join(active) if active else '-'}")
        print(f"  status: {status_str}")
    return 0


def cmd_flow_leave(args) -> int:
    try:
        project = _project(args)
        closed = flow_mod.leave(
            project, args.node_id, args.executor,
            reason=getattr(args, "reason", None),
            actor=_actor_from_env(),
        )
    except FileNotFoundError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    node = project.node(args.node_id)
    if _format(args) == "json":
        _print_json({
            "op": "flow.leave",
            "node": args.node_id,
            "executor": args.executor,
            "closed": closed,
            "active_locations": [loc.to_dict() for loc in node.active_locations()],
        })
    else:
        if closed:
            print(f"left {args.node_id} <- {args.executor}")
        else:
            print(f"{args.node_id} had no active location at {args.executor} (no-op)")
        active = [loc.executor_id for loc in node.active_locations()]
        print(f"  active locations: {', '.join(active) if active else '-'}")
    return 0


# ---------------------------------------------------------------------------
# where / inbox
# ---------------------------------------------------------------------------


def cmd_flow_where(args) -> int:
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1
    try:
        if getattr(args, "history", False):
            locs = flow_mod.history(project, args.node_id)
        else:
            locs = flow_mod.where(project, args.node_id)
    except FileNotFoundError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "node": args.node_id,
            "history": bool(getattr(args, "history", False)),
            "count": len(locs),
            "locations": locs,
        })
        return 0

    if not locs:
        print(f"{args.node_id}: no locations")
        return 0
    print(f"{args.node_id}: {len(locs)} location(s)")
    for loc in locs:
        ex = loc.get("executor_id", "?")
        entered = loc.get("entered_at", "?")
        left = loc.get("left_at")
        art = loc.get("last_artifact")
        status_bit = f" (left {left})" if left else " [active]"
        art_bit = f"  artifact={art}" if art else ""
        print(f"  {ex:22} entered {entered}{status_bit}{art_bit}")
    return 0


def cmd_flow_inbox(args) -> int:
    try:
        project = _project(args)
        pending = flow_mod.inbox(project, args.executor_id)
    except ValueError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "executor": args.executor_id,
            "count": len(pending),
            "pending": pending,
        })
        return 0

    if not pending:
        print(f"{args.executor_id}: inbox empty")
        return 0
    print(f"{args.executor_id}: {len(pending)} pending")
    for entry in pending:
        frm = entry.get("from_executor") or "<external>"
        art = f"  artifact={entry['artifact']}" if entry.get("artifact") else ""
        reason = f"  reason={entry['reason']}" if entry.get("reason") else ""
        print(f"  {entry['node']:10} from {frm:20} at {entry.get('pushed_at','?')}{art}{reason}")
    return 0


# ---------------------------------------------------------------------------
# push / ack
# ---------------------------------------------------------------------------


def cmd_flow_push(args) -> int:
    try:
        project = _project(args)
        ev = flow_mod.push(
            project, args.node_id, args.to_executor,
            from_executor=getattr(args, "from_executor", None),
            artifact=getattr(args, "artifact", None),
            reason=getattr(args, "reason", None),
            actor=_actor_from_env(),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "flow.push", "event": ev})
    else:
        frm = getattr(args, "from_executor", None) or "<external>"
        print(f"pushed {args.node_id}: {frm} -> {args.to_executor}")
        if getattr(args, "artifact", None):
            print(f"  artifact: {args.artifact}")
        pending = flow_mod.inbox(project, args.to_executor)
        print(f"  {args.to_executor} inbox: {len(pending)} pending")
    return 0


def cmd_flow_ack(args) -> int:
    try:
        project = _project(args)
        ev = flow_mod.ack(
            project, args.node_id, args.executor,
            outcome=getattr(args, "outcome", "processed") or "processed",
            note=getattr(args, "note", None),
            actor=_actor_from_env(),
        )
    except ValueError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "flow.ack", "event": ev})
    else:
        print(f"acked {args.node_id} at {args.executor} (outcome={args.outcome})")
        pending = flow_mod.inbox(project, args.executor)
        print(f"  {args.executor} inbox: {len(pending)} pending")
    return 0
