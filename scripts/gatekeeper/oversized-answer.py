#!/usr/bin/env python3
"""
Deterministic reader for an oversized pair's answer (#103, code half).

The gatekeeper posts an oversized pair's question once (SKILL.md Step 2).
Reading the owner's free-text choice and the proposal's one-line slice list
back out of comments is LLM bookkeeping of the kind `relation-readback.py`
replaced, so this script decides it: a pure stdin-JSON -> stdout
`key: value` process, outside the LLM.

Usage: no arguments. Reads one JSON object from stdin:

  {"comments": [{"ticket": "#5", "id": "c1",
                 "created_at": "2026-11-01T10:00:00Z",
                 "body": "<FULL body>"}, ...]}

`comments` are the comments of BOTH tickets of the pair. Bodies must be the
full text -- a truncated proposal loses its slice line. Order in the array
does not matter; comments are ordered by `created_at`.

Rules:
  * Applied record: any `gatekeeper:oversized v1` block naming the same
    `pair:` with a non-empty `created:` key (`created: #12,#13`, or `none`
    when the cut only re-scoped the two tickets). Checked first -> the cut
    happens once.
  * Proposal: the latest `gatekeeper:oversized v1` block without `created:`
    whose comment's ticket equals its `proposal_on:`. Its `(a) Re-cut ...`
    line carries `<n>. <slice> -- a user can then: <observable>
    (serves #x[, #y])` entries separated by ` . ` (middle dot).
  * Reply: a comment newer than the proposal, on either ticket, with no
    `gatekeeper:`/`adev:event`/`ato:event` block, no `## ... (gatekeeper)` /
    `(run)` heading and no `#ai-generated` marker. The latest reply that
    names a choice decides; several distinct letters in it -> unclear.
    Replies exist but none names a choice -> unclear.

stdout (exit 0 on a valid payload, 1 on invalid JSON / shape):
  verdict: apply|nothing
  reason: chose-a|chose-b|chose-c|unclear|no-reply|no-question|no-proposal|applied
  pair: #a,#b
  proposal_on: #a
  reply: <comment id or empty>
  slices: <n>
  slice_<i>: ...   observable_<i>: ...   covers_<i>: #x,#y   (apply only)
"""

import json
import re
import sys
from datetime import datetime, timezone

BLOCK_RE = re.compile(r"<!--\s*gatekeeper:oversized v1\s*\n(.*?)-->", re.S)
MACHINE_RE = re.compile(r"<!--\s*(?:gatekeeper:|adev:event|ato:event)")
HEADING_RE = re.compile(r"^#{1,6}\s.*\((?:gatekeeper|run)\)\s*$", re.M)
OPTION_RE = re.compile(r"option\s*\(?([abc])\)?(?![A-Za-z0-9])", re.I)
PAREN_RE = re.compile(r"\(([abc])\)", re.I)
BARE_RE = re.compile(r"^([abc])\s*[\).,:!]*\s*$", re.I)
SLICE_RE = re.compile(
    r"\d+\.\s+(.+?)\s+—\s+a user can then:\s+(.+?)\s*\(serves\s+([^)]*)\)\s*$"
)


class BadPayload(Exception):
    pass


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as e:
        raise BadPayload(f"bad created_at {value!r}") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def block_keys(text):
    keys = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            keys[k.strip()] = v.strip()
    return keys


def is_reply(body):
    return not (MACHINE_RE.search(body) or HEADING_RE.search(body)
                or "#ai-generated" in body)


def letters(body):
    found = {m.lower() for m in OPTION_RE.findall(body)}
    found |= {m.lower() for m in PAREN_RE.findall(body)}
    first = body.strip().splitlines()[0] if body.strip() else ""
    m = BARE_RE.match(first.strip())
    if m:
        found.add(m.group(1).lower())
    return found


def parse_slices(body):
    for line in body.splitlines():
        if re.match(r"\s*-\s*\(a\)", line):
            out = []
            for part in line.split(" · "):
                m = SLICE_RE.search(part)
                if m:
                    covers = re.findall(r"#\d+", m.group(3))
                    out.append((m.group(1).strip(), m.group(2).strip(), covers))
            return out
    return []


def emit(verdict, reason, pair="", proposal_on="", reply="", slices=()):
    print(f"verdict: {verdict}")
    print(f"reason: {reason}")
    print(f"pair: {pair}")
    print(f"proposal_on: {proposal_on}")
    print(f"reply: {reply}")
    print(f"slices: {len(slices)}")
    for i, (s, o, cv) in enumerate(slices, 1):
        print(f"slice_{i}: {s}")
        print(f"observable_{i}: {o}")
        print(f"covers_{i}: {','.join(cv)}")


def decide(comments):
    items = []
    for c in comments:
        if not isinstance(c, dict):
            raise BadPayload("comment is not an object")
        items.append((parse_time(c["created_at"]), c))
    items.sort(key=lambda t: t[0])

    proposal = None  # (time, pair, proposal_on, body)
    applied_pairs = set()
    for t, c in items:
        for raw in BLOCK_RE.findall(c.get("body", "")):
            keys = block_keys(raw)
            pair = keys.get("pair", "")
            if keys.get("created"):
                applied_pairs.add(pair)
            elif keys.get("proposal_on") and c.get("ticket") == keys["proposal_on"]:
                proposal = (t, pair, keys["proposal_on"], c.get("body", ""))
    if proposal is None:
        return emit("nothing", "no-question")
    ptime, pair, on, pbody = proposal
    if pair in applied_pairs:
        return emit("nothing", "applied", pair, on)

    replies = [(t, c) for t, c in items if t > ptime and is_reply(c.get("body", ""))]
    if not replies:
        return emit("nothing", "no-reply", pair, on)
    deciding = None
    for t, c in replies:
        if letters(c.get("body", "")):
            deciding = c
    if deciding is None:
        return emit("nothing", "unclear", pair, on, replies[-1][1].get("id", ""))
    chosen = letters(deciding["body"])
    rid = deciding.get("id", "")
    if len(chosen) > 1:
        return emit("nothing", "unclear", pair, on, rid)
    letter = next(iter(chosen))
    if letter != "a":
        return emit("nothing", f"chose-{letter}", pair, on, rid)
    slices = parse_slices(pbody)
    if not slices:
        return emit("nothing", "no-proposal", pair, on, rid)
    return emit("apply", "chose-a", pair, on, rid, slices)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        comments = payload["comments"]
        if not isinstance(comments, list):
            raise BadPayload("comments is not a list")
        decide(comments)
    except (ValueError, KeyError, TypeError, BadPayload) as e:
        print(f"invalid payload: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
