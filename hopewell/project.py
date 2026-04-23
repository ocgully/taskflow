"""Project — the central class. Loads config, reads/writes nodes, appends events."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from hopewell import attestation as att_mod
from hopewell import config as config_mod
from hopewell import events, meta as meta_mod, paths, storage
from hopewell.attestation import AgentRegistry
from hopewell.config import ProjectConfig
from hopewell.model import (
    ComponentRegistry, Edge, EdgeKind, Node, NodeInput, NodeOutput,
    NodeStatus, STATUS_TRANSITIONS, default_registry, format_node_id,
    parse_node_id,
)


CLAUDEIGNORE_SNIPPET = "/.hopewell/\n"

CLAUDEMD_BLOCK = """\
## Hopewell — do not read `.hopewell/` directly

`.hopewell/` holds the work graph (tickets, edges, events, attestations).
Agents must NOT read files in that directory during research. Use the
`hopewell query <...>` CLI (or the `hopewell` Python library) for any
lookup. Tree-browsing `.hopewell/` defeats the point of the tool (tokens
+ non-determinism). Violations surface in reviews as "you read
.hopewell/ — please re-do via the CLI."
"""


class Project:
    """A loaded Hopewell project rooted at a directory containing `.hopewell/`."""

    def __init__(self, root: Path, cfg: ProjectConfig, registry: ComponentRegistry) -> None:
        self.root = root.resolve()
        self.cfg = cfg
        self.registry = registry
        self._agent_registry: Optional[AgentRegistry] = None

    # ---- load / init ----

    @classmethod
    def load(cls, start: Optional[Path] = None) -> "Project":
        root = paths.require_project_root(start)
        cfg = config_mod.load(paths.hw_dir(root) / "config.toml")
        reg = default_registry()
        # Filter registry to enabled components (keeps validation useful without
        # rejecting built-ins a project chose not to enable).

        # v0.5.2: cross-version compatibility gate.
        #   - If meta.json is missing (legacy .hopewell/), auto-heal by writing
        #     a current-version stamp. Pre-0.5.2 projects stayed on schema 1,
        #     so auto-filling with SCHEMA_VERSION="1" is correct.
        #   - If meta.json is present, check_compatibility raises on mismatch.
        #   - minimum_version from config is enforced regardless.
        hw = paths.hw_dir(root)
        mf = meta_mod.load(hw)
        if mf is None and hw.is_dir():
            mf = meta_mod.write_for_init(hw)
        meta_mod.check_compatibility(mf, minimum_version=cfg.coordination.minimum_version)

        return cls(root, cfg, reg)

    @classmethod
    def init(cls, root: Path, *, id_prefix: str = "HW", name: Optional[str] = None,
             overwrite_claudemd: bool = False) -> "Project":
        root = root.resolve()
        paths.ensure_hw_dir(root)
        hw = paths.hw_dir(root)

        # Config
        cfg = ProjectConfig.default(name or root.name)
        cfg.id_prefix = id_prefix
        config_path = hw / "config.toml"
        if not config_path.is_file():
            config_mod.write(config_path, cfg)
        else:
            cfg = config_mod.load(config_path)

        # .claudeignore hint INSIDE .hopewell/ (visible; documented)
        claudeignore = hw / ".claudeignore"
        if not claudeignore.is_file():
            claudeignore.write_text(CLAUDEIGNORE_SNIPPET, encoding="utf-8")

        # And at the project root so it actually takes effect for Claude Code.
        root_claudeignore = root / ".claudeignore"
        existing = root_claudeignore.read_text(encoding="utf-8") if root_claudeignore.is_file() else ""
        if "/.hopewell/" not in existing:
            new = existing.rstrip() + ("\n" if existing else "") + CLAUDEIGNORE_SNIPPET
            root_claudeignore.write_text(new, encoding="utf-8")

        # CLAUDE.md block.
        claudemd = root / "CLAUDE.md"
        if claudemd.is_file():
            md_text = claudemd.read_text(encoding="utf-8")
            if "## Hopewell — do not read `.hopewell/` directly" not in md_text:
                suffix = md_text.rstrip() + "\n\n" + CLAUDEMD_BLOCK
                claudemd.write_text(suffix, encoding="utf-8")
        # If no CLAUDE.md exists, do NOT create one — respect whatever the
        # project prefers. The rule is still in .claudeignore.

        # Event log — first entry only on a truly new project. Re-running
        # `hopewell init` on an existing .hopewell/ is idempotent: skip the
        # duplicate project.init event but still refresh claudeignore /
        # CLAUDE.md rule / merge driver so upgrades flow in.
        events_path = hw / "events.jsonl"
        already_initialized = _has_project_init_event(events_path)
        if not already_initialized:
            events.append(events_path, "project.init",
                          data={"name": cfg.name, "id_prefix": cfg.id_prefix})

        # Empty edges log (so readers never have to worry about missing file).
        edges_log = hw / "edges.jsonl"
        if not edges_log.is_file():
            edges_log.write_text("", encoding="utf-8")

        # Git merge driver + .gitattributes for JSONL append-only logs (v0.5).
        # Best-effort: only activates in a git worktree. Idempotent.
        _install_merge_driver(root)

        # Write or refresh meta.json — the version contract (v0.5.2).
        if already_initialized:
            meta_mod.write_for_migrate(hw)
        else:
            meta_mod.write_for_init(hw)

        return cls.load(root)

    @classmethod
    def migrate(cls, start: Optional[Path] = None) -> "Project":
        """Re-apply every idempotent setup step to an existing `.hopewell/`.

        Used when a new Hopewell version adds project-level setup (new
        .gitattributes entries, new config sections, new CLAUDE.md rules).
        Safe to re-run any number of times.
        """
        root = paths.require_project_root(start)
        project = cls.load(root)

        # Re-run everything in init that is idempotent; skip the duplicate
        # project.init event by calling init() itself — it now guards on
        # existing events.
        cls.init(root, id_prefix=project.cfg.id_prefix, name=project.cfg.name)
        events.append(project.events_path, "project.migrate",
                      data={"from_version": "<=pre-migrate>"})
        return cls.load(root)

    # ---- paths ----

    @property
    def hw_dir(self) -> Path:
        return paths.hw_dir(self.root)

    @property
    def nodes_dir(self) -> Path:
        return self.hw_dir / "nodes"

    @property
    def events_path(self) -> Path:
        return self.hw_dir / "events.jsonl"

    @property
    def edges_path(self) -> Path:
        return self.hw_dir / "edges.jsonl"

    @property
    def views_dir(self) -> Path:
        return self.hw_dir / "views"

    @property
    def attestations_path(self) -> Path:
        return self.hw_dir / "attestations.jsonl"

    @property
    def agents_path(self) -> Path:
        return self.hw_dir / "agents.jsonl"

    @property
    def agent_registry(self) -> AgentRegistry:
        if self._agent_registry is None:
            self._agent_registry = AgentRegistry(self.agents_path)
        return self._agent_registry

    def _fingerprint_for(self, actor: Optional[str]) -> Optional[str]:
        """Look up `actor`'s current fingerprint in the agent registry."""
        if not actor:
            return None
        rec = self.agent_registry.get(actor)
        return rec.current_fingerprint if rec else None

    def _attest(self, *, kind: str, node: Optional[str], actor: Optional[str],
                commit: Optional[str] = None, reason: Optional[str] = None,
                evidence: Optional[List[str]] = None,
                data: Optional[Dict[str, Any]] = None) -> None:
        """Emit an attestation alongside the event log. Idempotent + append-only."""
        fp = self._fingerprint_for(actor)
        att_mod.record(
            self.attestations_path,
            kind=kind, node=node, actor=actor,
            fingerprint_hex=fp, commit=commit, reason=reason,
            evidence=evidence, data=data,
        )

    # ---- node id generator ----

    def next_node_id(self) -> str:
        prefix = self.cfg.id_prefix
        pad = self.cfg.id_pad
        existing: List[int] = []
        for p in self.nodes_dir.glob(f"{prefix}-*.md"):
            try:
                _, n = parse_node_id(p.stem)
                existing.append(n)
            except ValueError:
                continue
        next_n = (max(existing) + 1) if existing else 1
        return format_node_id(prefix, next_n, pad=pad)

    # ---- CRUD ----

    def new_node(self, *, components: List[str], title: str,
                 owner: Optional[str] = None,
                 parent: Optional[str] = None,
                 priority: str = "P2",
                 status: NodeStatus = NodeStatus.idea,
                 actor: Optional[str] = None) -> Node:
        errors = self.registry.validate_node_components(components)
        if errors:
            raise ValueError("; ".join(errors))
        node_id = self.next_node_id()
        node = Node(
            id=node_id,
            title=title,
            status=status,
            priority=priority,
            owner=owner,
            project=self.cfg.name,
            parent=parent,
            components=list(components),
        )
        self.save_node(node)
        events.append(self.events_path, "node.create", node=node_id, actor=actor,
                      data={"components": node.components, "title": title})
        self._attest(kind="node.create", node=node_id, actor=actor,
                     data={"components": node.components, "title": title})
        return node

    def save_node(self, node: Node) -> None:
        storage.write_node_file(self.node_path(node.id), node)

    def node_path(self, node_id: str) -> Path:
        return self.nodes_dir / f"{node_id}.md"

    def node(self, node_id: str) -> Node:
        path = self.node_path(node_id)
        if not path.is_file():
            raise FileNotFoundError(f"node not found: {node_id}")
        return storage.read_node_file(path)

    def has_node(self, node_id: str) -> bool:
        return self.node_path(node_id).is_file()

    def all_nodes(self) -> List[Node]:
        out: List[Node] = []
        for p in sorted(self.nodes_dir.glob("*.md")):
            try:
                out.append(storage.read_node_file(p))
            except Exception:
                continue
        return out

    def delete_node(self, node_id: str, *, actor: Optional[str] = None) -> None:
        path = self.node_path(node_id)
        if path.is_file():
            path.unlink()
            events.append(self.events_path, "node.delete", node=node_id, actor=actor)

    # ---- mutations ----

    def set_status(self, node_id: str, new_status: NodeStatus, *,
                   actor: Optional[str] = None, reason: Optional[str] = None) -> Node:
        node = self.node(node_id)
        old = node.status if isinstance(node.status, NodeStatus) else NodeStatus(node.status)
        if old == new_status:
            return node
        if not node.can_transition_to(new_status):
            raise ValueError(
                f"illegal status transition {old.value} -> {new_status.value} "
                f"(node {node_id}); allowed from {old.value}: "
                f"{sorted(s.value for s in STATUS_TRANSITIONS.get(old, set()))}"
            )
        node.status = new_status
        node.updated = _now()
        self.save_node(node)
        events.append(self.events_path, "node.status.change", node=node_id, actor=actor,
                      data={"from": old.value, "to": new_status.value, "reason": reason})
        self._attest(kind="node.status.change", node=node_id, actor=actor, reason=reason,
                     data={"from": old.value, "to": new_status.value})
        return node

    def touch(self, node_id: str, note: str, *, actor: Optional[str] = None) -> Node:
        node = self.node(node_id)
        ts = _now()
        who = actor or "unknown"
        node.notes.append(f"{ts} [{who}]  {note}")
        node.updated = ts
        self.save_node(node)
        events.append(self.events_path, "node.touch", node=node_id, actor=actor,
                      data={"note": note})
        self._attest(kind="node.touch", node=node_id, actor=actor, data={"note": note})
        return node

    def link(self, from_id: str, kind: EdgeKind, to_id: str, *,
             artifact: Optional[str] = None, reason: Optional[str] = None,
             actor: Optional[str] = None) -> Edge:
        if not self.has_node(from_id):
            raise FileNotFoundError(f"node not found: {from_id}")
        if kind in (EdgeKind.blocks, EdgeKind.parent, EdgeKind.related) and not self.has_node(to_id):
            raise FileNotFoundError(f"node not found: {to_id}")

        src = self.node(from_id)
        if kind == EdgeKind.blocks:
            if to_id not in src.blocks:
                src.blocks.append(to_id)
            dst = self.node(to_id)
            if from_id not in dst.blocked_by:
                dst.blocked_by.append(from_id)
            self.save_node(dst)
        elif kind == EdgeKind.parent:
            dst = self.node(to_id)
            dst.parent = from_id
            self.save_node(dst)
        elif kind == EdgeKind.related:
            if to_id not in src.related:
                src.related.append(to_id)
        elif kind == EdgeKind.produces:
            src.outputs.append(NodeOutput(path=to_id, kind=artifact or "artifact"))
        elif kind == EdgeKind.consumes:
            # to_id is the upstream node, artifact is the path
            src.inputs.append(NodeInput(from_node=to_id, artifact=artifact))
        self.save_node(src)

        edge = Edge(from_id=from_id, to_id=to_id, kind=kind, artifact=artifact, reason=reason)
        with self.edges_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(self._edge_to_json(edge) + "\n")
        events.append(self.events_path, "edge.create", actor=actor, data={
            "from": from_id, "to": to_id, "kind": kind.value, "artifact": artifact, "reason": reason
        })
        self._attest(kind="edge.create", node=from_id, actor=actor, reason=reason,
                     data={"from": from_id, "to": to_id, "kind": kind.value,
                           "artifact": artifact})
        return edge

    def close(self, node_id: str, *, commit: Optional[str] = None,
              reason: Optional[str] = None, actor: Optional[str] = None) -> Node:
        node = self.node(node_id)
        cur = node.status if isinstance(node.status, NodeStatus) else NodeStatus(node.status)
        # Walk toward done via the permitted path.
        sequence = {
            NodeStatus.idea: [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done],
            NodeStatus.blocked: [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done],
            NodeStatus.ready: [NodeStatus.doing, NodeStatus.review, NodeStatus.done],
            NodeStatus.doing: [NodeStatus.review, NodeStatus.done],
            NodeStatus.review: [NodeStatus.done],
            NodeStatus.done: [],
        }.get(cur, [NodeStatus.done])
        for s in sequence:
            self.set_status(node_id, s, actor=actor, reason=reason)
        closing_note = f"closed" + (f" by commit {commit}" if commit else "") + \
                       (f" - {reason}" if reason else "")
        self.touch(node_id, closing_note, actor=actor)
        # Emit an explicit "node.close" attestation carrying commit + evidence so
        # quality metrics can trace closed-by-<fingerprint> cheaply.
        evidence = []
        if commit:
            evidence.append(f"commit:{commit}")
        self._attest(kind="node.close", node=node_id, actor=actor, commit=commit,
                     reason=reason, evidence=evidence or None)
        return self.node(node_id)

    # ---- integrity ----

    def check(self) -> List[str]:
        """Validate the graph. Returns a list of problems; empty = clean."""
        problems: List[str] = []
        all_nodes = {n.id: n for n in self.all_nodes()}

        for n in all_nodes.values():
            # component validation
            for err in self.registry.validate_node_components(n.components):
                problems.append(f"{n.id}: {err}")
            # dangling refs
            for ref in n.blocks:
                if ref not in all_nodes:
                    problems.append(f"{n.id}: blocks unknown node {ref}")
            for ref in n.blocked_by:
                if ref not in all_nodes:
                    problems.append(f"{n.id}: blocked_by unknown node {ref}")
            for ref in n.related:
                if ref not in all_nodes:
                    problems.append(f"{n.id}: related unknown node {ref}")
            if n.parent and n.parent not in all_nodes:
                problems.append(f"{n.id}: parent unknown node {n.parent}")
            for i in n.inputs:
                if i.from_node and i.from_node not in all_nodes:
                    problems.append(f"{n.id}: input references unknown node {i.from_node}")

        # Cycle detection (via blocks edges)
        adj: Dict[str, List[str]] = {nid: list(n.blocks) for nid, n in all_nodes.items()}
        for cyc in _find_cycles(adj):
            problems.append("cycle: " + " -> ".join(cyc + [cyc[0]]))

        return problems

    # ---- helpers ----

    def _edge_to_json(self, e: Edge) -> str:
        import json
        return json.dumps({
            "ts": e.created,
            "from": e.from_id,
            "to": e.to_id,
            "kind": e.kind.value if isinstance(e.kind, EdgeKind) else e.kind,
            "artifact": e.artifact,
            "reason": e.reason,
        }, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# cycle detection
# ---------------------------------------------------------------------------

def _find_cycles(adj: Dict[str, List[str]]) -> List[List[str]]:
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: Dict[str, int] = {n: WHITE for n in adj}
    stack: List[str] = []
    cycles: List[List[str]] = []

    def visit(n: str) -> None:
        colour[n] = GRAY
        stack.append(n)
        for m in adj.get(n, []):
            if m not in colour:
                continue
            if colour[m] == GRAY:
                # Cycle from stack[stack.index(m):]
                cycle = stack[stack.index(m):]
                cycles.append(list(cycle))
            elif colour[m] == WHITE:
                visit(m)
        stack.pop()
        colour[n] = BLACK

    for n in list(adj.keys()):
        if colour[n] == WHITE:
            visit(n)
    return cycles


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_project_init_event(events_path: Path) -> bool:
    """Cheap scan for a prior project.init event; used to keep init idempotent."""
    if not events_path.is_file():
        return False
    try:
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                if '"kind": "project.init"' in line or '"kind":"project.init"' in line:
                    return True
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Merge driver + .gitattributes installer (v0.5)
# ---------------------------------------------------------------------------


_GITATTR_LINES = [
    ".hopewell/events.jsonl        merge=hopewell-jsonl",
    ".hopewell/attestations.jsonl  merge=hopewell-jsonl",
    ".hopewell/edges.jsonl         merge=hopewell-jsonl",
    ".hopewell/agents.jsonl        merge=hopewell-jsonl",
]

_GITATTR_MARKER = "# --- hopewell jsonl merge driver (managed) ---"
_GITATTR_END = "# --- /hopewell jsonl merge driver ---"


def _install_merge_driver(project_root: Path) -> None:
    """Write .gitattributes block + configure `merge.hopewell-jsonl.driver`.

    Silently no-ops if we're not in a git worktree — the project still works
    without the driver; it just means git-level conflicts on jsonl files
    would need manual resolution.
    """
    gitattr = project_root / ".gitattributes"
    existing = gitattr.read_text(encoding="utf-8") if gitattr.is_file() else ""
    if _GITATTR_MARKER not in existing:
        block = (
            ("\n" if existing and not existing.endswith("\n") else "")
            + f"{_GITATTR_MARKER}\n"
            + "\n".join(_GITATTR_LINES) + "\n"
            + f"{_GITATTR_END}\n"
        )
        gitattr.write_text(existing + block, encoding="utf-8")

    if (project_root / ".git").exists():
        try:
            subprocess.run(
                ["git", "config", "merge.hopewell-jsonl.driver",
                 "hopewell merge-driver jsonl %O %A %B"],
                cwd=str(project_root), check=True, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "config", "merge.hopewell-jsonl.name",
                 "Hopewell JSONL timestamp-sorted merge"],
                cwd=str(project_root), check=True, capture_output=True, timeout=10,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
