"""Branch-as-claim coordination (v0.5).

A claim is a remote git branch named `hopewell/<NODE-ID>[-<slug>]`. The
push IS the mutex: git's non-fast-forward rejection makes claiming atomic.
First pusher wins; second sees a collision and picks another node.

Releases delete the branch. Merging the branch into main (via PR) releases
the claim by virtue of the branch being gone.

Offline mode: `claim --offline` writes a local claim event without pushing.
Useful when disconnected or for local-only / solo projects.
"""
from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


CLAIM_BRANCH_PREFIX = "hopewell/"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    node_id: str
    branch: str
    claimer: Optional[str] = None
    pushed_at: Optional[str] = None
    last_commit: Optional[str] = None
    last_commit_at: Optional[str] = None
    local: bool = False

    def to_dict(self) -> Dict[str, Any]:
        age_hours = _age_hours(self.last_commit_at or self.pushed_at)
        return {
            "node_id": self.node_id,
            "branch": self.branch,
            "claimer": self.claimer,
            "pushed_at": self.pushed_at,
            "last_commit": self.last_commit,
            "last_commit_at": self.last_commit_at,
            "local": self.local,
            "age_hours": age_hours,
        }


class ClaimCollision(RuntimeError):
    """Raised when a claim push is rejected because the branch already exists."""

    def __init__(self, branch: str, existing: Optional[Claim] = None) -> None:
        super().__init__(
            f"claim collision on branch {branch!r}"
            + (f" — held by {existing.claimer}" if existing and existing.claimer else "")
        )
        self.branch = branch
        self.existing = existing


# ---------------------------------------------------------------------------
# Public API — claim / release / merge / query / prune
# ---------------------------------------------------------------------------


def claim(project, node_id: str, *, slug: Optional[str] = None,
          offline: bool = False, base: Optional[str] = None,
          actor: Optional[str] = None,
          push: bool = True) -> Claim:
    """Acquire a claim on `node_id`. Creates + pushes `hopewell/<node_id>[-<slug>]`.

    Raises:
        FileNotFoundError: the node doesn't exist
        ClaimCollision:    the branch is already taken on the remote
        RuntimeError:      git subprocess failure (e.g., dirty working tree)
    """
    if not project.has_node(node_id):
        raise FileNotFoundError(f"node not found: {node_id}")

    branch = _branch_name(node_id, slug)

    # Best-effort remote collision check before attempting push. Match any
    # hopewell/<node_id>[-*] branch so slugged + non-slugged variants collide.
    if push and not offline:
        prefixed = _list_remote_refs_for_node(project, node_id)
        if prefixed:
            existing_branch, existing_sha = next(iter(prefixed.items()))
            existing = _claim_from_remote(project, existing_branch, sha=existing_sha)
            raise ClaimCollision(existing_branch, existing)

    # Create the branch locally. Base is current HEAD unless overridden.
    if base:
        _run_git(project, "switch", "-c", branch, base)
    else:
        _run_git(project, "switch", "-c", branch)

    # Push to remote (unless offline).
    pushed_ok = False
    if push and not offline:
        try:
            _run_git(project, "push", "-u", "origin", branch, "--atomic")
            pushed_ok = True
        except subprocess.CalledProcessError as exc:
            # Roll back the local branch creation so the user isn't stranded.
            _try_run_git(project, "switch", "-")
            _try_run_git(project, "branch", "-D", branch)
            # Race: someone else pushed between our check and push.
            remote = _remote_branch_sha(project, branch)
            existing = _claim_from_remote(project, branch, sha=remote) if remote else None
            raise ClaimCollision(branch, existing) from exc

    # Emit claim event + attestation.
    from hopewell import events as events_mod
    events_mod.append(project.events_path, "node.claim", node=node_id, actor=actor,
                      data={"branch": branch, "base": base, "local": offline or not pushed_ok,
                            "pushed": pushed_ok})
    project._attest(kind="node.claim", node=node_id, actor=actor,
                    data={"branch": branch, "local": offline or not pushed_ok,
                          "pushed": pushed_ok})

    return Claim(
        node_id=node_id, branch=branch, claimer=actor,
        pushed_at=_now_iso() if pushed_ok else None,
        local=not pushed_ok,
    )


def release(project, node_id: str, *, actor: Optional[str] = None,
            delete_remote: bool = True) -> List[str]:
    """Release every `hopewell/<node_id>[-*]` branch held by this project.

    Returns the list of deleted branch names.
    """
    deleted: List[str] = []
    branches = _find_claim_branches(project, node_id)
    for br in branches:
        if delete_remote:
            # `git push origin :branch` deletes the remote branch.
            _try_run_git(project, "push", "origin", "--delete", br)
        # Local delete (from any branch except this one).
        current = _current_branch(project)
        if current == br:
            # Switch to a base so we can delete.
            base = _default_base(project)
            _try_run_git(project, "switch", base)
        _try_run_git(project, "branch", "-D", br)
        deleted.append(br)

    if deleted:
        from hopewell import events as events_mod
        events_mod.append(project.events_path, "node.release", node=node_id, actor=actor,
                          data={"branches": deleted})
        project._attest(kind="node.release", node=node_id, actor=actor,
                        data={"branches": deleted})

    return deleted


def query_claims(project, node_id: Optional[str] = None) -> List[Claim]:
    """List active claims (remote branches + unreleased local claims)."""
    claims: Dict[str, Claim] = {}

    # Remote: git ls-remote origin 'hopewell/*'
    remote = _list_remote_claim_refs(project)
    for branch, sha in remote.items():
        nid = _node_id_from_branch(branch)
        if not nid:
            continue
        if node_id and nid != node_id:
            continue
        c = _claim_from_remote(project, branch, sha=sha)
        if c:
            claims[branch] = c

    # Local: claim events without a later release event.
    local_claims = _local_claim_events(project)
    for nid, claim_ev in local_claims.items():
        if node_id and nid != node_id:
            continue
        branch = (claim_ev.get("data") or {}).get("branch", "")
        if branch in claims:
            claims[branch].claimer = claims[branch].claimer or claim_ev.get("actor")
            continue
        claims[branch or f"{CLAIM_BRANCH_PREFIX}{nid}(local)"] = Claim(
            node_id=nid,
            branch=branch or f"{CLAIM_BRANCH_PREFIX}{nid}",
            claimer=claim_ev.get("actor"),
            pushed_at=claim_ev.get("ts"),
            local=True,
        )

    return sorted(claims.values(), key=lambda c: (c.node_id, c.branch))


def prune_stale(project, *, stale_days: int = 14,
                actor: Optional[str] = None) -> List[str]:
    """Delete remote claim branches whose last commit is older than `stale_days`.

    Returns the list of pruned branch names.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    pruned: List[str] = []
    for claim in query_claims(project):
        ts_s = claim.last_commit_at or claim.pushed_at
        if not ts_s:
            continue
        try:
            ts = datetime.datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
        except ValueError:
            continue
        age = (now - ts).total_seconds() / 86400.0
        if age < stale_days:
            continue
        # Delete remote branch + emit release
        _try_run_git(project, "push", "origin", "--delete", claim.branch)
        from hopewell import events as events_mod
        events_mod.append(project.events_path, "node.release", node=claim.node_id, actor=actor,
                          data={"branches": [claim.branch], "reason": f"stale >{stale_days}d"})
        project._attest(kind="node.release", node=claim.node_id, actor=actor,
                        data={"branches": [claim.branch], "reason": f"stale >{stale_days}d"})
        pruned.append(claim.branch)
    return pruned


# ---------------------------------------------------------------------------
# Internals — git subprocess + claim parsing
# ---------------------------------------------------------------------------


def _branch_name(node_id: str, slug: Optional[str]) -> str:
    base = f"{CLAIM_BRANCH_PREFIX}{node_id}"
    if slug:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", slug.strip("-")).strip("-").lower()
        if safe:
            return f"{base}-{safe}"
    return base


def _node_id_from_branch(branch: str) -> Optional[str]:
    if not branch.startswith(CLAIM_BRANCH_PREFIX):
        return None
    tail = branch[len(CLAIM_BRANCH_PREFIX):]
    # Node id is the first component up to "-" (slug separator)
    m = re.match(r"^([A-Z][A-Z0-9]*-\d+)(?:-.*)?$", tail)
    return m.group(1) if m else None


def _run_git(project, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(project.root),
        capture_output=True, text=True, check=True, timeout=60,
    )


def _try_run_git(project, *args: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return _run_git(project, *args)
    except subprocess.CalledProcessError:
        return None
    except subprocess.TimeoutExpired:
        return None


def _current_branch(project) -> Optional[str]:
    r = _try_run_git(project, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r else None


def _default_base(project) -> str:
    # Prefer main, else master, else current branch.
    for cand in ("main", "master"):
        r = _try_run_git(project, "show-ref", "--verify", f"refs/heads/{cand}")
        if r and r.returncode == 0:
            return cand
    return _current_branch(project) or "main"


def _remote_branch_sha(project, branch: str) -> Optional[str]:
    """Return the sha of a remote branch or None if it doesn't exist."""
    r = _try_run_git(project, "ls-remote", "--heads", "origin", branch)
    if not r or not r.stdout.strip():
        return None
    line = r.stdout.strip().splitlines()[0]
    sha, _, _ref = line.partition("\t")
    return sha.strip() or None


def _list_remote_claim_refs(project) -> Dict[str, str]:
    """Return {branch: sha} for every hopewell/* branch on origin."""
    r = _try_run_git(project, "ls-remote", "--heads", "origin", f"{CLAIM_BRANCH_PREFIX}*")
    out: Dict[str, str] = {}
    if not r or not r.stdout.strip():
        return out
    for line in r.stdout.strip().splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            out[ref[len("refs/heads/"):]] = sha.strip()
    return out


def _list_remote_refs_for_node(project, node_id: str) -> Dict[str, str]:
    """All remote hopewell branches that carry this node id (any slug)."""
    out: Dict[str, str] = {}
    r = _try_run_git(project, "ls-remote", "--heads", "origin",
                     f"{CLAIM_BRANCH_PREFIX}{node_id}")
    r2 = _try_run_git(project, "ls-remote", "--heads", "origin",
                      f"{CLAIM_BRANCH_PREFIX}{node_id}-*")
    for r_ in (r, r2):
        if not r_ or not r_.stdout.strip():
            continue
        for line in r_.stdout.strip().splitlines():
            sha, _, ref = line.partition("\t")
            if ref.startswith("refs/heads/"):
                out[ref[len("refs/heads/"):]] = sha.strip()
    return out


def _claim_from_remote(project, branch: str, *, sha: Optional[str] = None) -> Claim:
    node_id = _node_id_from_branch(branch) or ""
    claim = Claim(node_id=node_id, branch=branch, local=False, last_commit=sha)
    if sha:
        r = _try_run_git(project, "log", "-1", "--format=%an%x00%aI", sha)
        if r and r.stdout.strip():
            name, _, ts = r.stdout.strip().partition("\x00")
            claim.claimer = f"@{name}" if name and not name.startswith("@") else name or None
            claim.last_commit_at = ts.strip() or None
            claim.pushed_at = claim.pushed_at or claim.last_commit_at
    return claim


def _find_claim_branches(project, node_id: str) -> List[str]:
    """Every branch that matches hopewell/<node_id>[-*] — local + remote."""
    found: set = set()
    for r in [
        _try_run_git(project, "branch", "--list", f"{CLAIM_BRANCH_PREFIX}{node_id}*"),
        _try_run_git(project, "ls-remote", "--heads", "origin", f"{CLAIM_BRANCH_PREFIX}{node_id}*"),
    ]:
        if not r or not r.stdout.strip():
            continue
        for line in r.stdout.strip().splitlines():
            line = line.strip().lstrip("*").strip()
            if "\t" in line:
                # ls-remote format: sha\trefs/heads/branch
                _, _, ref = line.partition("\t")
                if ref.startswith("refs/heads/"):
                    found.add(ref[len("refs/heads/"):])
            elif line:
                found.add(line)
    return sorted(found)


def _local_claim_events(project) -> Dict[str, Dict[str, Any]]:
    """Return {node_id: latest_claim_event} for claims without a later release."""
    from hopewell import events as events_mod
    latest_claim: Dict[str, Dict[str, Any]] = {}
    latest_release_ts: Dict[str, str] = {}
    for ev in events_mod.read_all(project.events_path):
        nid = ev.get("node")
        if not nid:
            continue
        if ev.get("kind") == "node.claim":
            latest_claim[nid] = ev
        elif ev.get("kind") == "node.release":
            latest_release_ts[nid] = ev.get("ts", "")
    active = {}
    for nid, ev in latest_claim.items():
        rel_ts = latest_release_ts.get(nid, "")
        if ev.get("ts", "") > rel_ts:
            active[nid] = ev
    return active


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_hours(ts_s: Optional[str]) -> Optional[float]:
    if not ts_s:
        return None
    try:
        ts = datetime.datetime.fromisoformat(ts_s.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return round((now - ts).total_seconds() / 3600.0, 2)
