"""Default flow-network template (HW-0027, expanded HW-0039).

Installed by `hopewell network defaults bootstrap`. The canonical
starting point for a Hopewell-consuming repo, modelled on the
**AgentFactory core marketplace roster** (12 agents) and the
validation-loop topology agents use to hand work off to one another.

The goals of this template:
* When a fresh project runs `network defaults bootstrap`, the Canvas
  tab shows every core agent as an executor — not just the five we
  had pre-HW-0039 — so the flow reflects the team a real project
  actually runs with.
* Back-edges are intentional (failure loops from gates return work to
  the builder). Cross-cutting agents (documentary, codemap-keeper,
  technical-writer, design-system-architect) are wired as
  subscribers on terminal events rather than strung into the main
  path so the primary flow stays readable.
* Projects are expected to edit — drop agents they don't use, add
  domain agents (e.g. @rendering-engineer, @ecs-engineer), layer
  extra gates. This template is a starting point, not a contract.

Re-running bootstrap on an existing network is idempotent (see
`write_default_template` — existing routes are preserved, template
routes are added only if not already present, and executor docs are
overwritten so divergent configs re-baseline).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from taskflow.executor import Executor, Route


# ---------------------------------------------------------------------------
# Agent roster — mirrors marketplaces/core/.claude/agents/ in AgentFactory.
# ---------------------------------------------------------------------------
# Format: (agent_id, label, short-role)
# Role strings are distilled from each agent's .md file (first-sentence
# gist). Keep them short — they render in the canvas node.
_CORE_AGENTS: List[Tuple[str, str, str]] = [
    ("@vision-keeper",           "Vision Keeper",
     "product vision authority — WHAT/WHY"),
    ("@product-manager",         "Product Manager",
     "backlog + live-ops tuning"),
    ("@planner",                 "Planner",
     "requirements discovery + spec authoring"),
    ("@architect",               "Architect",
     "design + decomposition + boundaries"),
    ("@orchestrator",            "Orchestrator",
     "dispatches work + manages gates"),
    ("@design-system-architect", "Design System Architect",
     "widget catalog + UI consistency"),
    ("@codemap-keeper",          "Codemap Keeper",
     "maintains layered codemap slices"),
    ("@devops",                  "DevOps",
     "CI/CD + deploy + live-ops infra"),
    ("@analytics",               "Analytics Engineer",
     "experiment design + RoI measurement"),
    ("@testing-qa",              "Testing/QA Strategist",
     "test discipline + fitness checks"),
    ("@technical-writer",        "Technical Writer",
     "user-facing docs, tutorials, reference"),
    ("@documentary",             "Documentary",
     "ADRs + progress stories + screenshots"),
]


def _agent(agent_id: str, label: str, role: str) -> Executor:
    """Helper — every agent is `agent + queue` with `kind=llm`."""
    return Executor(
        id=agent_id,
        label=label,
        components=["agent", "queue"],
        component_data={
            "agent": {"agent_id": agent_id, "kind": "llm", "role": role},
        },
    )


def default_template() -> Tuple[List[Executor], List[Route]]:
    # -----------------------------------------------------------------
    # Executors
    # -----------------------------------------------------------------
    executors: List[Executor] = []

    # --- sources ---
    executors.append(Executor(
        id="inbox",
        label="Inbox",
        components=["source"],
        component_data={"source": {"source_kind": "inbox"}},
    ))

    # --- agents (12, from AgentFactory core) ---
    for agent_id, label, role in _CORE_AGENTS:
        executors.append(_agent(agent_id, label, role))

    # --- services + gates ---
    # NOTE on consolidation: we intentionally keep a distinct
    # `code-review` *service/gate* even though `@code-reviewer` could
    # exist as an agent. In AgentFactory's core today there is no
    # separate @code-reviewer agent — review responsibility is
    # distributed across @architect (design review) and @testing-qa
    # (correctness review) with the gate representing the PR-level
    # pass/fail. Keeping `code-review` as a gate node lets the
    # topology represent the gate decision (pass → merge, fail →
    # rework) independently of whichever agent actually performed
    # the review. Projects that DO spawn a dedicated `@code-reviewer`
    # agent can wire it in without having to re-draw the gate.
    executors.append(Executor(
        id="code-review",
        label="Code Review",
        components=["service", "gate"],
        component_data={
            "service": {"service_kind": "code-review"},
            "gate": {"predicate_kind": "review-pass"},
        },
    ))
    executors.append(Executor(
        id="ci-pipeline",
        label="CI Pipeline",
        components=["service", "gate"],
        component_data={
            "service": {"service_kind": "ci"},
            "gate": {"predicate_kind": "ci-green"},
        },
    ))
    executors.append(Executor(
        id="uat-gate",
        label="UAT Gate",
        components=["gate"],
        component_data={"gate": {"predicate_kind": "uat-status"}},
    ))

    # --- targets ---
    executors.append(Executor(
        id="github-main",
        label="GitHub main",
        components=["service"],
        component_data={
            "service": {"service_kind": "github", "endpoint": "main"},
        },
    ))
    executors.append(Executor(
        id="prod-deploy",
        label="Production",
        components=["target"],
        component_data={"target": {"target_kind": "customer",
                                   "deployment_env": "prod"}},
    ))
    executors.append(Executor(
        id="archived",
        label="Archive",
        components=["target"],
        component_data={"target": {"target_kind": "archived"}},
    ))

    # -----------------------------------------------------------------
    # Routes — validation-loop topology
    # -----------------------------------------------------------------
    # Main path (required=True on the "happy path" edges):
    #
    #   inbox → @orchestrator → @product-manager → @planner → @architect
    #         → @engineer(*)  → code-review → github-main → ci-pipeline
    #         → uat-gate → prod-deploy → archived
    #
    # (*) AgentFactory core has no generic @engineer — implementation
    #     lives in domain-specific agents. In this template @architect
    #     routes directly to code-review to represent "build + open
    #     PR". Projects with domain engineers (e.g. @ecs-engineer,
    #     @rendering-engineer, @tool-creator) insert them between
    #     @architect and code-review locally.
    #
    # Optional fan-out from @architect to supporting agents
    # (@testing-qa authors tests, @design-system-architect validates
    # UI, @technical-writer drafts docs, @analytics wires
    # instrumentation) — these merge back at code-review.
    #
    # Cross-cutting subscribers listen on terminal events rather than
    # sitting in the happy path:
    #   * @documentary        ← archived            (capture story)
    #   * @codemap-keeper     ← github-main         (refresh codemap)
    #   * @technical-writer   ← github-main (cond)  (user-facing doc)
    #   * @design-system-arch ← github-main (cond)  (UI audit)
    #   * @analytics          ← prod-deploy         (start measuring)
    #   * @product-manager    ← prod-deploy         (live-ops tuning)
    #
    # Failure loops return work to @architect — the agent responsible
    # for decomposing the rework. Real projects may point these back
    # at a domain engineer once one exists.

    routes: List[Route] = [
        # --- intake + triage ---
        Route("inbox", "@orchestrator", required=True, label="new work"),
        Route("@orchestrator", "@vision-keeper",
              label="vision check", required=False),
        Route("@orchestrator", "@product-manager", required=True,
              label="backlog"),
        Route("@product-manager", "@planner", required=True, label="scope"),

        # --- discovery → design → build ---
        Route("@planner", "@architect", required=True, label="spec"),
        Route("@architect", "@testing-qa", label="test plan"),
        Route("@architect", "@design-system-architect",
              condition="components has ui",
              label="UI review"),
        Route("@architect", "@technical-writer",
              condition="components has user-facing",
              label="doc draft"),
        Route("@architect", "@analytics",
              condition="components has measurable",
              label="instrumentation plan"),
        Route("@architect", "code-review", required=True, label="PR"),

        # --- supporting agents converge back at the gate ---
        Route("@testing-qa", "code-review", label="tests"),
        Route("@design-system-architect", "code-review", label="UI audit"),
        Route("@technical-writer", "code-review", label="docs"),
        Route("@analytics", "code-review", label="telemetry"),

        # --- gate: code review ---
        Route("code-review", "github-main",
              condition="on_pass", required=True, label="merge"),
        Route("code-review", "@architect",
              condition="on_fail", label="rework"),

        # --- ci + uat + deploy ---
        Route("github-main", "ci-pipeline", required=True),
        Route("ci-pipeline", "uat-gate",
              condition="on_pass", required=True),
        Route("ci-pipeline", "@architect",
              condition="on_fail", label="fix"),
        Route("uat-gate", "@devops", condition="on_pass",
              label="deploy"),
        Route("@devops", "prod-deploy", required=True),
        Route("uat-gate", "@architect",
              condition="on_fail", label="rework"),
        Route("prod-deploy", "archived", required=True),

        # --- cross-cutting subscribers (non-required) ---
        Route("github-main", "@codemap-keeper", label="refresh codemap"),
        Route("github-main", "@technical-writer",
              condition="components has user-facing",
              label="update docs"),
        Route("github-main", "@design-system-architect",
              condition="components has ui",
              label="catalog audit"),
        Route("prod-deploy", "@analytics", label="start measuring"),
        Route("prod-deploy", "@product-manager", label="live-ops tuning"),
        Route("archived", "@documentary", label="capture story"),

        # --- subscriber terminals ---
        # Cross-cutters do bounded work per event and then land in
        # `archived` so the flow graph doesn't declare them as dead
        # ends. The archive represents "captured / filed" here, not
        # "the ticket is done".
        Route("@documentary", "archived", label="story captured"),
        Route("@codemap-keeper", "archived", label="codemap refreshed"),
        Route("@vision-keeper", "archived", label="vision affirmed"),
    ]

    return executors, routes


def write_default_template(project_root) -> Dict[str, int]:
    """Install the default template under `.hopewell/network/`.

    Idempotent in a sense: adding an executor that already exists is
    rewritten (overwrite=True) so re-bootstrapping brings divergent
    projects back to baseline. Existing routes are preserved — we only
    ADD the template's routes (so human-added routes survive).
    """
    from pathlib import Path
    from taskflow import network as net_mod

    root = Path(project_root)
    net_mod.ensure_network_dir(root)
    executors, routes = default_template()
    for ex in executors:
        net_mod.add_executor(root, ex, overwrite=True)
    # Dedup against existing routes so re-bootstrap is idempotent.
    existing = {r.key() for r in net_mod.load_network(root).routes}
    added = 0
    for r in routes:
        if r.key() in existing:
            continue
        net_mod.add_route(root, r)
        added += 1
    return {"executors": len(executors), "routes_added": added,
            "routes_in_template": len(routes)}
