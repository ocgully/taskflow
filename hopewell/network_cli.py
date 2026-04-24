"""CLI handler functions for `hopewell network ...` (HW-0027).

Kept in a separate module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    sp = sub.add_parser("network", help="Flow-network: executors + routes")
    nsub = sp.add_subparsers(dest="network_cmd", required=True)

    np = nsub.add_parser("init", help="Scaffold .hopewell/network/")
    np.add_argument("--quiet", action="store_true")
    np.set_defaults(func=lambda a: network_cli.cmd_network_init(a))

    np = nsub.add_parser("defaults", help="Default-template ops")
    np.add_argument("action", choices=["bootstrap"])
    np.add_argument("--quiet", action="store_true")
    np.set_defaults(func=lambda a: network_cli.cmd_network_defaults(a))

    np = nsub.add_parser("executor", help="executor add/rm/show/list")
    np.add_argument("action", choices=["add", "rm", "show", "list"])
    np.add_argument("id", nargs="?")
    np.add_argument("--components", default=None,
                    help="(add) comma-separated component list")
    np.add_argument("--component-data", default=None,
                    help="(add) JSON-encoded dict keyed by component name")
    np.add_argument("--parent", default=None,
                    help="(add) parent group executor id")
    np.add_argument("--label", default=None, help="(add) human display label")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli.cmd_network_executor(a))

    np = nsub.add_parser("route", help="route add/rm/list")
    np.add_argument("action", choices=["add", "rm", "list"])
    np.add_argument("from_id", nargs="?")
    np.add_argument("to", nargs="?")
    np.add_argument("--condition", default=None)
    np.add_argument("--label", default=None)
    np.add_argument("--required", action="store_true")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli.cmd_network_route(a))

    np = nsub.add_parser("show", help="Full flow-network render")
    np.add_argument("--format", choices=["text", "json", "mermaid"], default="text")
    np.set_defaults(func=lambda a: network_cli.cmd_network_show(a))

    np = nsub.add_parser("validate", help="Run validation rules")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli.cmd_network_validate(a))
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from hopewell import network as net_mod
from hopewell import network_defaults as defaults_mod
from hopewell import paths as paths_mod
from hopewell.executor import Executor, Route, validate_executor_id


def _project_root(args) -> Path:
    start = Path(args.project_root).resolve() if getattr(args, "project_root", None) else None
    return paths_mod.require_project_root(start)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


# ---------------------------------------------------------------------------
# network init / defaults
# ---------------------------------------------------------------------------


def cmd_network_init(args) -> int:
    root = _project_root(args)
    net_mod.ensure_network_dir(root)
    wrote = net_mod.install_gitattributes(root)
    if not getattr(args, "quiet", False):
        print(f"Initialized {net_mod.network_dir(root)}")
        print(f"  executors/    - one JSON per executor")
        print(f"  routes.jsonl  - append-only routes log")
        print(f"  components/   - optional project-custom executor components")
        if wrote:
            print(f"  .gitattributes - added routes.jsonl merge driver entry")
        else:
            print(f"  .gitattributes - already configured (skipped)")
        print(f"  next: `hopewell network defaults bootstrap` to seed a template")
    return 0


def cmd_network_defaults(args) -> int:
    root = _project_root(args)
    if args.action != "bootstrap":
        print(f"hopewell network defaults: unknown action {args.action!r}", file=sys.stderr)
        return 1
    summary = defaults_mod.write_default_template(root)
    if not getattr(args, "quiet", False):
        print(f"Bootstrapped default flow-network template:")
        print(f"  executors written: {summary['executors']}")
        print(f"  routes added:      {summary['routes_added']} "
              f"(template has {summary['routes_in_template']})")
        print(f"  next: `hopewell network show --format mermaid`")
    return 0


# ---------------------------------------------------------------------------
# executor add / rm / show / list
# ---------------------------------------------------------------------------


def cmd_network_executor(args) -> int:
    root = _project_root(args)
    action = args.action

    if action == "add":
        if not args.id:
            print("hopewell: executor add requires an id", file=sys.stderr)
            return 1
        try:
            validate_executor_id(args.id)
        except ValueError as e:
            print(f"hopewell: {e}", file=sys.stderr)
            return 1
        components = [c.strip() for c in (args.components or "").split(",") if c.strip()]
        if not components:
            print("hopewell: executor add requires --components c1,c2,...", file=sys.stderr)
            return 1
        component_data = {}
        if args.component_data:
            try:
                component_data = json.loads(args.component_data)
            except json.JSONDecodeError as e:
                print(f"hopewell: --component-data is not valid JSON: {e}", file=sys.stderr)
                return 1
            if not isinstance(component_data, dict):
                print("hopewell: --component-data must be a JSON object", file=sys.stderr)
                return 1
        ex = Executor(
            id=args.id,
            components=components,
            component_data=component_data,
            parent=args.parent,
            label=args.label,
        )
        try:
            path = net_mod.add_executor(root, ex)
        except FileExistsError as e:
            print(f"hopewell: {e}", file=sys.stderr)
            return 1
        if args.format == "json":
            _print_json({"op": "executor.add", "executor": ex.to_dict(),
                         "path": str(path)})
        else:
            print(f"added executor {ex.id}")
            print(f"  components: {', '.join(ex.components)}")
            print(f"  file:       {path}")
        return 0

    if action == "rm":
        if not args.id:
            print("hopewell: executor rm requires an id", file=sys.stderr)
            return 1
        ok = net_mod.remove_executor(root, args.id)
        if not ok:
            print(f"hopewell: no executor named {args.id!r}", file=sys.stderr)
            return 1
        if args.format == "json":
            _print_json({"op": "executor.rm", "id": args.id})
        else:
            print(f"removed executor {args.id} (and tombstoned its routes)")
        return 0

    if action == "show":
        if not args.id:
            print("hopewell: executor show requires an id", file=sys.stderr)
            return 1
        net = net_mod.load_network(root)
        ex = net.get(args.id)
        if ex is None:
            print(f"hopewell: no executor named {args.id!r}", file=sys.stderr)
            return 1
        if args.format == "json":
            _print_json({
                "executor": ex.to_dict(),
                "routes_out": [r.to_dict() for r in net.routes_from(args.id)],
                "routes_in":  [r.to_dict() for r in net.routes_to(args.id)],
            })
        else:
            print(f"# {ex.id}{'  -' + ex.label if ex.label else ''}")
            print(f"components:     {', '.join(ex.components) or '-'}")
            if ex.parent:
                print(f"parent:         {ex.parent}")
            if ex.component_data:
                print("component_data:")
                print(json.dumps(ex.component_data, indent=2, ensure_ascii=False))
            outs = net.routes_from(args.id)
            ins = net.routes_to(args.id)
            if outs:
                print("routes out:")
                for r in outs:
                    _print_route_text(r)
            if ins:
                print("routes in:")
                for r in ins:
                    _print_route_text(r)
        return 0

    if action == "list":
        net = net_mod.load_network(root)
        rows = sorted(net.executors.values(), key=lambda e: e.id)
        if args.format == "json":
            _print_json({"count": len(rows),
                         "executors": [ex.to_dict() for ex in rows]})
        else:
            print(f"{len(rows)} executor(s)")
            for ex in rows:
                lbl = f" - {ex.label}" if ex.label else ""
                print(f"  {ex.id:22} [{','.join(ex.components)}]{lbl}")
        return 0

    print(f"hopewell: unknown executor action {action!r}", file=sys.stderr)
    return 1


def _print_route_text(r: Route) -> None:
    req = " *required*" if r.required else ""
    cond = f" [{r.condition}]" if r.condition else ""
    lbl = f" ({r.label})" if r.label else ""
    print(f"  {r.from_id} -> {r.to_id}{cond}{lbl}{req}")


# ---------------------------------------------------------------------------
# route add / rm / list
# ---------------------------------------------------------------------------


def cmd_network_route(args) -> int:
    root = _project_root(args)
    action = args.action

    if action == "add":
        if not args.from_id or not args.to:
            print("hopewell: route add requires <from> <to>", file=sys.stderr)
            return 1
        r = Route(
            from_id=args.from_id, to_id=args.to,
            condition=args.condition, label=args.label,
            required=bool(args.required),
        )
        net_mod.add_route(root, r)
        if args.format == "json":
            _print_json({"op": "route.add", "route": r.to_dict()})
        else:
            print(f"added route {r.from_id} -> {r.to_id}"
                  + (f" [{r.condition}]" if r.condition else "")
                  + (" *required*" if r.required else ""))
        return 0

    if action == "rm":
        if not args.from_id or not args.to:
            print("hopewell: route rm requires <from> <to>", file=sys.stderr)
            return 1
        net_mod.remove_route(root, args.from_id, args.to, condition=args.condition)
        if args.format == "json":
            _print_json({"op": "route.rm", "from": args.from_id, "to": args.to,
                         "condition": args.condition})
        else:
            print(f"removed route {args.from_id} -> {args.to}"
                  + (f" [{args.condition}]" if args.condition else ""))
        return 0

    if action == "list":
        net = net_mod.load_network(root)
        rows = sorted(net.routes, key=lambda r: (r.from_id, r.to_id, r.condition or ""))
        if args.format == "json":
            _print_json({"count": len(rows),
                         "routes": [r.to_dict() for r in rows]})
        else:
            print(f"{len(rows)} route(s)")
            for r in rows:
                _print_route_text(r)
        return 0

    print(f"hopewell: unknown route action {action!r}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# network show + validate
# ---------------------------------------------------------------------------


def cmd_network_show(args) -> int:
    root = _project_root(args)
    net = net_mod.load_network(root)
    if args.format == "json":
        _print_json(net_mod.to_json(net))
        return 0
    if args.format == "mermaid":
        sys.stdout.write(net_mod.to_mermaid(net))
        return 0
    # text -tabular
    exs = sorted(net.executors.values(), key=lambda e: e.id)
    print(f"# Flow network - {len(exs)} executor(s), {len(net.routes)} route(s)")
    if exs:
        print("\n## Executors")
        for ex in exs:
            lbl = f" - {ex.label}" if ex.label else ""
            par = f"  (parent: {ex.parent})" if ex.parent else ""
            print(f"  {ex.id:22} [{','.join(ex.components)}]{lbl}{par}")
    if net.routes:
        print("\n## Routes")
        for r in sorted(net.routes, key=lambda r: (r.from_id, r.to_id, r.condition or "")):
            _print_route_text(r)
    return 0


def cmd_network_validate(args) -> int:
    root = _project_root(args)
    net = net_mod.load_network(root)
    problems = net_mod.validate(net)
    if args.format == "json":
        _print_json({"problems": problems, "clean": not problems,
                     "executor_count": len(net.executors),
                     "route_count": len(net.routes)})
        return 0 if not problems else 1
    if not problems:
        print(f"hopewell network validate: clean "
              f"({len(net.executors)} executor(s), {len(net.routes)} route(s))")
        return 0
    print(f"hopewell network validate: {len(problems)} problem(s)")
    for p in problems:
        print(f"  - {p}")
    return 1


# ---------------------------------------------------------------------------
# HW-0050 — annotate-auto-enforced
# ---------------------------------------------------------------------------


def cmd_network_annotate_auto_enforced(args) -> int:
    """Mark routes covered by Hopewell's git hooks with
    `data.auto_enforced = true`.

    Default is a dry-run that prints which routes would change. Pass
    `--apply` to persist.
    """
    root = _project_root(args)
    net = net_mod.load_network(root)
    candidates = net_mod.routes_covered_by_hooks(net)
    new_ones = [r for r in candidates if not r.data.get("auto_enforced")]

    payload = {
        "total_routes": len(net.routes),
        "covered_by_hooks": len(candidates),
        "would_annotate": [
            {"from": r.from_id, "to": r.to_id, "condition": r.condition or None}
            for r in new_ones
        ],
        "already_annotated": len(candidates) - len(new_ones),
        "applied": False,
    }
    if getattr(args, "apply", False):
        changed = net_mod.annotate_auto_enforced_routes(root, new_ones)
        payload["applied"] = True
        payload["changed"] = changed

    if args.format == "json":
        _print_json(payload)
        return 0

    print(f"hopewell network annotate-auto-enforced")
    print(f"  total routes:          {payload['total_routes']}")
    print(f"  covered by hooks:      {payload['covered_by_hooks']}")
    print(f"  already annotated:     {payload['already_annotated']}")
    print(f"  would annotate (new):  {len(new_ones)}")
    for r in new_ones:
        cond = f" [{r.condition}]" if r.condition else ""
        print(f"    - {r.from_id} -> {r.to_id}{cond}")
    if payload["applied"]:
        print(f"  APPLIED: {payload['changed']} route(s) updated")
    elif new_ones:
        print(f"  (dry-run — rerun with --apply to persist)")
    return 0
