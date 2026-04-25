r"""Claude Code hook entry points for Hopewell (HW-0040).

Wires the Claude Code agent runtime (SessionStart / PreToolUse /
PostToolUse / Stop / UserPromptSubmit / SubagentStop) into Hopewell's
flow runtime, so that `flow.enter` / `flow.leave` events fire
automatically whenever a Claude agent picks up or finishes work on a
node — without the human having to run `taskflow flow push/ack/enter/
leave` by hand.

Category-C territory (HW-0050 boundary note)
--------------------------------------------

This module is the ONLY place where Hopewell does "Category C" hook
work: **context injection into a running AI agent session**. The
`taskflow hooks install --full` git hooks (HW-0050, see
`hopewell/hooks.py`) deliberately do NOT do Category C — a git hook
runs in the shell without any knowledge that a Claude Code session is
attached, so it can't prepend Pedia context, resume active claims, or
inject spec slices into an agent prompt. Those injections require the
Claude Code runtime (SessionStart / UserPromptSubmit / PreToolUse).

So:

  * Categories A (bookkeeping) + B (declared gates) live in git hooks
    (`hopewell/hooks.py`, templates in `hopewell/hook_templates.py`).
  * Category C (context injection) lives here.
  * Category D (routing) and E (judgment) stay with the orchestrator.

Keeping this split explicit prevents the two hook surfaces from racing
to enforce the same invariant or, worse, disagreeing about it.

--------------------------------------------------------------------
Design decisions (document, then locked in)
--------------------------------------------------------------------

1. **How does a running session know which HW-ids it's working on?**

   Hybrid, in precedence order:

     a. **Active-marker file** `.hopewell/claude/active.json`
        Structure:
          {
            "session_id":  "abc123...",     # Claude Code session id
            "nodes":       ["HW-0040"],     # ids the session is on
            "executor":    "engineer",      # optional override
            "opened_at":   "2026-04-23T..." # iso8601
          }
        Written by the `pre-tool-use` handler when it detects an
        `HW-NNNN` reference in a Task/Agent prompt. Cleared by `stop`.

     b. **Regex over structured hook fields** — `HW-\d+` scanned in
        (in order) `tool_input.prompt`, `tool_input.description`,
        `prompt` (for UserPromptSubmit), `cwd`-derived branch name
        (`feat/HW-NNNN-*`).

     c. **`HOPEWELL_NODES` env var** — comma-separated ids.

   The active-marker wins over the regex scan (cheap, deterministic).
   If neither finds an id, the handler is a silent no-op.

2. **Who is the executor?**

   Precedence:
     a. `HOPEWELL_ACTOR` env var (always wins — explicit beats heuristic)
     b. `@<persona>` mention extracted from the tool prompt
     c. `executor` field in active.json
     d. `core.default_executor` in `.hopewell/config.toml` (if set)
     e. fall back to `"agent"` (still a no-op if the network doesn't
        know that id — flow.enter is guarded by `_require_executor`)

3. **Event mapping**

     Claude Code event      | Hopewell action
     -----------------------|---------------------------------------
     SessionStart           | (no-op by default; write session marker
                            |  if `.hopewell/claude/` exists)
     UserPromptSubmit       | Scan prompt for HW-NNNN, stash into
                            |  `pending_nodes` on the active marker
     PreToolUse (Task|Agent)| For each HW-NNNN in tool_input,
                            |  flow.enter <node> --executor <resolved>
     PostToolUse (Task)     | flow.leave for each node the matching
                            |  PreToolUse entered (paired by tool_use_id)
     Stop / SubagentStop    | flow.leave on every currently-open
                            |  location for session's recorded nodes;
                            |  clear `.hopewell/claude/active.json`
     SessionEnd             | Clear session marker; flow.leave any
                            |  remaining open locations
     (others)               | Silent no-op

   Errors (malformed input, project not found, unknown executor):
   swallowed. Hooks MUST exit 0 and print nothing — Claude Code hooks
   should never block the agent loop, and Hopewell runs on top of it.

--------------------------------------------------------------------
Entry points
--------------------------------------------------------------------

Each function below:
  * reads the hook JSON from stdin
  * returns an int exit code (0 always — we fail silent)
  * emits zero or more flow events via `hopewell.flow`
  * is safe to run outside a hopewell project (it just returns 0)

They are dispatched from `hopewell.claude_hooks_cli` (see that module
for argparse wiring; `hopewell/cli.py` is integrated separately).

Stdlib only.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

HW_ID_RE = re.compile(r"\b([A-Z]{2,6}-\d{3,6})\b")
ACTOR_MENTION_RE = re.compile(r"@([a-zA-Z0-9_\-]+)")
BRANCH_HW_RE = re.compile(r"(?:feat|fix|chore|hw)[/-]([A-Z]{2,6}-\d{3,6})", re.IGNORECASE)

ACTIVE_MARKER_RELPATH = Path("claude") / "active.json"


# ---------------------------------------------------------------------------
# safe I/O — every hook must fail silent
# ---------------------------------------------------------------------------


def _read_hook_input() -> Dict[str, Any]:
    """Parse stdin JSON; on any error return an empty dict."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _try_load_project(cwd: Optional[Path] = None):
    """Load a Hopewell Project from cwd (or os.getcwd()).

    Returns None silently if no `.hopewell/` is found or loading fails.
    Never raises.
    """
    try:
        from taskflow.project import Project
        start = cwd if cwd is not None else Path.cwd()
        return Project.load(Path(start).resolve())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# id + executor extraction
# ---------------------------------------------------------------------------


def _scan_hw_ids(text: Optional[str]) -> List[str]:
    if not text:
        return []
    seen: List[str] = []
    for m in HW_ID_RE.finditer(text):
        nid = m.group(1)
        if nid not in seen:
            seen.append(nid)
    return seen


def _branch_name(cwd: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            name = (out.stdout or "").strip()
            return name or None
    except Exception:
        return None
    return None


def extract_node_ids(payload: Dict[str, Any], cwd: Path) -> List[str]:
    """Pull HW-NNNN ids out of the hook payload.

    Searched in order (dedup-preserving):
      1. tool_input.prompt
      2. tool_input.description
      3. tool_input.command (for Bash hooks)
      4. prompt (UserPromptSubmit)
      5. current git branch name
      6. $HOPEWELL_NODES env var (comma-separated)
    """
    ids: List[str] = []
    tool_input = payload.get("tool_input") or {}
    for key in ("prompt", "description", "command"):
        for nid in _scan_hw_ids(tool_input.get(key) if isinstance(tool_input, dict) else None):
            if nid not in ids:
                ids.append(nid)
    for nid in _scan_hw_ids(payload.get("prompt")):
        if nid not in ids:
            ids.append(nid)
    branch = _branch_name(cwd)
    if branch:
        m = BRANCH_HW_RE.search(branch)
        if m:
            nid = m.group(1).upper()
            if nid not in ids:
                ids.append(nid)
    env_nodes = os.environ.get("HOPEWELL_NODES") or ""
    for chunk in env_nodes.split(","):
        chunk = chunk.strip()
        if chunk and chunk not in ids:
            ids.append(chunk)
    return ids


def resolve_executor(payload: Dict[str, Any], project, fallback: str = "agent") -> str:
    """Decide which executor the running session represents.

    Precedence: HOPEWELL_ACTOR env > @mention in tool prompt >
    active-marker executor > project config default > fallback.
    """
    def _norm(name: str) -> str:
        # Executor ids in the network can be either bare (`archived`,
        # `inbox`) or @-prefixed (`@architect`). Prefer the exact form
        # that appears in the project's executor set; otherwise return
        # the normalized candidate as-is.
        candidate = name.strip()
        if not candidate:
            return candidate
        try:
            from taskflow import network as net_mod
            known = set(net_mod.load_network(project.root).executors.keys())
        except Exception:
            return candidate
        if candidate in known:
            return candidate
        # Try with/without the `@` prefix.
        alt = candidate[1:] if candidate.startswith("@") else f"@{candidate}"
        if alt in known:
            return alt
        return candidate

    env_actor = os.environ.get("HOPEWELL_ACTOR")
    if env_actor:
        return _norm(env_actor)

    tool_input = payload.get("tool_input") or {}
    for key in ("prompt", "description"):
        text = tool_input.get(key) if isinstance(tool_input, dict) else None
        if text:
            m = ACTOR_MENTION_RE.search(text)
            if m:
                return _norm(f"@{m.group(1)}")

    marker = read_active_marker(project)
    if marker and marker.get("executor"):
        return _norm(str(marker["executor"]))

    try:
        cfg = getattr(project, "config", None)
        if cfg is not None:
            # Config object exposes .core.default_executor in newer hopewell;
            # fall back to raw dict access defensively.
            val = None
            core = getattr(cfg, "core", None)
            if core is not None:
                val = getattr(core, "default_executor", None)
            if val:
                return _norm(str(val))
    except Exception:
        pass

    return _norm(fallback)


# ---------------------------------------------------------------------------
# active-marker file — .hopewell/claude/active.json
# ---------------------------------------------------------------------------


def _active_marker_path(project) -> Optional[Path]:
    if project is None:
        return None
    try:
        return project.hw_dir / ACTIVE_MARKER_RELPATH
    except Exception:
        return None


def read_active_marker(project) -> Dict[str, Any]:
    p = _active_marker_path(project)
    if p is None or not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_active_marker(project, data: Dict[str, Any]) -> None:
    p = _active_marker_path(project)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def clear_active_marker(project) -> None:
    p = _active_marker_path(project)
    if p is None:
        return
    try:
        if p.is_file():
            p.unlink()
    except Exception:
        pass


def _record_enter(project, node_id: str, executor: str, tool_use_id: Optional[str]) -> None:
    """Append to active.json's `open_locations` so a later Stop/
    PostToolUse can pair-close them."""
    marker = read_active_marker(project) or {}
    opens: List[Dict[str, Any]] = list(marker.get("open_locations") or [])
    opens.append({
        "node": node_id,
        "executor": executor,
        "tool_use_id": tool_use_id,
        "entered_at": _now(),
    })
    marker["open_locations"] = opens
    nodes = list(marker.get("nodes") or [])
    if node_id not in nodes:
        nodes.append(node_id)
    marker["nodes"] = nodes
    if "opened_at" not in marker:
        marker["opened_at"] = _now()
    write_active_marker(project, marker)


def _pop_opens_for(
    project,
    *,
    tool_use_id: Optional[str] = None,
    all_remaining: bool = False,
) -> List[Dict[str, Any]]:
    """Remove and return open_location records matching a tool_use_id
    (or all of them if `all_remaining`)."""
    marker = read_active_marker(project) or {}
    opens: List[Dict[str, Any]] = list(marker.get("open_locations") or [])
    if not opens:
        return []
    if all_remaining:
        marker["open_locations"] = []
        write_active_marker(project, marker)
        return opens
    matched: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for entry in opens:
        if tool_use_id and entry.get("tool_use_id") == tool_use_id:
            matched.append(entry)
        else:
            remaining.append(entry)
    marker["open_locations"] = remaining
    write_active_marker(project, marker)
    return matched


# ---------------------------------------------------------------------------
# flow dispatch helpers (always swallow errors)
# ---------------------------------------------------------------------------


def _safe_enter(project, node_id: str, executor: str, *,
                actor: Optional[str], reason: Optional[str]) -> bool:
    try:
        from taskflow import flow as flow_mod
        return flow_mod.enter(
            project, node_id, executor,
            actor=actor, reason=reason,
        )
    except Exception:
        return False


def _safe_leave(project, node_id: str, executor: str, *,
                actor: Optional[str], reason: Optional[str]) -> bool:
    try:
        from taskflow import flow as flow_mod
        return flow_mod.leave(
            project, node_id, executor,
            actor=actor, reason=reason,
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# hook entry points
# ---------------------------------------------------------------------------


def _session_start_preamble(project) -> str:
    """Compose the session-preamble text injected via stdout on SessionStart.

    Claude Code's SessionStart hook injects the hook command's stdout
    into the session as additional context. We use that to remind the
    running agent that this project has a Hopewell flow network and the
    convention is to route through `@orchestrator` unless overridden.

    The preamble is intentionally short (a handful of lines) — it runs
    every session start, so the token cost should amortize across all
    the saved re-discovery work downstream.

    Content:
      * orchestrator-first routing rule
      * `/o` and `--direct` escape hatches
      * active claims (from taskflow resume, if cheap to read)
      * any open reconciliation reviews

    If anything errors during composition, return "" so SessionStart
    stays silent — we MUST NOT block the session.
    """
    lines: List[str] = []
    lines.append("[hopewell preamble] project has a Hopewell flow network; orchestrator-first routing is the convention.")
    lines.append("  - Default: route every request through @orchestrator (use /o <request> or /orchestrate <request>)")
    lines.append("  - Override: pass --direct to invoke a specific agent, or the agent may proceed for trivial reads")
    lines.append("  - Domain agents invoked directly for substantive work should redirect to @orchestrator")

    # Best-effort: surface active claims and open reviews. All failures
    # degrade to "nothing surfaced" — never raise out of a hook.
    try:
        active_nodes: List[str] = []
        marker = read_active_marker(project) or {}
        for nid in marker.get("nodes") or []:
            if isinstance(nid, str) and nid not in active_nodes:
                active_nodes.append(nid)
        for entry in marker.get("open_locations") or []:
            nid = (entry or {}).get("node") if isinstance(entry, dict) else None
            if isinstance(nid, str) and nid not in active_nodes:
                active_nodes.append(nid)
        if active_nodes:
            lines.append(f"  - Active claims on this session: {', '.join(active_nodes)}")
        else:
            lines.append("  - Active claims on this session: (none - run `taskflow resume` to pick something up)")
    except Exception:
        pass

    # Open reconciliation reviews, if cheaply queryable.
    try:
        from taskflow import network as net_mod
        net = net_mod.load_network(project.root)
        # Scan nodes for any open "downstream-review" / "reconciliation"
        # items. Keep this defensive — the network shape varies across
        # versions.
        open_reviews: List[str] = []
        nodes_iter = getattr(net, "nodes", None)
        if nodes_iter is not None:
            # `nodes` may be a dict {id: node} or a list.
            iterable = nodes_iter.values() if hasattr(nodes_iter, "values") else nodes_iter
            for node in iterable:
                status = (getattr(node, "status", "") or "").lower()
                components = getattr(node, "components", None) or []
                if isinstance(components, (list, tuple, set)):
                    comps_lower = {str(c).lower() for c in components}
                else:
                    comps_lower = set()
                is_review = (
                    "review" in comps_lower
                    or "reconciliation" in comps_lower
                    or "downstream-review" in comps_lower
                )
                is_open = status in {"ready", "doing", "review", "idea"}
                if is_review and is_open:
                    nid = getattr(node, "id", None)
                    if nid:
                        open_reviews.append(str(nid))
                if len(open_reviews) >= 5:
                    break
        if open_reviews:
            lines.append(f"  - Open reconciliation/review nodes: {', '.join(open_reviews)}")
    except Exception:
        pass

    return "\n".join(lines) + "\n"


def on_session_start() -> int:
    """SessionStart: record the session id on the active marker AND
    emit the orchestrator-first preamble on stdout (which Claude Code
    injects into the session as additional context).

    Silent no-op if no Hopewell project is present — preamble only
    applies where the flow network exists.
    """
    payload = _read_hook_input()
    project = _try_load_project()
    if project is None:
        return 0
    session_id = payload.get("session_id")
    marker = read_active_marker(project) or {}
    if session_id:
        marker["session_id"] = session_id
    marker.setdefault("opened_at", _now())
    # Don't clobber pre-existing open_locations from a resume.
    write_active_marker(project, marker)

    # Emit the orchestrator-first preamble. Any failure in composition
    # falls through silently — SessionStart must never block the session.
    try:
        preamble = _session_start_preamble(project)
        if preamble:
            sys.stdout.write(preamble)
            sys.stdout.flush()
    except Exception:
        pass
    return 0


def on_user_prompt_submit() -> int:
    """UserPromptSubmit: scan prompt for HW-NNNN; stash as pending."""
    payload = _read_hook_input()
    project = _try_load_project(Path(payload.get("cwd") or os.getcwd()))
    if project is None:
        return 0
    prompt = payload.get("prompt") or ""
    ids = _scan_hw_ids(prompt)
    if not ids:
        return 0
    marker = read_active_marker(project) or {}
    pending = list(marker.get("pending_nodes") or [])
    for nid in ids:
        if nid not in pending:
            pending.append(nid)
    marker["pending_nodes"] = pending
    write_active_marker(project, marker)
    return 0


def on_pre_tool_use() -> int:
    """PreToolUse: if Task/Agent tool referencing an HW-id, flow.enter."""
    payload = _read_hook_input()
    cwd = Path(payload.get("cwd") or os.getcwd())
    project = _try_load_project(cwd)
    if project is None:
        return 0
    tool_name = payload.get("tool_name") or ""
    # Only act on agent-dispatching tools. Matcher in settings.json
    # should already filter these, but double-check defensively.
    if tool_name not in ("Task", "Agent", "SubagentStart"):
        return 0

    node_ids = extract_node_ids(payload, cwd)
    if not node_ids:
        # Fall back to pending_nodes captured at UserPromptSubmit.
        marker = read_active_marker(project) or {}
        node_ids = list(marker.get("pending_nodes") or [])
    if not node_ids:
        return 0

    executor = resolve_executor(payload, project)
    actor = os.environ.get("HOPEWELL_ACTOR") or f"claude-code:{executor}"
    tool_use_id = payload.get("tool_use_id")

    for nid in node_ids:
        ok = _safe_enter(
            project, nid, executor,
            actor=actor,
            reason=f"claude-code PreToolUse:{tool_name}",
        )
        # Record regardless of whether flow.enter was a no-op — we still
        # want the PostToolUse/Stop to consider closing the location.
        _record_enter(project, nid, executor, tool_use_id)
        _ = ok  # silently ignore
    return 0


def on_post_tool_use() -> int:
    """PostToolUse: close the locations that the paired PreToolUse opened."""
    payload = _read_hook_input()
    cwd = Path(payload.get("cwd") or os.getcwd())
    project = _try_load_project(cwd)
    if project is None:
        return 0
    tool_name = payload.get("tool_name") or ""
    if tool_name not in ("Task", "Agent", "SubagentStop"):
        return 0

    tool_use_id = payload.get("tool_use_id")
    closing = _pop_opens_for(project, tool_use_id=tool_use_id)
    if not closing:
        return 0
    actor = os.environ.get("HOPEWELL_ACTOR") or "claude-code"
    for entry in closing:
        nid = entry.get("node")
        executor = entry.get("executor")
        if not nid or not executor:
            continue
        _safe_leave(
            project, nid, executor,
            actor=actor,
            reason=f"claude-code PostToolUse:{tool_name}",
        )
    return 0


def on_stop() -> int:
    """Stop / SubagentStop: close every remaining open location for the
    session, then clear the active marker."""
    _ = _read_hook_input()
    project = _try_load_project()
    if project is None:
        return 0
    closing = _pop_opens_for(project, all_remaining=True)
    actor = os.environ.get("HOPEWELL_ACTOR") or "claude-code"
    for entry in closing:
        nid = entry.get("node")
        executor = entry.get("executor")
        if not nid or not executor:
            continue
        _safe_leave(
            project, nid, executor,
            actor=actor, reason="claude-code Stop",
        )
    # Keep session_id + nodes for the next SessionStart-on-resume, but
    # clear pending queue so we don't replay stale prompts.
    marker = read_active_marker(project) or {}
    marker.pop("pending_nodes", None)
    write_active_marker(project, marker)
    return 0


def on_session_end() -> int:
    """SessionEnd: same as Stop + delete marker entirely."""
    _ = _read_hook_input()
    project = _try_load_project()
    if project is None:
        return 0
    closing = _pop_opens_for(project, all_remaining=True)
    actor = os.environ.get("HOPEWELL_ACTOR") or "claude-code"
    for entry in closing:
        nid = entry.get("node")
        executor = entry.get("executor")
        if not nid or not executor:
            continue
        _safe_leave(
            project, nid, executor,
            actor=actor, reason="claude-code SessionEnd",
        )
    clear_active_marker(project)
    return 0


def on_subagent_stop() -> int:
    """SubagentStop: alias for Stop — close all open locations."""
    return on_stop()


# ---------------------------------------------------------------------------
# dispatch table
# ---------------------------------------------------------------------------


DISPATCH: Dict[str, Any] = {
    "session-start":       on_session_start,
    "session-end":         on_session_end,
    "user-prompt-submit":  on_user_prompt_submit,
    "pre-tool-use":        on_pre_tool_use,
    "post-tool-use":       on_post_tool_use,
    "stop":                on_stop,
    "subagent-stop":       on_subagent_stop,
}


def dispatch(name: str) -> int:
    fn = DISPATCH.get(name)
    if fn is None:
        return 0
    try:
        return int(fn() or 0)
    except Exception:
        # Absolute last-ditch: never raise out of a hook.
        return 0
