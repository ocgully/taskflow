"""Attestation + agent fingerprinting (v0.4).

Every meaningful mutation (node create/touch/status change, edge create,
orchestrator run, github sync) emits both an `events.jsonl` entry (terse,
replay-source-of-truth) and an `attestations.jsonl` entry (richer, carrying
agent identity + fingerprint + reason + evidence).

Agent identity = "<name>@<fingerprint>", where fingerprint is the first 12
hex chars of the SHA-256 of the agent's doc file content. When the doc
changes (prompt tuning, tenet rewrites, new mantras) the fingerprint
changes, so "agent quality" can be tracked per version of the agent.

Attestations are append-only, sorted-keys JSON, one per line. Replay-safe.

This module exposes:

    AgentRegistry      — load / save .hopewell/agents.jsonl
    fingerprint(path)  — doc-SHA helper
    record(...)        — append an attestation
    iter(...)          — read attestations with filters
    quality(...)       — compute quality metrics for an agent
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


AGENTS_FILE = "agents.jsonl"
ATTESTATIONS_FILE = "attestations.jsonl"


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def fingerprint(doc_path: Path) -> str:
    """Return first 12 hex chars of SHA-256 of the file's UTF-8 content.

    Raises FileNotFoundError if the doc doesn't exist.
    """
    p = Path(doc_path)
    if not p.is_file():
        raise FileNotFoundError(f"agent doc not found: {p}")
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    return h[:12]


def fingerprint_from_text(text: str) -> str:
    """Same as `fingerprint` but from an in-memory string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# AgentRegistry
# ---------------------------------------------------------------------------


@dataclass
class AgentRecord:
    """One registered agent + the history of fingerprints observed for it."""

    name: str                                      # e.g. "@ecs-engineer"
    doc_path: Optional[str] = None                 # path to agent doc, relative to project root
    current_fingerprint: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)
    # history entries are {"fingerprint": "...", "first_seen": "<iso-ts>"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "doc_path": self.doc_path,
            "current_fingerprint": self.current_fingerprint,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AgentRecord":
        return cls(
            name=d["name"],
            doc_path=d.get("doc_path"),
            current_fingerprint=d.get("current_fingerprint"),
            history=list(d.get("history", [])),
        )


class AgentRegistry:
    """Persistent agent roster at `.hopewell/agents.jsonl`.

    File format: one JSON object per line = one AgentRecord. On save we
    rewrite the whole file (registry size is small; write cost is negligible).
    """

    def __init__(self, registry_path: Path) -> None:
        self.path = registry_path
        self._records: Dict[str, AgentRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec = AgentRecord.from_dict(d)
                self._records[rec.name] = rec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for name in sorted(self._records):
            rec = self._records[name]
            lines.append(json.dumps(rec.to_dict(), sort_keys=True, ensure_ascii=False))
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def register(self, name: str, doc_path: Optional[str], current_fp: Optional[str]) -> AgentRecord:
        """Register or refresh an agent. If `current_fp` differs from the last
        history entry's fingerprint, append a new history entry."""
        now = _now_iso()
        rec = self._records.get(name)
        if rec is None:
            rec = AgentRecord(name=name, doc_path=doc_path, current_fingerprint=current_fp,
                              history=[])
            self._records[name] = rec
        else:
            if doc_path:
                rec.doc_path = doc_path
            rec.current_fingerprint = current_fp

        if current_fp:
            if not rec.history or rec.history[-1].get("fingerprint") != current_fp:
                rec.history.append({"fingerprint": current_fp, "first_seen": now})

        self.save()
        return rec

    def get(self, name: str) -> Optional[AgentRecord]:
        return self._records.get(name)

    def all(self) -> List[AgentRecord]:
        return sorted(self._records.values(), key=lambda r: r.name)

    def fingerprints_for(self, name: str) -> List[str]:
        rec = self.get(name)
        if not rec:
            return []
        return [e["fingerprint"] for e in rec.history]


# ---------------------------------------------------------------------------
# Attestation record + I/O
# ---------------------------------------------------------------------------


def record(attestations_path: Path, *, kind: str, node: Optional[str],
           actor: Optional[str], fingerprint_hex: Optional[str],
           commit: Optional[str] = None, reason: Optional[str] = None,
           evidence: Optional[List[str]] = None,
           data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Append an attestation. Returns the written dict."""
    att: Dict[str, Any] = {
        "ts": _now_iso(),
        "kind": kind,
    }
    if node is not None:
        att["node"] = node
    # Agent identity as a stable compound: "<name>@<fingerprint>"
    if actor is not None:
        att["actor"] = actor
        if fingerprint_hex:
            att["agent_id"] = f"{actor}@{fingerprint_hex}"
            att["fingerprint"] = fingerprint_hex
    if commit is not None:
        att["commit"] = commit
    if reason:
        att["reason"] = reason
    if evidence:
        att["evidence"] = list(evidence)
    if data:
        att["data"] = data
    line = json.dumps(att, sort_keys=True, ensure_ascii=False)
    attestations_path.parent.mkdir(parents=True, exist_ok=True)
    with attestations_path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    return att


def iter_attestations(attestations_path: Path) -> Iterable[Dict[str, Any]]:
    if not attestations_path.is_file():
        return
    with attestations_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def query_attestations(attestations_path: Path, *, agent: Optional[str] = None,
                       fingerprint: Optional[str] = None,
                       node: Optional[str] = None,
                       since: Optional[str] = None,
                       kind: Optional[str] = None,
                       limit: Optional[int] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for att in iter_attestations(attestations_path):
        if agent and att.get("actor") != agent:
            continue
        if fingerprint and att.get("fingerprint") != fingerprint:
            continue
        if node and att.get("node") != node:
            continue
        if kind and att.get("kind") != kind:
            continue
        if since and att.get("ts", "") < since:
            continue
        out.append(att)
        if limit and len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Quality metrics
# ---------------------------------------------------------------------------


def quality(attestations_path: Path, agent: str,
            project_nodes: Dict[str, Any],
            registry: AgentRegistry) -> Dict[str, Any]:
    """Compute quality metrics for an agent, broken down by fingerprint.

    Metrics tracked per fingerprint:
      - nodes_closed            total close attestations
      - reopens                 nodes closed by this fingerprint that later went back to `doing`
      - defects_traced          `defect` nodes whose component_data.caused_by points at a node this fingerprint closed
      - avg_review_iterations   mean (review -> doing) transitions before done (review churn)

    `project_nodes` is a dict of node_id -> Node (loaded elsewhere; avoids a
    circular import with project.py).
    """
    rec = registry.get(agent)
    fingerprints = [e["fingerprint"] for e in (rec.history if rec else [])]
    if rec and rec.current_fingerprint and rec.current_fingerprint not in fingerprints:
        fingerprints.append(rec.current_fingerprint)

    per_fp: Dict[str, Dict[str, Any]] = {
        fp: {"fingerprint": fp,
             "nodes_closed": 0,
             "reopens": 0,
             "defects_traced": 0,
             "review_iterations_sum": 0,
             "review_iterations_count": 0}
        for fp in fingerprints
    }

    # Per-node replay: find which fingerprint closed each node + count reopens + review iterations
    # Build from attestations
    node_close_by_fp: Dict[str, str] = {}    # node_id -> fingerprint that moved it to done
    node_review_iterations: Dict[str, int] = {}

    for att in iter_attestations(attestations_path):
        if att.get("actor") != agent:
            continue
        fp = att.get("fingerprint")
        if fp not in per_fp:
            # Agent attested with a fingerprint not in the registry yet
            per_fp[fp] = {"fingerprint": fp, "nodes_closed": 0, "reopens": 0,
                          "defects_traced": 0, "review_iterations_sum": 0,
                          "review_iterations_count": 0}

        node_id = att.get("node")
        kind = att.get("kind", "")
        data = att.get("data", {}) or {}

        if kind == "node.status.change":
            to_status = data.get("to")
            from_status = data.get("from")
            if to_status == "done" and node_id:
                per_fp[fp]["nodes_closed"] += 1
                node_close_by_fp[node_id] = fp
            if from_status == "done" and to_status == "doing" and node_id:
                # Reopen counts against whoever last closed it
                closer_fp = node_close_by_fp.get(node_id)
                if closer_fp and closer_fp in per_fp:
                    per_fp[closer_fp]["reopens"] += 1
            if from_status == "review" and to_status == "doing" and node_id:
                node_review_iterations[node_id] = node_review_iterations.get(node_id, 0) + 1
            if to_status == "done" and node_id and node_id in node_review_iterations:
                per_fp[fp]["review_iterations_sum"] += node_review_iterations[node_id]
                per_fp[fp]["review_iterations_count"] += 1

    # Defects traceback: walk project_nodes for `defect` nodes with component_data.caused_by
    for node_id, node_obj in project_nodes.items():
        comps = getattr(node_obj, "components", [])
        if "defect" not in comps:
            continue
        cd = getattr(node_obj, "component_data", {}) or {}
        caused_by = (cd.get("defect") or {}).get("caused_by")
        if not caused_by:
            continue
        closer_fp = node_close_by_fp.get(caused_by)
        if closer_fp and closer_fp in per_fp:
            per_fp[closer_fp]["defects_traced"] += 1

    # Compute derived metrics
    fingerprints_out: List[Dict[str, Any]] = []
    for fp in fingerprints + [fp for fp in per_fp if fp not in fingerprints]:
        entry = per_fp[fp]
        avg = None
        if entry["review_iterations_count"]:
            avg = round(entry["review_iterations_sum"] / entry["review_iterations_count"], 2)
        fingerprints_out.append({
            "fingerprint": entry["fingerprint"],
            "nodes_closed": entry["nodes_closed"],
            "reopens": entry["reopens"],
            "defects_traced": entry["defects_traced"],
            "avg_review_iterations": avg,
        })

    # Trend label (improving / regressing / flat)
    trend = _trend_label(fingerprints_out)

    return {
        "agent": agent,
        "fingerprints": fingerprints_out,
        "trend": trend,
    }


def _trend_label(per_fp: List[Dict[str, Any]]) -> str:
    """Compare last two fingerprints on `reopens + defects_traced`. Lower = improving."""
    if len(per_fp) < 2:
        return "insufficient-data"
    a, b = per_fp[-2], per_fp[-1]
    a_score = (a.get("reopens") or 0) + (a.get("defects_traced") or 0)
    b_score = (b.get("reopens") or 0) + (b.get("defects_traced") or 0)
    if b_score < a_score:
        return "improving"
    if b_score > a_score:
        return "regressing"
    return "flat"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
