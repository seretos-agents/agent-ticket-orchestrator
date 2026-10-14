#!/usr/bin/env python3
"""
Deterministic verdict for a package session that ended `failed` or with no
terminal event (#93, code half of #92): `retry`, `triage` or `escalate`.

Mirrors the `failed`/no-terminal-event reaction in `skills/run/SKILL.md` step
2c and its "Hard ceiling: at most three package sessions per package per run"
and "Triage once per package per run" rules. #92's prose half wires this
script into the skill. Like `todo-verdict.py` it does no I/O of its own: every
fact arrives as stdin JSON, which makes it a pure function a test can pin
down exactly.

Usage: no arguments. Reads one JSON object from stdin:

  {"failures": 2, "terminal": "failed",
   "rounds": "plan-critic=1/3(1f,0i) review=2/3(2f,0i) rebase=0/3(0f,0i)",
   "triage_spent": false, "sessions": 2}

  `failures`     required int >= 1: `failed`/no-terminal-event session
                 endings for this package in this run, including this one.
  `terminal`     required, "failed" | "none": whether the session just read
                 ended on a `failed` event or on no terminal event.
  `rounds`       optional string, default "": the latest `adev:event`'s
                 `rounds:` value verbatim. Whitespace-separated tokens
                 `<gate>=<used>/<cap>(<f>f,<i>i)`; a token without the
                 parenthesised split counts as 0 findings. Any gate counts,
                 `rebase` included. A malformed token is invalid input.
  `triage_spent` required bool: a `## Blocked triage (run)` comment exists
                 for this package in this run (a split session consumes the
                 same slot).
  `sessions`     required int >= 1: package sessions this package has had in
                 this run, including the one just ended.

Decision, first match wins:
  sessions >= 3            -> escalate / ceiling-reached
  failures == 1            -> retry    / first-failure
  terminal == "none"       -> escalate / no-terminal-event
  triage_spent             -> escalate / triage-spent
  no gate with f > 0       -> escalate / infra-only
  otherwise                -> triage   / findings-rounds

stdout, `key: value` lines: `verdict: retry | triage | escalate`, then
`reason: <token>`. Exit 0 for every verdict (the caller acts on `verdict:`
alone); exit 1 on invalid input with a single `error: <what>` line and no
`verdict:` line.
"""

import json
import re
import sys

SESSION_CEILING = 3

_ROUND_RE = re.compile(r"^[a-z][a-z0-9-]*=\d+/\d+(?:\((\d+)f,(\d+)i\))?$")


class InvalidInput(ValueError):
    """Raised for any stdin shape the contract rejects; message becomes the
    `error: <what>` stdout line."""


def _positive_int(payload, key):
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise InvalidInput(f"{key} must be an int >= 1, got {value!r}")
    return value


def _parse(payload):
    if not isinstance(payload, dict):
        raise InvalidInput("input must be a JSON object")

    failures = _positive_int(payload, "failures")
    sessions = _positive_int(payload, "sessions")

    terminal = payload.get("terminal")
    if terminal not in ("failed", "none"):
        raise InvalidInput(f'terminal must be "failed" or "none", got {terminal!r}')

    triage_spent = payload.get("triage_spent")
    if not isinstance(triage_spent, bool):
        raise InvalidInput("triage_spent must be a bool")

    rounds = payload.get("rounds", "")
    if not isinstance(rounds, str):
        raise InvalidInput("rounds must be a string")

    has_findings = False
    for token in rounds.split():
        m = _ROUND_RE.match(token)
        if not m:
            raise InvalidInput(f"malformed rounds token {token!r}")
        if m.group(1) is not None and int(m.group(1)) > 0:
            has_findings = True

    return failures, terminal, triage_spent, sessions, has_findings


def _decide(failures, terminal, triage_spent, sessions, has_findings):
    if sessions >= SESSION_CEILING:
        return "escalate", "ceiling-reached"
    if failures == 1:
        return "retry", "first-failure"
    if terminal == "none":
        return "escalate", "no-terminal-event"
    if triage_spent:
        return "escalate", "triage-spent"
    if not has_findings:
        return "escalate", "infra-only"
    return "triage", "findings-rounds"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}")
        return 1

    try:
        facts = _parse(payload)
    except InvalidInput as exc:
        print(f"error: {exc}")
        return 1

    verdict, reason = _decide(*facts)
    print(f"verdict: {verdict}")
    print(f"reason: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
