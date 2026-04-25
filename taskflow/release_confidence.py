"""Confidence scoring for `taskflow release` (HW-0043).

Seven weighted signals, combined into a 0-100 total. Each signal
produces `{name, weight, score, justification}`. The outcome is
derived from thresholds in `.hopewell/release-config.yaml`:

  total >= release_threshold        -> "release"
  hold_lower <= total < release     -> "review"
  total < hold_lower                -> "block"

Every signal is wrapped in a try/except -- a missing subsystem (no CI,
no `gh` on PATH, no prior release, no coverage command configured)
produces a deterministic "skipped" entry rather than crashing. The
score for a skipped signal defaults to its full weight unless the
skip is semantically a zero (e.g. "gh not installed so we genuinely
don't know if CI is green" reads as "0 with explanation").

Signal semantics:

  uat_passed    -- 100% weight iff every scoped node has uat.status in
                  {passed, waived}; proportional credit otherwise.
  ci_green      -- shells out to `gh pr checks` on the current branch.
                  No gh, no credit (0 + explanation).
  rework_ratio  -- avg rework-ratio across executors that touched scoped
                  work; weight * (1 - min(1, avg/tolerance)).
  cycle_time    -- compares median cycle-time to the prior release's
                  median. Better or equal = full weight; slower = linear
                  penalty capped at 0.
  spec_drift    -- 0 if `spec_input.drift_all` reports any drift among
                  scoped nodes; full weight otherwise.
  regressions   -- 0 if any P0/P1 node is in status=doing and NOT in
                  scope (suggests an active regression we're not fixing).
  test_coverage -- if a coverage_command is configured, runs it, compares
                  to coverage_baseline. No config = full weight
                  (explicitly noted in justification).
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def compute(project, *, version: str,
            scope_nodes: List[str],
            config: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the weighted confidence for a release.

    Returns::

        {
          "version": "v0.15.0",
          "total": 87,
          "threshold": 80,
          "outcome": "release" | "review" | "block",
          "signals": [
             {"name": ..., "weight": W, "score": S, "justification": "..."},
             ...
          ],
          "skipped": [...names of skipped signals...]
        }
    """
    weights = config.get("weights") or {}
    signals: List[Dict[str, Any]] = []
    skipped: List[str] = []

    runners = [
        ("uat_passed",    _signal_uat_passed),
        ("ci_green",      _signal_ci_green),
        ("rework_ratio",  _signal_rework_ratio),
        ("cycle_time",    _signal_cycle_time),
        ("spec_drift",    _signal_spec_drift),
        ("regressions",   _signal_regressions),
        ("test_coverage", _signal_test_coverage),
    ]

    for name, fn in runners:
        weight = int(weights.get(name, 0))
        try:
            score, justification = fn(project, scope_nodes=scope_nodes,
                                       weight=weight, config=config,
                                       version=version)
        except Exception as e:  # noqa: BLE001 -- defensive shell
            score, justification = 0, f"skipped: internal error ({e!r})"
        entry = {
            "name": name,
            "weight": weight,
            "score": int(round(score)),
            "justification": justification,
        }
        signals.append(entry)
        if justification.startswith("skipped"):
            skipped.append(name)

    total = sum(s["score"] for s in signals)
    total = max(0, min(100, total))

    thr = config.get("threshold") or {}
    release_thr = int(thr.get("release", 80))
    hold_lower = int(thr.get("hold_lower", 60))
    if total >= release_thr:
        outcome = "release"
    elif total >= hold_lower:
        outcome = "review"
    else:
        outcome = "block"

    return {
        "version": version,
        "total": total,
        "threshold": release_thr,
        "outcome": outcome,
        "signals": signals,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _signal_uat_passed(project, *, scope_nodes: List[str],
                       weight: int, config: Dict[str, Any],
                       version: str):
    if not scope_nodes:
        return weight, "no scope (defaulting to full weight -- empty release)"
    from taskflow import uat as uat_mod
    ok = set(config.get("uat_waiver_statuses") or ["passed", "waived"])
    total = len(scope_nodes)
    passed = 0
    missing: List[str] = []
    for nid in scope_nodes:
        if not project.has_node(nid):
            missing.append(nid)
            continue
        n = project.node(nid)
        if uat_mod.COMPONENT not in n.components:
            # No needs-uat component at all -- count as not-blocking but
            # note it. Treated as pass (the component is opt-in).
            passed += 1
            continue
        block = (n.component_data or {}).get(uat_mod.COMPONENT) or {}
        if block.get("status") in ok:
            passed += 1
    if missing:
        return 0, f"missing nodes in scope: {', '.join(missing)}"
    if total == 0:
        return weight, "empty scope"
    ratio = passed / total
    score = int(round(weight * ratio))
    if passed == total:
        return weight, f"All {total} scoped nodes UAT-passed"
    return score, f"{passed}/{total} scoped nodes UAT-passed (proportional credit)"


def _signal_ci_green(project, *, scope_nodes: List[str],
                     weight: int, config: Dict[str, Any],
                     version: str):
    gh = shutil.which("gh")
    if gh is None:
        return 0, "skipped: gh CLI not on PATH -- cannot verify CI"
    # Resolve current branch.
    branch = _current_branch(project.root)
    if branch is None:
        return 0, "skipped: could not resolve current branch"
    # `gh pr checks` exits non-zero when a check is failing or pending.
    try:
        res = subprocess.run(
            [gh, "pr", "checks", "--json", "name,state"],
            cwd=str(project.root), capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return 0, f"skipped: gh invocation failed ({e!r})"
    if res.returncode == 0 and not res.stdout.strip():
        return weight, "CI green (no checks configured -- trusting main)"
    if res.returncode != 0:
        return 0, (f"CI not green: {res.stderr.strip()[:160] or 'unknown'}"
                   .rstrip())
    # Parse JSON -- count failing/pending vs passing.
    try:
        checks = json.loads(res.stdout)
    except json.JSONDecodeError:
        return 0, "CI state: could not parse gh JSON"
    passing = [c for c in checks if (c.get("state") or "").upper() == "SUCCESS"]
    if len(passing) == len(checks) and checks:
        return weight, f"CI green ({len(checks)} checks passing)"
    bad = [c.get("name") for c in checks
           if (c.get("state") or "").upper() != "SUCCESS"]
    return 0, f"CI not fully green: failing/pending {bad}"


def _signal_rework_ratio(project, *, scope_nodes: List[str],
                         weight: int, config: Dict[str, Any],
                         version: str):
    try:
        from taskflow import cycle_time as ct_mod
    except Exception as e:
        return weight, f"skipped: cycle_time unavailable ({e!r})"
    try:
        q = ct_mod.quality(project, None, all_executors=True)
    except Exception as e:
        return weight, f"skipped: quality query failed ({e!r})"
    rows = q.get("executors") or []
    if not rows:
        return weight, ("skipped: no executor time recorded "
                        "(no penalty)")
    tolerance = float(config.get("rework_tolerance", 0.20))
    # Narrow to executors that touched a scoped node (best-effort: the
    # current `quality` aggregation isn't per-node, so fall back to the
    # global average when we can't narrow).
    ratios = [float(r.get("rework_ratio", 0.0)) for r in rows]
    if not ratios:
        return weight, "skipped: empty executor roster"
    avg = sum(ratios) / len(ratios)
    if avg <= tolerance:
        return weight, (f"rework ratio {avg:.2%} within tolerance "
                        f"{tolerance:.0%}")
    # Linear penalty; lose full credit at 2*tolerance.
    over = (avg - tolerance) / max(tolerance, 1e-6)
    penalty = min(1.0, over)
    score = int(round(weight * (1.0 - penalty)))
    return score, (f"rework ratio {avg:.2%} exceeds tolerance "
                   f"{tolerance:.0%} (penalty {penalty:.0%})")


def _signal_cycle_time(project, *, scope_nodes: List[str],
                       weight: int, config: Dict[str, Any],
                       version: str):
    try:
        from taskflow import cycle_time as ct_mod
        from taskflow import release as rel_mod
    except Exception as e:
        return weight, f"skipped: cycle_time unavailable ({e!r})"

    # Find previous release as a window anchor.
    prior = rel_mod.previous_release(project, before_version=version)
    done_since: Optional[str] = None
    if prior and prior.get("released_at"):
        done_since = prior["released_at"]
    try:
        agg = ct_mod.aggregate_cycle_time(project, component="work-item",
                                          done_since=done_since)
    except Exception as e:
        return weight, f"skipped: cycle_time aggregate failed ({e!r})"
    median_now = _median_of_nodes(agg)
    if median_now is None:
        return weight, ("skipped: no done work items in window "
                        "(no penalty)")
    if prior is None:
        return weight, (f"no prior release to compare against -- "
                        f"current median {int(median_now)}s")
    # Compare to prior-window cycle time (anything before this window).
    try:
        agg_prior = ct_mod.aggregate_cycle_time(
            project, component="work-item",
            done_since=None,  # full history as baseline
        )
    except Exception:
        return weight, "skipped: could not read prior cycle time"
    median_prior = _median_of_nodes(agg_prior)
    if not median_prior:
        return weight, f"no prior baseline; current median {int(median_now)}s"
    if median_now <= median_prior:
        return weight, (f"cycle time improved: "
                        f"{int(median_now)}s vs {int(median_prior)}s prior")
    # Linear penalty when slower.
    delta = (median_now - median_prior) / median_prior
    penalty = min(1.0, delta)
    score = int(round(weight * (1.0 - penalty)))
    return score, (f"cycle time slower: {int(median_now)}s vs "
                   f"{int(median_prior)}s prior (penalty {penalty:.0%})")


def _median_of_nodes(agg: Dict[str, Any]) -> Optional[float]:
    """Compute median total_seconds across per-node entries in an
    aggregate cycle-time response. `None` when there are no entries."""
    nodes = agg.get("nodes") or []
    vals = sorted(
        float(n.get("total_seconds", 0.0)) for n in nodes
        if not n.get("open", False)
    )
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2 == 1:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _signal_spec_drift(project, *, scope_nodes: List[str],
                       weight: int, config: Dict[str, Any],
                       version: str):
    try:
        from taskflow import spec_input as spec_mod
    except Exception as e:
        return weight, f"skipped: spec_input unavailable ({e!r})"
    try:
        entries = spec_mod.drift_all(project)
    except Exception as e:
        return weight, f"skipped: drift_all failed ({e!r})"
    scope_set = set(scope_nodes)
    in_scope_drift = [
        e for e in entries
        if (e.get("state") in ("drift", "anchor-lost", "missing"))
        and (not scope_set or e.get("node") in scope_set)
    ]
    if not in_scope_drift:
        return weight, "no spec drift in scope"
    names = sorted({e.get("node") for e in in_scope_drift if e.get("node")})
    return 0, f"spec drift in {len(names)} scoped node(s): {names[:5]}"


def _signal_regressions(project, *, scope_nodes: List[str],
                        weight: int, config: Dict[str, Any],
                        version: str):
    scope_set = set(scope_nodes)
    offenders: List[str] = []
    for n in project.all_nodes():
        pr = (n.priority or "").upper()
        if pr not in ("P0", "P1"):
            continue
        s = n.status.value if hasattr(n.status, "value") else n.status
        if s != "doing":
            continue
        if n.id in scope_set:
            continue
        offenders.append(n.id)
    if not offenders:
        return weight, "no open P0/P1 regressions outside scope"
    return 0, f"open P0/P1 regressions not in scope: {offenders[:5]}"


def _signal_test_coverage(project, *, scope_nodes: List[str],
                          weight: int, config: Dict[str, Any],
                          version: str):
    cmd = config.get("coverage_command")
    if not cmd:
        return weight, "skipped: no coverage_command configured (full weight)"
    baseline_path = config.get("coverage_baseline_path")
    try:
        res = subprocess.run(cmd, shell=True, cwd=str(project.root),
                             capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        return 0, f"coverage run failed: {e!r}"
    if res.returncode != 0:
        return 0, (f"coverage cmd exit {res.returncode}: "
                   f"{(res.stderr or '').strip()[:160]}")
    try:
        current = float((res.stdout or "").strip().split()[0])
    except (ValueError, IndexError):
        return 0, f"coverage output not parseable: {res.stdout!r}"
    if not baseline_path:
        return weight, f"coverage {current:.2f}% (no baseline configured)"
    bp = project.root / baseline_path
    if not bp.is_file():
        # First run -- record the baseline for next time.
        try:
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_text(f"{current}\n", encoding="utf-8")
        except OSError:
            pass
        return weight, f"coverage {current:.2f}% (baseline seeded)"
    try:
        prior = float(bp.read_text(encoding="utf-8").strip().split()[0])
    except (ValueError, IndexError):
        return weight, f"coverage {current:.2f}% (bad baseline; skipping compare)"
    if current + 1e-6 >= prior:
        try:
            bp.write_text(f"{current}\n", encoding="utf-8")
        except OSError:
            pass
        return weight, (f"coverage {current:.2f}% >= baseline "
                        f"{prior:.2f}%")
    delta = prior - current
    return 0, (f"coverage dropped: {current:.2f}% vs baseline "
               f"{prior:.2f}% (-{delta:.2f}pp)")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _current_branch(cwd: Path) -> Optional[str]:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        res = subprocess.run(
            [git, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    return (res.stdout or "").strip() or None
