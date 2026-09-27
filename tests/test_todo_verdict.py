"""
Driving tests for `scripts/run/todo-verdict.py` (#86, code half of a lane
split whose prose half is #87): a pure stdin-JSON -> stdout-verdict helper
that mirrors `skills/run/SKILL.md` Step 1a ("Order Todo by dependency",
lines ~140-176) and "When is a blocker resolved" (~184-203), with one
documented tightening (transitive skip fires on *any* SKIPPED blocker, not
only when it is the *only* blocker -- Step 1a step 6's wording is silent on
that combination). #87's continuation loop will read `verdict:` alone to
decide whether to dispatch another package after a mid-run split leaves two
fresh tickets sitting in Todo.

Contract (settled here, since the script does not exist yet and this is
where its stdin/stdout shape is first pinned down for `implement` to build
against -- see plan.md for the full derivation):

  stdin (json.load(sys.stdin)):
    {"todo":   [{"id": 81, "blocked_by": [], "skip": ""},
                {"id": 80, "blocked_by": [81]}],   # board order, oldest first
     "closed": [12, 81]}                            # blocker ids the caller
                                                      # knows are closed

    `id` positive int, required. `blocked_by` list of ints, default `[]`.
    `skip` optional string, default `""`; non-empty = caller-known
    permanent-this-run reason, echoed verbatim. `closed` default `[]`.

  Classification, in order:
    1. drop edges to blockers in `closed`
    2. remaining blocker in `todo` -> INTERNAL edge; otherwise EXTERNAL-OPEN
    3. SKIPPED, in precedence:
       - non-empty `skip` -> `skipped: <skip>`
       - else first EXTERNAL-OPEN blocker -> `skipped: blocked by #<b> (not closed)`
       - else (to a fixpoint) any SKIPPED INTERNAL blocker, first in
         `blocked_by` list order -> `skipped: blocker #<b> skipped`
    4. Kahn topological order over the rest, board-order tie-break
    5. cycle members / their dependents: appended in board order at the end,
       never dropped

  stdout, `key: value` lines (`adev:event`'s dumb-reader convention), always
  in this order:
    verdict: dispatch | none
    next: #<id>                      -- first of `order`; empty on none
    none: todo-empty | all-skipped   -- only present when verdict is none
    order: #81 #80                   -- processing order, non-skipped only;
                                         empty on none
    ticket: #<id> runnable | waiting on #<b> [#<c>...] | cycle | skipped: <reason>
                                      -- one per todo ticket, board order
    cycle: #a -> #b -> #a, processed in board order
                                      -- zero or more, one line per distinct
                                         cycle, self-edge written #a -> #a

  `verdict: dispatch` iff `order` is non-empty; `next` is `order`'s first
  entry -- a `runnable` ticket whenever one exists, else the board-first
  cycle member. `verdict: none` iff `todo` is empty (`none: todo-empty`) or
  every ticket ended up SKIPPED (`none: all-skipped`).

  exit code: 0 on dispatch, 2 on none, 1 on invalid input (not a JSON
  object; `todo`/`closed` not lists; a ticket missing/non-int/duplicate
  `id`; non-list `blocked_by`; non-string `skip`) with `error: <what>` as a
  stdout line and no `verdict:` line at all.

The script does not exist yet at the time these tests are written
(phase=tests, RED only): every subprocess call below fails because Python
itself cannot open the missing file ("can't open file ... No such file or
directory") -- the RED reason actually observed is that `result.stdout` is
empty (the interpreter's complaint lands on stderr instead), so the first
assertion against stdout content is what actually fails, not a bare
exit-code coincidence.
"""

import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run" / "todo-verdict.py"


def run_verdict(payload):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def line_value(stdout, key):
    """Return the stripped remainder after `key:` for the first matching
    line, or None if no such line exists. Used for lines that may be
    legitimately empty (`next:`, `order:`) so an empty-string result can be
    told apart from an absent line."""
    prefix = f"{key}:"
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def lines_with_prefix(stdout, prefix):
    return [line for line in stdout.splitlines() if line.startswith(prefix)]


# --- R1: incident, split state ---------------------------------------------

def test_split_incident_code_half_runnable_original_waits():
    """The exact incident this package exists for: a mid-run split left #81
    (code half) and #80 (prose half, blocked_by #81) sitting in Todo. #81
    has no blocker and must be the next dispatch; #80 must report waiting,
    not run."""
    result = run_verdict({
        "todo": [
            {"id": 81},
            {"id": 80, "blocked_by": [81]},
        ],
    })
    assert "verdict: dispatch" in result.stdout, (
        f"expected 'verdict: dispatch', got stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    assert "next: #81" in result.stdout, result.stdout
    assert "order: #81 #80" in result.stdout, result.stdout
    assert "ticket: #81 runnable" in result.stdout, result.stdout
    assert "ticket: #80 waiting on #81" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


def test_split_incident_blocker_not_in_todo_and_not_closed_is_skipped():
    """Additional coverage for R1: if #81 already left Todo (dispatched by
    an earlier iteration of #87's loop) and was not closed, #80's edge is
    EXTERNAL-OPEN and #80 must be skipped, not waiting."""
    result = run_verdict({
        "todo": [
            {"id": 80, "blocked_by": [81]},
        ],
    })
    assert "ticket: #80 skipped: blocked by #81 (not closed)" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "verdict: none" in result.stdout, result.stdout
    assert "none: all-skipped" in result.stdout, result.stdout
    assert result.returncode == 2, result.stderr


# --- R2: after the code half closes -----------------------------------------

def test_split_incident_original_runnable_once_blocker_closed():
    """Once #81 (the code half) is closed, #80 (the prose half) becomes
    runnable -- this is the exact transition #87's loop must observe to
    dispatch #80 without a human noticing."""
    result = run_verdict({
        "todo": [
            {"id": 80, "blocked_by": [81]},
        ],
        "closed": [81],
    })
    assert "verdict: dispatch" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "next: #80" in result.stdout, result.stdout
    assert "ticket: #80 runnable" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


def test_closed_blocker_still_dropped_even_if_also_in_todo():
    """Additional coverage for R2: closed takes priority over board
    membership -- a blocker that is BOTH in `closed` and still physically
    present in `todo` (e.g. the caller's `closed` set was built from
    `done_this_run` before the card left the board) must still have its
    edge dropped, not classified as an unresolved INTERNAL edge."""
    result = run_verdict({
        "todo": [
            {"id": 81},
            {"id": 80, "blocked_by": [81]},
        ],
        "closed": [81],
    })
    assert "ticket: #80 runnable" in result.stdout, (
        f"expected #80's edge to #81 dropped (closed) rather than waiting: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ticket: #81 runnable" in result.stdout, result.stdout
    assert "verdict: dispatch" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


# --- R3: empty vs all-skipped "none" ----------------------------------------

def test_empty_todo_is_none_todo_empty():
    result = run_verdict({"todo": []})
    assert "verdict: none" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "none: todo-empty" in result.stdout, result.stdout
    assert line_value(result.stdout, "next") == "", result.stdout
    assert line_value(result.stdout, "order") == "", result.stdout
    assert result.returncode == 2, result.stderr


def test_all_skipped_is_none_all_skipped():
    """Three tickets, three different SKIPPED reasons, none runnable: a
    caller-declared skip, an external-open blocker, and a ticket behind the
    external-open one (transitive skip). `none:` here must read
    `all-skipped`, distinct from the empty-Todo case above."""
    result = run_verdict({
        "todo": [
            {"id": 1, "skip": "prose lane not installed"},
            {"id": 2, "blocked_by": [99]},
            {"id": 3, "blocked_by": [2]},
        ],
    })
    assert "ticket: #1 skipped: prose lane not installed" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ticket: #2 skipped: blocked by #99 (not closed)" in result.stdout, result.stdout
    assert "ticket: #3 skipped: blocker #2 skipped" in result.stdout, result.stdout
    assert "verdict: none" in result.stdout, result.stdout
    assert "none: all-skipped" in result.stdout, result.stdout
    assert line_value(result.stdout, "next") == "", result.stdout
    assert line_value(result.stdout, "order") == "", result.stdout
    assert result.returncode == 2, result.stderr


def test_skip_reason_echoed_verbatim():
    """Additional coverage for R3: an arbitrary caller-supplied skip string
    (a branch-check failure line, not one of the script's own vocabulary
    words) must be echoed exactly, not paraphrased or truncated."""
    result = run_verdict({
        "todo": [
            {"id": 5, "skip": "branch check failed: fatal: x"},
        ],
    })
    assert "ticket: #5 skipped: branch check failed: fatal: x" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode == 2, result.stderr


def test_skip_beats_external_open_blocker():
    """Additional coverage for R3: precedence order puts a caller-declared
    `skip` ahead of the EXTERNAL-OPEN check, even when both apply to the
    same ticket -- the skip reason must win."""
    result = run_verdict({
        "todo": [
            {"id": 5, "skip": "branch check failed: fatal: x", "blocked_by": [99]},
        ],
    })
    assert "ticket: #5 skipped: branch check failed: fatal: x" in result.stdout, (
        f"expected the explicit skip to take precedence over the open "
        f"blocker: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "blocked by #99" not in result.stdout, result.stdout
    assert result.returncode == 2, result.stderr


# --- R3b: transitive skip on ANY skipped blocker ----------------------------

def test_transitive_skip_applies_when_any_blocker_skipped():
    """Pins the case Step 1a step 6's "only blocker" wording leaves open:
    #3 has TWO blockers (#1, #2); #1 is SKIPPED (explicit skip) and #2 is
    runnable. Any SKIPPED blocker must still SKIP #3 -- #1 is first in
    `blocked_by` order, so it is the one named."""
    result = run_verdict({
        "todo": [
            {"id": 1, "skip": "x"},
            {"id": 2},
            {"id": 3, "blocked_by": [1, 2]},
        ],
    })
    assert "ticket: #3 skipped: blocker #1 skipped" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "order: #2" in result.stdout, result.stdout
    assert "next: #2" in result.stdout, result.stdout
    assert "verdict: dispatch" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


def test_transitive_skip_survives_partial_closure_of_blockers():
    """Additional coverage for R3b: closing ONE of #3's two blockers (#2)
    does not clear the skip -- #1 is still open and still SKIPPED, so #3
    must remain skipped."""
    result = run_verdict({
        "todo": [
            {"id": 1, "skip": "x"},
            {"id": 2},
            {"id": 3, "blocked_by": [1, 2]},
        ],
        "closed": [2],
    })
    assert "ticket: #3 skipped: blocker #1 skipped" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode == 0, result.stderr


# --- R4: cycle beside a runnable ticket --------------------------------------

def test_cycle_reported_and_emitted_last_in_board_order():
    """#5 is runnable; #7/#9 form a cycle; #11 is downstream of the cycle
    (blocked_by #9). All of #7/#9/#11 are unemitted by the Kahn pass and
    must be appended in board order, reported `cycle`, with exactly one
    cycle line naming only the cycle's own two members (#11 does not
    appear in it)."""
    result = run_verdict({
        "todo": [
            {"id": 5},
            {"id": 7, "blocked_by": [9]},
            {"id": 9, "blocked_by": [7]},
            {"id": 11, "blocked_by": [9]},
        ],
    })
    assert "verdict: dispatch" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "next: #5" in result.stdout, result.stdout
    assert "order: #5 #7 #9 #11" in result.stdout, result.stdout
    assert "ticket: #5 runnable" in result.stdout, result.stdout
    assert "ticket: #7 cycle" in result.stdout, result.stdout
    assert "ticket: #9 cycle" in result.stdout, result.stdout
    assert "ticket: #11 cycle" in result.stdout, result.stdout
    cycle_lines = lines_with_prefix(result.stdout, "cycle:")
    assert cycle_lines == ["cycle: #7 -> #9 -> #7, processed in board order"], (
        f"expected exactly one cycle line naming only #7/#9: {cycle_lines!r} "
        f"full stdout={result.stdout!r}"
    )
    assert result.returncode == 0, result.stderr


def test_self_edge_cycle_reported():
    """Additional coverage for R4: a ticket that blocks itself is a
    (degenerate) cycle of size one -- written as a self-edge, `#a -> #a`."""
    result = run_verdict({
        "todo": [
            {"id": 4, "blocked_by": [4]},
        ],
    })
    assert "ticket: #4 cycle" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    cycle_lines = lines_with_prefix(result.stdout, "cycle:")
    assert cycle_lines == ["cycle: #4 -> #4, processed in board order"], cycle_lines


# --- R4b: cycle-only Todo still dispatches -----------------------------------

def test_cycle_only_todo_dispatches_board_first_member():
    """Even when EVERY ticket in Todo is part of a cycle, `run` must still
    dispatch something -- a cycle is a ten-second human fix on the board,
    not a reason to stall the whole run. The board-first cycle member is
    `next`, and there is no `none:` line since verdict is dispatch."""
    result = run_verdict({
        "todo": [
            {"id": 7, "blocked_by": [9]},
            {"id": 9, "blocked_by": [7]},
        ],
    })
    assert "verdict: dispatch" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "next: #7" in result.stdout, result.stdout
    assert "order: #7 #9" in result.stdout, result.stdout
    assert "ticket: #7 cycle" in result.stdout, result.stdout
    assert "ticket: #9 cycle" in result.stdout, result.stdout
    assert not lines_with_prefix(result.stdout, "none:"), (
        f"verdict is dispatch; there must be no 'none:' line: stdout={result.stdout!r}"
    )
    cycle_lines = lines_with_prefix(result.stdout, "cycle:")
    assert len(cycle_lines) == 1, cycle_lines
    assert result.returncode == 0, result.stderr


def test_cycle_only_todo_with_a_skipped_ticket_still_picks_cycle_member():
    """Additional coverage for R4b: a SKIPPED ticket sitting alongside a
    cycle must not change which ticket becomes `next` -- the cycle's
    board-first member is still picked."""
    result = run_verdict({
        "todo": [
            {"id": 7, "blocked_by": [9]},
            {"id": 9, "blocked_by": [7]},
            {"id": 3, "skip": "x"},
        ],
    })
    assert "next: #7" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "ticket: #3 skipped: x" in result.stdout, result.stdout
    assert "verdict: dispatch" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


def test_single_unclosed_cycle_partner_left_in_todo_is_skipped():
    """Additional coverage for R4b: once #7 has been dispatched and left
    Todo (the cycle is now half-resolved by the caller), the remaining
    partner #9's blocker (#7) is no longer in `todo` and not `closed` --
    an ordinary EXTERNAL-OPEN skip, not a cycle, and with nothing else in
    Todo the verdict is `none`."""
    result = run_verdict({
        "todo": [
            {"id": 9, "blocked_by": [7]},
        ],
    })
    assert "verdict: none" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.returncode == 2, result.stderr


# --- R5: Kahn, board-order tie-break -----------------------------------------

def test_topological_order_uses_board_order_tie_break():
    """Board order is [3, 5, 8, 2]; #3 depends on #8 and #2 depends on #5.
    Kahn's algorithm with a board-order tie-break must emit #5 and #8
    before their respective dependents, and among simultaneously-ready
    candidates always pick the board-earliest one -- worked by hand in the
    plan: 5, 8, 3, 2."""
    result = run_verdict({
        "todo": [
            {"id": 3, "blocked_by": [8]},
            {"id": 5},
            {"id": 8},
            {"id": 2, "blocked_by": [5]},
        ],
    })
    assert "order: #5 #8 #3 #2" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "verdict: dispatch" in result.stdout, result.stdout
    assert "next: #5" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


def test_topological_order_with_no_edges_is_board_order():
    """Additional coverage for R5: with no `blocked_by` edges at all, the
    topological order degenerates to the board order unchanged."""
    result = run_verdict({
        "todo": [
            {"id": 3},
            {"id": 5},
            {"id": 8},
            {"id": 2},
        ],
    })
    assert "order: #3 #5 #8 #2" in result.stdout, (
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "next: #3" in result.stdout, result.stdout
    assert result.returncode == 0, result.stderr


# --- R6: invalid input rejected -----------------------------------------------

@pytest.mark.parametrize(
    "payload,label",
    [
        ({"todo": [{"id": 1}, {"id": 1}]}, "duplicate id"),
        ({"todo": [{"id": "1"}]}, "string id"),
        ({"todo": [{"id": 1, "blocked_by": 5}]}, "non-list blocked_by"),
    ],
)
def test_invalid_input_exits_1(payload, label):
    result = run_verdict(payload)
    assert result.returncode == 1, (
        f"[{label}] expected exit 1, got {result.returncode}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    error_lines = lines_with_prefix(result.stdout, "error:")
    assert error_lines, (
        f"[{label}] expected an 'error:' line in stdout, got "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert not lines_with_prefix(result.stdout, "verdict:"), (
        f"[{label}] invalid input must never print a 'verdict:' line: "
        f"stdout={result.stdout!r}"
    )
