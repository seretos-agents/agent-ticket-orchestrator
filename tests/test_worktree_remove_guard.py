"""
Behaviour tests for `scripts/run/worktree-remove-guard.py` and its
`hooks/hooks.json` registration (#84, code half of #80): a `PreToolUse` hook
that mechanically denies `mcp__plugin_agent-worktree_worktree__worktree_remove`
whenever the `pkg/*` checkout it targets is not clean and fully pushed to
`origin/<branch>` -- so `run` can never destroy a crashed session's
unsalvaged work, however its own prose is phrased (see #81's
`salvage-worktree.py`, which this guard is the backstop for, not a
replacement of).

Symptom this closes (verbatim from the ticket): "when run escalates a
package, it deletes the package's worktree and, with it, any uncommitted or
unpushed work a crashed session left behind (unity-avatar #4)".

Fixture pattern copied from `tests/test_salvage_worktree.py`: a real git
world -- a bare `origin.git`; a `seed` clone that pushes `main` and
`pkg/1-demo`; a `checkout` clone whose local (persisted) identity is shared
with every worktree cut from it; and a real linked worktree (`git worktree
add`) the guard is run against. No mocked git anywhere.

Neither `scripts/run/worktree-remove-guard.py` nor `hooks/hooks.json` exists
yet at the time these tests are written (phase=tests, RED only). Every test
below is written to the GREEN (post-implementation) behaviour; the RED
proof this round is that the guard script is missing, so every subprocess
invocation fails with Python's own "can't open file" (or, for the R4
hooks.json-reading tests, a `FileNotFoundError` from `HOOKS_JSON.read_text`)
-- never a masking failure of some other kind.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run" / "worktree-remove-guard.py"
SALVAGE_SCRIPT = REPO_ROOT / "scripts" / "run" / "salvage-worktree.py"
HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"
BRANCH = "pkg/1-demo"
TOOL_NAME = "mcp__plugin_agent-worktree_worktree__worktree_remove"
TOOL_NAME_CREATE = "mcp__plugin_agent-worktree_worktree__worktree_create"

GIT_CFG = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def run_git(cwd, *args):
    """Run a git command with a throwaway identity, asserting success --
    used for test-world setup, never for the guard's own git calls."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *GIT_CFG, *args],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def git_raw(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )


@pytest.fixture
def world(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    wt = tmp_path / "wt"

    run_git(tmp_path, "init", "--bare", str(origin))

    run_git(tmp_path, "init", "-b", "main", str(seed))
    (seed / "file.txt").write_text("seed\n", encoding="utf-8")
    run_git(seed, "add", "file.txt")
    run_git(seed, "commit", "-m", "seed commit")
    run_git(seed, "remote", "add", "origin", str(origin))
    run_git(seed, "push", "origin", "main")

    run_git(seed, "checkout", "-b", BRANCH)
    run_git(seed, "push", "origin", BRANCH)
    run_git(seed, "checkout", "main")

    run_git(tmp_path, "clone", str(origin), str(checkout))
    run_git(checkout, "checkout", "main")
    # Persisted identity (not just `-c`): the worktree the guard's own git
    # calls run against shares this repo's config -- worktree config is not
    # per-worktree by default.
    run_git(checkout, "config", "user.name", "wt")
    run_git(checkout, "config", "user.email", "wt@wt")
    run_git(checkout, "config", "commit.gpgsign", "false")

    run_git(checkout, "branch", "--track", BRANCH, f"origin/{BRANCH}")
    run_git(checkout, "worktree", "add", str(wt), BRANCH)

    return {"origin": origin, "seed": seed, "checkout": checkout, "wt": wt}


def add_worktree(checkout, tmp_path, name, branch, base="main"):
    """A second linked worktree, cut from `checkout`, on a brand-new branch
    that origin has never seen (no `origin/<branch>` ref at all)."""
    wt = tmp_path / name
    run_git(checkout, "branch", branch, base)
    run_git(checkout, "worktree", "add", str(wt), branch)
    return wt


def payload_for(checkout_path):
    return {
        "tool_name": TOOL_NAME,
        "tool_input": {
            "environment_id": "x",
            "checkout_path": str(checkout_path),
            "force": True,
        },
    }


def run_guard(payload):
    """Spawn the guard with the hook's JSON on stdin, the same shape the
    Claude Code harness feeds a PreToolUse hook."""
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
    )


# =============================================================================
# R1 -- unsalvaged work in a package checkout blocks removal
# =============================================================================


def test_dirty_worktree_is_denied(world):
    """A genuine modification to an already-tracked, already-pushed file --
    distinct from test_untracked_file_is_denied's brand-new untracked file
    below -- must also deny."""
    wt = world["wt"]
    (wt / "file.txt").write_text("modified tracked content\n", encoding="utf-8")

    result = run_guard(payload_for(wt))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr
    assert str(wt) in result.stderr
    assert "salvage-worktree.py" in result.stderr
    assert wt.is_dir(), "the worktree must not have been touched"


def test_unpushed_commit_is_denied(world):
    wt = world["wt"]
    (wt / "new.txt").write_text("local only\n", encoding="utf-8")
    run_git(wt, "add", "new.txt")
    run_git(wt, "commit", "-m", "local unpushed commit")

    result = run_guard(payload_for(wt))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr
    assert str(wt) in result.stderr


# --- Additional edge-case coverage ------------------------------------------


def test_branch_absent_from_origin_is_denied(world, tmp_path):
    """origin has no ref for this branch at all, and there are commits of
    our own on it -- denied via the missing-origin-ref path, distinct from
    `test_unpushed_commit_is_denied` where origin/<branch> exists but is
    behind."""
    checkout = world["checkout"]
    wt2 = add_worktree(checkout, tmp_path, "wt2", "pkg/2-nopush")
    (wt2 / "new.txt").write_text("content\n", encoding="utf-8")
    run_git(wt2, "add", "new.txt")
    run_git(wt2, "commit", "-m", "never pushed anywhere")

    result = run_guard(payload_for(wt2))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_branch_absent_from_origin_without_own_commits_is_denied(world, tmp_path):
    """Same missing-origin-ref case, but the branch is identical to `main`
    -- nothing dirty, nothing committed beyond what origin already has under
    a different name. Still denied: the guard cannot prove *this* branch is
    pushed just because its content happens to be reachable elsewhere."""
    checkout = world["checkout"]
    wt2 = add_worktree(checkout, tmp_path, "wt2", "pkg/3-nopush-clean")

    result = run_guard(payload_for(wt2))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_commit_only_on_other_origin_branch_is_denied(world):
    """The HEAD commit was pushed to origin, but under a different branch
    name -- origin/<branch> itself never received it. The guard must check
    strictly against origin/<branch>, not "is this commit on origin
    anywhere"."""
    wt = world["wt"]

    (wt / "shared.txt").write_text("shared content\n", encoding="utf-8")
    run_git(wt, "add", "shared.txt")
    run_git(wt, "commit", "-m", "reaches origin only under another name")
    run_git(wt, "push", "origin", "HEAD:refs/heads/other-branch")

    result = run_guard(payload_for(wt))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_untracked_file_is_denied(world):
    """A brand-new untracked file (nothing staged, nothing modified) is
    still "dirty" for the guard's purposes."""
    wt = world["wt"]
    (wt / "untracked.txt").write_text("brand new\n", encoding="utf-8")

    result = run_guard(payload_for(wt))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_detached_head_is_denied(world):
    wt = world["wt"]
    head = run_git(wt, "rev-parse", "HEAD").stdout.strip()
    run_git(wt, "checkout", "--detach", head)

    result = run_guard(payload_for(wt))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


# =============================================================================
# R2 -- clean pushed package checkout, and any non-package checkout, allowed
# =============================================================================


def test_clean_pushed_worktree_is_allowed(world):
    wt = world["wt"]

    result = run_guard(payload_for(wt))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_non_package_branch_is_allowed_even_when_dirty(world):
    """`checkout` sits on `main`, not `pkg/*` -- out of scope for the guard
    (the ticket's non-goal: interactive/human cleanup on ordinary branches).
    Dirty and unpushed changes there are irrelevant."""
    checkout = world["checkout"]
    (checkout / "dirty.txt").write_text("dirty on a non-pkg branch\n", encoding="utf-8")

    result = run_guard(payload_for(checkout))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


# --- Additional edge-case coverage ------------------------------------------


def test_seretos_only_is_allowed(world):
    """`.seretos/` (the untracked state `worktree_create` copies into every
    worktree, AGENTS.md #54) is excluded from the dirty check -- a worktree
    that is otherwise clean and pushed must still be allowed."""
    wt = world["wt"]
    seretos = wt / ".seretos"
    seretos.mkdir()
    (seretos / "state.json").write_text('{"lease": true}\n', encoding="utf-8")

    result = run_guard(payload_for(wt))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_allowed_after_salvage(world):
    """End-to-end with #81's own script: dirty -> salvage -> guard allows.
    This is the exact recovery path the guard's deny message points a
    caller at."""
    wt = world["wt"]
    (wt / "unsaved.txt").write_text("will be salvaged\n", encoding="utf-8")

    salvage = subprocess.run(
        [sys.executable, str(SALVAGE_SCRIPT), str(wt), BRANCH],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert salvage.returncode == 0, salvage.stdout + salvage.stderr
    assert salvage.stdout.startswith("salvage: pushed")

    result = run_guard(payload_for(wt))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


# =============================================================================
# R3 -- undecidable input fails closed
# =============================================================================


def test_missing_checkout_path_is_denied():
    payload = {"tool_name": TOOL_NAME, "tool_input": {"environment_id": "x", "force": True}}

    result = run_guard(payload)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "checkout_path" in result.stderr


# --- Additional edge-case coverage ------------------------------------------


def test_non_git_path_is_denied(tmp_path):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    result = run_guard(payload_for(not_a_repo))

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_malformed_stdin_is_denied():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="not json at all {{{", capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


def test_empty_checkout_path_is_denied():
    payload = {
        "tool_name": TOOL_NAME,
        "tool_input": {"environment_id": "x", "checkout_path": "", "force": True},
    }

    result = run_guard(payload)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "checkout_path" in result.stderr


# =============================================================================
# R4 -- the registered command reaches the guard on any host, else denies
# =============================================================================
#
# hooks/hooks.json's command is a POSIX shell snippet run directly by bash
# (never relying on the exec bit, which Git-for-Windows does not preserve --
# these tests set it explicitly on the shim files they write). A restricted
# PATH containing only hand-built shims proves the interpreter fallback
# (python3 -> python -> py) actually works and fails closed with none of the
# three present, without depending on whatever interpreters happen to be on
# the *real* dev/CI machine's PATH.


def _find_bash():
    if sys.platform == "win32":
        candidate = pathlib.Path(r"C:\Program Files\Git\bin\bash.exe")
        return str(candidate) if candidate.is_file() else None
    return shutil.which("bash")


BASH = _find_bash()
requires_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this host")

REAL_PYTHON = sys.executable.replace("\\", "/")
_which_git = shutil.which("git")
REAL_GIT = _which_git.replace("\\", "/") if _which_git else None


def _hooks_entries():
    """Load hooks/hooks.json's PreToolUse entries. Reading the file before
    it exists is R4's expected RED reason this round (FileNotFoundError),
    never an assertion failure."""
    data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    return data["hooks"]["PreToolUse"]


def _matching_command(entries, tool_name):
    for entry in entries:
        if re.fullmatch(entry["matcher"], tool_name):
            return entry["hooks"][0]["command"]
    return None


def _make_shim(bindir, name, real_path):
    shim = bindir / name
    shim.write_text(f'#!/usr/bin/env bash\nexec "{real_path}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return shim


def _shim_dir(tmp_path, name, python_names, include_git=True):
    bindir = tmp_path / name
    bindir.mkdir()
    for interp in python_names:
        _make_shim(bindir, interp, REAL_PYTHON)
    if include_git:
        assert REAL_GIT, "git not found on PATH to build the test shim from"
        _make_shim(bindir, "git", REAL_GIT)
    return bindir


def run_hook_command(command, payload, bindir):
    env = dict(os.environ)
    env["PATH"] = str(bindir)
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT).replace("\\", "/")
    return subprocess.run(
        [BASH, "-c", command],
        input=json.dumps(payload), capture_output=True, text=True, env=env, encoding="utf-8",
    )


@requires_bash
def test_hooks_json_command_denies_with_python3_only(world, tmp_path):
    entries = _hooks_entries()
    command = _matching_command(entries, TOOL_NAME)
    assert command, f"no PreToolUse entry matches {TOOL_NAME}"

    wt = world["wt"]
    (wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    bindir = _shim_dir(tmp_path, "bin-py3-only", ["python3"])

    result = run_hook_command(command, payload_for(wt), bindir)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr
    assert str(wt) in result.stderr


# --- Additional edge-case coverage ------------------------------------------


@requires_bash
def test_hooks_json_command_denies_with_no_python_interpreter(world, tmp_path):
    entries = _hooks_entries()
    command = _matching_command(entries, TOOL_NAME)
    wt = world["wt"]
    bindir = _shim_dir(tmp_path, "bin-empty", [], include_git=False)

    result = run_hook_command(command, payload_for(wt), bindir)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "no Python 3 interpreter" in result.stderr


@requires_bash
def test_hooks_json_command_falls_back_to_bare_python(world, tmp_path):
    """Only `python` (no `python3`, no `py`) on PATH -- the fallback loop
    must still find and use it."""
    entries = _hooks_entries()
    command = _matching_command(entries, TOOL_NAME)
    wt = world["wt"]
    (wt / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    bindir = _shim_dir(tmp_path, "bin-python-only", ["python"])

    result = run_hook_command(command, payload_for(wt), bindir)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "worktree-remove-guard: denied" in result.stderr


@requires_bash
def test_hooks_json_command_allows_clean_world_and_stdin_survives_the_probe(world, tmp_path):
    """The `python3 -c ...` interpreter probe must not consume the hook
    JSON piped on stdin -- the `exec` that follows still hands the guard the
    original payload intact."""
    entries = _hooks_entries()
    command = _matching_command(entries, TOOL_NAME)
    wt = world["wt"]
    bindir = _shim_dir(tmp_path, "bin-clean", ["python3"])

    result = run_hook_command(command, payload_for(wt), bindir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""


def test_hooks_json_matcher_rejects_worktree_create():
    entries = _hooks_entries()

    command = _matching_command(entries, TOOL_NAME_CREATE)

    assert command is None
