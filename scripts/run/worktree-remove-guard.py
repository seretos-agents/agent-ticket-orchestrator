#!/usr/bin/env python3
"""
`PreToolUse` hook (registered by `hooks/hooks.json`) that mechanically
denies `mcp__plugin_agent-worktree_worktree__worktree_remove` whenever the
`pkg/*` checkout it targets is not clean and fully pushed to
`origin/<branch>` -- so `run` can never destroy a crashed session's
unsalvaged work, however its own prose is phrased (#84, mechanical backstop
for #81's `salvage-worktree.py`, not a replacement of it).

Symptom this closes (verbatim from the ticket): "when run escalates a
package, it deletes the package's worktree and, with it, any uncommitted or
unpushed work a crashed session left behind (unity-avatar #4)".

Contract
--------

Reads the hook's JSON payload (`{"tool_name": ..., "tool_input": {...}}`) on
stdin. Prints nothing on stdout. On deny, prints one line to stderr starting
with `worktree-remove-guard: denied` and exits 2 (the plugin's own
`check-no-background.mjs` convention: exit 2 blocks the tool call, any other
non-zero exit does not). On allow, prints nothing and exits 0.

Decision order (deny-first, scope-check second -- see "Scope" below):

    1. stdin is not parseable JSON, or has no `tool_input` object -> deny
    2. `tool_input.checkout_path` missing/empty -> deny
    3. `checkout_path` is not inside a git working tree -> deny
    4. HEAD is not on a branch (detached, mid-rebase) -> deny
    5. the branch is not `pkg/*` -> ALLOW (out of scope, see below)
    6. the working tree is dirty (tracked or untracked, `.seretos/`
       excluded) -> deny
    7. `origin/<branch>` does not exist -> deny
    8. HEAD has commits `origin/<branch>` does not have -> deny
    9. otherwise -> allow

Any uncaught exception anywhere in this script is treated as "undecidable"
and denies (fail closed), never propagates as a stack trace.

Scope, precisely (the ticket's own non-goal: interactive/human cleanup on
ordinary branches is not this guard's business): every checkout `run` ever
cuts is `pkg/<id>-<slug>` (`scripts/package-branch.py`), so step 5 is what
actually limits this guard to `run`'s own worktrees. That scope check can
only run *after* a branch name has been resolved, so steps 1-4 -- which
guard the preconditions the scope check itself depends on (parseable input,
a real path, a real repo, a real branch) -- deny before scope is even known.
The practical residual: a *non*-`pkg/*` checkout with no resolvable branch
(missing `checkout_path`, a path that is not a git worktree at all, or a
detached HEAD) is denied too, not because it is in scope, but because the
guard has no branch name to prove it is *out* of scope. This is the
documented "undecidable input fails closed" behaviour (R3 in the test
strategy), not an accidental widening of R1/R2's `pkg/*` scope.

Two further narrowing notes, both deliberate: the scope check (step 5) reads
the *currently checked-out* branch of the worktree at call time
(`symbolic-ref --short HEAD`), not whatever branch the worktree was
originally created on -- a worktree that changed branches since creation is
judged on where it stands now, which is what actually matters for "is this
still `run`'s worktree". And "pushed to origin" (steps 7-8) is checked
against this repository's local `refs/remotes/origin/<branch>` -- no
`fetch` is ever run, matching `salvage-worktree.py`'s own no-fetch
discipline; a push that happened through some other clone and was never
fetched here will not yet be visible.
"""

import json
import os
import pathlib
import subprocess
import sys

GIT_ENV_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0"}

EXIT_ALLOW = 0
EXIT_DENY = 2

DENY_PREFIX = "worktree-remove-guard: denied"

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SALVAGE_SCRIPT = SCRIPT_DIR / "salvage-worktree.py"


def run_git(checkout_path, *args):
    """Run one git subprocess against checkout_path, non-interactively.

    Returns the CompletedProcess, or None if `git` itself could not be
    launched -- callers treat None as "can't prove this is safe".
    """
    env = dict(os.environ)
    env.update(GIT_ENV_OVERRIDES)
    try:
        return subprocess.run(
            ["git", "-C", str(checkout_path), *args],
            capture_output=True, text=True, env=env,
        )
    except (OSError, ValueError):
        return None


def deny(reason, path=None, branch=None):
    """Print the one-line deny message to stderr and return EXIT_DENY.

    Without a `path` (stdin unparseable, `checkout_path` missing) there is
    nothing concrete to point the caller at. With a `path` but no `branch`
    (non-git path, detached HEAD) there is no branch to build a salvage
    command from, so the message stops at the reason. With both, the
    message names the exact salvage command to run -- `SALVAGE_SCRIPT` is
    resolved from this script's own location (`__file__`), never the
    caller's cwd or an environment variable, so it is correct regardless of
    where the hook was invoked from.
    """
    if path is None:
        message = f"{DENY_PREFIX} — {reason}"
    elif branch is None:
        message = f"{DENY_PREFIX} {path} — {reason}"
    else:
        message = (
            f"{DENY_PREFIX} {path} — {reason}. Push the work to "
            f"origin/{branch} first (python {SALVAGE_SCRIPT} {path} {branch}; "
            f"if it reports clean but origin/{branch} is missing: "
            f"git -C {path} push origin HEAD:refs/heads/{branch}), "
            f"then retry with checkout_path."
        )
    print(message, file=sys.stderr)
    return EXIT_DENY


def decide() -> int:
    try:
        raw_stdin = sys.stdin.read()
    except (OSError, ValueError):
        return deny("could not read the hook payload from stdin")

    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError:
        return deny("malformed stdin (not valid JSON)")

    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    if not isinstance(tool_input, dict):
        return deny("malformed stdin (no tool_input object)")

    checkout_path = tool_input.get("checkout_path")
    if not checkout_path:
        return deny(
            "no checkout_path in tool_input; cannot verify the target is "
            "clean and pushed. Provide checkout_path and retry"
        )

    result = run_git(checkout_path, "rev-parse", "--is-inside-work-tree")
    if result is None or result.returncode != 0 or result.stdout.strip() != "true":
        return deny("not a git worktree", path=checkout_path)

    result = run_git(checkout_path, "symbolic-ref", "--short", "HEAD")
    if result is None or result.returncode != 0:
        return deny(
            "HEAD is not on a branch (detached HEAD, or mid-rebase)",
            path=checkout_path,
        )
    branch = result.stdout.strip()

    if not branch.startswith("pkg/"):
        return EXIT_ALLOW

    result = run_git(checkout_path, "status", "--porcelain", "--", ".", ":(exclude).seretos")
    if result is None or result.returncode != 0:
        return deny("could not check working tree status", path=checkout_path, branch=branch)
    if result.stdout.strip():
        return deny(
            "the working tree has uncommitted or untracked changes",
            path=checkout_path, branch=branch,
        )

    remote_ref = run_git(checkout_path, "rev-parse", "--verify", "--quiet",
                          f"refs/remotes/origin/{branch}")
    if remote_ref is None or remote_ref.returncode != 0:
        return deny(
            f"origin/{branch} does not exist; this branch has never been pushed",
            path=checkout_path, branch=branch,
        )

    result = run_git(checkout_path, "rev-list", "HEAD", "--not", f"refs/remotes/origin/{branch}")
    if result is None or result.returncode != 0:
        return deny("could not check for commits missing from origin", path=checkout_path, branch=branch)
    if result.stdout.strip():
        return deny(
            f"HEAD has commits not on origin/{branch}",
            path=checkout_path, branch=branch,
        )

    return EXIT_ALLOW


def main() -> int:
    try:
        return decide()
    except BaseException:
        return deny("unexpected internal error while deciding")


if __name__ == "__main__":
    # Deny messages use an em dash; on Windows, stderr otherwise defaults to
    # the console's active codepage (not UTF-8), which raises on that
    # character rather than encoding it -- the same reconfigure
    # `salvage-worktree.py` applies to its own stdout.
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
