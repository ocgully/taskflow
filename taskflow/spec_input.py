"""Spec-input component (HW-0031) — quote-by-reference to spec passages.

A work item that declares the `spec-input` component carries, under
``component_data["spec-input"]``, a set of *specs* it references. Each
spec is a file path plus a list of **slices** — specific passages of
that file (by heading or by line range). Each slice records the
content hash at reference time, so drift can be detected later.

Data shape::

    component_data:
      spec-input:
        specs:
          - path: specs/002-canvas/spec.md
            doc_sha: abc123                     # full-file sha at ref time
            slices:
              - anchor: "## Flow Network"       # optional heading slug
                lines: [45, 72]                 # required line range at ref time
                slice_sha: def456               # hash of slice content at ref time
                why: "defines executor routing contract"
              - lines: [103, 118]               # lines-only slice
                slice_sha: 789abc
                why: "..."

Design notes:

* We do NOT invent new edges — spec-inputs live in ``component_data`` so
  pre-v0.9 clients keep working (``extras`` preserves unknown fields;
  ``component_data`` is already round-tripped).
* Heading resolution is markdown-aware but deliberately simple: the
  first ATX heading line (``^(#{1,6}) <text>``) whose normalised slug
  matches ``anchor`` wins; the slice spans that line through the line
  before the next heading of equal or higher level.
* Hashes are stdlib sha256 of the UTF-8 bytes of the slice text, with
  trailing newline normalisation (see `_sha`).
* File paths are resolved relative to the project root when used with
  a Project; the stored ``path`` is always the project-relative form
  (POSIX slashes).

Public API — stdlib-only, functions not classes:

    add_spec_ref(project, node_id, path, *, heading=None, lines=None,
                 why=None, actor=None) -> dict
    ls_spec_refs(project, node_id) -> list[dict]
    rm_spec_ref(project, node_id, path, *, heading=None, lines=None,
                actor=None) -> bool
    drift(project, node_id, *, patch=False) -> list[dict]
    drift_all(project, *, patch=False) -> list[dict]
    consumers(project, spec_path, *, slice_anchor=None,
              slice_lines=None) -> list[dict]
"""
from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from taskflow import events as events_mod


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

COMPONENT_NAME = "spec-input"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# hashing + slug helpers
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    """sha256 of utf-8 bytes. Trailing newlines are preserved — the caller
    is expected to hand us the exact slice text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(text: str) -> str:
    """Normalise a heading for matching: lowercase, strip markdown markers,
    collapse non-alnum to single dashes, trim."""
    s = text.strip().lower()
    # Drop trailing # (setext-style close) and whitespace.
    s = re.sub(r"#+\s*$", "", s).strip()
    s = _SLUG_STRIP_RE.sub("-", s).strip("-")
    return s


def _normalise_heading_anchor(anchor: str) -> str:
    """Accept either raw heading text ('## Flow Network') or a slug ('flow-network').
    Return a canonical slug for comparison."""
    a = anchor.strip()
    # If it starts with '#', strip the markers.
    m = _HEADING_RE.match(a) if a.startswith("#") else None
    if m:
        a = m.group(2)
    return _slugify(a)


def _normalise_path(project, path: str) -> str:
    """Return the project-relative path in POSIX form.

    If `path` is an absolute path, make it relative to the project root.
    If it's already relative, keep it — but rewrite backslashes to forward
    slashes so Windows + *nix storage matches.
    """
    p = Path(path)
    if p.is_absolute():
        try:
            p = p.relative_to(project.root)
        except ValueError:
            # outside the project — store as-is but POSIX-form
            return str(PurePosixPath(*p.parts))
    return str(PurePosixPath(*p.parts))


# ---------------------------------------------------------------------------
# slice extraction
# ---------------------------------------------------------------------------


def _read_file_lines(project, rel_path: str) -> List[str]:
    full = (project.root / rel_path).resolve()
    text = full.read_text(encoding="utf-8")
    # Keep trailing newline splits faithful; splitlines drops the final "\n",
    # we want 1-based indexing with the original text content.
    lines = text.split("\n")
    # Don't keep a trailing empty element from a file ending in "\n".
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _slice_by_heading(lines: List[str], anchor: str) -> Tuple[int, int, str]:
    """Return (start_line, end_line, content). Line numbers are 1-based, inclusive.

    The slice starts at the heading line and ends at the line before the
    next heading of equal or higher level (or EOF).
    """
    want = _normalise_heading_anchor(anchor)
    start_idx: Optional[int] = None
    start_level = 0
    for i, ln in enumerate(lines):
        m = _HEADING_RE.match(ln)
        if not m:
            continue
        level = len(m.group(1))
        slug = _slugify(m.group(2))
        if slug == want:
            start_idx = i
            start_level = level
            break
    if start_idx is None:
        raise ValueError(f"heading not found: {anchor!r}")
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        m = _HEADING_RE.match(lines[j])
        if m and len(m.group(1)) <= start_level:
            end_idx = j
            break
    content = "\n".join(lines[start_idx:end_idx])
    return start_idx + 1, end_idx, content


def _slice_by_lines(lines: List[str], line_range: Tuple[int, int]) -> Tuple[int, int, str]:
    n = len(lines)
    a, b = line_range
    if a < 1 or b < a or a > n:
        raise ValueError(f"line range out of bounds: {a}-{b} (file has {n} lines)")
    b = min(b, n)
    content = "\n".join(lines[a - 1:b])
    return a, b, content


def _extract_slice(project, rel_path: str, *,
                   heading: Optional[str] = None,
                   lines: Optional[Tuple[int, int]] = None
                   ) -> Tuple[int, int, str, str, str]:
    """Return (start, end, content, slice_sha, doc_sha) for the requested slice.

    Exactly one of `heading` or `lines` must be supplied.
    """
    if heading and lines:
        raise ValueError("specify heading OR lines, not both")
    if not heading and not lines:
        raise ValueError("specify heading or lines")

    file_lines = _read_file_lines(project, rel_path)
    doc_sha = _sha("\n".join(file_lines) + "\n")  # file reconstructed
    if heading:
        start, end, content = _slice_by_heading(file_lines, heading)
    else:
        start, end, content = _slice_by_lines(file_lines, lines)  # type: ignore[arg-type]
    return start, end, content, _sha(content), doc_sha


# ---------------------------------------------------------------------------
# component_data round-trip
# ---------------------------------------------------------------------------


def _bucket(node) -> Dict[str, Any]:
    """Return (and initialise) the spec-input bucket on a node."""
    cd = node.component_data
    bucket = cd.setdefault(COMPONENT_NAME, {})
    if "specs" not in bucket or not isinstance(bucket.get("specs"), list):
        bucket["specs"] = []
    return bucket


def _find_spec(bucket: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    for s in bucket["specs"]:
        if s.get("path") == path:
            return s
    return None


def _slice_match(slice_rec: Dict[str, Any], *,
                 heading: Optional[str], lines: Optional[Tuple[int, int]]) -> bool:
    if heading:
        anchor = slice_rec.get("anchor")
        if not anchor:
            return False
        return _normalise_heading_anchor(anchor) == _normalise_heading_anchor(heading)
    if lines:
        recorded = slice_rec.get("lines")
        if not recorded or len(recorded) != 2:
            return False
        return int(recorded[0]) == int(lines[0]) and int(recorded[1]) == int(lines[1])
    return False


# ---------------------------------------------------------------------------
# add / ls / rm
# ---------------------------------------------------------------------------


def add_spec_ref(project, node_id: str, path: str, *,
                 heading: Optional[str] = None,
                 lines: Optional[Tuple[int, int]] = None,
                 why: Optional[str] = None,
                 actor: Optional[str] = None) -> Dict[str, Any]:
    """Record a spec-ref on a work item. Reads the file right now, hashes
    the slice, stores anchor/lines/slice_sha/why.

    Idempotent: calling twice with the same (heading|lines) updates the
    recorded hash + why to the current file content (useful after a
    deliberate re-baseline).

    Emits a ``spec.ref.add`` event.
    """
    rel = _normalise_path(project, path)
    start, end, content, slice_sha, doc_sha = _extract_slice(
        project, rel, heading=heading, lines=lines,
    )

    node = project.node(node_id)
    bucket = _bucket(node)
    spec = _find_spec(bucket, rel)
    if spec is None:
        spec = {"path": rel, "doc_sha": doc_sha, "slices": []}
        bucket["specs"].append(spec)
    else:
        spec["doc_sha"] = doc_sha

    slice_rec: Dict[str, Any] = {
        "lines": [start, end],
        "slice_sha": slice_sha,
    }
    if heading:
        slice_rec["anchor"] = heading.strip()
    if why:
        slice_rec["why"] = why

    # replace any matching existing slice
    existing_idx: Optional[int] = None
    for i, s in enumerate(spec["slices"]):
        if _slice_match(s, heading=heading, lines=lines):
            existing_idx = i
            break
    if existing_idx is not None:
        spec["slices"][existing_idx] = slice_rec
    else:
        spec["slices"].append(slice_rec)

    # Ensure the node declares the component. Preserve order.
    if COMPONENT_NAME not in node.components:
        node.components.append(COMPONENT_NAME)

    project.save_node(node)
    events_mod.append(
        project.events_path, "spec.ref.add",
        node=node_id, actor=actor,
        data={"path": rel, "lines": slice_rec["lines"],
              "anchor": slice_rec.get("anchor"), "slice_sha": slice_sha},
    )
    return {
        "node": node_id,
        "path": rel,
        "anchor": slice_rec.get("anchor"),
        "lines": slice_rec["lines"],
        "slice_sha": slice_sha,
        "doc_sha": doc_sha,
        "why": slice_rec.get("why"),
    }


def ls_spec_refs(project, node_id: str) -> List[Dict[str, Any]]:
    """Flat list of recorded slices across all specs. Sorted by path then
    starting line for stable output."""
    node = project.node(node_id)
    bucket = node.component_data.get(COMPONENT_NAME) or {}
    specs = bucket.get("specs") or []
    out: List[Dict[str, Any]] = []
    for spec in specs:
        path = spec.get("path")
        doc_sha = spec.get("doc_sha")
        for sl in spec.get("slices") or []:
            out.append({
                "path": path,
                "doc_sha": doc_sha,
                "anchor": sl.get("anchor"),
                "lines": list(sl.get("lines") or []),
                "slice_sha": sl.get("slice_sha"),
                "why": sl.get("why"),
            })
    out.sort(key=lambda r: (r.get("path") or "",
                            (r.get("lines") or [0])[0]))
    return out


def rm_spec_ref(project, node_id: str, path: str, *,
                heading: Optional[str] = None,
                lines: Optional[Tuple[int, int]] = None,
                actor: Optional[str] = None) -> bool:
    """Remove a single slice matching heading OR lines. If the last slice
    for a spec goes away, the spec entry is also removed.

    Returns True if anything was removed, False otherwise.
    """
    if not heading and not lines:
        raise ValueError("specify heading or lines to remove")
    rel = _normalise_path(project, path)

    node = project.node(node_id)
    bucket = node.component_data.get(COMPONENT_NAME) or {}
    specs = bucket.get("specs") or []
    spec = _find_spec({"specs": specs}, rel)
    if spec is None:
        return False

    before = len(spec["slices"])
    spec["slices"] = [s for s in spec["slices"]
                      if not _slice_match(s, heading=heading, lines=lines)]
    removed = len(spec["slices"]) < before
    if not removed:
        return False

    if not spec["slices"]:
        bucket["specs"] = [s for s in specs if s.get("path") != rel]

    # If no specs remain, drop the bucket entirely. Keep the component
    # flag on the node — removal of refs doesn't undeclare the role.
    if not (bucket.get("specs")):
        node.component_data.pop(COMPONENT_NAME, None)

    project.save_node(node)
    events_mod.append(
        project.events_path, "spec.ref.rm",
        node=node_id, actor=actor,
        data={"path": rel, "lines": list(lines) if lines else None,
              "anchor": heading},
    )
    return True


# ---------------------------------------------------------------------------
# drift detection
# ---------------------------------------------------------------------------


def _diff_slice(old: str, new: str, *, path: str, anchor: Optional[str],
                lines: List[int]) -> str:
    label = f"{path}"
    if anchor:
        label += f" @ {anchor}"
    if lines:
        label += f" [L{lines[0]}-L{lines[1]}]"
    a = old.splitlines(keepends=False)
    b = new.splitlines(keepends=False)
    return "\n".join(difflib.unified_diff(
        a, b,
        fromfile=f"{label} (recorded)",
        tofile=f"{label} (current)",
        lineterm="",
    ))


def drift(project, node_id: str, *, patch: bool = False) -> List[Dict[str, Any]]:
    """For every recorded slice, re-extract from disk and report drift.

    A returned entry looks like::

        {
          "node": "HW-0031",
          "path": "specs/foo.md",
          "anchor": "## Flow Network" | None,
          "lines_was": [45, 72],
          "lines_now": [45, 74],                # heading re-anchored may shift
          "slice_sha_was": "...",
          "slice_sha_now": "...",
          "state": "clean" | "drift" | "missing" | "anchor-lost",
          "patch": "<unified diff>"             # only when patch=True + drifted
        }

    ``state`` values:
      - ``clean``       — slice_sha matches
      - ``drift``       — content changed
      - ``missing``     — file no longer readable
      - ``anchor-lost`` — heading anchor can't be found in the file anymore
    """
    node = project.node(node_id)
    bucket = node.component_data.get(COMPONENT_NAME) or {}
    specs = bucket.get("specs") or []
    out: List[Dict[str, Any]] = []
    for spec in specs:
        path = spec.get("path")
        for sl in spec.get("slices") or []:
            anchor = sl.get("anchor")
            rec_lines = list(sl.get("lines") or [])
            rec_sha = sl.get("slice_sha")
            entry: Dict[str, Any] = {
                "node": node_id,
                "path": path,
                "anchor": anchor,
                "lines_was": rec_lines,
                "slice_sha_was": rec_sha,
                "why": sl.get("why"),
            }
            try:
                file_lines = _read_file_lines(project, path)
            except (FileNotFoundError, OSError) as e:
                entry["state"] = "missing"
                entry["error"] = str(e)
                out.append(entry)
                continue
            try:
                if anchor:
                    try:
                        start, end, content = _slice_by_heading(file_lines, anchor)
                    except ValueError:
                        entry["state"] = "anchor-lost"
                        out.append(entry)
                        continue
                else:
                    start, end, content = _slice_by_lines(
                        file_lines, (rec_lines[0], rec_lines[1]))
            except ValueError as e:
                entry["state"] = "drift"
                entry["error"] = str(e)
                out.append(entry)
                continue

            now_sha = _sha(content)
            entry["lines_now"] = [start, end]
            entry["slice_sha_now"] = now_sha
            if now_sha == rec_sha:
                entry["state"] = "clean"
            else:
                entry["state"] = "drift"
                if patch:
                    # We only stored the hash of the recorded slice, not its
                    # text. The best we can do is diff "slice at the recorded
                    # line range applied to the CURRENT file" against "slice
                    # at the current anchor/range" — which surfaces a shift
                    # if the heading moved but gives an empty diff if the
                    # line range is unchanged. When the diff is empty we
                    # annotate with the current slice contents + recorded
                    # sha so the reader at least sees what's there now.
                    old_content: Optional[str] = None
                    try:
                        _os, _oe, old_content = _slice_by_lines(
                            file_lines, (rec_lines[0], rec_lines[1]))
                    except Exception:  # noqa: BLE001
                        old_content = None
                    diff_text = ""
                    if old_content is not None:
                        diff_text = _diff_slice(
                            old_content, content,
                            path=path, anchor=anchor, lines=entry["lines_now"],
                        )
                    if not diff_text:
                        # No location shift and no prior text to diff against.
                        # Dump the current slice so the reviewer can eyeball.
                        header = (
                            f"# drift at {path}"
                            + (f" @ {anchor}" if anchor else "")
                            + f" [L{entry['lines_now'][0]}-L{entry['lines_now'][1]}]\n"
                            f"# recorded slice_sha={rec_sha}\n"
                            f"# current  slice_sha={now_sha}\n"
                            f"# (only hashes were stored; dumping current slice)\n"
                        )
                        diff_text = header + content
                    entry["patch"] = diff_text
            out.append(entry)
    return out


def drift_all(project, *, patch: bool = False) -> List[Dict[str, Any]]:
    """Run `drift` across every node that declares the `spec-input` component."""
    out: List[Dict[str, Any]] = []
    for node in project.all_nodes():
        if COMPONENT_NAME not in (node.components or []):
            continue
        out.extend(drift(project, node.id, patch=patch))
    return out


# ---------------------------------------------------------------------------
# reverse-nav
# ---------------------------------------------------------------------------


def consumers(project, spec_path: str, *,
              slice_anchor: Optional[str] = None,
              slice_lines: Optional[Tuple[int, int]] = None) -> List[Dict[str, Any]]:
    """Return nodes referencing `spec_path`.

    If `slice_anchor` or `slice_lines` is given, narrow to references
    whose slice matches that specific heading/line-range.
    """
    rel = _normalise_path(project, spec_path)
    out: List[Dict[str, Any]] = []
    for node in project.all_nodes():
        bucket = (node.component_data or {}).get(COMPONENT_NAME) or {}
        specs = bucket.get("specs") or []
        spec = None
        for s in specs:
            if s.get("path") == rel:
                spec = s
                break
        if spec is None:
            continue
        matching: List[Dict[str, Any]] = []
        for sl in spec.get("slices") or []:
            if slice_anchor or slice_lines:
                if _slice_match(sl,
                                heading=slice_anchor,
                                lines=slice_lines):
                    matching.append(sl)
            else:
                matching.append(sl)
        if not matching:
            continue
        out.append({
            "node": node.id,
            "title": node.title,
            "status": node.status.value if hasattr(node.status, "value") else node.status,
            "path": rel,
            "slices": [{
                "anchor": sl.get("anchor"),
                "lines": list(sl.get("lines") or []),
                "slice_sha": sl.get("slice_sha"),
                "why": sl.get("why"),
            } for sl in matching],
        })
    out.sort(key=lambda r: r["node"])
    return out


# ---------------------------------------------------------------------------
# argparse helpers
# ---------------------------------------------------------------------------


def parse_lines_arg(raw: str) -> Tuple[int, int]:
    """Parse '45-72' or '45:72' into (45, 72). Single '45' -> (45, 45)."""
    if raw is None:
        raise ValueError("empty --lines")
    s = raw.strip()
    if not s:
        raise ValueError("empty --lines")
    for sep in ("-", ":", ","):
        if sep in s:
            a, b = s.split(sep, 1)
            return int(a.strip()), int(b.strip())
    n = int(s)
    return n, n
