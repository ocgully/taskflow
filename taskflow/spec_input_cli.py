"""CLI handler functions for `taskflow spec-ref ...` and
`taskflow query consumers ...` (HW-0031).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success, no drift
    1 — unknown node / malformed input / file missing
    2 — drift detected (for `spec-ref drift`)

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    from taskflow import spec_input_cli as spec_cli_mod

    sp = sub.add_parser("spec-ref",
                        help="Spec-input: quote-by-reference links to spec slices")
    ssub = sp.add_subparsers(dest="spec_cmd", required=True)

    sp_add = ssub.add_parser("add", help="Record a spec-ref on a work item")
    sp_add.add_argument("node_id")
    sp_add.add_argument("--path", required=True)
    sp_add.add_argument("--heading", default=None,
                        help="Markdown heading text or slug (e.g. '## Flow Network')")
    sp_add.add_argument("--lines", default=None,
                        help="Line range, e.g. '45-72'. Mutually exclusive with --heading.")
    sp_add.add_argument("--why", default=None, help="Why this slice matters to the work item")
    sp_add.add_argument("--format", choices=["text", "json"], default="text")
    sp_add.set_defaults(func=lambda a: spec_cli_mod.cmd_specref_add(a))

    sp_ls = ssub.add_parser("ls", help="List recorded spec-refs on a work item")
    sp_ls.add_argument("node_id")
    sp_ls.add_argument("--format", choices=["text", "json"], default="text")
    sp_ls.set_defaults(func=lambda a: spec_cli_mod.cmd_specref_ls(a))

    sp_rm = ssub.add_parser("rm", help="Remove a spec-ref slice")
    sp_rm.add_argument("node_id")
    sp_rm.add_argument("--path", required=True)
    sp_rm.add_argument("--heading", default=None)
    sp_rm.add_argument("--lines", default=None)
    sp_rm.add_argument("--format", choices=["text", "json"], default="text")
    sp_rm.set_defaults(func=lambda a: spec_cli_mod.cmd_specref_rm(a))

    sp_dr = ssub.add_parser("drift",
                            help="Check slices for drift (exit 2 if any drift)")
    g = sp_dr.add_mutually_exclusive_group()
    g.add_argument("node_id", nargs="?", default=None)
    sp_dr.add_argument("--all", action="store_true",
                       help="Check every node with spec-input component")
    sp_dr.add_argument("--patch", action="store_true",
                       help="Emit unified diff for each drifted slice")
    sp_dr.add_argument("--format", choices=["text", "json"], default="text")
    sp_dr.set_defaults(func=lambda a: spec_cli_mod.cmd_specref_drift(a))

And on the existing `taskflow query` subparser:

    qp = query_sub.add_parser("consumers",
                              help="Who references a spec file (reverse-nav)")
    qp.add_argument("spec_path")
    qp.add_argument("--slice", dest="slice_spec", default=None,
                    help="Heading text ('## Foo') or line range ('45-72') to narrow")
    qp.add_argument("--format", choices=["text", "json"], default="text")
    qp.set_defaults(func=lambda a: spec_cli_mod.cmd_query_consumers(a))
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

from taskflow import spec_input as spec_mod


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def _project(args):
    from taskflow.project import Project
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


def _validate_slice_selector(args) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    heading = getattr(args, "heading", None)
    lines_raw = getattr(args, "lines", None)
    if heading and lines_raw:
        raise ValueError("--heading and --lines are mutually exclusive")
    lines = _parse_lines_opt(lines_raw)
    return heading, lines


# ---------------------------------------------------------------------------
# spec-ref add / ls / rm
# ---------------------------------------------------------------------------


def cmd_specref_add(args) -> int:
    try:
        project = _project(args)
        heading, lines = _validate_slice_selector(args)
        if not heading and not lines:
            raise ValueError("one of --heading or --lines is required")
        result = spec_mod.add_spec_ref(
            project, args.node_id, args.path,
            heading=heading, lines=lines,
            why=getattr(args, "why", None),
            actor=_actor_from_env(),
        )
    except (ValueError, FileNotFoundError, OSError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "spec.ref.add", "ref": result})
    else:
        where = result.get("anchor") or f"L{result['lines'][0]}-L{result['lines'][1]}"
        print(f"spec-ref added: {args.node_id} -> {result['path']} @ {where}")
        print(f"  lines:      {result['lines'][0]}-{result['lines'][1]}")
        print(f"  slice_sha:  {result['slice_sha']}")
        print(f"  doc_sha:    {result['doc_sha']}")
        if result.get("why"):
            print(f"  why:        {result['why']}")
    return 0


def cmd_specref_ls(args) -> int:
    try:
        project = _project(args)
        refs = spec_mod.ls_spec_refs(project, args.node_id)
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"node": args.node_id, "count": len(refs), "refs": refs})
        return 0

    if not refs:
        print(f"{args.node_id}: no spec-refs")
        return 0
    print(f"{args.node_id}: {len(refs)} spec-ref(s)")
    for r in refs:
        where = r["anchor"] or f"L{r['lines'][0]}-L{r['lines'][1]}"
        print(f"  {r['path']:40} {where}")
        print(f"    lines={r['lines'][0]}-{r['lines'][1]}  "
              f"slice_sha={r['slice_sha'][:12]}")
        if r.get("why"):
            print(f"    why: {r['why']}")
    return 0


def cmd_specref_rm(args) -> int:
    try:
        project = _project(args)
        heading, lines = _validate_slice_selector(args)
        if not heading and not lines:
            raise ValueError("one of --heading or --lines is required")
        removed = spec_mod.rm_spec_ref(
            project, args.node_id, args.path,
            heading=heading, lines=lines,
            actor=_actor_from_env(),
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "spec.ref.rm", "removed": removed,
                     "node": args.node_id, "path": args.path})
        return 0

    if removed:
        print(f"spec-ref removed: {args.node_id} -> {args.path}")
    else:
        print(f"no matching spec-ref on {args.node_id} (no-op)")
    return 0


# ---------------------------------------------------------------------------
# spec-ref drift
# ---------------------------------------------------------------------------


def _print_drift_text(entries: List[dict]) -> int:
    drifted = 0
    for e in entries:
        state = e.get("state")
        where = e.get("anchor") or f"L{e.get('lines_was', [0,0])[0]}-L{e.get('lines_was',[0,0])[1]}"
        label = f"{e.get('node','?')}  {e.get('path')} @ {where}"
        if state == "clean":
            print(f"  [clean]        {label}")
        elif state == "drift":
            drifted += 1
            print(f"  [DRIFT]        {label}")
            was = e.get("slice_sha_was") or ""
            now = e.get("slice_sha_now") or ""
            print(f"      was={was[:12]}  now={now[:12]}")
            if "lines_now" in e and e["lines_now"] != e.get("lines_was"):
                print(f"      lines was={e['lines_was']}  now={e['lines_now']}")
            if e.get("patch"):
                for line in str(e["patch"]).splitlines():
                    print(f"      {line}")
        elif state == "anchor-lost":
            drifted += 1
            print(f"  [ANCHOR-LOST]  {label}")
        elif state == "missing":
            drifted += 1
            print(f"  [MISSING]      {label}  ({e.get('error','')})")
        else:
            print(f"  [?{state}]     {label}")
    return drifted


def cmd_specref_drift(args) -> int:
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    want_all = bool(getattr(args, "all", False))
    node_id = getattr(args, "node_id", None)

    try:
        if want_all:
            entries = spec_mod.drift_all(project, patch=bool(getattr(args, "patch", False)))
        else:
            if not node_id:
                raise ValueError("node_id or --all required")
            entries = spec_mod.drift(project, node_id,
                                     patch=bool(getattr(args, "patch", False)))
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "scope": "all" if want_all else node_id,
            "count": len(entries),
            "drifted": sum(1 for e in entries if e.get("state") != "clean"),
            "entries": entries,
        })
    else:
        if not entries:
            target = "all spec-input nodes" if want_all else node_id
            print(f"{target}: no spec-refs")
            return 0
        drifted = _print_drift_text(entries)
        total = len(entries)
        print(f"\n  summary: {total - drifted} clean / {drifted} drifted")

    any_drift = any(e.get("state") != "clean" for e in entries)
    return 2 if any_drift else 0


# ---------------------------------------------------------------------------
# query consumers
# ---------------------------------------------------------------------------


def _parse_slice_filter(raw: Optional[str]
                        ) -> Tuple[Optional[str], Optional[Tuple[int, int]]]:
    """`--slice` accepts either a heading ('## Foo') or a line range ('45-72').

    If it starts with '#' or contains any non-numeric/non-separator
    character, treat it as a heading. Else try to parse as line range.
    """
    if not raw:
        return None, None
    s = raw.strip()
    if s.startswith("#"):
        return s, None
    # Try range first; fall back to heading.
    try:
        lines = spec_mod.parse_lines_arg(s)
        return None, lines
    except ValueError:
        return s, None


def cmd_query_consumers(args) -> int:
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    try:
        heading, lines = _parse_slice_filter(getattr(args, "slice_spec", None))
        results = spec_mod.consumers(
            project, args.spec_path,
            slice_anchor=heading, slice_lines=lines,
        )
    except ValueError as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "spec_path": args.spec_path,
            "slice_filter": getattr(args, "slice_spec", None),
            "count": len(results),
            "consumers": results,
        })
        return 0

    if not results:
        slice_bit = f" (slice {getattr(args,'slice_spec',None)})" if getattr(args, "slice_spec", None) else ""
        print(f"no consumers for {args.spec_path}{slice_bit}")
        return 0
    print(f"{len(results)} consumer(s) of {args.spec_path}:")
    for c in results:
        print(f"  {c['node']}  [{c['status']}]  {c['title']}")
        for sl in c["slices"]:
            where = sl.get("anchor") or f"L{sl['lines'][0]}-L{sl['lines'][1]}"
            print(f"      {where}  ({sl['slice_sha'][:12]})"
                  + (f"  — {sl['why']}" if sl.get("why") else ""))
    return 0
