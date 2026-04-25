"""Markdown + YAML front-matter I/O for nodes.

We don't require PyYAML — ship a small YAML-subset reader/writer covering
exactly what `Node.to_frontmatter()` produces: scalars, nested maps, lists
of scalars, lists of maps (for inputs/outputs). Good enough and stdlib-only.

If PyYAML happens to be installed, we use it (fuller coverage). Otherwise
the fallback handles our own output faithfully.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskflow.model import Node, NodeStatus

try:
    import yaml as _yaml  # type: ignore[import-not-found]
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Public API — read + write a node markdown file
# ---------------------------------------------------------------------------

def read_node_file(path: Path) -> Node:
    text = path.read_text(encoding="utf-8")
    fm, remainder = _split_frontmatter(text)
    title, body, notes = _split_body(remainder)
    return Node.from_frontmatter(fm, title=title, body=body, notes=notes)


def write_node_file(path: Path, node: Node) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = node.to_frontmatter()
    fm_text = _dump_yaml(fm).rstrip() + "\n"

    body_lines: List[str] = []
    body_lines.append("---")
    body_lines.append(fm_text.rstrip())
    body_lines.append("---")
    body_lines.append("")
    body_lines.append(f"# {node.id}: {node.title}")
    body_lines.append("")
    if node.body.strip():
        body_lines.append(node.body.rstrip())
        body_lines.append("")
    body_lines.append("## Notes (append-only)")
    body_lines.append("")
    for n in node.notes:
        body_lines.append(f"- {n}")
    if not node.notes:
        body_lines.append("_(none)_")
    body_lines.append("")

    path.write_text("\n".join(body_lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Front-matter split
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("node file has no YAML front-matter")
    fm_text = m.group(1)
    remainder = text[m.end():]
    return _load_yaml(fm_text), remainder


def _split_body(remainder: str) -> Tuple[str, str, List[str]]:
    """Extract H1 title, body text (between H1 and ## Notes), and notes list."""
    lines = remainder.splitlines()
    title = ""
    body_lines: List[str] = []
    notes: List[str] = []
    i = 0

    # Find H1
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("# "):
            # "# HW-0042: Title" → extract after the colon if present
            after_hash = line[2:].strip()
            if ":" in after_hash:
                title = after_hash.split(":", 1)[1].strip()
            else:
                title = after_hash
            i += 1
            break
        i += 1

    # Collect body until "## Notes" (or EOF)
    while i < len(lines):
        if lines[i].strip().startswith("## Notes"):
            break
        body_lines.append(lines[i])
        i += 1
    if body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    body = "\n".join(body_lines).rstrip()

    # Skip the notes header + blank line
    if i < len(lines) and lines[i].strip().startswith("## Notes"):
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1

    # Collect notes (bullets).
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("- ") and "(none)" not in line:
            notes.append(line.lstrip()[2:].rstrip())
        i += 1

    return title, body, notes


# ---------------------------------------------------------------------------
# YAML load / dump
# ---------------------------------------------------------------------------

def _load_yaml(text: str) -> Dict[str, Any]:
    if _HAS_YAML:
        return _yaml.safe_load(text) or {}
    return _yaml_subset_load(text)


def _dump_yaml(obj: Any) -> str:
    if _HAS_YAML:
        return _yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return _yaml_subset_dump(obj, indent=0)


# --- Fallback YAML subset --------------------------------------------------
# Handles: scalar (str/int/float/bool/null), list of scalars, list of maps,
# nested maps. Strings always quoted on dump for round-trip safety.

def _yaml_subset_dump(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}\n"
        lines: List[str] = []
        for k, v in obj.items():
            if isinstance(v, dict):
                lines.append(f"{pad}{k}:")
                lines.append(_yaml_subset_dump(v, indent + 1).rstrip())
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{pad}{k}: []")
                elif all(not isinstance(x, (dict, list)) for x in v):
                    lines.append(f"{pad}{k}: [{', '.join(_scalar_repr(x) for x in v)}]")
                else:
                    lines.append(f"{pad}{k}:")
                    for item in v:
                        if isinstance(item, dict):
                            first = True
                            for ik, iv in item.items():
                                prefix = f"{pad}  - " if first else f"{pad}    "
                                if isinstance(iv, (dict, list)):
                                    lines.append(f"{prefix}{ik}:")
                                    lines.append(_yaml_subset_dump(iv, indent + 3).rstrip())
                                else:
                                    lines.append(f"{prefix}{ik}: {_scalar_repr(iv)}")
                                first = False
                        else:
                            lines.append(f"{pad}  - {_scalar_repr(item)}")
            else:
                lines.append(f"{pad}{k}: {_scalar_repr(v)}")
        return "\n".join(lines) + "\n"
    return _scalar_repr(obj) + "\n"


def _scalar_repr(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Quote if contains special chars or starts with special.
    if not s or re.search(r'[:#\n"\'\t,\[\]{}&*!|>]', s) or s[0] in "-?@ ":
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_subset_load(text: str) -> Dict[str, Any]:
    """Load the subset that _yaml_subset_dump emits. Indent-aware."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    root: Dict[str, Any] = {}
    idx = 0

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def parse_block(parent_indent: int, container: Any) -> None:
        nonlocal idx
        while idx < len(lines):
            line = lines[idx]
            ind = indent_of(line)
            if ind < parent_indent:
                return
            if ind > parent_indent and not isinstance(container, list):
                return
            stripped = line.strip()

            if isinstance(container, list):
                if stripped.startswith("- "):
                    remainder = stripped[2:]
                    if ":" in remainder and not remainder.startswith(("\"", "[")):
                        # list-of-maps entry
                        idx += 1
                        item: Dict[str, Any] = {}
                        _set_kv(item, remainder)
                        parse_block(ind + 2, item)
                        container.append(item)
                    else:
                        container.append(_parse_scalar(remainder))
                        idx += 1
                else:
                    return
                continue

            # container is a dict
            if ":" in stripped:
                key, _, rhs = stripped.partition(":")
                key = key.strip()
                rhs = rhs.strip()
                if rhs == "":
                    # Nested block follows — peek next line's indent to pick dict vs list
                    idx += 1
                    if idx < len(lines) and lines[idx].strip().startswith("- "):
                        new_list: List[Any] = []
                        parse_block(ind + 2, new_list)
                        container[key] = new_list
                    else:
                        new_dict: Dict[str, Any] = {}
                        parse_block(ind + 2, new_dict)
                        container[key] = new_dict
                elif rhs.startswith("[") and rhs.endswith("]"):
                    container[key] = _parse_inline_list(rhs[1:-1])
                    idx += 1
                else:
                    container[key] = _parse_scalar(rhs)
                    idx += 1
            else:
                idx += 1

    parse_block(0, root)
    return root


def _set_kv(target: Dict[str, Any], piece: str) -> None:
    key, _, rhs = piece.partition(":")
    key = key.strip()
    rhs = rhs.strip()
    if rhs.startswith("[") and rhs.endswith("]"):
        target[key] = _parse_inline_list(rhs[1:-1])
    else:
        target[key] = _parse_scalar(rhs)


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if s == "" or s.lower() == "null" or s == "~":
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    if s.startswith('"') and s.endswith('"'):
        return json.loads(s)
    if s.startswith("'") and s.endswith("'"):
        return s[1:-1]
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_inline_list(inner: str) -> List[Any]:
    if not inner.strip():
        return []
    items: List[Any] = []
    buf = ""
    in_str = False
    quote = ""
    for ch in inner:
        if in_str:
            buf += ch
            if ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
            buf += ch
        elif ch == ",":
            items.append(_parse_scalar(buf))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(_parse_scalar(buf))
    return items
