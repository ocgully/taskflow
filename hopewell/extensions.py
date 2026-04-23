"""Project-level extensions: custom processors + custom components.

Two kinds of extension files live under the project's `.hopewell/` tree:

* `.hopewell/processors/*.py` — Python modules that register processors
  via `@hopewell.orchestrator.processor(...)`. They are imported with
  `importlib.util.spec_from_file_location` so they don't need to be on
  `sys.path` and don't require an `__init__.py`.

* `.hopewell/components/*.yaml` — declarative component definitions.
  Each file declares ONE component with shape:

      name: playtest-feedback
      description: Feedback collected from a playtest session
      schema:
        session_id: string
        cohort: string
      required_fields: [session_id]

`load_project_extensions` is called from `Project.load` after the
built-in component registry is constructed. A failure in any ONE file
is isolated: it's captured in the returned `errors` list; siblings
still load.

SECURITY: processor files are arbitrary Python executed with the
privileges of the running process. Anyone who can write into
`.hopewell/processors/` can run code. Treat `.hopewell/` the same as
any other source tree. See `docs/security.md` (proposed) for
warnings you should surface in your docs.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hopewell.project import Project


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def load_project_extensions(project: "Project") -> Dict[str, Any]:
    """Scan `.hopewell/processors/*.py` and `.hopewell/components/*.yaml`,
    register any it finds, and return a summary:

        {
          "processors_loaded": int,
          "processors_files": [<absolute path>, ...],
          "components_loaded": int,
          "components_files": [<absolute path>, ...],
          "errors": [{"file": <path>, "kind": "processor"|"component", "error": str}, ...],
        }

    Errors in any individual file do NOT raise — they are collected so
    the project can still load with partial extensions.
    """
    hw = project.hw_dir
    summary: Dict[str, Any] = {
        "processors_loaded": 0,
        "processors_files": [],
        "components_loaded": 0,
        "components_files": [],
        "errors": [],
    }

    proc_dir = hw / "processors"
    comp_dir = hw / "components"

    if proc_dir.is_dir():
        loaded, files, errs = _load_processors(proc_dir)
        summary["processors_loaded"] = loaded
        summary["processors_files"] = files
        summary["errors"].extend(errs)

    if comp_dir.is_dir():
        loaded, files, errs = _load_components(comp_dir, project.registry)
        summary["components_loaded"] = loaded
        summary["components_files"] = files
        summary["errors"].extend(errs)

    return summary


# ---------------------------------------------------------------------------
# Processor loading
# ---------------------------------------------------------------------------


def _load_processors(proc_dir: Path) -> "tuple[int, List[str], List[Dict[str, str]]]":
    """Import each `*.py` in proc_dir. Their import-time `@processor(...)`
    decorators register into hopewell.orchestrator._REGISTRY directly.

    Registry state is captured before/after each import so that ANY of
    the importing module's registrations can be counted — including
    processors wired via helpers, not just ones that happen to show up
    as a top-level decorator call.
    """
    from hopewell import orchestrator

    loaded = 0
    files: List[str] = []
    errors: List[Dict[str, str]] = []

    for path in sorted(proc_dir.glob("*.py")):
        if path.name.startswith("_"):
            # Skip dunders + private helpers by convention.
            continue
        # Wipe any previously-registered rules that came from THIS file so
        # repeated `Project.load()` calls don't accumulate duplicates.
        mod_name = _module_name_for(path)
        orchestrator._REGISTRY[:] = [
            r for r in orchestrator._REGISTRY
            if getattr(r.fn, "__module__", None) != mod_name
        ]
        before = len(orchestrator._REGISTRY)
        try:
            _import_processor_file(path, mod_name)
        except Exception as e:  # noqa: BLE001 — intentional broad catch
            errors.append({
                "file": str(path),
                "kind": "processor",
                "error": f"{type(e).__name__}: {e}",
                "traceback": _short_tb(),
            })
            continue
        after = len(orchestrator._REGISTRY)
        if after > before:
            loaded += (after - before)
            files.append(str(path))
        else:
            # No decorator fired — not fatal, but worth surfacing.
            errors.append({
                "file": str(path),
                "kind": "processor",
                "error": "imported without registering any processors (no @processor calls?)",
            })

    return loaded, files, errors


def _module_name_for(path: Path) -> str:
    """Stable synthetic module name for a processor file. Same path = same
    name, so repeated loads overwrite the entry in sys.modules rather than
    stacking fresh copies."""
    # We want determinism (same path -> same name) AND uniqueness across
    # different absolute paths that share a stem. Using the resolved absolute
    # path gives us both.
    resolved = str(path.resolve()).replace("\\", "/")
    suffix = f"{_stable_hash(resolved):x}"
    return f"hopewell_ext_{_sanitize(path.stem)}_{suffix}"


def _import_processor_file(path: Path, mod_name: str) -> None:
    """Import a single .py file by absolute path, without needing it on sys.path."""
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec so relative imports inside the user file (if any)
    # can find the module in sys.modules. Remove on failure to avoid poisoning.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _stable_hash(s: str) -> int:
    """Deterministic (non-PYTHONHASHSEED-salted) hash of a string."""
    import hashlib
    h = hashlib.sha1(s.encode("utf-8")).digest()
    # 32-bit fingerprint is plenty for collision-resistance among a handful
    # of project-local processor files.
    return int.from_bytes(h[:4], "big")


# ---------------------------------------------------------------------------
# Component loading
# ---------------------------------------------------------------------------


def _load_components(comp_dir: Path, registry) -> "tuple[int, List[str], List[Dict[str, str]]]":
    from hopewell.model import Component
    from hopewell.storage import _HAS_YAML, _yaml_subset_load

    try:
        import yaml as _yaml  # type: ignore[import-not-found]
        has_yaml = True
    except ImportError:
        _yaml = None  # type: ignore[assignment]
        has_yaml = False

    loaded = 0
    files: List[str] = []
    errors: List[Dict[str, str]] = []

    for path in sorted(list(comp_dir.glob("*.yaml")) + list(comp_dir.glob("*.yml"))):
        try:
            text = path.read_text(encoding="utf-8")
            if has_yaml:
                data = _yaml.safe_load(text) or {}
            else:
                data = _yaml_subset_load(text)
            if not isinstance(data, dict):
                raise ValueError(f"top-level of {path.name} must be a mapping")
            name = data.get("name")
            if not name or not isinstance(name, str):
                raise ValueError(f"{path.name}: missing string `name`")
            description = data.get("description", "") or ""
            schema = data.get("schema", {}) or {}
            if not isinstance(schema, dict):
                raise ValueError(f"{path.name}: `schema` must be a mapping")
            required_fields = data.get("required_fields", []) or []
            if not isinstance(required_fields, list):
                raise ValueError(f"{path.name}: `required_fields` must be a list")
            component = Component(
                name=name,
                description=description,
                schema=schema,
                required_fields=[str(x) for x in required_fields],
            )
            registry.register(component)
            loaded += 1
            files.append(str(path))
        except Exception as e:  # noqa: BLE001
            errors.append({
                "file": str(path),
                "kind": "component",
                "error": f"{type(e).__name__}: {e}",
            })

    return loaded, files, errors


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------


def list_loaded(project: "Project") -> Dict[str, Any]:
    """Summarise what's currently registered — intended for a future
    `hopewell extensions list` CLI. Does NOT re-scan; reports the state
    after `Project.load` ran.
    """
    from hopewell import orchestrator

    processors = [
        {
            "name": r.name,
            "requires": sorted(r.requires),
            "priority": r.priority,
            "module": getattr(r.fn, "__module__", None),
        }
        for r in orchestrator._REGISTRY
    ]
    components = [
        {
            "name": n,
            "description": project.registry.get(n).description if project.registry.get(n) else "",
            "required_fields": list(project.registry.get(n).required_fields) if project.registry.get(n) else [],
        }
        for n in project.registry.names()
    ]
    return {
        "processors": processors,
        "components": components,
        "extension_errors": list(getattr(project, "extension_errors", []) or []),
    }


def _short_tb() -> str:
    # Keep tracebacks compact; users can re-run with HOPEWELL_DEBUG for full.
    import os
    if os.environ.get("HOPEWELL_DEBUG"):
        return traceback.format_exc()
    lines = traceback.format_exc().strip().splitlines()
    return "\n".join(lines[-3:]) if lines else ""
