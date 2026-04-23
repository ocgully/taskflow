"""`.hopewell/meta.json` — the version contract for a project.

Written at `init`, refreshed at `migrate`, read on every `Project.load`.
Gates cross-version compatibility:

  - if the on-disk schema is newer than this package understands, refuse to
    act ("upgrade Hopewell");
  - if the on-disk schema is older than this package's minimum, refuse
    ("run `hopewell migrate`");
  - if config pins a `minimum_version` floor and this package is below it,
    refuse ("pin up or pip install -U").

All "refuse" cases raise typed errors (`HopewellVersionError`) with the
exact hint. Nothing silently corrupts across versions.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hopewell import SCHEMA_VERSION, __version__


META_FILE = "meta.json"


class HopewellVersionError(RuntimeError):
    """Raised when a project's schema / version contract conflicts with this package."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class MetaFile:
    hopewell_schema: str = SCHEMA_VERSION
    hopewell_version_last_setup: str = __version__
    created_at: str = ""
    last_migrated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hopewell_schema": self.hopewell_schema,
            "hopewell_version_last_setup": self.hopewell_version_last_setup,
            "created_at": self.created_at,
            "last_migrated_at": self.last_migrated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MetaFile":
        return cls(
            hopewell_schema=d.get("hopewell_schema") or SCHEMA_VERSION,
            hopewell_version_last_setup=d.get("hopewell_version_last_setup") or __version__,
            created_at=d.get("created_at") or "",
            last_migrated_at=d.get("last_migrated_at") or "",
        )


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def path_of(hw_dir: Path) -> Path:
    return hw_dir / META_FILE


def load(hw_dir: Path) -> Optional[MetaFile]:
    p = path_of(hw_dir)
    if not p.is_file():
        return None
    try:
        return MetaFile.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return None


def write_for_init(hw_dir: Path) -> MetaFile:
    now = _now_iso()
    mf = MetaFile(
        hopewell_schema=SCHEMA_VERSION,
        hopewell_version_last_setup=__version__,
        created_at=now,
        last_migrated_at=now,
    )
    _write(hw_dir, mf)
    return mf


def write_for_migrate(hw_dir: Path) -> MetaFile:
    existing = load(hw_dir)
    now = _now_iso()
    mf = MetaFile(
        hopewell_schema=SCHEMA_VERSION,
        hopewell_version_last_setup=__version__,
        created_at=(existing.created_at if existing else now),
        last_migrated_at=now,
    )
    _write(hw_dir, mf)
    return mf


def _write(hw_dir: Path, mf: MetaFile) -> None:
    p = path_of(hw_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mf.to_dict(), indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


# ---------------------------------------------------------------------------
# Compatibility check
# ---------------------------------------------------------------------------


def check_compatibility(mf: Optional[MetaFile], *, minimum_version: Optional[str] = None) -> None:
    """Raise HopewellVersionError if this package can't safely act on the project.

    If `mf` is None (meta.json missing for any reason), the caller is
    expected to auto-heal by writing one; we do not raise here.
    """
    if mf is not None:
        disk_schema = _schema_tuple(mf.hopewell_schema)
        pkg_schema = _schema_tuple(SCHEMA_VERSION)
        if disk_schema > pkg_schema:
            raise HopewellVersionError(
                f"this .hopewell/ uses schema {mf.hopewell_schema}, but this "
                f"Hopewell (v{__version__}) understands up to schema {SCHEMA_VERSION}. "
                f"Upgrade via `pip install -U hopewell` and retry."
            )
        if disk_schema < pkg_schema:
            raise HopewellVersionError(
                f"this .hopewell/ uses schema {mf.hopewell_schema}; this "
                f"Hopewell (v{__version__}) wants schema {SCHEMA_VERSION}. "
                f"Run `hopewell migrate` to upgrade the project files."
            )

    # minimum_version check — applies even without meta.json present.
    if minimum_version:
        if _version_tuple(__version__) < _version_tuple(minimum_version):
            raise HopewellVersionError(
                f"this project pins minimum_version = '{minimum_version}' "
                f"but this Hopewell is v{__version__}. "
                f"Run `pip install -U hopewell>={minimum_version}` and retry."
            )


# ---------------------------------------------------------------------------
# Version comparison — stdlib-only, simple dotted-int tuple
# ---------------------------------------------------------------------------


def _version_tuple(v: str) -> Tuple[int, ...]:
    parts: list = []
    for raw in v.split("."):
        # Strip pre-release suffix (e.g. "0.6.0a1" -> "0")
        digits = ""
        for ch in raw:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _schema_tuple(s: str) -> Tuple[int, ...]:
    # Schema is currently always an int-as-str (e.g. "1"). Use the same tuple
    # machinery so future schemas like "1.1" also compare naturally.
    return _version_tuple(s)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
