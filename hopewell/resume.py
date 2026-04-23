"""Session-resume + checkpoint (v0.5.3).

The missing piece for "I was mid-work; a new session starts; where was I?"

`resume` builds a single structured slice:
  - active claims (branches I hold)
  - nodes in `doing` / `review` status that concern me
  - my ready queue (what I could pick up next)
  - for each active node: the most recent checkpoint note + suggested next action

`checkpoint` is a labelled touch — prepends `[next]` to the note so resume
can pick it out. Multiple checkpoints accumulate; resume shows the latest.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from hopewell.model import Node, NodeStatus, TERMINAL_STATUSES


CHECKPOINT_PREFIX = "[next]"


def _actor_default(project) -> Optional[str]:
    """Best-effort identity resolution, mirroring cli._actor_from_env."""
    actor = os.environ.get("HOPEWELL_ACTOR") or os.environ.get("GIT_AUTHOR_NAME")
    if actor and not actor.startswith("@"):
        actor = "@" + actor
    return actor


def _latest_checkpoint(node: Node) -> Optional[str]:
    """Return the most recent '[next] ...' note body, or None."""
    for n in reversed(node.notes):
        if CHECKPOINT_PREFIX in n:
            idx = n.find(CHECKPOINT_PREFIX)
            return n[idx + len(CHECKPOINT_PREFIX):].strip()
    return None


def _latest_general_note(node: Node) -> Optional[str]:
    """Most recent note, regardless of prefix. Used as a fallback hint."""
    return node.notes[-1] if node.notes else None


def _node_status(n: Node) -> str:
    return n.status.value if isinstance(n.status, NodeStatus) else n.status


def checkpoint(project, node_id: str, next_step: str, *,
               actor: Optional[str] = None) -> Node:
    """Append a `[next]` checkpoint note to a node."""
    if not next_step or not next_step.strip():
        raise ValueError("checkpoint: --next must be non-empty")
    note = f"{CHECKPOINT_PREFIX} {next_step.strip()}"
    return project.touch(node_id, note, actor=actor)


def resume(project, *, name: Optional[str] = None,
           include_all: bool = False) -> Dict[str, Any]:
    """Build the resume JSON slice.

    If `name` is None and not `include_all`, default to the current actor
    (HOPEWELL_ACTOR or git author). If neither is set and --all isn't
    requested, return a sentinel telling the user which env var to set.
    """
    actor = name or _actor_default(project)
    if not actor and not include_all:
        return {
            "query": "resume",
            "actor": None,
            "hint": ("could not infer actor — set $HOPEWELL_ACTOR "
                     "or pass a name, or use --all to see every active claim"),
            "claims": [],
            "doing": [],
            "review": [],
            "ready_queue": [],
        }

    # Normalise actor.
    if actor and not actor.startswith("@"):
        actor = "@" + actor

    # Index nodes.
    all_nodes: Dict[str, Node] = {n.id: n for n in project.all_nodes()}

    # Claims held by this actor (remote branches + local unreleased events).
    from hopewell import claim as claim_mod
    claims_raw = claim_mod.query_claims(project)
    my_claims: List[Dict[str, Any]] = []
    all_active_claims: List[Dict[str, Any]] = []
    for c in claims_raw:
        record = c.to_dict()
        # Attach node context
        node = all_nodes.get(c.node_id)
        if node:
            record["title"] = node.title
            record["status"] = _node_status(node)
            cp = _latest_checkpoint(node)
            if cp:
                record["next"] = cp
            else:
                gn = _latest_general_note(node)
                if gn:
                    record["last_note"] = gn
            record["suggested_action"] = f"git switch {c.branch}"
        all_active_claims.append(record)
        if actor and c.claimer == actor:
            my_claims.append(record)

    # "doing" / "review" lists focused on the actor (via `owner` field) or all.
    doing: List[Dict[str, Any]] = []
    review: List[Dict[str, Any]] = []
    ready: List[Dict[str, Any]] = []

    for n in all_nodes.values():
        status = _node_status(n)
        matches_actor = (actor is None) or (n.owner == actor)
        if include_all:
            matches_actor = True

        if status == "doing" and matches_actor:
            entry = {
                "id": n.id, "title": n.title, "owner": n.owner,
                "priority": n.priority,
                "next": _latest_checkpoint(n),
                "last_note": _latest_general_note(n),
            }
            doing.append(entry)
        elif status == "review" and matches_actor:
            review.append({
                "id": n.id, "title": n.title, "owner": n.owner,
                "priority": n.priority,
            })

    # Ready queue (claim-aware) — who could they pick up?
    from hopewell.query import ready as ready_query
    ready_data = ready_query(project, owner=actor if actor else None)
    ready = ready_data.get("nodes", [])[:10]   # cap; don't overwhelm

    return {
        "query": "resume",
        "actor": actor,
        "include_all": include_all,
        "claims_held": my_claims if not include_all else all_active_claims,
        "doing": sorted(doing, key=lambda x: (x["priority"], x["id"])),
        "review": sorted(review, key=lambda x: (x["priority"], x["id"])),
        "ready_queue": ready,
        "counts": {
            "claims_held": len(my_claims) if not include_all else len(all_active_claims),
            "doing": len(doing),
            "review": len(review),
            "ready_queue": len(ready),
        },
    }


def render_text(data: Dict[str, Any]) -> str:
    """Human-friendly rendering of the resume JSON."""
    actor = data.get("actor") or "(anonymous)"
    lines: List[str] = [f"=== resume for {actor} ==="]

    if data.get("hint"):
        lines.append(f"  (note) {data['hint']}")

    claims = data.get("claims_held") or []
    lines.append(f"\n--- active claims ({len(claims)}) ---")
    if not claims:
        lines.append("  (none — nothing claimed right now)")
    for c in claims:
        lines.append(f"  {c.get('node_id', '?'):10} [{c.get('status','?'):6}] "
                     f"branch={c.get('branch')}")
        if c.get("title"):
            lines.append(f"    title: {c['title']}")
        if c.get("next"):
            lines.append(f"    next:  {c['next']}")
        elif c.get("last_note"):
            lines.append(f"    last:  {c['last_note']}")
        if c.get("suggested_action"):
            lines.append(f"    -> {c['suggested_action']}")

    doing = data.get("doing") or []
    if doing:
        lines.append(f"\n--- doing ({len(doing)}) ---")
        for n in doing:
            lines.append(f"  {n['id']:10} {n['priority']} {n['title']}")
            if n.get("next"):
                lines.append(f"    next: {n['next']}")

    review = data.get("review") or []
    if review:
        lines.append(f"\n--- review ({len(review)}) ---")
        for n in review:
            lines.append(f"  {n['id']:10} {n['priority']} {n['title']}")

    ready = data.get("ready_queue") or []
    if ready:
        lines.append(f"\n--- ready to pick up ({len(ready)}) ---")
        for n in ready[:5]:
            lines.append(f"  {n['id']:10} {n['priority']} {n['title']}")
        if len(ready) > 5:
            lines.append(f"  ... +{len(ready) - 5} more")

    return "\n".join(lines) + "\n"
