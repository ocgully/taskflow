"""Core orchestration for `hopewell backfill`.

Turns the output of per-source spiders into Node creations, deduped
against what's already in `.hopewell/nodes/` and against prior backfill
runs.

Idempotency is anchored in `.hopewell/backfill-sources.jsonl`: every
successful ingestion appends a record keyed by `(source_kind, source_id)`.
A second run sees those keys and skips.

Design notes:
- Stdlib only; per-source modules do the scanning.
- We never mutate existing nodes during backfill. If a candidate hashes
  into an already-present node, it's "skipped" and logged.
- GitHub-issue numbers reuse the project's id_prefix: `#42` -> `HW-0042`,
  tagged with `component_data.github_issue = {number: 42}`.
- Explicit `HW-NNNN` references in commits REUSE that id directly; if
  the id is already taken by an unrelated node the backfiller won't
  collide (we fall through to auto-allocate with a reason note).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from hopewell import backfill_git, backfill_issues, backfill_speckit, backfill_todo
from hopewell.model import NodeStatus, format_node_id, parse_node_id


# Default "ages back" window for git + issue spiders.
DEFAULT_SINCE_DAYS = 180


# ---------------------------------------------------------------------------
# Candidate: the intermediate representation every source produces.
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One thing the spider found that might become a Node."""
    source_kind: str                 # "git" | "issue" | "todo" | "spec"
    source_id: str                   # stable-across-runs id for idempotency
    title: str
    body: str = ""
    status: str = "idea"             # NodeStatus value
    components: List[str] = field(default_factory=list)
    owner: Optional[str] = None
    # Optional preferred node id (used by git HW-NNNN spider + GitHub issues).
    preferred_id: Optional[str] = None
    component_data: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    created: Optional[str] = None    # ISO-8601 UTC (will default to now)


# ---------------------------------------------------------------------------
# Heuristics: infer components from a title.
# ---------------------------------------------------------------------------


def infer_components(title: str, *, base: Optional[List[str]] = None) -> List[str]:
    t = (title or "").lower()
    tags: List[str] = list(base) if base else []
    # Defect-ish
    if re.search(r"\b(fix|bug|error|broken|regression|crash)\b", t):
        tags = _ensure(tags, "defect")
    # Debt / refactor
    if re.search(r"\b(refactor|cleanup|tidy|simplif|rename)\b", t):
        tags = _ensure(tags, "debt")
    # Documentation
    if re.search(r"\b(doc|docs|readme|changelog|tutorial)\b", t):
        tags = _ensure(tags, "documentation")
    # Test
    if re.search(r"\btest(s|ing)?\b", t):
        tags = _ensure(tags, "test")
    # New feature
    if re.search(r"\b(feat|feature|add|implement|introduce|build)\b", t):
        tags = _ensure(tags, "work-item")
        tags = _ensure(tags, "deliverable")
    if not tags:
        tags = ["work-item"]
    return tags


def _ensure(xs: List[str], v: str) -> List[str]:
    return xs if v in xs else xs + [v]


# ---------------------------------------------------------------------------
# Idempotency ledger
# ---------------------------------------------------------------------------


def _ledger_path(hw_dir: Path) -> Path:
    return hw_dir / "backfill-sources.jsonl"


def read_ledger(hw_dir: Path) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Return {(source_kind, source_id): record} from the ledger."""
    p = _ledger_path(hw_dir)
    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if not p.is_file():
        return out
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = (rec.get("source_kind", ""), rec.get("source_id", ""))
                if k[0] and k[1]:
                    out[k] = rec
    except OSError:
        pass
    return out


def append_ledger(hw_dir: Path, rec: Dict[str, Any]) -> None:
    p = _ledger_path(hw_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Spider → Candidate adapters
# ---------------------------------------------------------------------------


def candidates_from_git(
    root: Path, *, prefix: str, since_iso: Optional[str], limit: Optional[int],
) -> List[Candidate]:
    refs = backfill_git.spider(root, prefix=prefix, since_iso=since_iso, limit=limit)
    groups = backfill_git.aggregate(refs)
    out: List[Candidate] = []
    for ref, occurrences in groups.items():
        first = occurrences[0]
        closing = any(o.is_closing for o in occurrences)
        status = "done" if closing else "idea"
        components = infer_components(first.subject)
        if first.kind == "hw-id":
            preferred = ref
            source_id = f"git:hw:{ref}"
            component_data: Dict[str, Dict[str, Any]] = {}
        else:
            # issue-number: map #N -> <prefix>-NNNN, tag it.
            try:
                n = int(ref)
            except ValueError:
                continue
            preferred = format_node_id(prefix, n, pad=4)
            source_id = f"git:issue:{n}"
            component_data = {"github_issue": {"number": n}}
        notes: List[str] = []
        for o in occurrences:
            tag = "closing" if o.is_closing else "mention"
            notes.append(
                f"{o.commit_ts} [git:{o.commit_sha[:8]}]  {tag}: {o.subject}"
            )
        out.append(Candidate(
            source_kind="git",
            source_id=source_id,
            title=first.subject or ref,
            body=first.body,
            status=status,
            components=components,
            preferred_id=preferred,
            component_data=component_data,
            notes=notes,
            created=first.commit_ts,
        ))
    return out


def candidates_from_issues(
    issues: List[backfill_issues.Issue], *, prefix: str,
) -> List[Candidate]:
    out: List[Candidate] = []
    for iss in issues:
        status = "done" if iss.state == "closed" else "idea"
        components = infer_components(iss.title)
        preferred = format_node_id(prefix, iss.number, pad=4)
        out.append(Candidate(
            source_kind="issue",
            source_id=f"issue:{iss.number}",
            title=iss.title,
            body=iss.body,
            status=status,
            components=components,
            preferred_id=preferred,
            component_data={"github_issue": {
                "number": iss.number, "url": iss.url, "state": iss.state,
            }},
            notes=list(iss.comments),
            created=iss.created_at or None,
        ))
    return out


def candidates_from_todos(items: List[backfill_todo.TodoItem]) -> List[Candidate]:
    out: List[Candidate] = []
    for it in items:
        status = "done" if it.checked else "idea"
        components = infer_components(it.title)
        src_hash = hashlib.sha1(
            f"{it.source_path}|{it.section}|{it.title}".encode("utf-8")
        ).hexdigest()[:10]
        body = f"section: {it.section}" if it.section else ""
        out.append(Candidate(
            source_kind="todo",
            source_id=f"todo:{src_hash}",
            title=it.title,
            body=body,
            status=status,
            components=components,
            notes=[f"{Path(it.source_path).name}:{it.line_no}"],
        ))
    return out


def candidates_from_specs(items: List[backfill_speckit.SpecItem]) -> List[Candidate]:
    out: List[Candidate] = []
    for it in items:
        # Per prompt: git evidence is required to move beyond idea.
        if it.has_git_evidence and it.has_spec and it.has_plan and it.has_tasks:
            status = "done"
        else:
            status = "idea"
        components = infer_components(it.title, base=["work-item", "deliverable"])
        # SpecKit slug is the stable id.
        out.append(Candidate(
            source_kind="spec",
            source_id=f"spec:{it.slug}",
            title=it.title,
            body=it.body,
            status=status,
            components=components,
            component_data={"speckit": {
                "slug": it.slug,
                "number": it.number,
                "phase": it.phase,
                "path": it.path,
                "has_spec": it.has_spec,
                "has_plan": it.has_plan,
                "has_tasks": it.has_tasks,
                "has_git_evidence": it.has_git_evidence,
            }},
            notes=[f"spec: {it.path}"],
        ))
    return out


# ---------------------------------------------------------------------------
# Materialisation — write Nodes under .hopewell/nodes/
# ---------------------------------------------------------------------------


@dataclass
class BackfillReport:
    created: List[str] = field(default_factory=list)
    skipped_existing: List[Tuple[str, str]] = field(default_factory=list)   # (source_id, node_id)
    skipped_ledger: List[str] = field(default_factory=list)                 # source_id
    conflicts: List[str] = field(default_factory=list)                      # human-readable
    by_source: Dict[str, int] = field(default_factory=dict)

    def record_created(self, kind: str, node_id: str) -> None:
        self.created.append(node_id)
        self.by_source[kind] = self.by_source.get(kind, 0) + 1


def _allocate_id(project, preferred: Optional[str]) -> str:
    """Use `preferred` if it parses and isn't taken; otherwise auto-allocate."""
    if preferred:
        try:
            parse_node_id(preferred)
            if not project.has_node(preferred):
                return preferred
        except ValueError:
            pass
    return project.next_node_id()


def _status_from_str(v: str) -> NodeStatus:
    try:
        return NodeStatus(v)
    except ValueError:
        return NodeStatus.idea


def _walk_status_to(project, node_id: str, target: str, *, actor: str) -> None:
    """Advance a freshly-created (idea) node toward `target` via legal
    transitions. Used to land GitHub-closed / spec-done items straight in
    `done` without breaking the state machine."""
    if target in ("idea", "") or target is None:
        return
    seq_map = {
        "blocked":  [NodeStatus.blocked],
        "ready":    [NodeStatus.ready],
        "doing":    [NodeStatus.ready, NodeStatus.doing],
        "review":   [NodeStatus.ready, NodeStatus.doing, NodeStatus.review],
        "done":     [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done],
        "cancelled":[NodeStatus.cancelled],
        "archived": [NodeStatus.archived],
    }
    for s in seq_map.get(target, []):
        try:
            project.set_status(node_id, s, actor=actor, reason="backfill")
        except Exception:
            # state machine refused — bail silently; node stays where it is.
            break


def _content_hash(title: str, kind: str) -> str:
    norm = re.sub(r"\s+", " ", (title or "").strip().lower())
    return hashlib.sha1(f"{kind}|{norm}".encode("utf-8")).hexdigest()[:12]


def _existing_content_hashes(project) -> Set[str]:
    """Compute content-hashes of existing nodes so we can catch duplicates
    even across source_kinds."""
    out: Set[str] = set()
    for n in project.all_nodes():
        for kind in ("git", "issue", "todo", "spec", "_"):
            out.add(_content_hash(n.title, kind))
    return out


def apply_candidates(
    project, candidates: List[Candidate], *,
    dry_run: bool = False, actor: str = "backfill",
) -> BackfillReport:
    rep = BackfillReport()
    ledger = read_ledger(project.hw_dir)
    content_hashes = _existing_content_hashes(project)

    # Detect existing GitHub-issue bindings so we don't re-create them.
    existing_issue_numbers: Set[int] = set()
    existing_spec_slugs: Set[str] = set()
    for n in project.all_nodes():
        cd = n.component_data or {}
        if isinstance(cd, dict):
            gi = cd.get("github_issue")
            if isinstance(gi, dict) and "number" in gi:
                try:
                    existing_issue_numbers.add(int(gi["number"]))
                except (TypeError, ValueError):
                    pass
            sk = cd.get("speckit")
            if isinstance(sk, dict) and "slug" in sk:
                existing_spec_slugs.add(str(sk["slug"]))

    for cand in candidates:
        key = (cand.source_kind, cand.source_id)
        if key in ledger:
            rep.skipped_ledger.append(cand.source_id)
            continue

        # Exact preferred HW-id match → already-have-it
        if cand.preferred_id and project.has_node(cand.preferred_id):
            rep.skipped_existing.append((cand.source_id, cand.preferred_id))
            if not dry_run:
                append_ledger(project.hw_dir, {
                    "source_kind": cand.source_kind,
                    "source_id": cand.source_id,
                    "node_id": cand.preferred_id,
                    "action": "skipped-existing-id",
                    "ts": _now_iso(),
                })
            continue

        # GitHub-issue number already bound
        if cand.source_kind == "issue":
            gi = (cand.component_data or {}).get("github_issue", {})
            n = gi.get("number")
            if isinstance(n, int) and n in existing_issue_numbers:
                rep.skipped_existing.append((cand.source_id, f"issue#{n}"))
                if not dry_run:
                    append_ledger(project.hw_dir, {
                        "source_kind": "issue", "source_id": cand.source_id,
                        "node_id": None, "action": "skipped-existing-issue",
                        "ts": _now_iso(),
                    })
                continue

        # SpecKit slug already bound
        if cand.source_kind == "spec":
            sk = (cand.component_data or {}).get("speckit", {})
            slug = sk.get("slug")
            if isinstance(slug, str) and slug in existing_spec_slugs:
                rep.skipped_existing.append((cand.source_id, f"spec:{slug}"))
                if not dry_run:
                    append_ledger(project.hw_dir, {
                        "source_kind": "spec", "source_id": cand.source_id,
                        "node_id": None, "action": "skipped-existing-spec",
                        "ts": _now_iso(),
                    })
                continue

        # Content-hash duplicate (e.g. TODO that duplicates a spec)
        ch = _content_hash(cand.title, cand.source_kind)
        if ch in content_hashes:
            rep.skipped_existing.append((cand.source_id, "<content-dup>"))
            if not dry_run:
                append_ledger(project.hw_dir, {
                    "source_kind": cand.source_kind,
                    "source_id": cand.source_id,
                    "node_id": None, "action": "skipped-content-dup",
                    "ts": _now_iso(),
                })
            continue

        # MATERIALISE
        if dry_run:
            rep.created.append(cand.preferred_id or "<auto>")
            rep.by_source[cand.source_kind] = rep.by_source.get(cand.source_kind, 0) + 1
            continue

        node_id = _allocate_id(project, cand.preferred_id)
        try:
            node = project.new_node(
                components=list(cand.components or ["work-item"]),
                title=cand.title or node_id,
                owner=cand.owner,
                priority="P2",
                actor=actor,
            )
        except ValueError as e:
            rep.conflicts.append(f"{cand.source_id}: {e}")
            continue

        # If `new_node` auto-allocated but we wanted `preferred_id`, relocate
        # the file on disk (no event-log rewrite needed — the node.create
        # event is keyed on the allocated id; it stays truthful).
        if node.id != node_id and cand.preferred_id and cand.preferred_id == node_id:
            # Rename: write under preferred id, delete old
            old_path = project.node_path(node.id)
            node.id = node_id
            project.save_node(node)
            if old_path.is_file() and old_path != project.node_path(node_id):
                try:
                    old_path.unlink()
                except OSError:
                    pass

        # Attach body + component_data + notes (these aren't first-class
        # kwargs on new_node — we set them post-create and save again).
        if cand.body:
            node.body = cand.body
        if cand.component_data:
            node.component_data.update(cand.component_data)
        for note in cand.notes:
            node.notes.append(note)
        if cand.created:
            node.created = cand.created
        project.save_node(node)

        # Walk to target status (if not idea)
        _walk_status_to(project, node.id, cand.status, actor=actor)

        rep.record_created(cand.source_kind, node.id)
        append_ledger(project.hw_dir, {
            "source_kind": cand.source_kind,
            "source_id": cand.source_id,
            "node_id": node.id,
            "action": "created",
            "ts": _now_iso(),
        })
        # Update tracking sets so downstream candidates in this run also dedupe
        content_hashes.add(ch)
        if cand.source_kind == "issue":
            try:
                existing_issue_numbers.add(int(cand.component_data["github_issue"]["number"]))
            except Exception:
                pass
        if cand.source_kind == "spec":
            slug = cand.component_data.get("speckit", {}).get("slug")
            if isinstance(slug, str):
                existing_spec_slugs.add(slug)

    return rep


# ---------------------------------------------------------------------------
# High-level entry points
# ---------------------------------------------------------------------------


def run(
    project,
    *,
    sources: Iterable[str] = ("git", "todo", "spec"),
    since_iso: Optional[str] = None,
    github_repo: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    actor: str = "backfill",
) -> BackfillReport:
    """Run the requested sources and materialise candidates into nodes.

    `sources` default excludes `issues` — GitHub sync is opt-in. Callers
    that want it must explicitly include "issues" (or "all").
    """
    wanted = set(sources)
    if "all" in wanted:
        wanted = {"git", "issues", "todo", "spec"}

    candidates: List[Candidate] = []
    prefix = project.cfg.id_prefix

    since_iso = since_iso or _default_since_iso()

    if "git" in wanted and backfill_git.has_git_history(project.root):
        candidates.extend(candidates_from_git(
            project.root, prefix=prefix, since_iso=since_iso, limit=limit,
        ))

    if "todo" in wanted:
        candidates.extend(candidates_from_todos(backfill_todo.scan(project.root)))

    if "spec" in wanted:
        specs = backfill_speckit.scan(project.root)
        # Spec evidence needs ALL commits, not just ticket-referencing ones.
        git_subjects: List[str] = []
        if specs and backfill_git.has_git_history(project.root):
            git_subjects = backfill_git.all_commit_subjects(
                project.root, since_iso=since_iso, limit=limit,
            )
        backfill_speckit.correlate_git_evidence(specs, git_subjects)
        candidates.extend(candidates_from_specs(specs))

    if "issues" in wanted:
        issues = backfill_issues.fetch(repo=github_repo, cwd=project.root)
        candidates.extend(candidates_from_issues(issues, prefix=prefix))

    return apply_candidates(project, candidates, dry_run=dry_run, actor=actor)


def project_has_backfillable_sources(root: Path) -> bool:
    """Cheap check used by `hopewell init` to decide whether to auto-fire."""
    if backfill_git.has_git_history(root):
        return True
    if backfill_todo.discover(root):
        return True
    if backfill_speckit.specs_root(root) is not None:
        return True
    return False


def maybe_backfill_on_init(project, *, enabled: bool = True) -> Optional[BackfillReport]:
    """Called by `Project.init`. Fires the default source set (git+todo+spec;
    NEVER issues) when there's something to ingest and the ledger is empty.
    """
    if not enabled:
        return None
    # If there are already nodes, don't auto-fire — a re-init on a
    # populated project shouldn't suddenly duplicate work.
    try:
        existing = list(project.nodes_dir.glob("*.md"))
    except OSError:
        existing = []
    if existing:
        return None
    if not project_has_backfillable_sources(project.root):
        return None
    return run(project, sources=("git", "todo", "spec"))


def format_report(rep: BackfillReport, *, dry_run: bool = False) -> str:
    verb = "would create" if dry_run else "created"
    lines: List[str] = []
    lines.append(f"backfill: {verb} {len(rep.created)} node(s)")
    for kind, n in sorted(rep.by_source.items()):
        lines.append(f"  {kind:8s} {n}")
    if rep.skipped_ledger:
        lines.append(f"  skipped (ledger):    {len(rep.skipped_ledger)}")
    if rep.skipped_existing:
        lines.append(f"  skipped (existing):  {len(rep.skipped_existing)}")
    if rep.conflicts:
        lines.append(f"  conflicts: {len(rep.conflicts)}")
        for c in rep.conflicts[:10]:
            lines.append(f"    - {c}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _default_since_iso() -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=DEFAULT_SINCE_DAYS,
    )
    return dt.strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
