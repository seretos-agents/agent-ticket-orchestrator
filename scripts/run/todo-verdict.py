#!/usr/bin/env python3
"""
Deterministic runnable/blocked verdict over a project's Todo column (#86,
code half of a lane split whose prose half is #87).

Mirrors `skills/run/SKILL.md` Step 1a ("Order Todo by dependency", lines
~140-176) and "When is a blocker resolved" (~184-203), with one documented
tightening -- see "Differs from SKILL.md Step 1a step 6" below. #87's
continuation loop calls this script after every terminal `adev:event` to
decide whether another package can be dispatched without a human noticing a
mid-run split's two fresh tickets sitting untouched in Todo.

This script does no I/O of its own (no ticket reads, no filesystem, no
network) -- every fact it needs (which tickets are in Todo, which blocker
ids are already closed, which tickets the caller has permanently given up
on this run) arrives as stdin JSON, which is what makes it a pure function
a test can pin down exactly.

Usage: no arguments. Reads one JSON object from stdin:

  {"todo":   [{"id": 81, "blocked_by": [], "skip": ""},
              {"id": 80, "blocked_by": [81]}],   # board order, oldest first
   "closed": [12, 81]}                            # blocker ids the caller
                                                    # knows are closed

  `todo` (required, list): the Todo column, board order, oldest first. Each
  entry is an object:
    `id`         required, positive int, unique within `todo`.
    `blocked_by` optional, list of blocker ids, default `[]`.
    `skip`       optional string, default `""`.

  `skip` is a free-form, caller-supplied string naming ANY reason this
  ticket is permanently un-runnable for the rest of this run -- it is not
  limited to the two examples `skills/run/SKILL.md` happens to name
  (`prose lane not installed`, `branch check failed: <line>`). A future
  caller (#87) may supply any other reason it discovers at dispatch time --
  e.g. `per-run session bounds exhausted` -- and this script echoes
  whatever string it is given verbatim, without interpreting or validating
  its vocabulary.

  `closed` (optional, list of ints, default `[]`): blocker ids the caller
  already knows are closed (from `done_this_run` or a fresh `get_ticket`
  read) -- this script never looks any of this up itself.

Classification, in order:
  1. drop edges to blockers in `closed`
  2. remaining blocker in `todo` -> INTERNAL edge; otherwise EXTERNAL-OPEN
  3. SKIPPED, in precedence:
     - non-empty `skip` -> `skipped: <skip>`
     - else first EXTERNAL-OPEN blocker (in `blocked_by` order) ->
       `skipped: blocked by #<b> (not closed)`
     - else (to a fixpoint) any SKIPPED INTERNAL blocker, first in
       `blocked_by` list order -> `skipped: blocker #<b> skipped`
  4. Kahn topological order over the rest, board-order tie-break
  5. cycle members / their dependents: appended in board order at the end,
     never dropped

Differs from SKILL.md Step 1a step 6: step 6 only names "a package whose
ONLY blocker was itself SKIPPED". That wording is silent on a ticket with
several blockers where just one of them is SKIPPED and another is still
open -- and every reading of that silence would dispatch a package with an
unresolved open blocker, contradicting "When is a blocker resolved" (a
blocker is resolved exactly when closed; a SKIPPED blocker stays open for
the rest of the run) and step 4's own "any EXTERNAL-OPEN blocker" rule. This
script therefore tightens step 6 to "ANY SKIPPED blocker, not only the sole
one" -- deliberately, not an oversight. This script is the one that governs
that behaviour; aligning SKILL.md Step 1a's wording to match is #87's.

stdout, `key: value` lines (the same dumb-reader convention as `adev:event`
and `relation-readback.py`'s `verdict:`/`gap targets:`), always in this
order:

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
entry -- a `runnable` ticket whenever one exists, else the board-first cycle
member. `verdict: none` iff `todo` is empty (`none: todo-empty`) or every
ticket ended up SKIPPED (`none: all-skipped`).

**Only `next:` is meant to be acted on.** `order`, `ticket:` and `cycle:`
lines are diagnostics for a human or a log to read -- never a precomputed
queue for the caller to walk. The caller (#87's continuation loop) must
call this script again, from scratch, after every terminal event (a merge,
a close, a fresh skip) rather than reusing a prior `order:`/`ticket:`
snapshot: closing one blocker can change every other ticket's
classification (SKIPPED -> runnable, EXTERNAL-OPEN -> INTERNAL, etc.), and
this script has no way to tell a stale snapshot from a fresh one -- that
staleness check is the caller's responsibility, discharged simply by never
keeping the output around past the next terminal event.

exit code: 0 on dispatch, 2 on none, 1 on invalid input (not a JSON object;
`todo`/`closed` not lists; a ticket missing/non-int/duplicate `id`; non-list
`blocked_by`; non-string `skip`) with `error: <what>` as a stdout line and
no `verdict:` line at all.
"""

import json
import sys


class InvalidInput(ValueError):
    """Raised for any stdin shape the contract rejects; message becomes the
    `error: <what>` stdout line."""


def _parse(payload):
    if not isinstance(payload, dict):
        raise InvalidInput("input must be a JSON object")

    raw_todo = payload.get("todo")
    if not isinstance(raw_todo, list):
        raise InvalidInput("todo must be a list")

    raw_closed = payload.get("closed", [])
    if not isinstance(raw_closed, list):
        raise InvalidInput("closed must be a list")
    for c in raw_closed:
        if not isinstance(c, int) or isinstance(c, bool):
            raise InvalidInput(f"closed entries must be ints, got {c!r}")

    seen_ids = set()
    tickets = []
    for entry in raw_todo:
        if not isinstance(entry, dict):
            raise InvalidInput(f"todo entry must be an object, got {entry!r}")

        tid = entry.get("id")
        if not isinstance(tid, int) or isinstance(tid, bool) or tid <= 0:
            raise InvalidInput(f"ticket id must be a positive int, got {tid!r}")
        if tid in seen_ids:
            raise InvalidInput(f"duplicate ticket id #{tid}")
        seen_ids.add(tid)

        blocked_by = entry.get("blocked_by", [])
        if not isinstance(blocked_by, list):
            raise InvalidInput(f"blocked_by for #{tid} must be a list")
        for b in blocked_by:
            if not isinstance(b, int) or isinstance(b, bool):
                raise InvalidInput(
                    f"blocked_by entries for #{tid} must be ints, got {b!r}"
                )

        skip = entry.get("skip", "")
        if not isinstance(skip, str):
            raise InvalidInput(f"skip for #{tid} must be a string")

        tickets.append({"id": tid, "blocked_by": blocked_by, "skip": skip})

    return tickets, set(raw_closed)


def _classify_skips(tickets, todo_ids, closed_set):
    """Returns {id: reason} for every SKIPPED ticket, computed to a fixpoint
    for the transitive-skip rule (see module docstring, "Differs from
    SKILL.md Step 1a step 6")."""
    skip_reason = {}

    # Rule 1: explicit caller-supplied skip, highest precedence.
    for t in tickets:
        if t["skip"]:
            skip_reason[t["id"]] = t["skip"]

    # Rule 2: first EXTERNAL-OPEN blocker (not closed, not in todo).
    for t in tickets:
        tid = t["id"]
        if tid in skip_reason:
            continue
        for b in t["blocked_by"]:
            if b in closed_set:
                continue
            if b not in todo_ids:
                skip_reason[tid] = f"blocked by #{b} (not closed)"
                break

    # Rule 3: transitive skip -- any SKIPPED INTERNAL blocker, to a
    # fixpoint (a chain of several transitively-skipped tickets needs more
    # than one pass to fully propagate).
    changed = True
    while changed:
        changed = False
        for t in tickets:
            tid = t["id"]
            if tid in skip_reason:
                continue
            for b in t["blocked_by"]:
                if b in closed_set:
                    continue
                if b not in todo_ids:
                    continue  # already covered by rule 2
                if b in skip_reason:
                    skip_reason[tid] = f"blocker #{b} skipped"
                    changed = True
                    break

    return skip_reason


def _topological_order(tickets, active_ids, closed_set):
    """Kahn's algorithm, board-order tie-break, over the active (non-SKIPPED)
    tickets. Returns (emitted, leftover) -- both lists of ids in the order
    Step 1a step 5/7 requires: `emitted` cleanly resolved in dependency
    order, `leftover` (cycle members and anything downstream of one)
    appended afterwards in board order, never dropped."""
    board_order_ids = [t["id"] for t in tickets]
    board_index = {tid: i for i, tid in enumerate(board_order_ids)}
    active_board_order = [tid for tid in board_order_ids if tid in active_ids]

    unresolved = {}
    for t in tickets:
        if t["id"] not in active_ids:
            continue
        unresolved[t["id"]] = [b for b in t["blocked_by"] if b not in closed_set]

    indegree = {tid: len(unresolved[tid]) for tid in active_board_order}
    dependents = {tid: [] for tid in active_board_order}
    for tid in active_board_order:
        for b in unresolved[tid]:
            dependents[b].append(tid)

    ready = [tid for tid in active_board_order if indegree[tid] == 0]
    ready.sort(key=lambda x: board_index[x])

    emitted = []
    emitted_set = set()
    while ready:
        current = ready.pop(0)
        emitted.append(current)
        emitted_set.add(current)
        for dep in dependents[current]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                ready.append(dep)
        ready.sort(key=lambda x: board_index[x])

    leftover = [tid for tid in active_board_order if tid not in emitted_set]
    return emitted, leftover, unresolved


def _find_cycles(leftover, unresolved):
    """Walks each leftover ticket, following its first still-unemitted
    blocker, until a node repeats -- the repeat marks a distinct cycle,
    printed once. A ticket downstream of a cycle (not itself part of the
    repeat) is walked into the already-found cycle and contributes no
    further line."""
    unemitted_set = set(leftover)
    processed = set()
    cycles = []

    for start in leftover:
        if start in processed:
            continue
        path = []
        index_in_path = {}
        current = start
        while True:
            if current in processed:
                break
            if current in index_in_path:
                start_idx = index_in_path[current]
                cycle_nodes = path[start_idx:] + [current]
                cycles.append(cycle_nodes)
                for n in path[start_idx:]:
                    processed.add(n)
                break
            if current not in unemitted_set:
                break
            index_in_path[current] = len(path)
            path.append(current)
            nxt = None
            for b in unresolved[current]:
                if b in unemitted_set:
                    nxt = b
                    break
            if nxt is None:
                break
            current = nxt

    return cycles


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON: {exc}")
        return 1

    try:
        tickets, closed_set = _parse(payload)
    except InvalidInput as exc:
        print(f"error: {exc}")
        return 1

    if not tickets:
        print("verdict: none")
        print("next: ")
        print("none: todo-empty")
        print("order: ")
        return 2

    todo_ids = {t["id"] for t in tickets}
    skip_reason = _classify_skips(tickets, todo_ids, closed_set)
    active_ids = {t["id"] for t in tickets if t["id"] not in skip_reason}

    emitted, leftover, unresolved = _topological_order(tickets, active_ids, closed_set)
    order_ids = emitted + leftover
    leftover_set = set(leftover)
    cycles = _find_cycles(leftover, unresolved)

    if order_ids:
        print("verdict: dispatch")
        print(f"next: #{order_ids[0]}")
    else:
        print("verdict: none")
        print("next: ")
        print("none: all-skipped")

    print(f"order: {' '.join(f'#{i}' for i in order_ids)}")

    for t in tickets:
        tid = t["id"]
        if tid in skip_reason:
            status = f"skipped: {skip_reason[tid]}"
        elif tid in leftover_set:
            status = "cycle"
        elif not unresolved.get(tid):
            status = "runnable"
        else:
            status = "waiting on " + " ".join(f"#{b}" for b in unresolved[tid])
        print(f"ticket: #{tid} {status}")

    for cycle_nodes in cycles:
        chain = " -> ".join(f"#{n}" for n in cycle_nodes)
        print(f"cycle: {chain}, processed in board order")

    return 0 if order_ids else 2


if __name__ == "__main__":
    sys.exit(main())
