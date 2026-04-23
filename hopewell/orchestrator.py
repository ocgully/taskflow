"""Orchestrator runtime (v0.3 basic).

Executes a `Plan` from the scheduler, dispatches each node to a matching
processor, records events, and reactive-re-plans on each completion.

Processors are registered against a component shape. Rich agent-dispatching
processors land in v0.4 (they need the attestation system). v0.3 ships
three built-in, stateless processors:

- `noop` — placeholder; just marks done. Useful for graph-only nodes.
- `shell-cmd` — runs a shell command from the node's `component_data`.
- `codemap-check` — invokes `codemap check` in the project root.

Custom Python processors register via v0.7.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from hopewell import events
from hopewell import flow as flow_mod
from hopewell.model import Node, NodeStatus
from hopewell.project import Project
from hopewell.scheduler import Plan, Scheduler


# ---------------------------------------------------------------------------
# Processor registry
# ---------------------------------------------------------------------------


@dataclass
class ProcessorOutcome:
    status: str                                     # "success" | "failure" | "skip"
    message: str = ""
    artifacts: List[str] = field(default_factory=list)


ProcessorFn = Callable[[Project, Node], ProcessorOutcome]


@dataclass
class ProcessorRule:
    name: str
    requires: Set[str]
    fn: ProcessorFn
    priority: int = 0        # higher wins when multiple match


_REGISTRY: List[ProcessorRule] = []


def processor(name: str, *, requires: Set[str], priority: int = 0) -> Callable[[ProcessorFn], ProcessorFn]:
    def deco(fn: ProcessorFn) -> ProcessorFn:
        _REGISTRY.append(ProcessorRule(name=name, requires=set(requires), fn=fn, priority=priority))
        return fn
    return deco


def match_processor(node: Node) -> Optional[ProcessorRule]:
    matches = [r for r in _REGISTRY if r.requires.issubset(set(node.components))]
    if not matches:
        return None
    # Most-specific wins (largest `requires`), tiebreak priority, tiebreak name.
    matches.sort(key=lambda r: (-len(r.requires), -r.priority, r.name))
    return matches[0]


# ---------------------------------------------------------------------------
# Built-in processors
# ---------------------------------------------------------------------------


@processor("noop", requires=set())
def _noop(project: Project, node: Node) -> ProcessorOutcome:
    return ProcessorOutcome(status="success", message="noop")


@processor("shell-cmd", requires={"shell-cmd"}, priority=10)
def _shell_cmd(project: Project, node: Node) -> ProcessorOutcome:
    cfg = node.component_data.get("shell-cmd", {})
    cmd = cfg.get("cmd")
    if not cmd:
        return ProcessorOutcome(status="failure", message="shell-cmd: missing cmd in component_data")
    cwd = cfg.get("cwd") or str(project.root)
    timeout = int(cfg.get("timeout_s", 900))
    try:
        completed = subprocess.run(
            cmd if isinstance(cmd, list) else shlex.split(cmd),
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProcessorOutcome(status="failure", message=f"shell-cmd: timed out after {timeout}s")
    if completed.returncode != 0:
        return ProcessorOutcome(status="failure",
                                message=f"shell-cmd: exit {completed.returncode}\n{completed.stderr.strip()[:2000]}")
    return ProcessorOutcome(status="success", message=f"shell-cmd: exit 0")


@processor("codemap-check", requires={"code-map"}, priority=20)
def _codemap_check(project: Project, node: Node) -> ProcessorOutcome:
    # Run `codemap check` in the project root. If codemap isn't installed,
    # succeed with a skip — don't block on unavailable tooling.
    try:
        res = subprocess.run(
            ["codemap", "check", "--format", "json"],
            cwd=str(project.root), capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ProcessorOutcome(status="skip", message="codemap CLI not available")
    if res.returncode == 0:
        return ProcessorOutcome(status="success", message="codemap check: clean")
    return ProcessorOutcome(status="failure",
                            message=f"codemap check: exit {res.returncode}\n{res.stdout[:1000]}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run_id: str
    started: str
    finished: str
    waves_executed: int
    nodes_run: List[str]
    nodes_succeeded: List[str]
    nodes_failed: List[str]
    nodes_skipped: List[str]


class Runner:
    def __init__(self, project: Project) -> None:
        self.project = project

    def execute(self, *, dry_run: bool = False, max_parallel: Optional[int] = None,
                actor: Optional[str] = None) -> RunResult:
        run_id = uuid.uuid4().hex[:12]
        started = _now()
        scheduler = Scheduler(self.project)

        events.append(self.project.events_path, "orch.run.start", actor=actor,
                      data={"run_id": run_id, "dry_run": dry_run})

        nodes_run: List[str] = []
        nodes_succeeded: List[str] = []
        nodes_failed: List[str] = []
        nodes_skipped: List[str] = []
        waves_executed = 0
        # Track every node we've already attempted this run so reactive
        # re-plans can't loop over failures or no-op nodes forever.
        attempted: set = set()

        # Reactive re-plan loop with progress guard.
        while True:
            plan = scheduler.plan(max_parallel=max_parallel)
            # Filter out nodes we've already attempted this run; if that
            # empties the current wave, advance to the next unattempted wave.
            fresh_wave: List[str] = []
            for w in plan.waves:
                fresh_wave = [nid for nid in w.nodes if nid not in attempted]
                if fresh_wave:
                    break
            if not fresh_wave:
                break
            wave = fresh_wave
            waves_executed += 1
            for nid in wave:
                attempted.add(nid)
                node = self.project.node(nid)
                rule = match_processor(node)
                # Move to doing
                if not dry_run:
                    try:
                        self.project.set_status(nid, NodeStatus.doing, actor=actor,
                                                reason=f"run {run_id}")
                    except ValueError:
                        # Can't transition (e.g., from idea). Auto-advance via ready.
                        for stepped in (NodeStatus.ready, NodeStatus.doing):
                            try:
                                self.project.set_status(nid, stepped, actor=actor,
                                                        reason=f"run {run_id}")
                            except ValueError:
                                pass

                events.append(self.project.events_path, "orch.run.node.start",
                              node=nid, actor=actor,
                              data={"run_id": run_id, "processor": rule.name if rule else None})
                nodes_run.append(nid)

                if dry_run:
                    nodes_skipped.append(nid)
                    events.append(self.project.events_path, "orch.run.node.finish",
                                  node=nid, actor=actor,
                                  data={"run_id": run_id, "outcome": "dry-run-skip"})
                    continue

                if rule is None:
                    nodes_skipped.append(nid)
                    self.project.touch(nid, f"[orch] no processor for components {node.components}; skipped",
                                       actor=actor)
                    events.append(self.project.events_path, "orch.run.node.finish",
                                  node=nid, actor=actor,
                                  data={"run_id": run_id, "outcome": "no-processor"})
                    continue

                outcome = rule.fn(self.project, node)

                events.append(self.project.events_path, "orch.run.node.finish",
                              node=nid, actor=actor,
                              data={"run_id": run_id, "outcome": outcome.status,
                                    "processor": rule.name, "message": outcome.message})

                if outcome.status == "success":
                    self.project.touch(nid, f"[orch:{rule.name}] {outcome.message}", actor=actor)
                    # Advance review -> done
                    try:
                        self.project.set_status(nid, NodeStatus.review, actor=actor)
                        self.project.set_status(nid, NodeStatus.done, actor=actor,
                                                reason=f"processor {rule.name}")
                    except ValueError:
                        pass
                    nodes_succeeded.append(nid)
                elif outcome.status == "skip":
                    self.project.touch(nid, f"[orch:{rule.name}] skip — {outcome.message}", actor=actor)
                    nodes_skipped.append(nid)
                else:
                    self.project.touch(nid, f"[orch:{rule.name}] FAIL — {outcome.message}", actor=actor)
                    # retriable?
                    retries = node.component_data.get("retriable", {}).get("max_retries", 0) \
                        if node.has_component("retriable") else 0
                    attempted = node.component_data.get("retriable", {}).get("_attempts", 0)
                    if retries and attempted < retries:
                        # Bump attempts, leave in doing for re-plan to pick up
                        node.component_data.setdefault("retriable", {})["_attempts"] = attempted + 1
                        self.project.save_node(node)
                    else:
                        try:
                            self.project.set_status(nid, NodeStatus.blocked, actor=actor,
                                                    reason=f"processor {rule.name} failed")
                        except ValueError:
                            pass
                        nodes_failed.append(nid)
            # After each wave, re-plan — if a node finished out of order and
            # unblocked downstream work, that work appears in the next plan.

        # ------------------------------------------------------------------
        # HW-0028: flow-inbox drain (second pass, additive to blocks-DAG run).
        # ------------------------------------------------------------------
        if not dry_run:
            flow_result = self._drain_flow_inboxes(run_id=run_id, actor=actor)
            # Fold flow-dispatched nodes into the main counters so the run
            # summary reflects the full picture.
            for nid in flow_result.get("succeeded", []):
                if nid not in nodes_succeeded:
                    nodes_succeeded.append(nid)
                if nid not in nodes_run:
                    nodes_run.append(nid)
            for nid in flow_result.get("failed", []):
                if nid not in nodes_failed:
                    nodes_failed.append(nid)
                if nid not in nodes_run:
                    nodes_run.append(nid)
            for nid in flow_result.get("skipped", []):
                if nid not in nodes_skipped:
                    nodes_skipped.append(nid)
                if nid not in nodes_run:
                    nodes_run.append(nid)

        finished = _now()
        events.append(self.project.events_path, "orch.run.finish", actor=actor,
                      data={"run_id": run_id, "waves": waves_executed,
                            "succeeded": len(nodes_succeeded),
                            "failed": len(nodes_failed),
                            "skipped": len(nodes_skipped)})

        # Write per-run summary
        run_dir = self.project.hw_dir / "orchestrator" / "runs"
        run_dir.mkdir(parents=True, exist_ok=True)
        summary = {
            "run_id": run_id,
            "started": started,
            "finished": finished,
            "waves_executed": waves_executed,
            "nodes_run": nodes_run,
            "nodes_succeeded": nodes_succeeded,
            "nodes_failed": nodes_failed,
            "nodes_skipped": nodes_skipped,
            "dry_run": dry_run,
        }
        (run_dir / f"{run_id}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

        return RunResult(
            run_id=run_id, started=started, finished=finished,
            waves_executed=waves_executed,
            nodes_run=nodes_run, nodes_succeeded=nodes_succeeded,
            nodes_failed=nodes_failed, nodes_skipped=nodes_skipped,
        )

    # ------------------------------------------------------------------
    # HW-0028: flow inbox drain
    # ------------------------------------------------------------------

    def _drain_flow_inboxes(self, *, run_id: str,
                            actor: Optional[str]) -> Dict[str, List[str]]:
        """Walk every executor's inbox and try to dispatch pending pushes.

        For each pending push:
          * Load the target Node.
          * Match a processor by the node's component shape (the normal
            `match_processor` mechanism).
          * If no match: ack with outcome=no-processor (doesn't enter).
          * If match: run processor.
              - success: ack outcome=success; enter the target executor;
                check for auto-done (all required terminals reached).
              - failure: ack outcome=failure; do not enter.
              - skip:    ack outcome=skip;    do not enter.

        Concurrency guard: if the push has already been ack'd by this
        executor (race between runs, or a prior manual ack), the inbox
        projection would have dropped it — so `flow.inbox()` returning
        the push means it is genuinely pending. We additionally record
        each processed push in this run's local set to avoid acking a
        push twice within the same run.

        Returns `{"succeeded": [...], "failed": [...], "skipped": [...]}`
        with WorkItem node ids (not executors).
        """
        from hopewell import network as net_mod

        succeeded: List[str] = []
        failed: List[str] = []
        skipped: List[str] = []

        try:
            net = net_mod.load_network(self.project.root)
        except Exception:  # noqa: BLE001 — no network configured, nothing to do
            return {"succeeded": succeeded, "failed": failed, "skipped": skipped}

        if not net.executors:
            return {"succeeded": succeeded, "failed": failed, "skipped": skipped}

        processed_keys: Set[tuple] = set()
        # Iterate executors in a stable order.
        for eid in sorted(net.executors.keys()):
            pending = flow_mod.inbox(self.project, eid)
            for entry in pending:
                nid = entry.get("node")
                if not nid:
                    continue
                key = (eid, nid, entry.get("pushed_at"))
                if key in processed_keys:
                    continue
                processed_keys.add(key)

                # Must still exist (work-item might have been deleted).
                if not self.project.has_node(nid):
                    skipped.append(nid)
                    continue
                node = self.project.node(nid)
                rule = match_processor(node)

                events.append(self.project.events_path, "orch.run.flow.dispatch",
                              node=nid, actor=actor,
                              data={"run_id": run_id, "executor": eid,
                                    "processor": rule.name if rule else None})

                if rule is None:
                    # Ack so the inbox drains; record no-processor outcome.
                    self.project.flow_ack(nid, eid, outcome="no-processor",
                                          note="no matching processor", actor=actor)
                    skipped.append(nid)
                    continue

                try:
                    outcome = rule.fn(self.project, node)
                except Exception as e:  # noqa: BLE001 — processor failure is data
                    outcome = ProcessorOutcome(
                        status="failure",
                        message=f"processor exception: {type(e).__name__}: {e}"
                    )

                if outcome.status == "success":
                    self.project.flow_ack(nid, eid, outcome="success",
                                          note=outcome.message[:200] if outcome.message else None,
                                          actor=actor)
                    # Enter: target now holds a location for the work item.
                    try:
                        self.project.flow_enter(nid, eid, actor=actor,
                                                reason=f"run {run_id}")
                    except Exception:  # noqa: BLE001 — enter failure shouldn't kill the drain
                        pass
                    # Location changes may have satisfied `required`
                    # terminals — auto-fire done if so.
                    try:
                        flow_mod.maybe_auto_done(self.project, nid, actor=actor)
                    except Exception:  # noqa: BLE001
                        pass
                    succeeded.append(nid)
                elif outcome.status == "skip":
                    self.project.flow_ack(nid, eid, outcome="skip",
                                          note=outcome.message[:200] if outcome.message else None,
                                          actor=actor)
                    skipped.append(nid)
                else:
                    self.project.flow_ack(nid, eid, outcome="failure",
                                          note=outcome.message[:200] if outcome.message else None,
                                          actor=actor)
                    failed.append(nid)

        return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
