#!/usr/bin/env python3
"""The R&D lab: an Episode that holds experiments, so testing stops polluting the show.

WHY THIS EXISTS. Geoff, 2026-09-06: "please make an R&D episode to contain the
testing you and a peer engineer are doing. I want the tests published with provenance info
in field for me to track and to have a long term record."

Today wedge Versions hang off REAL show Shots. `SHOW01_A_0110` carries
`SHOW01_A_0110_PNL_OQ1_qwencompose_s9001` and friends, which means a shot in the
delivered episode is also a filing cabinet for abandoned experiments. It works,
and it makes the show's own record harder to read every time we test something.

THE STRUCTURE, mapped onto entities that already exist rather than a new type:

    Episode  RND            the lab. One per project, never delivered.
      Sequence  RND_<AREA>  one per line of investigation (CAMERA, TWOCHAR, FPS)
        Shot  RND_<AREA>_<NNNN>_<slug>   ONE EXPERIMENT. Carries the question.
          Version  ..._<arm>_s<seed>     ONE CELL. Carries the evidence.

A Shot is a unit of work and a Version is an attempt, which is exactly the shape
of an experiment and its cells, so nothing new had to be invented to hold them.

PROVENANCE. Every field the show already records is recorded here unchanged and
by the same helper: seed, steps, cfg, size, model, workflow template, workflow
hash, and the prompt AS SENT. That last one matters most, because
`the-record-says-we-sent-it` is a defect class this project has already been
bitten by three times: provenance claiming a clause the GPU never received.

What the show does NOT have, and an experiment needs, is added here:

    sg_rnd_experiment   which experiment this cell belongs to (CAM-01)
    sg_rnd_arm          which cell within it (B1 ControlNet 1.0)
    sg_rnd_hypothesis   what was predicted BEFORE the run
    sg_rnd_result       what was measured
    sg_rnd_verdict      works / inert / mixed / refuted / pending
    sg_rnd_seat         which seat ran it (node-1-genvideo, a peer engineer)

THE HYPOTHESIS FIELD IS THE POINT, not decoration. Pre-registering a prediction
is why a peer engineer's ControlNet result was readable at all: it had said in writing that
structural conditioning would win, so its own contradicting number meant
something instead of being quietly reinterpreted. A result with no recorded
prediction can be rationalised into agreeing with whatever we now believe.

Usage:
    python rnd_lab.py --setup                 idempotent: schema + Episode + Sequences
    python rnd_lab.py --list                  what is in the lab
    python rnd_lab.py --self-test
"""
import argparse
import os
import sys

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

PROJECT_ID = 9999
PROJ = {"type": "Project", "id": PROJECT_ID}
EPISODE_CODE = "RND"

# One sequence per line of investigation. Extend freely; --setup is idempotent
# and never removes one, on the same "extend, never replace" rule the rest of
# the schema work here follows.
AREAS = {
    "RND_CAMERA": "Camera setups: moving the camera within one set",
    "RND_TWOCHAR": "Two characters in one panel",
    "RND_FPS": "Frame rate, interpolation and motion quality",
    "RND_SET": "Set anchors, plates and background continuity",
    "RND_MODEL": "Model, LoRA and sampler comparisons",
    "RND_COMP": "Integrating the character INTO the room: ground contact, perspective, lighting",
    "RND_KEY": "Keyframes: first / last / middle panel driving a generation",
}

VERDICTS = ["pending", "works", "inert", "mixed", "refuted", "abandoned"]

# A control cell is not a lesser cell: it is the baseline the test arm
# is only meaningful against. Kept as a list so it filters in the UI.
ROLES = ["control", "test", "probe"]

# The episode-link field differs BY ENTITY TYPE and is not guessable.
# Imported from the tool that already established it, so the two cannot drift.
try:
    import sg_link_shots_to_episode as _LINK
    SEQ_EPISODE_FIELD = dict(_LINK.TARGETS)["Sequence"]
    SHOT_EPISODE_FIELD = dict(_LINK.TARGETS)["Shot"]
except Exception:                                             # noqa: BLE001
    # Fail to the VERIFIED values rather than to a guess, and say so.
    SEQ_EPISODE_FIELD, SHOT_EPISODE_FIELD = "episode", "sg_episode"

# The R&D-only fields. Everything else reuses the show's own provenance fields.
RND_FIELDS = [
    ("sg_rnd_experiment", "text", "RnD Experiment"),
    ("sg_rnd_arm", "text", "RnD Arm"),
    ("sg_rnd_hypothesis", "text", "RnD Hypothesis"),
    ("sg_rnd_result", "text", "RnD Result"),
    ("sg_rnd_seat", "text", "RnD Seat"),
    # The next three are a peer engineer's, and they improve on my first list.
    #
    # sg_rnd_axis: what was actually VARIED. Without it a reader six months
    # from now sees two renders and cannot tell that one was strength 0.8 and
    # the other 0.4, which is the entire dose-response finding.
    #
    # sg_rnd_limits: WHAT THIS DOES NOT ESTABLISH. The best idea in a peer engineer's
    # list. This project has twice generalised a mechanism from a single shot
    # and been wrong (see the memory `a-shot-is-a-sample-too`), and a wedge
    # gets over-quoted precisely because its caveats live in a chat message
    # that scrolls away. Storing the caveat NEXT TO the result is what stops
    # that.
    #
    # sg_rnd_role: a CONTROL cell is not a lesser cell. a peer engineer's no-control
    # render IS the finding; on its own it looks like an ordinary image.
    ("sg_rnd_axis_varied", "text", "RnD Axis Varied"),
    ("sg_rnd_does_not_establish", "text", "RnD Does NOT Establish"),
]


def log(m):
    print("[rnd] %s" % m, flush=True)


def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def _sg(be):
    return getattr(be, "sg", None) or getattr(be, "_sg", None)


# ------------------------------------------------------------------- schema
def setup_schema(sg, log=log):
    """Idempotent. Creates the R&D fields on Version if absent, and extends
    sg_stage with 'rnd'. EXTENDS, never replaces: the same invariant the rest
    of this codebase's schema work follows, because a replace here would
    silently drop stages the show depends on."""
    existing = sg.schema_field_read("Version")
    created = []
    for code, dtype, display in RND_FIELDS:
        if code in existing:
            continue
        sg.schema_field_create("Version", dtype, display)
        created.append(code)
    if created:
        log("created Version fields: %s" % ", ".join(created))
    else:
        log("Version R&D fields already present, no-op")

    # A verdict list, so it filters in the UI instead of being free text.
    if "sg_rnd_verdict" not in existing:
        sg.schema_field_create("Version", "list", "RnD Verdict",
                               properties={"valid_values": VERDICTS})
        log("created Version.sg_rnd_verdict with %s" % VERDICTS)
    if "sg_rnd_role" not in existing:
        sg.schema_field_create("Version", "list", "RnD Role",
                               properties={"valid_values": ROLES})
        log("created Version.sg_rnd_role with %s" % ROLES)

    # An R&D render is not a show stage. Give it its own value so review
    # housekeeping and every stage-keyed query can tell them apart -- the same
    # reasoning that put 'wedge' in this list originally.
    info = sg.schema_field_read("Version", "sg_stage")
    cur = info["sg_stage"]["properties"]["valid_values"]["value"]
    if "rnd" not in cur:
        sg.schema_field_update("Version", "sg_stage", {"valid_values": cur + ["rnd"]})
        log("Version.sg_stage extended with 'rnd' -- now: %s" % (cur + ["rnd"]))
    else:
        log("Version.sg_stage already has 'rnd', no-op")
    return created


def ensure_lab(sg, log=log):
    """Idempotent: the RND Episode and one Sequence per area. Returns
    (episode_row, {area_code: sequence_row})."""
    ep = sg.find_one("Episode", [["project", "is", PROJ],
                                 ["code", "is", EPISODE_CODE]], ["code"])
    if not ep:
        ep = sg.create("Episode", {"project": PROJ, "code": EPISODE_CODE,
                                   "description":
                                   "R&D lab. Experiments and wedges live here so "
                                   "they stop hanging off delivered show Shots. "
                                   "Never delivered, never cut."})
        log("created Episode %s (id %s)" % (EPISODE_CODE, ep["id"]))
    else:
        log("Episode %s already exists (id %s)" % (EPISODE_CODE, ep["id"]))

    seqs = {}
    for code, desc in sorted(AREAS.items()):
        row = sg.find_one("Sequence", [["project", "is", PROJ],
                                       ["code", "is", code]], ["code"])
        if not row:
            # SEQUENCE USES THE NATIVE `episode`, SHOT USES THE CUSTOM
            # `sg_episode`. Not a style choice and not guessable: it is
            # verified in sg_link_shots_to_episode.TARGETS and I got it wrong
            # here first, creating the Episode and then failing on the very
            # next call with "Sequence.sg_episode doesn't exist". Imported
            # rather than restated so the two cannot drift (invariant 11).
            row = sg.create("Sequence", {"project": PROJ, "code": code,
                                         "description": desc,
                                         SEQ_EPISODE_FIELD:
                                             {"type": "Episode", "id": ep["id"]}})
            log("  created Sequence %s" % code)
        seqs[code] = row
    return ep, seqs


def ensure_experiment(sg, area, slug, question, seq_row, episode_id=None, log=log):
    """Idempotent: one Shot per experiment. `question` is what it is asking,
    stored where a human reads it."""
    if area not in AREAS:
        raise ValueError("unknown area %r; known: %s" % (area, sorted(AREAS)))
    code = "%s_%s" % (area, slug)
    row = sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", code]], ["code"])
    if row:
        return row, False
    data = {"project": PROJ, "code": code, "description": question,
            "sg_sequence": {"type": "Sequence", "id": seq_row["id"]}}
    if episode_id:
        data[SHOT_EPISODE_FIELD] = {"type": "Episode", "id": episode_id}
    row = sg.create("Shot", data)
    log("  created experiment Shot %s" % code)
    return row, True


def publish_cell(sg, shot_row, code, arm, hypothesis, result, verdict, seat,
                 role="test", axis="", limits="",
                 media_path=None, provenance=None, log=log):
    """One CELL of an experiment: a Version carrying the evidence.

    provenance is the SHOW's own fields, passed through unchanged so an R&D
    render is auditable by exactly the same query as a show render: seed,
    steps, cfg, size, model, workflow template, workflow hash, prompt as sent.

    A cell with no media is allowed and is NOT a silent no-op: some results are
    numbers rather than pictures (a pixel-difference table, a frame-sharpness
    measurement). The verdict and the result text are the record then, and the
    log says so rather than implying an image exists."""
    if verdict not in VERDICTS:
        raise ValueError("verdict %r not in %s" % (verdict, VERDICTS))

    # PRE-REGISTRATION IS A PRECONDITION OF PUBLISHING, not a habit.
    #
    # a peer engineer reviewed the rule doc and caught that the pre-registration rule
    # failed the doc's OWN test ("a rule that depends on remembering is not a
    # rule; prefer rules a script can fail"). Nothing could check that a
    # prediction was written BEFORE a result: a reader of the finished record
    # sees both in one file, committed together, and cannot tell the order.
    #
    # a peer engineer's fix, which is better than mine: make the pre-registration its own
    # COMMIT before the render, so git timestamps it and ordering becomes a
    # fact. This is the ShotGrid half of that: an experiment carries its
    # hypothesis, and a cell CANNOT be published against an experiment that has
    # none. You must register the prediction before you can record a result.
    if not (hypothesis or "").strip():
        raise ValueError(
            "REFUSED: cell %s has no hypothesis. The prediction is registered "
            "BEFORE the result, or the result can be rationalised into agreeing "
            "with whatever we now believe." % code)
    if not (limits or "").strip():
        raise ValueError(
            "REFUSED: cell %s does not say what it does NOT establish. That line "
            "is what stops a wedge being over-quoted later, and this project has "
            "generalised from a single shot and been wrong twice." % code)
    if role not in ROLES:
        raise ValueError("role %r not in %s" % (role, ROLES))
    existing = sg.find_one("Version", [["project", "is", PROJ], ["code", "is", code]], ["code"])
    if existing:
        log("  cell %s already published, no-op" % code)
        return existing, False

    data = {
        "project": PROJ,
        "entity": {"type": "Shot", "id": shot_row["id"]},
        "code": code,
        "sg_stage": "rnd",
        # An experiment is never a review candidate for the show. rjct keeps
        # it out of the review queue that sg_review_housekeeping sweeps
        # (collapsed 2026-09-08 from a status of its own, which Geoff did
        # not like and approved retiring; the reason is on this row, not
        # the status), which is the whole reason these were polluting show
        # Shots before.
        "sg_status_list": "rjct",
        "sg_rnd_arm": arm,
        "sg_rnd_hypothesis": hypothesis,
        "sg_rnd_result": result,
        "sg_rnd_verdict": verdict,
        "sg_rnd_seat": seat,
        "sg_rnd_experiment": shot_row["code"],
        "sg_rnd_role": role,
        "sg_rnd_axis_varied": axis,
        "sg_rnd_does_not_establish": limits,
        "description": "%s | %s" % (arm, result),
    }
    for k, v in (provenance or {}).items():
        data[k] = v
    v = sg.create("Version", data)
    if media_path and os.path.isfile(media_path):
        sg.upload("Version", v["id"], media_path,
                  field_name="sg_uploaded_movie" if media_path.lower().endswith(".mp4")
                  else "sg_uploaded_movie", display_name=code)
        log("  cell %s published WITH media" % code)
    else:
        log("  cell %s published, numbers only (no media attached)" % code)
    return v, True


def verify_fields(sg, log=log):
    """Every field publish_cell() writes must EXIST on the live site.

    SHOTGRID DERIVES THE FIELD CODE FROM THE DISPLAY NAME, not from anything
    you pass. Asking for "RnD Axis Varied" produced `sg_rnd_axis_varied`, and
    "RnD Does NOT Establish" produced `sg_rnd_does_not_establish`, while the
    code writing them said `sg_rnd_axis` and `sg_rnd_limits`. Those writes
    would have gone to fields that do not exist.

    So the codes are DISCOVERED here rather than trusted. Returns the list of
    missing codes; empty is good."""
    live = set(sg.schema_field_read("Version").keys())
    want = [f[0] for f in RND_FIELDS] + ["sg_rnd_verdict", "sg_rnd_role"]
    missing = [c for c in want if c not in live]
    if missing:
        log("MISSING on the live site: %s" % ", ".join(missing))
        log("  (ShotGrid names fields from the DISPLAY name; re-run --setup "
            "and check what code it actually made)")
    else:
        log("all %d R&D field codes verified present on Version" % len(want))
    return missing


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("every area code is prefixed RND_, so the lab is greppable",
       all(a.startswith("RND_") for a in AREAS))
    ck("verdicts include the two that matter most: inert and refuted",
       "inert" in VERDICTS and "refuted" in VERDICTS)
    ck("hypothesis is a FIELD, not a convention in the description",
       any(f[0] == "sg_rnd_hypothesis" for f in RND_FIELDS))
    ck("seat is recorded, so a result can be traced to the box that ran it",
       any(f[0] == "sg_rnd_seat" for f in RND_FIELDS))
    ck("the AXIS varied is a field, or a dose response is unreadable later",
       any(f[0] == "sg_rnd_axis_varied" for f in RND_FIELDS))
    ck("CANARY: 'what this does NOT establish' is a field, not a chat message",
       any(f[0] == "sg_rnd_does_not_establish" for f in RND_FIELDS))
    ck("a control cell can be labelled as such", "control" in ROLES)

    # THE PRE-REGISTRATION CANARIES. a peer engineer's review: the rule "record the
    # prediction before the result" could not be checked by any script, so it
    # failed the record's own standard. It is a PRECONDITION of the publish
    # now, and these two prove the refusal actually fires. Removing either one
    # silently returns the rule to the honour system.
    class _Stub(object):
        def find_one(self, *a, **k):
            return None

        def create(self, *a, **k):
            return {"id": 1}

    def _refuses(**kw):
        try:
            publish_cell(_Stub(), {"id": 1, "code": "X"}, "CELL", arm="a",
                         result="r", verdict="works", seat="node-1", **kw)
            return False
        except ValueError:
            return True

    ck("CANARY: publishing a cell with NO hypothesis is refused",
       _refuses(hypothesis="", limits="something"))
    ck("CANARY: publishing a cell with no 'does NOT establish' is refused",
       _refuses(hypothesis="something", limits=""))
    ck("a cell WITH both is not refused (the guard is not just always-on)",
       not _refuses(hypothesis="something", limits="something"))

    # CANARY: an experiment cell is retired to rjct, not the status this
    # collapsed from 2026-09-08 (Geoff did not like it, approved retiring
    # it). Captures the actual dict passed to sg.create(), not a constant
    # under test, so a regression back to the old literal fails here.
    class _CaptureStub(_Stub):
        def __init__(self):
            self.created = []

        def create(self, entity_type, data):
            self.created.append(data)
            return {"id": 1}

    cap = _CaptureStub()
    publish_cell(cap, {"id": 1, "code": "S"}, "CELL2", arm="a", result="r",
                verdict="works", seat="node-1", hypothesis="h", limits="l")
    ck("CANARY: publish_cell writes sg_status_list=rjct for the experiment "
       "cell, never the retired literal",
       cap.created and cap.created[0]["sg_status_list"] == "rjct")

    class _SG(object):
        def __init__(self):
            self.created = []
            self.updated = []

        def schema_field_read(self, entity, field=None):
            if field == "sg_stage":
                return {"sg_stage": {"properties":
                                     {"valid_values": {"value": ["panel", "wedge"]}}}}
            return {}      # no R&D fields yet

        def schema_field_create(self, entity, dtype, display, properties=None):
            self.created.append((entity, dtype, display))

        def schema_field_update(self, entity, field, props):
            self.updated.append((entity, field, props))

    sg = _SG()
    setup_schema(sg, log=lambda m: None)
    ck("setup creates every R&D field when none exist",
       len(sg.created) == len(RND_FIELDS) + 2)   # +2: the verdict and role lists
    ck("CANARY: sg_stage is EXTENDED, never replaced",
       sg.updated and sg.updated[0][2]["valid_values"] == ["panel", "wedge", "rnd"])
    ck("the existing stages survive the extension",
       "panel" in sg.updated[0][2]["valid_values"]
       and "wedge" in sg.updated[0][2]["valid_values"])

    class _SGHas(_SG):
        def schema_field_read(self, entity, field=None):
            if field == "sg_stage":
                return {"sg_stage": {"properties":
                                     {"valid_values": {"value": ["panel", "rnd"]}}}}
            return dict((f[0], {}) for f in
                        RND_FIELDS + [("sg_rnd_verdict", "", ""),
                                      ("sg_rnd_role", "", "")])

    sg2 = _SGHas()
    setup_schema(sg2, log=lambda m: None)
    ck("setup is IDEMPOTENT: a second run creates and updates nothing",
       not sg2.created and not sg2.updated)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--setup", action="store_true",
                    help="idempotently create the schema, Episode and Sequences")
    ap.add_argument("--list", action="store_true", help="show what is in the lab")
    ap.add_argument("--verify", action="store_true",
                    help="check every field publish_cell writes exists live")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    sg = _sg(sg_connect())
    if a.setup:
        setup_schema(sg)
        ensure_lab(sg)
        return 1 if verify_fields(sg) else 0
    if a.verify:
        return 1 if verify_fields(sg) else 0
    if a.list:
        ep = sg.find_one("Episode", [["project", "is", PROJ],
                                     ["code", "is", EPISODE_CODE]], ["code"])
        if not ep:
            log("no RND Episode yet -- run --setup")
            return 1
        shots = sg.find("Shot", [["project", "is", PROJ],
                                 ["code", "starts_with", "RND_"]],
                        ["code", "description", "sg_sequence"])
        log("RND Episode id %s, %d experiment(s)" % (ep["id"], len(shots)))
        for s in sorted(shots, key=lambda r: r["code"]):
            vs = sg.find("Version", [["entity", "is", {"type": "Shot", "id": s["id"]}]],
                         ["code", "sg_rnd_verdict"])
            log("  %-38s %d cell(s)  %s" % (s["code"], len(vs),
                                            (s.get("description") or "")[:60]))
        return 0
    ap.error("one of --setup, --list or --self-test is required")


if __name__ == "__main__":
    sys.exit(main())
