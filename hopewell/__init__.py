"""Hopewell — flow-framework tool for AI-agent-driven work.

Public API:

    from hopewell import Project, Node, Component
    from hopewell.query import ready, deps, waves
    from hopewell.orchestrator import Scheduler, Runner

Composition over typing — a node IS the components it HAS. Traditional
work types (feature, defect, epic, test, release) are component sets,
not enum values. Projects extend with custom components.

Per-project storage in `.hopewell/`. Markdown with YAML front-matter for
human editing; JSONL event log is the authoritative source. Agents MUST
query via the CLI or this library, not read `.hopewell/` files directly
(see CLAUDE.md deny rules).
"""
from __future__ import annotations

__version__ = "0.16.0"
SCHEMA_VERSION = "1"

from hopewell.model import (
    Component,
    ComponentRegistry,
    Edge,
    EdgeKind,
    Event,
    Node,
    NodeStatus,
)
from hopewell.project import CircularDependencyError, Project

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
