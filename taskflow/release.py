"""Release tooling (HW-0043).

A `release` node is a composition-based record pinning a version,
scope, confidence score, report location, and final outcome. Hopewell
owns the lifecycle; the @release-engineer core agent is the primary
consumer.

Lifecycle:

  1. `start(version, scope=[...])` — creates a release node in
     status=draft. Auto-scopes `done` work items since the previous
     release tag unless explicit scope is passed.
  2. `scope_add / scope_rm` — validates each candidate is in
     status=done AND UAT=passed|waived before accepting.
  3. `score(version)` — computes (but does NOT persist) the 7-signal
     confidence breakdown via `release_confidence.compute`.
  4. `generate_report(version)` — writes / regenerates
     `.hopewell/releases/<version>.md` idempotently from current
     project state.
  5. `finalize(version, ...)` — re-runs score, persists it if
     >= threshold, transitions status=released (optionally shells out
     to `gh release create`). Below threshold, status stays=draft and
     the caller sees what's missing.
  6. `kickback(version, ...)` — creates a `needs-rework` work-item
     node blocking the release, transitions status=kicked-back, emits
     `flow.push` to @orchestrator (or explicit route).

Design rules:

* Stdlib only. `gh` invocations via `subprocess` (best-effort).
* `release_confidence.compute` must tolerate missing subsystems — see
  that module.
* Report schema matches what the @release-engineer agent expects
  (scope list, pipeline timing placeholder, quality signals, bugs
  caught, confidence breakdown with justification).

Report idempotency: `generate_report` overwrites the same file path
every time, produced from the current graph state — no historical
drift to track.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskflow import events as events_mod
from taskflow import uat as uat_mod
from taskflow.model import EdgeKind, NodeStatus


COMPONENT = "release"
REWORK_COMPONENT_TAG = "needs-rework"

STATUS_DRAFT = "draft"
STATUS_HELD = "held"
STATUS_RELEASED = "released"
STATUS_KICKED_BACK = "kicked-back"

VALID_STATUSES = {STATUS_DRAFT, STATUS_HELD, STATUS_RELEASED, STATUS_KICKED_BACK}

# Default report location under the project.
RELEASES_SUBDIR = "releases"


# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------------------
# config load (.hopewell/release-config.yaml)
# ---------------------------------------------------------------------------


DEFAULT_CONFIG: Dict[str, Any] = {
    "threshold": {
        "release": 80,
        "hold_upper": 79,
        "hold_lower": 60,
    },
    "weights": {
        "uat_passed":       20,
        "ci_green":         20,
        "rework_ratio":     15,
        "cycle_time":       10,
        "spec_drift":       10,
        "regressions":      15,
        "test_coverage":    10,
    },
    "rework_tolerance": 0.20,
    "default_branch": "main",
    "github": {
        "repo": None,  # resolved from project.cfg.github.repo when not set
    },
    "coverage_command": None,
    "coverage_baseline_path": None,
    "uat_waiver_statuses": ["passed", "waived"],
}


def config_path(project) -> Path:
    return project.hw_dir / "release-config.yaml"


def load_config(project) -> Dict[str, Any]:
    """Load `.hopewell/release-config.yaml` with fallbacks to defaults.

    Stdlib-only: accepts a stripped-down YAML-ish format identical to
    what `hopewell.storage` emits (simple maps + scalars + lists). If
    PyYAML is present we use it; otherwise we fall back to a thin
    scanner good enough for the config's fixed shape.
    """
    cfg = _deep_copy(DEFAULT_CONFIG)
    path = config_path(project)
    if not path.is_file():
        return cfg
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
        loaded = yaml.safe_load(text) or {}
    except ImportError:
        loaded = _tiny_yaml_load(text)
    _merge(cfg, loaded)
    return cfg


def _deep_copy(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: _deep_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_deep_copy(v) for v in d]
    return d


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> None:
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _tiny_yaml_load(text: str) -> Dict[str, Any]:
    """Last-resort YAML subset loader: supports flat + 2-level maps of
    scalars/lists. Good enough for a config with known shape; we also
    accept `#`-comments and blank lines. Not a general YAML parser."""
    out: Dict[str, Any] = {}
    stack: List[Tuple[int, Dict[str, Any]]] = [(0, out)]
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent < stack[-1][0]:
            stack.pop()
        if not stack:
            stack.append((0, out))
        cur = stack[-1][1]
        body = line.strip()
        if body.startswith("- "):
            # list item under the most recent key
            parent_key = cur.get("__last_key__")
            if parent_key is None:
                continue
            lst = cur.setdefault(parent_key, [])
            if not isinstance(lst, list):
                lst = []
                cur[parent_key] = lst
            lst.append(_scalar(body[2:].strip()))
            continue
        if ":" not in body:
            continue
        key, _, rest = body.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not rest:
            cur[key] = {}
            cur["__last_key__"] = key
            stack.append((indent + 2, cur[key]))
        else:
            cur[key] = _scalar(rest)
            cur["__last_key__"] = key
    # strip bookkeeping keys recursively
    def _clean(d: Any) -> Any:
        if isinstance(d, dict):
            d.pop("__last_key__", None)
            for v in d.values():
                _clean(v)
        return d
    return _clean(out)


def _scalar(s: str) -> Any:
    s = s.strip()
    if s.startswith(("'", '"')) and s.endswith(s[0]) and len(s) >= 2:
        return s[1:-1]
    lower = s.lower()
    if lower in ("null", "none", "~"):
        return None
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


# ---------------------------------------------------------------------------
# release node lookup
# ---------------------------------------------------------------------------


def find_release_node(project, version: str):
    """Return the release Node with `version`, or None."""
    for n in project.all_nodes():
        if COMPONENT not in n.components:
            continue
        block = (n.component_data or {}).get(COMPONENT) or {}
        if block.get("version") == version:
            return n
    return None


def list_releases(project, *, status: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in project.all_nodes():
        if COMPONENT not in n.components:
            continue
        block = (n.component_data or {}).get(COMPONENT) or {}
        rstatus = block.get("status") or STATUS_DRAFT
        if status and status != "all" and rstatus != status:
            continue
        out.append(_release_summary(n, block))
    out.sort(key=lambda r: (r.get("released_at") or r.get("version") or ""))
    return out


def _release_summary(node, block: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": node.id,
        "version": block.get("version"),
        "status": block.get("status") or STATUS_DRAFT,
        "scope_count": len(block.get("scope_nodes") or []),
        "confidence_score": block.get("confidence_score"),
        "report_path": block.get("report_path"),
        "tag": block.get("tag"),
        "released_at": block.get("released_at"),
        "released_by": block.get("released_by"),
        "title": node.title,
    }


# ---------------------------------------------------------------------------
# scope validation
# ---------------------------------------------------------------------------


def _node_status_value(node) -> str:
    s = node.status
    return s.value if hasattr(s, "value") else s


def validate_scope_candidate(project, node_id: str, cfg: Dict[str, Any]) -> List[str]:
    """Return a list of errors (empty = OK) for including `node_id` in scope."""
    errs: List[str] = []
    if not project.has_node(node_id):
        return [f"unknown node: {node_id}"]
    node = project.node(node_id)
    if _node_status_value(node) != NodeStatus.done.value:
        errs.append(
            f"{node_id}: status is {_node_status_value(node)!r} (need 'done' to scope)"
        )
    if uat_mod.COMPONENT in node.components:
        block = (node.component_data or {}).get(uat_mod.COMPONENT) or {}
        ok_statuses = set(cfg.get("uat_waiver_statuses")
                          or ["passed", "waived"])
        if block.get("status") not in ok_statuses:
            errs.append(
                f"{node_id}: UAT status {block.get('status')!r} "
                f"(need one of {sorted(ok_statuses)})"
            )
    return errs


# ---------------------------------------------------------------------------
# auto-scope from previous tag
# ---------------------------------------------------------------------------


def previous_release(project, *, before_version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Most recent released release, optionally before `before_version`."""
    all_r = list_releases(project, status=STATUS_RELEASED)
    if before_version:
        all_r = [r for r in all_r if r.get("version") != before_version]
    all_r.sort(key=lambda r: r.get("released_at") or "", reverse=True)
    return all_r[0] if all_r else None


def _tag_date(tag: str, cwd: Path) -> Optional[str]:
    """Resolve `git` tag -> iso creation date (committer date). Best-effort."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        res = subprocess.run(
            [git, "log", "-1", "--format=%cI", tag],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None


def auto_scope_from_window(project, from_tag: Optional[str]) -> Tuple[List[str], Optional[str]]:
    """Return (node_ids, window_start_iso). All `done` work-items whose
    updated timestamp is >= the from_tag's date. If from_tag can't be
    resolved or is None, returns every done work-item (window_start=None).
    """
    start_iso: Optional[str] = None
    if from_tag:
        start_iso = _tag_date(from_tag, project.root)
    out: List[str] = []
    for n in project.all_nodes():
        if COMPONENT in n.components:
            continue  # skip other release nodes
        if _node_status_value(n) != NodeStatus.done.value:
            continue
        if start_iso:
            upd = n.updated or n.created
            if upd and upd < start_iso:
                continue
        out.append(n.id)
    return out, start_iso


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def start(project, version: str, *,
          scope: Optional[List[str]] = None,
          from_window: Optional[str] = None,
          actor: Optional[str] = None) -> Any:
    """Create a release node in status=draft for `version`.

    If `scope` is provided, it's validated + used verbatim. Otherwise,
    if `from_window` is provided (a git tag name), we auto-scope every
    `done` node updated since that tag's date. Falling back to all
    `done` nodes when from_window is None.
    """
    if find_release_node(project, version) is not None:
        raise ValueError(f"release {version!r} already exists")

    cfg = load_config(project)
    if scope is None:
        scope, _ = auto_scope_from_window(project, from_window)

    # Validate every scope entry. Raise on any error — "start" is the
    # first gate so it's cheap to fail early.
    errs: List[str] = []
    for nid in scope:
        errs.extend(validate_scope_candidate(project, nid, cfg))
    if errs:
        raise ValueError("invalid scope:\n  - " + "\n  - ".join(errs))

    # Create the node
    release_node = project.new_node(
        components=[COMPONENT, "grouping"],
        title=f"Release {version}",
        owner=actor,
        priority="P1",
        actor=actor,
    )
    block: Dict[str, Any] = {
        "version": version,
        "scope_nodes": list(scope),
        "status": STATUS_DRAFT,
        "report_path": _default_report_path(version),
    }
    release_node.component_data[COMPONENT] = block
    project.save_node(release_node)
    events_mod.append(project.events_path, "release.start",
                      node=release_node.id, actor=actor,
                      data={"version": version, "scope_count": len(scope),
                            "from_window": from_window})
    return release_node


def _default_report_path(version: str) -> str:
    safe = version.replace("/", "_").replace("\\", "_")
    return f".hopewell/{RELEASES_SUBDIR}/{safe}.md"


def _require_release(project, version: str):
    node = find_release_node(project, version)
    if node is None:
        raise FileNotFoundError(f"no release node for version {version!r}")
    return node


def scope_add(project, version: str, node_id: str, *,
              actor: Optional[str] = None) -> Dict[str, Any]:
    rel = _require_release(project, version)
    cfg = load_config(project)
    errs = validate_scope_candidate(project, node_id, cfg)
    if errs:
        raise ValueError("; ".join(errs))
    block = rel.component_data.setdefault(COMPONENT, {})
    scope = list(block.get("scope_nodes") or [])
    if node_id not in scope:
        scope.append(node_id)
    block["scope_nodes"] = scope
    project.save_node(rel)
    events_mod.append(project.events_path, "release.scope.add",
                      node=rel.id, actor=actor,
                      data={"version": version, "node": node_id})
    return _release_summary(rel, block)


def scope_rm(project, version: str, node_id: str, *,
             actor: Optional[str] = None) -> Dict[str, Any]:
    rel = _require_release(project, version)
    block = rel.component_data.setdefault(COMPONENT, {})
    scope = [n for n in (block.get("scope_nodes") or []) if n != node_id]
    block["scope_nodes"] = scope
    project.save_node(rel)
    events_mod.append(project.events_path, "release.scope.rm",
                      node=rel.id, actor=actor,
                      data={"version": version, "node": node_id})
    return _release_summary(rel, block)


# ---------------------------------------------------------------------------
# score (thin wrapper)
# ---------------------------------------------------------------------------


def score(project, version: str) -> Dict[str, Any]:
    rel = _require_release(project, version)
    block = (rel.component_data or {}).get(COMPONENT) or {}
    cfg = load_config(project)
    from taskflow import release_confidence as rc
    return rc.compute(project, version=version,
                      scope_nodes=block.get("scope_nodes") or [],
                      config=cfg)


# ---------------------------------------------------------------------------
# finalize
# ---------------------------------------------------------------------------


def finalize(project, version: str, *,
             dry_run: bool = False,
             tag: bool = False,
             gh_release: bool = False,
             actor: Optional[str] = None) -> Dict[str, Any]:
    """Final gate. Re-runs score; if >= threshold, transitions to
    released and (optionally) cuts tag + GitHub release. If under,
    stays in draft and returns the breakdown so the caller can see
    what's missing.
    """
    rel = _require_release(project, version)
    block = rel.component_data.setdefault(COMPONENT, {})
    cfg = load_config(project)
    sc = score(project, version)

    threshold = cfg.get("threshold", {}).get("release", 80)
    total = sc.get("total", 0)
    outcome = "release" if total >= threshold else "below-threshold"
    result: Dict[str, Any] = {
        "version": version,
        "total": total,
        "threshold": threshold,
        "outcome": outcome,
        "score": sc,
        "dry_run": dry_run,
        "tag_created": None,
        "gh_release_created": None,
        "missing": [],
    }

    if outcome == "below-threshold":
        for s in sc.get("signals", []):
            if s.get("score", 0) < s.get("weight", 0):
                result["missing"].append({
                    "name": s.get("name"),
                    "got": s.get("score"),
                    "weight": s.get("weight"),
                    "justification": s.get("justification"),
                })
        return result

    if dry_run:
        return result

    # Persist score + breakdown + flip status to released
    block["confidence_score"] = total
    block["score_breakdown"] = sc.get("signals", [])
    block["status"] = STATUS_RELEASED
    block["released_at"] = _now()
    block["released_by"] = actor or os.environ.get("HOPEWELL_ACTOR") \
        or os.environ.get("GIT_AUTHOR_NAME")

    # Regenerate the report so the finalized snapshot is on disk.
    report_path = generate_report(project, version)
    block["report_path"] = str(
        report_path.relative_to(project.root)
    ).replace("\\", "/")

    if tag:
        tag_name = _git_tag(project, version)
        if tag_name:
            block["tag"] = tag_name
            result["tag_created"] = tag_name

    project.save_node(rel)
    events_mod.append(project.events_path, "release.finalize",
                      node=rel.id, actor=actor,
                      data={"version": version, "score": total,
                            "threshold": threshold})

    # Fan-out on flow network: push to github-main if that executor exists.
    _fanout_to_github_main(project, rel.id, actor=actor)

    if gh_release:
        ghr = _gh_release_create(project, version,
                                 report_path=report_path, cfg=cfg)
        result["gh_release_created"] = ghr

    return result


def _git_tag(project, version: str) -> Optional[str]:
    git = shutil.which("git")
    if not git:
        return None
    try:
        subprocess.run(
            [git, "tag", "-a", version, "-m", f"Release {version}"],
            cwd=str(project.root), check=True,
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return version


def _gh_release_create(project, version: str, *,
                       report_path: Path,
                       cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Shell out to `gh release create`. No new Python deps."""
    gh = shutil.which("gh")
    if gh is None:
        return {"skipped": True, "reason": "gh CLI not on PATH"}
    repo = (cfg.get("github") or {}).get("repo") or \
        getattr(project.cfg.github, "repo", None)
    cmd = [gh, "release", "create", version,
           "--title", f"Release {version}",
           "--notes-file", str(report_path)]
    if repo:
        cmd.extend(["--repo", repo])
    try:
        res = subprocess.run(cmd, cwd=str(project.root),
                             capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        return {"skipped": True, "reason": f"gh invocation failed: {e}"}
    if res.returncode != 0:
        return {"skipped": True, "reason": res.stderr.strip() or "gh failed"}
    return {"created": True, "stdout": res.stdout.strip()}


def _fanout_to_github_main(project, release_node_id: str, *,
                           actor: Optional[str] = None) -> None:
    """Best-effort `flow.push` to `github-main` service executor,
    iff the flow network exists and knows that executor."""
    try:
        from taskflow import network as net_mod
        net = net_mod.load_network(project.root)
    except Exception:
        return
    if "github-main" not in net.executors:
        return
    try:
        project.flow_push(release_node_id, "github-main",
                          from_executor="@release-engineer",
                          reason="release finalized", actor=actor)
    except Exception:
        # Network missing / invalid — not fatal.
        pass


# ---------------------------------------------------------------------------
# kickback
# ---------------------------------------------------------------------------


def kickback(project, version: str, *,
             root_cause: str,
             affected: List[str],
             route_to: str = "@orchestrator",
             actor: Optional[str] = None) -> Dict[str, Any]:
    """Kick a release back. Creates a needs-rework node blocking the
    release, transitions the release to kicked-back, emits flow.push
    to `route_to`.
    """
    rel = _require_release(project, version)
    block = rel.component_data.setdefault(COMPONENT, {})

    rework = project.new_node(
        components=["work-item", "defect"],
        title=f"Rework for release {version}: {root_cause[:60]}",
        owner=route_to,
        priority="P0",
        actor=actor,
    )
    # Tag defect data with the kickback reason + affected nodes.
    # Persist immediately — subsequent `project.touch`/`project.link`
    # calls re-read the node from disk and would otherwise overwrite
    # this in-memory mutation.
    rework.component_data["defect"] = {
        "root_cause": root_cause,
        "affected_versions": [version],
    }
    project.save_node(rework)
    # Cheap free-form note on affected work so humans can follow.
    if affected:
        project.touch(rework.id,
                      f"[kickback] affected: {', '.join(affected)}",
                      actor=actor)

    # rework BLOCKS release (release cannot progress until rework lands).
    # IMPORTANT: `project.link` re-reads + writes the target node, so
    # we must do the link FIRST then re-fetch `rel` before mutating
    # component_data, otherwise we'd clobber the freshly-written
    # blocked_by list.
    project.link(rework.id, EdgeKind.blocks, rel.id,
                 reason=f"release {version} kickback",
                 actor=actor)

    # Also relate the affected nodes for traceability.
    for nid in affected:
        if project.has_node(nid):
            try:
                project.link(rework.id, EdgeKind.related, nid,
                             reason="kickback affected", actor=actor)
            except Exception:
                pass

    # Re-load the release node so we don't clobber the link's writes.
    rel = project.node(rel.id)
    block = rel.component_data.setdefault(COMPONENT, {})

    # Transition release -> kicked-back
    block["status"] = STATUS_KICKED_BACK
    block["kickback"] = {
        "root_cause": root_cause,
        "affected": list(affected),
        "route_to": route_to,
        "rework_node": rework.id,
        "created_at": _now(),
    }
    project.save_node(rel)
    events_mod.append(project.events_path, "release.kickback",
                      node=rel.id, actor=actor,
                      data={"version": version, "rework_node": rework.id,
                            "route_to": route_to, "root_cause": root_cause})

    # flow.push to the route-to executor if it exists on the network.
    pushed = False
    try:
        from taskflow import network as net_mod
        net = net_mod.load_network(project.root)
        if route_to in net.executors:
            project.flow_push(rel.id, route_to,
                              from_executor="@release-engineer",
                              reason=f"kickback: {root_cause}",
                              actor=actor)
            pushed = True
    except Exception:
        pass

    return {
        "version": version,
        "release_node": rel.id,
        "rework_node": rework.id,
        "route_to": route_to,
        "flow_push": pushed,
        "status": STATUS_KICKED_BACK,
    }


# ---------------------------------------------------------------------------
# report generation
# ---------------------------------------------------------------------------


def generate_report(project, version: str, *,
                    path: Optional[Path] = None) -> Path:
    """Regenerate `.hopewell/releases/<version>.md` idempotently."""
    rel = _require_release(project, version)
    block = (rel.component_data or {}).get(COMPONENT) or {}
    cfg = load_config(project)
    if path is None:
        rp = block.get("report_path") or _default_report_path(version)
        path = project.root / rp
    path.parent.mkdir(parents=True, exist_ok=True)

    sc = score(project, version)
    text = _render_report(project, rel, block, sc, cfg)
    path.write_text(text, encoding="utf-8")

    rel.component_data[COMPONENT]["report_path"] = str(
        path.relative_to(project.root)
    ).replace("\\", "/")
    project.save_node(rel)
    return path


def _render_report(project, rel, block: Dict[str, Any],
                   sc: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    version = block.get("version")
    scope_ids = list(block.get("scope_nodes") or [])
    total = sc.get("total", 0)
    threshold = sc.get("threshold", 80)
    outcome = sc.get("outcome", "review")

    today = datetime.date.today().isoformat()
    lines: List[str] = []
    lines.append(f"# Release {version} — {today}")
    lines.append("")
    lines.append(f"_Status: **{block.get('status', STATUS_DRAFT)}**_")
    if block.get("released_at"):
        lines.append(f"_Released: {block['released_at']} by "
                     f"{block.get('released_by') or '?'}_")
    lines.append("")

    # Scope
    lines.append("## Scope")
    lines.append("")
    if not scope_ids:
        lines.append("_(empty scope)_")
    else:
        for nid in scope_ids:
            if not project.has_node(nid):
                lines.append(f"- {nid}: _(missing)_")
                continue
            n = project.node(nid)
            closed = _closed_at(n)
            uat_status = _uat_status_of(n)
            lines.append(
                f"- **{nid}**: {n.title}  "
                f"(closed {closed or '?'}, UAT {uat_status})"
            )
    lines.append("")

    # Pipeline timing (placeholder — may be populated by external runs)
    lines.append("## Pipeline timing")
    lines.append("")
    lines.append("_(not yet measurable — populated from CI when available)_")
    lines.append("")

    # Quality signals — pull each signal verbatim from the score
    lines.append("## Quality signals")
    lines.append("")
    for s in sc.get("signals", []):
        name = s.get("name")
        jv = s.get("justification") or ""
        lines.append(f"- **{name}**: {jv}")
    lines.append("")

    # Bugs / kickbacks
    lines.append("## Bugs caught / kicked back this cycle")
    lines.append("")
    kbs = _find_kickbacks(project, version)
    if kbs:
        for kb in kbs:
            lines.append(f"- {kb}")
    else:
        lines.append("_(none)_")
    lines.append("")

    # Confidence breakdown
    label = {"release": "RELEASE APPROVED",
             "review": "HOLD FOR REVIEW",
             "block": "BLOCK / KICKBACK"}.get(outcome, outcome.upper())
    lines.append(f"## Confidence score: {total} / 100  ->  {label}")
    lines.append("")
    lines.append("**Breakdown:**")
    lines.append("")
    for s in sc.get("signals", []):
        lines.append(
            f"- {s.get('name')} "
            f"({s.get('score')}/{s.get('weight')}): "
            f"{s.get('justification')}"
        )
    lines.append("")
    lines.append(f"_Threshold: {threshold} = release; "
                 "60-79 = review; <60 = block._")
    lines.append("")
    return "\n".join(lines) + "\n"


def _closed_at(node) -> Optional[str]:
    if _node_status_value(node) != NodeStatus.done.value:
        return None
    return node.updated


def _uat_status_of(node) -> str:
    if uat_mod.COMPONENT not in node.components:
        return "not-flagged"
    block = (node.component_data or {}).get(uat_mod.COMPONENT) or {}
    return block.get("status") or "pending"


def _find_kickbacks(project, version: str) -> List[str]:
    """Scan events.jsonl for release.kickback events on this version."""
    out: List[str] = []
    try:
        events = events_mod.read_all(project.events_path)
    except Exception:
        return out
    for ev in events:
        if ev.get("kind") != "release.kickback":
            continue
        data = ev.get("data") or {}
        if data.get("version") != version:
            continue
        out.append(
            f"kickback @ {ev.get('ts', '?')}: "
            f"{data.get('root_cause', '?')} "
            f"-> rework {data.get('rework_node')} "
            f"(route: {data.get('route_to')})"
        )
    return out


# ---------------------------------------------------------------------------
# config scaffolding
# ---------------------------------------------------------------------------


DEFAULT_CONFIG_TEMPLATE = """\
# .hopewell/release-config.yaml  (HW-0043)
# Tuning knobs for the release-engineer agent + taskflow release CLI.
# All fields are optional; Hopewell fills any missing key with a default.

threshold:
  release: 80       # >= release threshold ships via `finalize --gh-release`
  hold_upper: 79    # 60-79 is held for human review
  hold_lower: 60    # below this, release.finalize refuses to cut

weights:
  uat_passed:    20
  ci_green:      20
  rework_ratio:  15
  cycle_time:    10
  spec_drift:    10
  regressions:   15
  test_coverage: 10

rework_tolerance: 0.20   # 20% upper bound for per-executor rework ratio

default_branch: main
github:
  repo: null               # e.g. "owner/name"; null uses project.cfg.github.repo

# Optional project-specific test coverage hook.
# If set, `release_confidence` shells out to `coverage_command`, expects
# a number on stdout, and compares it to `coverage_baseline_path`'s
# stored prior value.
coverage_command: null
coverage_baseline_path: null

uat_waiver_statuses:
  - passed
  - waived
"""


def write_default_config(project) -> Path:
    """Convenience for `taskflow release` onboarding. Idempotent."""
    path = config_path(project)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return path
