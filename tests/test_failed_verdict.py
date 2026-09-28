"""
Driving tests for `scripts/run/failed-verdict.py` (#93, code half of #92): a
pure stdin-JSON -> stdout-verdict helper telling `run` what to do after a
package session ended `failed` (or with no terminal event): `retry`,
`triage` or `escalate`. #92's prose half wires it into SKILL.md 2c.

Contract (stdin, one JSON object):
  failures      int >= 1   failed / no-terminal endings this run, incl. this one
  terminal      "failed" | "none"
  rounds        optional string, latest adev:event `rounds:` value verbatim
  triage_spent  bool       a `## Blocked triage (run)` comment exists this run
  sessions      int >= 1   package sessions this run, incl. the one just ended

Decision, first match wins:
  sessions >= 3            -> escalate / ceiling-reached
  failures == 1            -> retry    / first-failure
  terminal == "none"       -> escalate / no-terminal-event
  triage_spent             -> escalate / triage-spent
  no gate with f > 0       -> escalate / infra-only
  otherwise                -> triage   / findings-rounds

stdout: `verdict:` then `reason:` lines, exit 0 for every verdict; exit 1 on
invalid input with a single `error:` line and no `verdict:` line.

Written before the script exists: every subprocess call fails because Python
cannot open the missing file (stderr), so stdout is empty and the first
assertion on stdout content is what fails.
"""

import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "run" / "failed-verdict.py"

FINDINGS_ROUNDS = (
    "plan-critic=1/3(1f,0i) test-critic=0/3 review=2/3(2f,0i) "
    "ci=1/3(0f,1i) rebase=0/3(0f,0i)"
)


def base(**overrides):
    payload = {
        "failures": 2,
        "terminal": "failed",
        "triage_spent": False,
        "sessions": 2,
        "rounds": FINDINGS_ROUNDS,
    }
    payload.update(overrides)
    return payload


def run_verdict(payload, raw=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True,
        text=True,
    )


def line_value(stdout, key):
    prefix = f"{key}:"
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def assert_verdict(result, verdict, reason):
    ctx = f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert line_value(result.stdout, "verdict") == verdict, ctx
    assert line_value(result.stdout, "reason") == reason, ctx
    assert result.returncode == 0, ctx


# --- R1: the symptom -- second failure with a findings round -> triage -------

def test_second_failure_with_findings_round_is_triage():
    assert_verdict(run_verdict(base()), "triage", "findings-rounds")


def test_findings_only_in_rebase_gate_is_triage():
    result = run_verdict(base(
        rounds="plan-critic=1/3(0f,1i) review=0/3(0f,0i) rebase=1/3(1f,0i)"
    ))
    assert_verdict(result, "triage", "findings-rounds")


def test_findings_in_last_token_is_triage():
    result = run_verdict(base(rounds="plan-critic=0/3 ci=1/3(0f,1i) review=1/3(1f,0i)"))
    assert_verdict(result, "triage", "findings-rounds")


# --- R2: first failure -> retry ----------------------------------------------

def test_first_failure_is_retry():
    result = run_verdict(base(failures=1, sessions=1))
    assert_verdict(result, "retry", "first-failure")


def test_first_failure_with_no_terminal_event_is_retry():
    result = run_verdict(base(failures=1, sessions=1, terminal="none"))
    assert_verdict(result, "retry", "first-failure")


def test_first_failure_with_infra_only_rounds_is_retry():
    result = run_verdict(base(failures=1, sessions=1, rounds="ci=1/3(0f,1i)"))
    assert_verdict(result, "retry", "first-failure")


def test_first_failure_with_empty_rounds_is_retry():
    result = run_verdict(base(failures=1, sessions=1, rounds=""))
    assert_verdict(result, "retry", "first-failure")


def test_first_failure_after_triage_redispatch_is_retry():
    result = run_verdict(base(failures=1, sessions=2, triage_spent=True))
    assert_verdict(result, "retry", "first-failure")


def test_rounds_omitted_defaults_to_empty():
    payload = base(failures=1, sessions=1)
    del payload["rounds"]
    assert_verdict(run_verdict(payload), "retry", "first-failure")


# --- R3: escalate cases -------------------------------------------------------

def test_second_failure_infra_only_escalates():
    result = run_verdict(base(rounds="plan-critic=1/3(0f,1i) ci=2/3(0f,2i)"))
    assert_verdict(result, "escalate", "infra-only")


def test_second_failure_empty_rounds_escalates():
    assert_verdict(run_verdict(base(rounds="")), "escalate", "infra-only")


def test_second_failure_triage_spent_escalates():
    result = run_verdict(base(triage_spent=True))
    assert_verdict(result, "escalate", "triage-spent")


def test_no_terminal_event_after_retry_escalates():
    result = run_verdict(base(terminal="none"))
    assert_verdict(result, "escalate", "no-terminal-event")


def test_ceiling_reached_escalates():
    result = run_verdict(base(sessions=3, failures=3))
    assert_verdict(result, "escalate", "ceiling-reached")


def test_ceiling_beats_first_failure_retry():
    """A first failed ending can still be the third session (triage
    re-dispatch plus rebase retry consumed the others): another session
    would break the hard ceiling, so escalate."""
    result = run_verdict(base(sessions=3, failures=1))
    assert_verdict(result, "escalate", "ceiling-reached")


def test_token_without_parens_counts_as_zero_findings():
    result = run_verdict(base(rounds="test-critic=0/3 plan-critic=2/3"))
    assert_verdict(result, "escalate", "infra-only")


# --- R4: invalid input --------------------------------------------------------

@pytest.mark.parametrize(
    "payload,label",
    [
        ([1, 2], "list payload"),
        (base(failures=0), "failures zero"),
        (base(sessions="2"), "string sessions"),
        (base(failures=True), "bool failures"),
        (base(sessions=0), "sessions zero"),
        ({k: v for k, v in base().items() if k != "triage_spent"}, "triage_spent missing"),
        (base(triage_spent=1), "triage_spent int"),
        (base(terminal="blocked"), "unknown terminal"),
        (base(rounds=5), "non-string rounds"),
        (base(rounds="review=two/3"), "malformed rounds token"),
    ],
)
def test_invalid_input_exits_1(payload, label):
    result = run_verdict(payload)
    ctx = f"[{label}] stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.returncode == 1, ctx
    assert line_value(result.stdout, "error") is not None, ctx
    assert line_value(result.stdout, "verdict") is None, ctx


def test_invalid_json_exits_1():
    result = run_verdict(None, raw="{")
    ctx = f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.returncode == 1, ctx
    assert line_value(result.stdout, "error") is not None, ctx
    assert line_value(result.stdout, "verdict") is None, ctx
