#!/usr/bin/env python3
r"""Keep the review pages showing only what actually needs a decision.

THE PARADIGM THIS SERVES (Geoff): the operator has pages filtered for things
pending review, and on each one they approve, request a revision with a note,
reject the alternates, or promote it onward. That only works if everything NOT
awaiting a decision is out of the way. Right now a status-only filter on this
episode returns 108 Versions and only 8 of them are real panel candidates.

THREE KINDS OF NOISE, and they are not the same thing:

  1. ALTERNATES THAT LOST. A sibling on the same entity and stage was
     approved. These are a judgement that has already been made, so they go to
     'rjct' -- rejected is the honest word for "we looked and chose another".
  2. EXPERIMENTS. sg_stage='wedge' cells. These were never review candidates:
     they are published because every render is published, not because anyone
     should decide about them.
  3. STAGE-LESS TEST CELLS. Old bake-off renders published before the stage
     vocabulary existed. Same reasoning as 2.

THE STATUS COLLAPSED 2026-09-08, THE REASON DID NOT. Kinds 2 and 3 used to
write a second status, spelled Omit, distinct from kind 1's rjct: "never a
candidate" versus "considered and turned down". Geoff asked what it meant,
said he did not like it, and approved collapsing it -- both existed only to
get a row out of the review queue, and rjct already does that, so keeping a
second status nobody drove any decision from was exactly the kind of record
his standing rule rejects. All three kinds now write 'rjct'. What still
distinguishes them is the REASON text next to each row (see classify()
below): kind 2 and 3 rows still say "experiment" / "no sg_stage", never
"rejected". The 1,405 Versions already carrying the retired status are
untouched; nothing new is written with it.

  4. A REVISION REQUEST THAT WAS ALREADY SATISFIED. A candidate at 'rrq'
     whose entity has since had a LATER version approved at the same stage.
     (There is also a fifth kind, below the docstring's original four:
     alternates that lost to an 'rrq', not just to an 'apr'. See KIND 5,
     below _rrq_losers().)
     The operator asked for a change, the change was made, and the change was
     approved: the conversation is over, but the original still sits on the
     "revision requested" page forever. Measured 2026-09-07: 5 of the 9 rrq
     panel Versions in SHOW01 were in exactly this state
     (SHOW01_A_0040_PNL_panel_v001 closed by _v003;
     SHOW01_A_0060_PNL_panel_v001 and _v003 both by _v019;
     SHOW01_A_0160_PNL_panel_v004 by _v007; SHOW01_A_0380_PNL_panel_v001 by
     _v005). It goes to 'rjct', because the operator DID judge it and another
     was chosen.

     THE DISCRIMINATOR IS THE TIMESTAMP, and it must be, because the reverse
     order is a genuinely open request: an approval, then a note asking for a
     revision OF that approval, leaves an rrq that is NEWER than the approval
     and is still waiting. Only an approval created AFTER the request closes
     it. Closing on membership alone would erase a live note.

NOTHING IS EVER APPROVED HERE. An 'rrq' with no LATER approval is untouched:
a revision the operator asked for and has not yet received is an open
conversation, not noise.

    python sg_review_housekeeping.py --dry-run
    python sg_review_housekeeping.py --episode SHOW01
    python sg_review_housekeeping.py --backfill-batch-id --dry-run
    python sg_review_housekeeping.py --self-test
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# For --backfill-batch-id: the REVIEW playlist naming convention is owned by
# sg_review_playlists.py (PREFIX = "REVIEW"); imported rather than
# restated so the two cannot drift about what a review playlist is called.
# No cycle risk: sg_review_playlists.py imports nothing of ours.
import sg_review_playlists as RPL                               # noqa: E402

PROJ = {"type": "Project", "id": 9999}

# THE STAGES WHERE A BATCH IS PRODUCED AND ONE WINNER IS PICKED. Only these
# have "alternates" at all, so only these are swept. Deliberately a short
# allow-list rather than a deny-list of episode/sequence: a stage added later
# is NOT swept until someone decides it should be, which is the safe default
# for a rule whose failure mode is silently rejecting somebody's work.
ALTERNATE_STAGES = ("panel", "video", "asset", "keyframe")

APPROVED = ("apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra")
# Only these are swept. 'rrq' is an open conversation; anything already
# terminal is left alone.
SWEEPABLE = ("rev",)
# 'rrq' is not swept as noise; it is CLOSED, and only when its own revision has
# landed and been approved. See kind 4 above.
SATISFIABLE = ("rrq",)
# AND ONLY ON A SHOT. A review agent found the hole on 2026-09-07, before it had
# fired: the rule keys on (entity type, entity id, stage), and on an ASSET that
# key is not one deliverable. Asset CHAR_CHARB carries `..._ANCHOR_v001` and
# `..._SHEET_v003_*` under the SAME sg_stage, independently approved; the same
# shape appears on CHARH_FLOCK and on PILOTCHARA_BEDROOM (ANCHOR versus EDIT). So
# an approved ANCHOR would have closed an open SHEET request it never answered.
# On a Shot the key IS one deliverable: a shot has one panel and one video.
SATISFIABLE_ENTITY_TYPES = ("Shot",)
LOST = "rjct"        # considered, another was chosen
# Collapsed 2026-09-08 (Geoff did not like the old second status and approved
# retiring it) -- was its own value, now shares rjct with LOST. The REASON
# text passed alongside this constant is what still says "never a candidate"
# rather than "considered and turned down"; see classify() below.
NOT_A_CANDIDATE = "rjct"   # never up for a decision

# KIND 5: AN ALTERNATE THAT LOST TO AN 'rrq', NOT ONLY TO AN 'apr'. Geoff,
# 2026-09-08, on REVIEW_SHOW01_A_0170_v017: choosing one candidate to revise
# is still a choice, and the seven siblings sitting at 'rev' next to it are
# exactly as much noise as if the winner had been approved outright. The
# 'rrq' Version itself is NEVER touched (his rule: it is how he keeps track
# of what he actually commented on), so only its 'rev' batch-mates move.
#
# THE HARD PART IS "BATCH-MATES", because his OWN rule right next to it is
# that MULTIPLE LIVE 'rrq' Versions on one entity/stage are NORMAL ("what if
# I want to revise it again and again... thats natural"). A flat rule keyed
# only on (entity type, entity id, stage) -- exactly what `approved` uses
# for the 'apr' case above -- cannot tell one publish run's own losers from a
# LATER run's still-undecided, not-yet-reviewed candidates: it would rush to
# reject fresh 'rev' work the moment ANY older 'rrq' exists at that key.
#
# THE FIX IS A REAL BATCH ID, NOT A GUESS. Geoff, on the first version of
# this rule, which clustered Versions by a gap in created_at: "stamp a batch
# id on the Version when the batch is published. time gap is flaky." He is
# right, and there is already a natural id: panel_compose.py's
# publish_alternates() names one Playlist per compose run
# (REVIEW_<shot>_v<NNN>, "these four came out of one run"), so that same
# string is now stamped on Version.sg_batch_id at PUBLISH time, by the code
# that actually publishes the batch (panel_compose.py, via
# sg_publish.publish_version()'s batch_id parameter) -- never reconstructed
# here afterwards. A Version with no sg_batch_id (published before this
# field existed) is simply not grouped by this rule; see
# --backfill-batch-id for the explicit, one-time fill of the unambiguous
# cases, and the module docstring's KIND 5 note above.
def log(m):
    print("[review] %s" % m, flush=True)


def _satisfied_request(v, stage, approved):
    """-> (LOST, why) for an 'rrq' whose revision landed and was approved.

    SAME STAGE ALLOW-LIST AS THE ALTERNATES RULE, and for the same reason. On a
    stage where two Versions of one entity are two different pieces of work (an
    episode cut of scene A and one of scene B), "something newer was approved"
    does not mean "your request was answered", and closing on it would erase a
    live note. On panel/video/asset/keyframe a later approval on the same entity
    IS the answer to the request."""
    if stage not in ALTERNATE_STAGES:
        return None, ("an open revision request at stage %s, where a later "
                      "approval is not necessarily its answer" % stage)
    e = v.get("entity") or {}
    if e.get("type") not in SATISFIABLE_ENTITY_TYPES:
        return None, ("an open revision request on a %s, where one stage holds "
                      "several different deliverables" % (e.get("type") or "?"))
    hit = approved.get((e.get("type"), e.get("id"), stage))
    made = v.get("created_at")
    if not hit:
        return None, "an open revision request; nothing approved at this stage"
    if made is None:
        # Refuse to guess. Without the request's own timestamp the rule has no
        # discriminator at all, and its failure mode is erasing a live note.
        return None, "an open revision request; no created_at to compare"
    when, code = hit
    if when is None or when <= made:
        # The approval PREDATES the request, so the request is ABOUT that
        # approval and is still open.
        return None, "an open revision request; the approval predates it"
    return LOST, "revision delivered: %s was approved after this request" % code


def _rrq_losers(vs):
    """-> set of Version ids: 'rev' Versions that share a BATCH (Version.
    sg_batch_id) with an 'rrq' Version. Only alternate stages are grouped at
    all -- same allow-list as the 'apr' case, and for the same reason (a
    stage without alternates has no batch to lose within).

    NO TIME HEURISTIC. The batch is keyed purely on sg_batch_id, stamped by
    the publisher at publish time (see the KIND 5 comment above classify()).
    A Version with no sg_batch_id (empty or missing -- published before this
    field existed, or by a path that has not adopted it) is excluded from
    every group here: it is grouped with nothing and loses to nothing,
    which is the safe direction. Refusing to guess its batch is the entire
    point of replacing the time heuristic; see --backfill-batch-id for the
    explicit, one-time way to give it one.

    Deliberately returns ids, not a richer structure: classify() only ever
    needs "is THIS Version one of them", and a flat id set is the cheapest
    thing that answers that without the caller re-deriving batches itself."""
    by_batch = {}
    for v in vs:
        stage = v.get("sg_stage")
        bid = (v.get("sg_batch_id") or "").strip()
        if stage not in ALTERNATE_STAGES or not bid:
            continue
        by_batch.setdefault(bid, []).append(v)
    losers = set()
    for group in by_batch.values():
        if any(v.get("sg_status_list") == "rrq" for v in group):
            losers.update(v["id"] for v in group
                          if v.get("sg_status_list") == "rev")
    return losers


def classify(v, approved, rrq_losers=frozenset()):
    """-> (new_status, reason) or (None, why-not).

    `approved` maps (entity type, entity id, stage) -> (created_at, code) for
    the LATEST approval at that key. It is a mapping and not a set because the
    'rrq' rule needs the approval's TIMESTAMP, and a set cannot answer "was
    something approved AFTER this request".

    `rrq_losers` is the set of Version ids computed by _rrq_losers(): 'rev'
    Versions that lost to an 'rrq' sibling sharing the SAME sg_batch_id. See
    KIND 5 above for why this needs a batch id and not a flat (entity,
    stage) key.

    Order matters: an experiment is not an alternate even if its entity has an
    approved sibling, because it was never in the running."""
    st = v.get("sg_status_list")
    stage = v.get("sg_stage")
    if st in SATISFIABLE:
        return _satisfied_request(v, stage, approved)
    if st not in SWEEPABLE:
        return None, "status %s is not swept" % st
    if stage == "wedge":
        return NOT_A_CANDIDATE, "experiment (sg_stage=wedge), never a review candidate"
    if not stage:
        return NOT_A_CANDIDATE, "no sg_stage: a test cell from before the stage vocabulary"
    e = v.get("entity") or {}
    # ALTERNATES ARE A FACT ABOUT SOME STAGES AND NOT OTHERS. Geoff, 2026-09-06:
    #
    #   "why do sequences and episodes need a watcher auto-rejecting other
    #    versions? what would that even accomplish? it's needed on asset
    #    keyframes, shot panels, shot videos ... those don't get wedges in
    #    batches/playlists where only one of a batch is expected to be
    #    approved, they normally just get one version. and it doesn't need to
    #    be approved to unlock any downstream entity."
    #
    # He is right, and this rule was overreaching rather than subtly wrong. The
    # alternates idea comes from REVIEW BATCHES: four panels of one shot, one
    # gets picked, the losers are noise. A cut is not one of four candidates,
    # nothing downstream waits on it being approved, and two cuts on one entity
    # are usually two DIFFERENT SCENES. Sweeping there turned "approve this
    # cut" into "silently reject the other scene", which is the trap that has
    # been sitting live on Episode SHOW01 all day.
    if stage not in ALTERNATE_STAGES:
        # LEAVE IT ALONE. Not NOT_A_CANDIDATE, which SWEEPS IT OUT OF REVIEW
        # (now via the same rjct status as LOST, collapsed from the old
        # second status 2026-09-08 -- see the module docstring). That was
        # the first version of this fix and it was worse
        # than the bug: it turned "approving a cut rejects its sibling" into
        # "every cut disappears from review", which is F025 again from the other
        # side. Deployed at 17:56 and caught on its first sweep, 6 SHOW01 cuts
        # and animatics gone in one cycle.
        #
        # A stage without alternates is not a settled decision and not a test
        # cell. It is a live candidate that simply has no siblings to lose to,
        # so the correct action is NOTHING.
        return None, ("a live candidate at stage %s, which has no alternates: "
                      "nothing to sweep it against" % stage)
    if v.get("id") in rrq_losers:
        return LOST, "an alternate; a sibling in this batch was set to rrq"
    if (e.get("type"), e.get("id"), stage) in approved:
        return LOST, "an alternate; a sibling at this stage is approved"
    return None, "a live candidate at stage %s" % stage


def sweep_filters(episode=None):
    """-> the Version filter list.

    THE EPISODE PREFIX USED TO BE A DEFAULT (`episode="SHOW01"`) AND THAT MADE
    THIS SWEEPER STRUCTURALLY BLIND TO DESIGN CANDIDATES. A design Version is
    coded `SHOW_SET_PILOTCHARB_BEDROOM_ANCHOR_turbo10_s52009`, which does not start
    with an episode code, so `code starts_with SHOW01` excluded every one of
    them. Only Shot-entity work (panels, videos) was ever swept.

    MEASURED 2026-09-07, and Geoff asked the question that found it: *"when one
    render of a wedge is approved/rrq aren't the rest supposed to be
    automatically set to rejected? I thought the pipeline had a watcher for
    that?"* There is one; it had never been able to see the batch. He approved
    `..._s52009` and its 12 siblings stayed at 'rev', looking like they still
    needed his attention.

    Scoping by PROJECT is what the sweeper actually needs: `classify()` already
    keys a batch on (entity type, entity id, stage), so it cannot confuse two
    Assets or two Shots. Narrowing to an episode stays available and is now a
    deliberate act."""
    f = [["project", "is", PROJ]]
    if episode:
        f.append(["code", "starts_with", episode])
    return f


def sweep(sg, episode=None, dry=True):
    vs = sg.find("Version", sweep_filters(episode),
                 ["code", "sg_status_list", "sg_stage", "entity", "created_at",
                  "sg_batch_id"],
                 order=[{"field_name": "code", "direction": "asc"}])
    # key -> (created_at, code) for the LATEST approval at that key. Latest and
    # not first: "has anything been approved SINCE this request" is answered by
    # the newest approval, and keeping the oldest would leave a satisfied
    # request open whenever an entity was approved twice.
    approved = {}
    for v in vs:
        if v.get("sg_status_list") in APPROVED:
            e = v.get("entity") or {}
            k = (e.get("type"), e.get("id"), v.get("sg_stage"))
            cur, when = approved.get(k), v.get("created_at")
            if cur is None or cur[0] is None or (when is not None and when > cur[0]):
                approved[k] = (when, v.get("code"))
    rrq_losers = _rrq_losers(vs)

    actions, kept = [], 0
    for v in vs:
        new, why = classify(v, approved, rrq_losers)
        if new:
            actions.append((v, new, why))
        elif v.get("sg_status_list") in SWEEPABLE + SATISFIABLE:
            kept += 1

    from collections import Counter
    log("%d Version(s) in %s; %d to move, %d left in review"
        % (len(vs), episode or "the whole project", len(actions), kept))
    for status, n in Counter(a[1] for a in actions).most_common():
        log("  -> %s: %d" % (status, n))
    for v, new, why in actions[:6]:
        log("     e.g. %-42s %s  (%s)" % (v["code"][:42], new, why))
    if len(actions) > 6:
        log("     ... and %d more" % (len(actions) - 6))

    if dry:
        log("DRY RUN, nothing written")
        return 0
    batch = [{"request_type": "update", "entity_type": "Version",
              "entity_id": v["id"], "data": {"sg_status_list": new}}
             for v, new, why in actions]
    for i in range(0, len(batch), 50):
        sg.batch(batch[i:i + 50])
    log("moved %d Version(s); %d remain in review" % (len(batch), kept))
    return len(batch)


# ------------------------------------------------------ backfill (explicit only)
def backfill_filters(episode=None):
    f = [["project", "is", PROJ]]
    if episode:
        f.append(["code", "starts_with", episode])
    return f


def backfill_plan(vs):
    """-> (to_stamp, counts). `to_stamp` is [(version, batch_code)] for every
    Version with NO sg_batch_id that sits in EXACTLY ONE REVIEW_* playlist --
    the one unambiguous case. `counts` reports the rest, never guesses:
    'already' (had a batch id already, left alone), 'zero' (in no REVIEW
    playlist -- nothing to stamp it with) and 'multi' (in two or more --
    which one is the real batch is exactly the question guessing used to
    answer, and this backfill exists to stop doing that)."""
    to_stamp = []
    counts = {"already": 0, "zero": 0, "multi": 0}
    prefix = "%s_" % RPL.PREFIX
    for v in vs:
        if (v.get("sg_batch_id") or "").strip():
            counts["already"] += 1
            continue
        pls = [p for p in (v.get("playlists") or [])
               if (p.get("name") or "").startswith(prefix)]
        if len(pls) == 1:
            to_stamp.append((v, pls[0]["name"]))
        elif len(pls) == 0:
            counts["zero"] += 1
        else:
            counts["multi"] += 1
    return to_stamp, counts


def backfill_batch_id(sg, episode=None, dry=True):
    """SEPARATE, EXPLICIT COMMAND -- never runs as part of sweep(). Gives
    sg_batch_id to Versions published before the field existed, using the
    single REVIEW playlist each already sits in as the id: the same natural
    id publish time now stamps directly (see the KIND 5 comment above
    classify()). A Version in zero or two-or-more REVIEW playlists is left
    unstamped and only counted -- guessing which playlist is the real batch
    is the exact heuristic this whole change removes, so this backfill
    refuses to do it too."""
    vs = sg.find("Version", backfill_filters(episode),
                 ["code", "sg_batch_id", "sg_stage", "playlists"])
    to_stamp, counts = backfill_plan(vs)
    log("%d Version(s) in %s; %d already had sg_batch_id, %d to stamp (exactly "
        "one REVIEW playlist), %d in NO REVIEW playlist (left unstamped), "
        "%d in TWO OR MORE (left unstamped -- ambiguous, not guessed)"
        % (len(vs), episode or "the whole project", counts["already"],
           len(to_stamp), counts["zero"], counts["multi"]))
    for v, code in to_stamp[:6]:
        log("     e.g. %-42s -> %s" % (v["code"][:42], code))
    if len(to_stamp) > 6:
        log("     ... and %d more" % (len(to_stamp) - 6))

    if dry:
        log("DRY RUN, nothing written")
        return 0
    batch = [{"request_type": "update", "entity_type": "Version",
              "entity_id": v["id"], "data": {"sg_batch_id": code}}
             for v, code in to_stamp]
    for i in range(0, len(batch), 50):
        sg.batch(batch[i:i + 50])
    log("stamped %d Version(s)" % len(batch))
    return len(batch)


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ent = {"type": "Shot", "id": 1}
    # (created_at, code) for the LATEST approval at that key. Plain ints stand
    # in for datetimes: the rule only ever compares them.
    keys = {("Shot", 1, "panel"): (100, "A_v009")}

    ck("a wedge takes the NOT_A_CANDIDATE path (nobody judged it), which is "
       "now the same rjct status LOST uses -- the reason text is the only "
       "thing that still says so",
       classify({"sg_status_list": "rev", "sg_stage": "wedge", "entity": ent},
                keys)[0] == NOT_A_CANDIDATE)
    ck("a stage-less test cell takes the same path, same reasoning",
       classify({"sg_status_list": "rev", "sg_stage": None, "entity": ent},
                keys)[0] == NOT_A_CANDIDATE)
    ck("a losing alternate at an approved stage is REJECTED",
       classify({"sg_status_list": "rev", "sg_stage": "panel", "entity": ent},
                keys)[0] == LOST)
    ck("a live candidate where nothing is approved is LEFT ALONE",
       classify({"sg_status_list": "rev", "sg_stage": "panel",
                 "entity": {"type": "Shot", "id": 2}}, keys)[0] is None)
    # The two that would do damage.
    ck("'rrq' with NOTHING approved on the entity stays open",
       classify({"sg_status_list": "rrq", "sg_stage": "panel", "created_at": 50,
                 "entity": {"type": "Shot", "id": 2}}, keys)[0] is None)
    ck("'rrq' whose revision LANDED and was approved is closed as rejected",
       classify({"sg_status_list": "rrq", "sg_stage": "panel", "created_at": 50,
                 "entity": ent}, keys)[0] == LOST)
    # THE ONE THAT WOULD ERASE A LIVE NOTE, and it is the whole reason the rule
    # carries a timestamp: an approval, then a revision asked OF it. The rrq is
    # NEWER than the approval and is still waiting.
    ck("CANARY: 'rrq' created AFTER the approval is an OPEN request, left alone",
       classify({"sg_status_list": "rrq", "sg_stage": "panel", "created_at": 150,
                 "entity": ent}, keys)[0] is None)
    ck("CANARY: 'rrq' with no created_at is left alone rather than guessed",
       classify({"sg_status_list": "rrq", "sg_stage": "panel", "created_at": None,
                 "entity": ent}, keys)[0] is None)
    ck("an approved VIDEO does not close a PANEL revision request",
       classify({"sg_status_list": "rrq", "sg_stage": "panel", "created_at": 50,
                 "entity": ent}, {("Shot", 1, "video"): (100, "V")})[0] is None)
    # Same allow-list as the alternates rule: two cuts on one entity are two
    # different scenes, so a newer approval is not an answer to this request.
    # THE HOLE A REVIEW AGENT FOUND BEFORE IT FIRED. One Asset carries several
    # different deliverables at one stage (ANCHOR, SHEET, ASPECTTEST, EDIT), so
    # a later approval there is not an answer to this request.
    ck("CANARY: an 'rrq' on an ASSET is left alone even with a later approval",
       classify({"sg_status_list": "rrq", "sg_stage": "keyframe", "created_at": 50,
                 "entity": {"type": "Asset", "id": 1}},
                {("Asset", 1, "keyframe"): (100, "OTHER_DELIVERABLE_v001")})[0] is None)
    ck("...and a SHOT at the same stage and shape IS still closed, so the "
       "narrowing did not disable the rule",
       classify({"sg_status_list": "rrq", "sg_stage": "keyframe", "created_at": 50,
                 "entity": {"type": "Shot", "id": 1}},
                {("Shot", 1, "keyframe"): (100, "X_v001")})[0] == LOST)
    ck("CANARY: an 'rrq' on an EPISODE cut is left alone even with a later "
       "approval",
       classify({"sg_status_list": "rrq", "sg_stage": "episode", "created_at": 50,
                 "entity": ent},
                {(ent["type"], ent["id"], "episode"): (100, "C")})[0] is None)
    # THE TRAP THAT WAS LIVE ALL DAY: two DIFFERENT SCENES sat on Episode
    # SHOW01 at stage 'episode', and approving either would have silently
    # rejected the other. A cut is not one of a batch of candidates.
    # LEFT ALONE means None, not NOT_A_CANDIDATE. The first version of this
    # returned NOT_A_CANDIDATE and swept every cut out of review; these
    # canaries assert the difference. NOT_A_CANDIDATE and LOST now share the
    # one rjct status (collapsed 2026-09-08), so "the status is not rjct" no
    # longer proves anything by itself -- it must be None, full stop.
    ck("CANARY: an episode cut is LEFT ALONE, not swept at all",
       classify({"sg_status_list": "rev", "sg_stage": "episode", "entity": ent},
                {(ent["type"], ent["id"], "episode"): (100, "X_v001")})[0] is None)
    ck("CANARY: a sequence cut is left alone too",
       classify({"sg_status_list": "rev", "sg_stage": "sequence", "entity": ent},
                {(ent["type"], ent["id"], "sequence"): (100, "X_v001")})[0] is None)
    ck("CANARY: an animatic is left alone (6 were swept before this canary existed)",
       classify({"sg_status_list": "rev", "sg_stage": "animatic", "entity": ent},
                {(ent["type"], ent["id"], "animatic"): (100, "X_v001")})[0] is None)
    ck("a stage nobody has classified is left alone (safe default)",
       classify({"sg_status_list": "rev", "sg_stage": "grade", "entity": ent},
                {(ent["type"], ent["id"], "grade"): (100, "X_v001")})[0] is None)
    ck("but a losing PANEL alternate is still swept, so the rule still works",
       classify({"sg_status_list": "rev", "sg_stage": "panel", "entity": ent},
                {(ent["type"], ent["id"], "panel"): (100, "X_v001")})[0] == LOST)
    ck("and a losing VIDEO alternate is still swept",
       classify({"sg_status_list": "rev", "sg_stage": "video", "entity": ent},
                {(ent["type"], ent["id"], "video"): (100, "X_v001")})[0] == LOST)

    ck("an APPROVED version is never touched",
       classify({"sg_status_list": "apr", "sg_stage": "panel", "entity": ent},
                keys)[0] is None)
    ck("an already-rejected version is not re-written",
       classify({"sg_status_list": "rjct", "sg_stage": "panel", "entity": ent},
                keys)[0] is None)
    ck("a wedge is omitted even when its entity HAS an approved panel",
       classify({"sg_status_list": "rev", "sg_stage": "wedge", "entity": ent},
                keys)[1].startswith("experiment"))
    # Approval at a DIFFERENT stage must not reject a candidate at this one.
    ck("an approved VIDEO does not reject a pending PANEL",
       classify({"sg_status_list": "rev", "sg_stage": "panel", "entity": ent},
                {("Shot", 1, "video"): (100, "V_v001")})[0] is None)

    # --- KIND 5: an alternate that lost to an 'rrq', not only to an 'apr' ---
    # Geoff, 2026-09-08, REVIEW_SHOW01_A_0170_v017: choosing one candidate to
    # revise is still a choice, and its 'rev' siblings are noise. Grouped
    # purely on Version.sg_batch_id now, stamped at publish time -- no
    # created_at, no gap, no clustering.
    ck("classify() rejects a 'rev' alternate whose id is in rrq_losers",
       classify({"id": 401, "sg_status_list": "rev", "sg_stage": "panel",
                 "entity": ent}, {}, {401})[0] == LOST)
    ck("CANARY: classify() leaves the 'rrq' Version itself untouched even if "
       "it somehow ended up in rrq_losers -- SATISFIABLE runs before the "
       "rrq_losers check is ever reached, structurally, not by convention",
       classify({"id": 402, "sg_status_list": "rrq", "sg_stage": "panel",
                 "entity": ent, "created_at": None}, {}, {402})[0] is None)
    ck("CANARY: a wedge cell's reason still says experiment, not a rejection, "
       "even if it somehow ended up in rrq_losers -- the wedge gate runs "
       "first",
       classify({"id": 403, "sg_status_list": "rev", "sg_stage": "wedge",
                 "entity": ent}, {}, {403})[1].startswith("experiment"))

    # Two real publish batches on the same shot/stage, told apart ONLY by
    # sg_batch_id -- no created_at at all, proving the grouping no longer
    # needs one. This is SHOW01_A_0170 in miniature: v012 (an earlier batch's
    # 'rev' straggler) sitting at the same (Shot, panel) key as v017's later
    # 'rrq', ~21 hours apart in the live project, but here told apart purely
    # by batch id.
    batch1 = [
        {"id": 101, "sg_status_list": "rev", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v012"},
        {"id": 102, "sg_status_list": "rrq", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v012"},
        {"id": 103, "sg_status_list": "rev", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v012"},
    ]
    batch2 = [
        {"id": 201, "sg_status_list": "rev", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v017"},
        {"id": 202, "sg_status_list": "rrq", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v017"},
        {"id": 203, "sg_status_list": "rev", "sg_stage": "panel", "entity": ent,
         "sg_batch_id": "REVIEW_SHOW01_A_0170_v017"},
    ]
    two_batches = batch1 + batch2
    losers = _rrq_losers(two_batches)
    ck("_rrq_losers: both 'rev' siblings sharing batch 1's id are caught",
       {101, 103} <= losers)
    ck("_rrq_losers: both 'rev' siblings sharing batch 2's DIFFERENT id are "
       "caught too, so MULTIPLE LIVE rrq's on one entity/stage (Geoff: "
       "'thats natural') each still clear their own batch",
       {201, 203} <= losers)
    ck("_rrq_losers: neither 'rrq' Version is ever in the loser set",
       102 not in losers and 202 not in losers)
    # CANARY: a Version with a DIFFERENT batch id from any rrq present is
    # untouched, even though it shares the same (entity, stage) key as both
    # batches above.
    other_batch = {"id": 301, "sg_status_list": "rev", "sg_stage": "panel",
                   "entity": ent, "sg_batch_id": "REVIEW_SHOW01_A_0170_v099"}
    ck("CANARY: a 'rev' Version with a DIFFERENT batch id is left alone, not "
       "swept by either batch's rrq",
       301 not in _rrq_losers(two_batches + [other_batch]))
    ck("...and classify() agrees: it is a live candidate, not LOST",
       classify(other_batch, {}, _rrq_losers(two_batches + [other_batch]))[0]
       is None)
    # CANARY: an UNSTAMPED Version (no sg_batch_id at all) is untouched too --
    # "do nothing with it" is the module's explicit rule for the unstamped
    # case, not merely a side effect of key-matching.
    unstamped = {"id": 302, "sg_status_list": "rev", "sg_stage": "panel",
                "entity": ent}
    ck("CANARY: a Version with NO sg_batch_id is left alone, never guessed "
       "into a batch by (entity, stage) alone",
       302 not in _rrq_losers(two_batches + [unstamped]))
    ck("...and classify() agrees: an unstamped Version is a live candidate, "
       "not LOST",
       classify(unstamped, {}, _rrq_losers(two_batches + [unstamped]))[0]
       is None)
    # CANARY: an unstamped Version does not even get pulled into someone
    # ELSE's batch by accident -- blank/whitespace ids must not collide.
    blank1 = {"id": 303, "sg_status_list": "rrq", "sg_stage": "panel",
             "entity": ent, "sg_batch_id": ""}
    blank2 = {"id": 304, "sg_status_list": "rev", "sg_stage": "panel",
             "entity": ent, "sg_batch_id": "   "}
    ck("CANARY: two DIFFERENT unstamped/blank Versions never group with each "
       "other either",
       304 not in _rrq_losers([blank1, blank2]))

    class _Stub(object):
        """A stub that REMEMBERS ITS WRITES, which is the whole point.

        The first version returned the same hardcoded rows however many times
        it had been written to, so the "idempotent" canary below swept a fresh
        stub once and asserted the same number twice. It could not have failed:
        a genuinely non-idempotent sweep re-flags rows whose status the FIRST
        pass already changed, and a stub that never changes has no second
        state to be wrong about. A review agent named it, 2026-09-07."""

        def __init__(self):
            self.batches = []
            self.rows = self._initial()

        def _initial(self):
            return [
                {"id": 1, "code": "A_v001", "sg_status_list": "apr",
                 "sg_stage": "panel", "entity": ent, "created_at": 100},
                {"id": 2, "code": "A_v002", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": ent, "created_at": 110},
                {"id": 3, "code": "W_s9001", "sg_status_list": "rev",
                 "sg_stage": "wedge", "entity": ent, "created_at": 90},
                {"id": 4, "code": "B_v001", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 2},
                 "created_at": 90},
                # asked for BEFORE A_v001 was approved: satisfied, close it
                {"id": 5, "code": "A_v000", "sg_status_list": "rrq",
                 "sg_stage": "panel", "entity": ent, "created_at": 50},
                # asked for AFTER it: still open, must survive the whole sweep
                {"id": 6, "code": "A_v002b", "sg_status_list": "rrq",
                 "sg_stage": "panel", "entity": ent, "created_at": 150},
                # KIND 5, a third shot so it can't collide with the approval
                # scenario above: one 'rev' batch chosen an 'rrq' out of it.
                # Both OTHER siblings must be rejected, the rrq left alone.
                # Grouped purely by sg_batch_id, stamped at publish time.
                {"id": 7, "code": "C_v001", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 3},
                 "sg_batch_id": "REVIEW_C_v001"},
                {"id": 8, "code": "C_v002", "sg_status_list": "rrq",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 3},
                 "sg_batch_id": "REVIEW_C_v001"},
                {"id": 9, "code": "C_v003", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 3},
                 "sg_batch_id": "REVIEW_C_v001"},
                # ...and an UNRELATED 'rev' straggler at the SAME (entity,
                # stage) key (Shot 3, panel) from a DIFFERENT batch id: must
                # survive, exactly like SHOW01_A_0170_PNL_panel_v012 did next
                # to v017's rrq.
                {"id": 10, "code": "C_v000", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 3},
                 "sg_batch_id": "REVIEW_C_v000_earlier"},
                # ...and an UNSTAMPED 'rev' Version at the SAME key too, from
                # before this field existed: "do nothing with it" is the
                # module's explicit rule -- must ALSO survive.
                {"id": 11, "code": "C_v004", "sg_status_list": "rev",
                 "sg_stage": "panel", "entity": {"type": "Shot", "id": 3}},
            ]

        def find(self, *a, **k):
            return [dict(r) for r in self.rows]

        def batch(self, reqs):
            self.batches.extend(reqs)
            by_id = dict((r["id"], r) for r in self.rows)
            for q in reqs:
                row = by_id.get(q["entity_id"])
                if row is not None:
                    row.update(q["data"])

    st = _Stub()
    ck("a dry run writes nothing", sweep(st, dry=True) == 0 and not st.batches)
    st2 = _Stub()
    n = sweep(st2, dry=False)
    got = dict((b["entity_id"], b["data"]["sg_status_list"]) for b in st2.batches)
    ck("the alternate is rejected, the wedge retired too (same rjct status "
       "now, different reason), the SATISFIED request closed, and the rrq's "
       "OTHER batch-mates are rejected too",
       n == 5 and got == {2: LOST, 3: NOT_A_CANDIDATE, 5: LOST,
                          7: LOST, 9: LOST})
    ck("the live candidate on another shot survives", 4 not in got)
    ck("CANARY: the request made AFTER the approval survives the whole sweep",
       6 not in got)
    ck("CANARY: the 'rrq' Version whose batch this is stays 'rrq', never "
       "written by the sweep",
       8 not in got)
    ck("CANARY: a 'rev' Version with a DIFFERENT batch id at the same "
       "(entity, stage) key survives the rrq that rejected its real "
       "batch-mates",
       10 not in got)
    ck("CANARY: an UNSTAMPED 'rev' Version at the same key survives too -- "
       "an unstamped Version is simply not swept",
       11 not in got)
    # IDEMPOTENCE, tested properly: sweep the SAME stub a second time, so the
    # second pass reads back the statuses the first pass WROTE. A rule that
    # re-flags settled rows shows up here and nowhere else.
    before = len(st2.batches)
    sweep(st2, dry=False)
    ck("IDEMPOTENT: sweeping the SAME stub again, now carrying the first "
       "pass's writes, proposes NOTHING MORE",
       len(st2.batches) == before)

    # ---------------------------------------------------------- backfill
    bf_rows = [
        {"id": 1, "code": "X_v001", "sg_batch_id": "REVIEW_X_ALREADY",
         "sg_stage": "panel", "playlists": []},
        {"id": 2, "code": "X_v002", "sg_batch_id": None, "sg_stage": "panel",
         "playlists": [{"id": 1, "name": "REVIEW_X_v002", "type": "Playlist"}]},
        {"id": 3, "code": "X_v003", "sg_batch_id": None, "sg_stage": "panel",
         "playlists": []},
        {"id": 4, "code": "X_v004", "sg_batch_id": None, "sg_stage": "panel",
         "playlists": [{"id": 1, "name": "REVIEW_A", "type": "Playlist"},
                       {"id": 2, "name": "REVIEW_B", "type": "Playlist"}]},
        {"id": 5, "code": "X_v005", "sg_batch_id": None, "sg_stage": "panel",
         # A playlist that is NOT a REVIEW playlist must not count -- same as
         # zero, not guessed.
         "playlists": [{"id": 3, "name": "SOME_OTHER_PLAYLIST", "type": "Playlist"}]},
    ]
    to_stamp, counts = backfill_plan(bf_rows)
    ck("backfill_plan: the Version in exactly ONE REVIEW playlist is stamped "
       "with that playlist's own code",
       to_stamp == [(bf_rows[1], "REVIEW_X_v002")])
    ck("backfill_plan: an already-stamped Version is counted and left alone",
       counts["already"] == 1)
    ck("backfill_plan: zero REVIEW playlists (none, or only a non-REVIEW one) "
       "is counted, never guessed",
       counts["zero"] == 2)
    ck("backfill_plan: two-or-more REVIEW playlists is counted, never guessed",
       counts["multi"] == 1)
    ck("backfill_plan: exactly one Version total gets stamped",
       len(to_stamp) == 1)

    class _BackfillStub(object):
        def __init__(self, rows):
            self.rows, self.batches = rows, []

        def find(self, *a, **k):
            return [dict(r) for r in self.rows]

        def batch(self, reqs):
            self.batches.extend(reqs)

    ck("backfill_batch_id: a dry run writes nothing",
       backfill_batch_id(_BackfillStub(bf_rows), dry=True) == 0)
    live = _BackfillStub(bf_rows)
    n_bf = backfill_batch_id(live, dry=False)
    ck("backfill_batch_id: a live run stamps exactly the one unambiguous "
       "Version, with the playlist's own code",
       n_bf == 1 and len(live.batches) == 1
       and live.batches[0]["entity_id"] == 2
       and live.batches[0]["data"]["sg_batch_id"] == "REVIEW_X_v002")

    # -------------------------------------------- the heuristic is GONE
    # Built via chr() and searched against the source with COMMENT LINES
    # stripped, so this canary cannot pass merely because a comment
    # explaining the removal happens to name what was removed. A docstring
    # is NOT a comment and is deliberately left in the searched text.
    def _source_no_comments():
        path = os.path.abspath(__file__)
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        return "".join(kept)

    def _needle(codes):
        return "".join(chr(c) for c in codes)

    _src = _source_no_comments()
    _needle_a = _needle([82, 79, 85, 78, 68, 95, 71, 65, 80, 95, 83, 69,
                         67, 79, 78, 68, 83])
    _needle_b = _needle([95, 103, 97, 112, 95, 115, 101, 99, 111, 110, 100, 115])
    _needle_c = _needle([95, 114, 111, 117, 110, 100, 115])
    ck("CANARY: the old time-gap-threshold constant is entirely gone from "
       "this file's code and docstrings (comment lines stripped; a "
       "docstring is NOT a comment)",
       _needle_a not in _src)
    ck("CANARY: the old per-pair gap-in-seconds helper is entirely gone too",
       _needle_b not in _src)
    ck("CANARY: the old created_at-clustering helper is entirely gone too",
       _needle_c not in _src)

    # ------------------------------------- the retired status is GONE, tree-wide
    # Geoff, 2026-09-08: asked what the old second status meant, did not like
    # it, approved collapsing it into rjct everywhere it was written. Same
    # discipline as the heuristic-removal canary just above (needle via
    # chr(), comment lines stripped, docstrings kept), widened from "this
    # file" to every module under tools/, because the write sites were
    # spread across three files and a canary that only watched this one
    # would miss the other two going stale.
    #
    # TWO DOCUMENTED EXCLUSIONS. sg_review_playlists.py: its self_test()
    # carries the retired status ONLY as a READ-side fixture proving an
    # old-status Version is excluded the same as a rejected one -- 1,405
    # live Versions still carry it, so that behaviour has to keep being
    # tested. It is never passed to sg.update()/sg.create() there. Excluding
    # it by name is what "nothing WRITES it" means; it is not a claim the
    # string appears nowhere in the tree.
    #
    # genvideo_service.py: KNOWN, NOT YET FIXED as of 2026-09-08. It writes
    # the retired status at retire_superseded_panels() (the design-change
    # obsolete-panel path). That file was mid-edit for an unrelated watcher
    # fix at the same time as this pass, so the write there was reported,
    # not changed here, to avoid a collision -- see STATE.md / the sweep
    # report for that date. Remove this exclusion once that file's own pass
    # lands, or this canary will stop meaning what its name says.
    def _no_comments(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        kept = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
        return "".join(kept)

    _retired = _needle([111, 109, 116])              # the retired status, lowercase
    _quoted = ['"' + _retired + '"', "'" + _retired + "'"]
    _excluded = {"sg_review_playlists.py", "genvideo_service.py"}
    _tools_dir = os.path.dirname(os.path.abspath(__file__))
    _write_hits = []
    for fn in sorted(os.listdir(_tools_dir)):
        if not fn.endswith(".py") or fn in _excluded:
            continue
        fp = os.path.join(_tools_dir, fn)
        if not os.path.isfile(fp):
            continue
        text = _no_comments(fp)
        if any(q in text for q in _quoted):
            _write_hits.append(fn)
    ck("CANARY: no module in tools/ still writes the retired status as a "
       "quoted literal, except the two documented exclusions above "
       "(sg_review_playlists.py's read-side fixture, genvideo_service.py's "
       "known not-yet-fixed site); needle built via chr(), comment lines "
       "stripped, docstrings kept",
       not _write_hits)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default=None,
                    help="narrow to one episode; default is the whole project, "
                         "which is what DESIGN batches need")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill-batch-id", action="store_true",
                    help="ONE-TIME, explicit only: stamp sg_batch_id on Versions "
                         "published before the field existed, from the single "
                         "REVIEW playlist each already sits in. Never runs as "
                         "part of sweep(); combine with --dry-run to preview.")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    sg = PMB.get_backend()
    if a.backfill_batch_id:
        backfill_batch_id(sg, episode=a.episode, dry=a.dry_run)
        return 0
    sweep(sg, episode=a.episode, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
