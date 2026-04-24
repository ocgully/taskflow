"""CLI handler functions for `hopewell reconcile ...` (HW-0034).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success
    1 — unknown node / malformed input / file missing
    2 — (reserved; the gate exit-code path lives in flow_cli, not here)

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    from hopewell import reconciliation_cli as recon_cli_mod

    sp = sub.add_parser(
        "reconcile",
        help="Reconciliation flow: queue + resolve downstream-review nodes "
             "for spec drift",
    )
    rsub = sp.add_subparsers(dest="reconcile_cmd", required=True)

    # reconcile queue <spec_path> [--heading | --lines] [--dry-run]
    rp = rsub.add_parser("queue",
        help="List/create downstream-review nodes for consumers of a spec slice")
    rp.add_argument("spec_path")
    rp.add_argument("--heading", default=None,
                    help="Markdown heading text or slug (e.g. '## Flow Network')")
    rp.add_argument("--lines", default=None,
                    help="Line range (e.g. '45-72'); mutually exclusive with --heading")
    rp.add_argument("--dry-run", action="store_true",
                    help="Print what would be created without writing")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: recon_cli_mod.cmd_reconcile_queue(a))

    # reconcile ls [--consumer HW-NNNN] [--spec PATH] [--status open|resolved|all]
    rp = rsub.add_parser("ls",
        help="List downstream-review nodes")
    rp.add_argument("--consumer", default=None)
    rp.add_argument("--spec", dest="spec_path", default=None)
    rp.add_argument("--status", choices=["open", "resolved", "all"],
                    default="open")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: recon_cli_mod.cmd_reconcile_ls(a))

    # reconcile resolve HW-NNNN --outcome ... [--notes ...] [--followup-title ...]
    rp = rsub.add_parser("resolve",
        help="Close a downstream-review with one of four outcomes")
    rp.add_argument("review_id")
    rp.add_argument("--outcome", required=True,
                    choices=["no-impact", "update-in-scope",
                             "update-out-of-scope", "spec-revert"])
    rp.add_argument("--notes", default=None)
    rp.add_argument("--followup-title", dest="followup_title", default=None,
                    help="Required when --outcome=update-out-of-scope")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=lambda a: recon_cli_mod.cmd_reconcile_resolve(a))

The module is also runnable directly during smoke tests:

    python -m hopewell.reconciliation_cli queue specs/foo.md --heading "## X"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from hopewell import reconciliation as recon_mod
from hopewell import spec_input as spec_mod


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def _project(args):
    from hopewell.project import Project
    start = Path(args.project_root).resolve() if getattr(args, "project_root", None) else None
    return Project.load(start)


def _actor_from_env() -> Optional[str]:
    return os.environ.get("HOPEWELL_ACTOR") or os.environ.get("GIT_AUTHOR_NAME")


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _format(args) -> str:
    return getattr(args, "format", "text") or "text"


def _parse_lines_opt(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    if raw is None:
        return None
    try:
        return spec_mod.parse_lines_arg(raw)
    except ValueError as e:
        raise ValueError(f"--lines: {e}")


# ---------------------------------------------------------------------------
# reconcile queue
# ---------------------------------------------------------------------------


def cmd_reconcile_queue(args) -> int:
    try:
        project = _project(args)
        heading = getattr(args, "heading", None)
        lines_raw = getattr(args, "lines", None)
        if heading and lines_raw:
            raise ValueError("--heading and --lines are mutually exclusive")
        lines = _parse_lines_opt(lines_raw)
        results = recon_mod.queue_reviews(
            project, args.spec_path,
            heading=heading, lines=lines,
            trigger=recon_mod.TRIGGER_SPEC_EDIT,
            actor=_actor_from_env(),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "op": "reconcile.queue",
            "spec_path": args.spec_path,
            "heading": getattr(args, "heading", None),
            "lines": getattr(args, "lines", None),
            "dry_run": bool(getattr(args, "dry_run", False)),
            "count": len(results),
            "created": sum(1 for r in results if r["action"] == "created"),
            "skipped_existing": sum(1 for r in results if r["action"] == "skipped-existing"),
            "skipped_clean": sum(1 for r in results if r["action"] == "skipped-clean"),
            "dry_run_count": sum(1 for r in results if r["action"] == "dry-run"),
            "results": results,
        })
        return 0

    if not results:
        print(f"reconcile queue {args.spec_path}: no consumers match this slice")
        return 0
    print(f"reconcile queue {args.spec_path}:")
    for r in results:
        slice_label = (
            r["slice"].get("anchor")
            or (f"L{r['slice']['lines'][0]}-L{r['slice']['lines'][1]}"
                if r["slice"].get("lines") else "?")
        )
        action = r["action"]
        rev = r.get("review_node") or "-"
        print(f"  {r['consumer']:10}  {slice_label:30}  state={r['drift_state']:12}"
              f"  action={action:18}  review={rev}")
    return 0


# ---------------------------------------------------------------------------
# reconcile ls
# ---------------------------------------------------------------------------


def cmd_reconcile_ls(args) -> int:
    try:
        project = _project(args)
        rows = recon_mod.list_reviews(
            project,
            consumer=getattr(args, "consumer", None),
            spec_path=getattr(args, "spec_path", None),
            status=getattr(args, "status", "open") or "open",
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "count": len(rows),
            "filter": {
                "consumer": getattr(args, "consumer", None),
                "spec_path": getattr(args, "spec_path", None),
                "status": getattr(args, "status", "open"),
            },
            "reviews": rows,
        })
        return 0

    if not rows:
        print(f"reconcile ls: no reviews matching filter "
              f"(status={getattr(args, 'status', 'open')})")
        return 0
    print(f"reconcile ls: {len(rows)} review(s)")
    for r in rows:
        slice_label = (
            (r.get("slice") or {}).get("anchor")
            or (f"L{r['slice']['lines'][0]}-L{r['slice']['lines'][1]}"
                if r.get("slice") and r['slice'].get("lines") else "?")
        )
        print(f"  {r['review_node']:10}  status={r['status']:9}  "
              f"consumer={r['consumer_node']:10}  spec={r['spec_path']}@{slice_label}")
        if r.get("outcome"):
            note = r.get("resolution_notes") or ""
            print(f"      outcome={r['outcome']}  notes={note}")
    return 0


# ---------------------------------------------------------------------------
# reconcile resolve
# ---------------------------------------------------------------------------


def cmd_reconcile_resolve(args) -> int:
    try:
        project = _project(args)
        result = recon_mod.resolve_review(
            project, args.review_id,
            outcome=args.outcome,
            notes=getattr(args, "notes", None),
            followup_title=getattr(args, "followup_title", None),
            actor=_actor_from_env(),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "reconcile.resolve", "result": result})
        return 0

    print(f"reconcile resolved: {args.review_id} (outcome={result['outcome']})")
    if result.get("followup_node"):
        print(f"  follow-up: {result['followup_node']}")
    if result.get("notes"):
        print(f"  notes: {result['notes']}")
    print(f"  consumer: {result.get('consumer_node')} "
          f"  spec: {result.get('spec_path')}")
    return 0


# ---------------------------------------------------------------------------
# Standalone runner — `python -m hopewell.reconciliation_cli ...`
# ---------------------------------------------------------------------------
#
# Lets us exercise the CLI before the cli.py wiring step lands. Mirrors
# the docstring snippet exactly.

def _build_standalone_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hopewell.reconciliation_cli",
        description="Reconciliation flow CLI — standalone runner.",
    )
    parser.add_argument("--project-root", default=None)
    sub = parser.add_subparsers(dest="reconcile_cmd", required=True)

    rp = sub.add_parser("queue",
        help="List/create downstream-review nodes for consumers of a spec slice")
    rp.add_argument("spec_path")
    rp.add_argument("--heading", default=None)
    rp.add_argument("--lines", default=None)
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_reconcile_queue)

    rp = sub.add_parser("ls", help="List downstream-review nodes")
    rp.add_argument("--consumer", default=None)
    rp.add_argument("--spec", dest="spec_path", default=None)
    rp.add_argument("--status", choices=["open", "resolved", "all"], default="open")
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_reconcile_ls)

    rp = sub.add_parser("resolve",
        help="Close a downstream-review with one of four outcomes")
    rp.add_argument("review_id")
    rp.add_argument("--outcome", required=True,
                    choices=["no-impact", "update-in-scope",
                             "update-out-of-scope", "spec-revert"])
    rp.add_argument("--notes", default=None)
    rp.add_argument("--followup-title", dest="followup_title", default=None)
    rp.add_argument("--format", choices=["text", "json"], default="text")
    rp.set_defaults(func=cmd_reconcile_resolve)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_standalone_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
