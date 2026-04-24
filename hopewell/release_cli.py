"""CLI handlers for `hopewell release ...` (HW-0043).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns
an int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success
    1 — unknown release / scope validation failed / user-facing error
    2 — finalize below threshold (hold)
    3 — kickback executed (informational — handler still returns 0 on
        success; code 3 reserved for future fail-fast orchestrators)

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    from hopewell import release_cli as release_cli_mod

    sp = sub.add_parser(
        "release",
        help="Release tooling: release nodes + confidence scoring + "
             "kickback flow (HW-0043)",
    )
    rsub = sp.add_subparsers(dest="release_cmd", required=True)

    # release start <version> [--scope HW-N,HW-M,...] [--from-window <tag>]
    rp = rsub.add_parser("start",
        help="Initialize a release node (status=draft)")
    rp.add_argument("version")
    rp.add_argument("--scope", default=None,
        help="Comma-separated node ids; omit to auto-scope from --from-window")
    rp.add_argument("--from-window", dest="from_window", default=None,
        help="Previous release tag to start the auto-scope window from")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_start(a))

    # release scope <version> --add|--rm HW-NNNN
    rp = rsub.add_parser("scope",
        help="Add or remove a node from a release's scope")
    rp.add_argument("version")
    g = rp.add_mutually_exclusive_group(required=True)
    g.add_argument("--add", dest="add_id", default=None)
    g.add_argument("--rm", dest="rm_id", default=None)
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_scope(a))

    # release report <version> [--path PATH] [--regenerate]
    rp = rsub.add_parser("report",
        help="Regenerate the standardized release report")
    rp.add_argument("version")
    rp.add_argument("--path", default=None)
    rp.add_argument("--regenerate", action="store_true",
        help="Overwrite report even if present")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_report(a))

    # release score <version> [--format ...]
    rp = rsub.add_parser("score", help="Compute + print the confidence score")
    rp.add_argument("version")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_score(a))

    # release finalize <version> [--dry-run] [--tag] [--gh-release]
    rp = rsub.add_parser("finalize",
        help="Final gate: persist score, release, optional tag + gh release")
    rp.add_argument("version")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--tag", action="store_true",
        help="Create a local git tag on success")
    rp.add_argument("--gh-release", dest="gh_release", action="store_true",
        help="Invoke `gh release create` on success")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_finalize(a))

    # release kickback <version> --root-cause ... --affected HW-N[,HW-M] [--route-to @agent]
    rp = rsub.add_parser("kickback",
        help="Kick a release back: create needs-rework node, block release")
    rp.add_argument("version")
    rp.add_argument("--root-cause", dest="root_cause", required=True)
    rp.add_argument("--affected", required=True,
        help="Comma-separated node ids impacted by the kickback")
    rp.add_argument("--route-to", dest="route_to", default="@orchestrator")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_kickback(a))

    # release show <version>
    rp = rsub.add_parser("show", help="Show a release")
    rp.add_argument("version")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_show(a))

    # release list [--status ...]
    rp = rsub.add_parser("list", help="List releases")
    rp.add_argument("--status",
        choices=["draft", "held", "released", "kicked-back", "all"],
        default="all")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: release_cli_mod.cmd_release_list(a))

The module is also runnable directly — smoke tests can call::

    python -m hopewell.release_cli start v0.1.0 --scope SM-0001,SM-0002

until cli.py wires this in.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from hopewell import release as release_mod
from hopewell import release_confidence as rc_mod


# ---------------------------------------------------------------------------
# generic helpers (mirror cli.py / reconciliation_cli.py patterns)
# ---------------------------------------------------------------------------


def _project(args):
    from hopewell.project import Project
    start = Path(args.project_root).resolve() \
        if getattr(args, "project_root", None) else None
    return Project.load(start)


def _actor_from_env() -> Optional[str]:
    return os.environ.get("HOPEWELL_ACTOR") or os.environ.get("GIT_AUTHOR_NAME")


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False,
                                default=str) + "\n")


def _fmt(args) -> str:
    return getattr(args, "format", "text") or "text"


def _split_ids(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def cmd_release_start(args) -> int:
    project = _project(args)
    actor = _actor_from_env()
    scope = _split_ids(getattr(args, "scope", None))
    scope = scope or None
    try:
        node = release_mod.start(
            project, args.version,
            scope=scope,
            from_window=getattr(args, "from_window", None),
            actor=actor,
        )
    except ValueError as e:
        print(f"hopewell release: {e}", file=sys.stderr)
        return 1
    block = (node.component_data or {}).get(release_mod.COMPONENT) or {}
    if _fmt(args) == "json":
        _print_json({
            "release_node": node.id,
            "version": block.get("version"),
            "status": block.get("status"),
            "scope_count": len(block.get("scope_nodes") or []),
            "scope_nodes": block.get("scope_nodes") or [],
        })
    else:
        print(f"Started release {block.get('version')} -> {node.id}  "
              f"(status={block.get('status')}, "
              f"scope={len(block.get('scope_nodes') or [])})")
        for nid in (block.get("scope_nodes") or []):
            print(f"  - {nid}")
    return 0


def cmd_release_scope(args) -> int:
    project = _project(args)
    actor = _actor_from_env()
    try:
        if args.add_id:
            summary = release_mod.scope_add(
                project, args.version, args.add_id, actor=actor)
            op = "added"
            touched = args.add_id
        else:
            summary = release_mod.scope_rm(
                project, args.version, args.rm_id, actor=actor)
            op = "removed"
            touched = args.rm_id
    except (FileNotFoundError, ValueError) as e:
        print(f"hopewell release scope: {e}", file=sys.stderr)
        return 1
    if _fmt(args) == "json":
        _print_json({"op": op, "node": touched, "release": summary})
    else:
        print(f"{op} {touched} "
              f"({args.version}, scope={summary['scope_count']})")
    return 0


def cmd_release_report(args) -> int:
    project = _project(args)
    try:
        path_arg = getattr(args, "path", None)
        path = Path(path_arg) if path_arg else None
        if path is not None and not path.is_absolute():
            path = project.root / path
        out = release_mod.generate_report(project, args.version, path=path)
    except FileNotFoundError as e:
        print(f"hopewell release report: {e}", file=sys.stderr)
        return 1
    if _fmt(args) == "json":
        _print_json({"version": args.version,
                     "report_path": str(out.relative_to(project.root)).replace("\\", "/")})
    else:
        print(f"Wrote {out}")
    return 0


def cmd_release_score(args) -> int:
    project = _project(args)
    try:
        sc = release_mod.score(project, args.version)
    except FileNotFoundError as e:
        print(f"hopewell release score: {e}", file=sys.stderr)
        return 1
    if _fmt(args) == "json":
        _print_json(sc)
        return 0
    print(f"Release {sc['version']}: {sc['total']}/100 "
          f"(threshold {sc['threshold']}) -> {sc['outcome']}")
    for s in sc["signals"]:
        print(f"  {s['name']:<14} {s['score']:>3}/{s['weight']:<3}  "
              f"{s['justification']}")
    if sc.get("skipped"):
        print(f"  (skipped: {', '.join(sc['skipped'])})")
    return 0


def cmd_release_finalize(args) -> int:
    project = _project(args)
    actor = _actor_from_env()
    try:
        res = release_mod.finalize(
            project, args.version,
            dry_run=bool(getattr(args, "dry_run", False)),
            tag=bool(getattr(args, "tag", False)),
            gh_release=bool(getattr(args, "gh_release", False)),
            actor=actor,
        )
    except FileNotFoundError as e:
        print(f"hopewell release finalize: {e}", file=sys.stderr)
        return 1
    if _fmt(args) == "json":
        _print_json(res)
    else:
        print(f"Release {args.version}: {res['total']}/100 "
              f"threshold {res['threshold']} -> {res['outcome']}"
              + (" (dry-run)" if res.get("dry_run") else ""))
        if res["outcome"] == "below-threshold":
            print("Missing / weak signals:")
            for m in res.get("missing", []):
                print(f"  - {m['name']} ({m['got']}/{m['weight']}): "
                      f"{m['justification']}")
        else:
            if res.get("tag_created"):
                print(f"  git tag: {res['tag_created']}")
            if res.get("gh_release_created"):
                print(f"  gh release: {res['gh_release_created']}")
    return 0 if res["outcome"] != "below-threshold" else 2


def cmd_release_kickback(args) -> int:
    project = _project(args)
    actor = _actor_from_env()
    affected = _split_ids(args.affected)
    if not affected:
        print("hopewell release kickback: --affected must list at least one node",
              file=sys.stderr)
        return 1
    try:
        res = release_mod.kickback(
            project, args.version,
            root_cause=args.root_cause,
            affected=affected,
            route_to=args.route_to,
            actor=actor,
        )
    except FileNotFoundError as e:
        print(f"hopewell release kickback: {e}", file=sys.stderr)
        return 1
    if _fmt(args) == "json":
        _print_json(res)
    else:
        print(f"Release {args.version} kicked back — status={res['status']}")
        print(f"  rework node:   {res['rework_node']}")
        print(f"  routed to:     {res['route_to']}")
        print(f"  flow.push:     {'yes' if res['flow_push'] else 'no (no executor)'}")
    return 0


def cmd_release_show(args) -> int:
    project = _project(args)
    node = release_mod.find_release_node(project, args.version)
    if node is None:
        print(f"hopewell release show: no release for {args.version!r}",
              file=sys.stderr)
        return 1
    block = (node.component_data or {}).get(release_mod.COMPONENT) or {}
    summary = {
        "release_node": node.id,
        **block,
        "title": node.title,
        "blocked_by": list(node.blocked_by),
        "blocks": list(node.blocks),
    }
    if _fmt(args) == "json":
        _print_json(summary)
        return 0
    print(f"# {node.id} — {node.title}")
    print(f"version:      {block.get('version')}")
    print(f"status:       {block.get('status')}")
    print(f"scope_nodes:  {len(block.get('scope_nodes') or [])}")
    for nid in (block.get("scope_nodes") or []):
        print(f"  - {nid}")
    if block.get("confidence_score") is not None:
        print(f"confidence:   {block.get('confidence_score')}")
    if block.get("tag"):
        print(f"tag:          {block.get('tag')}")
    if block.get("released_at"):
        print(f"released_at:  {block.get('released_at')}")
        print(f"released_by:  {block.get('released_by')}")
    if block.get("report_path"):
        print(f"report:       {block.get('report_path')}")
    if block.get("kickback"):
        kb = block["kickback"]
        print("kickback:")
        print(f"  root_cause: {kb.get('root_cause')}")
        print(f"  affected:   {kb.get('affected')}")
        print(f"  route_to:   {kb.get('route_to')}")
        print(f"  rework:     {kb.get('rework_node')}")
    if node.blocked_by:
        print(f"blocked_by:   {', '.join(node.blocked_by)}")
    return 0


def cmd_release_list(args) -> int:
    project = _project(args)
    status = getattr(args, "status", None) or "all"
    rows = release_mod.list_releases(project,
                                     status=(None if status == "all" else status))
    if _fmt(args) == "json":
        _print_json({"count": len(rows), "status_filter": status,
                     "releases": rows})
        return 0
    if not rows:
        print(f"no releases (filter={status})")
        return 0
    print(f"{len(rows)} release(s)")
    for r in rows:
        print(f"  {r['version']:<12}  {r['status']:<12}  "
              f"scope={r['scope_count']:<3}  "
              f"score={r['confidence_score'] if r['confidence_score'] is not None else '—'}  "
              f"node={r['id']}")
    return 0


# ---------------------------------------------------------------------------
# standalone parser — matches reconciliation_cli pattern
# ---------------------------------------------------------------------------


def _build_standalone_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hopewell.release_cli",
                                description="Release tooling (HW-0043)")
    p.add_argument("--project-root", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("start")
    rp.add_argument("version")
    rp.add_argument("--scope", default=None)
    rp.add_argument("--from-window", dest="from_window", default=None)
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_start)

    rp = sub.add_parser("scope")
    rp.add_argument("version")
    g = rp.add_mutually_exclusive_group(required=True)
    g.add_argument("--add", dest="add_id", default=None)
    g.add_argument("--rm", dest="rm_id", default=None)
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_scope)

    rp = sub.add_parser("report")
    rp.add_argument("version")
    rp.add_argument("--path", default=None)
    rp.add_argument("--regenerate", action="store_true")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_report)

    rp = sub.add_parser("score")
    rp.add_argument("version")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_score)

    rp = sub.add_parser("finalize")
    rp.add_argument("version")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--tag", action="store_true")
    rp.add_argument("--gh-release", dest="gh_release", action="store_true")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_finalize)

    rp = sub.add_parser("kickback")
    rp.add_argument("version")
    rp.add_argument("--root-cause", dest="root_cause", required=True)
    rp.add_argument("--affected", required=True)
    rp.add_argument("--route-to", dest="route_to", default="@orchestrator")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_kickback)

    rp = sub.add_parser("show")
    rp.add_argument("version")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_show)

    rp = sub.add_parser("list")
    rp.add_argument("--status",
                    choices=["draft", "held", "released", "kicked-back", "all"],
                    default="all")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_release_list)

    return p


def _force_utf8_stdout() -> None:
    """Mirror cli.py behaviour so Windows cp1252 doesn't eat unicode."""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _force_utf8_stdout()
    parser = _build_standalone_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
