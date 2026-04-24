"""CLI handler for `hopewell flow trace <node_id>` (HW-0035).

Kept out of `hopewell/cli.py` so the core parser file stays slim. The
wiring snippet below is what `cli.py` grows inside its `flow` subparser
block:

    from hopewell import flow_trace_cli as flow_trace_cli_mod

    fp = fsub.add_parser("trace",
        help="Show a work item's traversal (chronological, across executors)")
    fp.add_argument("node_id")
    fp.add_argument("--format", choices=["text", "json", "mermaid"],
                    default="text")
    fp.add_argument("--compact", action="store_true",
                    help="(text) drop header/footer; just the event lines")
    fp.set_defaults(func=lambda a: flow_trace_cli_mod.cmd_flow_trace(a))

Exit codes:
    0 — success
    1 — unknown node / malformed input

Output contracts:
    --format text     chronological event log, one per line, plus a
                      header summarising event count + visited chain.
    --format json     the full trace dict (see flow_trace.trace).
    --format mermaid  a sequenceDiagram block (no code fences — pipe
                      into a file or a mermaid renderer yourself).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from hopewell import flow_trace as flow_trace_mod


def _project(args):
    from hopewell.project import Project
    start = Path(args.project_root).resolve() if getattr(args, "project_root", None) else None
    return Project.load(start)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def cmd_flow_trace(args) -> int:
    try:
        project = _project(args)
        tr = flow_trace_mod.trace(project, args.node_id)
    except FileNotFoundError as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 1

    fmt = getattr(args, "format", "text") or "text"
    if fmt == "json":
        _print_json(tr)
        return 0
    if fmt == "mermaid":
        sys.stdout.write(flow_trace_mod.render_mermaid(tr))
        return 0
    # Default: text.
    compact = bool(getattr(args, "compact", False))
    sys.stdout.write(flow_trace_mod.render_text(tr, compact=compact))
    return 0
