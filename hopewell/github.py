"""GitHub issues ingestion (v0.3).

One-way sync: GitHub issues → Hopewell nodes. No `gh` dependency — direct
REST via stdlib `urllib`. Optional `requests` if installed (faster, cleaner
errors). Incremental by `updated_at`.

Each issue becomes a node with:
  - components: default_components + mapped(label_to_components) + ["github-issue"]
  - title: issue.title
  - status: "ready" (open) | "done" (closed)
  - component_data.github-issue: { repo, number, url, gh_state, labels, author }
  - notes appended with issue URL + last sync ts

Idempotent: re-running finds the node by `component_data.github-issue.url`
and updates in place.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from hopewell import events
from hopewell.model import Node, NodeStatus
from hopewell.project import Project


API_BASE = "https://api.github.com"
STATE_PATH = "orchestrator/github-state.json"


@dataclass
class SyncResult:
    repo: str
    fetched: int
    created: int
    updated: int
    already_matching: int
    since: Optional[str]
    new_since: str


def sync_from_github(project: Project, *, since: Optional[str] = None,
                     state: str = "all", actor: Optional[str] = None) -> SyncResult:
    """Pull issues from the configured repo into Hopewell nodes.

    If `since` is None we read the last-synced timestamp from state file.
    """
    gh_cfg = project.cfg.github
    if not gh_cfg.repo:
        raise ValueError(
            "github.repo not configured in .hopewell/config.toml — set it first."
        )

    state_file = project.hw_dir / STATE_PATH
    state_file.parent.mkdir(parents=True, exist_ok=True)
    prev_state = _load_state(state_file)
    effective_since = since or prev_state.get(gh_cfg.repo, {}).get("last_synced")

    events.append(project.events_path, "github.sync.start", actor=actor,
                  data={"repo": gh_cfg.repo, "since": effective_since, "state": state})

    token = os.environ.get(gh_cfg.token_env)
    issues = list(_iter_issues(gh_cfg.repo, state=state, since=effective_since, token=token))

    # Index existing nodes by GH url for idempotent upsert.
    url_to_node: Dict[str, Node] = {}
    for n in project.all_nodes():
        data = n.component_data.get("github-issue") or {}
        url = data.get("url")
        if url:
            url_to_node[url] = n

    created = 0
    updated = 0
    already = 0

    for issue in issues:
        if issue.get("pull_request"):
            # skip PRs — issues only for v0.3
            continue
        url = issue["html_url"]
        components = _components_for_issue(issue, gh_cfg.label_to_components,
                                           gh_cfg.default_components)
        gh_data = {
            "repo": gh_cfg.repo,
            "number": issue["number"],
            "url": url,
            "gh_state": issue["state"],
            "labels": [l["name"] for l in issue.get("labels", [])],
            "author": (issue.get("user") or {}).get("login"),
        }

        if url in url_to_node:
            node = url_to_node[url]
            changed = _apply_issue_to_node(project, node, issue, components, gh_data,
                                           actor=actor)
            if changed:
                updated += 1
            else:
                already += 1
        else:
            node = project.new_node(
                components=sorted(set(components) | {"github-issue"}),
                title=issue["title"],
                owner=None,
                actor=actor,
            )
            node.component_data["github-issue"] = gh_data
            if issue["state"] == "closed":
                # idea -> ready -> doing -> review -> done
                project.save_node(node)
                _advance_to(project, node.id, NodeStatus.done, actor=actor)
            else:
                project.save_node(node)
                try:
                    project.set_status(node.id, NodeStatus.ready, actor=actor,
                                       reason="github import")
                except ValueError:
                    pass
            project.touch(node.id, f"[github] imported {url}", actor=actor)
            created += 1

    new_since = _latest_updated_at(issues, fallback=effective_since)
    prev_state.setdefault(gh_cfg.repo, {})["last_synced"] = new_since
    _save_state(state_file, prev_state)

    events.append(project.events_path, "github.sync.finish", actor=actor,
                  data={"repo": gh_cfg.repo, "created": created, "updated": updated,
                        "already_matching": already, "fetched": len(issues),
                        "new_since": new_since})

    return SyncResult(
        repo=gh_cfg.repo, fetched=len(issues), created=created, updated=updated,
        already_matching=already, since=effective_since, new_since=new_since or "",
    )


def pull_one(project: Project, issue_ref: str, *, actor: Optional[str] = None) -> Node:
    """Pull a single issue — `owner/repo#N` — into a node."""
    if "#" not in issue_ref:
        raise ValueError("issue-ref must look like `owner/repo#N`")
    repo_part, num_part = issue_ref.split("#", 1)
    number = int(num_part)
    token = os.environ.get(project.cfg.github.token_env)
    issue = _get_issue(repo_part, number, token=token)

    url = issue["html_url"]
    url_to_node = {
        (n.component_data.get("github-issue") or {}).get("url"): n
        for n in project.all_nodes()
    }

    components = _components_for_issue(issue, project.cfg.github.label_to_components,
                                       project.cfg.github.default_components)
    gh_data = {
        "repo": repo_part,
        "number": number,
        "url": url,
        "gh_state": issue["state"],
        "labels": [l["name"] for l in issue.get("labels", [])],
        "author": (issue.get("user") or {}).get("login"),
    }

    if url in url_to_node and url_to_node[url] is not None:
        node = url_to_node[url]
        _apply_issue_to_node(project, node, issue, components, gh_data, actor=actor)
    else:
        node = project.new_node(
            components=sorted(set(components) | {"github-issue"}),
            title=issue["title"], actor=actor,
        )
        node.component_data["github-issue"] = gh_data
        project.save_node(node)
        if issue["state"] == "closed":
            _advance_to(project, node.id, NodeStatus.done, actor=actor)
        else:
            try:
                project.set_status(node.id, NodeStatus.ready, actor=actor,
                                   reason="github import (single)")
            except ValueError:
                pass
    return node


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _apply_issue_to_node(project: Project, node: Node, issue: Dict[str, Any],
                         components: List[str], gh_data: Dict[str, Any],
                         actor: Optional[str]) -> bool:
    changed = False

    # Title drift
    if node.title != issue["title"]:
        node.title = issue["title"]
        changed = True

    # Component-set drift — union with existing + github-issue
    target_components = sorted(set(components) | set(node.components) | {"github-issue"})
    if set(target_components) != set(node.components):
        node.components = target_components
        changed = True

    # gh data refresh
    if node.component_data.get("github-issue") != gh_data:
        node.component_data["github-issue"] = gh_data
        changed = True

    if changed:
        project.save_node(node)

    # Status drift → close/reopen
    cur = node.status if isinstance(node.status, NodeStatus) else NodeStatus(node.status)
    if issue["state"] == "closed" and cur != NodeStatus.done:
        _advance_to(project, node.id, NodeStatus.done, actor=actor)
        changed = True
    elif issue["state"] == "open" and cur == NodeStatus.done:
        # Re-open: move to doing
        try:
            project.set_status(node.id, NodeStatus.doing, actor=actor,
                               reason="github reopened upstream")
            changed = True
        except ValueError:
            pass

    if changed:
        project.touch(node.id, f"[github] sync update from {issue['html_url']}", actor=actor)
    return changed


def _advance_to(project: Project, node_id: str, target: NodeStatus,
                actor: Optional[str]) -> None:
    # Walk through allowed transitions to reach `target`.
    sequence_to_done = [NodeStatus.ready, NodeStatus.doing, NodeStatus.review, NodeStatus.done]
    if target == NodeStatus.done:
        for s in sequence_to_done:
            try:
                project.set_status(node_id, s, actor=actor, reason="github state=closed")
            except ValueError:
                continue


def _components_for_issue(issue: Dict[str, Any], label_map: Dict[str, str],
                          defaults: List[str]) -> List[str]:
    out: Set[str] = set(defaults)
    for lab in issue.get("labels", []):
        name = lab["name"]
        if name in label_map:
            out.add(label_map[name])
    return sorted(out)




def _iter_issues(repo: str, *, state: str = "all", since: Optional[str] = None,
                 token: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    """Iterate all issues matching the filters, handling pagination."""
    per_page = 100
    url = f"{API_BASE}/repos/{repo}/issues"
    params = {"state": state, "per_page": str(per_page)}
    if since:
        params["since"] = since
    while url:
        full = url + ("?" + urllib.parse.urlencode(params) if params and "?" not in url else "")
        data, next_url = _http_json(full, token=token, include_next=True)
        for item in data:
            yield item
        url = next_url
        params = None  # next URL already encodes them


def _get_issue(repo: str, number: int, *, token: Optional[str] = None) -> Dict[str, Any]:
    url = f"{API_BASE}/repos/{repo}/issues/{number}"
    data, _ = _http_json(url, token=token, include_next=False)
    return data


def _http_json(url: str, *, token: Optional[str], include_next: bool):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "hopewell/0.5 (+https://github.com/ocgully/Hopewell)",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            next_url = None
            if include_next:
                link = resp.headers.get("Link", "")
                next_url = _parse_next_link(link)
            return json.loads(body), next_url
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"GitHub API {e.code} {e.reason}: {detail}") from None


def _parse_next_link(link_header: str) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if section.endswith('rel="next"'):
            start = section.find("<") + 1
            end = section.find(">")
            if start > 0 and end > start:
                return section[start:end]
    return None


def _latest_updated_at(issues: List[Dict[str, Any]], fallback: Optional[str]) -> Optional[str]:
    ts = fallback
    for i in issues:
        if i.get("updated_at") and (ts is None or i["updated_at"] > ts):
            ts = i["updated_at"]
    return ts


def _load_state(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_state(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
