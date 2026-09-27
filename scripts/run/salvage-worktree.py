#!/usr/bin/env python3
"""
Salvage a package worktree's uncommitted/unpushed work onto its own branch on
`origin`, before a caller removes the worktree (#81, code half of #80).

Symptom this closes (verbatim from the ticket): "when run escalates a
package whose session died or stopped, work that session wrote to the
worktree but never pushed is destroyed with the worktree and cannot be
recovered".

Usage
-----

    salvage-worktree.py <worktree_path> <branch>

stdout is always exactly one line:

    salvage: clean               nothing to save; safe to remove.
    salvage: pushed <sha>        <sha> is the full HEAD sha now on
                                  origin/<branch>; safe to remove.
    salvage: kept <path> — <reason>
                                  do NOT remove; <path> is the
                                  <worktree_path> argument echoed verbatim,
                                  <reason> is one line (git stderr reduced to
                                  its first line where applicable).

Exit codes
----------

    0   clean, or pushed -- safe to remove the worktree.
    1   uncaught crash -- no `salvage:` line.
    2   usage error (missing/wrong arguments) -- no `salvage:` line.
    3   kept -- do not remove the worktree.

Contract for #80: **remove the worktree only on exit 0.**

`.seretos/` (whatever `worktree_create` places there, top-level only -- not
relied on being gitignored) is never committed, whether it was dirty or
already staged before this script ran. Never force-pushes: a diverged
origin ends in `kept`, not an overwrite.
"""

import os
import pathlib
import subprocess
import sys

GIT_ENV_OVERRIDES = {"GIT_TERMINAL_PROMPT": "0"}

EXIT_CLEAN_OR_PUSHED = 0
EXIT_CRASH = 1
EXIT_USAGE = 2
EXIT_KEPT = 3


def run_git(worktree_path, *args):
    """Run one git subprocess against worktree_path, non-interactively.

    Returns the CompletedProcess, or None if `git` itself could not be
    launched (e.g. not installed) -- callers treat None as a failure.
    """
    env = dict(os.environ)
    env.update(GIT_ENV_OVERRIDES)
    try:
        return subprocess.run(
            ["git", "-C", str(worktree_path), *args],
            capture_output=True, text=True, env=env,
        )
    except FileNotFoundError:
        return None


def stderr_first_line(result):
    stderr_lines = (result.stderr or "").strip().splitlines()
    return stderr_lines[0] if stderr_lines else "(no stderr)"


def kept(worktree_path, reason):
    print(f"salvage: kept {worktree_path} — {reason}")
    return EXIT_KEPT


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: salvage-worktree.py <worktree_path> <branch>", file=sys.stderr)
        return EXIT_USAGE

    worktree_path = pathlib.Path(sys.argv[1])
    branch = sys.argv[2]

    # Step 1: is it even a git worktree?
    if not worktree_path.is_dir():
        return kept(worktree_path, "not a git worktree")

    result = run_git(worktree_path, "rev-parse", "--is-inside-work-tree")
    if result is None or result.returncode != 0 or result.stdout.strip() != "true":
        return kept(worktree_path, "not a git worktree")

    # Step 2: HEAD must be on <branch> -- covers detached HEAD / rebase in
    # progress, and a worktree the caller pointed at the wrong branch.
    result = run_git(worktree_path, "symbolic-ref", "--short", "HEAD")
    if result is None or result.returncode != 0:
        return kept(worktree_path, "HEAD is not on a branch")
    current_branch = result.stdout.strip()
    if current_branch != branch:
        return kept(worktree_path, f"HEAD is on {current_branch}, not {branch}")

    # Step 3: no unmerged paths -- never push conflict markers onto a branch
    # a retry resumes from.
    result = run_git(worktree_path, "ls-files", "-u")
    if result is None or result.returncode != 0:
        return kept(worktree_path, "could not check for unmerged paths")
    if result.stdout.strip():
        return kept(worktree_path, "unmerged paths")

    # Step 4: dirty check, .seretos/ excluded.
    result = run_git(
        worktree_path, "status", "--porcelain", "--untracked-files=all",
        "--", ".", ":(exclude).seretos",
    )
    if result is None or result.returncode != 0:
        return kept(worktree_path, "could not check working tree status")
    dirty = bool(result.stdout.strip())

    # Step 5: unpushed check.
    remote_ref = run_git(worktree_path, "rev-parse", "--verify", "--quiet",
                          f"refs/remotes/origin/{branch}")
    if remote_ref is not None and remote_ref.returncode == 0:
        result = run_git(worktree_path, "rev-list", f"origin/{branch}..HEAD")
    else:
        result = run_git(worktree_path, "rev-list", "HEAD", "--not", "--remotes=origin")
    if result is None or result.returncode != 0:
        return kept(worktree_path, "could not check for unpushed commits")
    unpushed = bool(result.stdout.strip())

    # Step 6: nothing dirty, nothing unpushed -- clean.
    if not dirty and not unpushed:
        print("salvage: clean")
        return EXIT_CLEAN_OR_PUSHED

    # Step 7: commit the dirty tree, .seretos/ excluded even if pre-staged.
    if dirty:
        result = run_git(worktree_path, "add", "-A", "--", ".", ":(exclude).seretos")
        if result is None or result.returncode != 0:
            return kept(worktree_path, "could not stage changes")

        result = run_git(worktree_path, "reset", "-q", "--", ".seretos")
        if result is None or result.returncode != 0:
            return kept(worktree_path, "could not exclude .seretos from the index")

        result = run_git(
            worktree_path, "commit", "--no-verify", "-m",
            f"salvage: uncommitted work on {branch} before worktree removal",
        )
        if result is None or result.returncode != 0:
            return kept(worktree_path, f"commit failed: {stderr_first_line(result) if result else '(git not found)'}")

    # Step 8: push, never forced.
    result = run_git(worktree_path, "push", "--no-verify", "origin", f"HEAD:refs/heads/{branch}")
    if result is None or result.returncode != 0:
        reason = stderr_first_line(result) if result else "(git not found)"
        return kept(worktree_path, f"push failed: {reason}")

    # Step 9: success.
    result = run_git(worktree_path, "rev-parse", "HEAD")
    if result is None or result.returncode != 0:
        return kept(worktree_path, "pushed but could not resolve HEAD sha")

    print(f"salvage: pushed {result.stdout.strip()}")
    return EXIT_CLEAN_OR_PUSHED


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
