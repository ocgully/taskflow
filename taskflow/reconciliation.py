"""Reconciliation flow (HW-0034) — downstream-review nodes for spec drift.

Two triggers, one node type. When a spec file changes in a way that
drifts a slice referenced via the `spec-input` component (HW-0031),
downstream consumers may need to re-evaluate their work. The
``downstream-review`` node makes that re-evaluation a first-class graph
object: it BLOCKS the consumer until a human (or agent) records one of
four explicit outcomes.

Trigger A — manual / batched (spec-edit side)
---------------------------------------------
After a user edits a spec, they invoke
``taskflow reconcile queue <spec_path> [--heading|--lines]`` (or click
the equivalent button in the spec viewer). For each consumer node whose
recorded slice covers the edited range AND whose slice has actually
drifted, we create one ``downstream-review`` node — UNLESS an OPEN review
already covers that exact (consumer, spec_path, slice-key) tuple, in
which case we do nothing (idempotent).

Trigger B — automatic / pickup gate (work-side)
-----------------------------------------------
``flow.enter`` calls
``check_drift_gate(project, node_id, executor_id)`` before adding a
location. The gate fires ONLY when the executor has the ``agent``
component (humans / services / gates / queues / targets are exempt —
fast path); when fired, the gate runs ``spec_input.drift`` over the
consumer node's references and:

  * If no drift: pass.
  * If drift AND an open review already covers the drifted slice:
    block (raise ``ReconciliationRequired`` pointing at the existing
    review).
  * If drift AND no open review: create one (Trigger B), then block
    (raise ``ReconciliationRequired`` pointing at the new review).

The bypass env var ``HOPEWELL_SKIP_RECONCILIATION=1`` disables Trigger
B (useful for scripts and CI smoke runs).

Idempotency
-----------
Trigger B detects "already-queued" reviews via a query over OPEN
``downstream-review`` nodes whose ``component_data["downstream-review"]``
matches the same ``consumer_node`` + ``spec_path`` + slice-key. The
slice-key is ``anchor`` if present else ``tuple(lines)`` — same logic
the spec_input ``_slice_match`` helper uses internally.

Resolution outcomes
-------------------
``resolve_review(project, review_id, outcome=...)`` records the
decision and unblocks the consumer:

* ``no-impact`` — slice changed but semantics unchanged. Re-pin the
  consumer's recorded ``slice_sha`` to the current value (calls
  ``spec_input.add_spec_ref`` which is idempotent for the same slice
  selector). Close the review.
* ``update-in-scope`` — consumer absorbs the new spec into its scope.
  No new node. Close the review.
* ``update-out-of-scope`` — spawn a follow-up work item that BLOCKS
  the consumer (the consumer proceeds on the OLD spec until the
  follow-up lands). Close the review. ``followup_title`` REQUIRED.
* ``spec-revert`` — record the decision; the actual revert is a
  manual git operation. Close the review.

Public API — stdlib-only, functions not classes::

    queue_reviews(project, spec_path, *, heading=None, lines=None,
                  trigger="spec-edit", actor=None, dry_run=False) -> list[dict]
    check_drift_gate(project, node_id, executor_id, *,
                     actor=None) -> None  # raises ReconciliationRequired
    list_reviews(project, *, consumer=None, spec_path=None,
                 status="open") -> list[dict]
    resolve_review(project, review_id, *, outcome, notes=None,
                   followup_title=None, actor=None) -> dict

Plus the exception::

    class ReconciliationRequired(Exception):
        review_node_id: str
        drifted_slices: list[dict]
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from taskflow import events as events_mod
from taskflow import spec_input as spec_mod
from taskflow.model import EdgeKind, NodeStatus


COMPONENT_NAME = "downstream-review"

# Outcomes — keep in sync with the docstring + CLI choices.
OUTCOME_NO_IMPACT = "no-impact"
OUTCOME_UPDATE_IN_SCOPE = "update-in-scope"
OUTCOME_UPDATE_OUT_OF_SCOPE = "update-out-of-scope"
OUTCOME_SPEC_REVERT = "spec-revert"

VALID_OUTCOMES = {
    OUTCOME_NO_IMPACT,
    OUTCOME_UPDATE_IN_SCOPE,
    OUTCOME_UPDATE_OUT_OF_SCOPE,
    OUTCOME_SPEC_REVERT,
}

# Triggers
TRIGGER_SPEC_EDIT = "spec-edit"
TRIGGER_PICKUP_GATE = "pickup-gate"

# Status labels used inside component_data (NOT NodeStatus). The node
# itself moves through the standard idea->done lifecycle; this field
# answers "open vs resolved" without parsing the node status.
REVIEW_STATUS_OPEN = "open"
REVIEW_STATUS_RESOLVED = "resolved"


# ---------------------------------------------------------------------------
# Exception raised by Trigger B
# ---------------------------------------------------------------------------


class ReconciliationRequired(Exception):
    """Raised by ``check_drift_gate`` when a flow.enter must be blocked.

    Attributes:
        review_node_id:  the (existing or freshly-created) downstream-review
                         node that must be resolved before the gate will pass.
        drifted_slices:  list of drift entries (same shape as
                         ``spec_input.drift`` returns) that motivated the
                         block — handy for the CLI to print a useful message.
    """

    def __init__(self, review_node_id: str, drifted_slices: List[Dict[str, Any]]) -> None:
        self.review_node_id = review_node_id
        self.drifted_slices = drifted_slices
        # Compose a single-line summary that flow_cli surfaces verbatim.
        slice_bits = []
        for sl in drifted_slices:
            anchor = sl.get("anchor")
            lines = sl.get("lines_was") or sl.get("lines") or []
            where = anchor if anchor else (
                f"L{lines[0]}-L{lines[1]}" if len(lines) >= 2 else "?")
            slice_bits.append(f"{sl.get('path','?')}@{where}")
        slices = ", ".join(slice_bits) if slice_bits else "(unknown slice)"
        super().__init__(
            f"blocked on drift reconciliation — review node {review_node_id}; "
            f"drifted slices: {slices}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _slice_key(anchor: Optional[str], lines: Optional[List[int]]) -> Tuple[str, str]:
    """Produce a stable identity tuple for a slice.

    The consumer view is "specific anchor OR specific line range";
    matches the granularity of ``spec_input._slice_match``.
    Returns ``(kind, value)`` where kind is one of ``anchor`` / ``lines``.
    """
    if anchor:
        return ("anchor", spec_mod._normalise_heading_anchor(anchor))
    if lines and len(lines) >= 2:
        return ("lines", f"{int(lines[0])}-{int(lines[1])}")
    return ("none", "")


def _open_reviews(project) -> List[Any]:
    """All ``downstream-review`` nodes whose component_data status is open."""
    out: List[Any] = []
    for node in project.all_nodes():
        if COMPONENT_NAME not in (node.components or []):
            continue
        bucket = (node.component_data or {}).get(COMPONENT_NAME) or {}
        if (bucket.get("status") or REVIEW_STATUS_OPEN) == REVIEW_STATUS_OPEN:
            out.append(node)
    return out


def _all_reviews(project) -> List[Any]:
    out: List[Any] = []
    for node in project.all_nodes():
        if COMPONENT_NAME in (node.components or []):
            out.append(node)
    return out


def _existing_open_review_for(
    project,
    consumer_node: str,
    spec_path: str,
    anchor: Optional[str],
    lines: Optional[List[int]],
) -> Optional[Any]:
    """Find an OPEN downstream-review for this exact (consumer, spec, slice)."""
    want_key = _slice_key(anchor, lines)
    rel = spec_mod._normalise_path(project, spec_path)
    for node in _open_reviews(project):
        bucket = (node.component_data or {}).get(COMPONENT_NAME) or {}
        if bucket.get("consumer_node") != consumer_node:
            continue
        if bucket.get("spec_path") != rel:
            continue
        slice_rec = bucket.get("slice") or {}
        got_key = _slice_key(slice_rec.get("anchor"), slice_rec.get("lines"))
        if got_key == want_key:
            return node
    return None


def _drift_for_slice(
    project,
    consumer_node: str,
    spec_path: str,
    anchor: Optional[str],
    lines: Optional[List[int]],
) -> Optional[Dict[str, Any]]:
    """Run ``spec_input.drift`` on the consumer and return the entry that
    matches our slice key, or None if no such entry exists."""
    want_key = _slice_key(anchor, lines)
    rel = spec_mod._normalise_path(project, spec_path)
    try:
        entries = spec_mod.drift(project, consumer_node, patch=True)
    except (ValueError, FileNotFoundError):
        return None
    for entry in entries:
        if entry.get("path") != rel:
            continue
        got_key = _slice_key(entry.get("anchor"), entry.get("lines_was"))
        if got_key == want_key:
            return entry
    return None


def _make_drift_snapshot(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a spec_input drift entry into the persisted snapshot shape."""
    return {
        "recorded_slice_sha": entry.get("slice_sha_was"),
        "current_slice_sha": entry.get("slice_sha_now"),
        "patch": entry.get("patch") or "",
        "state": entry.get("state"),
    }


# ---------------------------------------------------------------------------
# Trigger A — queue downstream reviews from the spec-edit side
# ---------------------------------------------------------------------------


def queue_reviews(
    project,
    spec_path: str,
    *,
    heading: Optional[str] = None,
    lines: Optional[Tuple[int, int]] = None,
    trigger: str = TRIGGER_SPEC_EDIT,
    actor: Optional[str] = None,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """For each consumer of the given spec slice whose slice has drifted,
    create (or report) a downstream-review node.

    Selector: if ``heading`` or ``lines`` is given, only consumers
    referencing that specific slice are considered. Otherwise every
    slice on every consumer of ``spec_path`` is checked.

    Returns a list of result dicts, one per consumer-slice pair::

        {
          "consumer": "HW-0042",
          "spec_path": "specs/x.md",
          "slice": {"anchor": "## Foo", "lines": [12, 34]},
          "drift_state": "drift" | "anchor-lost" | "missing" | "clean",
          "action": "created" | "skipped-existing" | "skipped-clean" | "dry-run",
          "review_node": "HW-0099" | None,
        }

    ``dry_run=True`` skips creation and reports what would happen.
    """
    rel = spec_mod._normalise_path(project, spec_path)
    lines_list: Optional[List[int]] = list(lines) if lines else None

    # Discover consumers, narrowed by slice if a selector was given.
    consumer_rows = spec_mod.consumers(
        project, rel,
        slice_anchor=heading, slice_lines=lines,
    )

    out: List[Dict[str, Any]] = []
    for row in consumer_rows:
        consumer_id = row["node"]
        for slice_rec in row.get("slices") or []:
            slice_anchor = slice_rec.get("anchor")
            slice_lines = list(slice_rec.get("lines") or [])
            slice_payload = {
                "anchor": slice_anchor,
                "lines": slice_lines,
            }

            entry = _drift_for_slice(
                project, consumer_id, rel,
                slice_anchor, slice_lines,
            )
            # No drift -> nothing to queue.
            if entry is None or entry.get("state") == "clean":
                out.append({
                    "consumer": consumer_id,
                    "spec_path": rel,
                    "slice": slice_payload,
                    "drift_state": (entry or {}).get("state", "unknown"),
                    "action": "skipped-clean",
                    "review_node": None,
                })
                continue

            existing = _existing_open_review_for(
                project, consumer_id, rel, slice_anchor, slice_lines,
            )
            if existing is not None:
                out.append({
                    "consumer": consumer_id,
                    "spec_path": rel,
                    "slice": slice_payload,
                    "drift_state": entry.get("state"),
                    "action": "skipped-existing",
                    "review_node": existing.id,
                })
                continue

            if dry_run:
                out.append({
                    "consumer": consumer_id,
                    "spec_path": rel,
                    "slice": slice_payload,
                    "drift_state": entry.get("state"),
                    "action": "dry-run",
                    "review_node": None,
                })
                continue

            review = _create_review_node(
                project,
                consumer_id=consumer_id,
                spec_path=rel,
                slice_payload=slice_payload,
                drift_entry=entry,
                trigger=trigger,
                actor=actor,
            )
            out.append({
                "consumer": consumer_id,
                "spec_path": rel,
                "slice": slice_payload,
                "drift_state": entry.get("state"),
                "action": "created",
                "review_node": review.id,
            })
    return out


def _create_review_node(
    project,
    *,
    consumer_id: str,
    spec_path: str,
    slice_payload: Dict[str, Any],
    drift_entry: Dict[str, Any],
    trigger: str,
    actor: Optional[str],
) -> Any:
    """Materialise one downstream-review node + its references / blocks edges."""
    where = slice_payload.get("anchor") or (
        f"L{slice_payload['lines'][0]}-L{slice_payload['lines'][1]}"
        if slice_payload.get("lines") else "?"
    )
    title = (
        f"Reconcile drift for {consumer_id} — {spec_path} @ {where} ({trigger})"
    )
    review = project.new_node(
        components=["work-item", COMPONENT_NAME],
        title=title,
        owner=actor,
        actor=actor,
    )
    node_obj = project.node(review.id)
    node_obj.component_data.setdefault(COMPONENT_NAME, {})
    node_obj.component_data[COMPONENT_NAME].update({
        "consumer_node": consumer_id,
        "spec_path": spec_path,
        "slice": slice_payload,
        "drift_snapshot": _make_drift_snapshot(drift_entry),
        "trigger": trigger,
        "status": REVIEW_STATUS_OPEN,
        "outcome": None,
        "resolution_notes": None,
    })
    project.save_node(node_obj)

    # Wire `references` -> consumer (informational, the review consults it)
    # and `blocks` -> consumer (execution-ordering: consumer cannot proceed
    # past flow.enter on an agent until this review resolves).
    try:
        project.link(review.id, EdgeKind.references, consumer_id,
                     reason=f"downstream-review for spec drift ({trigger})",
                     actor=actor)
    except FileNotFoundError:
        pass
    try:
        project.link(review.id, EdgeKind.blocks, consumer_id,
                     reason=f"drift in {spec_path} @ {where}",
                     actor=actor)
    except FileNotFoundError:
        pass

    events_mod.append(
        project.events_path, "reconcile.review.create",
        node=review.id, actor=actor,
        data={
            "consumer": consumer_id,
            "spec_path": spec_path,
            "slice": slice_payload,
            "trigger": trigger,
        },
    )
    return review


# ---------------------------------------------------------------------------
# Trigger B — pickup gate invoked from flow.enter
# ---------------------------------------------------------------------------


def check_drift_gate(
    project,
    node_id: str,
    executor_id: str,
    *,
    actor: Optional[str] = None,
) -> None:
    """Pre-flight gate for ``flow.enter`` against agent-component executors.

    Fast path: skipped entirely when

      * ``HOPEWELL_SKIP_RECONCILIATION=1`` is set, OR
      * the executor lacks the ``agent`` component, OR
      * the consumer node has no ``spec-input`` references.

    Slow path: runs ``spec_input.drift`` and either passes silently
    (no drift) or raises ``ReconciliationRequired`` (drift detected),
    creating an open review node first if Trigger A hasn't already.
    """
    if os.environ.get("HOPEWELL_SKIP_RECONCILIATION") == "1":
        return

    # Cheap exit: is the executor an `agent`?
    try:
        from taskflow import network as net_mod
    except Exception:
        return
    try:
        net = net_mod.load_network(project.root)
    except Exception:
        return
    ex = net.executors.get(executor_id)
    if ex is None or not ex.has_component("agent"):
        return

    # Cheap exit: does the consumer have any spec-input refs at all?
    try:
        node = project.node(node_id)
    except FileNotFoundError:
        return
    if "spec-input" not in (node.components or []):
        return

    # Slow path — run drift.
    try:
        drift_entries = spec_mod.drift(project, node_id, patch=True)
    except (ValueError, FileNotFoundError):
        return
    drifted = [e for e in drift_entries if e.get("state") and e.get("state") != "clean"]
    if not drifted:
        return

    # For each drifted slice, ensure there is an open review covering it.
    # If none exists, create one. The blocking review is the FIRST drifted
    # slice (deterministic, oldest-first by sort order in spec_input.drift).
    review_node_id: Optional[str] = None
    for entry in drifted:
        slice_anchor = entry.get("anchor")
        slice_lines_was = entry.get("lines_was")
        existing = _existing_open_review_for(
            project, node_id, entry.get("path") or "",
            slice_anchor, slice_lines_was,
        )
        if existing is not None:
            if review_node_id is None:
                review_node_id = existing.id
            continue
        slice_payload = {
            "anchor": slice_anchor,
            "lines": list(slice_lines_was or []),
        }
        review = _create_review_node(
            project,
            consumer_id=node_id,
            spec_path=entry.get("path") or "",
            slice_payload=slice_payload,
            drift_entry=entry,
            trigger=TRIGGER_PICKUP_GATE,
            actor=actor,
        )
        if review_node_id is None:
            review_node_id = review.id

    assert review_node_id is not None  # we returned early if `drifted` was empty
    raise ReconciliationRequired(review_node_id, drifted)


# ---------------------------------------------------------------------------
# List / show
# ---------------------------------------------------------------------------


def list_reviews(
    project,
    *,
    consumer: Optional[str] = None,
    spec_path: Optional[str] = None,
    status: str = "open",
) -> List[Dict[str, Any]]:
    """List downstream-review nodes, filtered by consumer / spec / status.

    Status: ``"open"`` / ``"resolved"`` / ``"all"``.
    """
    status = (status or "open").lower()
    if status not in {"open", "resolved", "all"}:
        raise ValueError(f"unknown status filter: {status!r}")

    rel: Optional[str] = None
    if spec_path:
        rel = spec_mod._normalise_path(project, spec_path)

    out: List[Dict[str, Any]] = []
    for node in _all_reviews(project):
        bucket = (node.component_data or {}).get(COMPONENT_NAME) or {}
        st = bucket.get("status") or REVIEW_STATUS_OPEN
        if status != "all" and st != status:
            continue
        if consumer and bucket.get("consumer_node") != consumer:
            continue
        if rel and bucket.get("spec_path") != rel:
            continue
        out.append({
            "review_node": node.id,
            "title": node.title,
            "consumer_node": bucket.get("consumer_node"),
            "spec_path": bucket.get("spec_path"),
            "slice": bucket.get("slice"),
            "trigger": bucket.get("trigger"),
            "status": st,
            "outcome": bucket.get("outcome"),
            "resolution_notes": bucket.get("resolution_notes"),
            "node_status": node.status.value if hasattr(node.status, "value") else node.status,
        })
    out.sort(key=lambda r: r["review_node"])
    return out


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------


def resolve_review(
    project,
    review_id: str,
    *,
    outcome: str,
    notes: Optional[str] = None,
    followup_title: Optional[str] = None,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Close a downstream-review with one of the four outcomes.

    Side effects (per outcome):

      * ``no-impact``           — re-pin the consumer's ``slice_sha`` to the
                                  current value (idempotent ``add_spec_ref``).
      * ``update-in-scope``     — none beyond closing the review.
      * ``update-out-of-scope`` — create a follow-up work-item that BLOCKS
                                  the consumer; ``followup_title`` REQUIRED.
      * ``spec-revert``         — record the decision; the actual revert is
                                  manual.

    In ALL cases the review's `status` flips to ``resolved``, the
    ``outcome``/``resolution_notes`` are persisted, and the review node
    is closed (which removes the `blocks` edge it held over the consumer).
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"unknown outcome: {outcome!r} "
            f"(valid: {sorted(VALID_OUTCOMES)})"
        )
    if outcome == OUTCOME_UPDATE_OUT_OF_SCOPE and not followup_title:
        raise ValueError(
            "outcome=update-out-of-scope requires --followup-title"
        )

    review = project.node(review_id)
    bucket = (review.component_data or {}).get(COMPONENT_NAME) or {}
    if not bucket:
        raise ValueError(
            f"{review_id} is not a downstream-review node "
            f"(missing component_data['{COMPONENT_NAME}'])"
        )
    if (bucket.get("status") or REVIEW_STATUS_OPEN) != REVIEW_STATUS_OPEN:
        raise ValueError(
            f"{review_id} already resolved (outcome={bucket.get('outcome')})"
        )

    consumer_id = bucket.get("consumer_node")
    spec_path = bucket.get("spec_path")
    slice_rec = bucket.get("slice") or {}
    slice_anchor = slice_rec.get("anchor")
    slice_lines = slice_rec.get("lines")
    slice_lines_tuple: Optional[Tuple[int, int]] = None
    if slice_lines and len(slice_lines) >= 2:
        slice_lines_tuple = (int(slice_lines[0]), int(slice_lines[1]))

    followup_id: Optional[str] = None

    if outcome == OUTCOME_NO_IMPACT:
        if consumer_id and spec_path:
            # Preserve the existing `why` on the consumer's slice — without
            # this, re-adding would silently strip it. We look up the
            # consumer's current ref before re-pinning.
            preserved_why: Optional[str] = None
            try:
                for r in spec_mod.ls_spec_refs(project, consumer_id):
                    if r.get("path") != spec_path:
                        continue
                    if slice_anchor:
                        if r.get("anchor") and (
                            spec_mod._normalise_heading_anchor(r["anchor"])
                            == spec_mod._normalise_heading_anchor(slice_anchor)
                        ):
                            preserved_why = r.get("why")
                            break
                    elif slice_lines_tuple:
                        rl = r.get("lines") or []
                        if (len(rl) >= 2
                                and int(rl[0]) == slice_lines_tuple[0]
                                and int(rl[1]) == slice_lines_tuple[1]):
                            preserved_why = r.get("why")
                            break
            except (ValueError, FileNotFoundError):
                pass
            try:
                spec_mod.add_spec_ref(
                    project, consumer_id, spec_path,
                    heading=slice_anchor,
                    lines=None if slice_anchor else slice_lines_tuple,
                    why=preserved_why,
                    actor=actor,
                )
            except (ValueError, FileNotFoundError) as e:
                raise ValueError(
                    f"no-impact re-pin failed for {consumer_id} -> "
                    f"{spec_path}: {e}"
                )

    elif outcome == OUTCOME_UPDATE_OUT_OF_SCOPE:
        followup = project.new_node(
            components=["work-item"],
            title=followup_title,
            owner=actor,
            actor=actor,
        )
        followup_id = followup.id
        # The follow-up BLOCKS the consumer until it lands.
        try:
            project.link(
                followup.id, EdgeKind.blocks, consumer_id,
                reason=f"spec update spawned by review {review_id}",
                actor=actor,
            )
        except FileNotFoundError:
            pass

    # update-in-scope and spec-revert: no extra side effects beyond the
    # bookkeeping below.

    # Persist the outcome on the review.
    bucket["status"] = REVIEW_STATUS_RESOLVED
    bucket["outcome"] = outcome
    if notes:
        bucket["resolution_notes"] = notes
    if followup_id:
        bucket["followup_node"] = followup_id
    review.component_data[COMPONENT_NAME] = bucket
    project.save_node(review)

    events_mod.append(
        project.events_path, "reconcile.review.resolve",
        node=review_id, actor=actor,
        data={
            "outcome": outcome,
            "notes": notes,
            "consumer_node": consumer_id,
            "spec_path": spec_path,
            "slice": slice_rec,
            "followup_node": followup_id,
        },
    )

    # Close the review node — moves status to done, which the existing
    # `blocks` edge model interprets as "constraint satisfied" (a node
    # whose blockers are all in a terminal status is unblocked).
    try:
        project.close(
            review_id,
            reason=f"reconcile resolved: outcome={outcome}",
            actor=actor,
        )
    except Exception:  # noqa: BLE001
        # Closing a review is best-effort; the bookkeeping above is the
        # source of truth for "open vs resolved".
        pass

    return {
        "review_node": review_id,
        "outcome": outcome,
        "notes": notes,
        "consumer_node": consumer_id,
        "spec_path": spec_path,
        "slice": slice_rec,
        "followup_node": followup_id,
    }
