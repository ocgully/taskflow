"""Hopewell CLI — argparse over the library. v0.1 + v0.2 + v0.3 commands."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

from hopewell import __version__, SCHEMA_VERSION
from hopewell import attestation as att_mod
from hopewell import claim as claim_mod
from hopewell import events as events_mod
from hopewell import evolve as evolve_mod
from hopewell import extensions as extensions_mod
from hopewell import hooks as hooks_mod
from hopewell import merge_driver as merge_driver_mod
from hopewell import network_cli as network_cli_mod
from hopewell import paths as paths_mod
from hopewell import resume as resume_mod
from hopewell import uat as uat_mod
from hopewell.model import EdgeKind, NodeStatus
from hopewell.project import CircularDependencyError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project(args):
    from hopewell.project import Project
    start = Path(args.project_root).resolve() if args.project_root else None
    return Project.load(start)


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def _render_table(headers: List[str], rows: List[List[str]], *,
                  max_widths: Optional[List[Optional[int]]] = None) -> str:
    """Deterministic markdown table. Cells that exceed their max width get
    truncated with an ellipsis. `max_widths=None` means no cap for that col."""
    if not rows:
        widths = [len(h) for h in headers]
    else:
        widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(str(cell)))
    if max_widths:
        for i, cap in enumerate(max_widths):
            if cap is not None and widths[i] > cap:
                widths[i] = cap

    def trunc(s: str, w: int) -> str:
        s = str(s)
        if len(s) <= w:
            return s.ljust(w)
        return (s[: w - 1] + "…").ljust(w)

    lines = [
        "| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |",
        "|" + "|".join("-" * (widths[i] + 2) for i in range(len(headers))) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(trunc(row[i], widths[i]) for i in range(len(row))) + " |")
    return "\n".join(lines)


def _components_summary(comps: List[str], max_count: int = 3) -> str:
    if not comps:
        return "—"
    head = ", ".join(comps[:max_count])
    if len(comps) > max_count:
        head += f" +{len(comps) - max_count}"
    return head


def _actor_from_env() -> Optional[str]:
    return os.environ.get("HOPEWELL_ACTOR") or os.environ.get("GIT_AUTHOR_NAME")


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def cmd_init(args) -> int:
    from hopewell.project import Project
    root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    project = Project.init(root, id_prefix=args.prefix, name=args.name)
    if not args.quiet:
        print(f"Initialized {project.hw_dir}")
        print(f"  project name: {project.cfg.name}")
        print(f"  id_prefix:    {project.cfg.id_prefix}")
        print("  next steps:   `hopewell new --components work-item --title \"...\"`")
    return 0


# ---------------------------------------------------------------------------
# new / show / list / touch / link / close / check / graph / render / info
# ---------------------------------------------------------------------------


def cmd_new(args) -> int:
    project = _project(args)
    components = [c.strip() for c in args.components.split(",") if c.strip()]
    node = project.new_node(
        components=components,
        title=args.title,
        owner=args.owner,
        parent=args.parent,
        priority=args.priority,
        actor=_actor_from_env(),
    )
    if args.format == "json":
        from hopewell.query import show
        _print_json(show(project, node.id))
    else:
        print(f"Created {node.id} — {node.title}")
        print(f"  components: {', '.join(node.components)}")
        print(f"  file:       {project.node_path(node.id)}")
    return 0


def cmd_show(args) -> int:
    project = _project(args)
    from hopewell.query import show
    data = show(project, args.id)
    if args.format == "json":
        _print_json(data)
    else:
        n = data["node"]
        print(f"# {n['id']} — {n['title']}")
        print(f"Status:     {n['status']}   Priority: {n['priority']}   Owner: {n['owner'] or '—'}")
        print(f"Components: {', '.join(n['components'])}")
        if n["blocked_by"]:
            print(f"Blocked by: {', '.join(n['blocked_by'])}")
        if n["blocks"]:
            print(f"Blocks:     {', '.join(n['blocks'])}")
        if n.get("inputs"):
            print("Inputs:")
            for i in n["inputs"]:
                print(f"  - {i}")
        if n.get("outputs"):
            print("Outputs:")
            for o in n["outputs"]:
                print(f"  - {o}")
        if n.get("body"):
            print("\n" + n["body"])
        if n.get("notes"):
            print("\nNotes:")
            for note in n["notes"]:
                print(f"  - {note}")
    return 0


def cmd_list(args) -> int:
    project = _project(args)
    from hopewell.query import list_nodes
    data = list_nodes(project, status=args.status, component=args.component,
                      has_all=(args.has_all.split(",") if args.has_all else None),
                      owner=args.owner)
    if args.format == "json":
        _print_json(data)
    else:
        print(f"{data['count']} node(s)")
        if data["nodes"]:
            rows = [
                [n["id"], n["status"], n["priority"], n["owner"] or "—",
                 n["title"], _components_summary(n["components"])]
                for n in data["nodes"]
            ]
            print(_render_table(
                ["ID", "Status", "Pri", "Owner", "Title", "Components"],
                rows,
                max_widths=[10, 9, 3, 18, 55, 36],
            ))
    return 0


def cmd_ready(args) -> int:
    project = _project(args)
    from hopewell.query import ready
    data = ready(project, owner=args.owner)
    if args.format == "json":
        _print_json(data)
    else:
        print(f"{data['count']} ready node(s)"
              + (f" (excluded claimed: {', '.join(data['excluded_claimed'])})"
                 if data.get("excluded_claimed") else ""))
        if data["nodes"]:
            rows = [
                [n["id"], n["priority"], n["owner"] or "—",
                 n["title"], _components_summary(n["components"])]
                for n in data["nodes"]
            ]
            print(_render_table(
                ["ID", "Pri", "Owner", "Title", "Components"],
                rows,
                max_widths=[10, 3, 18, 60, 36],
            ))
    return 0


def cmd_touch(args) -> int:
    project = _project(args)
    project.touch(args.id, args.note, actor=_actor_from_env())
    if not args.quiet:
        print(f"Appended note to {args.id}")
    return 0


def cmd_link(args) -> int:
    project = _project(args)
    try:
        kind = EdgeKind(args.kind)
    except ValueError:
        print(f"hopewell: unknown edge kind '{args.kind}' — "
              f"expected one of {[e.value for e in EdgeKind]}", file=sys.stderr)
        return 1
    try:
        edge = project.link(args.from_id, kind, args.to, artifact=args.artifact,
                            reason=args.reason, actor=_actor_from_env())
    except CircularDependencyError as exc:
        # Surface the cycle path; let the caller decide what to break.
        print(f"hopewell: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"{edge.from_id} --[{edge.kind.value if hasattr(edge.kind,'value') else edge.kind}]--> {edge.to_id}")
    return 0


def cmd_close(args) -> int:
    project = _project(args)
    project.close(args.id, commit=args.commit, reason=args.reason, actor=_actor_from_env())
    if not args.quiet:
        print(f"Closed {args.id}")
    return 0


def cmd_check(args) -> int:
    project = _project(args)
    problems = project.check()
    if args.format == "json":
        _print_json({"problems": problems, "clean": not problems})
        return 0 if not problems else 1
    if not problems:
        print("hopewell check: clean.")
        return 0
    print(f"hopewell check: {len(problems)} problem(s)")
    for p in problems:
        print(f"  - {p}")
    return 1


def cmd_graph(args) -> int:
    project = _project(args)
    from hopewell.render import views as views_mod
    content = views_mod.graph(project.all_nodes())
    sys.stdout.write(content)
    return 0


def cmd_render(args) -> int:
    project = _project(args)
    from hopewell.render import views as views_mod
    out = views_mod.render_all(project)
    if not args.quiet:
        for name in out:
            print(f"Rendered {project.views_dir / name}")
    return 0


def cmd_claim(args) -> int:
    project = _project(args)
    try:
        c = claim_mod.claim(project, args.id, slug=args.slug, offline=args.offline,
                            base=args.base, actor=_actor_from_env(), push=not args.no_push)
    except claim_mod.ClaimCollision as exc:
        ex = exc.existing
        _print_json({
            "claim": "collision",
            "branch": exc.branch,
            "existing": ex.to_dict() if ex else None,
            "hint": "Pick another ready task or ask the claimer to release.",
        })
        return 1
    except FileNotFoundError as exc:
        print(f"hopewell: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"hopewell: git failed — {exc.stderr.strip() if exc.stderr else exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        _print_json(c.to_dict())
    else:
        mode = "local-only" if c.local else "pushed"
        print(f"claimed {c.node_id} on branch {c.branch} ({mode})")
        if not c.local:
            print(f"  upstream: origin/{c.branch}")
        print(f"  next: start your work, then `hopewell close {c.node_id} ...` when done.")
    return 0


def cmd_release(args) -> int:
    project = _project(args)
    deleted = claim_mod.release(project, args.id, actor=_actor_from_env(),
                                delete_remote=not args.keep_remote)
    if args.format == "json":
        _print_json({"node": args.id, "deleted_branches": deleted})
    else:
        if deleted:
            print(f"released {args.id}: deleted {len(deleted)} branch(es)")
            for b in deleted:
                print(f"  - {b}")
        else:
            print(f"no claim branches found for {args.id}")
    return 0


def cmd_prune_claims(args) -> int:
    project = _project(args)
    pruned = claim_mod.prune_stale(project, stale_days=args.stale_days,
                                   actor=_actor_from_env())
    if args.format == "json":
        _print_json({"stale_days": args.stale_days, "pruned": pruned})
    else:
        if not pruned:
            print(f"no stale claims (>{args.stale_days}d) found")
        else:
            print(f"pruned {len(pruned)} stale claim(s):")
            for b in pruned:
                print(f"  - {b}")
    return 0


def cmd_merge_driver(args) -> int:
    # Invoked by git: `hopewell merge-driver jsonl <ancestor> <ours> <theirs>`
    return merge_driver_mod.run_cli([args.kind, args.ancestor, args.ours, args.theirs])


def cmd_uat(args) -> int:
    project = _project(args)
    actor = _actor_from_env()

    if args.action == "flag":
        criteria = [c for c in (args.criteria or []) if c.strip()]
        block = uat_mod.flag(project, args.id, acceptance_criteria=criteria or None, actor=actor)
        _print_json({"node": args.id, "uat": block})
        return 0

    if args.action == "unflag":
        uat_mod.unflag(project, args.id, actor=actor, reason=args.reason)
        if not args.quiet:
            print(f"unflagged {args.id}")
        return 0

    if args.action in ("pass", "fail", "waive"):
        status_map = {"pass": uat_mod.STATUS_PASSED,
                      "fail": uat_mod.STATUS_FAILED,
                      "waive": uat_mod.STATUS_WAIVED}
        block = uat_mod.mark(
            project, args.id, status_map[args.action],
            verified_by=args.verified_by or actor,
            notes=args.notes, failure_reason=args.reason, actor=actor,
        )
        if args.format == "json":
            _print_json({"node": args.id, "uat": block})
        else:
            print(f"{args.id} UAT {status_map[args.action]}" +
                  (f" — {args.reason}" if args.reason else ""))
        return 0

    if args.action == "list":
        status = args.status or "pending"
        rows = uat_mod.list_uat(project, status=status)
        if args.format == "json":
            _print_json({"uat_status_filter": status, "count": len(rows), "items": rows})
        else:
            if not rows:
                print(f"no UAT items with status={status}")
                return 0
            print(f"UAT {status} — {len(rows)} item(s):\n")
            for r in rows:
                print(f"  {r['id']:10} [{r['uat_status']:7}] {r['title']}")
                if r.get("acceptance_criteria"):
                    for c in r["acceptance_criteria"][:6]:
                        print(f"    - {c}")
                if r.get("failure_reason"):
                    print(f"    FAILURE: {r['failure_reason']}")
                if r.get("verified_by"):
                    print(f"    verified by {r['verified_by']} @ {r.get('verified_at', '?')}")
                print(f"    pass:  hopewell uat pass  {r['id']} [--notes \"...\"]")
                print(f"    fail:  hopewell uat fail  {r['id']} --reason \"...\"")
                print(f"    waive: hopewell uat waive {r['id']} --reason \"...\"")
                print()
        return 0

    if args.action == "show":
        rows = [r for r in uat_mod.list_uat(project, status="all") if r["id"] == args.id]
        if not rows:
            print(f"hopewell: {args.id} has no needs-uat component", file=sys.stderr)
            return 1
        _print_json(rows[0])
        return 0

    if args.action == "backfill":
        has_all = [c.strip() for c in (args.has_all or "").split(",") if c.strip()]
        touched = uat_mod.backfill(
            project,
            node_status=args.status, component=args.component,
            has_all=has_all or None, since=args.since,
            dry_run=args.dry_run, actor=actor,
        )
        if args.format == "json":
            _print_json({"dry_run": args.dry_run, "count": len(touched), "touched": touched})
        else:
            print(("would flag" if args.dry_run else "flagged") + f" {len(touched)} node(s):")
            for r in touched:
                print(f"  {r['id']:10} [{r['node_status']}] {r['title']}")
        return 0

    print(f"hopewell: unknown uat action '{args.action}'", file=sys.stderr)
    return 1


def cmd_resume(args) -> int:
    """Show this agent's active work + suggested next action per claim."""
    project = _project(args)
    data = resume_mod.resume(project, name=args.name, include_all=args.all)
    if args.format == "json":
        _print_json(data)
    else:
        sys.stdout.write(resume_mod.render_text(data))
    return 0


def cmd_checkpoint(args) -> int:
    """Record a [next] checkpoint note on a node — `hopewell resume` surfaces it."""
    project = _project(args)
    project_actor = _actor_from_env()
    try:
        resume_mod.checkpoint(project, args.id, args.next, actor=project_actor)
    except FileNotFoundError as exc:
        print(f"hopewell: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"checkpoint recorded on {args.id}: [next] {args.next}")
    return 0


def cmd_evolve(args) -> int:
    """LLM-driven graph evolution ops."""
    project = _project(args)
    actor = _actor_from_env()

    if args.action == "add-node":
        components = [c.strip() for c in (args.components or "").split(",") if c.strip()]
        if not components:
            print("hopewell: evolve add-node requires --components", file=sys.stderr)
            return 1
        nid = evolve_mod.add_node(project, components=components,
                                  title=args.title, owner=args.owner,
                                  parent=args.parent, actor=actor, reason=args.reason)
        _print_json({"op": "add_node", "node": nid})
        return 0

    if args.action == "wire":
        try:
            evolve_mod.wire(project, args.from_id, args.to, args.kind,
                            artifact=args.artifact, reason=args.reason, actor=actor)
        except CircularDependencyError as exc:
            print(f"hopewell: {exc}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"wired {args.from_id} --[{args.kind}]--> {args.to}")
        return 0

    if args.action == "unwire":
        evolve_mod.unwire(project, args.from_id, args.to, args.kind,
                          actor=actor, reason=args.reason)
        if not args.quiet:
            print(f"unwired {args.from_id} -[{args.kind}]- {args.to}")
        return 0

    if args.action == "add-loop":
        over = [x.strip() for x in (args.over or "").split(",") if x.strip()]
        if not args.name or not over or not args.until:
            print("hopewell: evolve add-loop requires --name, --over, --until", file=sys.stderr)
            return 1
        nid = evolve_mod.add_loop(project, args.name, over, args.until,
                                  max_iterations=args.max_iterations, actor=actor)
        _print_json({"op": "add_loop", "node": nid})
        return 0

    if args.action == "rollback":
        try:
            evolve_mod.rollback(project, args.change_id, actor=actor)
        except (KeyError, ValueError) as exc:
            print(f"hopewell: {exc}", file=sys.stderr)
            return 1
        if not args.quiet:
            print(f"rolled back change {args.change_id}")
        return 0

    if args.action == "list":
        evolutions = evolve_mod.list_evolutions(project)
        if args.limit:
            evolutions = evolutions[: args.limit]
        _print_json({"count": len(evolutions), "evolutions": evolutions})
        return 0

    print(f"hopewell: unknown evolve action '{args.action}'", file=sys.stderr)
    return 1


def cmd_extensions(args) -> int:
    """List project-defined Python processors + YAML components."""
    project = _project(args)
    data = extensions_mod.list_loaded(project)
    if args.action == "list":
        _print_json(data)
        return 0
    if args.action == "check":
        errs = data.get("errors") or []
        if args.format == "json":
            _print_json({"extensions": data, "ok": not errs})
        else:
            if errs:
                print(f"hopewell extensions check: {len(errs)} error(s)")
                for e in errs:
                    print(f"  {e.get('file','?')}: {e.get('kind','?')}: {e.get('error','?')}")
            else:
                counts = (f"processors={data.get('processors_loaded', 0)}, "
                          f"components={data.get('components_loaded', 0)}")
                print(f"hopewell extensions check: clean ({counts})")
        return 0 if not errs else 1
    print(f"hopewell: unknown extensions action '{args.action}'", file=sys.stderr)
    return 1


def cmd_web(args) -> int:
    """Launch the local web UI (requires `hopewell[web]` extras)."""
    try:
        from hopewell.web import server as web_server
    except ImportError as exc:
        print(f"hopewell web: {exc}", file=sys.stderr)
        print("hopewell web: install extras with `pip install 'hopewell[web]'`", file=sys.stderr)
        return 2
    root = _project(args).root
    web_server.run(project_root=str(root), port=args.port,
                   host=args.host, open_browser=args.open_browser)
    return 0


def cmd_migrate(args) -> int:
    """Re-apply every idempotent project-level setup step (merge driver,
    .gitattributes, .claudeignore, CLAUDE.md block) to an existing
    `.hopewell/`. Use after upgrading Hopewell to pick up new setup."""
    from hopewell.project import Project
    start = Path(args.project_root).resolve() if args.project_root else None
    try:
        project = Project.migrate(start)
    except FileNotFoundError as exc:
        print(f"hopewell: {exc}", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"Migrated {project.hw_dir}")
        print(f"  ran: merge-driver install, .gitattributes refresh, CLAUDE.md rule check")
        print(f"  to bring newer Hopewell project-level setup into an existing tree")
    return 0


def cmd_info(args) -> int:
    try:
        project = _project(args)
    except FileNotFoundError as e:
        _print_json({"initialized": False, "error": str(e)})
        return 0
    from hopewell.query import graph
    _print_json({
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "project_root": str(project.root),
        "hw_dir": str(project.hw_dir),
        "config": {
            "name": project.cfg.name,
            "id_prefix": project.cfg.id_prefix,
            "enabled_components": project.cfg.enabled_components,
            "github_repo": project.cfg.github.repo,
        },
        "node_count": len(project.all_nodes()),
    })
    return 0


# ---------------------------------------------------------------------------
# query subcommand tree
# ---------------------------------------------------------------------------


def cmd_query(args) -> int:
    project = _project(args)
    from hopewell import query as q
    if args.subject == "ready":
        data = q.ready(project, owner=args.owner)
    elif args.subject == "deps":
        data = q.deps(project, args.name, transitive=args.transitive)
    elif args.subject == "waves":
        data = q.waves(project)
    elif args.subject == "critical-path":
        data = q.critical_path(project)
    elif args.subject == "component":
        data = q.component_nodes(project, args.name)
    elif args.subject == "metrics":
        data = q.metrics(project, by=args.by)
    elif args.subject == "graph":
        data = q.graph(project)
    elif args.subject == "show":
        data = q.show(project, args.name)
    elif args.subject == "attestations":
        data = {
            "query": "attestations",
            "filters": {"agent": args.owner, "fingerprint": args.fingerprint,
                        "node": args.name, "since": args.since, "kind": args.att_kind,
                        "limit": args.limit},
            "attestations": att_mod.query_attestations(
                project.attestations_path,
                agent=args.owner, fingerprint=args.fingerprint,
                node=args.name, since=args.since, kind=args.att_kind,
                limit=args.limit,
            ),
        }
    elif args.subject == "claims":
        data = q.claims(project, node_id=args.name)
    else:
        print(f"hopewell: unknown query subject '{args.subject}'", file=sys.stderr)
        return 1
    _print_json(data)
    return 0


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------


def cmd_agent(args) -> int:
    project = _project(args)
    reg = project.agent_registry

    if args.action == "register":
        name = args.name
        if not name.startswith("@"):
            name = "@" + name
        doc_path: Optional[str] = None
        fp: Optional[str] = None
        if args.doc:
            p = Path(args.doc)
            if not p.is_absolute():
                p = (project.root / p).resolve()
            fp = att_mod.fingerprint(p)
            try:
                doc_path = str(p.relative_to(project.root)).replace("\\", "/")
            except ValueError:
                doc_path = str(p)
        elif args.fingerprint:
            fp = args.fingerprint
        rec = reg.register(name, doc_path=doc_path, current_fp=fp)
        _print_json(rec.to_dict())
        return 0

    if args.action == "list":
        _print_json({
            "agents": [r.to_dict() for r in reg.all()],
        })
        return 0

    if args.action == "fingerprint":
        name = args.name
        if name and not name.startswith("@"):
            name = "@" + name
        rec = reg.get(name)
        if rec is None:
            print(f"hopewell: no agent registered as {name!r}", file=sys.stderr)
            return 1
        # If a --doc is provided, recompute + register if changed
        if args.doc:
            p = Path(args.doc)
            if not p.is_absolute():
                p = (project.root / p).resolve()
            new_fp = att_mod.fingerprint(p)
            if new_fp != rec.current_fingerprint:
                try:
                    doc_rel = str(p.relative_to(project.root)).replace("\\", "/")
                except ValueError:
                    doc_rel = str(p)
                rec = reg.register(name, doc_path=doc_rel, current_fp=new_fp)
        _print_json(rec.to_dict())
        return 0

    if args.action == "quality":
        name = args.name
        if name and not name.startswith("@"):
            name = "@" + name
        # Build {node_id: Node} for defect-traceback
        nodes_map = {n.id: n for n in project.all_nodes()}
        data = att_mod.quality(project.attestations_path, name, nodes_map, reg)
        _print_json(data)
        return 0

    print(f"hopewell: unknown agent action '{args.action}'", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def cmd_orch(args) -> int:
    project = _project(args)
    if args.action == "plan":
        from hopewell.scheduler import Scheduler
        plan = Scheduler(project).plan(max_parallel=args.max)
        _print_json(plan.to_dict())
        return 0
    if args.action == "run":
        from hopewell.orchestrator import Runner
        result = Runner(project).execute(dry_run=args.dry_run, max_parallel=args.max,
                                         actor=_actor_from_env())
        _print_json({
            "run_id": result.run_id,
            "started": result.started, "finished": result.finished,
            "waves_executed": result.waves_executed,
            "nodes_run": result.nodes_run,
            "nodes_succeeded": result.nodes_succeeded,
            "nodes_failed": result.nodes_failed,
            "nodes_skipped": result.nodes_skipped,
        })
        return 0 if not result.nodes_failed else 1
    if args.action == "status":
        # latest run summary
        runs = project.hw_dir / "orchestrator" / "runs"
        files = sorted(runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            print("no runs yet")
            return 0
        _print_json(json.loads(files[0].read_text(encoding="utf-8")))
        return 0
    print(f"hopewell: unknown orch action '{args.action}'", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------


def cmd_github(args) -> int:
    project = _project(args)
    from hopewell import github as gh_mod
    if args.action == "sync":
        try:
            res = gh_mod.sync_from_github(project, since=args.since, state=args.state,
                                          actor=_actor_from_env())
        except (ValueError, RuntimeError) as e:
            print(f"hopewell: {e}", file=sys.stderr)
            return 2
        _print_json({
            "repo": res.repo,
            "fetched": res.fetched,
            "created": res.created,
            "updated": res.updated,
            "already_matching": res.already_matching,
            "since": res.since,
            "new_since": res.new_since,
        })
        return 0
    if args.action == "pull":
        try:
            node = gh_mod.pull_one(project, args.ref, actor=_actor_from_env())
        except (ValueError, RuntimeError) as e:
            print(f"hopewell: {e}", file=sys.stderr)
            return 2
        print(f"Pulled {node.id} — {node.title}")
        return 0
    if args.action == "config":
        _print_json({
            "repo": project.cfg.github.repo,
            "default_components": project.cfg.github.default_components,
            "label_to_components": project.cfg.github.label_to_components,
            "token_env": project.cfg.github.token_env,
            "token_present": bool(os.environ.get(project.cfg.github.token_env)),
        })
        return 0
    print(f"hopewell: unknown github action '{args.action}'", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# hooks
# ---------------------------------------------------------------------------


def cmd_hooks(args) -> int:
    project = _project(args)
    if args.action == "install":
        path = hooks_mod.install(project.root)
        if not args.quiet:
            print(f"Installed {path}")
        return 0
    if args.action == "uninstall":
        ok = hooks_mod.uninstall(project.root)
        if not args.quiet:
            print("Uninstalled." if ok else "No hopewell hook found.")
        return 0
    print(f"hopewell: unknown hooks action '{args.action}'", file=sys.stderr)
    return 1


# Internal: invoked by the installed hook script.
def cmd_hook_on_commit(args) -> int:
    try:
        project = _project(args)
    except FileNotFoundError:
        return 0  # not a hopewell project; silently no-op
    refs = _extract_refs(args.message, project.cfg.id_prefix)
    closed_refs = _extract_close_refs(args.message, project.cfg.id_prefix)
    actor = _actor_from_env() or "commit-hook"
    for ref in refs:
        if not project.has_node(ref):
            continue
        project.touch(ref, f"[commit] {args.commit[:12]} — {args.message.splitlines()[0][:80]}",
                      actor=actor)
    for ref in closed_refs:
        if not project.has_node(ref):
            continue
        try:
            project.close(ref, commit=args.commit, reason="closed via commit message",
                          actor=actor)
        except Exception:
            pass
    if refs or closed_refs:
        from hopewell.render import views as views_mod
        views_mod.render_all(project)
    return 0


_REF_RE_CACHE = {}


def _extract_refs(msg: str, prefix: str) -> List[str]:
    pat = _REF_RE_CACHE.setdefault(prefix, re.compile(rf"\b({re.escape(prefix)}-\d+)\b"))
    return sorted(set(pat.findall(msg)))


def _extract_close_refs(msg: str, prefix: str) -> List[str]:
    pat = re.compile(rf"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+({re.escape(prefix)}-\d+)",
                     re.IGNORECASE)
    return sorted(set(pat.findall(msg)))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hopewell",
                                description="Hopewell — flow-framework tool.")
    p.add_argument("--version", action="version",
                   version=f"hopewell {__version__} (schema {SCHEMA_VERSION})")
    p.add_argument("--project-root", default=None)

    sub = p.add_subparsers(dest="command", required=True)

    # init
    sp = sub.add_parser("init", help="Initialise .hopewell/ in the project")
    sp.add_argument("--prefix", default="HW")
    sp.add_argument("--name", default=None)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_init)

    # new
    sp = sub.add_parser("new", help="Create a new node")
    sp.add_argument("--components", required=True,
                    help="Comma-separated component list (e.g. work-item,deliverable,user-facing)")
    sp.add_argument("--title", required=True)
    sp.add_argument("--owner", default=None)
    sp.add_argument("--parent", default=None)
    sp.add_argument("--priority", default="P2")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_new)

    # show
    sp = sub.add_parser("show", help="Show a node")
    sp.add_argument("id")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_show)

    # list
    sp = sub.add_parser("list", help="List nodes with filters")
    sp.add_argument("--status", default=None)
    sp.add_argument("--component", default=None)
    sp.add_argument("--has-all", default=None, help="Comma-separated components all must be present")
    sp.add_argument("--owner", default=None)
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_list)

    # ready
    sp = sub.add_parser("ready", help="List nodes whose inputs are all satisfied")
    sp.add_argument("--owner", default=None)
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_ready)

    # touch
    sp = sub.add_parser("touch", help="Append an append-only note to a node")
    sp.add_argument("id")
    sp.add_argument("--note", required=True)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_touch)

    # link
    sp = sub.add_parser("link", help="Create a typed edge: link <from> <kind> <to>")
    sp.add_argument("from_id")
    sp.add_argument("kind", choices=[e.value for e in EdgeKind])
    sp.add_argument("to")
    sp.add_argument("--artifact", default=None)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_link)

    # close
    sp = sub.add_parser("close", help="Close a node (walks through allowed transitions to done)")
    sp.add_argument("id")
    sp.add_argument("--commit", default=None)
    sp.add_argument("--reason", default=None)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_close)

    # check
    sp = sub.add_parser("check", help="Validate the graph (cycles, dangling refs, schema)")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_check)

    # graph
    sp = sub.add_parser("graph", help="Print mermaid graph to stdout")
    sp.set_defaults(func=cmd_graph)

    # render
    sp = sub.add_parser("render", help="Regenerate .hopewell/views/*")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_render)

    # info
    sp = sub.add_parser("info", help="Project + config + state summary (JSON)")
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("migrate", help="Re-apply idempotent setup after a Hopewell upgrade")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_migrate)

    # evolve — LLM-driven graph evolution (v0.6 HW-0014)
    sp = sub.add_parser("evolve", help="Evolve the work graph (add-node, wire, unwire, add-loop, rollback, list)")
    sp.add_argument("action", choices=["add-node", "wire", "unwire", "add-loop", "rollback", "list"])
    # add-node
    sp.add_argument("--components", default=None, help="(add-node) Comma-separated component list")
    sp.add_argument("--title", default=None, help="(add-node) Title string")
    sp.add_argument("--owner", default=None, help="(add-node)")
    sp.add_argument("--parent", default=None, help="(add-node) Parent node id")
    # wire / unwire
    sp.add_argument("--from", dest="from_id", default=None, help="(wire/unwire) Source node id")
    sp.add_argument("--to", default=None, help="(wire/unwire) Target node id")
    sp.add_argument("--kind", default=None,
                    help="(wire/unwire) Edge kind (blocks | produces | consumes | parent | related)")
    sp.add_argument("--artifact", default=None, help="(wire) Artifact path")
    # add-loop
    sp.add_argument("--name", default=None, help="(add-loop)")
    sp.add_argument("--over", default=None, help="(add-loop) Comma-separated node ids in the loop body")
    sp.add_argument("--until", default=None, help="(add-loop) Predicate text")
    sp.add_argument("--max-iterations", dest="max_iterations", type=int, default=10,
                    help="(add-loop) Default 10")
    # rollback
    sp.add_argument("change_id", nargs="?", default=None, help="(rollback) Change id to undo")
    # list
    sp.add_argument("--limit", type=int, default=None, help="(list) Cap entries; newest first")
    # common
    sp.add_argument("--reason", default=None)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_evolve)

    # extensions — custom processors + YAML components (HW-0016)
    sp = sub.add_parser("extensions", help="Inspect project-defined processors + components")
    sp.add_argument("action", choices=["list", "check"])
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_extensions)

    # web — local web UI (HW-0015)
    sp = sub.add_parser("web", help="Launch the local web UI (requires [web] extras)")
    sp.add_argument("--port", type=int, default=7420)
    sp.add_argument("--host", default="127.0.0.1",
                    help="Bind host (default loopback — override only if you know why)")
    sp.add_argument("--open", dest="open_browser", action="store_true",
                    help="Open in the default browser on start")
    sp.set_defaults(func=cmd_web)

    # network — flow-network executors + routes (HW-0027)
    sp = sub.add_parser("network", help="Flow-network: executors + routes (v0.7)")
    nsub = sp.add_subparsers(dest="network_cmd", required=True)

    np = nsub.add_parser("init", help="Scaffold .hopewell/network/")
    np.add_argument("--quiet", action="store_true")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_init(a))

    np = nsub.add_parser("defaults", help="Default-template ops")
    np.add_argument("action", choices=["bootstrap"])
    np.add_argument("--quiet", action="store_true")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_defaults(a))

    np = nsub.add_parser("executor", help="executor add/rm/show/list")
    np.add_argument("action", choices=["add", "rm", "show", "list"])
    np.add_argument("id", nargs="?")
    np.add_argument("--components", default=None,
                    help="(add) comma-separated component list")
    np.add_argument("--component-data", default=None,
                    help="(add) JSON-encoded dict keyed by component name")
    np.add_argument("--parent", default=None,
                    help="(add) parent group executor id")
    np.add_argument("--label", default=None, help="(add) human display label")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_executor(a))

    np = nsub.add_parser("route", help="route add/rm/list")
    np.add_argument("action", choices=["add", "rm", "list"])
    np.add_argument("from_id", nargs="?")
    np.add_argument("to", nargs="?")
    np.add_argument("--condition", default=None)
    np.add_argument("--label", default=None)
    np.add_argument("--required", action="store_true")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_route(a))

    np = nsub.add_parser("show", help="Full flow-network render")
    np.add_argument("--format", choices=["text", "json", "mermaid"], default="text")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_show(a))

    np = nsub.add_parser("validate", help="Run validation rules")
    np.add_argument("--format", choices=["text", "json"], default="text")
    np.set_defaults(func=lambda a: network_cli_mod.cmd_network_validate(a))

    # resume + checkpoint (v0.5.3 session-resume protocol)
    sp = sub.add_parser("resume", help="Show your active work + where you left off on each node")
    sp.add_argument("name", nargs="?", default=None,
                    help="Optional actor name (@ prefix auto-added); defaults to $HOPEWELL_ACTOR")
    sp.add_argument("--all", action="store_true",
                    help="Show every active claim across the project, not just yours")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_resume)

    sp = sub.add_parser("checkpoint",
                        help="Record a [next] note — captures what you were about to do so resume surfaces it")
    sp.add_argument("id")
    sp.add_argument("--next", required=True, help="Brief description of the next step")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_checkpoint)

    # uat — User-Acceptance Testing tracking (v0.5.4)
    sp = sub.add_parser("uat", help="User-acceptance testing: flag/list/pass/fail/waive/backfill")
    sp.add_argument("action",
                    choices=["flag", "unflag", "list", "show", "pass", "fail", "waive", "backfill"])
    sp.add_argument("id", nargs="?", default=None)
    # flag
    sp.add_argument("--criteria", action="append", default=None,
                    help="(flag) Acceptance-criteria bullet; repeat to add multiple")
    # mark
    sp.add_argument("--notes", default=None, help="(pass/fail/waive) Free-form notes from the verifier")
    sp.add_argument("--reason", default=None,
                    help="(fail/waive/unflag) Required rationale")
    sp.add_argument("--verified-by", default=None,
                    help="(pass/fail/waive) Override verifier identity (defaults to $HOPEWELL_ACTOR)")
    # list
    sp.add_argument("--status", default=None,
                    choices=["pending", "passed", "failed", "waived", "all", "any",
                             "idea", "blocked", "ready", "doing", "review", "done",
                             "archived", "cancelled"],
                    help="(list) UAT status filter (default: pending). (backfill) node-status filter.")
    # backfill
    sp.add_argument("--component", default=None,
                    help="(backfill) Only flag nodes carrying this component")
    sp.add_argument("--has-all", default=None,
                    help="(backfill) Comma-separated component list; all must be present")
    sp.add_argument("--since", default=None, help="(backfill) ISO-8601 timestamp")
    sp.add_argument("--dry-run", action="store_true", help="(backfill) Report what would be flagged; don't mutate")
    # common
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_uat)

    # query
    sp = sub.add_parser("query", help="Read-only JSON queries")
    sp.add_argument("subject", choices=["ready", "deps", "waves", "critical-path",
                                        "component", "metrics", "graph", "show",
                                        "attestations", "claims"])
    sp.add_argument("name", nargs="?", default=None)
    sp.add_argument("--owner", default=None)
    sp.add_argument("--transitive", action="store_true")
    sp.add_argument("--by", default="component", choices=["component", "status", "owner"])
    sp.add_argument("--fingerprint", default=None, help="(attestations) filter by agent fingerprint")
    sp.add_argument("--since", default=None, help="(attestations) ISO-8601 timestamp; return entries since")
    sp.add_argument("--att-kind", default=None, help="(attestations) filter by kind")
    sp.add_argument("--limit", type=int, default=None, help="(attestations) cap results")
    sp.set_defaults(func=cmd_query)

    # agent
    sp = sub.add_parser("agent", help="Agent registry + fingerprinting + quality")
    sp.add_argument("action", choices=["register", "list", "fingerprint", "quality"])
    sp.add_argument("name", nargs="?", default=None,
                    help="Agent name (@ prefix auto-added if missing)")
    sp.add_argument("--doc", default=None, help="Path to agent doc file (for fingerprint)")
    sp.add_argument("--fingerprint", default=None, help="Explicit fingerprint hex (12 chars) if --doc isn't handy")
    sp.set_defaults(func=cmd_agent)

    # claim / release / prune-claims (v0.5 coordination)
    sp = sub.add_parser("claim", help="Claim a node by pushing a hopewell/<id> branch")
    sp.add_argument("id")
    sp.add_argument("--slug", default=None, help="Append -<slug> to the branch for readability")
    sp.add_argument("--base", default=None, help="Branch from this base instead of current HEAD")
    sp.add_argument("--offline", action="store_true", help="Write a local claim event without pushing")
    sp.add_argument("--no-push", action="store_true", help="Create the branch locally only")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_claim)

    sp = sub.add_parser("release", help="Release a claim — delete hopewell/<id>[-*] branches")
    sp.add_argument("id")
    sp.add_argument("--keep-remote", action="store_true",
                    help="Delete the local branch only; keep the remote branch in place")
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_release)

    sp = sub.add_parser("prune-claims", help="Delete stale claim branches on origin")
    sp.add_argument("--stale-days", type=int, default=14)
    sp.add_argument("--format", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_prune_claims)

    # merge-driver — invoked by git, not humans.
    sp = sub.add_parser("merge-driver", help=argparse.SUPPRESS)
    sp.add_argument("kind", choices=["jsonl"])
    sp.add_argument("ancestor")
    sp.add_argument("ours")
    sp.add_argument("theirs")
    sp.set_defaults(func=cmd_merge_driver)

    # orch
    sp = sub.add_parser("orch", help="Orchestrator: plan / run / status")
    sp.add_argument("action", choices=["plan", "run", "status"])
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--max", type=int, default=None, help="Max parallel per wave")
    sp.set_defaults(func=cmd_orch)

    # github
    sp = sub.add_parser("github", help="GitHub issues: sync / pull / config")
    sp.add_argument("action", choices=["sync", "pull", "config"])
    sp.add_argument("ref", nargs="?", help="For `pull`: owner/repo#N")
    sp.add_argument("--since", default=None, help="ISO-8601 timestamp")
    sp.add_argument("--state", default="all", choices=["open", "closed", "all"])
    sp.set_defaults(func=cmd_github)

    # hooks
    sp = sub.add_parser("hooks", help="Install/uninstall the git post-commit hook")
    sp.add_argument("action", choices=["install", "uninstall"])
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_hooks)

    # hook-on-commit (internal — invoked by the hook script)
    sp = sub.add_parser("hook-on-commit", help=argparse.SUPPRESS)
    sp.add_argument("--message", required=True)
    sp.add_argument("--commit", required=True)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(func=cmd_hook_on_commit)

    return p


def _force_utf8_stdout() -> None:
    """Force stdout/stderr to UTF-8 so Unicode in node titles, notes, and
    rendered output survives Windows' cp1252 default."""
    for stream_name in ("stdout", "stderr"):
        s = getattr(sys, stream_name, None)
        if s is None:
            continue
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv=None) -> int:
    _force_utf8_stdout()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"hopewell: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nhopewell: interrupted", file=sys.stderr)
        return 130
    except Exception as e:
        # Version-contract errors surface with a clean message (no traceback).
        from hopewell.meta import HopewellVersionError
        if isinstance(e, HopewellVersionError):
            print(f"hopewell: {e}", file=sys.stderr)
            return 3
        raise
