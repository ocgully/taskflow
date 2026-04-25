# Claude Code hooks integration (HW-0040)

Hopewell can hook into the [Claude Code hook system](https://code.claude.com/docs/en/hooks)
so that **flow events fire automatically** whenever a Claude agent
picks up or finishes work on a Hopewell node. No more manually running
`hopewell flow push`, `flow.enter`, `flow.leave` — the canvas reflects
reality because Claude Code tells Hopewell when an agent starts/stops.

This sits on top of the existing git `post-commit` hook (which
touches/closes nodes on commits that reference `HW-NNNN`). The two are
complementary:

| Trigger             | What fires                                          |
|---------------------|-----------------------------------------------------|
| Git post-commit     | `hopewell touch` / `close` on referenced nodes      |
| Claude Code hooks   | `flow.enter` / `flow.leave` / (maybe) `flow.push`   |

## Event mapping

| Claude Code event     | Hopewell action                                                                                               |
|-----------------------|---------------------------------------------------------------------------------------------------------------|
| `SessionStart`        | Write session id to `.hopewell/claude/active.json`. **No flow event.**                                        |
| `UserPromptSubmit`    | Scan the user prompt for `HW-NNNN` refs and stash them on the active marker's `pending_nodes` queue.          |
| `PreToolUse` (Task / Agent / SubagentStart) | For every HW-id found in `tool_input.{prompt,description}`, branch name, or `pending_nodes`: **emit `flow.enter`** on `<node>` for the resolved executor. Record the opened location on the marker. |
| `PostToolUse` (Task / Agent) | **Emit `flow.leave`** on every location opened by the matching `PreToolUse` (paired by `tool_use_id`). |
| `Stop`                | **Emit `flow.leave`** on every remaining open location for this session. Clear `pending_nodes`.               |
| `SubagentStop`        | Same as `Stop`.                                                                                               |
| `SessionEnd`          | `flow.leave` any remaining open locations, then delete `.hopewell/claude/active.json` entirely.               |
| All other events      | Silent no-op.                                                                                                 |

### How the session knows which HW-ids it's working on

A hybrid approach, in precedence order:

1. **Active marker file** — `.hopewell/claude/active.json`. Persistently
   records the ids and opened locations for the current Claude Code
   session. Written by `PreToolUse` and `UserPromptSubmit`; cleared by
   `SessionEnd` (and partially by `Stop`).
2. **Regex over structured hook fields** — `HW-\d+` is scanned in
   `tool_input.prompt`, `tool_input.description`, `tool_input.command`,
   `prompt` (UserPromptSubmit), and the current git branch name
   (`feat/HW-NNNN-*`).
3. **`HOPEWELL_NODES` env var** — comma-separated fallback, e.g.
   `HOPEWELL_NODES=HW-0040,HW-0041 claude` kicks a session that already
   knows its scope.

The active marker wins, regex is the fallback, the env var is last.
If none match, the hook is a silent no-op (zero flow events emitted).

### How the executor is identified

Precedence:

1. `HOPEWELL_ACTOR` env var (explicit beats heuristic — always wins).
2. `@<persona>` mention parsed out of the tool prompt (`"Work on
   HW-0040 as @engineer"` → `engineer`).
3. `executor` field on the active marker (set via a slash command, or
   `HOPEWELL_ACTOR` at session start).
4. `core.default_executor` in `.hopewell/config.toml`.
5. Literal fallback `"agent"` — still gated by
   `flow._require_executor`, so unknown executors become silent no-ops.

## Installation

One-time per machine:

```bash
hopewell hooks install --claude-code
```

This writes to `~/.claude/settings.json` (user scope) by default. Use
`--scope project` for per-project installation (`./.claude/settings.json`)
or `--settings-path PATH` for anywhere else (useful for testing /
CI / devcontainers).

Dry-run to preview:

```bash
hopewell hooks install --claude-code --dry-run
```

Uninstall cleanly (Hopewell-installed entries only — other hooks are
left untouched):

```bash
hopewell hooks uninstall --claude-code
```

### What gets written

After `hooks install --claude-code`, `settings.json` contains a
`hooks` section that looks roughly like:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [
          { "type": "command",
            "command": "HOPEWELL_EVENT=session-start python -m hopewell.claude_hooks_cli dispatch session-start  # hopewell:managed",
            "timeout": 10 }
      ] }
    ],
    "PreToolUse": [
      { "matcher": "Task|Agent",
        "hooks": [
          { "type": "command",
            "command": "HOPEWELL_EVENT=pre-tool-use python -m hopewell.claude_hooks_cli dispatch pre-tool-use  # hopewell:managed",
            "timeout": 10 }
      ] }
    ],
    "PostToolUse": [ ... ],
    "Stop":        [ ... ],
    "SubagentStop":[ ... ],
    "SessionEnd":  [ ... ],
    "UserPromptSubmit": [ ... ]
  }
}
```

The `# hopewell:managed` marker at the end of every command string is
how `hopewell hooks uninstall --claude-code` recognizes its own
entries — third-party hooks in the same file are left intact.

## Rules of engagement

Hooks MUST be fast and MUST NOT block. Therefore:

- Every handler **swallows every exception** and exits **0**. The worst
  case is a missed flow event; Claude Code never gets a hook error.
- If `.hopewell/` is not found (cwd not in a Hopewell project), the
  handler silently no-ops.
- If the hook input is malformed JSON, the handler silently no-ops.
- If the resolved executor is unknown to the network, the underlying
  `flow.enter` raises but the caller catches it — still a no-op.

This is by design: Hopewell is additive telemetry over the Claude Code
agent loop, never a gate on it.

## Debugging

Run a handler by hand to see what it would do:

```bash
echo '{"hook_event_name":"PreToolUse","tool_name":"Task","tool_input":{"description":"Work on HW-0040","prompt":"Implement HW-0040 as @engineer"}}' \
  | python -m hopewell.claude_hooks_cli dispatch pre-tool-use
```

Then check the events log + location:

```bash
hopewell flow where HW-0040 --history
tail .hopewell/events.jsonl
cat .hopewell/claude/active.json
```

Set `HOPEWELL_ACTOR=engineer` in your shell to force the executor
identity regardless of prompt content (useful when you always play one
persona in a given terminal).
