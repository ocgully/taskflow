"""Git merge driver for `.hopewell/*.jsonl` append-only logs.

Git invokes the driver when it detects a conflict on a file configured with
`merge=hopewell-jsonl` in `.gitattributes`. Our job: union ancestor + ours +
theirs, dedupe on canonical JSON, sort by `ts` field, write back to `%A`.

Three files involved (per git docs):
  %O  ancestor (common base)
  %A  "ours" — THIS is where we write the resolved result
  %B  "theirs"

Exit 0 = merged cleanly. Exit non-zero = let git surface a conflict.

Installed via `taskflow init` (or `taskflow merge-driver install`) which
writes .gitattributes + runs `git config merge.hopewell-jsonl.driver ...`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def merge_jsonl(ours: Path, theirs: Path, ancestor: Path) -> int:
    """Perform the three-way merge in-place on `ours`.

    Returns the exit code for git (0 = clean).
    """
    all_records: Dict[str, Dict[str, Any]] = {}  # key -> record

    for p in (ancestor, ours, theirs):
        for raw in _read_lines(p):
            rec = _parse_line(raw)
            if rec is None:
                continue
            key = _dedupe_key(rec)
            all_records.setdefault(key, rec)

    merged = sorted(all_records.values(), key=lambda r: (r.get("ts", ""), _dedupe_key(r)))
    ours.write_text(
        "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in merged),
        encoding="utf-8",
    )
    return 0


def _read_lines(p: Path) -> List[str]:
    if not p or not p.is_file():
        return []
    try:
        return p.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()


def _parse_line(line: str):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _dedupe_key(rec: Dict[str, Any]) -> str:
    """Canonical signature for dedup: serialising the whole record sorted."""
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI entrypoint — `taskflow merge-driver jsonl %O %A %B`
# ---------------------------------------------------------------------------


def run_cli(args: List[str]) -> int:
    if len(args) < 3 or args[0] != "jsonl":
        print("usage: taskflow merge-driver jsonl <ancestor> <ours> <theirs>")
        return 1
    _, ancestor, ours, theirs = args[:4]
    return merge_jsonl(Path(ours), Path(theirs), Path(ancestor))
