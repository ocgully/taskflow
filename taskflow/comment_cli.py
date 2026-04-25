"""CLI handler functions for `taskflow comment ...` (HW-0033).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Christopher wires the subparser + dispatch after this lands.

Every handler takes an `args` namespace (argparse-style) and returns an
int exit code, matching the rest of `cli.py`.

Exit codes:
    0 — success
    1 — unknown comment / malformed input / project not loadable

Suggested subparser wiring (drop into `_build_parser` in `cli.py`):

    from taskflow import comment_cli as comment_cli_mod

    cp = sub.add_parser("comment",
                        help="Comment threads on nodes or spec files (HW-0033)")
    csub = cp.add_subparsers(dest="comment_cmd", required=True)

    cp_post = csub.add_parser("post", help="Post a new comment")
    cp_post.add_argument("target", help="Node id (e.g. HW-0042) or spec path")
    g = cp_post.add_mutually_exclusive_group()
    g.add_argument("--anchor", choices=["whole-file"], default=None,
                   help="Whole-file anchor (default if no --heading / --lines)")
    g.add_argument("--heading", default=None,
                   help="Heading text or slug, e.g. '## Flow Network'")
    g.add_argument("--lines", default=None,
                   help="Line range, e.g. '45-72'")
    cp_post.add_argument("--explicit-anchor", default=None,
                         help="Named escape-hatch anchor (<!-- anchor:NAME -->)")
    cp_post.add_argument("--body", required=True, help="Comment body")
    cp_post.add_argument("--format", choices=["text", "json"], default="text")
    cp_post.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_post(a))

    cp_ls = csub.add_parser("ls", help="List threads for a target")
    cp_ls.add_argument("target")
    cp_ls.add_argument("--status", choices=["open", "resolved", "all"],
                       default="open")
    cp_ls.add_argument("--format", choices=["text", "json"], default="text")
    cp_ls.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_ls(a))

    cp_res = csub.add_parser("resolve", help="Resolve a comment thread")
    cp_res.add_argument("comment_id")
    cp_res.add_argument("--reason", default=None)
    cp_res.add_argument("--format", choices=["text", "json"], default="text")
    cp_res.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_resolve(a))

    cp_reo = csub.add_parser("reopen", help="Re-open a resolved comment thread")
    cp_reo.add_argument("comment_id")
    cp_reo.add_argument("--format", choices=["text", "json"], default="text")
    cp_reo.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_reopen(a))

    cp_edit = csub.add_parser("edit", help="Edit a comment body")
    cp_edit.add_argument("comment_id")
    cp_edit.add_argument("--body", required=True)
    cp_edit.add_argument("--format", choices=["text", "json"], default="text")
    cp_edit.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_edit(a))

    cp_pro = csub.add_parser("promote", help="Promote a thread to a review node")
    cp_pro.add_argument("comment_id")
    cp_pro.add_argument("--title", required=True)
    cp_pro.add_argument("--body-prefix", default="")
    cp_pro.add_argument("--format", choices=["text", "json"], default="text")
    cp_pro.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_promote(a))

    cp_orph = csub.add_parser("orphans",
                              help="Threads whose anchors failed reconciliation")
    cp_orph.add_argument("--format", choices=["text", "json"], default="text")
    cp_orph.set_defaults(func=lambda a: comment_cli_mod.cmd_comment_orphans(a))
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskflow import comment as comment_mod


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


_LINES_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")


def _parse_lines(raw: str) -> Tuple[int, int]:
    m = _LINES_RE.match(raw or "")
    if not m:
        raise ValueError(f"bad --lines value: {raw!r} (want 'start-end')")
    start, end = int(m.group(1)), int(m.group(2))
    if start < 1 or end < start:
        raise ValueError(f"bad line range: {start}-{end}")
    return (start, end)


def _anchor_kwargs_from_args(args) -> Dict[str, Any]:
    """Pick heading vs lines vs whole-file. Returns kwargs for comment.post()."""
    heading = getattr(args, "heading", None)
    lines_raw = getattr(args, "lines", None)
    anchor_opt = getattr(args, "anchor", None)
    explicit = getattr(args, "explicit_anchor", None)

    selected = [x for x in (heading, lines_raw, anchor_opt) if x]
    if len(selected) > 1:
        raise ValueError(
            "pass at most one of --heading / --lines / --anchor whole-file"
        )

    if heading:
        return {
            "anchor_type": comment_mod.ANCHOR_HEADING,
            "heading": heading,
            "explicit_anchor": explicit,
        }
    if lines_raw:
        return {
            "anchor_type": comment_mod.ANCHOR_LINE_RANGE,
            "lines": _parse_lines(lines_raw),
            "explicit_anchor": explicit,
        }
    # default: whole-file
    return {
        "anchor_type": comment_mod.ANCHOR_WHOLE_FILE,
        "explicit_anchor": explicit,
    }


# ---------------------------------------------------------------------------
# pretty print one thread
# ---------------------------------------------------------------------------


def _thread_one_liner(t) -> str:
    anchor = t.anchor or {}
    typ = anchor.get("type") or "whole-file"
    rec = t.reconciled_anchor or {}
    state = rec.get("_state") or "?"
    state_badge = {
        "resolved": "ok",
        "drifted": "DRIFT",
        "orphaned": "ORPHAN",
    }.get(state, state.upper())
    if typ == comment_mod.ANCHOR_HEADING:
        slug = anchor.get("heading_slug") or "?"
        where = f"#{slug}"
    elif typ == comment_mod.ANCHOR_LINE_RANGE:
        lines = (rec.get("lines") if rec.get("_state") == "drifted"
                 else anchor.get("lines")) or [0, 0]
        where = f"L{lines[0]}-L{lines[1]}"
    else:
        where = "(whole-file)"
    target = t.target or {}
    tgt_label = target.get("node") or target.get("spec") or "?"
    status = "[resolved]" if t.resolved else "[open]    "
    actor = t.actor or "?"
    body_trim = (t.body or "").replace("\n", " ").strip()
    if len(body_trim) > 80:
        body_trim = body_trim[:77] + "..."
    return (f"  {t.id}  {status}  {tgt_label:12}  {where:16}  [{state_badge}]  "
            f"{actor}  {t.ts}\n    {body_trim}")


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


def cmd_comment_post(args) -> int:
    try:
        project = _project(args)
        kwargs = _anchor_kwargs_from_args(args)
        body = getattr(args, "body", None) or ""
        if not body.strip():
            raise ValueError("--body is required (non-empty)")
        thread = comment_mod.post(project, args.target, body,
                                  actor=_actor_from_env(), **kwargs)
    except (ValueError, KeyError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "comment.post", "thread": thread.to_dict()})
    else:
        print(f"posted {thread.id} on {args.target}")
        anchor = thread.anchor
        typ = anchor.get("type")
        if typ == comment_mod.ANCHOR_HEADING:
            print(f"  anchor: heading '{anchor.get('heading_slug')}'  "
                  f"lines={anchor.get('lines')}")
        elif typ == comment_mod.ANCHOR_LINE_RANGE:
            print(f"  anchor: lines L{anchor['lines'][0]}-L{anchor['lines'][1]}")
        else:
            print(f"  anchor: whole-file")
        print(f"  body:   {thread.body}")
    return 0


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


def cmd_comment_ls(args) -> int:
    try:
        project = _project(args)
        threads = comment_mod.threads_for_target(project, args.target)
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    wanted = (getattr(args, "status", "open") or "open").lower()
    if wanted == "open":
        threads = [t for t in threads if not t.resolved]
    elif wanted == "resolved":
        threads = [t for t in threads if t.resolved]
    # else "all"

    if _format(args) == "json":
        _print_json({
            "target": args.target,
            "status": wanted,
            "count": len(threads),
            "threads": [t.to_dict() for t in threads],
        })
        return 0

    if not threads:
        print(f"{args.target}: no {wanted} comments")
        return 0
    print(f"{args.target}: {len(threads)} {wanted} comment(s)")
    for t in threads:
        print(_thread_one_liner(t))
    return 0


# ---------------------------------------------------------------------------
# resolve / reopen / edit
# ---------------------------------------------------------------------------


def cmd_comment_resolve(args) -> int:
    try:
        project = _project(args)
        thread = comment_mod.resolve(project, args.comment_id,
                                     reason=getattr(args, "reason", None),
                                     actor=_actor_from_env())
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "comment.resolve", "thread": thread.to_dict()})
    else:
        print(f"resolved {thread.id}")
        if thread.resolve_reason:
            print(f"  reason: {thread.resolve_reason}")
    return 0


def cmd_comment_reopen(args) -> int:
    try:
        project = _project(args)
        thread = comment_mod.reopen(project, args.comment_id,
                                    actor=_actor_from_env())
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "comment.reopen", "thread": thread.to_dict()})
    else:
        print(f"reopened {thread.id}")
    return 0


def cmd_comment_edit(args) -> int:
    try:
        project = _project(args)
        thread = comment_mod.edit(project, args.comment_id, args.body,
                                  actor=_actor_from_env())
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "comment.edit", "thread": thread.to_dict()})
    else:
        print(f"edited {thread.id}")
        print(f"  body: {thread.body}")
    return 0


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def cmd_comment_promote(args) -> int:
    try:
        project = _project(args)
        result = comment_mod.promote(project, args.comment_id, args.title,
                                     body_prefix=getattr(args, "body_prefix", "") or "",
                                     actor=_actor_from_env())
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({"op": "comment.promote", **result})
    else:
        print(f"promoted {result['thread_id']} -> review node {result['review_node']}")
        ref = result.get("references") or {}
        if ref.get("to"):
            note = ref.get("note")
            extra = f" ({note})" if note else ""
            print(f"  references: {ref['to']}{extra}")
    return 0


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------


def cmd_comment_orphans(args) -> int:
    try:
        project = _project(args)
        orph = comment_mod.orphans(project)
    except (ValueError, FileNotFoundError) as e:
        print(f"taskflow: {e}", file=sys.stderr)
        return 1

    if _format(args) == "json":
        _print_json({
            "count": len(orph),
            "threads": [t.to_dict() for t in orph],
        })
        return 0

    if not orph:
        print("no orphaned comments — all anchors resolve")
        return 0
    print(f"{len(orph)} orphan(s) — anchors could not be resolved:")
    for t in orph:
        print(_thread_one_liner(t))
    return 0
