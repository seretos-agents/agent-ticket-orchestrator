"""
Driving tests for `scripts/gatekeeper/oversized-answer.py` (#103, code half).

The gatekeeper posts an oversized pair's question once (SKILL.md Step 2,
"An oversized pair is a Question, not a cut"). Reading the owner's free-text
answer and the proposal's one-line slice list back out of comments is LLM
bookkeeping of exactly the kind `relation-readback.py` replaced, so this
script decides it: stdin JSON in, `key: value` lines out.

Contract pinned here:

  stdin:  {"comments": [{"ticket": "#5", "id": "c1",
                         "created_at": "2026-11-01T10:00:00Z",
                         "body": "<FULL body, never truncated>"}, ...]}
          the comments of BOTH tickets of the pair.
  stdout: verdict: apply|nothing
          reason: chose-a|chose-b|chose-c|unclear|no-reply|no-question|
                  no-proposal|applied
          pair: #a,#b          proposal_on: #a        reply: <comment id>
          slices: <n>   and, per slice, slice_<i> / observable_<i> / covers_<i>
  exit:   0 on a valid payload, 1 on invalid JSON / shape.

Decisions answering the plan-critic's notes:
  * the proposal's slice line is the REAL one SKILL.md Step 2 posts
    ("- (a) Re-cut into these slices *(recommended)* — 1. <slice> — a user
    can then: <observable> (serves #9, #14) · 2. ..."), copied below;
  * the latest reply that NAMES a choice decides (a later "thanks" does not
    cancel an earlier "option a"; a later "actually option b" does);
  * a reply is a human comment: comments carrying a machine block, a
    "(gatekeeper)"/"(run)" heading or the `#ai-generated` marker never count;
  * an applied-record is any `gatekeeper:oversized v1` block with a
    `created:` key for the same pair (its value may be `none` when the cut
    only re-scoped the two existing tickets), so the cut happens once.

R4 evidence note (test-critic round 2, F1): the plan's R4 (narrowing the
contract assertions in tests/test_pipeline_contract.py) is re-declared
`existing-suite`. It is prose presence in markdown, which the repo's contract
tests own; the implementer narrows those assertions and the existing contract
tests must stay green on the SKILL.md as edited. No driving test is added for it
(the earlier variant-splicing tests only checked strings they injected).

The script does not exist when these tests are first written (phase=tests):
RED is the missing `verdict:` line on stdout (the interpreter's "can't open
file" lands on stderr).
"""

import json
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gatekeeper" / "oversized-answer.py"

PROPOSAL = """## Clarification needed (gatekeeper)

### Q1 Two large overlapping tickets — cut them differently before they run?
**About:** #9 (VR rig) and #14 (teleport anchors) are both large and overlap in the rig motion code.
**Decision:** whether the two run as filed, one after the other, or are re-cut first into slices that each ship something a user can see.
- (a) Re-cut into these slices *(recommended)* — 1. Rig stands and moves — a user can then: look around and walk in VR (serves #9, #14) · 2. Teleport to an anchor — a user can then: point at an anchor and arrive there (serves #14)
- (b) Run both as filed, in dependency order — the overlap is built once in the first and re-touched by the second.
- (c) I cut them myself — reply here once the tickets are edited.

<!-- gatekeeper:oversized v1
pair: #9,#14
proposal_on: #9
-->"""

POINTER = """## Clarification needed (gatekeeper)

This ticket and #9 are asked about together — the question and its proposal are on #9; answer there.

<!-- gatekeeper:oversized v1
pair: #9,#14
proposal_on: #9
-->"""

NO_SLICES = PROPOSAL.replace(
    PROPOSAL[PROPOSAL.index("- (a) Re-cut"):PROPOSAL.index("- (b)")], ""
)


def c(ticket, cid, minute, body):
    return {"ticket": ticket, "id": cid, "created_at": f"2026-11-01T10:{minute:02d}:00Z",
            "body": body}


def base(extra=()):
    return [c("#9", "p1", 0, PROPOSAL), c("#14", "p2", 1, POINTER), *extra]


def run(comments):
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps({"comments": comments}),
        capture_output=True, text=True,
    )


def parse(result):
    out = {}
    for line in result.stdout.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
        elif line.endswith(":"):
            out[line[:-1].strip()] = ""
    return out


def where(result):
    return f"stdout={result.stdout!r} stderr={result.stderr!r}"


@pytest.mark.parametrize("reply_text", [
    "Option a", "(a)", "a", "option (A), go ahead", "Option a.\n\nPlease start with the rig.",
])
def test_plain_option_a_reply_applies_the_proposal(reply_text):
    r = run(base([c("#9", "r1", 5, reply_text)]))
    out = parse(r)
    assert out.get("verdict") == "apply", where(r)
    assert out.get("reason") == "chose-a", where(r)
    assert out.get("reply") == "r1", where(r)
    assert out.get("pair") == "#9,#14" and out.get("proposal_on") == "#9", where(r)
    assert out.get("slices") == "2", where(r)
    assert out.get("slice_1") == "Rig stands and moves", where(r)
    assert out.get("observable_1") == "look around and walk in VR", where(r)
    assert out.get("covers_1") == "#9,#14", where(r)
    assert out.get("slice_2") == "Teleport to an anchor", where(r)
    assert out.get("observable_2") == "point at an anchor and arrive there", where(r)
    assert out.get("covers_2") == "#14", where(r)
    assert r.returncode == 0, r.stderr


def test_a_reply_on_the_pointer_ticket_counts():
    r = run(base([c("#14", "r1", 5, "Option a")]))
    assert parse(r).get("verdict") == "apply", where(r)
    assert parse(r).get("reply") == "r1", where(r)


def test_a_later_comment_without_a_choice_does_not_cancel_option_a():
    r = run(base([c("#9", "r1", 5, "Option a"), c("#9", "r2", 9, "thanks, the deadline is Friday")]))
    out = parse(r)
    assert out.get("verdict") == "apply" and out.get("reply") == "r1", where(r)


def test_a_later_reply_repeating_a_decides_with_its_own_id():
    r = run(base([c("#9", "r1", 5, "Option b"), c("#14", "r2", 9, "On reflection, option a")]))
    out = parse(r)
    assert out.get("verdict") == "apply" and out.get("reason") == "chose-a", where(r)
    assert out.get("reply") == "r2", where(r)


def test_a_later_reply_naming_another_choice_overrides_an_earlier_a():
    r = run(base([c("#9", "r1", 5, "Option a"), c("#14", "r2", 9, "Actually, option b")]))
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "chose-b", where(r)
    assert out.get("reply") == "r2", where(r)
    assert not any(k.startswith("slice_") for k in out), where(r)


def test_option_a_with_no_slices_in_the_proposal_applies_nothing():
    r = run([c("#9", "p1", 0, NO_SLICES), c("#14", "p2", 1, POINTER), c("#9", "r1", 5, "a")])
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "no-proposal", where(r)


@pytest.mark.parametrize("reply_text,reason", [
    ("Option b", "chose-b"),
    ("(c) I'll cut them myself", "chose-c"),
    ("b", "chose-b"),
    ("a or b, not sure", "unclear"),
    ("Option a or option b", "unclear"),
    ("I think a split is fine", "unclear"),
    ("A split is premature", "unclear"),
    ("A third option would suit me better", "unclear"),
])
def test_non_a_answers_apply_nothing(reply_text, reason):
    r = run(base([c("#9", "r1", 5, reply_text)]))
    out = parse(r)
    assert out.get("verdict") == "nothing", where(r)
    assert out.get("reason") == reason, where(r)
    assert out.get("reply") == "r1", where(r)
    assert not any(k.startswith("slice_") for k in out), where(r)
    assert r.returncode == 0, r.stderr


def test_no_reply_is_nothing():
    r = run(base())
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "no-reply", where(r)


@pytest.mark.parametrize("machine_body", [
    "## Frame (gatekeeper)\n\nok\n\n<!-- gatekeeper:frame v1\nac_rewritten: no\n-->",
    "Started.\n\n<!-- adev:event v1\nevent: started\n-->",
    "Escalated: x\n\n<!-- ato:event v1\nevent: escalated\n-->",
    "## Blocked triage (run)\n\nOption (a) is the answer",
    "Option a\n\n#ai-generated",
])
def test_pipeline_and_ai_comments_are_not_replies(machine_body):
    r = run(base([c("#9", "r1", 5, machine_body)]))
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "no-reply", where(r)


def test_a_reply_older_than_the_proposal_is_not_an_answer():
    r = run([c("#9", "r0", 0, "Option a"), c("#9", "p1", 3, PROPOSAL), c("#14", "p2", 4, POINTER)])
    out = parse(r)
    assert out.get("reason") == "no-reply", where(r)


def test_applied_pair_returns_nothing():
    done = c("#9", "f1", 12, "## Re-cut applied (gatekeeper)\n\n<!-- gatekeeper:oversized v1\n"
                              "pair: #9,#14\ncreated: #12,#13\n-->")
    r = run(base([c("#9", "r1", 5, "Option a"), done]))
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "applied", where(r)
    assert not any(k.startswith("slice_") for k in out), where(r)


def test_applied_record_without_new_tickets_still_counts():
    done = c("#14", "f1", 12, "<!-- gatekeeper:oversized v1\npair: #9,#14\ncreated: none\n-->")
    r = run(base([c("#9", "r1", 5, "Option a"), done]))
    assert parse(r).get("reason") == "applied", where(r)


def test_applied_record_for_a_different_pair_does_not_suppress_apply():
    other = c("#9", "f1", 12, "<!-- gatekeeper:oversized v1\npair: #20,#21\ncreated: #22\n-->")
    r = run(base([c("#9", "r1", 5, "Option a"), other]))
    assert parse(r).get("verdict") == "apply", where(r)


def test_no_oversized_block_is_no_question():
    r = run([c("#9", "x1", 0, "just a comment"), c("#9", "r1", 5, "Option a")])
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "no-question", where(r)


def test_invalid_json_exits_one():
    r = subprocess.run([sys.executable, str(SCRIPT)], input="not json",
                       capture_output=True, text=True)
    assert r.returncode == 1, where(r)


PROPOSAL_2 = """## Clarification needed (gatekeeper)

### Q1 Two large overlapping tickets - cut them differently before they run?
**About:** #21 (importer) and #33 (exporter) are both large and share the codec.
**Decision:** whether to re-cut.
- (a) Re-cut into these slices *(recommended)* — 1. Read a file — a user can then: open a saved project (serves #21) · 2. Write a file — a user can then: save a project and reopen it (serves #21, #33) · 3. Export to CSV — a user can then: open the data in a spreadsheet (serves #33)
- (b) Run both as filed, in dependency order — overlap.
- (c) I cut them myself — reply here.

<!-- gatekeeper:oversized v1
pair: #21,#33
proposal_on: #21
-->"""

POINTER_2 = POINTER.replace("#9", "#21").replace("#14", "#33").replace("pair: #21,#33", "pair: #21,#33")


def test_a_second_proposal_is_parsed_not_hard_coded():
    r = run([c("#21", "p1", 0, PROPOSAL_2), c("#33", "p2", 1, POINTER_2),
             c("#33", "r7", 5, "Option a")])
    out = parse(r)
    assert out.get("verdict") == "apply" and out.get("reason") == "chose-a", where(r)
    assert out.get("pair") == "#21,#33" and out.get("proposal_on") == "#21", where(r)
    assert out.get("reply") == "r7", where(r)
    assert out.get("slices") == "3", where(r)
    assert [out.get(f"slice_{i}") for i in (1, 2, 3)] == [
        "Read a file", "Write a file", "Export to CSV"], where(r)
    assert [out.get(f"observable_{i}") for i in (1, 2, 3)] == [
        "open a saved project", "save a project and reopen it",
        "open the data in a spreadsheet"], where(r)
    assert [out.get(f"covers_{i}") for i in (1, 2, 3)] == ["#21", "#21,#33", "#33"], where(r)


@pytest.mark.parametrize("machine_body", [
    "<!-- gatekeeper:frame v1\nac_rewritten: no\n-->\n\nOption a",
    "Option a\n\n<!-- adev:event v1\nevent: started\n-->",
    "option (a)\n\n<!-- ato:event v1\nevent: escalated\n-->",
])
def test_machine_block_comments_with_choice_text_are_not_replies(machine_body):
    r = run(base([c("#9", "r1", 5, machine_body)]))
    out = parse(r)
    assert out.get("verdict") == "nothing" and out.get("reason") == "no-reply", where(r)


def test_ordering_follows_created_at_not_array_position():
    comments = [c("#9", "r2", 9, "Actually, option b"), c("#9", "r1", 5, "Option a"),
                c("#14", "p2", 1, POINTER), c("#9", "p1", 0, PROPOSAL)]
    out = parse(run(comments))
    assert out.get("verdict") == "nothing" and out.get("reason") == "chose-b"
    assert out.get("reply") == "r2"


def test_newer_than_proposal_is_by_timestamp_not_position():
    comments = [c("#9", "p1", 3, PROPOSAL), c("#14", "p2", 4, POINTER),
                c("#9", "r0", 0, "Option a")]
    out = parse(run(comments))
    assert out.get("reason") == "no-reply"
