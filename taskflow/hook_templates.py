"""Shell-script templates for the git hooks Hopewell installs (HW-0050).

Kept separate from `hooks.py` (the installer) and `gates.py` (the gate
logic) so a reviewer can see each hook's shell script standalone.

Hook structure
--------------

Every hook starts with a `# hopewell:managed` sentinel comment on the
line immediately after the shebang. The installer uses that sentinel
PLUS the classic `--- hopewell hook (managed; do not edit this block) ---`
BEGIN/END block markers so it can do either:

  * Surgical inline removal when the hook file contains OTHER user code.
  * Whole-file deletion when the hook is purely ours (sentinel present
    AND the file is otherwise empty of user content).

The shell scripts are bash-compatible and target Git Bash on Windows,
macOS, and Linux. They invoke `hopewell gate <name>` (a subcommand the
CLI wrapper adds) to run the gate and interpret its exit code. If
`hopewell` isn't on PATH, the hook falls through to `python -m hopewell`
and, if that also fails, exits 0 (never block on tooling failure).

Bypass
------

Every hook short-circuits to `exit 0` immediately if the environment
variable `HOPEWELL_SKIP_HOOKS=1` is set. This is documented prominently
in the README and the hook scripts themselves.
"""
from __future__ import annotations


SENTINEL = "# hopewell:managed"
MARKER_BEGIN = "# --- hopewell hook (managed; do not edit this block) ---"
MARKER_END = "# --- /hopewell hook ---"


# ---------------------------------------------------------------------------
# post-commit (category A: mechanical bookkeeping)
# ---------------------------------------------------------------------------

POST_COMMIT_BODY = r"""
# Hopewell post-commit: scan last commit for node refs; touch + re-render.
# Emits flow events (touch -> implicit enter/leave; fixes/closes -> node.close).
COMMIT_MSG=$(git log -1 --pretty=%B 2>/dev/null)
COMMIT_SHA=$(git rev-parse HEAD 2>/dev/null)
if [ -n "$COMMIT_MSG" ] && [ -n "$COMMIT_SHA" ]; then
  if command -v hopewell >/dev/null 2>&1; then
    hopewell hook-on-commit --message "$COMMIT_MSG" --commit "$COMMIT_SHA" --quiet 2>/dev/null || true
  elif command -v python >/dev/null 2>&1; then
    python -m hopewell hook-on-commit --message "$COMMIT_MSG" --commit "$COMMIT_SHA" --quiet 2>/dev/null || true
  fi
fi
exit 0
"""


# ---------------------------------------------------------------------------
# pre-commit (category B: declared gates — drift only)
# ---------------------------------------------------------------------------
#
# NOTE: the HW-NNNN reference gate lives in `commit-msg`, not `pre-commit`.
# pre-commit runs BEFORE `-m` messages are persisted to COMMIT_EDITMSG,
# so the gate couldn't read them. commit-msg runs after, with the
# message file passed as $1 — that's where we check references.

PRE_COMMIT_BODY = r"""
# Hopewell pre-commit:
#   reject commits while spec-refs are drifted and uncovered.
# Bypass: HOPEWELL_SKIP_HOOKS=1 git commit ...
if [ "${HOPEWELL_SKIP_HOOKS:-0}" = "1" ]; then
  exit 0
fi

HOPEWELL=""
if command -v hopewell >/dev/null 2>&1; then
  HOPEWELL="hopewell"
elif command -v python >/dev/null 2>&1; then
  HOPEWELL="python -m hopewell"
else
  exit 0
fi

$HOPEWELL gate drift
RC=$?
if [ $RC -ne 0 ] && [ $RC -ne 100 ]; then
  exit $RC
fi

exit 0
"""


# ---------------------------------------------------------------------------
# commit-msg (category B: declared gate — HW-NNNN reference)
# ---------------------------------------------------------------------------

COMMIT_MSG_BODY = r"""
# Hopewell commit-msg: reject commits missing an HW-NNNN reference.
# $1 is the path to the file containing the pending commit message.
# Bypass: HOPEWELL_SKIP_HOOKS=1 git commit ...
if [ "${HOPEWELL_SKIP_HOOKS:-0}" = "1" ]; then
  exit 0
fi

COMMIT_MSG_FILE="$1"
if [ -z "$COMMIT_MSG_FILE" ] || [ ! -f "$COMMIT_MSG_FILE" ]; then
  exit 0
fi

HOPEWELL=""
if command -v hopewell >/dev/null 2>&1; then
  HOPEWELL="hopewell"
elif command -v python >/dev/null 2>&1; then
  HOPEWELL="python -m hopewell"
else
  exit 0
fi

# Feed the message on stdin — avoids shell quoting + multi-line issues.
cat "$COMMIT_MSG_FILE" | $HOPEWELL gate hw-ref --stdin
RC=$?
if [ $RC -ne 0 ] && [ $RC -ne 100 ]; then
  exit $RC
fi
exit 0
"""


# ---------------------------------------------------------------------------
# pre-push (category B: declared gates — release readiness on trunk)
# ---------------------------------------------------------------------------

PRE_PUSH_BODY = r"""
# Hopewell pre-push:
#   on a push to main/master/trunk, block if an in-progress release node
#   scores below its threshold (taskflow release score <version>).
# Bypass: HOPEWELL_SKIP_HOOKS=1 git push ...
if [ "${HOPEWELL_SKIP_HOOKS:-0}" = "1" ]; then
  exit 0
fi

# `git push` feeds pre-push hooks <local_ref> <local_sha> <remote_ref>
# <remote_sha> lines on stdin, one per ref being pushed. We scan for any
# line targeting a trunk branch; if none, we exit 0 immediately.
TRUNK_TOUCHED=0
while read local_ref local_sha remote_ref remote_sha; do
  case "$remote_ref" in
    refs/heads/main|refs/heads/master|refs/heads/trunk)
      TRUNK_TOUCHED=1
      ;;
  esac
done

if [ "$TRUNK_TOUCHED" = "0" ]; then
  exit 0
fi

HOPEWELL=""
if command -v hopewell >/dev/null 2>&1; then
  HOPEWELL="hopewell"
elif command -v python >/dev/null 2>&1; then
  HOPEWELL="python -m hopewell"
else
  exit 0
fi

$HOPEWELL gate release
RC=$?
if [ $RC -ne 0 ] && [ $RC -ne 100 ]; then
  exit $RC
fi

exit 0
"""


# ---------------------------------------------------------------------------
# Hook-body registry
# ---------------------------------------------------------------------------

HOOK_BODIES = {
    "post-commit": POST_COMMIT_BODY,
    "pre-commit":  PRE_COMMIT_BODY,
    "commit-msg":  COMMIT_MSG_BODY,
    "pre-push":    PRE_PUSH_BODY,
}


def render(hook_name: str) -> str:
    """Produce the full hook script for `hook_name` (with shebang + sentinel)."""
    body = HOOK_BODIES[hook_name]
    return (
        "#!/usr/bin/env bash\n"
        f"{SENTINEL}\n"
        f"{MARKER_BEGIN}\n"
        f"{body.strip()}\n"
        f"{MARKER_END}\n"
    )
