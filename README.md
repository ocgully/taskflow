# Hopewell

**Flow-framework tool for AI-agent-driven work.** Composition-over-typing
node graph, parallel scheduler, orchestrator, GitHub-issues ingestion, and
(soon) a web UI. Tickets and work items are the same thing here: typed
components riding a DAG that agents can execute.

Named after a ship in *Gulliver's Travels*. Work *sails* through a network
of ports (nodes), carrying cargo (artifacts).

> **Status: v0.5.** CLI + Python library + basic orchestrator + GitHub
> ingestion + attestation ledger + agent fingerprinting + **coordination
> (branch-as-claim + JSONL merge driver)**. Web UI and LLM-driven graph
> evolution still pending. See **Roadmap** below.

---

## Why this exists

Existing trackers (Jira, Linear, GitHub Projects) are built for humans
clicking. Existing markdown-in-git trackers (`tk`, Backlog.md) are built
for one project, one list. **Hopewell is built for agents and humans to
share the same graph** — every work item is a typed node; every dependency
is an edge; the orchestrator runs ready nodes; the LLM can evolve the graph.

Three principles make it different:

1. **Composition over typing.** A node IS the components it HAS.
   `feature = {work-item, deliverable, user-facing}`. Projects add custom
   components (e.g., `playtest-feedback`) without forking.
2. **Graph-first, not list-first.** The backlog is a DAG. Parallel waves
   are the default; serialization happens when edges force it.
3. **Humans read markdown, agents query the CLI.** `.hopewell/` is
   Claude-ignored. Agents go through `hopewell query ...`; never grep.

## What Hopewell is — and isn't

Hopewell is the **ledger**, the **map**, and the **viewport** — not the
executor.

- **Ledger.** Records nodes, edges, events, claims, attestations.
  Append-only, merge-safe, the one source of truth agents and humans
  agree on.
- **Map.** Authors the flow network — the executor topology that defines
  where work routes, which gates apply, what "done" means. Agents
  consult this map to know where to push.
- **Viewport.** Projects the ledger through the map — ready queues, flow
  inboxes, traversals, cycle time, rework ratios, drift alerts. Makes
  both the state and the shape of work visible.

What it **is not**: an executor. Hopewell does not spawn agents, hand
off tokens, schedule compute, or coordinate worktrees. Execution lives
in the agent runtime (Claude Code, agent marketplaces, bundle scripts).
Hopewell is the contract and the ledger they agree on.

---

## Install

```bash
pip install hopewell
# or from source:
git clone https://github.com/ocgully/Hopewell
pip install -e hopewell/

# Optional extras:
pip install 'hopewell[web]'      # FastAPI + SSE (v0.6)
pip install 'hopewell[github]'   # uses `requests` for cleaner HTTP errors
pip install 'hopewell[full]'     # everything
```

Python 3.10+.

---

## Quick start

```bash
cd my-project/
hopewell init                    # scaffolds .hopewell/ + .claudeignore

# Create some nodes
hopewell new --components work-item,deliverable,user-facing \
             --title "Implement login flow" --owner @alice

# List what you've got
hopewell list
hopewell ready                   # just the actionable ones

# Link dependencies
hopewell link HW-0001 blocks HW-0002
hopewell link HW-0002 consumes design/login.figma --from HW-0001

# Check health
hopewell check                   # cycles, dangling refs, schema
hopewell query waves             # who can work in parallel

# Close with evidence
hopewell close HW-0001 --commit abc123 --reason "tests pass, shipped"

# Keep it fresh on every commit (minimal: post-commit only)
hopewell hooks install

# Or install the full gate set (pre-commit + post-commit + pre-push — HW-0050)
hopewell hooks install --full
```

## Version compatibility — multiple agents on different Hopewell versions

Hopewell is a local package, so in a multi-agent / multi-machine setup
nothing guarantees every agent is on the same version. Three mechanisms
keep version-skew from silently corrupting data.

### 1. `.hopewell/meta.json` — the version contract

Every project has one, written at `init` and refreshed at `migrate`:

```json
{
  "hopewell_schema": "1",
  "hopewell_version_last_setup": "0.5.2",
  "created_at": "…",
  "last_migrated_at": "…"
}
```

On every `Project.load()`:
- If `hopewell_schema` > the running package's schema → **refuse**
  with `"this .hopewell/ uses schema N, but this Hopewell understands up
  to schema M. Upgrade via `pip install -U hopewell` and retry."`
- If `hopewell_schema` < the running package's schema → **refuse** with
  `"run `hopewell migrate` to upgrade the project files."`
- If schemas match → proceed.

If `meta.json` is missing for any reason (deleted by hand, filesystem
glitch), the next `Project.load()` auto-writes one stamped with the
current package schema + version. Defense-in-depth, never the primary
path.

### 2. `[coordination] minimum_version` — the floor

Repos can pin a minimum Hopewell version in `.hopewell/config.toml`:

```toml
[coordination]
minimum_version = "0.5.2"
```

Any Hopewell below that floor refuses to act with:
`"this project pins minimum_version = '0.5.2' but this Hopewell is
v0.5.0. Run `pip install -U hopewell>=0.5.2` and retry."`

Use this when the team has committed to a feature set available
only in a newer version — prevents an out-of-date agent from writing
data the team can't consume.

### 3. Preserve-unknown round-trip

When an older Hopewell reads a node file written by a newer version,
fields it doesn't recognise are captured into a private `extras` dict
and re-emitted verbatim on save. An older agent editing a newer-format
node loses nothing.

Same principle for event-log JSONL records — unknown fields pass
through untouched.

### Policy

- **Breaking changes bump the schema version.** Schema bumps are rare
  and always ship alongside an upgrade path in `hopewell migrate`.
- **Most releases are additive** — v0.1 through v0.5.2 all stayed on
  schema 1 because every change was forward-compatible.
- **Pin your team.** Add `minimum_version` to `config.toml` when you
  adopt a feature that matters to the team; add `hopewell>=X.Y.Z`
  to CI's lockfile. Informal version drift between team members is
  where most pain comes from.

## Upgrading — `hopewell migrate`

After upgrading the `hopewell` package (new version brings new
project-level setup — `.gitattributes` entries, CLAUDE.md rules,
config sections), run once in each project that already has `.hopewell/`:

```bash
pip install -U hopewell        # or: pip install -e <path/to/local/clone>
cd <your-project>
hopewell migrate               # idempotent; safe to re-run any time
```

It re-applies every idempotent step `hopewell init` performs: refresh
the `.gitattributes` block + git-config merge driver, top-up the
root-level `.claudeignore`, append the "do not read `.hopewell/`" block
to `CLAUDE.md` if missing. No events or nodes are touched except for an
audit `project.migrate` entry in `events.jsonl`.

`hopewell init` on an existing `.hopewell/` is also idempotent — running
it won't add a duplicate `project.init` event — but `migrate` is the
named command for the intent.

## UAT — user-acceptance testing

Internal tests passing isn't always enough. Some work needs a human to
verify against acceptance criteria before it's truly shipped. The
`needs-uat` component flags those nodes; Hopewell tracks pending /
passed / failed / waived outcomes separately from the node's primary
status.

```bash
# Flag at creation or retroactively
hopewell uat flag HW-0042
hopewell uat flag HW-0042 --criteria "handles 1000 entities" \
                          --criteria "no frame drops on VR target"

# List what's pending (default) — emits pass/fail/waive commands inline
hopewell uat list
hopewell uat list --status failed
hopewell uat list --status all

# Record outcomes
hopewell uat pass  HW-0042 --notes "verified on Quest 3, 60fps stable"
hopewell uat fail  HW-0042 --reason "dropped frames under 50 entities; needs batching"
hopewell uat waive HW-0042 --reason "internal tooling — developer-only, no end-user impact"

# Show one node's UAT state
hopewell uat show HW-0042

# Remove UAT flag entirely (rare — for 'never actually needed UAT' cases)
hopewell uat unflag HW-0042 --reason "..."
```

### Retroactive backfill

If a project realises after-the-fact that UAT tracking was missing (the
common Gulliver-style scenario: every done node got shipped with no
explicit verification), `hopewell uat backfill` adds `needs-uat=pending`
to every node matching a filter:

```bash
# Every done node gets flagged as needing UAT
hopewell uat backfill --status done

# Only done nodes with the `user-facing` component (skip internal tooling)
hopewell uat backfill --status done --has-all user-facing

# Since a specific date
hopewell uat backfill --status done --since 2026-04-01

# Preview before touching anything
hopewell uat backfill --status done --dry-run
```

Backfill never overrides an explicit UAT decision — nodes already
carrying `needs-uat` (even with status=waived) are left alone.

### Views

`.hopewell/views/UAT.md` regenerates on every `hopewell render`. Four
sections: pending / failed / passed / waived, each listing nodes with
their acceptance criteria as checkbox bullets + the exact CLI commands
to mark outcomes. Human-browsable; also great for a review session
where you walk a list item-by-item.

### State-machine semantics

UAT status is **orthogonal** to node status. A node can be `done` with
UAT `pending` — that's the "shipped internally but not yet verified
with the end user" state. The `done` state machine transition is
unchanged; UAT is a separate axis. A `uat fail` outcome does NOT
auto-reopen the node (that's a follow-up decision for the owner —
reopen via `hopewell set-status HW-NNNN doing`, or waive with a
rationale, or ticket the fix as a new node).

## Session resume — picking up mid-work

Agents and humans regularly leave work mid-stream: a Claude Code
session ends, a developer stops for the day, a CI job wraps. Hopewell's
session-resume protocol is:

**At session start**:
```bash
hopewell resume
```
Returns the active claims you hold, nodes in `doing`/`review` you
own, the latest `[next]` checkpoint on each, and a suggested
`git switch <branch>` command. Zero guesswork about where to pick up.

**Before stopping mid-work**:
```bash
hopewell checkpoint HW-0042 --next "finish the scheduler tests; the retry path still fails"
```
Appends a `[next]`-prefixed note to the node. Your next session's
`hopewell resume` surfaces that line as the suggested next action on
that claim.

**Session end (work complete)**:
```bash
hopewell close HW-0042 --commit <sha> --reason "..."
```
Or just let the post-commit hook close it via a commit message like
`fixes HW-0042`. Either releases the claim.

### Resume output

```
=== resume for @christopher ===

--- active claims (2) ---
  HW-0014    [doing ] branch=hopewell/HW-0014
    title: Hopewell v0.6 LLM-driven graph evolution
    next:  scaffold evolve.py; wire add-node first, then add-loop
    -> git switch hopewell/HW-0014
  HW-0042    [review] branch=hopewell/HW-0042-scheduler
    title: Implement ECS scheduler v2
    next:  docs only; kick to technical-writer after tests green

--- doing (2) ---
  HW-0014    P2  Hopewell v0.6 LLM-driven graph evolution
    next: scaffold evolve.py; wire add-node first, then add-loop
  HW-0042    P2  Implement ECS scheduler v2

--- ready to pick up (5) ---
  HW-0010    P2  Codemap TypeScript Layer 1
  HW-0011    P3  Codemap Layer 4: assets and user-facing strings
  ...
```

`hopewell resume @alice` shows @alice's state (useful for handoffs).
`hopewell resume --all` shows every active claim across the project
regardless of claimer.

### Protocol (for agents + humans)

1. **First action of any session**: `hopewell resume`. Don't reconstruct
   state from `git log` or note timestamps — use the tool.
2. **Before leaving a node you're not closing**: `hopewell checkpoint
   <id> --next "..."`. The whole point is that *future you*
   (or a different agent) doesn't have to re-read the whole file to
   find out where the work stopped.
3. **Close via CLI or commit message**. `hopewell close` emits an
   attestation with the closing commit sha; `fixes HW-NNNN` in a
   commit message triggers the same via the post-commit hook.

## Coordination — multiple agents & humans in the same repo

Hopewell coordinates concurrent work with **pure git** — no server, no
central mediator required. The design rests on two primitives:

1. **Branch-as-claim** — to start work, push `hopewell/<node-id>`. Git's
   non-fast-forward rejection IS the mutex: first pusher wins. Second
   agent sees a collision and picks another ready node.
2. **JSONL merge driver** — `.hopewell/events.jsonl` and siblings are
   append-only; a shipped merge driver unions + timestamp-sorts them so
   concurrent branches merge cleanly. Wired automatically at
   `hopewell init`.

```bash
# Agent A
hopewell claim HW-0042                          # pushes hopewell/HW-0042; OK, I own it.
# … work, commits, eventually …
hopewell close HW-0042 --commit abc123
# After PR merge, clean up:
hopewell release HW-0042                        # deletes the branch remote+local

# Agent B, same repo, parallel session
hopewell ready                                  # HW-0042 filtered out (claimed)
hopewell claim HW-0050                          # different node, no collision

# Agent C tries HW-0042 too:
hopewell claim HW-0042
# { "claim": "collision", "branch": "hopewell/HW-0042",
#   "existing": {"claimer": "@alice", "pushed_at": "…", "age_hours": 0.5},
#   "hint": "Pick another ready task or ask the claimer to release." }
```

### Race-condition matrix

| Race | Layer that handles it | Result |
|------|-----------------------|--------|
| Two agents create the same task | n/a — each gets a unique node id | No collision |
| Two agents grab the same ready task | `hopewell claim` atomic git push | First wins; second gets `ClaimCollision` with the existing claimer's info |
| Two agents both close to main | Normal git merge rules | Standard git dance |
| Claim abandoned (agent dies mid-work) | `hopewell prune-claims --stale-days N` | Sweeps branches whose last commit is > N days old |
| Offline work | `hopewell claim HW-0042 --offline` | Writes a local claim event without pushing; sync-on-reconnect is operator-driven |
| Private repo, solo developer | Works unchanged | `hopewell ready` + `claim` + `release` all local; the branch still reserves the name remotely if you push |
| Concurrent appends to `events.jsonl` on two branches | JSONL merge driver + `.gitattributes` | Auto-merges; ordered by `ts`; dedupes identical lines |
| Two processes run `orch run` on the same tree | Local-only danger; one-liner you-probably-won't-hit | Advisory lock in `.hopewell/orchestrator/` (in progress — see roadmap) |
| Slug variants (A claims `HW-0042-foo`, B tries plain `HW-0042`) | Claim check matches `hopewell/HW-0042[-*]` before push | B sees collision on A's slugged branch |

### Claim lifecycle

```
idea/ready ── hopewell claim ──► doing       (branch hopewell/<id> pushed)
                               │
         hopewell close ◄──────┴──► hopewell release  (branch deleted)
                                          │
                               merged PR  ─┴─► clean state
```

- `hopewell claim <id>` creates + pushes `hopewell/<id>`. Fails atomically on collision.
- `hopewell claim <id> --offline` writes a local claim event; skip the push. Useful disconnected or for solo work that'll never push.
- `hopewell claim <id> --slug <word>` appends a readable slug — `hopewell/<id>-<slug>`. Still collides with un-slugged variants of the same node.
- `hopewell release <id>` deletes every `hopewell/<id>[-*]` branch (local + remote).
- `hopewell query claims [<id>]` lists every active claim (remote branches + unreleased local events), with claimer + last-commit age.
- `hopewell prune-claims --stale-days 14` deletes abandoned claim branches.

### Why not Jira / GitHub Issues as mediator?

Considered and rejected for coordination *per se* — they're **UI for
humans**, not coordination primitives. Branch-push gives you the same
mutex with no SaaS dependency, full offline capability, and automatic
cleanup when the PR merges. (Hopewell still has one-way **ingestion**
from GitHub Issues — see next section — for the case where tasks
originate outside the team. That's orthogonal to coordination.)

### Policy for teams adopting Hopewell

1. **Rule**: before starting any work, run `hopewell claim <id>`. If it succeeds, you have the branch. If it collides, pick another task or coordinate with the current claimer through your normal team channels.
2. **Rule**: `hopewell ready` is the canonical "what can I pick up" query. It filters out nodes with active claims by default.
3. **Rule**: close nodes through the CLI (`hopewell close`) or via a commit message reference (`fixes HW-0042`). The post-commit hook + your PR merge handle the rest.
4. **Rule**: release (or let auto-prune sweep) stale claims promptly. A lingering claim is lock pollution.

---

## Git hooks — mechanical bookkeeping + declared gates (HW-0050)

Hopewell ships three git hooks, installed into `.git/hooks/` on demand.
Pick the profile that matches how strictly you want Hopewell to gate
day-to-day git operations:

```bash
# Minimal — post-commit only. Emits flow events + closes nodes on
# 'fixes HW-NNNN'. Never blocks. Safe default for casual use.
hopewell hooks install

# Full — adds the pre-commit + pre-push gates on top of post-commit.
# Recommended for projects where every commit should reference a work
# item and trunk is protected by release-readiness.
hopewell hooks install --full

# Inspect what's installed
hopewell hooks status

# Simulate the release-readiness gate (dry-run)
hopewell hooks test-pre-push [--branch main]

# Remove all Hopewell-managed hook blocks
hopewell hooks uninstall
```

### What each hook does

| Hook | Category | Blocks when |
|------|----------|-------------|
| `post-commit`  | A — bookkeeping  | Never (always exits 0). Parses `HW-NNNN` from the commit message, touches affected nodes, emits flow events, closes nodes on `fixes/closes HW-NNNN`. |
| `commit-msg`   | B — declared gate | Commit message lacks any `HW-NNNN` reference. (Runs AFTER `pre-commit` so both `-m "..."` and editor-authored messages are covered.) |
| `pre-commit`   | B — declared gate | Spec-refs are drifted and no active reconciliation review covers them. |
| `pre-push`     | B — declared gate | Pushing to `main` / `master` / `trunk` and any in-progress release node scores below its threshold. Non-trunk pushes are never gated. |

### Bypass

Every gate respects the same environment variable. Use sparingly; it's
for genuinely exceptional commits (WIP branches, doc-only tweaks, bulk
refactor merges) — your team should have a shared norm about when it's
appropriate.

```bash
HOPEWELL_SKIP_HOOKS=1 git commit -m "wip: no ticket yet"
HOPEWELL_SKIP_HOOKS=1 git push origin main
```

Per-gate overrides also exist for CI / automation scripts:

```bash
HOPEWELL_GATE_SKIP_HW_REF=1   # skip just the hw-ref gate
HOPEWELL_GATE_SKIP_DRIFT=1    # skip just the drift gate
HOPEWELL_GATE_SKIP_RELEASE=1  # skip just the release-readiness gate
```

### Why git hooks for A + B, and Claude hooks for C

* **Category A (bookkeeping)** — recording that a commit references HW-0042
  is pure mechanism. Git hooks do it reliably, whether you're in Claude
  Code, a terminal, an IDE, or running git from a CI job.
* **Category B (declared gates)** — drift + release-readiness are
  declarative invariants; they should bind the git operation itself, not
  just AI sessions.
* **Category C (context injection — Pedia, resume, spec slices)** — this
  requires knowing that an AI agent is running. Git hooks can't do it.
  See `hopewell claude-hooks install` (HW-0040) for the Claude Code
  hooks that cover Category C.

See also: [Hooks vs. orchestrator — scope analysis](https://github.com/ocgully/AgentFactory/blob/main/patterns/drafts/hooks-vs-orchestrator.md)
which defines categories A through E and justifies why routing +
judgment stay with the orchestrator.

### Visualising which routes hooks cover

When a flow-network route is fully enforced by a git hook (e.g.
`code-review -> release` covered by `pre-push`'s release-score check),
annotate it so the web canvas renders it with a distinct style:

```bash
hopewell network annotate-auto-enforced           # dry-run
hopewell network annotate-auto-enforced --apply   # persist
```

Annotated routes show up in the canvas as dashed, desaturated edges that
preserve their source hue. Humans see "this edge is hook-driven, not
orchestrator-driven" at a glance; the orchestrator doesn't need to
think about those routes because Hopewell already enforces them.

---

## GitHub ingestion

One-way sync: GitHub issues → Hopewell nodes. Used for cases where tasks
**originate outside the team** (customer bug reports, community
feature requests). Coordination itself is handled by branch-as-claim
above; this is strictly for ingesting external task sources.

```toml
# .hopewell/config.toml
[github]
repo = "ocgully/my-project"
default_components = ["work-item"]
sync_interval_minutes = 60

[github.label_to_components]
bug = "defect"
feature = "user-facing"
security = "risk"
```

Then:

```bash
export GITHUB_TOKEN=ghp_xxxxx
hopewell github sync                       # incremental
hopewell github pull ocgully/foo#42        # one-shot
```

Issues with `closed` state become nodes with `done` status. Re-opened
issues transition back to `doing`. Labels map to components (unknowns
pass silently; still visible in `component_data.github-issue.labels`).

## Orchestrator

```bash
hopewell orch plan               # show wave schedule + critical path
hopewell orch run --dry-run      # preview
hopewell orch run                # execute; ready nodes dispatched to processors
hopewell orch status             # last run summary
```

Built-in processors (v0.3):
- `noop` — marks done. Useful for graph-only nodes.
- `shell-cmd` — runs a command from `component_data.shell-cmd.cmd`.
- `codemap-check` — invokes `codemap check` as a structural gate (needs
  [codemap](https://github.com/ocgully/codemap) installed).

Rich agent-dispatching processors land in v0.4 (they need the attestation
system). Custom Python processors land in v0.7.

---

## Data model (composition-over-typing)

A node is a markdown file with YAML front-matter:

```markdown
---
id: HW-0042
status: ready
priority: P2
owner: "@alice"
components:
  - work-item
  - deliverable
  - user-facing
  - flagged
inputs:
  - from_node: HW-0010
    artifact: specs/042-api/contracts.json
    required: true
outputs:
  - path: src/api/login.ts
    kind: code
blocks: [HW-0050]
blocked_by: [HW-0010]
component_data:
  flagged:
    flag_name: auth.v2
---

# HW-0042: Implement login flow

## Why
...

## Notes (append-only)
- 2026-04-22T19:30Z [@alice]  Started.
```

**18 built-in components**: `work-item`, `deliverable`, `user-facing`,
`internal`, `defect`, `risk`, `debt`, `test`, `documentation`, `screenshot`,
`design`, `code-map`, `grouping`, `deployment-target`, `approval-gate`,
`flagged`, `retriable`, `github-issue`.

Traditional types → component sets:

| Type | Components |
|------|-----------|
| Feature | `work-item, deliverable, user-facing` |
| Bug | `work-item, defect, deliverable` |
| Epic | `grouping, user-facing` |
| Release | `grouping, deployment-target, approval-gate` |
| Test | `work-item, test` |
| ADR | `documentation, design` |
| Imported GH issue | `work-item, github-issue, ...labels` |

Extend by adding YAML to `.hopewell/components/` (v0.7) — no fork needed.

---

## State machine (strict)

```
idea ──► blocked ──► ready ──► doing ──► review ──► done ──► archived
  │         │          │         │          │
  └─────────┴──────────┴─────────┴──────────┴──► cancelled
```

Transitions enforced by the library. Illegal transitions return a typed
error listing what's allowed.

**"Done" semantics depend on the `deployment-target` component**:
- `deployment-target: customer` → done = reached end users
- `deployment-target: internal` → done = merged to main
- No target → `definition_of_done` predicates all green

---

## Hide-from-Claude convention

`hopewell init` writes `.claudeignore` at the project root with
`/.hopewell/` so Claude Code skips the directory during context-gathering.
It also adds a block to your `CLAUDE.md` (if present) telling agents to
use `hopewell query ...` rather than grep.

**For hard enforcement**, add to `.claude/settings.json`:

```json
{
  "permissions": {
    "deny": [
      { "tool": "Read", "path_pattern": "**/.hopewell/**" },
      { "tool": "Grep", "path_pattern": "**/.hopewell/**" },
      { "tool": "Glob", "path_pattern": "**/.hopewell/**" }
    ]
  }
}
```

---

## CLI reference

```
hopewell init [--prefix HW] [--name <name>]
hopewell new --components c1,c2,... --title "..." [--owner @x] [--parent HW-N]
hopewell show <id> [--format text|json]
hopewell list [--status S] [--component C] [--has-all A,B] [--owner @x]
hopewell ready [--owner @x]
hopewell touch <id> --note "..."
hopewell link <from> {blocks|produces|consumes|parent|related} <to> [--artifact <p>]
hopewell close <id> [--commit <sha>] [--reason "..."]
hopewell check
hopewell graph                   # mermaid source
hopewell render                  # regenerate .hopewell/views/*
hopewell info                    # JSON summary

hopewell query ready | deps <id> [--transitive] | waves | critical-path
            | component <name> | metrics [--by component|status|owner] | graph | show <id>
            | attestations [--owner @x] [--fingerprint <hex>] [<node-id>]
                           [--since <ts>] [--att-kind <k>] [--limit N]

hopewell agent register <name> [--doc <path>] [--fingerprint <hex>]
hopewell agent list
hopewell agent fingerprint <name> [--doc <path>]     # optionally re-hash doc
hopewell agent quality <name>                         # per-fingerprint metrics

# v0.5 coordination
hopewell claim <id> [--slug <word>] [--base <branch>] [--offline] [--no-push]
hopewell release <id> [--keep-remote]
hopewell prune-claims [--stale-days 14]
hopewell query claims [<id>]

hopewell orch {plan|run|status} [--dry-run] [--max N]

# v0.5.3 session-resume
hopewell resume [@name] [--all] [--format text|json]
hopewell checkpoint <id> --next "..."

# v0.5.4 UAT tracking
hopewell uat flag <id> [--criteria "..."]
hopewell uat {pass|fail|waive} <id> [--notes "..."] [--reason "..."]
hopewell uat list [--status pending|passed|failed|waived|all]
hopewell uat show <id>
hopewell uat backfill [--status done] [--has-all user-facing,...] [--dry-run]
hopewell uat unflag <id> --reason "..."

hopewell github {sync|pull|config} [ref] [--since <ts>] [--state open|closed|all]

hopewell hooks {install|uninstall|status|test-pre-push} [--full|--minimal] [--claude-code]

hopewell network annotate-auto-enforced [--apply]
```

`hw` is an alias for `hopewell`.

---

## Python library

```python
from hopewell import Project, NodeStatus
from hopewell.query import ready, deps, waves, metrics
from hopewell.orchestrator import Runner
from hopewell.github import sync_from_github

project = Project.load(".")                 # walks up to find .hopewell/

# Query
for n in ready(project)["nodes"]:
    print(n["id"], n["title"])

# Mutate
project.touch("HW-0042", "Reviewed; merging.", actor="@alice")

# Orchestrate
Runner(project).execute(max_parallel=4)

# Sync
sync_from_github(project)

# Attestation + agent fingerprinting (v0.4)
from hopewell.attestation import AgentRegistry, fingerprint, query_attestations
reg = project.agent_registry
reg.register("@alice", doc_path="docs/alice.md",
             current_fp=fingerprint(project.root / "docs/alice.md"))
# Every project.touch/set_status/close/link now emits an attestation
# tagged with agent_id = "@alice@<12-char-fingerprint>".
```

The CLI is argparse over this library — everything the CLI does, your
scripts can do without shelling out.

---

## Roadmap

| Version | Contents |
|---------|----------|
| **v0.1** | Foundations: model, storage, CLI (init/new/show/list/touch/link/close/check/graph/render), library, hooks, .claudeignore |
| **v0.2** | Query + Scheduler: ready/deps/waves/critical-path/metrics |
| **v0.3** | Orchestrator basic + **GitHub ingestion** |
| **v0.4** | Attestation ledger + agent fingerprinting (doc-SHA); `hopewell agent register / list / fingerprint / quality`; `hopewell query attestations`; quality metrics per fingerprint |
| **v0.5** | **Coordination: branch-as-claim (`hopewell claim / release / prune-claims`), collision detection across slug variants, claim-aware `hopewell ready` + `hopewell query claims`, JSONL merge driver + `.gitattributes` installed on init, `[coordination]` config section** |
| v0.6 | LLM-driven graph evolution (`hopewell evolve ...`), loops |
| v0.7 | Web UI (FastAPI + SSE + zero-build tree/canvas/timeline) |
| v0.8 | Custom Python processors + YAML component loader + agent-dispatch processor |
| v0.9 | Replace SpecKit planning artefacts |

---

## Relationship to other tools

- **[codemap](https://github.com/ocgully/codemap)** — structural view of
  code. Hopewell's `code-map` component cites codemap queries
  (e.g., `codemap check`) as acceptance gates.
- **[AgentFactory](https://github.com/ocgully/agentfactory)** — marketplaces
  + patterns + bootstrap. AgentFactory consumes Hopewell; `/bootstrap-from-roadmap`
  installs it as part of onboarding.
- **SpecKit `specs/{NNN}/`** — feature-design artefacts. In v0.8 Hopewell
  nodes with `design` + `documentation` + `grouping` components replace
  the SpecKit directory structure; until then, Hopewell references them.

---

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
