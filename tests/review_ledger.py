#!/usr/bin/env python3
"""Phase 5: the review-to-revision ledger. Append-only, verbatim, never overwrites.

Three rules this enforces, each because the alternative loses information you
need later:

  1. NOTES ARE KEPT VERBATIM. A paraphrase is a lossy summary written before you
     know which detail mattered. When a revision comes back wrong, the reviewer's
     exact words are the only thing that settles what was asked.
  2. A CHANGE REQUEST MAKES THE NEXT VERSION, NEVER AN OVERWRITE. Overwriting
     destroys the thing the note refers to, so the note stops making sense and
     the history of a shot becomes unreadable.
  3. AMBIGUOUS IS ITS OWN BUCKET AND NEVER COUNTS AS DONE. This site has
     `appcbb` (Approved CBB) and `appgra` (Approved with Grading Note): both say
     "approved" and both carry outstanding work. Folding them into approved is
     how a shot ships with a note still open.

The status map is read from THIS site, not from ShotGrid defaults. The canonical
reference guessed rev/apr/vwd/na; the real list has 14 values and no `vwd` at
all. Verified with schema_field_read on 2026-08-26.
"""
import json
import os
import sys
from datetime import datetime, timezone

# Verified against your-tracker.example.com, 2026-08-26.
APPROVED = {"apr": "Approved", "ad": "Approved by Director", "fin": "Final",
            "paf": "Presented as Final", "dlvr": "Delivered"}
# Approved WITH outstanding work. Deliberately not "approved".
PROVISIONAL = {"appcbb": "Approved CBB", "appgra": "Approved with Grading Note"}
NEEDS_REVISION = {"rrq": "Revision Requested", "rjct": "Rejected",
                  "tekfix": "Pending Tech Fix"}
PENDING = {"rev": "Pending Wangle Review", "pf": "Pending Client Feedback"}
INACTIVE = {"na": "N/A", "omt": "Omit"}


def classify(status):
    if status in APPROVED:
        return "approved"
    if status in PROVISIONAL:
        return "provisional"
    if status in NEEDS_REVISION:
        return "needs_revision"
    if status in PENDING:
        return "pending"
    if status in INACTIVE:
        return "inactive"
    # An unknown value is NOT assumed benign. It goes to provisional, which
    # never counts as done, so a new studio status cannot silently ship a shot.
    return "provisional"


def load(path):
    if not os.path.exists(path):
        return {"shot": None, "entries": [], "seen_note_ids": []}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path, led):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(led, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)          # atomic: a crash mid-write cannot truncate the ledger


def append(led, kind, **fields):
    """APPEND ONLY. Nothing in this module ever edits or removes a prior entry."""
    entry = {"kind": kind, "recorded_at": datetime.now(timezone.utc).isoformat()}
    entry.update(fields)
    led["entries"].append(entry)
    return entry


def next_version_number(led):
    """The next version is always one past the highest ever SEEN, not one past
    the highest still alive. A deleted v002 must not let a new render reuse
    v002, because the reviewer's note about the old v002 still exists."""
    highest = 0
    for e in led["entries"]:
        n = e.get("version_number")
        if isinstance(n, int) and n > highest:
            highest = n
    return highest + 1


def is_done(led):
    """A shot is done only on a real approval with no unresolved change request
    after it. Provisional never qualifies."""
    state = None
    for e in led["entries"]:
        if e["kind"] == "status":
            state = classify(e["status"])
        elif e["kind"] == "note" and e.get("requests_change"):
            state = "needs_revision"
    return state == "approved"


def summarise(led):
    print("shot: %s" % led.get("shot"))
    print("entries: %d   notes seen: %d" % (len(led["entries"]), len(led["seen_note_ids"])))
    for e in led["entries"]:
        if e["kind"] == "version":
            print("  v%03d  version %s (sg id %s)" % (e["version_number"], e["code"], e.get("sg_id")))
        elif e["kind"] == "status":
            print("        status -> %-7s (%s)" % (e["status"], classify(e["status"])))
        elif e["kind"] == "note":
            flag = "CHANGE REQUESTED" if e.get("requests_change") else "comment"
            print("        note [%s] by %s: %r" % (flag, e.get("author"), e["content_verbatim"][:70]))
    print("")
    print("DONE: %s   next version would be v%03d" % (is_done(led), next_version_number(led)))


CHANGE_WORDS = ("please", "can you", "could you", "needs", "fix", "change", "redo",
                "again", "instead", "revise", "adjust", "too ", "more ", "less ")


def note_requests_change(text):
    low = text.lower()
    return any(w in low for w in CHANGE_WORDS)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        led = {"shot": "SELFTEST", "entries": [], "seen_note_ids": []}
        append(led, "version", version_number=1, code="SELFTEST_v001", sg_id=1)
        append(led, "status", status="rev")
        assert not is_done(led), "pending must not be done"
        append(led, "status", status="appcbb")
        assert classify("appcbb") == "provisional", "appcbb must be provisional"
        assert not is_done(led), "PROVISIONAL MUST NEVER COUNT AS DONE"
        print("self-test: appcbb (Approved CBB) -> provisional, not done")
        append(led, "status", status="unknown_new_status")
        assert not is_done(led), "an unknown status must not count as done"
        print("self-test: unknown status -> provisional, not done")
        append(led, "status", status="apr")
        assert is_done(led), "apr should close the shot"
        print("self-test: apr -> approved, done")
        append(led, "note", content_verbatim="Please make the car red again.",
               author="reviewer", requests_change=True)
        assert not is_done(led), "a change request AFTER approval must reopen"
        print("self-test: change request after approval reopens the shot")
        assert next_version_number(led) == 2
        print("self-test: next version is v002")
        print("self-test PASSED")
        sys.exit(0)
    print(__doc__)
