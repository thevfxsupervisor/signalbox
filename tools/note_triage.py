#!/usr/bin/env python3
"""Stage 8: decide whether an operator's review note is a live revision
request.

REMOVED 2026-09-08, Geoff's direct instruction. This module used to invent
four "addressability" classes (post/take/asset/prompt) and route a note to
whichever one its words matched, on the theory that some notes could be
serviced cheaper than a full regeneration. His own 400-word framing note on
SHOW01_A_0210 was classified 'post-addressable', and serviced by nothing,
because it contained the word "colours" once - a word he had copied
verbatim from this module's own preserve-suffix wording. His words: "There
should be no other choices or decisions being made about notes. If an
operator gives a note, it needs to be addressed."

THE RULE NOW, and it is the whole specification: an ACTIONABLE operator
note is a revision request. There is no other live outcome (an EDIT of the
current panel is on the roadmap, not built today). No keyword decides
anything about WHICH kind of note this is: classify() no longer looks at
the words in a note at all, beyond whether there is any text.

WHAT STILL DECIDES SOMETHING, and it is not a class. is_actionable() and
is_pipeline_record() answer a different question: is this an operator
REQUEST at all, as opposed to a closed note, the pipeline's own [auto]
bookkeeping, or a note whose Versions are all settled. That distinction is
structural (ShotGrid state), never a word match on the note's text.

    python note_triage.py --classify "close-up, framed off centre"   one note
    python note_triage.py --sweep                    triage open notes in ShotGrid
    python note_triage.py --self-test
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_context as EPCTX                                # noqa: E402

PROJ = {"type": "Project", "id": 9999}
EP = EPCTX.resolve_episode()

PROMPT, NONE = "prompt-addressable", "unclassified"
VERDICT = {PROMPT: "revise prompt", NONE: "not reviewed"}

# THE ONLY THING classify() STILL MARKS, and it is not a fall-through among
# several classes any more - it is the sole reason classify() ever returns
# NONE: the note carried no text at all. was_unmatched() below still checks
# for this string; prompt_revision.py's rrq fall-through that used to
# consult it is gone (see revision_requested()'s docstring for why).
UNMATCHED_REASON = "empty note: no text to act on"


def was_unmatched(reasons):
    """-> True if classify() reached its fall-through rather than matching a
    rule. Keyed on the constant, never on a substring a reader might retype.

    KEPT FOR THIS MODULE'S OWN INTERNAL USE ONLY. The docstring used to say
    "prompt_revision still checks it", and that stopped being true when the
    rrq fall-through was removed: grep confirms the only caller is this file.
    It still earns its keep here, because a caller inside this module needs to
    tell "matched a rule" from "matched nothing" even though the fall-through
    returns NONE either way. Corrected 2026-09-09 by a grooming sweep."""
    return any(UNMATCHED_REASON in str(r) for r in (reasons or []))


def classify(text):
    """-> (class, [reasons]).

    AN ACTIONABLE OPERATOR NOTE IS A REVISION REQUEST. There is no other
    live outcome (edit-of-the-current-panel is on the roadmap, not built),
    and no keyword decides anything about WHICH kind of note this is --
    Geoff, 2026-09-08, after his own 400-word framing note on
    SHOW01_A_0210 was routed to a class nothing services because it
    contained the word "colours" once, copied verbatim from this module's
    own preserve-suffix wording. The post/take/asset-addressable classes
    this function used to choose among by matching words are removed; do
    not reintroduce keyword-based routing here.

    Whether a note is even an operator REQUEST at all -- as opposed to the
    pipeline's own [auto] bookkeeping, a closed note, or a note whose
    Versions are all settled -- is is_actionable()'s job, upstream of this
    call, never this function's. This function only answers whether there
    is any TEXT to act on."""
    low = " ".join((text or "").lower().split())
    if not low:
        return NONE, [UNMATCHED_REASON]
    return PROMPT, ["operator note: addressed as a revision request"]


# --- ShotGrid ---------------------------------------------------------------

def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def cmd_sweep(sg, dry):
    """CLI entry point: sweep this process's resolved episode, return an exit
    code. The work itself lives in sweep() so the standing service can call it
    per episode without inheriting this module's single default."""
    sweep(sg, dry=dry, episode_code=EP.code)
    return 0


# --- what counts as a REQUEST, and what is merely a RECORD -------------------
#
# GEOFF, 2026-09-04: "notes on rejected don't require acting, they are optional
# records of something."
#
# That one sentence is the fix for a defect this module had all day. A Shot
# accumulates notes forever -- review feedback, but also the pipeline's own
# bookkeeping ("PROVISIONAL D14 approval", "Approved design changed for ...").
# Feeding all of them to the classifier meant decisions already taken were
# being read as changes still wanted.
#
# A note is ACTIONABLE only if it hangs off a Version still awaiting a decision.
# Once that Version is approved, rejected or omitted, its notes are the record
# of why -- worth reading, never worth acting on. A note linked to the SHOT and
# to no Version at all stays actionable: there is no decision to have closed it.
# THE PIPELINE'S OWN BOOKKEEPING, MARKED AT THE SOURCE.
#
# The rule above (a note is settled once its Version is) has a hole, and the
# comment above names the exact notes that fall through it: the cascade's
# "Approved design changed for ..." notices hang off the SHOT and no Version, so
# nothing ever closes them and they stay actionable forever. Measured 2026-09-07:
# 29 actionable notes on SHOW01, of which 24 were the pipeline telling itself
# something it had just done. Each one is an LLM call the proposer then has to
# decline.
#
# AUTHOR IDENTITY DOES NOT SEPARATE THEM, which is the trap. Both the cascade and
# an OPERATOR working through ShotGrid's API write as the same ApiUser, so
# "ApiUser means record" would silence exactly the notes that must act. Measured:
# 58 of 60 recent notes are ApiUser-authored, including a genuine operator note.
#
# So the writer marks its own. A note whose subject starts with this prefix is
# the system talking to the operator, and is never a request TO the system.
AUTO_NOTE_PREFIX = "[auto] "

# THE VERSION STATUS SAYS WHETHER TO ACT. THE NOTE SAYS WHAT TO DO.
#
# Geoff, 2026-09-07, and it retires two heuristics of mine at once:
#
#   "It should be that one note is sent, but that more may be coming later.
#    More stakeholders, or just a pause in note taking. The operator is the one
#    to set the version's status to rrq, that is the signal that the notes
#    session is finished and they should be implemented."
#
#   "A note from an operator should only ever be a request. Not an approval,
#    that is done with status on a task. Possibly in a rare case a record, but
#    then the version is acted on according to its status."
#
# So a note ALONE is never the trigger. A review session accumulates notes from
# several people over time, and firing on the first one implements half a
# review. 'rrq' is the operator saying the session is finished.
#
# THIS REPLACES THE F243 BACKSTOP, which made a note NEWER THAN THE APPROVAL
# actionable by itself. That was built to make a sloppier gesture work and it
# fires exactly one note too early. Retracted; see F254.
#
# It also dissolves the F247/F251 mess from the other side. An approval
# rationale note is harmless now whatever it says, because the Version it hangs
# off is APPROVED, not 'rrq', so nothing acts on it. The classifier's job
# shrinks to what it was always good at: choosing WHICH treatment, never
# WHETHER.
LIVE_VERSION_STATUSES = ("rev", "rrq")     # Pending Wangle Review, Revision Requested

# AND THE NOTE'S OWN STATUS SAYS WHETHER IT IS A REQUEST AT ALL.
#
# Site valid_values for Note.sg_status_list, read from the schema 2026-09-08:
# 'opn' (Open), 'urr' (Under Revision), 'addr' (Addressed), 'clsd' (Closed),
# 'qu' (Question). Only ONE of those is an open request to this pipeline.
#
# THIS IS A WHITELIST ON PURPOSE, and it replaces a blocklist that named only
# 'clsd'. A blocklist is actionable-by-default, so every status added to the
# site later leaks straight into the request channel -- which is exactly what
# was about to happen: the panel-composition refusal path now writes a Note at
# 'urr' to tell the operator WHY a shot stopped, and under the old rule the
# proposer would have picked that note up and spent a `claude -p` call trying
# to implement the pipeline's own error message. That is F209 (24 of 29
# "actionable" notes were self-authored) re-created with a new status code.
#
# An EMPTY status stays actionable: nothing has closed it, and that is the
# safe direction for a note written by a tool that did not set the field.
#
# Measured on the live project before changing anything (303 Notes): 270
# 'clsd', 32 'opn', 1 'urr', 0 'addr', 0 'qu', 0 unset. The one 'urr' note is
# already non-actionable through its Version ('rjct'), so this rule changes
# the verdict on ZERO notes today. It is a guard against tomorrow.
ACTIONABLE_NOTE_STATUSES = ("opn",)

# AND THE HOLE THAT RULE LEFT, FOUND BY REHEARSING THE DEMO. "A note on a
# settled Version is a record" is right for a note that predates the decision,
# and exactly backwards for the operator gesture the whole demo is built on:
# looking at an APPROVED shot and asking for a change. That note is a REQUEST,
# it is what ShotGrid's own 'rrq' means, and until 2026-09-07 the loop filed it
# as history and did nothing. Measured by writing one on
# SHOW01_A_0050_PNL_panel_v001: triage read 83 notes, called it record-only, and
# no proposal was ever made.
#
# THE DISCRIMINATOR IS THE TIMESTAMP, the same one F241 needed: a note created
# AFTER the approval is asking to revise it; a note created before it is the
# record of why it was approved. An approval time that cannot be established
# leaves the note a record, which is the safe direction.
#
# Measured before changing anything: project-wide this flips exactly ONE note,
# because every other note on an approved Version is either the pipeline's own
# bookkeeping or predates its approval. It is not a floodgate.
APPROVED_VERSION_STATUSES = ("apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra")


def is_pipeline_record(note):
    """-> True for a note the PIPELINE wrote to report something it did."""
    return str((note or {}).get("subject") or "").startswith(AUTO_NOTE_PREFIX)


def approval_times(sg, version_ids):
    """-> {version id: when it was LAST approved}, from the event log.

    EventLogEntry is ShotGrid's own record of who changed what and when, so this
    asks the system rather than inferring from `updated_at`, which moves for any
    field. A Version with no approval event simply does not appear, and the
    caller must treat that as "unknown", never as "not approved"."""
    out = {}
    ids = [i for i in (version_ids or []) if i]
    if not ids:
        return out
    CHUNK = 200
    for i in range(0, len(ids), CHUNK):
        evs = sg.find("EventLogEntry",
                      [["event_type", "is", "Shotgun_Version_Change"],
                       ["attribute_name", "is", "sg_status_list"],
                       ["entity", "in", [{"type": "Version", "id": v}
                                         for v in ids[i:i + CHUNK]]]],
                      ["entity", "meta", "created_at"])
        for e in evs:
            m = e.get("meta") or {}
            ent = e.get("entity") or {}
            if m.get("new_value") in APPROVED_VERSION_STATUSES and ent.get("id"):
                cur = out.get(ent["id"])
                if cur is None or e.get("created_at") > cur:
                    out[ent["id"]] = e.get("created_at")
    return out


def is_actionable(note, vstatus, approved_at=None):
    """-> (bool, why). `vstatus` maps Version id -> sg_status_list.

    Linked to no Version -> actionable (nothing has closed it).
    Linked to Versions -> actionable while at least ONE is still live.

    `approved_at` is accepted and DELIBERATELY UNUSED, see the comment on
    LIVE_VERSION_STATUSES: a note on an APPROVED Version is never actionable,
    however new it is, because the operator has not yet said the review session
    is finished. The parameter stays in the signature so callers that already
    compute the map do not break, and so a future reader finds this note rather
    than re-inventing the rule.
    """
    if is_pipeline_record(note):
        return False, "the pipeline's own bookkeeping, a record and not a request"
    # A NOTE THAT IS NOT OPEN IS SETTLED, whatever it is linked to. This is the
    # same rule as below, read on the note itself rather than through a Version,
    # and the closed half of it was missing once already: closing 16 misfiled
    # Notes changed nothing, because every one of them linked only to a Shot and
    # so came back "nothing has closed it" -- while sg_status_list said, in as
    # many words, that something had.
    #
    # Widened from ("clsd", "closed") to a whitelist 2026-09-08; see
    # ACTIONABLE_NOTE_STATUSES for why a blocklist here is unsafe.
    _nst = (note.get("sg_status_list") or "").strip().lower()
    if _nst and _nst not in ACTIONABLE_NOTE_STATUSES:
        return False, ("a record: the Note's own status is '%s', and only %s is an "
                       "open request" % (_nst, "/".join(ACTIONABLE_NOTE_STATUSES)))

    vids = [l["id"] for l in (note.get("note_links") or [])
            if l.get("type") == "Version"]
    if not vids:
        return True, "linked to no Version and still open; nothing has closed it"
    live = [v for v in vids if vstatus.get(v) in LIVE_VERSION_STATUSES]
    if live:
        return True, "on a Version still awaiting a decision"
    seen = sorted(set(vstatus.get(v) or "?" for v in vids))
    if any(s in APPROVED_VERSION_STATUSES for s in seen):
        return False, ("on APPROVED work and the operator has not set it to 'rrq': "
                       "a review session may still be collecting notes, and 'rrq' is "
                       "the signal that it is finished (%s)" % ", ".join(seen))
    return False, "a record: every linked Version is settled (%s)" % ", ".join(seen)


def revision_requested(note, vstatus):
    """-> True if the operator has explicitly asked for this note to be actioned.

    THE STATUS IS THE INTENT, so the TEXT DOES NOT HAVE TO PROVE IT. Geoff,
    2026-09-08, designing this: *"we only need to decide if it is a
    regeneration or an edit... that could be decided by a different status."*

    WHY THIS EXISTS. classify() decides what a note MEANS by matching words in
    it, and that surface has now been the defect three times in one day: it
    missed a note about a character's pose (F332), the narrow fix for that
    dragged grade notes into the most expensive class (F347), and then it
    silently ignored this, a perfectly ordinary panel note Geoff wrote while
    testing the loop:

        "close-up looking straight at the laptop on the desk from the point of
         view of sitting at the desk, on the far left side of the desk a bowl
         of old cold pasta sitting under the light of a desk lamp"

    That is a complete, correct description of the wanted frame. It matched no
    rule because it never says "should be" or "framing" or "add". **It
    describes the target image directly, which is the most natural way to write
    a note and the one shape the classifier cannot see.** The pipeline logged
    UNCLASSIFIED every cycle and did nothing, and the operator had no way to
    know why.

    The operator had ALREADY said what they wanted by setting the Version to
    'rrq'. Making the sentence earn it a second time is the pipeline
    second-guessing a decision it was handed."""
    if is_pipeline_record(note):
        return False
    # THE SECOND DOOR INTO THE SAME ROOM. This function has no live caller
    # today (see the F377 comment in prompt_revision.py), but it is the other
    # published way to ask "should this note be actioned", and a note-status
    # guard on only one of the two is not a guard: the day someone calls this
    # again, a 'urr' record walks straight in. Reachable in practice too -- if
    # record_panel_refusal()'s Version write fails and its Note write succeeds,
    # a 'urr' note sits on an 'rrq' Version, which is precisely this predicate.
    _nst = (note.get("sg_status_list") or "").strip().lower()
    if _nst not in ("",) + ACTIONABLE_NOTE_STATUSES:
        return False
    return any(vstatus.get(l["id"]) == "rrq"
               for l in (note.get("note_links") or [])
               if l.get("type") == "Version")


def aggregate(classes):
    """-> ONE class for a shot from every actionable note on it.

    THE BUG THIS EXISTS TO KILL (measured 2026-09-04). The old loop wrote a
    verdict PER NOTE. A shot with two notes classifying differently got two
    writes, and kept whichever landed last -- so PILOT01_A_0090 was written
    'unclassified' at 19:37 and 'prompt-addressable' at 19:38, from the same
    132 notes, and would have flipped every 45 seconds forever. The
    "only write a change" guard could not help: it compares against the STORED
    value, and the stored value was always wrong for one of the two notes.

    NOT A RANKING ANY MORE. There used to be four classes and this picked
    the most expensive one when a shot's notes disagreed. With one live
    class this is just "did ANY of this shot's notes ask for something":
    PROMPT wins over NONE, because under-treating silently leaves a note
    unaddressed and the reviewer must catch it again, which is worse than
    an unnecessary proposal pass.
    """
    if not classes:
        return None
    return PROMPT if PROMPT in classes else classes[0]


def sweep(sg, dry=False, episode_code=None):
    """Classify one episode's REVIEW notes and record one verdict per Shot.
    -> the number of Shots whose stored class actually CHANGED.

    Writes onto the SHOT (that is where the field lives) so a reviewer filtering
    a page by 'fix in post' sees exactly the shots that need no GPU.

    `episode_code` is a parameter rather than the module's EP because EP comes
    from resolve_episode(), which defaults to PILOT01 unless GENVIDEO_EPISODE is
    set. A sweep run without that variable silently ignored all 55 SHOW01
    shots -- it reported "127 notes" and every one of them was PILOT01's.

    NOTE SOURCES, and both are needed. Shot.open_notes is ShotGrid's own
    rollup and is the cheap one, but it holds only notes linked to the SHOT:
    measured on PILOT01_A_0090, note 51148 ("The camera angle should be a wide
    shot instead of close") links to a Version ONLY and is absent from
    open_notes -- and that is exactly the shape a reviewer's note takes when
    they type it against a Version in the review player. So notes on the
    shot's Versions are gathered too, and the two sets deduped by Note id.

    The Version query is scoped to THIS EPISODE's shots. It used to be
    sg.find("Version", [["project","is",PROJ]]) -- every Version in the
    project, ~412 ids paged through a Note lookup, twice per service cycle,
    to classify one episode.
    """
    code = episode_code or EP.code
    shots = sg.find("Shot", [["project", "is", PROJ], ["code", "starts_with", code]],
                    ["code", "sg_note_class", "sg_review_verdict", "open_notes"])
    if not shots:
        print("[triage] no shots matching %s" % code)
        return 0
    by_id = dict((s["id"], s) for s in shots)

    vs = sg.find("Version",
                 [["entity", "in", [{"type": "Shot", "id": i} for i in by_id]]],
                 ["code", "entity", "sg_status_list"])
    vstatus = dict((v["id"], v.get("sg_status_list")) for v in vs)
    shot_of = dict((v["id"], (v.get("entity") or {}).get("id")) for v in vs)

    # Every note reachable from this episode: on a Version, or on the Shot.
    targets = ([{"type": "Version", "id": i} for i in vstatus]
               + [{"type": "Shot", "id": i} for i in by_id])
    CHUNK = 400
    notes_by_id = {}
    for i in range(0, len(targets), CHUNK):
        for nt in sg.find("Note", [["note_links", "in", targets[i:i + CHUNK]]],
                          ["content", "note_links", "subject", "sg_status_list",
                           "created_at"]):
            notes_by_id[nt["id"]] = nt
    notes = list(notes_by_id.values())
    if not notes:
        print("[triage] %s: no notes on any Shot or Version yet." % code)
        return 0

    # Approval times only for the Versions a note could actually be revising:
    # approved, and carrying at least one open note. Keeps the event-log query
    # small instead of asking about every Version in the episode.
    noted = set(l["id"] for nt in notes for l in (nt.get("note_links") or [])
                if l.get("type") == "Version")
    approved_at = approval_times(
        sg, [v for v in noted if vstatus.get(v) in APPROVED_VERSION_STATUSES])

    per_shot, counts, skipped = {}, {}, 0
    unmatched = []
    for nt in notes:
        live, why = is_actionable(nt, vstatus, approved_at)
        if not live:
            skipped += 1
            continue
        body = "%s %s" % (nt.get("subject") or "", nt.get("content") or "")
        cls, _why = classify(body)
        # AN UNCLASSIFIED NOTE IS NAMED, NOT COUNTED. classify() now returns
        # NONE only for an empty note (no subject, no content) -- every
        # non-empty operator note is a revision request, unconditionally
        # (Geoff, 2026-09-08: "if an operator gives a note, it needs to be
        # addressed"). The rrq-gated upgrade this branch used to carry
        # (was_unmatched() + revision_requested()) is now redundant: classify()
        # never produces an "unmatched but really meant" result for real text
        # to upgrade in the first place. revision_requested() stays defined
        # and self-tested below; it is simply not called from here any more.
        if cls == NONE and was_unmatched(_why):
            unmatched.append(nt)
        counts[cls] = counts.get(cls, 0) + 1
        for link in (nt.get("note_links") or []):
            sid = None
            if link.get("type") == "Shot":
                sid = link["id"]
            elif link.get("type") == "Version":
                sid = shot_of.get(link["id"])
            if sid in by_id:
                per_shot.setdefault(sid, []).append(cls)

    batch = []
    for sid, classes in per_shot.items():
        shot = by_id[sid]
        cls = aggregate(classes)
        if cls is None:
            continue
        if (shot.get("sg_note_class") == cls
                and shot.get("sg_review_verdict") == VERDICT[cls]):
            continue
        batch.append({"request_type": "update", "entity_type": "Shot",
                      "entity_id": sid,
                      "data": {"sg_note_class": cls,
                               "sg_review_verdict": VERDICT[cls]}})
        print("  %-18s %-20s from %d actionable note(s)%s"
              % (shot["code"], cls, len(classes),
                 "" if len(set(classes)) == 1
                 else "  [not every note on this shot asked for something; "
                      "addressing the one that did]"))

    for nt in unmatched[:8]:
        tgt = ", ".join("%s %s" % (l.get("type"), l.get("name"))
                        for l in (nt.get("note_links") or []))
        print("  UNCLASSIFIED note %s on %s: %r -- it carried no text, so nothing "
              "was done with it. Add the request in the note, or close it if it "
              "is a record."
              % (nt.get("id"), tgt[:60], (nt.get("subject") or "")[:70]))
    if len(unmatched) > 8:
        print("  ... and %d more unclassified note(s)" % (len(unmatched) - 8))
    print("[triage] %s: %d note(s), %d actionable, %d record-only (settled Versions): %s"
          % (code, len(notes), len(notes) - skipped, skipped, counts))
    if dry:
        print("[triage] DRY RUN, nothing written (%d would change)" % len(batch))
        return 0
    for i in range(0, len(batch), 50):
        sg.batch(batch[i:i + 50])
    if batch:
        print("[triage] verdicts written to %d shot(s)" % len(batch))
    return len(batch)


# --- self-test --------------------------------------------------------------

def self_test():
    fails = []

    def ck(name, cond):
        print("  %-62s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # Geoff's REAL notes from this session. These used to route to a
    # 'post-addressable' colour-pass class that no longer exists (removed
    # 2026-09-08, Geoff's instruction): every one of them is now simply
    # addressed as a revision request, the same as any other operator note.
    real = [
        "It should not be so dark, here is a brighter reference",
        "too brown, should be bright soft appealing 80's colors",
        "Still looks like draft quality. too low res and blurry",
        "the images and videos I've seen is terrible. too low samples? "
        "poor model? low resolution?",
    ]
    for txt in real:
        got, why = classify(txt)
        ck("real note -> addressed as a revision request: %r" % txt[:34],
           got == PROMPT)

    ck("'another take' is addressed like any other note now (no "
       "seed-only class exists any more)",
       classify("can we see another take")[0] == PROMPT)
    ck("'off-model' is addressed like any other note now (no "
       "asset-only class exists any more)",
       classify("CharB is off-model here")[0] == PROMPT)
    ck("content note is a prompt note",
       classify("she should be sitting at the table")[0] == PROMPT)
    ck("empty note is unclassified", classify("")[0] == NONE)
    ck("CANARY a whitespace-only note still routes nowhere (no text is "
       "not a request)", classify("   ")[0] == NONE)

    # --- THE BUG THAT TRIGGERED THIS REMOVAL, 2026-09-08. Geoff's real
    # 400-word framing note on SHOW01_A_0210 was classified
    # 'post-addressable' -- serviced by nothing -- because it contained the
    # word "colours" once, copied verbatim from this module's own
    # preserve-suffix wording. There is no more class for a keyword to
    # route into: prove it, and prove the canary is not vacuous by
    # confirming the note really does contain the trigger word a keyword
    # check would have caught.
    _colours_note = ("Frame PilotCharA centre-left, three-quarter angle, "
                      "matching the established set colours and lighting "
                      "from the previous shot.")
    ck("the note DOES contain the incidental word 'colours' (or this "
       "canary tests nothing)",
       re.search(r"\bcolou?rs?\b", _colours_note.lower()) is not None)
    ck("CANARY an incidental word like 'colours' does not misroute the "
       "note -- restoring a keyword class check would break this",
       classify(_colours_note)[0] == PROMPT)

    # --- THE IMPERATIVE REWRITE and THE POSE NOTE. Both used to be real
    # gaps in a keyword matcher (F332, 2026-09-07): a note asking for
    # exactly this went unmatched and did nothing. There is no matcher to
    # have gaps in any more; kept as regression fixtures.
    ck("an imperative rewrite ('Rewrite the beat so it reads...') is "
       "addressed",
       classify("Rewrite the beat so it reads: PilotCharB stands on the left "
                "of frame.")[0] == PROMPT)
    ck("a POSE note is addressed",
       classify("PilotCharB's pose: hands on the keyboard, head tipped to the "
                "screen")[0] == PROMPT)
    ck("frame-relative placement language is addressed",
       classify("Place each character against the frame, not against "
                "each other.")[0] == PROMPT)

    # --- THE STATUS IS THE INTENT. Geoff wrote this note while testing the
    # loop and the OLD keyword classifier ignored it every cycle: it is a
    # complete description of the wanted frame and uses none of the old
    # trigger words. It no longer needs the revision_requested()/'rrq'
    # workaround below to be addressed -- classify() addresses it directly,
    # like any other non-empty note.
    GEOFF = ("close-up looking straight at the laptop on the desk from the point of "
             "view of sitting at the desk, on the far left side of the desk a bowl of "
             "old cold pasta sitting under the light of a desk lamp")
    ck("CANARY: the real note Geoff wrote is now addressed directly, with "
       "no rrq-based workaround needed",
       classify(GEOFF)[0] == PROMPT)

    # revision_requested() and is_actionable() are UNCHANGED by this
    # removal -- they answer "is this an operator request at all", never
    # "which kind" -- and stay self-tested here even though neither this
    # module's own sweep() nor prompt_revision.py's selectors call
    # revision_requested() any more (see the module docstring and the
    # removal report: with classify() no longer producing an "unmatched"
    # result for real text, the rrq fall-through it used to gate is dead).
    _rrq = {7: 'rrq'}
    _apr = {7: 'apr'}
    _n = {'subject': 'x', 'content': GEOFF, 'sg_status_list': 'opn',
          'note_links': [{'type': 'Version', 'id': 7}]}
    ck("CANARY: with the Version at 'rrq' the operator has stated the intent",
       revision_requested(_n, _rrq) is True)
    ck("CANARY: with the Version APPROVED it is NOT",
       revision_requested(_n, _apr) is False)
    _auto = dict(_n, subject='[auto] design changed')
    ck("CANARY: the pipeline's OWN bookkeeping never counts, even at 'rrq'",
       revision_requested(_auto, _rrq) is False)
    _nover = dict(_n, note_links=[{'type': 'Shot', 'id': 3}])
    ck("CANARY: a note on the Shot with NO Version carries no status, so "
       "it cannot claim the operator asked",
       revision_requested(_nover, _rrq) is False)

    # --- THE APPROVAL-RATIONALE SAFETY PROPERTY MOVED, IT DID NOT VANISH.
    # classify() used to refuse an "Approval rationale for ..." note by
    # keyword (the APPROVE regex, now removed). The property that actually
    # mattered -- a rationale note on a SHIPPED, APPROVED panel must never
    # re-open it -- was always is_actionable()'s job too, and is now its
    # job ALONE: a note on an APPROVED Version with no 'rrq' is not
    # actionable, so classify() is never even called on it by sweep(). See
    # the is_actionable() canaries below (unchanged) for that proof.
    ck("a recipe note ('redo with turbo lora') is now addressed here too "
       "-- note_triage no longer refuses it by keyword; that refusal now "
       "lives solely in prompt_revision.py's DESIGN_RECIPE_WORDS gate, "
       "scoped to the Asset design loop",
       classify("redo with turbo lora to match the other one")[0] == PROMPT)

    # --- sweep(): episode scoping and change-only writes. Both are what the
    # standing service depends on, and both were broken in the way a caller
    # cannot see: the sweep reported "127 notes" while ignoring every SHOW01
    # shot, and rewrote identical rows on every pass.
    class _SweepStub(object):
        def __init__(self, vstatus=("rev", "rev"), notes=None):
            self.filters = []
            self.batches = []
            self.vstatus = vstatus
            self.notes = notes
            self.shots = [
                {"id": 1, "code": "SHOW01_A_0040", "sg_note_class": None,
                 "sg_review_verdict": None, "open_notes": []},
                {"id": 2, "code": "SHOW01_A_0050",
                 "sg_note_class": PROMPT, "sg_review_verdict": VERDICT[PROMPT],
                 "open_notes": []},
            ]

        def find(self, et, filters, fields=None, **k):
            self.filters.append((et, filters))
            if et == "Shot":
                pref = [f[2] for f in filters if f[0] == "code"]
                return [s for s in self.shots
                        if not pref or s["code"].startswith(pref[0])]
            if et == "Version":
                return [{"id": 10, "code": "V1", "sg_status_list": self.vstatus[0],
                         "entity": {"type": "Shot", "id": 1}},
                        {"id": 11, "code": "V2", "sg_status_list": self.vstatus[1],
                         "entity": {"type": "Shot", "id": 2}}]
            if self.notes is not None:
                return self.notes
            return [{"id": 100, "subject": "",
                     "content": "the shirt should carry no readable text",
                     "note_links": [{"type": "Version", "id": 10},
                                    {"type": "Version", "id": 11}]}]

        def batch(self, reqs):
            self.batches.extend(reqs)

    st = _SweepStub()
    n = sweep(st, dry=False, episode_code="SHOW01")
    shot_filters = [f for et, f in st.filters if et == "Shot"]
    ck("sweep scopes Shots by the episode_code it was GIVEN, not the module default",
       any(["code", "starts_with", "SHOW01"] in f for f in shot_filters))
    ck("sweep writes only the shot whose class actually changed",
       n == 1 and len(st.batches) == 1 and st.batches[0]["entity_id"] == 1)

    # The Version query must be scoped to THIS EPISODE's shots. It used to ask
    # for every Version in the project and page ~412 ids through a Note lookup,
    # twice per service cycle, to classify one episode.
    vf = [f for et, f in st.filters if et == "Version"]
    ck("the Version query is scoped to this episode's shots, not the whole project",
       vf and all(any(c[0] == "entity" for c in f) for f in vf)
       and not any(any(c[0] == "project" for c in f) for f in vf))

    st2 = _SweepStub()
    st2.shots[0]["sg_note_class"] = PROMPT
    st2.shots[0]["sg_review_verdict"] = VERDICT[PROMPT]
    ck("a second sweep over unchanged data writes NOTHING (idle stays quiet)",
       sweep(st2, dry=False, episode_code="SHOW01") == 0 and not st2.batches)

    st3 = _SweepStub()
    sweep(st3, dry=True, episode_code="SHOW01")
    ck("a dry sweep performs zero writes", not st3.batches)

    # --- GEOFF'S RULE: notes on a settled Version are records, not requests --
    # "notes on rejected don't require acting, they are optional records of
    # something." Everything below is that sentence, made testable.
    ck("a note on a REJECTED Version is not actionable",
       is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                     {10: "rjct"})[0] is False)
    ck("a note on an APPROVED Version with NO timestamps is not actionable",
       is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"})[0] is False)
    # --- THE DEMO'S CENTRAL GESTURE: a note asking to revise APPROVED work.
    # Found by rehearsing it. Ints stand in for datetimes; only compared.
    # RETRACTED, see F254. This used to assert the OPPOSITE: that a note newer
    # than the approval fires by itself. Geoff, 2026-09-07: "the operator is the
    # one to set the version's status to rrq, that is the signal that the notes
    # session is finished and they should be implemented." A review collects
    # notes from several people over time; firing on the first one implements
    # half a review.
    ck("CANARY a note on APPROVED work does NOT fire on its own, however new: "
       "'rrq' is the operator saying the note session is finished",
       is_actionable({"created_at": 200,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {10: 100})[0] is False)
    ck("...and the refusal explains that 'rrq' is what would make it fire, so "
       "the next reader is not left guessing why nothing happened",
       "rrq" in is_actionable({"created_at": 200,
                               "note_links": [{"type": "Version", "id": 10}]},
                              {10: "apr"}, {10: 100})[1])
    ck("CANARY the SAME note fires the moment the operator sets that Version to "
       "'rrq', which is the whole gesture",
       is_actionable({"created_at": 200,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "rrq"}, {10: 100})[0] is True)
    ck("CANARY several notes can accumulate on approved work and none of them "
       "fires until the status flips: a review is not implemented in halves",
       all(is_actionable({"created_at": c,
                          "note_links": [{"type": "Version", "id": 10}]},
                         {10: "apr"}, {10: 100})[0] is False
           for c in (150, 200, 250)))
    ck("CANARY a note OLDER than the approval stays a record: it is the reason "
       "the thing was approved, not a request to change it",
       is_actionable({"created_at": 50,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {10: 100})[0] is False)
    ck("CANARY a note written at the SAME instant as the approval is a record "
       "(strictly newer, or an approval note re-fires itself forever)",
       is_actionable({"created_at": 100,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {10: 100})[0] is False)
    ck("CANARY an UNKNOWN approval time leaves the note a record, never a guess",
       is_actionable({"created_at": 200,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {})[0] is False)
    ck("CANARY the rule does NOT resurrect notes on REJECTED work, however new",
       is_actionable({"created_at": 999,
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "rjct"}, {10: 100})[0] is False)
    ck("CANARY the pipeline's own bookkeeping is still silenced even when it is "
       "newer than the approval",
       is_actionable({"created_at": 200, "subject": AUTO_NOTE_PREFIX + "x",
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {10: 100})[0] is False)
    ck("CANARY a CLOSED note newer than the approval is still a record",
       is_actionable({"created_at": 200, "sg_status_list": "clsd",
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "apr"}, {10: 100})[0] is False)
    ck("the sweep FETCHES created_at, or the rule above can never fire",
       "created_at" in __import__("inspect").getsource(sweep))
    ck("the sweep CALLS approval_times, or approved_at is always empty and the "
       "rule is dead code",
       "approval_times(" in __import__("inspect").getsource(sweep))
    ck("a note on a Version PENDING REVIEW is actionable",
       is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                     {10: "rev"})[0] is True)
    ck("a note on a Version at REVISION REQUESTED is actionable",
       is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                     {10: "rrq"})[0] is True)
    ck("a note linked to the SHOT only stays actionable (nothing closed it)",
       is_actionable({"note_links": [{"type": "Shot", "id": 1}]}, {})[0] is True)
    ck("CANARY a CLOSED note is a record, even linked to a live Version",
       is_actionable({"sg_status_list": "clsd",
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "rev"})[0] is False)
    ck("CANARY a closed note linked only to a Shot is a record too",
       is_actionable({"sg_status_list": "clsd",
                      "note_links": [{"type": "Shot", "id": 1}]}, {})[0] is False)
    ck("an OPEN note is unaffected by the closed-note rule",
       is_actionable({"sg_status_list": "opn",
                      "note_links": [{"type": "Shot", "id": 1}]}, {})[0] is True)
    # --- F209 GUARD: the refusal Note must never re-enter the request channel.
    # genvideo_service.record_panel_refusal() writes a Note at 'urr' ("Under
    # Revision") on the Version it just marked 'prf', so the operator can see
    # WHY a shot stopped. If that note were actionable the proposer would spend
    # a `claude -p` call every cycle trying to implement the pipeline's own
    # error message, which is exactly the failure that got note-posting deleted
    # from that path in the first place.
    #
    # The literals below are DELIBERATELY typed out rather than read from
    # ACTIONABLE_NOTE_STATUSES: a canary built from the constant the code under
    # test also uses cannot fail. Change the constant and this goes red.
    ck("CANARY a note at 'urr' is NOT actionable, even on a live Version "
       "(the refusal Note must never re-enter the request channel)",
       is_actionable({"sg_status_list": "urr",
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "rrq"})[0] is False)
    ck("CANARY a note at 'urr' linked only to a Shot is not actionable either",
       is_actionable({"sg_status_list": "urr",
                      "note_links": [{"type": "Shot", "id": 1}]}, {})[0] is False)
    ck("CANARY 'addr' and 'qu' are not open requests either (whitelist, not a "
       "blocklist naming only 'clsd')",
       is_actionable({"sg_status_list": "addr",
                      "note_links": [{"type": "Version", "id": 10}]},
                     {10: "rrq"})[0] is False
       and is_actionable({"sg_status_list": "qu",
                          "note_links": [{"type": "Version", "id": 10}]},
                         {10: "rrq"})[0] is False)
    ck("a note with NO status set is still actionable (nothing has closed it)",
       is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                     {10: "rrq"})[0] is True)
    ck("CANARY revision_requested() ALSO refuses a 'urr' note -- the proposer "
       "reaches the note through this second door, and a guard on only one of "
       "them is not a guard",
       revision_requested({"sg_status_list": "urr",
                           "note_links": [{"type": "Version", "id": 10}]},
                          {10: "rrq"}) is False)
    ck("the sweep FETCHES sg_status_list, or the rule above can never fire",
       "sg_status_list" in __import__("inspect").getsource(sweep))
    ck("one live Version among several settled ones keeps the note actionable",
       is_actionable({"note_links": [{"type": "Version", "id": 10},
                                     {"type": "Version", "id": 11}]},
                     {10: "rjct", 11: "rev"})[0] is True)
    ck("the refusal SAYS it is a record and names the settled status",
       "record" in is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                                 {10: "rjct"})[1]
       and "rjct" in is_actionable({"note_links": [{"type": "Version", "id": 10}]},
                                   {10: "rjct"})[1])

    stR = _SweepStub(vstatus=("rjct", "rjct"))
    ck("a sweep whose only notes sit on rejected Versions writes NOTHING",
       sweep(stR, dry=False, episode_code="SHOW01") == 0 and not stR.batches)

    # --- AGGREGATION: one verdict per shot, not one write per note ----------
    # THE MEASURED BUG. PILOT01_A_0090 carried notes classifying differently and
    # was written 'unclassified' at 19:37 and 'prompt-addressable' at 19:38,
    # from the same 132 notes, and would have flipped every 45s forever.
    ck("aggregate picks PROMPT when any of a shot's notes requested something",
       aggregate([NONE, PROMPT]) == PROMPT)
    ck("aggregate is order-independent (the last note must not win)",
       aggregate([PROMPT, NONE]) == aggregate([NONE, PROMPT]) == PROMPT)
    ck("aggregate of one class is that class", aggregate([NONE]) == NONE)
    ck("aggregate of nothing is None, not a default verdict",
       aggregate([]) is None)

    # One real note and one EMPTY note (the only way classify() still
    # returns NONE) on the same shot: this used to be built from an
    # approval-worded note, which no longer classifies NONE at all -- an
    # empty note is the only remaining way two of a shot's notes disagree.
    disagree = [
        {"id": 1, "subject": "", "content": "she should be sitting down",
         "note_links": [{"type": "Version", "id": 10}]},
        {"id": 2, "subject": "", "content": "   ",
         "note_links": [{"type": "Version", "id": 10}]},
    ]
    stA = _SweepStub(notes=disagree)
    nA = sweep(stA, dry=False, episode_code="SHOW01")
    ck("a shot with one real note and one empty note gets exactly ONE "
       "write, not one per note",
       len([b for b in stA.batches if b["entity_id"] == 1]) == 1)
    ck("...and that write addresses the note that actually asked for "
       "something",
       stA.batches[0]["data"]["sg_note_class"] == PROMPT)

    # CONVERGENCE, the property the old code could never have. Feed the result
    # of the first sweep back in and the second must be silent. Reversing the
    # note order proves the outcome does not depend on which note landed last.
    stB = _SweepStub(notes=list(reversed(disagree)))
    stB.shots[0]["sg_note_class"] = PROMPT
    stB.shots[0]["sg_review_verdict"] = VERDICT[PROMPT]
    ck("CANARY the churn is dead: a re-sweep of the same shot writes nothing",
       sweep(stB, dry=False, episode_code="SHOW01") == 0 and not stB.batches)

    ck("every class maps to a real ShotGrid verdict",
       set(VERDICT) == {PROMPT, NONE})

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", metavar="TEXT")
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if ns.classify:
        cls, why = classify(ns.classify)
        print("class:   %s" % cls)
        print("verdict: %s" % VERDICT[cls])
        for w in why:
            print("  because %s" % w)
        return 0
    if ns.sweep:
        return cmd_sweep(sg_connect(), ns.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
