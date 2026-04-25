"""Comment system — HW-0033.

A commenting layer on top of the markdown / spec viewer. Users can comment
on a node .md or a spec file, either whole-file, anchored to a heading
section, or pinned to a line range. Threads are append-only events in
`.hopewell/comments.jsonl`; current state is the projection of those
events.

Design choices
--------------

* **Append-only, stdlib-only.** One file, JSONL, one dict per line.
  Unknown future kinds are preserved on round-trip (forward compat).
* **Hybrid anchor** with graceful degradation:

    1. `whole-file`       — always resolves.
    2. `heading-section`  — looks up `heading_slug` in live content; on
                            hit, re-computes the section's line range.
    3. `line-range`       — looks for `content_hash` of +/-3 lines
                            around the stored lines; on hit, updates
                            `lines` to the new location. On miss, the
                            comment is flagged `orphaned` so it shows
                            up in a dedicated "needs re-anchoring"
                            bucket rather than being silently dropped.
    4. Escape hatch: `<!-- anchor:foo -->` in the target file. If
       `explicit_anchor` is set and the marker exists, it wins.

* **Promotion to review** creates a new node with components
  `[work-item, comment-review]` + a `references` edge back to the
  commented-on node (or target), and pins the originating thread id in
  `component_data["comment-review"]`.

Public API
----------

Storage layer:

    COMMENTS_FILENAME
    comments_path(project) -> Path
    read_events(path) -> list[dict]
    append_event(path, event) -> dict
    project_threads(events) -> list[Thread]   # projection
    get_thread(events, comment_id) -> Thread | None

Anchor layer:

    build_anchor(type, *, heading=None, lines=None, content=None,
                 explicit_anchor=None) -> dict
    reconcile_anchor(anchor, content) -> dict
        # Returns a new anchor dict with `_state` field set to one of
        # {resolved, drifted, orphaned} and, for line-range, possibly
        # updated `lines`. `content_hash` is recomputed if drifted but
        # still locatable.

Mutations:

    post(project, target, body, *, anchor=None, actor=None) -> Thread
    edit(project, comment_id, body, *, actor=None) -> Thread
    resolve(project, comment_id, reason=None, *, actor=None) -> Thread
    reopen(project, comment_id, *, actor=None) -> Thread
    promote(project, comment_id, title, *,
            body_prefix="", actor=None) -> dict   # new review node info

Query helpers:

    threads_for_target(project, target_id_or_path) -> list[Thread]
    orphans(project) -> list[Thread]   # anchors that can't resolve

`Thread` is a lightweight dataclass representing one comment's current
projected state plus the latest resolution reconciliation result.

Everything in this module is stdlib-only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

COMMENTS_FILENAME = "comments.jsonl"

# Recognised top-level event kinds. Anything else is preserved verbatim but
# doesn't contribute to projected state (forward-compatibility).
KIND_POST = "comment.post"
KIND_EDIT = "comment.edit"
KIND_RESOLVE = "comment.resolve"
KIND_REOPEN = "comment.reopen"

KNOWN_KINDS = {KIND_POST, KIND_EDIT, KIND_RESOLVE, KIND_REOPEN}

# Anchor `type` vocabulary.
ANCHOR_WHOLE_FILE = "whole-file"
ANCHOR_HEADING = "heading-section"
ANCHOR_LINE_RANGE = "line-range"

ANCHOR_TYPES = {ANCHOR_WHOLE_FILE, ANCHOR_HEADING, ANCHOR_LINE_RANGE}

# Reconciliation state labels stored under `_state` on a reconciled anchor.
STATE_RESOLVED = "resolved"   # anchor still points where it used to
STATE_DRIFTED = "drifted"     # located via fallback (hash / heading); lines updated
STATE_ORPHANED = "orphaned"   # cannot find anywhere — surface for re-anchoring

_EXPLICIT_ANCHOR_RE = re.compile(
    r"<!--\s*anchor:\s*([A-Za-z0-9_.\-]+)\s*-->"
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


# ---------------------------------------------------------------------------
# Public dataclass — projection row
# ---------------------------------------------------------------------------


@dataclass
class Thread:
    """Projected current state of a comment thread."""

    id: str
    target: Dict[str, Any]         # {"node": "HW-0042"} or {"spec": "specs/x.md"}
    anchor: Dict[str, Any]         # see build_anchor()
    body: str
    actor: Optional[str]
    ts: str                        # original post ts
    updated: str                   # last-mutation ts
    resolved: bool = False
    resolved_by: Optional[str] = None
    resolved_ts: Optional[str] = None
    resolve_reason: Optional[str] = None
    edits: int = 0                 # count of comment.edit events applied
    # Reconciliation result attached on read — never persisted.
    reconciled_anchor: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Drop reconciled_anchor if unset (noise reduction).
        if d.get("reconciled_anchor") is None:
            d.pop("reconciled_anchor", None)
        return d


# ---------------------------------------------------------------------------
# Timestamp + id helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )


def new_comment_id() -> str:
    """Short, unambiguous comment id: `c-` + 8 hex chars."""
    return "c-" + secrets.token_hex(4)


# ---------------------------------------------------------------------------
# Storage: read / append
# ---------------------------------------------------------------------------


def comments_path(project) -> Path:
    """Path to `.hopewell/comments.jsonl` for this project."""
    return project.hw_dir / COMMENTS_FILENAME


def read_events(path: Path) -> List[Dict[str, Any]]:
    """Read all events (raw dicts). Skips malformed lines silently.

    Unknown kinds are returned verbatim — the projection layer filters."""
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_event(path: Path, event: Dict[str, Any]) -> Dict[str, Any]:
    """Append one event. Injects `ts` if missing. Returns the event as stored."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if "ts" not in event or not event["ts"]:
        event = {**event, "ts": _now()}
    line = json.dumps(event, sort_keys=True, ensure_ascii=False)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    return event


# ---------------------------------------------------------------------------
# Projection: events -> Thread state
# ---------------------------------------------------------------------------


def project_threads(events: Iterable[Dict[str, Any]]) -> List[Thread]:
    """Walk events (in recorded order) and return current per-id thread state."""
    by_id: Dict[str, Thread] = {}
    for ev in events:
        kind = ev.get("kind")
        if kind not in KNOWN_KINDS:
            continue  # forward-compat: preserve unknown kinds on disk, skip here
        cid = ev.get("id")
        if not cid:
            continue
        ts = ev.get("ts") or ""
        actor = ev.get("actor")
        if kind == KIND_POST:
            target = ev.get("target") or {}
            # Target may be {"node": "HW-0042"} or {"spec": "specs/x.md"}.
            anchor = target.get("anchor") or {"type": ANCHOR_WHOLE_FILE}
            # Strip anchor out of target for the projection's target-only record.
            target_clean = {k: v for k, v in target.items() if k != "anchor"}
            thread = Thread(
                id=cid,
                target=target_clean,
                anchor=anchor,
                body=ev.get("body") or "",
                actor=actor,
                ts=ts,
                updated=ts,
            )
            by_id[cid] = thread
        elif kind == KIND_EDIT:
            t = by_id.get(cid)
            if t is None:
                continue
            if "body" in ev:
                t.body = ev.get("body") or ""
            t.updated = ts or t.updated
            t.edits += 1
        elif kind == KIND_RESOLVE:
            t = by_id.get(cid)
            if t is None:
                continue
            t.resolved = True
            t.resolved_by = ev.get("by") or actor
            t.resolved_ts = ts
            t.resolve_reason = ev.get("reason")
            t.updated = ts or t.updated
        elif kind == KIND_REOPEN:
            t = by_id.get(cid)
            if t is None:
                continue
            t.resolved = False
            t.resolved_by = None
            t.resolved_ts = None
            t.resolve_reason = None
            t.updated = ts or t.updated
    return list(by_id.values())


def get_thread(events: Iterable[Dict[str, Any]], comment_id: str) -> Optional[Thread]:
    for t in project_threads(events):
        if t.id == comment_id:
            return t
    return None


# ---------------------------------------------------------------------------
# Anchor helpers — hashing + locating
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify_heading(text: str) -> str:
    """Slug identical to what GitHub-flavoured markdown produces for an anchor.

    Lower, non-alnum -> dash, strip leading/trailing dashes. Good enough
    — the purpose here is stable identity between edits, not link parity.
    """
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\- ]+", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _split_lines(content: str) -> List[str]:
    """Split to lines, preserving 1-based indexability via list[i-1]."""
    lines = content.split("\n")
    # Strip a single trailing empty line from files ending in \n so line
    # counts match what editors display.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _section_lines(lines: List[str], heading_idx: int) -> Tuple[int, int]:
    """Given the 0-based index of a heading line, return [start_1based, end_1based]
    of the entire section (heading line through the line BEFORE the next heading
    at the same-or-shallower level). End-of-file is used if no next heading."""
    if heading_idx < 0 or heading_idx >= len(lines):
        return (1, 1)
    m = _HEADING_RE.match(lines[heading_idx])
    level = len(m.group(1)) if m else 6
    end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        m2 = _HEADING_RE.match(lines[j])
        if m2 and len(m2.group(1)) <= level:
            end = j
            break
    return (heading_idx + 1, end)


def _find_heading_slug(lines: List[str], slug: str) -> Optional[int]:
    """Return 0-based index of first heading whose slug matches, else None."""
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if not m:
            continue
        if slugify_heading(m.group(2)) == slug:
            return i
    return None


def _find_explicit_anchor(content: str, name: str) -> Optional[int]:
    """Return 1-based line number of `<!-- anchor:name -->` marker, else None."""
    lines = _split_lines(content)
    for i, line in enumerate(lines, 1):
        for m in _EXPLICIT_ANCHOR_RE.finditer(line):
            if m.group(1) == name:
                return i
    return None


def _hash_context(lines: List[str], start_1based: int, end_1based: int,
                  radius: int = 3) -> str:
    """Hash of [start - radius, end + radius] clamped to bounds. Used for re-anchoring."""
    lo = max(1, start_1based - radius)
    hi = min(len(lines), end_1based + radius)
    chunk = "\n".join(lines[lo - 1:hi])
    return _sha(chunk)


def _find_line_range_by_hash(lines: List[str],
                             span: int,
                             target_hash: str,
                             radius: int = 3) -> Optional[Tuple[int, int]]:
    """Scan the file for an [i, i+span-1] window whose +/-`radius` context hash
    equals `target_hash`. Returns the (start_1, end_1) that matches or None.

    O(n) scan — fine for spec-sized files. If multiple windows match we
    return the first; collisions on a SHA-256 of a multi-line chunk are
    not a practical concern."""
    n = len(lines)
    if span < 1 or n < span:
        return None
    for start in range(1, n - span + 2):
        end = start + span - 1
        if _hash_context(lines, start, end, radius=radius) == target_hash:
            return (start, end)
    return None


# ---------------------------------------------------------------------------
# Anchor build + reconcile
# ---------------------------------------------------------------------------


def build_anchor(anchor_type: str, *,
                 content: Optional[str] = None,
                 heading: Optional[str] = None,
                 lines: Optional[Tuple[int, int]] = None,
                 explicit_anchor: Optional[str] = None) -> Dict[str, Any]:
    """Construct a storage-shape anchor.

    For heading-section or line-range the caller passes the CURRENT
    file content so we can stamp `content_hash` / resolve heading-slug.
    """
    if anchor_type not in ANCHOR_TYPES:
        raise ValueError(f"unknown anchor type: {anchor_type}")

    out: Dict[str, Any] = {"type": anchor_type}
    if explicit_anchor:
        out["explicit_anchor"] = explicit_anchor

    if anchor_type == ANCHOR_WHOLE_FILE:
        return out

    if content is None:
        raise ValueError("build_anchor: heading/line anchors need current `content`")
    file_lines = _split_lines(content)

    if anchor_type == ANCHOR_HEADING:
        if not heading:
            raise ValueError("heading-section anchor needs `heading`")
        # Accept either full "## Foo" or just "Foo" or a pre-made slug.
        heading_text = heading.lstrip("#").strip()
        slug = slugify_heading(heading_text)
        idx = _find_heading_slug(file_lines, slug)
        if idx is None:
            # Anchor is still valid — we store the slug for later resolution
            # even though the heading isn't present in content right now.
            out["heading_slug"] = slug
            return out
        out["heading_slug"] = slug
        start, end = _section_lines(file_lines, idx)
        out["lines"] = [start, end]
        out["content_hash"] = _hash_context(file_lines, start, end)
        return out

    # anchor_type == ANCHOR_LINE_RANGE
    if not lines:
        raise ValueError("line-range anchor needs `lines=(start, end)`")
    start, end = int(lines[0]), int(lines[1])
    if start < 1 or end < start:
        raise ValueError(f"bad line range: {lines!r}")
    out["lines"] = [start, end]
    # Clamp hashing to file bounds so a slightly-out-of-range spec still hashes.
    clamped_end = min(end, max(1, len(file_lines)))
    clamped_start = min(start, clamped_end)
    out["content_hash"] = _hash_context(file_lines, clamped_start, clamped_end)
    return out


def reconcile_anchor(anchor: Dict[str, Any],
                     content: Optional[str]) -> Dict[str, Any]:
    """Return a NEW dict: the same anchor plus a `_state` field.

    States:
      - `resolved`  : whole-file, or explicit-anchor hit, or heading/line
                      range already points at the original location.
      - `drifted`   : located via fallback (content hash / heading slug);
                      `lines` updated to the new location.
      - `orphaned`  : neither primary nor fallback located anything.

    `content=None` means we couldn't read the target — anchor returned
    unchanged with `_state="orphaned"`."""
    out = dict(anchor)
    typ = out.get("type") or ANCHOR_WHOLE_FILE

    if content is None:
        out["_state"] = STATE_ORPHANED
        return out

    # Explicit-anchor escape hatch — wins if present.
    explicit = out.get("explicit_anchor")
    if explicit:
        ln = _find_explicit_anchor(content, explicit)
        if ln is not None:
            out["_state"] = STATE_RESOLVED
            out["explicit_anchor_line"] = ln
            return out
        # else fall through to the primary strategy

    if typ == ANCHOR_WHOLE_FILE:
        out["_state"] = STATE_RESOLVED
        return out

    lines = _split_lines(content)

    if typ == ANCHOR_HEADING:
        slug = out.get("heading_slug")
        if not slug:
            out["_state"] = STATE_ORPHANED
            return out
        idx = _find_heading_slug(lines, slug)
        if idx is None:
            out["_state"] = STATE_ORPHANED
            return out
        start, end = _section_lines(lines, idx)
        prev_lines = out.get("lines") or [start, end]
        drifted = [start, end] != list(prev_lines)
        out["lines"] = [start, end]
        out["content_hash"] = _hash_context(lines, start, end)
        out["_state"] = STATE_DRIFTED if drifted else STATE_RESOLVED
        return out

    if typ == ANCHOR_LINE_RANGE:
        stored_lines = out.get("lines") or [1, 1]
        try:
            s, e = int(stored_lines[0]), int(stored_lines[1])
        except (TypeError, ValueError, IndexError):
            out["_state"] = STATE_ORPHANED
            return out
        stored_hash = out.get("content_hash")
        span = max(1, e - s + 1)
        # Primary: is the stored range still valid?
        if 1 <= s <= len(lines) and e <= len(lines) and stored_hash:
            current_hash = _hash_context(lines, s, e)
            if current_hash == stored_hash:
                out["_state"] = STATE_RESOLVED
                return out
        # Fallback: locate the stored context-hash anywhere in the file.
        if stored_hash:
            located = _find_line_range_by_hash(lines, span, stored_hash)
            if located is not None:
                out["lines"] = [located[0], located[1]]
                out["_state"] = STATE_DRIFTED
                return out
        # Give up.
        out["_state"] = STATE_ORPHANED
        return out

    # Unknown anchor type — treat as orphaned rather than crashing.
    out["_state"] = STATE_ORPHANED
    return out


# ---------------------------------------------------------------------------
# Target resolution: content loader for a node or a spec path
# ---------------------------------------------------------------------------


def _resolve_target(project, target: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Given a target dict, return (content or None, human_label).

    Accepted shapes:
        {"node":  "HW-0042"}
        {"spec":  "specs/x.md"}           # path relative to project root
    """
    if not isinstance(target, dict):
        return (None, "?")
    if "node" in target and target["node"]:
        node_id = str(target["node"])
        try:
            p = project.node_path(node_id)
            return (p.read_text(encoding="utf-8"), node_id)
        except (FileNotFoundError, OSError):
            return (None, node_id)
    if "spec" in target and target["spec"]:
        rel = str(target["spec"])
        candidate = Path(rel)
        if candidate.is_absolute():
            return (None, rel)
        resolved = (project.root / candidate).resolve()
        try:
            resolved.relative_to(project.root)
        except ValueError:
            return (None, rel)
        if not resolved.is_file():
            return (None, rel)
        try:
            return (resolved.read_text(encoding="utf-8"), rel)
        except OSError:
            return (None, rel)
    return (None, "?")


def _parse_target_id(target_id_or_path: str) -> Dict[str, Any]:
    """Heuristic: does `x` look like a node id (PREFIX-####)? Else a spec path."""
    from taskflow.model import parse_node_id
    try:
        parse_node_id(target_id_or_path)
        return {"node": target_id_or_path}
    except ValueError:
        return {"spec": target_id_or_path.replace("\\", "/")}


# ---------------------------------------------------------------------------
# High-level mutations
# ---------------------------------------------------------------------------


def post(project, target_id_or_path: str, body: str, *,
         anchor_type: str = ANCHOR_WHOLE_FILE,
         heading: Optional[str] = None,
         lines: Optional[Tuple[int, int]] = None,
         explicit_anchor: Optional[str] = None,
         actor: Optional[str] = None) -> Thread:
    """Record a new comment. Returns the projected Thread.

    The anchor is built against the CURRENT target content so
    `content_hash` is accurate at time-of-post."""
    target = _parse_target_id(target_id_or_path)
    content, _label = _resolve_target(project, target)
    # whole-file anchors don't require content, but heading/line do
    anchor = build_anchor(anchor_type,
                          content=content,
                          heading=heading,
                          lines=lines,
                          explicit_anchor=explicit_anchor)

    cid = new_comment_id()
    event = {
        "kind": KIND_POST,
        "id": cid,
        "target": {**target, "anchor": anchor},
        "body": body,
        "actor": actor,
    }
    append_event(comments_path(project), event)
    thread = get_thread(read_events(comments_path(project)), cid)
    # Should be non-None since we just wrote it.
    assert thread is not None
    return _attach_reconciliation(project, thread)


def edit(project, comment_id: str, body: str, *,
         actor: Optional[str] = None) -> Thread:
    path = comments_path(project)
    existing = get_thread(read_events(path), comment_id)
    if existing is None:
        raise KeyError(f"comment not found: {comment_id}")
    append_event(path, {"kind": KIND_EDIT, "id": comment_id,
                        "body": body, "actor": actor})
    t = get_thread(read_events(path), comment_id)
    assert t is not None
    return _attach_reconciliation(project, t)


def resolve(project, comment_id: str, *,
            reason: Optional[str] = None,
            actor: Optional[str] = None) -> Thread:
    path = comments_path(project)
    existing = get_thread(read_events(path), comment_id)
    if existing is None:
        raise KeyError(f"comment not found: {comment_id}")
    event = {"kind": KIND_RESOLVE, "id": comment_id, "actor": actor}
    if actor:
        event["by"] = actor
    if reason:
        event["reason"] = reason
    append_event(path, event)
    t = get_thread(read_events(path), comment_id)
    assert t is not None
    return _attach_reconciliation(project, t)


def reopen(project, comment_id: str, *, actor: Optional[str] = None) -> Thread:
    path = comments_path(project)
    existing = get_thread(read_events(path), comment_id)
    if existing is None:
        raise KeyError(f"comment not found: {comment_id}")
    append_event(path, {"kind": KIND_REOPEN, "id": comment_id, "actor": actor})
    t = get_thread(read_events(path), comment_id)
    assert t is not None
    return _attach_reconciliation(project, t)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def _attach_reconciliation(project, thread: Thread) -> Thread:
    content, _ = _resolve_target(project, thread.target)
    thread.reconciled_anchor = reconcile_anchor(thread.anchor, content)
    return thread


def all_threads(project, *, attach_reconciliation: bool = True) -> List[Thread]:
    events = read_events(comments_path(project))
    threads = project_threads(events)
    if attach_reconciliation:
        for t in threads:
            _attach_reconciliation(project, t)
    return threads


def threads_for_target(project, target_id_or_path: str) -> List[Thread]:
    want = _parse_target_id(target_id_or_path)
    out: List[Thread] = []
    for t in all_threads(project):
        if _target_matches(t.target, want):
            out.append(t)
    # Chronological — oldest first feels right for a discussion thread.
    out.sort(key=lambda x: x.ts)
    return out


def _target_matches(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Targets match if they point at the same node or the same spec path."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get("node") and a.get("node") == b.get("node"):
        return True
    if a.get("spec") and b.get("spec"):
        return _norm_path(a["spec"]) == _norm_path(b["spec"])
    return False


def _norm_path(p: str) -> str:
    return str(p).replace("\\", "/").strip().lstrip("./")


def orphans(project) -> List[Thread]:
    """Threads whose anchors failed reconciliation — the 'needs re-anchoring' bucket."""
    out: List[Thread] = []
    for t in all_threads(project):
        rec = t.reconciled_anchor or {}
        if rec.get("_state") == STATE_ORPHANED:
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Promotion to review node
# ---------------------------------------------------------------------------


def promote(project, comment_id: str, title: str, *,
            body_prefix: str = "",
            actor: Optional[str] = None) -> Dict[str, Any]:
    """Promote a comment thread to a `comment-review` node.

    The new node gets components `[work-item, comment-review]`, a
    `references` edge back to the commented-on node (if the target was
    a node), and `component_data["comment-review"]` pins the originating
    thread + anchor.

    Spec-file targets don't have a node to link to via `references`;
    in that case the edge is still recorded in edges.jsonl with the
    spec path as `to` (for trace-ability) but no node-side materialisation.

    Returns:
        {
          "review_node": "<new node id>",
          "thread_id":   "c-abc",
          "references":  {"to": <node_id_or_spec_path>, "kind": "references"}
        }
    """
    from taskflow.model import EdgeKind

    path = comments_path(project)
    thread = get_thread(read_events(path), comment_id)
    if thread is None:
        raise KeyError(f"comment not found: {comment_id}")

    target = thread.target or {}
    commented_on = target.get("node") or target.get("spec") or ""

    # Build the new node.
    new_node = project.new_node(
        components=["work-item", "comment-review"],
        title=title,
        actor=actor,
    )

    # Pin the thread on the new node.
    node_obj = project.node(new_node.id)
    node_obj.component_data.setdefault("comment-review", {})
    node_obj.component_data["comment-review"].update({
        "thread_id": thread.id,
        "commented_on": commented_on,
        "anchor": dict(thread.anchor or {}),
    })
    # Optional body prefix so the reviewer sees what the comment said.
    if body_prefix or thread.body:
        quoted = thread.body.strip()
        if quoted:
            cited = "\n".join(f"> {ln}" for ln in quoted.splitlines())
        else:
            cited = ""
        lead = body_prefix.rstrip()
        bits: List[str] = []
        if lead:
            bits.append(lead)
        if cited:
            bits.append(f"Original comment ({thread.id} by "
                        f"{thread.actor or 'unknown'} at {thread.ts}):\n\n{cited}")
        if bits:
            node_obj.body = "\n\n".join(bits)
    project.save_node(node_obj)

    # Wire the `references` edge back to the commented-on target.
    ref_info: Dict[str, Any] = {"to": commented_on, "kind": "references"}
    if commented_on:
        try:
            project.link(new_node.id, EdgeKind.references, commented_on,
                         reason=f"promoted from comment {thread.id}",
                         actor=actor)
        except FileNotFoundError:
            # Target isn't a node (e.g. spec path that doesn't parse) — drop
            # a hand-written record in component_data; the review still pins
            # the thread so nothing is lost.
            ref_info["note"] = "target not a node; edge not materialised"

    return {
        "review_node": new_node.id,
        "thread_id": thread.id,
        "references": ref_info,
    }


# ---------------------------------------------------------------------------
# Convenience: threads to dict (JSON-friendly)
# ---------------------------------------------------------------------------


def thread_to_dict(t: Thread) -> Dict[str, Any]:
    return t.to_dict()


def threads_to_dicts(ts: Iterable[Thread]) -> List[Dict[str, Any]]:
    return [t.to_dict() for t in ts]
