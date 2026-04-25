"""Core data model — Nodes, Components, Edges, Events.

Philosophy: composition over typing. A node IS the set of components it HAS.
Processors discover by component shape; projects extend by adding new
components rather than forking the code.

All types are dataclasses for dict-friendliness and light JSON round-trips.
"""
from __future__ import annotations

import dataclasses
import datetime
import enum
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class NodeStatus(str, enum.Enum):
    """Strict state machine. Transitions enforced by `Node.set_status`."""

    idea = "idea"
    blocked = "blocked"
    ready = "ready"
    doing = "doing"
    review = "review"
    done = "done"
    archived = "archived"
    cancelled = "cancelled"


# Allowed status transitions (state-machine).
# key = from, value = set of legal `to` states.
STATUS_TRANSITIONS: Dict[NodeStatus, Set[NodeStatus]] = {
    NodeStatus.idea:      {NodeStatus.blocked, NodeStatus.ready, NodeStatus.cancelled, NodeStatus.archived},
    NodeStatus.blocked:   {NodeStatus.ready, NodeStatus.cancelled, NodeStatus.archived},
    NodeStatus.ready:     {NodeStatus.doing, NodeStatus.blocked, NodeStatus.cancelled, NodeStatus.archived},
    NodeStatus.doing:     {NodeStatus.review, NodeStatus.blocked, NodeStatus.ready, NodeStatus.cancelled},
    NodeStatus.review:    {NodeStatus.done, NodeStatus.doing, NodeStatus.blocked, NodeStatus.cancelled},
    NodeStatus.done:      {NodeStatus.archived, NodeStatus.doing},   # reopen via `doing`
    NodeStatus.archived:  set(),
    NodeStatus.cancelled: {NodeStatus.archived},
}

TERMINAL_STATUSES: Set[NodeStatus] = {NodeStatus.done, NodeStatus.archived, NodeStatus.cancelled}


class EdgeKind(str, enum.Enum):
    blocks = "blocks"           # upstream must reach a terminal status before downstream can start
    produces = "produces"       # artifact flow: upstream produces something downstream consumes
    consumes = "consumes"       # inverse of produces, stored separately for fast reverse lookup
    parent = "parent"           # grouping: downstream is a child of upstream
    related = "related"         # informational, no execution constraint
    references = "references"   # A consults / cites B — no execution constraint.
                                # Introduced with HW-0033 (comment-review promotion)
                                # so a review node can point back at the node it
                                # commented on. Distinct from `related`: this
                                # asserts directional consultation ("A references
                                # B"), not a symmetric affinity.


# ---------------------------------------------------------------------------
# Component
# ---------------------------------------------------------------------------


@dataclass
class Component:
    """A component is a contract. Declaring one on a node announces that the
    node participates in a role the orchestrator and processors may recognise.
    """

    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)   # JSON-Schema-ish for component_data
    required_fields: List[str] = field(default_factory=list)

    def validate_data(self, data: Dict[str, Any]) -> List[str]:
        """Return list of validation errors. Empty list = valid."""
        errors: List[str] = []
        for fld in self.required_fields:
            if fld not in data:
                errors.append(f"component `{self.name}`: missing required field `{fld}`")
        return errors


class ComponentRegistry:
    """Loadable registry of components. Projects extend via .hopewell/components/*.yaml."""

    def __init__(self) -> None:
        self._components: Dict[str, Component] = {}

    def register(self, component: Component) -> None:
        existing = self._components.get(component.name)
        if existing is not None:
            # Idempotent re-registration: silently accept the exact same shape
            # (common when a project's Python code reloads extensions or when
            # `Project.load` is called twice in one process). Conflicting
            # shape is still an error — loud failure beats mystery.
            if (existing.description == component.description
                    and existing.schema == component.schema
                    and existing.required_fields == component.required_fields):
                return
            raise ValueError(
                f"component `{component.name}` already registered with a "
                f"different definition"
            )
        self._components[component.name] = component

    def get(self, name: str) -> Optional[Component]:
        return self._components.get(name)

    def names(self) -> List[str]:
        return sorted(self._components.keys())

    def validate_node_components(self, component_names: Iterable[str]) -> List[str]:
        """Return errors if any component is unknown."""
        errors: List[str] = []
        for name in component_names:
            if name not in self._components:
                errors.append(f"unknown component: `{name}`")
        return errors


# Built-in component definitions shipped with Hopewell v1.
BUILTIN_COMPONENTS: List[Component] = [
    Component(
        name="work-item",
        description="Trackable unit of effort.",
        schema={"estimate_hours": "number", "priority": "string"},
    ),
    Component(
        name="deliverable",
        description="Produces a concrete artifact.",
        schema={"definition_of_done": "array", "acceptance": "array"},
    ),
    Component(
        name="user-facing",
        description="Reaches end users; release-noteable.",
        schema={"persona": "string", "release_notes": "string"},
    ),
    Component(
        name="internal",
        description="Internal-only work; no user-facing impact.",
    ),
    Component(
        name="defect",
        description="Fixes a regression or bug.",
        schema={"root_cause": "string", "affected_versions": "array"},
    ),
    Component(
        name="risk",
        description="Security / compliance / governance concern.",
        schema={"risk_category": "string", "severity": "string"},
    ),
    Component(
        name="debt",
        description="Removes future impediment.",
        schema={"blocks_which_future_work": "array"},
    ),
    Component(
        name="test",
        description="Validates something.",
        schema={"test_kind": "string", "target_node": "string"},
    ),
    Component(
        name="documentation",
        description="Text artifact for humans or agents.",
        schema={"doc_kind": "string", "audience": "string"},
    ),
    Component(
        name="screenshot",
        description="Visual artifact.",
        schema={"captures_what": "string", "device": "string"},
    ),
    Component(
        name="design",
        description="Design artifact (mockup, spec, ADR).",
        schema={"tool": "string", "link": "string"},
    ),
    Component(
        name="code-map",
        description="Links to a codemap query whose result gates this node.",
        schema={"query": "string", "expected": "string"},
    ),
    Component(
        name="grouping",
        description="Aggregates child nodes — epic / story / release.",
        schema={"children_query": "string"},
    ),
    Component(
        name="deployment-target",
        description="Declares where work must land.",
        schema={"target_env": "string"},
        required_fields=["target_env"],
    ),
    Component(
        name="approval-gate",
        description="Requires human sign-off.",
        schema={"approvers": "array", "approval_criteria": "string"},
    ),
    Component(
        name="flagged",
        description="Live-ops feature-flag gated.",
        schema={"flag_name": "string", "rollout_plan": "string"},
        required_fields=["flag_name"],
    ),
    Component(
        name="retriable",
        description="Orchestrator retries on failure.",
        schema={"max_retries": "integer", "backoff": "string"},
    ),
    Component(
        name="github-issue",
        description="Originated from a GitHub issue; maintains linkback.",
        schema={"repo": "string", "number": "integer", "url": "string", "gh_state": "string"},
        required_fields=["repo", "number"],
    ),
    Component(
        name="loop",
        description=(
            "Iterative subgraph driver (v0.6). The orchestrator walks the "
            "nodes listed in `over` up to `max_iterations` times or until "
            "the `until` predicate is satisfied. Each pass is recorded in "
            "`iterations`. Created via `hopewell.evolve.add_loop`."
        ),
        schema={
            "over": "array of node ids (the subgraph to iterate)",
            "until": "string (predicate describing exit condition)",
            "max_iterations": "integer (safety ceiling)",
            "iterations": "array (append-only run records)",
        },
    ),
    Component(
        name="needs-uat",
        description=("Requires user-acceptance testing. Internal tests passing "
                     "is not sufficient; a human has to verify against "
                     "acceptance criteria before this node is truly shipped."),
        schema={
            "status": "enum: pending | passed | failed | waived",
            "acceptance_criteria": "array of strings",
            "verified_by": "string (agent or human name)",
            "verified_at": "iso-ts",
            "notes": "string",
            "failure_reason": "string",
        },
    ),
    Component(
        name="spec-input",
        description=(
            "Quote-by-reference to specific passages of spec files. Each "
            "recorded slice carries a content hash for drift detection and "
            "a `why` to remind the consumer what they're relying on. See "
            "`hopewell.spec_input` + `taskflow spec-ref ...` CLI."
        ),
        schema={
            "specs": (
                "array of {path, doc_sha, slices=[{anchor?, lines=[N,M], "
                "slice_sha, why?}]}"
            ),
        },
    ),
    Component(
        name="downstream-review",
        description=(
            "Reconciliation node generated when a consumed spec slice "
            "drifts (HW-0034). Pins the consumer node id, the spec path, "
            "the specific slice that drifted, and a snapshot of the drift "
            "(recorded vs current sha + unified diff). Holds a `blocks` "
            "edge over the consumer until resolved with one of four "
            "outcomes (no-impact, update-in-scope, update-out-of-scope, "
            "spec-revert). See `hopewell.reconciliation` + the "
            "`taskflow reconcile ...` CLI."
        ),
        schema={
            "consumer_node": "string (the work item that referenced the slice)",
            "spec_path": "string (project-relative spec path)",
            "slice": (
                "object {anchor?: string, lines: [start, end]} — same "
                "selector shape as spec-input slices"
            ),
            "drift_snapshot": (
                "object {recorded_slice_sha, current_slice_sha, patch, state}"
            ),
            "trigger": "enum: spec-edit | pickup-gate",
            "status": "enum: open | resolved",
            "outcome": (
                "enum: no-impact | update-in-scope | update-out-of-scope | "
                "spec-revert | null"
            ),
            "resolution_notes": "string (optional)",
            "followup_node": "string (set when outcome=update-out-of-scope)",
        },
        required_fields=["consumer_node", "spec_path"],
    ),
    Component(
        name="release",
        description=(
            "Release record (HW-0043). Represents a cut / candidate cut "
            "of a versioned bundle of work. Owns scope (the set of "
            "work-item nodes included), a confidence score, a "
            "standardized report, and the final-gate outcome (draft, "
            "held, released, kicked-back). See `hopewell.release` + "
            "the `taskflow release ...` CLI, and the @release-engineer "
            "core agent doc for workflow context."
        ),
        schema={
            "version":          "string (project-defined; e.g. v0.15.0)",
            "scope_nodes":      "array of node ids included in this release",
            "confidence_score": "integer 0-100 (persisted at finalize)",
            "report_path":      "string (relative, e.g. .hopewell/releases/v0.15.0.md)",
            "tag":              "string (git tag created on finalize, if any)",
            "released_at":      "iso-ts (set on successful finalize)",
            "released_by":      "string (agent or human handle)",
            "status":           "enum: draft | held | released | kicked-back",
            "kickback":         (
                "optional object {root_cause, affected, route_to, "
                "rework_node, created_at} populated on kickback"
            ),
            "score_breakdown":  (
                "optional array of {name, weight, score, justification} "
                "— persisted on finalize for audit"
            ),
        },
        required_fields=["version"],
    ),
    Component(
        name="comment-review",
        description=(
            "Review node promoted from a comment thread (HW-0033). "
            "component_data pins the originating thread id + the node (or "
            "spec path) the comment was anchored on so the review can be "
            "traced back to the discussion that spawned it. Always paired "
            "with a `references` edge to the commented-on node."
        ),
        schema={
            "thread_id": "string (comment id — e.g. c-abc123)",
            "commented_on": "string (node id OR spec path, same shape as anchor target)",
            "anchor": (
                "object {type: whole-file|heading-section|line-range, "
                "heading_slug?, lines?, content_hash?, explicit_anchor?}"
            ),
        },
        required_fields=["thread_id", "commented_on"],
    ),
]


def default_registry() -> ComponentRegistry:
    """A fresh registry populated with all built-in components."""
    reg = ComponentRegistry()
    for c in BUILTIN_COMPONENTS:
        reg.register(c)
    return reg


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class NodeInput:
    """Declares something this node needs before it can run."""

    from_node: Optional[str] = None            # upstream node id (if from-upstream)
    artifact: Optional[str] = None             # artifact path produced by the upstream
    kind: Optional[str] = None                 # external kind (design, spec, feedback, etc.)
    description: Optional[str] = None
    required: bool = True


@dataclass
class NodeLocation:
    """A work-item's presence at an Executor (HW-0028).

    A WorkItem can hold multiple locations simultaneously — e.g. it sits
    at @architect while an Engineering branch runs in parallel. A
    location is "active" while `left_at` is None; it becomes a
    historical record once set.

    `last_artifact` is an optional hint about the most recent artifact
    produced at this executor (for UI + push reasoning).
    """

    executor_id: str
    entered_at: str                            # iso ts
    left_at: Optional[str] = None              # set on exit
    last_artifact: Optional[str] = None

    def is_active(self) -> bool:
        return self.left_at is None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "executor_id": self.executor_id,
            "entered_at": self.entered_at,
        }
        if self.left_at is not None:
            d["left_at"] = self.left_at
        if self.last_artifact is not None:
            d["last_artifact"] = self.last_artifact
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "NodeLocation":
        return cls(
            executor_id=d.get("executor_id") or d.get("executor") or "",
            entered_at=d.get("entered_at") or _now(),
            left_at=d.get("left_at"),
            last_artifact=d.get("last_artifact"),
        )


@dataclass
class NodeOutput:
    """Declares something this node produces when it completes."""

    path: Optional[str] = None                 # artifact path
    kind: Optional[str] = None                 # artifact kind (code, doc, fixture, signal)
    signal: Optional[str] = None               # abstract completion signal (e.g. "ready-to-review")


@dataclass
class Node:
    """Primary work unit. Composition-based: its capabilities and the
    processors that handle it are determined by `components`, not a type enum."""

    id: str
    title: str
    status: NodeStatus = NodeStatus.idea
    priority: str = "P2"                               # P0..P3
    created: str = field(default_factory=lambda: _now())
    updated: str = field(default_factory=lambda: _now())
    owner: Optional[str] = None
    project: Optional[str] = None
    parent: Optional[str] = None
    components: List[str] = field(default_factory=list)
    inputs: List[NodeInput] = field(default_factory=list)
    outputs: List[NodeOutput] = field(default_factory=list)
    blocks: List[str] = field(default_factory=list)
    blocked_by: List[str] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    # HW-0033: directional "A references B" edge material (e.g. a
    # comment-review node pointing back at the node it commented on).
    # Separate from `related` so it's unambiguously directional.
    references: List[str] = field(default_factory=list)
    component_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # v0.8 (HW-0028): multi-location presence in the flow network.
    # A WorkItem can be at several Executors at once; each NodeLocation
    # records entry/exit timestamps. Orthogonal to `status` and `claim`.
    locations: List[NodeLocation] = field(default_factory=list)
    body: str = ""                                     # free-form markdown body (not notes)
    notes: List[str] = field(default_factory=list)     # append-only
    # v0.5.2: preserve unknown front-matter fields across round-trips so that
    # an older Hopewell editing a newer-format node doesn't silently drop
    # fields it doesn't understand.
    extras: Dict[str, Any] = field(default_factory=dict)

    # ---- status transitions ----
    def can_transition_to(self, new_status: NodeStatus) -> bool:
        try:
            cur = NodeStatus(self.status) if not isinstance(self.status, NodeStatus) else self.status
        except ValueError:
            return False
        return new_status in STATUS_TRANSITIONS.get(cur, set())

    # ---- component helpers ----
    def has_component(self, name: str) -> bool:
        return name in self.components

    def has_all(self, names: Iterable[str]) -> bool:
        wanted = set(names)
        return wanted.issubset(set(self.components))

    def has_any(self, names: Iterable[str]) -> bool:
        return bool(set(names) & set(self.components))

    # ---- serialisation ----
    # Fields we know how to round-trip. Anything ELSE in the front-matter is
    # preserved as-is via `extras` so older Hopewells don't destroy newer data.
    KNOWN_FIELDS = {
        "id", "status", "priority", "created", "updated",
        "owner", "project", "parent", "components",
        "inputs", "outputs", "blocks", "blocked_by", "related",
        "references", "component_data", "locations",
    }

    # ---- flow/location helpers (HW-0028) ----
    def active_locations(self) -> List["NodeLocation"]:
        return [loc for loc in self.locations if loc.is_active()]

    def location_at(self, executor_id: str) -> Optional["NodeLocation"]:
        """Return the most recent ACTIVE location at `executor_id`, if any."""
        for loc in reversed(self.locations):
            if loc.executor_id == executor_id and loc.is_active():
                return loc
        return None

    def to_frontmatter(self) -> Dict[str, Any]:
        """Convert to the dict we'll serialise into YAML front-matter."""
        d: Dict[str, Any] = {
            "id": self.id,
            "status": self.status.value if isinstance(self.status, NodeStatus) else self.status,
            "priority": self.priority,
            "created": self.created,
            "updated": self.updated,
        }
        if self.owner:
            d["owner"] = self.owner
        if self.project:
            d["project"] = self.project
        if self.parent:
            d["parent"] = self.parent
        if self.components:
            d["components"] = list(self.components)
        if self.inputs:
            d["inputs"] = [_dataclass_to_dict_sparse(i) for i in self.inputs]
        if self.outputs:
            d["outputs"] = [_dataclass_to_dict_sparse(o) for o in self.outputs]
        if self.blocks:
            d["blocks"] = list(self.blocks)
        if self.blocked_by:
            d["blocked_by"] = list(self.blocked_by)
        if self.related:
            d["related"] = list(self.related)
        if self.references:
            d["references"] = list(self.references)
        if self.component_data:
            d["component_data"] = self.component_data
        if self.locations:
            d["locations"] = [loc.to_dict() for loc in self.locations]
        # Preserve any fields a newer Hopewell wrote that we don't recognise.
        for k, v in self.extras.items():
            if k not in d:
                d[k] = v
        return d

    @classmethod
    def from_frontmatter(cls, fm: Dict[str, Any], *, title: str, body: str, notes: List[str]) -> "Node":
        extras = {k: v for k, v in fm.items() if k not in cls.KNOWN_FIELDS}
        return cls(
            id=fm["id"],
            title=title,
            status=NodeStatus(fm.get("status", "idea")),
            priority=fm.get("priority", "P2"),
            created=fm.get("created") or _now(),
            updated=fm.get("updated") or _now(),
            owner=fm.get("owner"),
            project=fm.get("project"),
            parent=fm.get("parent"),
            components=list(fm.get("components", [])),
            inputs=[NodeInput(**_coerce_input(i)) for i in (fm.get("inputs") or [])],
            outputs=[NodeOutput(**_coerce_output(o)) for o in (fm.get("outputs") or [])],
            blocks=list(fm.get("blocks", [])),
            blocked_by=list(fm.get("blocked_by", [])),
            related=list(fm.get("related", [])),
            references=list(fm.get("references", [])),
            component_data=dict(fm.get("component_data", {})),
            locations=[NodeLocation.from_dict(x) for x in (fm.get("locations") or [])
                       if isinstance(x, dict)],
            body=body,
            notes=notes,
            extras=extras,
        )


# ---------------------------------------------------------------------------
# Edge + Event
# ---------------------------------------------------------------------------


@dataclass
class Edge:
    """An explicit edge — when stored in edges.jsonl with a rationale."""
    from_id: str
    to_id: str
    kind: EdgeKind
    artifact: Optional[str] = None
    reason: Optional[str] = None
    created: str = field(default_factory=lambda: _now())


@dataclass
class Event:
    """Append-only record of a graph mutation."""
    ts: str
    kind: str                                          # e.g. node.status.change, node.create
    node: Optional[str] = None
    actor: Optional[str] = None                        # agent id or human handle
    data: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dataclass_to_dict_sparse(obj) -> Dict[str, Any]:
    """dataclass -> dict, dropping None values for cleaner front-matter."""
    if dataclasses.is_dataclass(obj):
        d = dataclasses.asdict(obj)
        return {k: v for k, v in d.items() if v not in (None, [], {}, "")}
    return obj


def _coerce_input(x: Any) -> Dict[str, Any]:
    """Accept either a dict or NodeInput; return the kwargs-ready dict."""
    if isinstance(x, NodeInput):
        return dataclasses.asdict(x)
    if isinstance(x, dict):
        return {k: x.get(k) for k in ("from_node", "artifact", "kind", "description", "required")}
    raise TypeError(f"unexpected input shape: {type(x).__name__}")


def _coerce_output(x: Any) -> Dict[str, Any]:
    if isinstance(x, NodeOutput):
        return dataclasses.asdict(x)
    if isinstance(x, dict):
        return {k: x.get(k) for k in ("path", "kind", "signal")}
    raise TypeError(f"unexpected output shape: {type(x).__name__}")


# Node id generation: "<PREFIX>-<ZERO-PADDED-N>".
_ID_RE = re.compile(r"^([A-Z][A-Z0-9]*)-(\d+)$")


def parse_node_id(node_id: str) -> Tuple[str, int]:
    m = _ID_RE.match(node_id)
    if not m:
        raise ValueError(f"malformed node id: {node_id!r} (expected `<PREFIX>-<N>`)")
    return m.group(1), int(m.group(2))


def format_node_id(prefix: str, n: int, *, pad: int = 4) -> str:
    return f"{prefix}-{n:0{pad}d}"


# Content-address helper (for agent fingerprinting — lives elsewhere but the
# primitive is universal enough to put here).
def sha_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
