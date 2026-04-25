"""CLI handlers + settings.json installer for Claude Code hooks (HW-0040).

Kept in its own module so `hopewell/cli.py` isn't touched in this
ticket. Wiring for `hopewell/cli.py` is documented at the bottom of
this docstring; Christopher (or follow-up work) will drop it into
`_build_parser`.

This module also provides its own `python -m hopewell.claude_hooks_cli`
entry point (see `main()` at the bottom) so the hooks work even before
`cli.py` is extended.

--------------------------------------------------------------------
Command surface
--------------------------------------------------------------------

  hopewell claude-hooks <event>
      Where <event> is one of:
          session-start | session-end | user-prompt-submit |
          pre-tool-use | post-tool-use | stop | subagent-stop
      Reads the hook JSON from stdin, executes the corresponding
      handler in `hopewell.claude_hooks`, and always exits 0.

  taskflow hooks install --claude-code [--dry-run]
                                        [--settings-path PATH]
                                        [--scope user|project]
      Registers the hook commands in Claude Code's `settings.json`.
      Defaults to `~/.claude/settings.json` (user scope). Use
      `--scope project` to write to `./.claude/settings.json`.
      Use `--dry-run` to print the resulting JSON without writing.
      Use `--settings-path` to write to an arbitrary path (testing).

  taskflow hooks uninstall --claude-code [--settings-path PATH]
                                          [--scope user|project]
      Removes the Hopewell hook registrations (identified by command
      signature) from Claude Code's settings.json. Leaves unrelated
      hooks untouched. Writes back the trimmed settings file.

--------------------------------------------------------------------
Suggested wiring for `cli.py::_build_parser` (drop in verbatim):
--------------------------------------------------------------------

    from taskflow import claude_hooks_cli as ch_cli_mod

    # --- 'claude-hooks' top-level subparser ---
    sp = sub.add_parser("claude-hooks",
                         help=argparse.SUPPRESS)  # internal
    sp.add_argument("event",
                    choices=list(ch_cli_mod.EVENT_CHOICES))
    sp.set_defaults(func=ch_cli_mod.cmd_claude_hooks_dispatch)

    # --- extend the existing 'hooks install' subcommand ---
    # (the existing `hooks` parser lives in cli.py; add these flags)
    hooks_parser.add_argument("--claude-code", action="store_true",
                              help="Install Claude Code hooks too")
    hooks_parser.add_argument("--dry-run", action="store_true")
    hooks_parser.add_argument("--settings-path", default=None)
    hooks_parser.add_argument("--scope",
                              choices=["user", "project"],
                              default="user")
    # then in cmd_hooks, if args.claude_code: call
    #     ch_cli_mod.cmd_install_claude_code(args)
    # (for install) / cmd_uninstall_claude_code (for uninstall).

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from taskflow import claude_hooks as ch_mod


# ---------------------------------------------------------------------------
# shared constants
# ---------------------------------------------------------------------------

EVENT_CHOICES: Tuple[str, ...] = tuple(ch_mod.DISPATCH.keys())

# Marker placed in every hook command we install — used for
# round-trip uninstall / re-install without touching other hooks.
HOOK_MARKER = "# hopewell:managed"

# Ordered list of (ClaudeCodeEvent, hopewell-handler-name, matcher).
# Matcher `None` means "no matcher field" (i.e. match everything that
# fires this event).
REGISTRATIONS: List[Tuple[str, str, Optional[str]]] = [
    ("SessionStart",      "session-start",       None),
    ("SessionEnd",        "session-end",         None),
    ("UserPromptSubmit",  "user-prompt-submit",  None),
    ("PreToolUse",        "pre-tool-use",        "Task|Agent"),
    ("PostToolUse",       "post-tool-use",       "Task|Agent"),
    ("Stop",              "stop",                None),
    ("SubagentStop",      "subagent-stop",       None),
]


# ---------------------------------------------------------------------------
# dispatch entry (`hopewell claude-hooks <event>`)
# ---------------------------------------------------------------------------


def cmd_claude_hooks_dispatch(args) -> int:
    """Handler for `hopewell claude-hooks <event>` — reads stdin JSON,
    runs the matching hook, always returns 0 (fail-silent)."""
    event = getattr(args, "event", None)
    if event is None:
        return 0
    return ch_mod.dispatch(event)


# ---------------------------------------------------------------------------
# settings.json installer
# ---------------------------------------------------------------------------


def _default_settings_path(scope: str) -> Path:
    if scope == "project":
        return Path.cwd() / ".claude" / "settings.json"
    home = Path(os.environ.get("HOME") or os.path.expanduser("~"))
    return home / ".claude" / "settings.json"


def _resolve_settings_path(args) -> Path:
    override = getattr(args, "settings_path", None)
    if override:
        return Path(override).resolve()
    scope = getattr(args, "scope", "user") or "user"
    return _default_settings_path(scope).resolve()


def _hopewell_invocation() -> str:
    """Return a shell-quoted command that runs our dispatch entry."""
    # Prefer the installed console script if on PATH; otherwise fall
    # back to `python -m hopewell.claude_hooks_cli` so it works in
    # every dev environment.
    return (
        'hopewell-claude-hook "$HOPEWELL_EVENT" 2>/dev/null '
        '|| python -m hopewell.claude_hooks_cli dispatch "$HOPEWELL_EVENT" '
        '2>/dev/null || true'
    )


def _build_command(event_name: str) -> str:
    """Build the `command` string for a Claude Code hook entry.

    The command sets HOPEWELL_EVENT (our short name) then invokes the
    dispatcher. We swallow errors so Claude Code never blocks.
    """
    _ = _hopewell_invocation()  # documents the shape; kept for readability
    return (
        f"HOPEWELL_EVENT={event_name} python -m hopewell.claude_hooks_cli "
        f"dispatch {event_name}  {HOOK_MARKER}"
    )


def _is_hopewell_hook_entry(entry: Dict[str, Any]) -> bool:
    """Identify a hook command we installed (via the marker)."""
    if not isinstance(entry, dict):
        return False
    if entry.get("type") != "command":
        return False
    cmd = entry.get("command") or ""
    return HOOK_MARKER in cmd


def build_hooks_section(existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build/merge the `hooks` section of Claude Code settings.json.

    Preserves any non-hopewell hook entries. Fully replaces our own
    entries (identified by `HOOK_MARKER`).
    """
    hooks = dict(existing or {})
    for event_name, short_name, matcher in REGISTRATIONS:
        # Per Claude Code docs each event maps to a LIST of
        # {matcher, hooks: [...]} groups. We:
        #   1. Drop groups that contain only hopewell entries
        #   2. Strip hopewell entries from mixed groups
        #   3. Append a fresh group for our registration
        groups: List[Dict[str, Any]] = list(hooks.get(event_name) or [])
        cleaned: List[Dict[str, Any]] = []
        for g in groups:
            if not isinstance(g, dict):
                cleaned.append(g)
                continue
            inner = g.get("hooks") or []
            kept = [h for h in inner if not _is_hopewell_hook_entry(h)]
            if kept:
                new_g = dict(g)
                new_g["hooks"] = kept
                cleaned.append(new_g)
            # else: group was only hopewell's — drop it entirely

        entry: Dict[str, Any] = {
            "type": "command",
            "command": _build_command(short_name),
            "timeout": 10,
        }
        group: Dict[str, Any] = {"hooks": [entry]}
        if matcher:
            group["matcher"] = matcher
        cleaned.append(group)
        hooks[event_name] = cleaned
    return hooks


def install_claude_code_settings(
    settings_path: Path,
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Install Hopewell hooks into the given settings.json file.

    Returns the full settings dict that would be (or was) written.
    Creates parent directories on real writes.
    """
    existing: Dict[str, Any] = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}

    hooks_section = build_hooks_section(existing.get("hooks"))
    merged = dict(existing)
    merged["hooks"] = hooks_section

    if not dry_run:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return merged


def uninstall_claude_code_settings(settings_path: Path) -> bool:
    """Strip Hopewell-installed hook entries from settings.json.

    Returns True if any entries were removed.
    """
    if not settings_path.is_file():
        return False
    try:
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(existing, dict):
        return False
    hooks = existing.get("hooks") or {}
    if not isinstance(hooks, dict):
        return False

    changed = False
    new_hooks: Dict[str, Any] = {}
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            new_hooks[event_name] = groups
            continue
        new_groups: List[Dict[str, Any]] = []
        for g in groups:
            if not isinstance(g, dict):
                new_groups.append(g)
                continue
            inner = g.get("hooks") or []
            kept = [h for h in inner if not _is_hopewell_hook_entry(h)]
            if len(kept) != len(inner):
                changed = True
            if kept:
                new_g = dict(g)
                new_g["hooks"] = kept
                new_groups.append(new_g)
            # empty group -> dropped (and changed=True already set)
        if new_groups:
            new_hooks[event_name] = new_groups
    existing["hooks"] = new_hooks

    if changed:
        settings_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return changed


# ---------------------------------------------------------------------------
# CLI-style install/uninstall handlers (to be called from cli.py later)
# ---------------------------------------------------------------------------


def cmd_install_claude_code(args) -> int:
    settings_path = _resolve_settings_path(args)
    dry_run = bool(getattr(args, "dry_run", False))
    merged = install_claude_code_settings(settings_path, dry_run=dry_run)
    if dry_run:
        sys.stdout.write(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        return 0
    if not getattr(args, "quiet", False):
        sys.stdout.write(f"Installed Claude Code hooks -> {settings_path}\n")
    return 0


def cmd_uninstall_claude_code(args) -> int:
    settings_path = _resolve_settings_path(args)
    changed = uninstall_claude_code_settings(settings_path)
    if not getattr(args, "quiet", False):
        if changed:
            sys.stdout.write(f"Removed Hopewell hooks from {settings_path}\n")
        else:
            sys.stdout.write(f"No Hopewell hooks found in {settings_path}\n")
    return 0


# ---------------------------------------------------------------------------
# standalone entry point: `python -m hopewell.claude_hooks_cli ...`
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hopewell.claude_hooks_cli",
        description="Claude Code hook dispatcher + settings.json installer (HW-0040)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    dp = sub.add_parser("dispatch", help="Run a hook handler (reads stdin JSON)")
    dp.add_argument("event", choices=list(EVENT_CHOICES))
    dp.set_defaults(func=cmd_claude_hooks_dispatch)

    ip = sub.add_parser("install", help="Install hooks into Claude Code settings.json")
    ip.add_argument("--dry-run", action="store_true")
    ip.add_argument("--settings-path", default=None)
    ip.add_argument("--scope", choices=["user", "project"], default="user")
    ip.add_argument("--quiet", action="store_true")
    ip.set_defaults(func=cmd_install_claude_code)

    up = sub.add_parser("uninstall", help="Remove Hopewell hooks from settings.json")
    up.add_argument("--settings-path", default=None)
    up.add_argument("--scope", choices=["user", "project"], default="user")
    up.add_argument("--quiet", action="store_true")
    up.set_defaults(func=cmd_uninstall_claude_code)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except Exception:
        # dispatch must always return 0; install/uninstall surface as 1
        if getattr(args, "cmd", None) == "dispatch":
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
