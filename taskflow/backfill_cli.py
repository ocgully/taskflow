"""CLI handler for `taskflow backfill`.

CLI wiring (to be added to hopewell/cli.py by a follow-up):

    # backfill
    sp = sub.add_parser(
        "backfill",
        help="Populate .hopewell/nodes/ from git history / issues / TODO / specs",
    )
    sp.add_argument(
        "--source", default="git,todo,spec",
        help="Comma-separated: git | issues | todo | spec | all "
             "(default: git,todo,spec — issues is opt-in)",
    )
    sp.add_argument("--since", default=None,
                    help="ISO date; commits/issues older than this are ignored "
                         "(default: 180 days ago)")
    sp.add_argument("--github", action="store_true",
                    help="Include GitHub issues via `gh` CLI (implies --source issues)")
    sp.add_argument("--github-repo", default=None,
                    help="owner/name (defaults to gh's autodetection)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--limit", type=int, default=None,
                    help="Cap commits scanned (safety valve for huge histories)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_backfill)

    # init (extension)
    # In the existing init parser, add:
    sp.add_argument("--no-backfill", action="store_true",
                    help="Skip the auto-backfill step on init")
    # (Auto-backfill fires when .hopewell/nodes/ is empty and discoverable
    # sources are present. Use --no-backfill to opt out.)

The `cmd_backfill` entry point below is importable by cli.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, List

from taskflow import backfill as backfill_mod


def _project(args):
    from taskflow.project import Project
    start = Path(args.project_root).resolve() if args.project_root else None
    return Project.load(start)


def _parse_sources(raw: str, *, github: bool) -> List[str]:
    parts = [p.strip() for p in (raw or "").split(",") if p.strip()]
    if github and "issues" not in parts and "all" not in parts:
        parts.append("issues")
    return parts or ["git", "todo", "spec"]


def cmd_backfill(args) -> int:
    project = _project(args)
    sources = _parse_sources(getattr(args, "source", "git,todo,spec"),
                             github=bool(getattr(args, "github", False)))
    report = backfill_mod.run(
        project,
        sources=sources,
        since_iso=getattr(args, "since", None),
        github_repo=getattr(args, "github_repo", None),
        dry_run=bool(getattr(args, "dry_run", False)),
        limit=getattr(args, "limit", None),
    )

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        out: dict = {
            "created": list(report.created),
            "by_source": dict(report.by_source),
            "skipped_ledger": list(report.skipped_ledger),
            "skipped_existing": [
                {"source_id": sid, "node_id": nid}
                for sid, nid in report.skipped_existing
            ],
            "conflicts": list(report.conflicts),
            "dry_run": bool(getattr(args, "dry_run", False)),
        }
        sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(backfill_mod.format_report(
            report, dry_run=bool(getattr(args, "dry_run", False)),
        ) + "\n")
    return 0
