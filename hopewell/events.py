"""Append-only event log.

Every graph mutation emits a JSON line to `.hopewell/events.jsonl`. Replay
reconstructs state; node files on disk are a projection for human editing.

Events are deterministic enough to diff across branches (sorted keys,
explicit timestamps).
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def append(events_path: Path, kind: str, *, node: Optional[str] = None,
           actor: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append an event. Returns the event dict as written."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event: Dict[str, Any] = {
        "ts": _now(),
        "kind": kind,
    }
    if node is not None:
        event["node"] = node
    if actor is not None:
        event["actor"] = actor
    if data:
        event["data"] = data
    line = json.dumps(event, sort_keys=True, ensure_ascii=False)
    # Atomic-ish append — open in 'a' mode, single write, line-buffered.
    with events_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    return event


def read_all(events_path: Path) -> List[Dict[str, Any]]:
    if not events_path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def iter_since(events_path: Path, ts: str) -> Iterable[Dict[str, Any]]:
    for ev in read_all(events_path):
        if ev.get("ts", "") > ts:
            yield ev


# Canonical event kinds emitted by the library. Not enforced; just documented.
EVENT_KINDS = {
    "project.init",
    "node.create",
    "node.update",
    "node.status.change",
    "node.touch",
    "node.delete",
    "edge.create",
    "edge.delete",
    "artifact.record",
    "github.sync.start",
    "github.sync.finish",
    "orch.plan",
    "orch.run.start",
    "orch.run.node.start",
    "orch.run.node.finish",
    "orch.run.finish",
    "orch.run.fail",
    # HW-0028 — flow runtime
    "flow.push",
    "flow.ack",
    "flow.leave",
    "flow.enter",
    "orch.run.flow.dispatch",
}
