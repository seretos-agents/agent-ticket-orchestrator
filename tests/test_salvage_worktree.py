"""
Behaviour tests for `scripts/run/salvage-worktree.py` (#81): does every
uncommitted/unpushed change in a package worktree reach `origin/<branch>`
before a caller removes the worktree, without ever committing `.seretos/`
or force-pushing over a diverged origin?

Each test builds a temp repo world: a bare `origin.git`; a `seed` clone that
pushes `main` and `pkg/1-demo`; a `checkout` clone whose local (persisted,
not just `-c`) git identity is shared with every worktree cut from it --
worktree config is not per-worktree by default, so this is what lets the
script's own git commands (run with no `-c` flags of their own) commit
inside the worktree; and a real linked worktree (`git worktree add`) that
the script is run against. No mocked git anywhere -- every assertion reads
straight from the bare `origin.git`, never trusting the script's own printed
line alone.
"""

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run" / "salvage-worktree.py"
BRANCH = "pkg/1-demo"

GIT_CFG = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def run_git(cwd, *args):
    """Run a git command with a throwaway identity, asserting success --
    used for test-world setup, never for the script's own git calls."""
    result = subprocess.run(
        ["git", "-C", str(cwd), *GIT_CFG, *args],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def git_raw(cwd, *args):
    """Run a git command with no `-c` overrides and no success assertion --
    used for read-only verification and for a deliberately failing merge."""
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )


def origin_ref_sha(origin, ref=f"refs/heads/{BRANCH}"):
    result = git_raw(origin, "rev-parse", ref)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def origin_has_ref(origin, ref):
    return git_raw(origin, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def origin_show(origin, ref, path):
    return git_raw(origin, "show", f"{ref}:{path}")


def origin_ls_tree(origin, ref):
    result = git_raw(origin, "ls-tree", "-r", "--name-only", ref)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


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
    # Persisted identity (not just `-c`): the worktree the script commits in
    # shares this repo's config, since worktree config is not per-worktree
    # by default -- the script's own git calls carry no `-c` flags.
    run_git(checkout, "config", "user.name", "wt")
    run_git(checkout, "config", "user.email", "wt@wt")
    run_git(checkout, "config", "commit.gpgsign", "false")

    run_git(checkout, "branch", "--track", BRANCH, f"origin/{BRANCH}")
    run_git(checkout, "worktree", "add", str(wt), BRANCH)

    return {"origin": origin, "seed": seed, "checkout": checkout, "wt": wt}


def run_script(worktree_path, branch=BRANCH):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(worktree_path), branch],
        capture_output=True, text=True, encoding="utf-8",
    )


# --- R1: uncommitted work reaches origin ------------------------------------

def test_uncommitted_work_is_reachable_on_origin(world):
    """R1: a modified tracked file, a new untracked file in a subdirectory,
    and a staged-only change are all readable from origin's branch after
    salvage; a second run on the now-clean worktree reports clean and moves
    nothing."""
    wt = world["wt"]
    origin = world["origin"]

    (wt / "file.txt").write_text("seed\nmodified\n", encoding="utf-8")
    sub = wt / "sub"
    sub.mkdir()
    (sub / "new.txt").write_text("new in subdir\n", encoding="utf-8")
    (wt / "staged.txt").write_text("staged only\n", encoding="utf-8")
    run_git(wt, "add", "staged.txt")

    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    tip = origin_ref_sha(origin)
    assert result.stdout.splitlines() == [f"salvage: pushed {tip}"]

    assert origin_show(origin, f"refs/heads/{BRANCH}", "file.txt").stdout == "seed\nmodified\n"
    assert origin_show(origin, f"refs/heads/{BRANCH}", "sub/new.txt").stdout == "new in subdir\n"
    assert origin_show(origin, f"refs/heads/{BRANCH}", "staged.txt").stdout == "staged only\n"

    # Additional edge-case coverage: a second run on a now-clean worktree.
    second = run_script(wt)
    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.splitlines() == ["salvage: clean"]
    assert origin_ref_sha(origin) == tip


# --- R2: unpushed commits reach origin ---------------------------------------

def test_unpushed_commits_are_reachable_on_origin(world):
    """R2: local commits on the branch, with a clean working tree, reach
    origin."""
    wt = world["wt"]
    origin = world["origin"]

    (wt / "committed.txt").write_text("local commit content\n", encoding="utf-8")
    run_git(wt, "add", "committed.txt")
    run_git(wt, "commit", "-m", "local unpushed commit")
    local_head = run_git(wt, "rev-parse", "HEAD").stdout.strip()

    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"salvage: pushed {local_head}"]
    assert origin_ref_sha(origin) == local_head
    assert origin_show(origin, f"refs/heads/{BRANCH}", "committed.txt").stdout == "local commit content\n"


def test_branch_never_pushed_is_created_on_origin(world, tmp_path):
    """R2 additional coverage: a branch cut locally from `main` that origin
    never had at all is created on origin by salvage."""
    checkout = world["checkout"]
    origin = world["origin"]
    new_branch = "pkg/2-new"
    wt2 = tmp_path / "wt2"

    run_git(checkout, "branch", new_branch, "main")
    run_git(checkout, "worktree", "add", str(wt2), new_branch)
    (wt2 / "brand_new.txt").write_text("brand new branch content\n", encoding="utf-8")
    run_git(wt2, "add", "brand_new.txt")
    run_git(wt2, "commit", "-m", "first commit on new branch")
    local_head = run_git(wt2, "rev-parse", "HEAD").stdout.strip()

    assert not origin_has_ref(origin, f"refs/heads/{new_branch}")

    result = run_script(wt2, branch=new_branch)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [f"salvage: pushed {local_head}"]
    assert origin_ref_sha(origin, f"refs/heads/{new_branch}") == local_head


# --- R3: .seretos/ is never committed ----------------------------------------

def test_seretos_is_never_committed(world):
    """R3: `.seretos/` content is excluded from the salvage commit, even
    though it is not gitignored -- exclusion is by pathspec only."""
    wt = world["wt"]
    origin = world["origin"]

    seretos = wt / ".seretos"
    seretos.mkdir()
    (seretos / "state.json").write_text('{"lease": true}\n', encoding="utf-8")
    (wt / "real_change.txt").write_text("keep me\n", encoding="utf-8")

    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    tip = origin_ref_sha(origin)
    assert result.stdout.splitlines() == [f"salvage: pushed {tip}"]
    assert origin_show(origin, f"refs/heads/{BRANCH}", "real_change.txt").stdout == "keep me\n"
    assert ".seretos/state.json" not in origin_ls_tree(origin, f"refs/heads/{BRANCH}")
    # working tree is untouched
    assert (seretos / "state.json").exists()


def test_prestaged_seretos_is_never_committed(world):
    """R3: `.seretos/` content already staged in the index *before* the
    script runs must still never reach origin -- a `git add` pathspec
    exclusion on the script's own staging step alone is not enough, because
    a bare `git commit` commits the whole index."""
    wt = world["wt"]
    origin = world["origin"]

    seretos = wt / ".seretos"
    seretos.mkdir()
    (seretos / "state.json").write_text('{"lease": true}\n', encoding="utf-8")
    run_git(wt, "add", "-f", ".seretos/state.json")
    (wt / "other_change.txt").write_text("other change\n", encoding="utf-8")

    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    tip = origin_ref_sha(origin)
    assert result.stdout.splitlines() == [f"salvage: pushed {tip}"]
    assert origin_show(origin, f"refs/heads/{BRANCH}", "other_change.txt").stdout == "other change\n"
    assert ".seretos/state.json" not in origin_ls_tree(origin, f"refs/heads/{BRANCH}")
    assert (seretos / "state.json").exists()


def test_only_seretos_changed_reports_clean(world):
    """R3 additional coverage: when `.seretos/` is the only dirty path,
    salvage has nothing to save and reports clean without committing."""
    wt = world["wt"]
    origin = world["origin"]

    seretos = wt / ".seretos"
    seretos.mkdir()
    (seretos / "state.json").write_text('{"lease": true}\n', encoding="utf-8")

    tip_before = origin_ref_sha(origin)
    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["salvage: clean"]
    assert origin_ref_sha(origin) == tip_before


# --- R4: a clean worktree is reported clean ----------------------------------

def test_clean_worktree_reports_clean(world):
    """R4: a worktree with nothing dirty and nothing unpushed reports
    clean, commits nothing, and pushes nothing."""
    wt = world["wt"]
    origin = world["origin"]

    local_head_before = run_git(wt, "rev-parse", "HEAD").stdout.strip()
    tip_before = origin_ref_sha(origin)

    result = run_script(wt)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["salvage: clean"]
    assert run_git(wt, "rev-parse", "HEAD").stdout.strip() == local_head_before
    assert origin_ref_sha(origin) == tip_before


# --- R5: a push that cannot land keeps the worktree, never forces -----------

def test_diverged_origin_is_kept_not_forced(world):
    """R5: origin's branch diverged from the worktree's local history --
    salvage must not force-push over it; the worktree, and the would-be
    salvage commit, are kept in place instead."""
    wt = world["wt"]
    seed = world["seed"]
    origin = world["origin"]

    run_git(seed, "checkout", BRANCH)
    (seed / "diverged.txt").write_text("someone else pushed this\n", encoding="utf-8")
    run_git(seed, "add", "diverged.txt")
    run_git(seed, "commit", "-m", "diverged commit pushed by someone else")
    run_git(seed, "push", "origin", BRANCH)
    diverged_tip = origin_ref_sha(origin)

    (wt / "my_change.txt").write_text("my uncommitted change\n", encoding="utf-8")

    result = run_script(wt)

    assert result.returncode == 3, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"salvage: kept {wt} — push failed: "), lines
    assert origin_ref_sha(origin) == diverged_tip

    # the change survives -- captured in a local salvage commit that a later
    # retry can still push, never lost with the worktree
    assert (wt / "my_change.txt").exists()
    status = git_raw(wt, "status", "--porcelain", "--untracked-files=all").stdout
    assert status.strip() == "", "the salvage commit should have captured the change locally"


def test_unreachable_origin_is_kept(world, tmp_path):
    """R5: origin is unreachable (repointed at a nonexistent path) -- kept,
    never a crash, never a force-push."""
    wt = world["wt"]
    origin = world["origin"]
    tip_before = origin_ref_sha(origin)

    missing = tmp_path / "missing.git"
    run_git(wt, "remote", "set-url", "origin", str(missing))
    (wt / "change.txt").write_text("change\n", encoding="utf-8")

    result = run_script(wt)

    assert result.returncode == 3, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"salvage: kept {wt} — "), lines
    assert origin_ref_sha(origin) == tip_before


def test_head_not_on_branch_is_kept(world):
    """R5: HEAD detached (not on the named branch) -- kept, even with
    nothing dirty."""
    wt = world["wt"]
    origin = world["origin"]
    tip_before = origin_ref_sha(origin)

    head = run_git(wt, "rev-parse", "HEAD").stdout.strip()
    run_git(wt, "checkout", "--detach", head)

    result = run_script(wt)

    assert result.returncode == 3, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"salvage: kept {wt} — "), lines
    assert origin_ref_sha(origin) == tip_before


def test_unmerged_paths_are_kept(world):
    """R5: unmerged paths (a conflicted merge in progress) -- kept; salvage
    must never push conflict markers onto the branch."""
    wt = world["wt"]
    origin = world["origin"]
    tip_before = origin_ref_sha(origin)

    seed_sha = run_git(wt, "rev-parse", BRANCH).stdout.strip()

    (wt / "file.txt").write_text("seed\nlocal-change\n", encoding="utf-8")
    run_git(wt, "commit", "-am", "local change on branch")

    run_git(wt, "branch", "other-side", seed_sha)
    run_git(wt, "checkout", "other-side")
    (wt / "file.txt").write_text("seed\nother-change\n", encoding="utf-8")
    run_git(wt, "commit", "-am", "other change")
    run_git(wt, "checkout", BRANCH)

    merge = subprocess.run(
        ["git", "-C", str(wt), *GIT_CFG, "merge", "other-side"],
        capture_output=True, text=True,
    )
    assert merge.returncode != 0, "expected a merge conflict, got: " + merge.stdout + merge.stderr
    unmerged = git_raw(wt, "ls-files", "-u").stdout
    assert unmerged.strip() != "", "expected unmerged paths from the conflicting merge"

    result = run_script(wt)

    assert result.returncode == 3, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"salvage: kept {wt} — "), lines
    assert origin_ref_sha(origin) == tip_before


def test_path_that_is_not_a_worktree_is_kept(tmp_path):
    """R5: a path that isn't a git worktree at all -- kept, exit 3, never a
    crash."""
    not_a_worktree = tmp_path / "not_a_repo"
    not_a_worktree.mkdir()

    result = run_script(not_a_worktree)

    assert result.returncode == 3, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"salvage: kept {not_a_worktree} — "), lines


def test_missing_arguments_exit_2_with_no_salvage_line():
    """R5: missing/wrong CLI arguments are a usage error -- exit 2, no
    `salvage:` line at all (distinct from `kept`'s exit 3)."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, encoding="utf-8",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert not any(line.startswith("salvage:") for line in result.stdout.splitlines())
