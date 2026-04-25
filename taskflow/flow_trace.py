"""Flow-trace: per-workitem traversal view (HW-0035).

A work item's *journey* is the chronological sequence of flow events
(`flow.push` / `flow.ack` / `flow.enter` / `flow.leave`) recorded in
`.hopewell/events.jsonl` for that node. This module reads those events
and renders three views:

    trace(project, node_id) -> {
        "node_id": "...",
        "events":  [ {ts, kind, executor, from?, to?, reason?, artifact?,
                     outcome?}, ... ],          # chronological
        "visited": [ "<executor_id>", ... ],    # de-duped, in order of
                                                # first appearance
        "summary": { "first_ts", "last_ts",
                     "event_count", "visited_count",
                     "reentries": [exec_ids...] }  # any executor seen
                                                   # more than once via
                                                   # flow.enter
    }

    render_text(trace)     -> "[ts] kind executor [from -> to] [reason]\\n..."
    render_mermaid(trace)  -> a sequenceDiagram source
    render_json(trace)     -> the dict above, json.dumps-ready

Stdlib only; no web deps. Designed so the same projection powers both
the `taskflow flow trace` CLI and the `/api/items/{id}/journey`
endpoint (which already returns a compatible shape — this module
supersets it).

Reuses `hopewell.events.read_all` for the event source so the projection
stays in lockstep with the event log; no parallel parser to drift.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from taskflow import events as events_mod


FLOW_KINDS = {"flow.push", "flow.ack", "flow.enter", "flow.leave"}


# ---------------------------------------------------------------------------
# core projection
# ---------------------------------------------------------------------------


def trace(project, node_id: str) -> Dict[str, Any]:
    """Return the full trace projection for `node_id`.

    Raises `FileNotFoundError` if the node doesn't exist in the project.
    Returns a trace dict even if there are no flow events yet (empty
    lists — useful for "hasn't started yet" rendering).
    """
    if not project.has_node(node_id):
        raise FileNotFoundError(f"node not found: {node_id}")

    raw = events_mod.read_all(project.events_path)
    out_events: List[Dict[str, Any]] = []
    visited: List[str] = []
    seen: set = set()
    enter_counts: Dict[str, int] = {}

    for ev in raw:
        if ev.get("kind") not in FLOW_KINDS:
            continue
        if ev.get("node") != node_id:
            continue
        data = ev.get("data") or {}
        entry: Dict[str, Any] = {
            "ts": ev.get("ts"),
            "kind": ev.get("kind"),
        }
        # Normalise executor fields so downstream renderers don't need
        # to know which key each event-kind uses.
        exec_id: Optional[str] = data.get("executor") or data.get("to_executor")
        from_id: Optional[str] = data.get("from_executor")
        if exec_id:
            entry["executor"] = exec_id
        if from_id:
            entry["from_executor"] = from_id
        if data.get("reason"):
            entry["reason"] = data["reason"]
        if data.get("artifact"):
            entry["artifact"] = data["artifact"]
        if data.get("outcome"):
            entry["outcome"] = data["outcome"]
        if data.get("note"):
            entry["note"] = data["note"]
        if ev.get("actor"):
            entry["actor"] = ev["actor"]
        out_events.append(entry)

        for candidate in (from_id, exec_id):
            if candidate and candidate not in seen:
                seen.add(candidate)
                visited.append(candidate)
        if ev.get("kind") == "flow.enter" and exec_id:
            enter_counts[exec_id] = enter_counts.get(exec_id, 0) + 1

    reentries = sorted(eid for eid, n in enter_counts.items() if n > 1)
    summary: Dict[str, Any] = {
        "event_count": len(out_events),
        "visited_count": len(visited),
        "reentries": reentries,
    }
    if out_events:
        summary["first_ts"] = out_events[0].get("ts")
        summary["last_ts"] = out_events[-1].get("ts")
    else:
        summary["first_ts"] = None
        summary["last_ts"] = None

    return {
        "node_id": node_id,
        "events": out_events,
        "visited": visited,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# renderers
# ---------------------------------------------------------------------------


def _short_kind(kind: str) -> str:
    # "flow.push" -> "push". Keeps the text column tight.
    return kind.split(".", 1)[1] if "." in kind else kind


def render_text(tr: Dict[str, Any], *, compact: bool = False) -> str:
    """Chronological event log as text.

    Each line: `[ts] kind executor [from -> to] [reason] [artifact=..]`

    `compact=True` drops redundant framing lines (header, visited
    footer) so the output is pipe-friendly.
    """
    lines: List[str] = []
    if not compact:
        lines.append(f"# flow.trace {tr['node_id']}")
        s = tr["summary"]
        if s["event_count"] == 0:
            lines.append("  (no flow events yet)")
        else:
            lines.append(
                f"  {s['event_count']} event(s), "
                f"{s['visited_count']} executor(s) visited, "
                f"{s['first_ts']} -> {s['last_ts']}"
            )
            if s["reentries"]:
                lines.append(f"  re-entries: {', '.join(s['reentries'])}")
        lines.append("")

    for ev in tr["events"]:
        kind = _short_kind(ev.get("kind", ""))
        ex = ev.get("executor") or ""
        frm = ev.get("from_executor")
        parts = [f"[{ev.get('ts', '')}]", f"{kind:<6}"]
        if frm and ex:
            parts.append(f"{frm} -> {ex}")
        elif ex:
            parts.append(ex)
        if ev.get("reason"):
            parts.append(f"({ev['reason']})")
        if ev.get("artifact"):
            parts.append(f"artifact={ev['artifact']}")
        if ev.get("outcome") and ev.get("outcome") != "processed":
            parts.append(f"outcome={ev['outcome']}")
        lines.append(" ".join(parts))

    if not compact and tr["events"]:
        lines.append("")
        lines.append(f"visited: {' -> '.join(tr['visited'])}")

    return "\n".join(lines) + ("\n" if lines else "")


def _mermaid_safe(name: str) -> str:
    """Mermaid participant identifiers can't contain spaces or most
    punctuation. Wrap anything that's not a plain slug in quotes — the
    sequenceDiagram parser accepts `participant "@my-agent" as alias`
    but it's simpler to emit a safe alias and show the real name as a
    label."""
    # Keep @ + word chars + dashes; replace the rest.
    safe = []
    for ch in name:
        if ch.isalnum() or ch in "_-@":
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe) or "unknown"


def render_mermaid(tr: Dict[str, Any]) -> str:
    """Sequence diagram of the item's journey.

    Participants are executors (ordered by first appearance). Messages
    are flow events:
      * `flow.push`:  from -> to : `push (reason)`
      * `flow.ack`:   executor -> executor : `ack [outcome]`
      * `flow.enter`: executor -> executor : `enter`
      * `flow.leave`: executor -> executor : `leave`

    Pushes go as solid arrows (`->>`); acks as dashed (`-->>`). Self-
    loops (enter/leave/ack) emit as self-arrows so the diagram reads
    without synthetic participants.
    """
    lines: List[str] = ["sequenceDiagram"]

    # Participants — preserve order of first appearance.
    participants: List[str] = []
    seen_parts: set = set()
    for ex in tr["visited"]:
        if ex and ex not in seen_parts:
            seen_parts.add(ex)
            participants.append(ex)
    if not participants:
        # No journey yet — emit a placeholder so mermaid still renders.
        lines.append(f"  Note over _none: no flow events for {tr['node_id']}")
        return "\n".join(lines) + "\n"

    for ex in participants:
        alias = _mermaid_safe(ex)
        if alias == ex:
            lines.append(f"  participant {alias}")
        else:
            lines.append(f'  participant {alias} as "{ex}"')

    # Messages.
    for ev in tr["events"]:
        kind = ev.get("kind")
        ex = ev.get("executor")
        frm = ev.get("from_executor")
        reason = ev.get("reason")
        outcome = ev.get("outcome")
        if not ex:
            continue
        ex_a = _mermaid_safe(ex)

        if kind == "flow.push":
            src = _mermaid_safe(frm) if frm else "inbox"
            if frm and frm not in seen_parts:
                # Unseen in visited (e.g. push from external/None handled
                # above) — declare lazily to keep diagram valid.
                lines.insert(1 + len(participants), f"  participant {src}")
                participants.append(frm)
                seen_parts.add(frm)
            label = "flow.push"
            if reason:
                label += f" ({reason})"
            lines.append(f"  {src}->>{ex_a}: {label}")

        elif kind == "flow.ack":
            # Self-dashed arrow: the executor acks its own inbox.
            label = "flow.ack"
            if outcome and outcome != "processed":
                label += f" [{outcome}]"
            lines.append(f"  {ex_a}-->>{ex_a}: {label}")

        elif kind == "flow.enter":
            lines.append(f"  {ex_a}->>{ex_a}: flow.enter")

        elif kind == "flow.leave":
            lines.append(f"  {ex_a}->>{ex_a}: flow.leave")

    return "\n".join(lines) + "\n"
