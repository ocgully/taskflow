"""CLI handler for `taskflow query markov` (HW-0036).

Kept separate so `hopewell/cli.py` isn't touched in this ticket —
matches the pattern established by `cycle_time_cli.py` for HW-0038.
Christopher wires the subparser / `cmd_query` dispatch after this
lands.

Argparse wiring (add to `cli.py`'s `sub.add_parser("query", ...)` block
and the `cmd_query` dispatch):

    # cli.py:1211 — extend the query `subject` choices:
    sp.add_argument("subject", choices=[..., "markov"])

    # extra markov-only flags (won't collide with existing ones):
    sp.add_argument("--window", default="30d",
                    help="markov: time window (all|30d|7d|1d|release-tag)")
    sp.add_argument("--no-singletons", dest="include_singletons",
                    action="store_false", default=True,
                    help="markov: exclude single-traversal items from "
                         "base-rate total")
    sp.add_argument("--top", type=int, default=10,
                    help="markov: top-N rework edges to list in text mode")
    sp.add_argument("--by", choices=["probability", "count", "time_weight"],
                    default="probability",
                    help="markov: ranking metric for --top table")

    # cli.py:609 — add to cmd_query dispatch:
    elif args.subject == "markov":
        from taskflow import markov_cli as mk_cli
        return mk_cli.cmd_query_markov(args)

The handler returns an int exit code like the rest of `cli.py`:

    0 — success
    1 — project load / unknown flag

Standalone invocation (smoke-testing before cli.py wiring):

    python -m hopewell.markov_cli --window all
    python -m hopewell.markov_cli --window 30d --format json --top 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from taskflow import markov as markov_mod


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
    return getattr(args, "format", None) or "text"


def _pct(x: float) -> str:
    return f"{int(round(x * 100))}%"


# ---------------------------------------------------------------------------
# query markov
# ---------------------------------------------------------------------------


def cmd_query_markov(args) -> int:
    """`taskflow query markov [--window W] [--no-singletons] [--top N] [--by K]`.

    Prints an aggregate summary + per-edge transition probabilities.
    JSON mode (`--format json`) dumps the full data structure as-is;
    this is also what the web UI's `/api/markov` endpoint serves.
    """
    try:
        project = _project(args)
    except Exception as e:  # noqa: BLE001
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    window = getattr(args, "window", None) or "30d"
    include_singletons = getattr(args, "include_singletons", True)
    if include_singletons is None:
        include_singletons = True
    top_n = int(getattr(args, "top", 10) or 10)
    by = getattr(args, "by", "probability") or "probability"

    data = markov_mod.compute(
        project, window=window, include_singletons=include_singletons,
    )

    if _format(args) == "json":
        _print_json(data)
        return 0

    _print_markov_text(data, top_n=top_n, by=by)
    return 0


def _print_markov_text(data: Dict[str, Any], *, top_n: int, by: str) -> None:
    win = data["window"]
    if data["window_requested"] != win:
        win = f"{win}  (requested {data['window_requested']})"
    print(f"markov traversal analytics  (window: {win})")
    print(
        f"  items:         {data['total_items']} total"
        f"  ({data['contributing_items']} contributing, "
        f"{data['singleton_items']} single-traversal)"
    )
    print(f"  transitions:   {data['total_transitions']}")
    print(
        f"  rework events: {data['rework_events']}   "
        f"(rework-ratio {_pct(data['rework_ratio'])})"
    )

    edges = data.get("edges") or []
    if not edges:
        print("  no edges observed in window.")
        return

    print(f"\n  edges (sorted by count):")
    hdr = (f"    {'from':<22} {'to':<22}  {'count':>5}  "
           f"{'P':>5}  {'mean-dwell':>10}  {'kind':>4}")
    print(hdr)
    for e in edges[:25]:
        kind = "back" if e["is_back"] else "fwd"
        conf = ""
        if e["classification_source"] == "observed":
            conf = f"*"
        print(
            f"    {e['from']:<22} {e['to']:<22}  "
            f"{e['count']:>5}  {_pct(e['probability']):>5}  "
            f"{e['mean_dwell']:>10}  {kind:>4}{conf}"
        )
    if len(edges) > 25:
        print(f"    ... ({len(edges) - 25} more)")

    backs = markov_mod.top_rework_edges(data, n=top_n, by=by)
    if backs:
        print(f"\n  top {len(backs)} rework edges by {by}:")
        hdr2 = (f"    {'from':<22} {'to':<22}  {'count':>5}  "
                f"{'P':>5}  {'P×dwell':>10}")
        print(hdr2)
        for e in backs:
            print(
                f"    {e['from']:<22} {e['to']:<22}  "
                f"{e['count']:>5}  {_pct(e['probability']):>5}  "
                f"{e['time_weight']:>10}"
            )

    # Footnote for `*` markers
    if any(e.get("classification_source") == "observed" for e in edges):
        print("\n  * classification inferred from observed traversals "
              "(edge not in declared routes)")


# ---------------------------------------------------------------------------
# standalone entry (`python -m hopewell.markov_cli`)
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hopewell.markov_cli",
        description="Markov / rework analytics (HW-0036). "
                    "Mirrors `taskflow query markov` (pending cli.py wiring).",
    )
    p.add_argument("--project-root", default=None,
                   help="Project root (auto-detected from CWD if omitted)")
    p.add_argument("--window", default="30d",
                   help="Time window: all|30d|7d|1d|release-tag")
    p.add_argument("--no-singletons", dest="include_singletons",
                   action="store_false", default=True,
                   help="Exclude single-traversal items from base rate")
    p.add_argument("--top", type=int, default=10,
                   help="Top-N rework edges to list in text mode")
    p.add_argument("--by", choices=["probability", "count", "time_weight"],
                   default="probability",
                   help="Ranking metric for --top table")
    p.add_argument("--format", choices=["text", "json"], default="text")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return cmd_query_markov(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
