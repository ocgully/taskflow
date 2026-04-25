"""Render BACKLOG.md, graph.md, metrics.md — deterministic, no timestamps.

Called by `hopewell render` and after every mutation via the library. All
outputs regenerate from the node files + events; never hand-edited.
"""
from __future__ import annotations

from typing import Any, Dict, List

from taskflow.model import Node, NodeStatus, TERMINAL_STATUSES
from taskflow.project import Project


def render_all(project: Project) -> Dict[str, str]:
    """Regenerate every view. Returns { filename: content }."""
    nodes = project.all_nodes()
    out = {
        "BACKLOG.md": backlog(nodes, project),
        "graph.md": graph(nodes),
        "metrics.md": metrics(nodes),
        "UAT.md": uat_view(project),
    }
    for name, content in out.items():
        (project.views_dir / name).write_text(content, encoding="utf-8")
    return out


def uat_view(project: Project) -> str:
    """Render the UAT status view — grouped by uat_status."""
    from taskflow import uat as uat_mod
    all_items = uat_mod.list_uat(project, status="all")
    by_status: Dict[str, List[Dict[str, Any]]] = {}
    for r in all_items:
        by_status.setdefault(r["uat_status"], []).append(r)

    lines: List[str] = [
        f"# UAT — User-Acceptance Testing",
        "",
        f"_Generated from `.hopewell/nodes/*.md` — edit via `taskflow uat ...`, not this file._",
        "",
        f"Total UAT-flagged nodes: **{len(all_items)}**",
    ]
    counts = {s: len(by_status.get(s, [])) for s in ("pending", "failed", "passed", "waived")}
    lines.append(f"- pending: {counts['pending']}  |  failed: {counts['failed']}  "
                 f"|  passed: {counts['passed']}  |  waived: {counts['waived']}")
    lines.append("")

    for status_label, header in [("pending", "🟡 Pending UAT"),
                                 ("failed", "🔴 Failed UAT"),
                                 ("passed", "✅ Passed UAT"),
                                 ("waived", "⚪ Waived")]:
        rows = by_status.get(status_label, [])
        if not rows:
            continue
        lines.append(f"## {header} ({len(rows)})")
        lines.append("")
        for r in rows:
            lines.append(f"### {r['id']} — {r['title']}")
            lines.append(f"**Node status**: `{r['node_status']}`   **Owner**: {r['owner'] or '—'}")
            if r.get("acceptance_criteria"):
                lines.append("**Acceptance criteria**:")
                for c in r["acceptance_criteria"]:
                    lines.append(f"- [ ] {c}")
            if r.get("verified_by"):
                lines.append(f"**Verified by**: {r['verified_by']} @ {r.get('verified_at', '?')}")
            if r.get("notes"):
                lines.append(f"**Notes**: {r['notes']}")
            if r.get("failure_reason"):
                lines.append(f"**Failure reason**: {r['failure_reason']}")
            if status_label == "pending":
                lines.append("")
                lines.append("```bash")
                lines.append(f"taskflow uat pass  {r['id']} --notes \"...\"")
                lines.append(f"taskflow uat fail  {r['id']} --reason \"...\"")
                lines.append(f"taskflow uat waive {r['id']} --reason \"...\"")
                lines.append("```")
            lines.append("")

    if not all_items:
        lines.append("_No UAT-flagged nodes yet._  "
                     "Flag a node with `taskflow uat flag <id>` or backfill "
                     "with `taskflow uat backfill --status done`.")
        lines.append("")
    return "\n".join(lines) + "\n"


def backlog(nodes: List[Node], project: Project) -> str:
    lines = [
        f"# {project.cfg.name} — Backlog",
        "",
        f"_Project: `{project.cfg.name}`. This view regenerates on every `hopewell render`; edit node files, not this file._",
        "",
    ]

    by_status: Dict[str, List[Node]] = {}
    for n in nodes:
        by_status.setdefault(_status_str(n.status), []).append(n)

    # Ready queue first (actionable)
    for status in ["doing", "review", "ready", "blocked", "idea", "done", "archived", "cancelled"]:
        group = by_status.get(status, [])
        if not group:
            continue
        lines.append(f"## {status} ({len(group)})")
        lines.append("")
        lines.append("| ID | Title | Components | Owner | Blocked by |")
        lines.append("|----|-------|------------|-------|------------|")
        for n in sorted(group, key=lambda x: (x.priority, x.id)):
            comps = ", ".join(f"`{c}`" for c in n.components[:5])
            more = f" +{len(n.components) - 5}" if len(n.components) > 5 else ""
            blocked = ", ".join(f"`{b}`" for b in n.blocked_by) or "—"
            owner = n.owner or "—"
            title = n.title.replace("|", "\\|")
            lines.append(f"| `{n.id}` | {title} | {comps}{more} | {owner} | {blocked} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def graph(nodes: List[Node]) -> str:
    lines = [
        "# Graph",
        "",
        "_Mermaid. Renders in GitHub / VS Code / Obsidian._",
        "",
        "```mermaid",
        "graph LR",
    ]
    # Node declarations with status-derived style
    for n in sorted(nodes, key=lambda x: x.id):
        label = f"{n.id}<br/>{_shorten(n.title, 30)}"
        lines.append(f'  {_safe(n.id)}["{label}"]')

    # Edges
    edges_seen = set()
    for n in sorted(nodes, key=lambda x: x.id):
        for b in sorted(n.blocks):
            key = ("blocks", n.id, b)
            if key not in edges_seen and any(x.id == b for x in nodes):
                lines.append(f"  {_safe(n.id)} --> {_safe(b)}")
                edges_seen.add(key)
        for i in n.inputs:
            if i.from_node and any(x.id == i.from_node for x in nodes):
                key = ("consumes", i.from_node, n.id)
                if key not in edges_seen:
                    lines.append(f"  {_safe(i.from_node)} -.-> {_safe(n.id)}")
                    edges_seen.add(key)

    # Classes by status
    status_class = {
        "done": "fill:#98FB98,stroke:#2e7d32",
        "doing": "fill:#FFD54F,stroke:#f57f17",
        "ready": "fill:#90CAF9,stroke:#1565c0",
        "blocked": "fill:#EF9A9A,stroke:#c62828",
        "archived": "fill:#E0E0E0,stroke:#616161",
        "cancelled": "fill:#E0E0E0,stroke:#616161",
        "idea": "fill:#CFD8DC,stroke:#455a64",
        "review": "fill:#CE93D8,stroke:#6a1b9a",
    }
    for n in sorted(nodes, key=lambda x: x.id):
        s = _status_str(n.status)
        if s in status_class:
            lines.append(f"  style {_safe(n.id)} {status_class[s]}")

    lines.append("```")
    lines.append("")
    return "\n".join(lines) + "\n"


def metrics(nodes: List[Node]) -> str:
    lines = [
        "# Metrics",
        "",
        f"**Total nodes**: {len(nodes)}",
        "",
    ]

    # By status
    by_status: Dict[str, int] = {}
    for n in nodes:
        by_status[_status_str(n.status)] = by_status.get(_status_str(n.status), 0) + 1
    lines.append("## By status")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|--------|-------|")
    for status in ["idea", "blocked", "ready", "doing", "review", "done", "archived", "cancelled"]:
        if status in by_status:
            lines.append(f"| {status} | {by_status[status]} |")
    lines.append("")

    # By component
    by_comp: Dict[str, int] = {}
    for n in nodes:
        for c in n.components:
            by_comp[c] = by_comp.get(c, 0) + 1
    lines.append("## By component")
    lines.append("")
    lines.append("| Component | Nodes |")
    lines.append("|-----------|-------|")
    for comp in sorted(by_comp, key=lambda x: (-by_comp[x], x)):
        lines.append(f"| `{comp}` | {by_comp[comp]} |")
    lines.append("")

    # By owner
    by_owner: Dict[str, int] = {}
    for n in nodes:
        by_owner[n.owner or "(unassigned)"] = by_owner.get(n.owner or "(unassigned)", 0) + 1
    lines.append("## By owner")
    lines.append("")
    lines.append("| Owner | Nodes |")
    lines.append("|-------|-------|")
    for owner in sorted(by_owner, key=lambda x: (-by_owner[x], x)):
        lines.append(f"| {owner} | {by_owner[owner]} |")
    lines.append("")

    # WIP / cycle counts
    wip = sum(1 for n in nodes
              if (_status_str(n.status) in ("doing", "review")))
    done = sum(1 for n in nodes if _status_str(n.status) == "done")
    lines += [
        "## Flow",
        "",
        f"- **Work in progress** (doing + review): {wip}",
        f"- **Done**: {done}",
        f"- **Blocked**: {by_status.get('blocked', 0)}",
        "",
    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _status_str(s) -> str:
    return s.value if isinstance(s, NodeStatus) else s


def _safe(s: str) -> str:
    return s.replace("-", "_").replace(".", "_")


def _shorten(s: str, n: int) -> str:
    if len(s) <= n:
        return s.replace('"', "'")
    return (s[: n - 1].replace('"', "'")) + "…"
