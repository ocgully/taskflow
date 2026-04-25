"""Built-in executor components (HW-0027).

Separate registry from WorkItem components (`hopewell.model.Component`).
Projects may extend by dropping JSON files into
`.hopewell/network/components/*.json` — loaded by
`hopewell.network.load_registry`.

Schema fields are indicative, not strictly enforced — the minimal rule is
`required_fields`. Projects are free to add custom fields; they will
round-trip through `component_data` untouched.
"""
from __future__ import annotations

from typing import List

from taskflow.executor import ExecutorComponent


BUILTIN_EXECUTOR_COMPONENTS: List[ExecutorComponent] = [
    ExecutorComponent(
        name="agent",
        description=("LLM / human / mixed executor. Picks up work items, "
                     "acts on them, and pushes them onto downstream "
                     "executor inboxes."),
        schema={
            "agent_id": "string (e.g. @planner, @engineer)",
            "kind": "enum: human | llm | mixed",
            "role": "string (free-form description)",
        },
    ),
    ExecutorComponent(
        name="service",
        description=("External service integration — GitHub, CI, package "
                     "registries, deployment targets with an API."),
        schema={
            "service_kind": "string (github | ci | package-registry | ...)",
            "endpoint": "string (URL, optional)",
            "auth_env": "string (env var holding a credential, optional)",
        },
    ),
    ExecutorComponent(
        name="gate",
        description=("Automated pass/fail predicate. Routes downstream "
                     "based on an evaluation."),
        schema={
            "predicate_kind": "string (comp-check | uat-status | manual | ...)",
            "on_pass_route": "string (downstream executor id, optional)",
            "on_fail_route": "string (downstream executor id, optional)",
        },
    ),
    ExecutorComponent(
        name="target",
        description=("Terminal sink. Work enters and comes to rest — "
                     "customers, internal envs, archive."),
        schema={
            "target_kind": "enum: customer | internal | archived",
            "deployment_env": "string (prod | staging | internal | ...)",
        },
    ),
    ExecutorComponent(
        name="source",
        description=("Origin — work enters the network here (inbox, "
                     "GitHub-issue-ingest, timer, webhook, etc.)."),
        schema={
            "source_kind": "string (inbox | github-issue-ingest | webhook | ...)",
        },
    ),
    ExecutorComponent(
        name="queue",
        description=("FIFO holding. Pairs with agent/service to buffer "
                     "work items waiting to be processed."),
        schema={
            "max_size": "integer (optional; None = unbounded)",
            "overflow_policy": "enum: block | drop-oldest | drop-newest (optional)",
        },
    ),
    ExecutorComponent(
        name="group",
        description=("Nests child executors — a sub-network. Children "
                     "are identified by their `parent` field pointing "
                     "at this group's id."),
        schema={
            "children": "array of executor ids (denormalised convenience; "
                        "authoritative source is each child's `parent` field)",
        },
    ),
]
