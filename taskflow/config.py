"""TOML config loader + defaults.

Uses stdlib `tomllib` (Python 3.11+). For 3.10 compat we fall back to a
small in-tree parser covering the keys we actually use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib as _toml  # type: ignore[attr-defined]
    _HAS_TOMLLIB = True
except ImportError:  # Python 3.10
    _HAS_TOMLLIB = False


@dataclass
class GithubConfig:
    repo: Optional[str] = None
    default_components: List[str] = field(default_factory=lambda: ["work-item"])
    label_to_components: Dict[str, str] = field(default_factory=dict)
    sync_interval_minutes: int = 0
    token_env: str = "GITHUB_TOKEN"


@dataclass
class CoordinationConfig:
    # Branch-as-claim (v0.5). "auto" = use if origin exists + remote reachable;
    # "always" = require push; "never" = local claims only.
    mode: str = "auto"
    base_branch: str = "main"
    stale_claim_days: int = 14
    # v0.5.2: repo-level minimum Hopewell version floor. None = no enforcement.
    # A load below this version refuses with a typed error + upgrade hint.
    minimum_version: Optional[str] = None


@dataclass
class OrchestratorConfig:
    max_parallel: int = 4


@dataclass
class ProjectConfig:
    name: str = "unnamed"
    # New projects default to TF (TaskFlow). Existing projects' config.toml
    # files have `id_prefix = "HW"` which is preserved on load — existing
    # HW-NNNN ticket IDs are immutable across the rebrand.
    id_prefix: str = "TF"
    id_pad: int = 4
    enabled_components: List[str] = field(default_factory=list)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    github: GithubConfig = field(default_factory=GithubConfig)
    coordination: CoordinationConfig = field(default_factory=CoordinationConfig)

    @classmethod
    def default(cls, name: str = "unnamed") -> "ProjectConfig":
        cfg = cls(name=name)
        cfg.enabled_components = [
            "work-item", "deliverable", "user-facing", "internal",
            "defect", "risk", "debt",
            "test", "documentation", "screenshot", "design", "code-map",
            "grouping", "deployment-target", "approval-gate", "flagged",
            "retriable", "github-issue",
        ]
        return cfg

    def to_toml_string(self) -> str:
        lines = [
            "# .taskflow/config.toml  —  edit freely.",
            "",
            "[project]",
            f'name = "{self.name}"',
            f'id_prefix = "{self.id_prefix}"',
            f'id_pad = {self.id_pad}',
            "",
            "[components]",
            'enabled = [',
            *[f'  "{c}",' for c in self.enabled_components],
            "]",
            "",
            "[orchestrator]",
            f"max_parallel = {self.orchestrator.max_parallel}",
            "",
            "[github]",
            f'repo = "{self.github.repo or ""}"',
            f'default_components = {_toml_list(self.github.default_components)}',
            f'sync_interval_minutes = {self.github.sync_interval_minutes}',
            f'token_env = "{self.github.token_env}"',
            "",
            "[github.label_to_components]",
        ]
        for label, comp in sorted(self.github.label_to_components.items()):
            lines.append(f'"{label}" = "{comp}"')
        lines += [
            "",
            "[coordination]",
            f'mode = "{self.coordination.mode}"               # auto | always | never',
            f'base_branch = "{self.coordination.base_branch}"',
            f'stale_claim_days = {self.coordination.stale_claim_days}',
        ]
        if self.coordination.minimum_version:
            lines.append(f'minimum_version = "{self.coordination.minimum_version}"')
        else:
            lines.append('# minimum_version = "0.5.2"    # uncomment to pin a floor')
        return "\n".join(lines) + "\n"


def _toml_list(xs: List[str]) -> str:
    return "[" + ", ".join(f'"{x}"' for x in xs) + "]"


def load(config_path: Path) -> ProjectConfig:
    if not config_path.is_file():
        return ProjectConfig.default()
    text = config_path.read_text(encoding="utf-8")
    data = _parse_toml(text)
    return _from_dict(data)


def _parse_toml(text: str) -> Dict[str, Any]:
    if _HAS_TOMLLIB:
        return _toml.loads(text)
    return _fallback_parse(text)


def _from_dict(d: Dict[str, Any]) -> ProjectConfig:
    proj = d.get("project", {}) or {}
    comp = d.get("components", {}) or {}
    orch = d.get("orchestrator", {}) or {}
    gh = d.get("github", {}) or {}
    coord = d.get("coordination", {}) or {}

    cfg = ProjectConfig(
        name=proj.get("name", "unnamed"),
        id_prefix=proj.get("id_prefix", "TF"),
        id_pad=int(proj.get("id_pad", 4)),
        enabled_components=list(comp.get("enabled", [])),
        orchestrator=OrchestratorConfig(
            max_parallel=int(orch.get("max_parallel", 4)),
        ),
        github=GithubConfig(
            repo=gh.get("repo") or None,
            default_components=list(gh.get("default_components", ["work-item"])),
            label_to_components=dict(gh.get("label_to_components", {})),
            sync_interval_minutes=int(gh.get("sync_interval_minutes", 0)),
            token_env=gh.get("token_env", "GITHUB_TOKEN"),
        ),
        coordination=CoordinationConfig(
            mode=coord.get("mode", "auto"),
            base_branch=coord.get("base_branch", "main"),
            stale_claim_days=int(coord.get("stale_claim_days", 14)),
            minimum_version=coord.get("minimum_version") or None,
        ),
    )
    if not cfg.enabled_components:
        cfg.enabled_components = ProjectConfig.default().enabled_components
    return cfg


# ---------------------------------------------------------------------------
# Minimal TOML fallback for Python 3.10 — just the subset we emit above.
# ---------------------------------------------------------------------------

def _fallback_parse(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    cur_section: Dict[str, Any] = out
    cur_path: List[str] = []
    buffer_key = None
    buffer_lines: List[str] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()

        if buffer_key is not None:
            buffer_lines.append(line)
            if "]" in line:
                joined = "\n".join(buffer_lines)
                cur_section[buffer_key] = _parse_list(joined[joined.index("[") + 1 : joined.rindex("]")])
                buffer_key = None
                buffer_lines = []
            continue

        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("[") and stripped.endswith("]"):
            path = stripped[1:-1].split(".")
            d = out
            for part in path:
                d = d.setdefault(part, {})
            cur_section = d
            cur_path = path
            continue

        if "=" not in stripped:
            continue
        key, _, rhs = stripped.partition("=")
        key = key.strip().strip('"')
        rhs = rhs.strip()
        if rhs.startswith("[") and "]" not in rhs:
            buffer_key = key
            buffer_lines = [rhs]
            continue
        cur_section[key] = _parse_value(rhs)

    return out


def _parse_value(s: str) -> Any:
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        return _parse_list(s[1:-1])
    if s in ("true", "false"):
        return s == "true"
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_list(inner: str) -> List[Any]:
    items: List[Any] = []
    # naive: split on commas not inside quotes
    buf = ""
    in_str = False
    for ch in inner:
        if ch == '"':
            in_str = not in_str
            buf += ch
        elif ch == "," and not in_str:
            if buf.strip():
                items.append(_parse_value(buf))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        items.append(_parse_value(buf))
    return items


def write(config_path: Path, cfg: ProjectConfig) -> None:
    config_path.write_text(cfg.to_toml_string(), encoding="utf-8")
