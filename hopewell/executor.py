"""Executor + Route — the **flow-network** model (HW-0027).

Distinction from `hopewell.model.Node` (the WorkItem / packet):

* A `Node` in v0.1..v0.6 is the *thing that flows* — a ticket / work item /
  packet.
* An `Executor` is a *station* in the flow network — agents, services,
  gates, queues, sources, targets, groups. Work items are pushed onto the
  inboxes of executors as the flow runtime (HW-0028) walks the graph.
* A `Route` is a directed edge between executors, optionally annotated
  with a predicate (`condition`) and a "required for downstream done?"
  flag (`required`).

Philosophy: **compositional, not typed.** An Executor carries a set of
components and per-component data (exactly like a Node). An `agent` is
"any executor with the `agent` component"; a `gate` is "any executor
with the `gate` component". Combinations are first-class — e.g. an
executor with `{agent, queue}` is an agent with a buffered inbox.

Stored as JSON:
    .hopewell/network/executors/<id>.json   — one file per executor
    .hopewell/network/routes.jsonl          — append-only routes log
    .hopewell/network/components/*.json     — project-custom components
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


# ---------------------------------------------------------------------------
# ExecutorComponent + Registry  (separate namespace from WorkItem components)
# ---------------------------------------------------------------------------


@dataclass
class ExecutorComponent:
    """Contract declared by an executor.

    Mirrors `hopewell.model.Component` but lives in its own registry so
    project code can reuse names like `agent` / `queue` without colliding
    with WorkItem components.
    """

    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)
    required_fields: List[str] = field(default_factory=list)

    def validate_data(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        for fld in self.required_fields:
            if fld not in data:
                errors.append(
                    f"executor-component `{self.name}`: missing required field `{fld}`"
                )
        return errors


class ExecutorComponentRegistry:
    """Separate namespace from `ComponentRegistry` (work-items).

    Projects extend via `.hopewell/network/components/*.json`.
    """

    def __init__(self) -> None:
        self._components: Dict[str, ExecutorComponent] = {}

    def register(self, component: ExecutorComponent) -> None:
        existing = self._components.get(component.name)
        if existing is not None:
            if (existing.description == component.description
                    and existing.schema == component.schema
                    and existing.required_fields == component.required_fields):
                return
            raise ValueError(
                f"executor-component `{component.name}` already registered "
                f"with a different definition"
            )
        self._components[component.name] = component

    def get(self, name: str) -> Optional[ExecutorComponent]:
        return self._components.get(name)

    def names(self) -> List[str]:
        return sorted(self._components.keys())

    def validate_executor_components(self, component_names: Iterable[str]) -> List[str]:
        errors: List[str] = []
        for name in component_names:
            if name not in self._components:
                errors.append(f"unknown executor-component: `{name}`")
        return errors


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


# Executor ids: lowercase kebab-ish with optional @ prefix (e.g. "@planner",
# "code-review", "prod-deploy"). Liberal — we don't want to police agent
# naming conventions here; the runtime uses ids as strings.
_ID_RE = re.compile(r"^[A-Za-z@][A-Za-z0-9@_\-\.]*$")


def validate_executor_id(eid: str) -> None:
    if not isinstance(eid, str) or not eid:
        raise ValueError("executor id must be a non-empty string")
    if not _ID_RE.match(eid):
        raise ValueError(
            f"malformed executor id: {eid!r} "
            f"(allowed: [A-Za-z@][A-Za-z0-9@_\\-\\.]*)"
        )


@dataclass
class Executor:
    """A station in the flow network.

    Composition-based — behaviour is inferred from `components` + their
    `component_data`. The runtime (HW-0028) dispatches work items to
    executors based on that shape.
    """

    id: str
    components: List[str] = field(default_factory=list)
    component_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    parent: Optional[str] = None        # for nesting under a `group` executor
    label: Optional[str] = None         # optional human label (display only)
    created: str = field(default_factory=lambda: _now())
    updated: str = field(default_factory=lambda: _now())
    extras: Dict[str, Any] = field(default_factory=dict)

    # ---- component helpers ----
    def has_component(self, name: str) -> bool:
        return name in self.components

    def has_all(self, names: Iterable[str]) -> bool:
        wanted = set(names)
        return wanted.issubset(set(self.components))

    def has_any(self, names: Iterable[str]) -> bool:
        return bool(set(names) & set(self.components))

    # ---- serialisation ----
    KNOWN_FIELDS = {
        "id", "components", "component_data", "parent", "label",
        "created", "updated",
    }

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "components": list(self.components),
            "created": self.created,
            "updated": self.updated,
        }
        if self.component_data:
            d["component_data"] = self.component_data
        if self.parent:
            d["parent"] = self.parent
        if self.label:
            d["label"] = self.label
        for k, v in self.extras.items():
            if k not in d:
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Executor":
        extras = {k: v for k, v in d.items() if k not in cls.KNOWN_FIELDS}
        return cls(
            id=d["id"],
            components=list(d.get("components", [])),
            component_data=dict(d.get("component_data", {})),
            parent=d.get("parent"),
            label=d.get("label"),
            created=d.get("created") or _now(),
            updated=d.get("updated") or _now(),
            extras=extras,
        )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@dataclass
class Route:
    """Directed edge in the flow network.

    Cycles are legal and expected (review loops: code-review -> @engineer
    on fail). This is NOT the work-item blocks-DAG — no cycle check.

    `required` drives "done" computation in HW-0028: an executor's work
    item is considered terminally done when every route with required=True
    out of it has been satisfied. Non-required routes are informational /
    optional paths.
    """

    from_id: str
    to_id: str
    condition: Optional[str] = None       # e.g. "on_pass", "on_fail", free-form predicate text
    label: Optional[str] = None           # display-only
    required: bool = False
    created: str = field(default_factory=lambda: _now())
    # HW-0050: free-form annotations on the route itself (e.g.
    # `auto_enforced: true` when a Hopewell git hook covers this edge).
    # Distinct from `component_data` on executors; routes don't currently
    # carry components, so this is a flat dict.
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "from": self.from_id,
            "to": self.to_id,
            "created": self.created,
        }
        if self.condition:
            d["condition"] = self.condition
        if self.label:
            d["label"] = self.label
        if self.required:
            d["required"] = True
        if self.data:
            d["data"] = dict(self.data)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Route":
        return cls(
            from_id=d["from"],
            to_id=d["to"],
            condition=d.get("condition"),
            label=d.get("label"),
            required=bool(d.get("required", False)),
            created=d.get("created") or _now(),
            data=dict(d.get("data") or {}),
        )

    def key(self) -> str:
        """Identity for dedup: from|to|condition. Two routes with the
        same (from,to) but different conditions are distinct (e.g.
        on_pass vs on_fail)."""
        return f"{self.from_id}|{self.to_id}|{self.condition or ''}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def read_executor_file(path: Path) -> Executor:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return Executor.from_dict(data)


def write_executor_file(path: Path, executor: Executor) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(executor.to_dict(), indent=2,
                         sort_keys=True, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
