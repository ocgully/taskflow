"""taskflow — flow-framework tool for AI-agent-driven work.

Previously shipped as `hopewell`. Renamed to `taskflow` in April 2026.
The legacy `hopewell` CLI entry point remains as a deprecation shim
that prints a stderr warning and forwards to `taskflow`.

Public API:

    from taskflow import Project, Node, Component
    from taskflow.query import ready, deps, waves
    from taskflow.orchestrator import Scheduler, Runner

Composition over typing — a node IS the components it HAS. Traditional
work types (feature, defect, epic, test, release) are component sets,
not enum values. Projects extend with custom components.

Per-project storage in `.taskflow/` (legacy: `.hopewell/`). Markdown
with YAML front-matter for human editing; JSONL event log is the
authoritative source. Agents MUST query via the CLI or this library,
not read `.taskflow/` files directly (see CLAUDE.md deny rules).

Ticket IDs: existing `HW-NNNN` IDs are immutable — they retain their
prefix forever. New tickets created post-rename use `TF-NNNN`.
"""
from __future__ import annotations

__version__ = "0.17.0"
SCHEMA_VERSION = "1"

from taskflow.model import (
    Component,
    ComponentRegistry,
    Edge,
    EdgeKind,
    Event,
    Node,
    NodeStatus,
)
from taskflow.project import CircularDependencyError, Project

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "Component",
    "ComponentRegistry",
    "CircularDependencyError",
    "Edge",
    "EdgeKind",
    "Event",
    "Node",
    "NodeStatus",
    "Project",
]
