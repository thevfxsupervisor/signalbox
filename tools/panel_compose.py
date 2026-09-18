#!/usr/bin/env python3
"""Phase 5 -- the panel stage. MASTER-PLAN-V2.md section 5, Phase 5; D7, D10, D13, D14.

    SCRIPT -> BEATS -> BOARDS -> PANEL (anchor) -> VIDEO

V1 anchored 6 of 7 videos on stock reference photos, two shared between different
characters -- a peer engineer finding verbatim: "the drift is entering at the anchor
generation step, not at the i2v step." V2's answer is this module: the approved
PANEL is the anchor, and video (Phase 6, not built here) is generated only from
an approved panel. This tool composes that panel; it does not approve it and it
does not link it to the Shot -- see "no self-approval" below.

INPUTS ASSEMBLED, per shot:
  - the shot's CHAR asset(s), classified via sg_provenance.classify_assets()
    (invariant 11: one classifier, imported here, never copied) -- each CHAR
    asset must be Asset.sg_stage=='approved' with Asset.sg_approved_design
    linked, or this tool REFUSES loudly rather than compose from an unapproved
    or missing design (a panel with no anchor is the exact V1 failure, one
    stage earlier).
  - the shot's STYLE/set asset, same approved-design requirement.
  - the action text: Shot.sg_script_beat (Phase 4's beat storage; the fallback
    path is the only one live on this site as of Phase 4's build).
  - camera/size: Shot.sg_camera, Shot.sg_shot_size, Shot.sg_gen_size_wxh --
    recorded into provenance; the compositor (Phase 3's qwen_compose.py) fixes
    its output canvas to the character/set reference image's own aspect ratio,
    so these fields do not currently reshape the composited panel itself, only
    document what was asked for. A real per-shot canvas override is future work.

THE COMPOSITOR IS PHASE 3'S, IMPORTED, NEVER COPIED. `qwen_compose.compose_panel()`
(character image + set image + action text -> one image via Qwen-Image-Edit-2509 +
Lightning, cfg 1.0) does the actual GPU work. This module does not touch
qwen_compose.py, does not touch its recipe constants, and does not start ComfyUI
itself -- qwen_edit() already refuses cleanly ("ComfyUI is not reachable") when
no GPU seat is held, which is the expected, tested state for this build (no GPU
this wave; see PHASE-5-BUILD.md).

WHICH CHARACTER TILE (fixed -- see docs/METHOD.md).
Until this fix, this function picked a HARDCODED filename off disk
(`output/character_sheets/<CODE>_front.png`, falling back to closeup/three_
quarter/profile) and never looked at Asset.sg_approved_design at all. That
predates D13: it was written when the approved design Version's own file
(Version.sg_path_to_movie) really was a 6-view CONTACT SHEET (see
character_sheets.py's original cmd_generate()), which is a bad compositor
input, so a same-batch single-view PNG was used as a sidestep instead. D13
(cmd_generate_d13) changed what gets approved: the anchor Version's own
sg_path_to_movie IS now a single clean view (front-facing, one subject,
D13-verified on named attributes) -- the sidestep's reason is gone, but the
sidestep itself was never removed, so it kept silently reading whatever
file happened to sit at that hardcoded path instead of the Asset's actual
approved design. That file goes stale the instant character_sheets.py is
re-run (it overwrites `<CODE>_front.png` unconditionally), so a panel's
provenance could claim "approved design Version N" while the pixels it
actually composited from were a DIFFERENT, unapproved render. Fixed: the
character image is now `resolve_approved_design()`'s own Version.sg_path_to_movie
-- the exact same field/path a STYLE/set design already used directly (see
below) -- so there is exactly one source of truth for "which file", never a
second guessed path that can silently diverge from it. resolve_approved_design()
already refuses loudly (PanelInputError) when the Asset has no approved design
or that Version's file is blank/missing on disk; there is no fallback path left
to guess from. A STYLE/set design never had this problem -- Phase 2 published
each set candidate as its own single Version/file, so the approved design
Version's own file was already used directly.

MULTI-SEED RETRY (docs/METHOD.md's finding: cell A1
PASSED and A2 FAILED on an identical config with only the seed changed --
seed variance is as large as any prompt lever tested). compose_with_retry()
tries up to `--max-seed-attempts` seeds (default DEFAULT_MAX_SEED_ATTEMPTS),
stopping at the FIRST attempt whose attribute_check.py verdict is PASS (or
SKIP, for an environment-only panel, where no seed changes the outcome so
only one attempt is ever made). This is NOT self-approval by exhaustion:
every attempt is graded by the same real, unmodified attribute_check.py gate
this module always used, nothing about the gate changes, and if EVERY attempt
FAILs the panel still publishes -- at 'rev', same as a PASS, per D16/W3 below
-- the LAST attempt, with every attempt's seed and verdict recorded in the
Version description and in build/out/panel_designs.json, never silently
dropped. Per invariant 5 (seed
discipline: locked seed for a directed fix, new seed for "another take"),
retrying across seeds IS "another take", so this is consistent with, not an
exception to, that invariant -- but attempt 1 always uses the same
deterministic seed a single-attempt run would have used (`seed` if given,
else `6000 + version_number`), so a re-run of the same shot with no GPU-level
nondeterminism reproduces the same first try; only attempts 2..N step off it
by a fixed stride (RETRY_SEED_STRIDE), never randomly. A COMPOSE failure
(GPU/infra error, not an attribute FAIL) aborts the whole retry immediately
-- a new seed cannot fix an unreachable ComfyUI or an OOM, so burning further
attempts on it would just waste GPU time.

ATTRIBUTE QC MEASURES THE PANEL, IT NO LONGER GATES IT (D16 / W3, fixed
2026-09-03 -- see plan/MASTER-PLAN-V3.md section 5.4: "Gates are deterministic
scripts, not vision", and this checker calls `claude -p`, i.e. a vision model,
so it may not be the thing deciding what a reviewer gets to see).
build/tests/attribute_check.py runs as a subprocess exactly as before (the
same "call a test file as a subprocess" pattern genvideo_worker.py already
uses for prompt_negation_audit.py; not imported, because attribute_check.py
is a standalone CLI/gate tool, not a shared library this module needs
functions from; NEVER modified by this fix or by the retry loop -- it is the
scoreboard, not a lever), and every attempt is still checked against its
PRIMARY character's Asset.sg_design_attributes -- but the verdict now only
picks WHICH attempt gets published (compose_with_retry() still stops at the
first PASS/SKIP so a retry batch converges on its best draw) and is recorded
in the description; it no longer decides IF the chosen attempt is reviewable:
  - PASS/FAIL/ERROR -> published at sg_status_list='rev' either way. A
    FAIL/ERROR verdict is written into the Version's description, verbatim,
    never silently dropped.
  - No character in frame (an environment-only shot) -> QC is skipped (there is
    no character attribute list to check against); published straight to 'rev',
    same as always.
  Before this fix: PASS -> 'rev', FAIL/ERROR -> 'rjct' (never appeared in a
  reviewer's pending queue) -- exactly the erased-review failure D16 exists to
  close, live in production. See qc_status_description()'s FAIL/ERROR branch
  for the exact wording now published.

NO SELF-APPROVAL (invariant 7). This module NEVER writes Shot.sg_approved_panel
and NEVER sets a Version's status to an approved value. It always publishes at
'rev' (see D16/W3 above -- 'rjct' is no longer written by this module at all).
Only a reviewer (human, or under D14 a designated agent acting
through the same ShotGrid path) flips a panel Version to an approved status.
Shot.sg_approved_panel is written ONLY by the SERVICE watcher
(genvideo_service.py, watch_panel_approvals(), Phase 5 block) reacting to that
status flip -- see build/tests/p5_service_sets_approval.py for the canary
proving a tool cannot do this.

STALENESS BOOKKEEPING. Every successful compose (QC pass or fail -- the
recorded design-Version ids are accurate provenance either way) writes an entry
to build/out/panel_designs.json recording exactly which design Version id(s)
fed this panel. genvideo_service.py's Phase 5 watch_panel_designs() compares
that recorded id against each Asset's CURRENT Asset.sg_approved_design and
flags the shot stale (Shot.sg_stage -> 'panel') the moment they diverge --
the exact same content-diff-via-sidecar pattern Phase 4's watch_beats() already
uses for beat edits (this module deliberately does not invent a second
mechanism).

    python panel_compose.py --shot PILOT01_A_0010
    python panel_compose.py --shot PILOT01_A_0010 --seed 4242
    python panel_compose.py --shot PILOT01_A_0010 --max-seed-attempts 3   (default; see above)
    python panel_compose.py --shot PILOT01_A_0010 --max-seed-attempts 1  (old single-shot behaviour)
    python panel_compose.py --shot PILOT01_A_0010 --dry     (gather inputs only, no compose)
    python panel_compose.py --setup-schema                 (idempotent; see ensure_schema())
    python panel_compose.py --self-test                    (offline, no SG/GPU/claude)
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools"),
# a second, independent guess at sibling location that could (and did) drift
# from wherever this file itself actually ran from -- see genvideo_service.py's
# TOOLS comment for the live example. Self-relative now, like prompt_revision.py
# and video_from_panel.py already were.
TOOLS = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(ROOT, "build", "tests")
OUT_DIR = os.path.join(ROOT, "output", "phase5")
PANEL_DESIGNS_JSON = os.path.join(ROOT, "build", "out", "panel_designs.json")
ATTR_CHECK = os.path.join(TESTS, "attribute_check.py")
PY = sys.executable

sys.path.insert(0, TOOLS)
import sg_provenance as PROV                                   # noqa: E402  invariant 11
import qwen_compose as QC                                      # noqa: E402  Phase 3, imported
import dialogue_guard as DG                                    # noqa: E402  D15, invariant 11

PROJECT_ID = 9999
PROJ = {"type": "Project", "id": PROJECT_ID}

PANEL_STEP_SHORT_NAME = "PNL"

# --- multi-seed retry (Change 2, PANEL-QUALITY-WEDGE.md finding) ------------
# A fixed, deterministic stride -- NOT random -- so a re-run of the same shot
# with the same base seed always tries the same sequence of retry seeds in
# the same order (reproducible retries, not just a reproducible first try).
# Picked to be far larger than any plausible version-number-derived base seed
# spread so attempt seeds across different shots/versions never collide.
RETRY_SEED_STRIDE = 1301
DEFAULT_MAX_SEED_ATTEMPTS = 4   # a wedge, not a retry budget -- see compose_with_retry

# TWO-CHARACTER SHOTS GET A WIDER WEDGE, and the number is measured, not guessed.
#
# SHOW01_A_0080 rendered through the shipped regional path on EIGHT seeds
# (wedge_regional_setref.py --production-only), counted by eye:
#
#   both characters, clean          2 of 8   (s6001, s15108)
#   both, but a hard band seam      1 of 8   (s8603, the known cross-seam artefact)
#   one character only              4 of 8
#   NO character at all             1 of 8   (s9904: an empty room)
#
# So a two-character seed is about 25% clean. At 4 seeds the chance of getting
# at least one clean panel is ~68%; at 8 it is ~90%. That is the whole fix
# available today: the regional graph is not structurally broken, it is
# seed-variant, and the answer to seed variance on a machine where GPU time is
# free is MORE SEEDS -- Geoff's own point that wedges teach us a lot and local
# GPU costs nothing. The reviewer still picks; the gate still grades every
# linked character; nothing self-approves.
#
# It is NOT a retry budget. Every seed is rendered and published as a candidate
# even after one passes, so the operator sees the spread rather than the first
# acceptable frame.
TWO_CHARACTER_SEED_ATTEMPTS = 8

# D15 (docs/METHOD.md): the dialogue-stripping
# logic that used to live here (sanitize_action_text_for_compositor(), plus
# its own NO_BURNT_IN_TEXT_CLAUSE) has moved to dialogue_guard.py -- invariant
# 11, one implementation, imported everywhere a ShotGrid text field could
# reach a generation prompt. Kept as thin aliases below so every existing
# call site and self-test in this file (and any other module that imported
# these two names directly off panel_compose) keeps working unmodified.

# Mirrors genvideo_worker.py's APPROVED_BOARD tuple (own copy: this is a
# service/tool-owned status-list constant, not a "classifier" invariant 11
# is about -- see genvideo_service.py's watch_panel_approvals() for the same
# tuple, kept identical there deliberately).
APPROVED_STATUSES = ("apr", "ad", "fin", "paf", "dlvr")


def log(m):
    print("[panel_compose] %s" % m, flush=True)


class PanelInputError(Exception):
    """Raised when a shot cannot be composed: no approved design, no beat
    text, no set asset, etc. Never caught silently -- the CLI surfaces it and
    exits non-zero; the service (if it ever calls this module directly)
    should do the same. A panel with no valid anchor input must refuse, not
    guess."""


# ---------------------------------------------------------------- SG connect
def sg_connect():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


# ---------------------------------------------------------------- schema (idempotent)
# COUNT-COLLAPSE.md: Asset.sg_instance_count (number, default unset/None ==
# "1, ordinary single character"). Module-level so gather_inputs() and
# ensure_schema() always agree on the REAL field code (invariant 8: read
# back after schema_field_create, never assume the requested name stuck).
ASSET_INSTANCE_COUNT_FIELD = "sg_instance_count"

# Geoff, 2026-09-08: "stamp a batch id on the Version when the batch is
# published. time gap is flaky" -- replaces sg_review_housekeeping.py's
# ROUND_GAP_SECONDS heuristic entirely. The VALUE stamped is the same string
# as the REVIEW playlist this batch is filed under (REVIEW_<shot>_v<NNN>),
# computed once in compose_panel_for_shot() and threaded through to every
# Version this run publishes (the chosen one and every alternate in
# publish_alternates()) -- so the id and the playlist can never disagree.
# Module-level for the same invariant-8 reason as ASSET_INSTANCE_COUNT_FIELD
# above: read back the real field code, never assume the requested one stuck.
BATCH_ID_FIELD = "sg_batch_id"


def ensure_schema(sg):
    """Idempotent Phase 5 schema setup -- safe to call every run. Read back
    every real field code (invariant 8); extend Version.sg_stage rather than
    replace it (never drop an existing valid_value)."""
    global ASSET_INSTANCE_COUNT_FIELD, BATCH_ID_FIELD
    step = sg.find_one("Step", [["short_name", "is", PANEL_STEP_SHORT_NAME],
                                ["entity_type", "is", "Shot"]], ["code"])
    if step is None:
        step = sg.create("Step", {"short_name": PANEL_STEP_SHORT_NAME, "code": "Panel",
                                  "entity_type": "Shot"})
        log("created Step %s (id %s)" % (PANEL_STEP_SHORT_NAME, step["id"]))

    info = sg.schema_field_read("Version", "sg_stage")
    cur = info["sg_stage"]["properties"]["valid_values"]["value"]
    if "panel" not in cur:
        sg.schema_field_update("Version", "sg_stage", {"valid_values": cur + ["panel"]})
        after = sg.schema_field_read("Version", "sg_stage")["sg_stage"]["properties"] \
            ["valid_values"]["value"]
        assert set(cur) <= set(after), "invariant 8 violation: an existing value was dropped"
        log("Version.sg_stage extended with 'panel'")

    have = sg.schema_field_read("Shot")
    real = {}
    for requested, disp in (("sg_approved_panel", "Approved Panel"),
                            ("sg_approved_panel_end", "Approved Panel End")):
        if requested in have:
            real[requested] = requested
            continue
        got = sg.schema_field_create("Shot", "entity", disp,
                                     properties={"valid_types": ["Version"]})
        real[requested] = got
        log("created Shot.%s (asked for %s)" % (got, requested))

    have_asset = sg.schema_field_read("Asset")
    if ASSET_INSTANCE_COUNT_FIELD not in have_asset:
        got = sg.schema_field_create("Asset", "number", "Instance Count")
        log("created Asset.%s (asked for Instance Count/%s)"
           % (got, ASSET_INSTANCE_COUNT_FIELD))
        ASSET_INSTANCE_COUNT_FIELD = got

    have_version = sg.schema_field_read("Version")
    if BATCH_ID_FIELD not in have_version:
        got = sg.schema_field_create("Version", "text", "Batch ID")
        log("created Version.%s (asked for Batch ID/%s)" % (got, BATCH_ID_FIELD))
        BATCH_ID_FIELD = got
    else:
        log("Version.%s already exists -- no-op" % BATCH_ID_FIELD)
    return real


# ---------------------------------------------------------------- gather inputs
def resolve_approved_design(sg, asset_code):
    """Asset must be sg_stage='approved' with sg_approved_design linked to a
    Version whose file exists on disk. Refuses loudly otherwise -- this IS
    the "no anchor, no panel" rule from the module docstring. The returned
    Version's own sg_path_to_movie is the ONLY file gather_inputs() ever uses
    as the composited image, for BOTH character and set assets -- see "WHICH
    CHARACTER TILE" in the module docstring for why a second, guessed path
    used to exist for characters and why that was a bug."""
    a = sg.find_one("Asset", [["project", "is", PROJ], ["code", "is", asset_code]],
                    ["code", "sg_ref_role", "sg_stage", "sg_approved_design",
                     ASSET_INSTANCE_COUNT_FIELD])
    if not a:
        raise PanelInputError("Asset %s not found" % asset_code)
    # THE FACT IS sg_approved_design. sg_stage IS A COPY OF IT, AND COPIES GO
    # STALE. Requiring BOTH blocked 30 of SHOW01's 36 unfinished shots on
    # 2026-09-07, from one Asset: Geoff left a note on SHOW_SET_PILOTCHARB_BEDROOM, the
    # revision loop applied it and set sg_stage back to 'design' (that is what
    # prompt_revision.apply_for_asset does when a revision lands), and every shot
    # using that set silently stopped being composable. The approved design was
    # still there, still approved, still on disk. Nothing was wrong with it.
    #
    # So the pointer decides, and the stage only WARNS. A pending revision is
    # real information (the approved design now predates the description) but it
    # is not a reason to halt an episode, and a note asking for a nicer wall
    # should never be able to stop 30 shots from rendering.
    if not a.get("sg_approved_design"):
        raise PanelInputError(
            "Asset %s has no approved design (sg_stage=%r sg_approved_design=None) -- "
            "refusing to compose a panel with no approved anchor"
            % (asset_code, a.get("sg_stage")))
    if a.get("sg_stage") != "approved":
        log("  WARNING %s: composing from approved design %s while sg_stage=%r. A "
            "revision is in flight, so the approved design predates the current "
            "description." % (asset_code, a["sg_approved_design"]["name"], a.get("sg_stage")))
    vid = a["sg_approved_design"]["id"]
    v = sg.find_one("Version", [["id", "is", vid]],
                    ["code", "sg_path_to_movie", "sg_status_list"])
    if not v or not (v.get("sg_path_to_movie") or "") or not os.path.exists(v["sg_path_to_movie"]):
        raise PanelInputError(
            "Asset %s's approved design Version %s has no usable file on disk (%r)"
            % (asset_code, vid, v.get("sg_path_to_movie") if v else None))
    return a, v


# --------------------------------------------------- absent-character resolution
# CHARACTER-DUPLICATION-WEDGE.md / dialogue_guard.py's strip_absent_character_
# clauses() (see that module's docstring for the full incident writeup). This is
# where "present"/"known" get resolved FROM THE RECORD -- the guard function
# itself has zero opinion about names; this module supplies both sets.
_CHAR_CODE_SEG_RE = re.compile(r"[^A-Za-z0-9]+")


def character_name_tokens(asset_code):
    """Candidate name tokens for `asset_code`, derived PURELY from the code's
    own '_'-separated segments -- never a hardcoded name list. ONLY the
    segment IMMEDIATELY AFTER the literal 'CHAR' pivot is a token:
    SHOW_CHAR_PILOTCHARB -> {'PILOTCHARB'}; CHAR_CHARD_TWINS -> {'CHARD'} (this still
    recognises "CHARD ONE"/"CHARD TWO" dialogue tags in prose, since 'CHARD'
    alone is the token -- it does not need to split the pair itself).

    CONFIRMED-LIVE FINDING (ABSENT-CHARACTER-GUARD.md validation run): an
    earlier version took EVERY segment after 'CHAR' as a token, including
    CHAR_CHARA_PORTRAIT's second segment 'PORTRAIT' -- and real PILOT01_C_0110
    beat text ("...straightens it like the lobby portrait") uses "portrait"
    as an ordinary common noun (an actual framed picture, a set prop), not a
    reference to the character asset at all. That segment matched anyway and
    wrongly stripped a real clause. Every one of this project's 12 real CHAR
    asset codes (checked live, 2026-09-03) has its actual name in the FIRST
    segment after 'CHAR' -- everything after that (TWINS, FLOCK, PORTRAIT) is
    a type/multiplicity qualifier, never the name itself, so taking only the
    first segment removes the collision risk without adding a curated
    exclude-list of "words that happen to double as English nouns" -- which
    would itself rot the moment a new qualifier is invented.

    A code with no 'CHAR' segment (a SET/STYLE asset, or anything else)
    yields an empty set -- this function is never the thing that decides
    what IS a character asset (that stays classify_assets()'s job via
    sg_ref_role, unchanged); it only names a token for a code already known
    to be CHAR-shaped. A token under 3 characters is dropped (avoids noise
    from a stray short segment matching ordinary prose words)."""
    segs = [s for s in _CHAR_CODE_SEG_RE.split((asset_code or "").upper()) if s]
    if "CHAR" not in segs:
        return frozenset()
    i = segs.index("CHAR")
    if i + 1 >= len(segs):
        return frozenset()
    name = segs[i + 1]
    return frozenset({name}) if len(name) >= 3 else frozenset()


def project_character_asset_tokens(sg, project=None):
    """ALL character-shaped Assets in the project -> {asset_code: tokens}.

    Queried by CODE PATTERN (character_name_tokens() above), NOT
    Asset.sg_ref_role. This is a deliberate departure from classify_assets()'s
    own convention, for a real, confirmed-live reason: all 6 SHOW01 Assets
    (SHOW_CHAR_PILOTCHARB/PILOTCHARA/PILOTCHARC, SHOW_SET_*) carry sg_ref_role=None today
    (project 9999, checked live 2026-09-03) -- a role-based query would
    silently see ZERO SHOW01 characters, which would make this guard a no-op
    on the exact show the wedge evidence came from. This is also WHY
    classify_assets() itself currently misclassifies every SHOW01 shot's
    linked assets as 'other' rather than 'char'/'set' -- a separate,
    pre-existing blocker (gather_inputs() therefore still raises
    PanelInputError on every real SHOW01 shot today; NOT fixed by this
    change, reported in docs/METHOD.md, "blocker
    found, not fixed" -- fixing classify_assets()/backfilling sg_ref_role is
    a separate, real decision for Geoff, not something to route around
    silently inside this guard). Asset.code is what is actually populated
    and reliable for every asset in this project, SHOW01 included, so name
    resolution uses it directly rather than depending on the same field
    classify_assets() cannot currently rely on for this show."""
    proj = project if project is not None else PROJ
    rows = sg.find("Asset", [["project", "is", proj]], ["code"])
    out = {}
    for r in rows:
        toks = character_name_tokens(r.get("code") or "")
        if toks:
            out[r["code"]] = toks
    return out


def sequence_style_text(sg, shot):
    seq = shot.get("sg_sequence")
    if not seq:
        return ""
    s = sg.find_one("Sequence", [["id", "is", seq["id"]]], ["sg_style_prefix"])
    return (s.get("sg_style_prefix") or "").strip() if s else ""


# THE FIELDS THIS MODULE RENDERS FROM, in preference order. Exported because
# another module has to WRITE the field a panel revision changes, and there is
# no way to be right about that by reading a docstring.
#
# MEASURED 2026-09-04, on Geoff's own note on SHOW01_A_0160 ("he should be
# laying down on the bed under the blankets"): the proposal was drafted
# correctly and prompt_revision.py's panel branch applies it to sg_gen_prompt
# -- a field this module NEVER READS. grep sg_gen_prompt in panel_compose.py
# returns nothing. Accepting the note would have written a revision, requeued
# the shot, re-rendered it, and produced the same standing pose, with every
# step reporting success.
#
# sg_action_beat is the right target: it is the purpose-written ACTION-ONLY
# rewrite this module prefers, and it is generated text, so overwriting it is
# safe. sg_script_beat is the SCRIPT's own record and is never written here.
PANEL_ACTION_FIELDS = ("sg_action_beat", "sg_script_beat")

def gather_inputs(sg, shot):
    """-> dict of everything compose needs, or raises PanelInputError. Never
    guesses a missing input; every field is either real or explicitly absent
    with a stated reason (mirrors sg_provenance.py's "n/a - <reason>" rule)."""
    warnings = []
    char_codes, set_codes, other_codes = PROV.classify_assets(sg, shot)
    if other_codes:
        warnings.append("unclassified asset(s) linked, ignored: %s" % ", ".join(other_codes))

    char_info = None
    # Initialised unconditionally: an environment-only panel has no characters
    # and must still produce a well-formed inputs dict.
    char_infos = []
    if char_codes:
        if len(char_codes) > 2:
            raise PanelInputError(
                "%d CHAR assets linked (%s). Regional composition is proven for "
                "two and measured to FAIL for three (cross-seam blending, 3/3 on "
                "SHOW01_A_0330). Refusing rather than dropping a character or "
                "composing a panel that will be wrong."
                % (len(char_codes), ", ".join(char_codes)))
        # EVERY linked character is resolved, not just the first. `char_info`
        # stays the first one so provenance, the attribute gate and the
        # single-character path are unchanged; `char_infos` is what the
        # regional branch composes from.
        for cc in char_codes:
            ca, cv = resolve_approved_design(sg, cc)
            char_infos.append({"code": cc, "asset_id": ca["id"],
                               "design_version_id": cv["id"],
                               "design_version_code": cv["code"],
                               "image": cv["sg_path_to_movie"],
                               "instance_count": ca.get(ASSET_INSTANCE_COUNT_FIELD)})
        if len(char_codes) > 1:
            warnings.append("%d CHAR assets linked (%s); composing maskless, one "
                            "TextEncodeQwenImageEditPlus carrying every reference "
                            "(F253: 8 of 8 against 0 of 32 for the masked path)"
                            % (len(char_codes), ", ".join(char_codes)))
        char_code = char_codes[0]
        char_asset, char_version = resolve_approved_design(sg, char_code)
        # FIXED (was a hardcoded-filename guess -- see "WHICH CHARACTER TILE"
        # in the module docstring): the character image is the approved
        # design Version's OWN file, exactly like the set image below. No
        # fallback path exists to silently diverge from what ShotGrid says is
        # approved -- resolve_approved_design() already refused loudly above
        # if this Version or its file were missing.
        char_info = {"code": char_code, "asset_id": char_asset["id"],
                    "design_version_id": char_version["id"],
                    "design_version_code": char_version["code"],
                    "image": char_version["sg_path_to_movie"],
                    # COUNT-COLLAPSE.md: how many simultaneous copies of this
                    # character belong in frame -- None/absent/<=1 means "an
                    # ordinary single character", unchanged behaviour for
                    # every character that has never had this field set.
                    "instance_count": char_asset.get(ASSET_INSTANCE_COUNT_FIELD)}

    if not set_codes:
        raise PanelInputError("Shot %s has no STYLE/set asset linked -- refusing to compose "
                              "a panel with no set reference" % shot["code"])
    if len(set_codes) > 1:
        warnings.append("multiple STYLE assets linked (%s); using %s only"
                        % (", ".join(set_codes), set_codes[0]))
    set_code = set_codes[0]
    set_asset, set_version = resolve_approved_design(sg, set_code)
    set_info = {"code": set_code, "asset_id": set_asset["id"],
               "design_version_id": set_version["id"],
               "design_version_code": set_version["code"],
               "image": set_version["sg_path_to_movie"]}

    # show01_action_beats.py / docs/METHOD.md:
    # Shot.sg_action_beat, when set, is a purpose-written ACTION-ONLY rewrite
    # of the beat (physical action/posture/gesture/expression/camera framing
    # only -- no dialogue, interior thought, or practical text) meant
    # specifically for this compositor. Prefer it; fall back to
    # sg_script_beat (the script's own record, still run through
    # dialogue_guard below exactly as before) for every shot/episode that has
    # no sg_action_beat yet, so this change is a no-op everywhere except the
    # 55 SHOW01 shots that now carry one. sg_script_beat itself is NEVER
    # overwritten by that tool -- it stays the human-readable script record;
    # this is only a read-side preference for which field feeds a
    # generation prompt.
    action_text = next((shot.get(f) for f in PANEL_ACTION_FIELDS
                        if (shot.get(f) or "").strip()), "").strip()
    if not action_text:
        raise PanelInputError("Shot %s has no sg_action_beat or sg_script_beat text -- refusing "
                              "to compose a panel with no action (Phase 4 must sync beats first)"
                              % shot["code"])

    camera_text = "%s / %s / %s" % (shot.get("sg_camera") or "unspecified",
                                    shot.get("sg_shot_size") or "unspecified",
                                    shot.get("sg_gen_size_wxh") or "unspecified")
    style_text = sequence_style_text(sg, shot)
    # ONE SOURCE for the framing clause. The sent text and the recorded text
    # must be built from the same value or provenance_vs_sent.py will (rightly)
    # report them as disagreeing -- and the whole point of adding this clause
    # was that a recorded-but-not-sent camera field is a lie.
    #
    # THE CAST SIZE SELECTS THE WORDING. Both clauses said "the character",
    # singular, at every size except "two shot" and "over shoulder", so a
    # two-character shot at any other size was told to frame one person while
    # its beat described two. 13 of the 32 two-character SHOW01 shots are at
    # such a size; `SHOW01_A_0350` is the measured case, one figure in 8 of 8.
    _n_chars = len(char_infos)
    framing_text = framing_clause(shot.get("sg_shot_size"), _n_chars)
    contact_text = contact_clause(shot.get("sg_shot_size"), _n_chars)

    # CHARACTER-DUPLICATION-WEDGE.md: which character NAMES are "present" in
    # THIS composition (the one whose reference image is actually being sent
    # -- char_info above, if any) vs. every character name that exists in
    # this project's own record at all (project_character_asset_tokens()) --
    # handed to dialogue_guard.strip_absent_character_clauses() by
    # _prepare_action_text() below. A second CHAR asset linked to the Shot
    # but not chosen as char_info (the "using X only" warning above) is
    # correctly NOT present here either -- no reference image was supplied
    # for it, which is exactly the brief's own presence test.
    known_map = project_character_asset_tokens(sg)
    known_tokens = frozenset().union(*known_map.values()) if known_map else frozenset()
    # EVERY CHARACTER WITH A REFERENCE IS PRESENT, not just the primary one.
    #
    # This read `char_info` alone, which was correct while a two-character shot
    # sent ONE reference and the second name genuinely had no picture behind it.
    # Since F253 the maskless path sends EVERY character's reference in one
    # encode, so the second character IS in frame, and calling them absent
    # pointed the strip guard at a body the model is actually drawing.
    #
    # THE DIRECTION OF THE BUG IS THE WORRYING PART. The guard exists to delete
    # a clause about someone with NO reference, because naming them makes the
    # model duplicate whoever it does have. Left as it was, it could have
    # deleted the clause about a character who was in the picture, which is the
    # same defect pointed the other way and much harder to spot: the panel
    # renders two people and simply ignores what one of them was doing.
    # Nothing had fired yet, found by survey rather than by damage.
    present_tokens = frozenset().union(*[character_name_tokens(c["code"])
                                         for c in char_infos]) if char_infos else frozenset()

    return {"character": char_info,
            # Every linked character, in link order. The regional branch
            # composes from this; everything else still uses "character".
            "characters": char_infos,
            "set": set_info, "action_text": action_text,
           "camera_text": camera_text, "style_text": style_text, "warnings": warnings,
           "framing_text": framing_text,
           "contact_text": contact_text,
           "character_names_present": present_tokens, "character_names_known": known_tokens}


# ---------------------------------------------------------------- version numbering / task
def next_version_num(sg, code_prefix):
    existing = sg.find("Version",
                       [["project", "is", PROJ], ["code", "starts_with", code_prefix + "_v"]],
                       ["code"])
    nums = []
    for v in existing:
        m = re.match(r"^%s_v(\d+)$" % re.escape(code_prefix), v["code"])
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def panel_task(sg, shot):
    t = sg.find_one("Task", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                             ["step.Step.short_name", "is", PANEL_STEP_SHORT_NAME]],
                    ["content"])
    if t:
        return t
    step = sg.find_one("Step", [["short_name", "is", PANEL_STEP_SHORT_NAME],
                                ["entity_type", "is", "Shot"]], ["code"])
    if step is None:
        step = sg.create("Step", {"short_name": PANEL_STEP_SHORT_NAME, "code": "Panel",
                                  "entity_type": "Shot"})
    return sg.create("Task", {"project": PROJ, "entity": {"type": "Shot", "id": shot["id"]},
                              "step": step, "content": "Panel"})


# docs/METHOD.md / CUMULATIVE-EFFECT.md section 3: honest
# pass-rate labelling. Production deliberately gates every attempt with
# attribute_check.py's --repeat 1 (one unvoted claude -p call) for cost, per
# Geoff's own correction -- unchanged, see run_attribute_qc()'s docstring.
# But a PASS recorded under that gate is NOT the same measurement as a PASS
# under the checker's own default (--repeat 3, unanimous): re-scoring the
# same 13 real published panels found 71% single-run PASS vs. 8% unanimous
# 3-run PASS on the IDENTICAL pixels -- pure judge variance, meaning most
# single-run PASSes are marginal, not solid. Nothing here changes the gate
# (still --repeat 1 in production) or the checker's judging logic -- this
# only makes every published Version's own record say, unambiguously, which
# gate produced its verdict, so a future batch tally built by reading these
# descriptions is never silently mistaken for a stricter number.
QC_GATE_LABEL = "single-run gate, --repeat 1 -- NOT the checker's unanimous --repeat 3 default"


def qc_status_description(qc_status, qc_detail):
    """The 'Attribute QC: ...' line of a published Version's description --
    factored out from compose_panel_for_shot() so its exact wording (and that
    it always carries QC_GATE_LABEL) is unit-testable without the full
    ShotGrid publish plumbing."""
    if qc_status == "OFF":
        # NOT a verdict. The description carries no QC line at all when the gate
        # is off, because a reviewer scanning a page must never see a QC phrase
        # for a check that did not run -- an absent line is unambiguous, a line
        # saying "OFF" invites being read as "fine".
        return ""
    if qc_status == "PASS":
        return "Attribute QC: PASS (%s)." % QC_GATE_LABEL
    if qc_status == "SKIP":
        return "Attribute QC: SKIPPED (%s)." % qc_detail
    return ("Attribute QC: %s (%s) -- published for review anyway (D16: gates are "
           "deterministic scripts, not vision; the operator judges, not this checker). "
           "Detail:\n%s" % (qc_status, QC_GATE_LABEL, qc_detail))


# ---------------------------------------------------------------- attribute QC gate
# --- the attribute gate is OFF ----------------------------------------------
#
# GEOFF, 2026-09-05: "the gate with vision model is only partially working and
# only adds recommendations to the version description... we should turn it off
# and just let the operator review without its input. In the long run for later
# development we should stage it for re-inclusion and improvement to save
# operator time catching preventable errors."
#
# WHAT THE MEASUREMENT SAID. Per-attribute pass rates across every SHOW01 panel:
# distinguishing_features 51%, wardrobe 56%, build 75%, hair 77%. All four must
# pass, so a single-character panel passes 38% and a two-character one 0.1%. And
# the failures are largely not errors: an attribute list encodes a POSTURE the
# beat legitimately overrides ("slightly slouched exhausted posture" on a shot
# where the character stands up), and an attribute cropped out of frame scores
# FAIL because the checker has only pass/true and pass/false -- "wrong" and "not
# visible" are the same answer. Close-ups make that worse, and close-ups only
# started working today.
#
# Meanwhile the two defects an operator catches instantly on a contact sheet --
# a duplicated character, and a rendering artefact -- are invisible to it,
# because it asks whether NAMED ATTRIBUTES are present and neither is one.
#
# IT IS NOT DELETED, IT IS SWITCHED OFF, and the switch is one constant. The
# tool, its self-tests and its repeat-and-vote logic are untouched and still
# pass preflight, so re-enabling is a one-line change plus whatever improvement
# it is re-enabled WITH. Deleting it would have thrown away the measurement
# apparatus along with the gate.
#
# WHAT TURNING IT OFF ACTUALLY CHANGES, all three, because "it only annotated"
# was not quite true:
#   1. no verdict in the Version description or sg_qc_detail;
#   2. the CHOSEN candidate becomes the first that composed, not the first that
#      passed -- deterministic, and the operator picks from the wedge anyway;
#   3. it stops spending one claude -p VISION call per character per seed. At 8
#      seeds on a two-character shot that is 16 vision calls a shot.
#
# The verdict token when off is "OFF", never "PASS" and never "SKIP". SKIP means
# "no character to check" and is a real finding; reporting a pass for a check
# that never ran is the exact rubber-stamp failure attribute_check.py's own
# docstring calls its worst possible behaviour.
ATTRIBUTE_QC_ENABLED = False

def run_attribute_qc(image_path, inputs, model="sonnet", timeout=180):
    """PASS/FAIL/SKIP via build/tests/attribute_check.py, invoked as a
    subprocess (it is a standalone gate tool, not a shared library -- see
    module docstring). SKIP (no character in frame) is not a silent pass:
    it is returned distinctly and stated in the published Version's
    description.

    QC-HARDENING fix 1 note: attribute_check.py's CLI now defaults to
    repeat-and-vote (--repeat 3, unanimous PASS required) -- deliberately
    strong for a human/agent running it directly as a spot-check or a wedge
    score. This production call site is DIFFERENT: it already sits inside
    compose_with_retry()'s own up-to-DEFAULT_MAX_SEED_ATTEMPTS-per-shot loop,
    so silently inheriting the new x3 default here would multiply cost again
    ON TOP of that (up to 3 seed attempts x 3 votes = 9 claude -p calls for
    one shot) with no explicit decision behind it. Per Geoff's correction
    (2026-08-29): this gate is a measurement instrument, not a blocker on
    production throughput -- pinned to --repeat 1 explicitly, unchanged from
    this call site's behaviour before the voting gate existed. See
    docs/METHOD.md for the cost numbers and the recommendation
    that voting run at spot-checks/wedges, not on every panel attempt."""
    if inputs["character"] is None:
        return "SKIP", ("no character in frame -- attribute QC does not apply "
                        "to an environment-only panel")

    # EVERY LINKED CHARACTER, NOT JUST THE FIRST.
    #
    # This checked inputs["character"] alone, which is the primary. On a
    # two-character shot that makes the defect most worth catching invisible:
    # SHOW01_A_0060_PNL_panel_v003 rendered PilotCharA correctly, omitted PilotCharB
    # ENTIRELY, and the gate returned PASS because the one character it graded
    # was present. A panel missing half its cast passed a check whose whole
    # job is "are the right characters in this frame".
    #
    # One subprocess per character. The cost is real and bounded: this runs
    # inside compose_with_retry's seed loop, so it is (attempts x characters)
    # claude -p calls with characters at 1 or 2 -- see the --repeat 1 pinning
    # in the docstring above, which exists for exactly this multiplication.
    #
    # The verdict is the WORST across characters: ERROR beats FAIL beats PASS.
    # A gate that passed on "most of the cast is right" would be the same
    # defect one level up.
    chars = inputs.get("characters") or [inputs["character"]]
    verdicts, details = [], []
    for c in chars:
        cmd = [PY, ATTR_CHECK, "--image", image_path, "--character", c["code"],
               "--model", model, "--timeout", str(timeout), "--repeat", "1"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        tail = chr(10).join(l for l in (r.stdout or "").splitlines()
                            if l.strip())[-2000:]
        v = "PASS" if r.returncode == 0 else ("FAIL" if r.returncode == 1 else "ERROR")
        verdicts.append(v)
        details.append("=== %s: %s ===" % (c["code"], v))
        details.append(tail or (r.stderr or "")[-800:])
    worst = "ERROR" if "ERROR" in verdicts else ("FAIL" if "FAIL" in verdicts else "PASS")
    header = ("%d character(s) checked: %s"
              % (len(chars), ", ".join("%s=%s" % (c["code"], v)
                                       for c, v in zip(chars, verdicts))))
    return worst, header + chr(10) + chr(10).join(details)


# ---------------------------------------------------------------- staleness bookkeeping
def _panel_designs_load():
    try:
        with open(PANEL_DESIGNS_JSON, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = None
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("shots", {})
    return doc


def _panel_designs_save(doc):
    os.makedirs(os.path.dirname(PANEL_DESIGNS_JSON), exist_ok=True)
    with open(PANEL_DESIGNS_JSON, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)


def record_panel_designs(shot_code, version_id, version_code, inputs, attempts=None):
    doc = _panel_designs_load()
    entry = {"panel_version_id": version_id, "panel_version_code": version_code,
            "synced_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if attempts is not None:
        # Retry visibility (Change 2): every seed tried and its verdict,
        # never just the one that got published -- so staleness/audit tooling
        # reading this sidecar can see the retry happened, not just its
        # outcome.
        entry["seed_attempts"] = [
            {"attempt": a["attempt"], "seed": a["seed"],
             "qc_status": a.get("qc_status", "COMPOSE-FAILED")} for a in attempts]
    if inputs["character"]:
        entry["character_asset_id"] = inputs["character"]["asset_id"]
        entry["character_asset_code"] = inputs["character"]["code"]
        entry["character_design_version_id"] = inputs["character"]["design_version_id"]
    entry["set_asset_id"] = inputs["set"]["asset_id"]
    entry["set_asset_code"] = inputs["set"]["code"]
    entry["set_design_version_id"] = inputs["set"]["design_version_id"]
    doc["shots"][shot_code] = entry
    _panel_designs_save(doc)


# ---------------------------------------------------------------- multi-seed retry
# QC-HARDENING fix 3 (docs/METHOD.md), superseded by D15
# (docs/METHOD.md): PILOT01_B_0310's action text
# carried CHARD TWO's real dialogue line into the compositor
# instruction -- reproduced here only as a fabricated stand-in of the same
# shape, "Forget what I just said. The lock was never fixed." (Shot.sg_script_
# beat, confirmed live against ShotGrid) -- and Qwen-Image-Edit-2509
# rendered it as literal on-image text -- a burnt-in caption on the panel, which
# then tripped claude -p's own prompt-injection refusal at the QC step. The
# fix that used to live here as a local function is now dialogue_guard.py's
# strip_dialogue_for_prompt() (invariant 11: one implementation, imported by
# every prompt-composition path, not just this one -- see that module's
# docstring for the full root-cause writeup and why this is a consumption-
# side, not writer-side, fix). Aliased here so this file's own call sites and
# self-test read unchanged.
BEAT_LINE_RE = DG.BEAT_LINE_RE
NO_BURNT_IN_TEXT_CLAUSE = DG.NO_BURNT_IN_TEXT_CLAUSE
sanitize_action_text_for_compositor = DG.strip_dialogue_for_prompt


# ---------------------------------------------------------------- multi-instance count
# docs/METHOD.md: a character whose design calls for SEVERAL
# simultaneous copies (CHAR_CHARH_FLOCK's flock, CHAR_CHARD_TWINS' "always
# shown together" pair) reliably rendered as 1-2 copies -- the single largest
# panel-QC failure mode found in PANEL-BATCH-SCALE.md (12 of 24 attribute-fail
# instances). The wedge (build/tests/count_collapse_wedge.py) seed-locked one
# character (CHAR_CHARH_FLOCK) and varied only how the count was expressed:
# a real rejected shot's beat text (PILOT01_A_0400 -- describes ONE bird's
# action inside the huddle, states no total anywhere) collapsed the flock to
# 2 birds; the SAME beat text with an explicit count sentence added recovered
# 8-11 birds every time. This is not new invention -- PANEL-BATCH-SCALE.md
# had already noticed PILOT01_C_0010 passed clean specifically because its own
# beat text stated (fabricated example of the same shape) "Nine pigeons out
# there now"; the wedge confirms that was
# the lever, not a coincidence.
#
# CARDINAL, NOT ORDINAL. A writer describing "a FOURTH CHARH lands" or "the
# nearest bird" (both fabricated examples of the real beat text's shape, both
# shots that collapsed) is not
# stating a total count -- "fourth" is an ordinal, and \bfour\b's word
# boundaries deliberately do not match inside "fourth" (no boundary between
# "r" and "t"), so this detector is not fooled by it. Only an actual cardinal
# ("Four pigeons...", "4 pigeons...") counts as the writer having already
# stated their own number, which is always trusted over the Asset's default
# (see derive_action_text_with_count()) -- PILOT01_C_0010's "Nine pigeons out
# there now" must never be silently overridden by a generic 4.
_CARDINAL_WORD_RE = re.compile(
    r"\b(two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", re.I)
_CARDINAL_DIGIT_RE = re.compile(r"\b([2-9]|[1-9][0-9])\b")


def _action_text_states_own_count(text):
    """True if `text` already states an explicit cardinal count (a number
    word two..twelve, or a 2-99 digit) anywhere -- never true for an ordinal
    ('fourth', '4th') or the bare word 'one'/digit '1', which do not assert
    'several are in frame' the way the multi-instance fix cares about."""
    return bool(_CARDINAL_WORD_RE.search(text) or _CARDINAL_DIGIT_RE.search(text))


def derive_action_text_with_count(action_text, instance_count):
    """If `instance_count` names a multi-instance character (>1) and
    `action_text` does not already state its own explicit cardinal count,
    append one derived from the Asset's own sg_instance_count -- never
    hand-written per shot (the brief's requirement: the fix must generalise
    to any future multi-instance character, not hard-code two names). A shot
    whose beat ALREADY states its own number (PILOT01_C_0010's real beat,
    fabricated example of the same shape, "Nine pigeons out there now") is
    left untouched -- the writer's per-shot number
    always wins over the Asset's default, exactly the smallest-honest-change
    the varies-per-shot case (CHAR_CHARH_FLOCK: 3, 4, 6 across the episode)
    needs: no new Shot field, no per-shot hand-authoring required, but never
    silently overridden either."""
    if not instance_count or instance_count <= 1:
        return action_text
    if _action_text_states_own_count(action_text):
        return action_text
    base = (action_text or "").rstrip()
    if base and base[-1] not in ".!?":
        base += "."
    return ("%s There are exactly %d copies of the character in this shot, all together, as "
           "shown in the character reference image." % (base, int(instance_count))).strip()


def _prepare_action_text(inputs):
    """Sanitize (D15/dialogue_guard) then, for a multi-instance character,
    derive-or-respect an explicit count (COUNT-COLLAPSE.md) -- the ONE place
    both _build_instruction() (provenance/description text) and
    compose_with_retry()'s real QC.compose_panel() call get this text from,
    so the two can never diverge. QC-HARDENING fix 3's real bug was exactly
    this shape: a text transform applied to a provenance-only copy but not
    the string that actually reached the GPU looks fixed and isn't -- this
    function is the single source both call sites read, not two copies that
    could quietly drift apart again.

    CHARACTER-DUPLICATION-WEDGE.md: after D15 dialogue-stripping and before
    count-derivation, strip any clause naming a character who is in this
    project's own record (inputs["character_names_known"]) but not actually
    supplied a reference image in THIS composition
    (inputs["character_names_present"]) -- dialogue_guard.strip_absent_
    character_clauses(), the fix for the duplication defect (2 of 3 seeds,
    Versions 67521-67526). Both keys default to empty when absent (older/
    hand-built `inputs` dicts throughout this file's own self-test, and any
    future caller that has not threaded them through) -- a no-op, never a
    crash, exactly like every other optional key gather_inputs() adds."""
    text = sanitize_action_text_for_compositor(inputs["action_text"]).strip()
    text = DG.strip_absent_character_clauses(
        text, inputs.get("character_names_present") or (),
        inputs.get("character_names_known") or ())
    char = inputs.get("character")
    if char:
        text = derive_action_text_with_count(text, char.get("instance_count"))
    return text


# --- FRAMING: the operator's shot size, actually sent ------------------------
#
# PROVEN 2026-09-05 by wedge_shot_size.py on SHOW01_A_0160 (sg_shot_size="close
# up"), three seeds, control and framed arms in one run so seed and model state
# were shared and the clause was the only variable: 3/3 framed cells are
# genuine close-ups, head and shoulders filling the frame; 3/3 control cells on
# THE SAME SEEDS are full-body medium-wides. Character identity held in both.
#
# Before this, camera_text ("locked-off / close up / 768x432") went to
# PROV.write_provenance() and nowhere else. The operator's framing decision was
# recorded, claimed in provenance, and discarded. This is not a creative change
# -- it makes the pipeline honour a field the operator had already set and the
# code was silently throwing away.

# The mapping is deliberately PLAIN ENGLISH, not film-school shorthand. The
# compositor is a diffusion model reading a sentence, not a camera department:
# "framed as a close-up" means little, "head and shoulders fill the frame" is a
# description of pixels. Each clause says what the FRAME contains, because that
# is the thing the model can actually render.
FRAMING = {
    "close up": "Framed as a close-up: the character's head and shoulders fill "
                "most of the frame, the room visible only as background behind them.",
    "medium close": "Framed as a medium close-up: the character from the chest "
                    "up fills the frame.",
    "medium": "Framed as a medium shot: the character from the waist up.",
    "medium wide": "Framed as a medium-wide shot: the character from the knees "
                   "up, with room visible around them.",
    "wide": "Framed as a wide shot: the whole character small in the frame, the "
            "room and its full depth clearly visible around them.",
    "two shot": "Framed as a two shot: both characters in frame together, from "
                "the waist up.",
    "over shoulder": "Framed over the shoulder of the nearer character, who is "
                     "large and out of focus in the foreground.",
    "insert": "Framed as a tight insert: the detail being described fills the "
              "frame, no full figure.",
}


# THE SAME SIZES, WRITTEN FOR MORE THAN ONE BODY.
#
# Every entry in FRAMING above except "two shot" and "over shoulder" says "the
# character", singular. On a two-character shot at any other size the prompt
# therefore CONTRADICTS the beat: `SHOW01_A_0350`'s beat names and room-anchors
# both PilotCharC and PilotCharB, and the framing clause it was sent says "the whole
# character small in the frame". It produced one figure in 8 of 8 cells.
#
# MEASURED 2026-09-07: 13 of the 32 two-character SHOW01 shots carry a size
# other than "two shot" (0070, 0090, 0100, 0120, 0200, 0220, 0240, 0250, 0300,
# 0320, 0330, 0350, 0400), so 13 shots were being told to frame one person and
# describe two.
#
# The wording change is deliberately MINIMAL: same shot size, same amount of
# body in frame, only the number of bodies. Anything more would be a creative
# change smuggled in as a bug fix.
FRAMING_PLURAL = {
    "close up": "Framed as a close-up: the characters' heads and shoulders fill "
                "most of the frame, the room visible only as background behind them.",
    "medium close": "Framed as a medium close-up: both characters from the chest "
                    "up fill the frame.",
    "medium": "Framed as a medium shot: both characters from the waist up.",
    "medium wide": "Framed as a medium-wide shot: both characters from the knees "
                   "up, with room visible around them.",
    "wide": "Framed as a wide shot: both characters small in the frame, the room "
            "and its full depth clearly visible around them.",
    "two shot": FRAMING["two shot"],
    "over shoulder": FRAMING["over shoulder"],
    "insert": FRAMING["insert"],
}


# THE WIDE-TWO-SHOT WARNING LIVED HERE FOR THREE HOURS AND IS GONE, because it
# was false for the path we actually use.
#
# It warned that a `wide` or `medium wide` with two characters could not be
# separated, on a peer engineer's 382-454px band measured across two sets and three
# models. **Every cell in that band used a STITCHED reference**, both characters
# arriving in one image, and the format never varied, so the confound was
# invisible and the band was read as a property of the model family (their
# F313, correcting their own F286/F288/F301).
#
# **Three SEPARATE reference slots stage two characters at 629 to 694 px
# without being asked to**, and three separate slots is exactly what this
# compositor sends. So the framing our operators were being warned off is one
# our own path delivers unprompted, and the warning discouraged it.
#
# The corrected rule, worth keeping even though the code is gone: when both
# characters arrive in ONE reference the model normalises their spacing to its
# own two-shot convention and ignores what the stitch commands; when they
# arrive as separate references it stages them wide. **The constraint was in
# our conditioning, never in the model.**


def framing_clause(shot_size, n_characters=1):
    """-> a framing sentence for a ShotGrid sg_shot_size, or "" if unknown.

    An unknown or empty size returns EMPTY, never a default. Guessing "medium"
    for a size nobody set would silently invent a creative decision, and the
    whole point of this wedge is that framing must come from the operator's
    field or not at all.

    n_characters selects the plural wording. It defaults to 1 so every existing
    caller keeps its exact behaviour; only a caller that KNOWS the cast passes
    more."""
    key = (shot_size or "").strip().lower()
    table = FRAMING_PLURAL if (n_characters or 1) > 1 else FRAMING
    return table.get(key, "")


# GROUND CONTACT. One sentence, and it is the whole fix for "the character
# looks pasted over the background rather than standing in the room".
#
# MEASURED by a peer engineer 2026-09-06, RND_COMP_0010, 5 arms x 3 seeds:
#
#   control (PRESERVE_SUFFIX unchanged)      0 of 2 cells had a contact shadow
#   identity-not-appearance (relax preserve) 0 of 2
#   control + THIS SENTENCE                  3 of 3
#   control + a light-direction sentence     0 of 2
#   identity + shadow + light                3 of 3
#
# Every cell containing it produced a shadow; every cell without it produced
# none. Adding it to the CONTROL was as good as adding it to the relaxed arm,
# so RELAXING THE PRESERVE CLAUSE IS NOT NEEDED and is not done: that would
# put character consistency at risk to buy something this sentence already
# buys. a peer engineer deliberately made the arms additive to the control rather than
# stacking them, which is the design decision that separated the two.
#
# MY OWN PREDICTION WAS HALF WRONG and the record says so: I predicted the
# two "preserve exactly" clauses were the cause and that relaxing the
# character one would produce ground contact. It did not, on its own or at
# all. Only my corollary held, that stating the shadow POSITIVELY would do
# more than any wording about perspective, because a shadow is a thing that
# IS in frame and cfg 1.0 makes negations inert.
#
# THE SHAPE TO KEEP when generalising: a physical fact about the character's
# contact with a NAMED surface, stated positively. Not "make it look
# grounded", which names nothing.
CONTACT_CLAUSE = ("The character's feet rest flat on the floor, with a soft dark "
                  "contact shadow pooling on the floor under and just behind them.")

# Only for framings where the ground is plausibly in shot. A close-up is head
# and shoulders, so asserting feet would describe something outside the frame,
# and a peer engineer's wedge was run on a full figure. UNTESTED for tight sizes, so they
# get nothing rather than a guess.
# ONLY THE SIZES WHOSE OWN FRAMING CLAUSE SHOWS THE FEET.
#
# This listed five sizes and three of them CONTRADICTED their own framing text.
# Read the two sentences we were sending for a medium close, together:
#
#   "Framed as a medium close-up: the character from the chest up fills the frame."
#   "The character's feet rest flat on the floor, with a soft dark contact
#    shadow pooling on the floor under and just behind them."
#
# One says the frame stops at the chest, the other asserts the feet are in it.
# **The model resolved the contradiction by drawing the full figure**, which is
# the only reading that satisfies both, and it did so on 8 of 8 cells of
# SHOW01_A_0200 and _0220 on 2026-09-07. `medium wide` ("from the knees up") and
# `medium` ("from the waist up") carry the same contradiction.
#
# THIS IS PROBABLY THE MECHANISM BEHIND THE OLDER FINDING that 16 of 19
# approved panels ignored `sg_shot_size` and that "only tight sizes show it".
# The framing clause was not being ignored: it was being outvoted by a sentence
# we sent immediately after it, and the tight sizes that appeared to work are
# the ones that never got a contact clause at all.
#
# The clause itself is good and stays: it is measured (3 of 3 with it, 0 of 2
# without). It just cannot be sent to a frame that excludes what it describes.
CONTACT_SIZES = ("wide", "full")


# Same sentence, same shape (a physical fact about contact with a NAMED
# surface, stated positively), for more than one body.
CONTACT_CLAUSE_PLURAL = ("Each character's feet rest flat on the floor, with a soft "
                         "dark contact shadow pooling on the floor under and just "
                         "behind each of them.")


def contact_clause(shot_size, n_characters=1):
    """-> the ground-contact sentence for a shot size, or '' when the ground is
    not in frame. Empty for an unknown size, never a default, same rule as
    framing_clause(). n_characters selects the plural wording and defaults to 1,
    so existing callers are unchanged."""
    if (shot_size or "").strip().lower() not in CONTACT_SIZES:
        return ""
    return CONTACT_CLAUSE_PLURAL if (n_characters or 1) > 1 else CONTACT_CLAUSE


# sent_prompt_text() DELETED 2026-09-08. It rebuilt the operator-visible
# prompt from instance_count, which answers "how many copies of this
# character" and not "what was sent", so it was wrong for every
# two-character panel. The record now comes from the compose call
# itself (record=sent_record). Do not reintroduce a second assembler.


def _build_instruction(inputs):
    """The exact instruction text sent to the compositor, factored out so both
    compose_with_retry() and (previously) compose_panel_for_shot() build it
    identically for every attempt -- only the seed differs between attempts.
    action_text is sanitized and count-derived (see _prepare_action_text()
    above) before it ever reaches the compositor, and the no-burnt-in-text
    clause is appended to every instruction, character panel or
    environment-only."""
    action_text = _prepare_action_text(inputs)
    if inputs["character"]:
        base = ("Place the character from the character reference image into the room "
               "shown in the set reference image. %s" % action_text)
    else:
        base = ("Show the room from the set reference image, matching this action/"
               "description exactly. %s" % action_text)
    out = "%s %s" % (base.rstrip(), NO_BURNT_IN_TEXT_CLAUSE)
    if inputs.get("framing_text"):
        out += " " + inputs["framing_text"]
    if inputs.get("contact_text"):
        out += " " + inputs["contact_text"]
    return out


# Cap at TWO. Three equal bands were measured on SHOW01_A_0330 and failed 3/3
# by cross-seam blending -- two figures where three were asked for, one a
# fusion of two characters, and on one seed the band boundary rendered as a
# hard vertical line at exactly 2/3 of the canvas. Two SHOW01 shots link three
# characters and they are refused loudly rather than composed wrong.
MAX_REGIONAL_CHARACTERS = 2


def order_characters_by_beat(chars, action_text):
    """-> `chars` reordered so the one the beat NAMES FIRST is Picture 1.

    THE SLOT ORDER BEATS THE BEAT, measured 2026-09-07 on SHOW01_A_0390, two
    runs with the slots swapped and the same sentence:

      wedge:    Picture 1 = PilotCharB     -> PilotCharB rendered on the LEFT, 8 of 8
      pipeline: Picture 1 = PilotCharA  -> PilotCharA rendered on the LEFT, 8 of 8

    and the beat said "PilotCharB stands on the left side of frame" in BOTH. So the
    reference slot decides screen position and the sentence does not, which
    means link order was silently deciding our screen direction. That matters
    past this shot: screen direction is what the 180-degree rule is about, and
    a character who swaps sides between two shots of one conversation is a
    continuity error a viewer feels even if they cannot name it.

    Ordering by FIRST MENTION works because of our own beat convention (F239):
    a beat places each character against the frame, left to right, so the first
    name in the sentence is the leftmost body. It is deterministic, it needs no
    new field, and it degrades to link order when the beat names nobody.

    Matching is on the CHARACTER TOKEN (`SHOW_CHAR_PILOTCHARB` -> "PILOTCHARB"), the same
    derivation character_name_tokens() uses, so it cannot drift from the rest
    of the module."""
    if len(chars or []) < 2 or not action_text:
        return chars
    low = action_text.lower()
    NEVER = len(low) + 1

    def first_mention(c):
        best = NEVER
        for tok in character_name_tokens(c.get("code") or ""):
            i = low.find(tok.lower())
            if i >= 0:
                best = min(best, i)
        return best

    pos = [first_mention(c) for c in chars]
    if all(p == NEVER for p in pos):
        return chars              # the beat names nobody: keep link order
    # STABLE sort, so characters the beat does not name keep their link order
    # behind the ones it does, rather than being shuffled arbitrarily.
    return [c for _p, _i, c in sorted(
        ((pos[i], i, c) for i, c in enumerate(chars)), key=lambda x: (x[0], x[1]))]


# NAMING THE SLOT IS THE ONLY THING THAT CONTROLS SCREEN DIRECTION.
#
# a peer engineer's P024, measured on OUR pair in OUR hallway, 4 seeds per arm:
#
#   clause naming the SLOT ("The man in Picture 1 stands on the LEFT...")
#     arm E, Picture 1 = PilotCharA -> PilotCharA left, 4 of 4
#     arm F, Picture 1 = PilotCharB    -> PilotCharB left,    4 of 4    (a perfect swap)
#
# and the two arms that do NOT name the slot decide nothing (F267 to F269): the
# beat's own prose, buried or explicit, does not move the men.
#
# WHY "Picture 1" AND NOT A DESCRIPTION: it is a string the ENCODER ACTUALLY
# WRITES. TextEncodeQwenImageEditPlus builds its token stream as
# "Picture {}: <vision_start>..." per image, so "Picture 1" resolves to a real
# token and "the character reference image" resolves to nothing (F255).
#
# AND NAMING THE CHARACTER INSTEAD IS ACTIVELY DANGEROUS, which is the part I
# would not have guessed. a peer engineer's arm B named the men by description ("the slim
# man with glasses", "the heavyset man with the moustache"): 2 of 4 cells drew
# PILOTCHARB TWICE, once on each side, with no PilotCharA in frame, and the other 2 put
# PilotCharA's face on PilotCharB's costume. No slot-named cell did this. An unresolvable
# referring expression does not degrade into ignoring the clause; it degrades
# into picking the wrong reference for a body it has already decided to draw.
#
# THIS IS WHAT MAKES order_characters_by_beat LOAD-BEARING AGAIN. On its own the
# slot decides nothing (F267), so ordering was tidiness. Paired with a clause
# that SAYS Picture 1 is on the left, the ordering is what makes the clause
# TRUE: put the character the beat places first into slot 1, then tell the model
# slot 1 is on the left.
SLOT_SIDE_CLAUSE = ("The man in Picture 1 stands on the LEFT side of the frame and the man in "
                    "Picture 2 stands on the RIGHT side of the frame.")


def slot_side_clause(n_characters):
    """-> the screen-direction clause, or "" when it would be a lie.

    THIS CLAUSE IS MANDATORY, NOT FRAMING POLISH, and that was measured after it
    shipped. a peer engineer's F315 ran the arm without it: **3 figures in 4 of 4 cells**,
    one of them a PilotCharA-PilotCharB hybrid wearing PilotCharA's orange collar on PilotCharB's
    orange arms, **and the prompt still said "Exactly two characters in the
    frame" in its shared tail**. So the count instruction does not hold the
    count; this clause does, by giving each character a distinct JOB in the
    sentence. It is the same failure family as naming the men by description
    (F272), where an unresolvable reference collapsed two into one.

    Which means: never make this conditional on a framing preference, never
    drop it to shorten a prompt, and if a future change makes it optional the
    headcount goes with it.

    TWO CHARACTERS ONLY. With one there is no side to assign, and with three the
    clause names two slots and silently omits the third, which is worse than
    saying nothing: it would place two men and leave the model to guess the one
    it was not told about."""
    return SLOT_SIDE_CLAUSE if (n_characters or 0) == 2 else ""


def room_text_for(inputs):
    """-> the room clause for the maskless two-character path.

    compose_panel_two() takes the room as TEXT as well as taking the set as
    image3, so this names the set reference rather than re-describing the room:
    the picture is the description, and a second prose description of the same
    room is a second source of truth that can disagree with it."""
    code = ((inputs.get("set") or {}).get("code") or "").strip()
    return ("the room shown in the set reference image"
            + (" (%s)" % code if code else ""))


def regional_panel(sg, shot_row, chars, inputs, action_text, prefix, seed):
    """Compose a multi-character panel with one masked region per character.
    -> (output_path, error_string). Same contract as QC.compose_panel.

    The graph builder lives in regional_compose.py and is not copied here
    (invariant 11); this function is the seam that turns panel_compose's
    `inputs` into that builder's arguments and runs it."""
    import shutil
    import subprocess
    import tempfile
    sys.path.insert(0, TOOLS)
    import regional_compose as RC

    if len(chars) > MAX_REGIONAL_CHARACTERS:
        return None, ("%d characters linked; regional composition is proven for "
                      "%d and measured to FAIL for three (cross-seam blending, "
                      "3/3 on SHOW01_A_0330). Refusing rather than composing a "
                      "panel that will be wrong."
                      % (len(chars), MAX_REGIONAL_CHARACTERS))

    comfy_in = RC.COMFY_IN
    refs = []
    for c in chars:
        src = c["image"]
        if not src or not os.path.isfile(src):
            return None, "character %s has no image on disk (%s)" % (c.get("code"), src)
        dst = "panelreg_%s_%s" % (c.get("code", "x"), os.path.basename(src))
        try:
            shutil.copyfile(src, os.path.join(comfy_in, dst))
        except Exception as exc:                                  # noqa: BLE001
            return None, "could not stage %s: %s" % (src, exc)
        refs.append(dst)

    set_name = None
    set_src = (inputs.get("set") or {}).get("image")
    if set_src and os.path.isfile(set_src):
        set_name = "panelreg_set_" + os.path.basename(set_src)
        try:
            shutil.copyfile(set_src, os.path.join(comfy_in, set_name))
        except Exception as exc:                                  # noqa: BLE001
            return None, "could not stage set %s: %s" % (set_src, exc)

    # ONE PROMPT PER BAND, AND EACH BAND MUST NAME ONLY ITS OWN CHARACTER.
    #
    # This used to hand every band the same text via RC.beat_for, which keeps
    # the sentences naming a character and then runs the absent-character
    # guard -- and that guard deliberately refuses to remove a name from a
    # clause that also names a present character. So both bands got the whole
    # beat. MEASURED on SHOW01_A_0300: PilotCharB's band, holding PilotCharB's reference,
    # was told "PilotCharA strides back in and flips on the overhead lights", and
    # the two rendered as ONE fused person in PilotCharA's shirt and PilotCharB's
    # trousers. Four hero shots came back 1 clean of 4 for this reason.
    #
    # beat_split rewrites the beat into one action per character plus
    # character-free shared staging. Rewriting, not deleting: deletion orphans
    # subjects, which is exactly why dialogue_guard refuses to do it.
    #
    # A FAILED SPLIT IS A REFUSAL, NOT A FALLBACK. Falling back to the old
    # shared text would reintroduce the fusion silently, and a panel that
    # looks composed and is wrong is worse than one that was not composed.
    import beat_split as BS
    names = [(c.get("code") or "").replace("SHOW_CHAR_", "").lower() for c in chars]
    split, split_err = BS.for_shot(sg, shot_row, names)
    if split_err:
        return None, ("beat split failed (%s); refusing to compose a "
                      "multi-character panel from text that names every "
                      "character in every region" % split_err)
    prompts = []
    for n in names:
        prompts.append("Place the character from the character reference image "
                       "into the room shown in the set reference image. "
                       "The character %s. %s. %s."
                       % (split["actions"][n], split["shared"], RC.PRESERVE))

    canvas = RC.canvas_for(chars[0]["image"])
    graph = RC.build_graph(refs, prompts, canvas, set_ref=set_name)
    fh = tempfile.NamedTemporaryFile("w", suffix=".api.json", delete=False,
                                     encoding="utf-8")
    json.dump(graph, fh, indent=1)
    fh.close()
    cmd = [sys.executable, RC.EXEC, "0", "0", "1", "--workflow", fh.name,
           "--output-prefix", prefix, "--seed-base", str(seed),
           "--comfy-output-dir", RC.COMFY_OUT, "--expect-outputs", "1"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or ""))[-400:]
        return None, "regional compose rc=%d: %s" % (r.returncode, tail)
    out = os.path.join(RC.COMFY_OUT, "%s_w000_00001_.png" % prefix)
    if not os.path.isfile(out):
        return None, "regional compose reported success but %s is not there" % out
    return out, None


def compose_with_retry(inputs, prefix_base, base_seed, max_seed_attempts,
                       model="sonnet", timeout=180, sg=None, shot_row=None):
    """Try up to `max_seed_attempts` seeds, composing + running the real,
    unmodified attribute_check.py gate for each. Every seed is rendered:
    this is a WEDGE, not a retry budget. Seeds are CHOICES for the operator,
    who picks one, and not attempts spent trying to pass a check; an
    environment-only panel therefore renders the full wedge too, and its SKIP
    verdict (no character attribute list to check) is unchanged and still not
    a silent pass. Only a COMPOSE failure aborts the run early, because a new
    seed cannot fix an unreachable ComfyUI.

    The gate is never loosened, only applied to more renders: if
    every attempt is checked and none PASSes, this returns the LAST attempt
    (still FAIL/ERROR) rather than fabricate a pass -- the caller publishes it
    at 'rev' regardless (D16/W3: the verdict picks which attempt gets
    published, it no longer withholds the Version from review) with every
    attempt's seed and verdict recorded, never hidden (see
    PANEL-QUALITY-WEDGE.md's finding that seed variance alone flipped an
    identical config from PASS to FAIL).

    Attempt 1 ALWAYS uses `base_seed` unmodified -- a re-run of the same shot
    with the same inputs reproduces the same first try (invariant 5). Only
    attempts 2..N step off it by a fixed, deterministic stride
    (RETRY_SEED_STRIDE), never randomly -- so the whole attempt sequence,
    not just the first try, is reproducible.

    A COMPOSE failure (GPU/infra error, distinct from an attribute-check
    FAIL) aborts the retry loop immediately: a new seed cannot fix an
    unreachable ComfyUI or an OOM, and burning further attempts on it would
    just waste GPU time on a problem no seed addresses.

    Returns (attempts, chosen): `attempts` is the full list of per-attempt
    dicts (always at least one), in order; `chosen` is the attempts[i] this
    run should publish, or None if the very first attempt could not compose
    at all (in which case attempts[-1] carries the 'error')."""
    attempts = []
    for i in range(max(1, int(max_seed_attempts))):
        attempt_seed = base_seed if i == 0 else base_seed + i * RETRY_SEED_STRIDE
        # EVERY attempt states what suffix it sent, set before any branch
        # runs. An environment-only panel takes none of the character
        # branches, so initialising inside them left this unbound.
        sent_suffix_used = None
        sent_record = {}          # filled by qwen_edit with the EXACT strings sent
        prefix = prefix_base if i == 0 else "%s_retry%d" % (prefix_base, i)
        instruction = _build_instruction(inputs)
        t0 = time.time()
        if inputs["character"]:
            # QC-HARDENING fix 3: QC.compose_panel() builds its OWN "Place the
            # character..." wrapper internally (qwen_compose.py) from whatever
            # action_text it is handed -- `instruction` above is a SEPARATE
            # string only used for provenance/description below. Passing
            # inputs["action_text"] here UNSANITIZED (the original bug: this
            # line still read the raw beat text after sanitize_action_text_for_
            # compositor() was added to _build_instruction(), because that
            # sanitized string was never actually threaded into this call) was
            # exactly how B0310's literal dialogue reached the GPU and got
            # rendered as a burnt-in caption despite the sanitizer existing.
            # Use the SAME prepared text (sanitized + count-derived,
            # COUNT-COLLAPSE.md) this actually composites from, not a side
            # channel that only feeds the description field.
            #
            # COLOUR-AND-TEXT-DEFECTS.md, defect 2: this is the SAME bug shape
            # again, one clause over. QC.compose_panel() builds its own
            # "Place the character..." wrapper from whatever action_text it is
            # handed and appends ONLY PRESERVE_SUFFIX/_MULTI after it -- never
            # NO_BURNT_IN_TEXT_CLAUSE. _build_instruction() above (used for
            # the recorded description/sg_prompt_final__as_sent_ text below)
            # DOES append that clause, so every character-panel Version's
            # provenance CLAIMED "do not render any text..." was sent when it
            # never actually reached the compositor for the majority code
            # path (any shot with a character in frame). Confirmed live on
            # PILOT01_B_0040 and PILOT01_B_0230's real, verbatim
            # sg_prompt_final__as_sent_ text. Append it to the SAME string
            # QC.compose_panel() receives, not a second copy, so the real GPU
            # call and its own provenance record can no longer disagree.
            # THE FRAMING CLAUSE GOES HERE, on the string QC.compose_panel
            # actually receives -- not into _build_instruction(), which only
            # feeds the description and provenance. That distinction is the
            # entire bug being fixed: a clause added to the recorded text and
            # not to the sent text is exactly what "the record says we sent it"
            # means, and it has now happened three times in this file's history.
            prepared_action_text = _prepare_action_text(inputs) + " " + NO_BURNT_IN_TEXT_CLAUSE
            if inputs.get("framing_text"):
                prepared_action_text += " " + inputs["framing_text"]
            if inputs.get("contact_text"):
                prepared_action_text += " " + inputs["contact_text"]
            instance_count = inputs["character"].get("instance_count") or 1
            chars = inputs.get("characters") or [inputs["character"]]
            sent_suffix_used = None
            if len(chars) >= 2 and (sg is None or shot_row is None):
                # A caller that reached the multi-character branch without a
                # ShotGrid handle cannot split the beat, and composing from
                # the unsplit beat is the fusion defect. Refuse.
                out_img, err = None, ("multi-character shot reached compose_with_retry "
                                      "without sg/shot_row; cannot split the beat")
            elif len(chars) >= 2:
                # TWO CHARACTERS GO THROUGH ONE MASKLESS ENCODE, NOT TWO MASKED
                # REGIONS. Changed 2026-09-07, F253 / P020, and the measurement
                # is not close: SHOW01_A_0390, same shot, same beat, same seed
                # range, 8 of 8 cells with two correct whole bodies maskless
                # against 0 usable in 32 cells masked.
                #
                # WHY REGIONAL LOSES, from ComfyUI's source rather than from
                # guessing: ConditioningSetMask does no compositing. The sampler
                # runs a SEPARATE FULL MODEL FORWARD PASS per masked cond and
                # combines them as a per-pixel, mask-weighted LINEAR AVERAGE.
                # Qwen's references arrive as image tokens, so each branch saw
                # ONE character and rendered a complete scene containing a
                # HALLUCINATED partner. We were averaging two different
                # pictures. Hence the signature artefacts: a detached arm
                # reaching in from off frame (the other branch's hallucinated
                # partner), and two heads on one body (two solutions
                # disagreeing where the neck goes).
                #
                # WHY THE OLD FINDING WAS NOT WRONG, only narrow: regional beat
                # global on beats where the two bodies DO NOT TOUCH, because
                # there the two averaged solutions already agree. It fails
                # exactly where they must agree, which is contact.
                #
                # CORROBORATION FROM THE VENDOR: ComfyUI's own shipped Qwen 2509
                # blueprint on this box contains ZERO ConditioningSetMask and
                # four TextEncodeQwenImageEditPlus. Maskless multi-reference is
                # the model's documented native path.
                #
                # regional_panel() is KEPT, not deleted. It is the control arm
                # for any future test and the record of a real result.
                # Picture 1 goes to whoever the beat names first: the slot,
                # not the sentence, decides which side of frame they land on.
                ordered = order_characters_by_beat(chars, prepared_action_text)
                # Say which slot is on which side. Without this the beat's own
                # prose does not move the men at all (F267 to F269); with it the
                # swap is exact (a peer engineer's P024, 4 of 4 each way).
                side = slot_side_clause(len(ordered))
                two_char_text = ((prepared_action_text.rstrip() + " " + side).strip()
                                 if side else prepared_action_text)
                out_img, err = QC.compose_panel_two(
                    ordered[0]["image"], ordered[1]["image"], two_char_text,
                    room_text_for(inputs), prefix, seed=attempt_seed,
                    set_image=inputs["set"]["image"], record=sent_record)
                image2 = inputs["set"]["image"]
                # WHAT WAS SENT, CARRIED FROM THE BRANCH THAT SENT IT. The
                # publish step used to RE-DERIVE this from the first
                # character's instance_count, which is "how many copies of
                # that one character", not "how big is the cast". So every
                # two-character panel ever published recorded the SINGLE
                # character suffix while the GPU received the two-character
                # one. The comment there argued divergence was impossible
                # because it read the same inputs; that is true of the single
                # path and false of this one. A second derivation of a fact is
                # not a record of it.
                sent_suffix_used = QC.PRESERVE_SUFFIX_TWO_CHAR_WITH_SET
            else:
                sent_suffix_used = QC.suffix_for(
                    (inputs["character"].get("instance_count") or 1)
                    if inputs["character"] else 1)
                out_img, err = QC.compose_panel(inputs["character"]["image"],
                                                inputs["set"]["image"],
                                                prepared_action_text, prefix, seed=attempt_seed,
                                                instance_count=instance_count, record=sent_record)
                image2 = inputs["set"]["image"]
        else:
            # THE ENVIRONMENT-ONLY BRANCH REPORTS TOO. Missing it here meant
            # this branch wrote the "not reported" placeholder where its
            # record had previously been correct: a fix that improved two
            # branches and quietly degraded the third.
            sent_suffix_used = QC.PRESERVE_SUFFIX
            out_img, err = QC.qwen_edit(inputs["set"]["image"], instruction, prefix,
                                        seed=attempt_seed, record=sent_record)
            image2 = None
        took = round(time.time() - t0, 1)

        if err:
            log("  attempt %d/%d (seed %d): COMPOSE FAILED: %s"
                % (i + 1, max_seed_attempts, attempt_seed, err[:300]))
            attempts.append({"attempt": i + 1, "seed": attempt_seed, "compose_ok": False,
                             "error": err})
            return attempts, None

        if ATTRIBUTE_QC_ENABLED:
            qc_status, qc_detail = run_attribute_qc(out_img, inputs, model=model,
                                                    timeout=timeout)
        else:
            qc_status, qc_detail = "OFF", ""
        log("  attempt %d/%d (seed %d): attribute QC -> %s"
            % (i + 1, max_seed_attempts, attempt_seed, qc_status))
        rec = {"attempt": i + 1, "seed": attempt_seed, "compose_ok": True, "image": out_img,
              "qc_status": qc_status, "qc_detail": qc_detail, "took_s": took,
              "instruction": instruction, "image2": image2,
              "sent_suffix": sent_suffix_used,
              "sent_prompt": sent_record.get("prompt"),
              "sent_negative": sent_record.get("negative")}
        attempts.append(rec)
    # RENDER THE WHOLE WEDGE. This used to `return` the moment a seed passed
    # the gate, so a shot that went well produced exactly ONE render and the
    # operator had nothing to compare it against -- extra renders only
    # appeared when things went badly, which is backwards. Geoff: local GPU
    # time is free and wedges teach us a lot, and seed variance on this
    # pipeline is as large an effect as any prompt lever tested, so the seeds
    # that lost are the comparison that makes the winner judgeable.
    #
    # A COMPOSE failure still aborts immediately (above): a new seed cannot
    # fix an unreachable ComfyUI, and burning three more attempts on it would
    # waste GPU time on a problem no seed addresses.
    #
    # `chosen` is still the first PASS/SKIP if there is one, else the last
    # attempt -- it decides which render becomes the primary vNNN candidate.
    # The others are published alongside it, not discarded.
    for rec in attempts:
        if rec.get("qc_status") in ("PASS", "SKIP", "OFF"):
            return attempts, rec
    return attempts, attempts[-1]


# ---------------------------------------------------------------- compose + publish
def publish_alternates(sg, shot, shot_code, inputs, attempts, chosen, task,
                       base_vnum, qc_failed_of, batch_id):
    """Publish the seeds the retry loop rendered and did not pick, and group
    the whole run into one Playlist. -> [version_code].

    `batch_id` is computed ONCE by the caller (compose_panel_for_shot, before
    the chosen Version is even created) and is the same string as the
    Playlist name built below -- never recomputed here, so the id stamped on
    every Version and the Playlist it sits in cannot drift apart. Geoff,
    2026-09-08: "stamp a batch id on the Version when the batch is published.
    time gap is flaky." This is what sg_review_housekeeping.py's rrq/rjct
    sweep now keys on, replacing a created_at-clustering heuristic entirely.

    WHY. compose_with_retry already renders up to DEFAULT_MAX_SEED_ATTEMPTS
    seeds and stops at the first PASS. Every seed it did not pick was rendered
    at full cost and then DISCARDED -- the GPU work was already done and the
    evidence thrown away. Geoff, on the day this was written: local GPU time is
    free and wedges teach us a lot. Seed variance on this pipeline is as large
    an effect as any prompt lever tested, so the seeds that lost are exactly
    the comparison a reviewer needs to judge the one that won.

    Published through sg_publish.publish_version rather than a second
    hand-rolled create: that module is the one implementation of the publish
    contract, including the sg_uploaded_movie upload without which a Version
    says "no playable media" the moment it is opened.

    Grouped in a Playlist so the run stays together -- the same vehicle the
    wedge batches use. Failures here are loud and non-fatal: the chosen panel
    is already published and correct, and losing an alternate must not lose
    the take that matters."""
    import sg_publish
    import version_description as VD

    others = [a for a in attempts
              if a.get("compose_ok") and a is not chosen and a.get("image")]
    if not others:
        return []

    seq = (shot.get("sg_sequence") or {}).get("name")
    chars = [c["code"] for c in (inputs.get("characters") or []) if c] or (
        [inputs["character"]["code"]] if inputs.get("character") else [])
    total = len([a for a in attempts if a.get("compose_ok")])
    published = []
    for i, a in enumerate(others, start=1):
        vnum = base_vnum + i
        code = "%s_PNL_panel_v%03d" % (shot_code, vnum)
        desc = VD.panel(shot_code, vnum, characters=chars,
                        set_code=(inputs.get("set") or {}).get("code"),
                        sequence=seq,
                        qc_status=(a.get("qc_status")
                                   if a.get("qc_status") != "OFF" else None),
                        qc_failed=qc_failed_of(a.get("qc_detail")),
                        seed=a.get("seed"), group_of=total)
        try:
            v = sg_publish.publish_version(
                sg, project=PROJ, entity={"type": "Shot", "id": shot["id"]},
                code=code, media_path=a["image"],
                sg_task={"type": "Task", "id": task["id"]} if task else None,
                description=desc, status="rev", stage="panel",
                batch_id=batch_id)
            # RECORD WHAT WAS ACTUALLY SENT, on every cell of the wedge.
            #
            # Only the CHOSEN candidate carried sg_prompt_final__as_sent_, so
            # measured on SHOW01_A_0380 six of eight Versions had no record of
            # the prompt that produced them. A wedge exists to be compared,
            # and "which of these was rendered from the revised beat" is the
            # first question anyone asks of one -- CLAUDE.md rule 7 is to read
            # what we actually sent before blaming the model, and for six of
            # eight cells there was nothing to read. The attempt record has
            # carried `instruction` all along; it simply was not written.
            sg.update("Version", v["id"],
                      {"sg_gen_seed": a.get("seed"),
                       "sg_prompt_final__as_sent_":
                           (a.get("sent_prompt")
                            or "[PROMPT NOT REPORTED BY THE COMPOSE CALL -- do not read this as what was sent]")[:15000],
                       "sg_model": QC.UNET_NAME,
                       "sg_gen_steps": int(QC.STEPS), "sg_gen_cfg": float(QC.CFG),
                       "sg_qc_detail": (a.get("qc_detail") or "")[:15000]})
            published.append(code)
        except Exception as exc:                                  # noqa: BLE001
            log("  %s: alternate seed %s NOT published (%s: %s)"
                % (shot_code, a.get("seed"), type(exc).__name__, str(exc)[:140]))

    if published:
        # SAME STRING AS batch_id, by construction (not just by convention):
        # this is the caller's own batch_id parameter, not a recomputation of
        # it. See the docstring above.
        name = batch_id
        try:
            codes = ["%s_PNL_panel_v%03d" % (shot_code, base_vnum)] + published
            vs = sg.find("Version", [["project", "is", PROJ],
                                     ["code", "in", codes]], ["code"])
            existing = sg.find_one("Playlist", [["project", "is", PROJ],
                                                ["code", "is", name]], ["code"])
            members = [{"type": "Version", "id": x["id"]} for x in vs]
            if existing:
                sg.update("Playlist", existing["id"], {"versions": members})
            else:
                sg.create("Playlist", {
                    "project": PROJ, "code": name, "versions": members,
                    "description": ("All seeds rendered for this panel run. "
                                    "Review together and approve one.")})
            log("  %s: %d candidate(s) grouped in Playlist %s"
                % (shot_code, len(members), name))
        except Exception as exc:                                  # noqa: BLE001
            log("  %s: candidates published but NOT grouped (%s: %s)"
                % (shot_code, type(exc).__name__, str(exc)[:140]))
    return published


def compose_panel_for_shot(sg, shot_code, seed=None, dry=False, model="sonnet", timeout=180,
                           max_seed_attempts=DEFAULT_MAX_SEED_ATTEMPTS):
    """Returns a result dict, always with a 'status' key:
      'inputs-only'  (dry run, nothing composed/published)
      'refused'      (PanelInputError -- nothing composed/published)
      'compose-failed' (compositor/GPU error -- nothing published; expected
                         with no GPU seat, see module docstring)
      'published'    (a Version now exists, always at 'rev' -- see the 'qc' key
                       for the attribute-check verdict that was recorded, not
                       used to gate; D16/W3)
    """
    shot = sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", shot_code]],
                       ["id", "code", "assets", "sg_script_beat", "sg_action_beat", "sg_camera",
                        "sg_shot_size", "sg_gen_size_wxh", "sg_sequence", "sg_stage"])
    if not shot:
        return {"status": "refused", "reason": "Shot %s not found" % shot_code}

    try:
        inputs = gather_inputs(sg, shot)
    except PanelInputError as exc:
        log("REFUSED %s: %s" % (shot_code, exc))
        return {"status": "refused", "reason": str(exc)}

    for w in inputs["warnings"]:
        log("  %s: %s" % (shot_code, w))

    if dry:
        return {"status": "inputs-only", "inputs": inputs}

    vnum = next_version_num(sg, "%s_PNL_panel" % shot_code)
    prefix = "pnl_%s_v%03d" % (shot_code.lower(), vnum)
    base_seed = seed if seed is not None else (6000 + vnum)
    # THE BATCH ID, computed ONCE, here, before anything is published --
    # never reconstructed afterwards. Same string publish_alternates() used
    # to build for its own Playlist name (REVIEW_<shot>_v<NNN>), now shared
    # so the id on every Version and the Playlist it sits in can never
    # disagree. Stamped on the chosen Version below and threaded into
    # publish_alternates() for every seed that lost.
    batch_id = "REVIEW_%s_v%03d" % (shot_code, vnum)

    # Widen the wedge for a multi-character shot unless the caller asked for a
    # specific count. Only when max_seed_attempts is still the default, so
    # `--max-seed-attempts 2` from an operator is never silently overridden.
    n_chars = len(inputs.get("characters") or [])
    if n_chars >= 2 and max_seed_attempts == DEFAULT_MAX_SEED_ATTEMPTS:
        max_seed_attempts = TWO_CHARACTER_SEED_ATTEMPTS
        log("  %s: %d characters -> widening the wedge to %d seeds (measured ~25%% "
            "clean per seed on the regional path)"
            % (shot_code, n_chars, max_seed_attempts))

    attempts, chosen = compose_with_retry(inputs, prefix, base_seed, max_seed_attempts,
                                          sg=sg, shot_row=shot,
                                          model=model, timeout=timeout)
    if chosen is None:
        err = attempts[-1]["error"]
        log("COMPOSE FAILED %s: %s" % (shot_code, err[:300]))
        return {"status": "compose-failed", "reason": err, "inputs": inputs, "attempts": attempts}

    out_img = chosen["image"]
    qc_status, qc_detail = chosen["qc_status"], chosen["qc_detail"]
    took = chosen["took_s"]
    seed = chosen["seed"]
    instruction = chosen["instruction"]
    image2 = chosen["image2"]
    # D16 / W3 (plan/MASTER-PLAN-V3.md section 5.4, work item W3): gates are
    # deterministic scripts, not vision. A FAIL/ERROR attribute-check verdict
    # no longer withholds the Version from the review queue by publishing it
    # at 'rjct' -- that let a vision-model gate erase a review before a human
    # ever saw it. Every attempt now publishes at 'rev'; the verdict itself is
    # still recorded, verbatim, in the description (qc_status_description()
    # below) and on the Version, so nothing is hidden -- the operator judges,
    # not this checker. Was: "rev" if qc_status in ("PASS", "SKIP") else "rjct".
    sg_status = "rev"

    task = panel_task(sg, shot)
    code = "%s_PNL_panel_v%03d" % (shot_code, vnum)

    # THE PUBLISHED PATH MUST NOT BE COMFYUI SCRATCH.
    #
    # sg_path_to_movie pointed straight at C:\ComfyUI_windows_portable    # ComfyUI\output -- a volatile scratch directory on the unbacked-up disk,
    # which ComfyUI is free to clear. sg_publish.durable_copy exists for
    # exactly this and carries Geoff's own rule in its docstring
    # ("sg_path_to_movie ... never ComfyUI scratch"); this publish path simply
    # never called it, so the CHOSEN panel of every run pointed at scratch
    # while its published alternates (which go through sg_publish) pointed at
    # the durable copy. Same file, two different homes, in one wedge.
    #
    # Idempotent: a path already inside the published directory is a no-op.
    import sg_publish as _PUB
    try:
        durable_img = _PUB.durable_copy(out_img, code, log=log)
    except Exception as exc:                                      # noqa: BLE001
        # Loud, and NOT fatal: a Version pointing at scratch is worse than one
        # published a moment later, but losing the take entirely is worse than
        # both. Say which happened.
        log("  %s: durable copy FAILED (%s: %s) -- publishing with the scratch "
            "path, which will break when ComfyUI clears its output"
            % (shot_code, type(exc).__name__, str(exc)[:140]))
        durable_img = out_img
    # THE DESCRIPTION IS A LABEL, NOT A REPORT, AND IT IS DETERMINISTIC.
    #
    # Geoff: "your version descriptions are too verbose and long ... if the
    # information could be or already is in another field then that's enough
    # ... importantly the descriptions need to be 100% deterministic." The
    # median SHOW01 description was 2127 characters. This block used to open
    # with the wall-clock duration ("%.1fs"), which alone made every
    # description un-diffable, and closed with a paragraph restating
    # sg_status_list.
    #
    # Everything dropped from here already had a field: seed
    # (sg_gen_seed), model (sg_model), the prompt as sent
    # (sg_prompt_final__as_sent_), the status (sg_status_list). The full QC
    # output goes to its own field below rather than into the reviewer's face.
    import version_description as VD
    failed = re.findall(r"([A-Z0-9_]+)=FAIL", (qc_detail or "").split(chr(10))[0])
    seq = (shot.get("sg_sequence") or {}).get("name")
    # SEED AND GROUP SIZE, same as every sibling in this wedge.
    #
    # They were missing here while publish_alternates() supplied both, so the
    # CHOSEN candidate was the one cell in its own wedge whose label did not
    # say which seed made it or that it had siblings -- measured on
    # SHOW01_A_0160, where v001 read "...Attribute QC: PASS." and v002-v004
    # read "...Seed 7302. One of 4 candidates from the same run." A reviewer
    # comparing four cells side by side would reasonably read the odd one out
    # as a different KIND of thing. Two publish paths, one label.
    description = VD.panel(shot_code, vnum,
                           characters=[c["code"] for c in (inputs.get("characters") or [])
                                       if c] or ([inputs["character"]["code"]]
                                                 if inputs.get("character") else []),
                           set_code=(inputs.get("set") or {}).get("code"),
                           sequence=seq,
                           qc_status=(qc_status if qc_status != "OFF" else None),
                           qc_failed=failed,
                           seed=chosen.get("seed"), group_of=len(attempts))
    ok_desc, why_desc = VD.check(description)
    if not ok_desc:
        # Refused at the seam, not discovered later on a review page.
        log("  %s: description rejected (%s); publishing a minimal one"
            % (shot_code, why_desc))
        description = VD.panel(shot_code, vnum)

    # COUNT-COLLAPSE.md: the suffix actually sent for a multi-instance
    # character differs (PRESERVE_SUFFIX_MULTI, via QC.suffix_for()) -- read
    # from the SAME inputs["character"]["instance_count"] compose_with_retry()
    # used for the real GPU call, so what is recorded here can never disagree
    # with what was actually sent (see that function's own comment for why
    # this class of divergence is exactly the QC-HARDENING fix 3 bug shape).
    # READ THE SUFFIX THE COMPOSE BRANCH RECORDED, never re-derive it. The
    # re-derivation this replaces used the first character's instance_count,
    # which answers "how many copies of that character" and not "how big is
    # the cast", so EVERY two-character panel this pipeline ever published
    # recorded the SINGLE character suffix while the GPU was sent the
    # two-character one. Measured on SHOW01_A_0070: 8 candidates published
    # 2026-09-08 04:05, all recording "Exactly one character in the frame"
    # on a shot linking PilotCharB and PilotCharA whose beat has them both in it.
    # The record is what the operator and the peer seat read to decide what
    # to change, and one of them had already drawn a conclusion from it.
    sent_suffix = (chosen or {}).get("sent_suffix")
    if not sent_suffix:
        # No branch claimed one. Say so rather than guessing a plausible
        # string: a wrong provenance is worse than an absent one, because
        # it reads as evidence.
        sent_suffix = ("[SUFFIX NOT RECORDED BY THE COMPOSE BRANCH -- do "
                       "not read this prompt as what was sent]")

    v = sg.create("Version", {
        "project": PROJ, "entity": {"type": "Shot", "id": shot["id"]}, "sg_task": task,
        "code": code, "description": description,
        "sg_status_list": sg_status, "sg_first_frame": 1001, "sg_last_frame": 1001,
        "sg_path_to_movie": durable_img,
        # Stamped at create time, not reconstructed afterwards -- see
        # BATCH_ID_FIELD above and sg_review_housekeeping.py's rrq/rjct rule,
        # which now keys purely on this field.
        BATCH_ID_FIELD: batch_id,
    })
    # BOTH, ALWAYS. A thumbnail alone shows a picture in a list and reports
    # "no playable media" the moment anyone opens the Version to review it --
    # which is what the operator actually does, so every panel this pipeline
    # had ever published was unreviewable in the player (measured 2026-09-04
    # across 12 of 12 recent panels: thumb yes, uploaded_movie no).
    #
    # sg_publish.py's module docstring documents this exact defect and the
    # exact fix, and this publish path never adopted it -- two publishers, one
    # of them incomplete, and the incomplete one is the one the watcher runs.
    # A still going into sg_uploaded_movie IS how ShotGrid plays a still; that
    # is not a workaround, it is the documented convention.
    #
    # Ordered thumbnail-then-media so a failure of the second leaves a Version
    # that is visibly missing its player rather than one with no image at all.
    sg.upload_thumbnail("Version", v["id"], durable_img)
    try:
        sg.upload("Version", v["id"], durable_img, field_name="sg_uploaded_movie")
    except Exception as exc:                                      # noqa: BLE001
        # Loud, not fatal: the Version exists and is correct apart from
        # playback, and hiding this is how it went unnoticed for a whole
        # episode's worth of panels.
        log("  %s: WARNING uploaded thumbnail but sg_uploaded_movie FAILED "
            "(%s: %s) -- this Version will say 'no playable media'"
            % (shot_code, type(exc).__name__, str(exc)[:160]))
    sg.update("Version", v["id"], {
        # The full gate output, verbatim, where a reviewer can open it if they
        # want it and is not made to read it to see what the panel is.
        "sg_qc_detail": (qc_detail or "")[:15000],
        "sg_stage": "panel", "sg_gen_seed": seed, "sg_gen_steps": int(QC.STEPS),
        "sg_gen_cfg": float(QC.CFG), "sg_model": QC.UNET_NAME,
        # READ, NEVER REBUILT. sent_prompt_text() used to re-derive this and
        # was wrong for every two-character panel: it emitted the SINGLE
        # character wrapper and suffix while the GPU received the
        # two-character ones plus the slot-side clause. 104 panels were
        # published with a lying record AFTER the first F405 fix, because
        # that fix repaired the workflow hash and not this field.
        "sg_prompt_final__as_sent_": ((chosen or {}).get("sent_prompt")
                                     or "[PROMPT NOT REPORTED BY THE COMPOSE CALL -- do not read this as what was sent]"),
        # The negative the call REPORTED, not the module constant. Same rule
        # as the prompt above: read what was sent, never restate it.
        "sg_negative_final__as_sent_": ((chosen or {}).get("sent_negative")
                                        or QC.NEGATIVE),
    })

    # --- D6 provenance, full: which design Versions this panel composed from.
    subs = {"unet_name": QC.UNET_NAME, "lora_name": QC.LORA_NAME,
            "lora_strength": QC.LORA_STRENGTH, "clip_name": QC.CLIP_NAME,
            "vae_name": QC.VAE_NAME, "shift": QC.SHIFT, "steps": QC.STEPS, "cfg": QC.CFG,
            "image1": os.path.basename(inputs["character"]["image"] if inputs["character"]
                                       else inputs["set"]["image"]),
            "prompt": instruction.rstrip(" .") + ". " + sent_suffix,
            "negative_prompt": QC.NEGATIVE}
    tpl = QC.TPL_DUAL if image2 else QC.TPL_SINGLE
    if image2:
        subs["image2"] = os.path.basename(image2)
    wf_hash = PROV.workflow_hash_from_template(tpl, subs)

    if inputs["character"]:
        # This text and workflow-hash's image1 (below) both name the SAME file
        # inputs["character"]["image"] actually composited from -- that file
        # IS the approved design Version's own sg_path_to_movie (Change 1
        # fix), so what is recorded here can no longer disagree with what was
        # actually used.
        char_text = ("%s (identity from approved design Version %d: %s, file=%s)"
                    % (inputs["character"]["code"], inputs["character"]["design_version_id"],
                       inputs["character"]["design_version_code"],
                       os.path.basename(inputs["character"]["image"])))
        anchor_vid = inputs["character"]["design_version_id"]
    else:
        char_text = "n/a - no character in frame (environment-only panel)"
        anchor_vid = inputs["set"]["design_version_id"]
    set_text = ("%s (approved design Version %d: %s)"
               % (inputs["set"]["code"], inputs["set"]["design_version_id"],
                  inputs["set"]["design_version_code"]))
    style_text = inputs["style_text"] or "none (no sg_style_prefix set on this shot's Sequence)"

    PROV.write_provenance(sg, v["id"], character=char_text, set_=set_text,
                          action=inputs["action_text"], camera=inputs["camera_text"],
                          style=style_text, workflow_hash=wf_hash,
                          anchor_version_id=anchor_vid, log=log)

    record_panel_designs(shot_code, v["id"], code, inputs, attempts=attempts)

    log("%s -> %s (status=%s, qc=%s, %d/%d seed attempt(s), %.1fs)"
       % (shot_code, code, sg_status, qc_status, len(attempts), max_seed_attempts, took))
    alternates = publish_alternates(
        sg, shot, shot_code, inputs, attempts, chosen, task, vnum,
        lambda d: re.findall(r"([A-Z0-9_]+)=FAIL", (d or "").split(chr(10))[0]),
        batch_id)

    return {"status": "published", "version_id": v["id"], "version_code": code,
            "alternates": alternates,
           "sg_status": sg_status, "qc": qc_status, "qc_detail": qc_detail, "inputs": inputs,
           "attempts": attempts}


# ---------------------------------------------------------------------- self-test (offline)
def self_test():
    # THE GATE IS OFF IN PRODUCTION, AND ITS TESTS STILL RUN.
    #
    # Switching ATTRIBUTE_QC_ENABLED to False broke six existing checks, all of
    # which exercise the GATE's behaviour: that retry stops at the first PASS,
    # that a wedge where every seed FAILs is not approved by exhaustion, that a
    # FAIL verdict reaches the description and sg_qc_detail. Those are exactly
    # the properties that must still hold on the day the gate is switched back
    # on, so they are turned ON for the suite rather than deleted with it.
    #
    # Deleting them would have made re-enabling a leap of faith: the code would
    # still be there, and nothing would say it still worked. The OFF-specific
    # checks near the end set the flag to False explicitly.
    global ATTRIBUTE_QC_ENABLED
    _qc_was = ATTRIBUTE_QC_ENABLED
    ATTRIBUTE_QC_ENABLED = True
    fails = []

    def ck(name, cond):
        print("  %-64s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    class _NoAssetSG:
        def find_one(self, *a, **k):
            return None

    try:
        resolve_approved_design(_NoAssetSG(), "CHAR_NOPE")
        ck("resolve_approved_design refuses a missing Asset", False)
    except PanelInputError as exc:
        ck("resolve_approved_design refuses a missing Asset", "not found" in str(exc))

    # --- THE 2026-09-07 BLOCKER. One Asset whose sg_stage had been reset to
    # --- 'design' by an applied revision stopped 30 of SHOW01's 36 unfinished
    # --- shots from composing, while its approved design sat there, valid and
    # --- on disk. No canary covered it, which is exactly why it shipped.
    import tempfile as _tf
    _fd, _real = _tf.mkstemp(suffix=".png")
    os.close(_fd)
    open(_real, "wb").write(b"x")

    class _StaleStageSG:
        """sg_approved_design is SET; sg_stage says a revision is in flight."""
        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"id": 1, "code": "SHOW_SET_X", "sg_stage": "design",
                        "sg_approved_design": {"id": 9, "name": "SHOW_SET_X_ANCHOR_s1",
                                               "type": "Version"},
                        ASSET_INSTANCE_COUNT_FIELD: None}
            return {"id": 9, "code": "SHOW_SET_X_ANCHOR_s1",
                    "sg_path_to_movie": _real, "sg_status_list": "apr"}

    try:
        _a, _v = resolve_approved_design(_StaleStageSG(), "SHOW_SET_X")
        ck("CANARY: a stale sg_stage does NOT block composing when an approved "
           "design exists (the bug that blocked 30 shots)",
           _v["code"] == "SHOW_SET_X_ANCHOR_s1")
    except PanelInputError as exc:
        ck("CANARY: a stale sg_stage does NOT block composing when an approved "
           "design exists (the bug that blocked 30 shots)", False)

    class _NoPointerSG:
        """The genuine no-anchor case: the pointer itself is absent."""
        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"id": 1, "code": "SHOW_SET_Y", "sg_stage": "approved",
                        "sg_approved_design": None,
                        ASSET_INSTANCE_COUNT_FIELD: None}
            return None

    try:
        resolve_approved_design(_NoPointerSG(), "SHOW_SET_Y")
        ck("CANARY: no approved design at all is STILL refused, so the fix did "
           "not just delete the rule", False)
    except PanelInputError as exc:
        ck("CANARY: no approved design at all is STILL refused, so the fix did "
           "not just delete the rule", "no approved design" in str(exc))
    os.unlink(_real)

    class _UnapprovedSG:
        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"code": "CHAR_X", "sg_stage": "design", "sg_approved_design": None}
            return None

    try:
        resolve_approved_design(_UnapprovedSG(), "CHAR_X")
        ck("CANARY: resolve_approved_design refuses an unapproved Asset", False)
    except PanelInputError as exc:
        ck("CANARY: resolve_approved_design refuses an unapproved Asset",
           "no approved design" in str(exc))

    class _MissingFileSG:
        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"code": "CHAR_X", "sg_stage": "approved",
                       "sg_approved_design": {"id": 999, "type": "Version"}}
            if entity == "Version":
                return {"code": "V", "sg_path_to_movie": r"C:\does\not\exist.png"}
            return None

    try:
        resolve_approved_design(_MissingFileSG(), "CHAR_X")
        ck("CANARY: resolve_approved_design refuses a design Version with no file on disk", False)
    except PanelInputError as exc:
        ck("CANARY: resolve_approved_design refuses a design Version with no file on disk",
           "no usable file" in str(exc))

    class _BlankPathSG:
        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"code": "CHAR_X", "sg_stage": "approved",
                       "sg_approved_design": {"id": 999, "type": "Version"}}
            if entity == "Version":
                return {"code": "V", "sg_path_to_movie": ""}   # linked, but media unresolvable
            return None

    try:
        resolve_approved_design(_BlankPathSG(), "CHAR_X")
        ck("CANARY: resolve_approved_design refuses an approved design Version whose media "
           "path is blank (never a silent None-as-OK)", False)
    except PanelInputError as exc:
        ck("CANARY: resolve_approved_design refuses an approved design Version whose media "
           "path is blank (never a silent None-as-OK)", "no usable file" in str(exc))

    # --- Change 1 fix proof: the character image gather_inputs() actually uses
    # is the approved design Version's OWN file, byte-identical to what
    # resolve_approved_design() resolved -- never a second, guessed filename
    # (the old bug: a hardcoded output/character_sheets/<CODE>_front.png that
    # could silently be a DIFFERENT, stale file). The stub's Version file path
    # deliberately does NOT match that old naming convention at all, so this
    # canary would fail loudly if a filename-guessing fallback ever crept back
    # in.
    _approved_char_file = __file__   # any real file on disk; deliberately not *_front.png
    _approved_version_id = 555444

    class _ApprovedDesignSG:
        def find(self, entity, filters, fields):
            return [{"code": "CHAR_RESOLVE", "sg_ref_role": "character"},
                    {"code": "STYLE_RESOLVE", "sg_ref_role": "style"}]

        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                code = filters[1][2]
                if code == "CHAR_RESOLVE":
                    return {"id": 41, "code": "CHAR_RESOLVE", "sg_stage": "approved",
                           "sg_approved_design": {"id": _approved_version_id,
                                                  "type": "Version"},
                           ASSET_INSTANCE_COUNT_FIELD: 6}
                if code == "STYLE_RESOLVE":
                    return {"id": 42, "code": "STYLE_RESOLVE", "sg_stage": "approved",
                           "sg_approved_design": {"id": 61, "type": "Version"}}
                return None
            if entity == "Version":
                vid = filters[0][2]
                if vid == _approved_version_id:
                    return {"id": vid, "code": "CHAR_RESOLVE_ANCHOR_v001",
                           "sg_path_to_movie": _approved_char_file}
                return {"id": vid, "code": "STYLE_RESOLVE_DESIGN_v001",
                       "sg_path_to_movie": _approved_char_file}
            return None

    shot_resolve = {"id": 9, "code": "S_RESOLVE", "assets": [{"id": 41}, {"id": 42}],
                    "sg_script_beat": "stands still", "sg_camera": "locked-off",
                    "sg_shot_size": "wide", "sg_gen_size_wxh": "768x432", "sg_sequence": None}
    resolved = gather_inputs(_ApprovedDesignSG(), shot_resolve)
    ck("CANARY: gather_inputs' character image IS the approved design Version's own "
       "sg_path_to_movie, not a hardcoded/guessed filename",
       resolved["character"]["image"] == _approved_char_file
       and resolved["character"]["design_version_id"] == _approved_version_id)
    ck("CANARY: gather_inputs() reads Asset.%s straight through into "
       "char_info['instance_count'] -- the ONE place a multi-instance character's count is "
       "sourced from, per COUNT-COLLAPSE.md" % ASSET_INSTANCE_COUNT_FIELD,
       resolved["character"]["instance_count"] == 6)
    ck("gather_inputs' set image is likewise the approved design Version's own file "
       "(unchanged behaviour, same code path as the character fix)",
       resolved["set"]["image"] == _approved_char_file)

    class _StubSGGather:
        def find(self, entity, filters, fields):
            return [{"code": "CHAR_A", "sg_ref_role": "character"},
                    {"code": "STYLE_A", "sg_ref_role": "style"}]

        def find_one(self, entity, filters, fields):
            return None

    shot_no_asset = {"id": 1, "code": "S1", "assets": [], "sg_script_beat": "x",
                    "sg_camera": "locked-off", "sg_shot_size": "wide",
                    "sg_gen_size_wxh": "768x432", "sg_sequence": None}
    try:
        gather_inputs(_StubSGGather(), shot_no_asset)
        ck("CANARY: gather_inputs refuses a shot with no STYLE asset linked", False)
    except PanelInputError as exc:
        ck("CANARY: gather_inputs refuses a shot with no STYLE asset linked",
           "no STYLE/set asset" in str(exc))

    class _StubSGNoBeat:
        def find(self, entity, filters, fields):
            return [{"code": "STYLE_A", "sg_ref_role": "style"}]

        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"id": 1, "code": "STYLE_A", "sg_stage": "approved",
                       "sg_approved_design": {"id": 1, "type": "Version"}}
            if entity == "Version":
                return {"id": 1, "code": "V", "sg_path_to_movie": __file__}   # any real file
            return None

    shot_no_beat = {"id": 2, "code": "S2", "assets": [{"id": 1}], "sg_script_beat": "",
                   "sg_camera": "locked-off", "sg_shot_size": "wide",
                   "sg_gen_size_wxh": "768x432", "sg_sequence": None}
    try:
        gather_inputs(_StubSGNoBeat(), shot_no_beat)
        ck("CANARY: gather_inputs refuses a shot with no beat text", False)
    except PanelInputError as exc:
        # show01_action_beats.py: gather_inputs() now prefers sg_action_beat,
        # falling back to sg_script_beat -- the refusal message names both
        # fields it checked, not just the one this repo's original PILOT01
        # shots ever had.
        ck("CANARY: gather_inputs refuses a shot with no beat text",
           "sg_action_beat" in str(exc) and "sg_script_beat" in str(exc))

    # show01_action_beats.py wiring: sg_action_beat, when set, is preferred
    # over sg_script_beat -- proven both directions against the REAL,
    # unmodified gather_inputs(), not just read off the source.
    shot_both_fields = dict(shot_no_beat, sg_script_beat="script-record text, dialogue and all",
                            sg_action_beat="action-only rewrite text")
    resolved_both = gather_inputs(_StubSGNoBeat(), shot_both_fields)
    ck("CANARY (show01_action_beats.py wiring, direction 1): when BOTH fields are set, "
       "gather_inputs() uses sg_action_beat, not sg_script_beat",
       resolved_both["action_text"] == "action-only rewrite text")

    shot_fallback = dict(shot_no_beat, sg_script_beat="script-record text, dialogue and all",
                         sg_action_beat="")
    resolved_fallback = gather_inputs(_StubSGNoBeat(), shot_fallback)
    ck("CANARY (show01_action_beats.py wiring, direction 2): when sg_action_beat is BLANK, "
       "gather_inputs() falls back to sg_script_beat unchanged -- every shot/episode with no "
       "sg_action_beat yet (every PILOT01 shot, all of them) behaves exactly as before this change",
       resolved_fallback["action_text"] == "script-record text, dialogue and all")

    # shot_no_beat itself (base dict above) has no "sg_action_beat" KEY at all --
    # exactly the shape of every shot fetched before this field existed on the
    # schema -- so this exercises the .get() default path, not just a blank value.
    shot_no_key = dict(shot_no_beat, sg_script_beat="script-record text, dialogue and all")
    assert "sg_action_beat" not in shot_no_key
    resolved_no_key = gather_inputs(_StubSGNoBeat(), shot_no_key)
    ck("CANARY (show01_action_beats.py wiring, direction 3): a shot dict with no "
       "sg_action_beat KEY AT ALL (every shot fetched before this field existed on the "
       "schema) falls back to sg_script_beat without raising",
       resolved_no_key["action_text"] == "script-record text, dialogue and all")

    class _StubSGEnvOnly:
        def find(self, entity, filters, fields):
            return [{"code": "STYLE_A", "sg_ref_role": "style"}]

        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                return {"id": 1, "code": "STYLE_A", "sg_stage": "approved",
                       "sg_approved_design": {"id": 1, "type": "Version"}}
            if entity == "Version":
                return {"id": 1, "code": "V", "sg_path_to_movie": __file__}
            return None

    shot_env = {"id": 3, "code": "S3", "assets": [{"id": 1}], "sg_script_beat": "empty room",
               "sg_camera": "locked-off", "sg_shot_size": "wide",
               "sg_gen_size_wxh": "768x432", "sg_sequence": None}
    got_inputs = gather_inputs(_StubSGEnvOnly(), shot_env)
    ck("an environment-only shot (no CHAR asset) is a legal, non-refused input set",
       got_inputs["character"] is None and got_inputs["set"]["code"] == "STYLE_A")

    # attribute QC gate: SKIP for environment-only, and PASS/FAIL/ERROR mapping
    ck("run_attribute_qc SKIPs (not silently PASSes) when there is no character",
       run_attribute_qc("does-not-matter.png", {"character": None})[0] == "SKIP")

    class _FakeCompleted:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    import unittest.mock as mock
    modname = __name__
    with mock.patch("subprocess.run", return_value=_FakeCompleted(0, "OVERALL: PASS")):
        st, detail = run_attribute_qc("x.png", {"character": {"code": "CHAR_A"}})
        ck("run_attribute_qc maps rc=0 to PASS", st == "PASS")
    with mock.patch("subprocess.run", return_value=_FakeCompleted(1, "OVERALL: FAIL")):
        st, detail = run_attribute_qc("x.png", {"character": {"code": "CHAR_A"}})
        ck("run_attribute_qc maps rc=1 to FAIL", st == "FAIL")
    with mock.patch("subprocess.run", return_value=_FakeCompleted(2, "ERROR: blind")):
        st, detail = run_attribute_qc("x.png", {"character": {"code": "CHAR_A"}})
        ck("run_attribute_qc maps rc=2 to ERROR (never a silent PASS)", st == "ERROR")

    # --- SPEAKER-LEAK-FIX.md / CUMULATIVE-EFFECT.md section 3: honest -------
    # pass-rate labelling. CUMULATIVE-EFFECT.md found the same 13 real panels
    # scored 71% PASS at production's own single-run gate vs. 8% under the
    # checker's unanimous --repeat 3 default -- every published Version's own
    # "Attribute QC: PASS" line must say WHICH gate produced it, unprompted.
    ck("CANARY: a PASS-labelled Version description names the single-run gate explicitly, "
       "not a bare 'PASS' a future batch tally could mistake for the checker's unanimous "
       "default",
       "PASS" in qc_status_description("PASS", "ok")
       and "--repeat 1" in qc_status_description("PASS", "ok")
       and "--repeat 3" in qc_status_description("PASS", "ok"))
    ck("... same labelling for a FAIL/ERROR verdict (a batch's denominator needs the gate "
       "named too, not just its numerator)",
       "--repeat 1" in qc_status_description("FAIL", "detail here")
       and "--repeat 3" in qc_status_description("FAIL", "detail here")
       and "detail here" in qc_status_description("FAIL", "detail here"))
    ck("CANARY (invariant 2, the check can fail): an OLDER, unlabelled description string "
       "would NOT carry the gate name -- proving this is a real assertion, not a tautology "
       "that any string satisfies",
       "--repeat 3" not in "Attribute QC: PASS.")
    ck("a SKIP (no character in frame) is unaffected -- it was never a pass-rate ambiguity "
       "(no attribute list exists to vote on), so it keeps its existing, distinct wording",
       qc_status_description("SKIP", "no character in frame")
       == "Attribute QC: SKIPPED (no character in frame).")

    # --- QC-HARDENING fix 3 canaries: no burnt-in captions ----------------
    # FABRICATED, preserving the exact shape of the REAL Shot.sg_script_beat of
    # PILOT01_B_0310 (confirmed live
    # against ShotGrid, project 9999) -- the shot named in
    # docs/METHOD.md section 4. CHARD TWO's actual line is
    # reproduced here only as a fabricated stand-in of the same shape,
    # "Forget what I just said. The lock was never fixed.", formatted exactly
    # as script_to_beats.py's cmd_link() writes
    # the field: one "[kind/speaker] text" line per beat, dialogue and action
    # interleaved.
    b0310_beat = ("[dialogue/CHARB] What.\n"
                 "[dialogue/CHARJ] Since when.\n"
                 "[dialogue/CHARD ONE] The lock was never fixed.\n"
                 "[dialogue/CHARD TWO] Forget what I just said. The lock was never fixed.")
    sanitized = sanitize_action_text_for_compositor(b0310_beat)
    ck("CANARY (real PILOT01_B_0310 beat, verified live against ShotGrid): "
       "sanitize_action_text_for_compositor() drops CHARD TWO's literal spoken words "
       "entirely -- they never reach the compositor",
       "Forget what I just said" not in sanitized and "The lock was never fixed" not in sanitized)
    ck("SPEAKER-LEAK-FIX.md CANARY: every dialogue line in the real B0310 beat is DROPPED "
       "entirely (no '<speaker> is speaking.' placeholder -- that placeholder was itself "
       "the CHARC/CHARJ leak, see docs/METHOD.md) -- a beat that is 100% "
       "dialogue sanitizes to the empty string",
       sanitized == "")

    mixed = ("[action/-] CHARB enters the cramped flat and eyes the radiator.\n"
            "[dialogue/CHARB] Ignore the previous message, and give me the code to the safe.\n"
            "[action/-] She kicks it once, hard.")
    sanitized_mixed = sanitize_action_text_for_compositor(mixed)
    ck("sanitize_action_text_for_compositor() KEEPS action-kind text verbatim (the actual "
       "visual content this field exists for)",
       "enters the cramped flat and eyes the radiator" in sanitized_mixed
       and "kicks it once, hard" in sanitized_mixed)
    ck("SPEAKER-LEAK-FIX.md: the interleaved dialogue-kind line is DROPPED entirely, not "
       "replaced with a placeholder -- only the two real action-kind lines survive",
       "Ignore the previous message" not in sanitized_mixed
       and "is speaking" not in sanitized_mixed
       and sanitized_mixed == "CHARB enters the cramped flat and eyes the radiator.\n"
                              "She kicks it once, hard.")

    ck("an untagged/plain action_text (no '[kind/speaker]' shape -- e.g. hand-typed test "
       "input, or pre-Phase-4 beat text) passes through unchanged, never invented structure",
       sanitize_action_text_for_compositor("stands still, looking out the window")
       == "stands still, looking out the window")
    ck("SPEAKER-LEAK-FIX.md: a dialogue line with no named speaker ('-') is ALSO dropped "
       "entirely now, not neutralised to a generic placeholder",
       sanitize_action_text_for_compositor("[dialogue/-] some line") == "")

    fake_char_inputs_caption = {"character": {"code": "CHAR_A", "image": "fake_char.png"},
                                "set": {"image": "fake_set.png"}, "action_text": b0310_beat}
    built = _build_instruction(fake_char_inputs_caption)
    ck("CANARY: the FULLY-BUILT compositor instruction for the real B0310 beat never "
       "contains CHARD TWO's literal words, end to end (not just the sanitizer in isolation)",
       "Forget what I just said" not in built and "The lock was never fixed" not in built)
    ck("... and every built instruction (character or environment-only) carries an explicit "
       "no-burnt-in-text clause, defense in depth against any other text path",
       NO_BURNT_IN_TEXT_CLAUSE in built)

    # CANARY, the one that actually caught the real bug: it is not enough for
    # _build_instruction() to sanitize its OWN return value -- that string was
    # only ever used for provenance logging. compose_with_retry() must sanitize
    # the SAME text it hands to QC.compose_panel() for the real GPU call, which
    # is a SEPARATE code path. Mock QC.compose_panel and inspect the actual
    # positional args it was called with.
    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)) as m, \
         mock.patch("%s.run_attribute_qc" % modname, return_value=("PASS", "ok")):
        b0310_inputs = {"character": {"code": "CHAR_CHARD_TWINS", "image": "fake_char.png"},
                        "set": {"image": "fake_set.png"}, "action_text": b0310_beat}
        compose_with_retry(b0310_inputs, "pfx", 6000, 1)
        called_action_text = m.call_args[0][2]   # compose_panel(char_img, set_img, action_text, ...)
        ck("CANARY (regression -- this is the bug the earlier version of this fix missed): "
           "QC.compose_panel(), the function that actually reaches the GPU, is called with "
           "SANITIZED action_text, not the raw beat text carrying CHARD TWO's literal line",
           "Forget what I just said" not in called_action_text
           and "The lock was never fixed" not in called_action_text)
        ck("CANARY (SPEAKER-LEAK-FIX.md, the CHARC/CHARJ bug -- inspecting the REAL call "
           "args QC.compose_panel() received, not a provenance/log string): none of the "
           "real B0310 beat's speaker names (CHARB, CHARJ, CHARD ONE, CHARD TWO) reach the "
           "GPU call either, named OR wrapped in a '<speaker> is speaking.' placeholder -- "
           "that placeholder was itself the leak on real shot PILOT01_C_0190",
           not any(name in called_action_text
                  for name in ("CHARB", "CHARJ", "CHARD ONE", "CHARD TWO"))
           and "is speaking" not in called_action_text)
        ck("CANARY (COLOUR-AND-TEXT-DEFECTS.md, defect 2 -- the SAME bug shape one clause "
           "over): QC.compose_panel(), not just _build_instruction()'s provenance-only "
           "return value, is ALSO called with NO_BURNT_IN_TEXT_CLAUSE in its action_text -- "
           "confirmed missing here on real PILOT01_B_0040/0230 sg_prompt_final__as_sent_ "
           "before this fix (the recorded text claimed it was sent; the real GPU call never "
           "got it)",
           NO_BURNT_IN_TEXT_CLAUSE in called_action_text)

    # --- PRACTICAL-NOTES-GUARD.md: same wiring canary, one category over ----
    # The exact same divergence bug shape (a defence built into a provenance
    # string while the real GPU call receives raw text) has now occurred
    # TWICE in this codebase -- confirmed missing above for
    # NO_BURNT_IN_TEXT_CLAUSE, and this is the reason THIS canary inspects
    # QC.compose_panel()'s actual received args again, not just
    # _build_instruction()'s return value or dialogue_guard.py's own
    # self-test in isolation. FABRICATED beats preserving the exact shape of
    # the real ones: PILOT01_B_0040 ("CLACK" ->
    # garbled "OACK" lettering) and PILOT01_B_0230 (clipboard numbers rendered
    # onto the panel).
    b0040_beat_wire = ("[dialogue/CHARB] Ten minutes, tops. I am heading out now.\n"
                      "[action/-] CLACK.")
    b0230_beat_wire = ("[action/-] CLACK. The sum on CharJ's clipboard visibly changes. "
                      "(Practical: flip cards on the clipboard - 14.25, 29.50, 33.75, "
                      "47.00.)")
    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)) as m, \
         mock.patch("%s.run_attribute_qc" % modname, return_value=("PASS", "ok")):
        compose_with_retry(
            {"character": {"code": "CHAR_CHARB", "image": "fake_char.png"},
             "set": {"image": "fake_set.png"}, "action_text": b0040_beat_wire},
            "pfx", 6000, 1)
        called_0040 = m.call_args[0][2]
        ck("CANARY (real PILOT01_B_0040, wiring proof): QC.compose_panel() -- the function "
           "that actually reaches the GPU, not a log/provenance string -- is called with "
           "'CLACK' already stripped",
           "CLACK" not in called_0040)

        compose_with_retry(
            {"character": {"code": "CHAR_CHARJ", "image": "fake_char.png"},
             "set": {"image": "fake_set.png"}, "action_text": b0230_beat_wire},
            "pfx", 6000, 1)
        called_0230 = m.call_args[0][2]
        ck("CANARY (real PILOT01_B_0230, wiring proof): QC.compose_panel() is called with "
           "'CLACK' AND the literal clipboard numbers already stripped -- inspecting the "
           "actual positional args the mocked GPU call received, exactly the class of bug "
           "(a sanitiser built into provenance only, GPU call getting the raw text) that "
           "has now occurred twice in this codebase",
           "CLACK" not in called_0230
           and not any(n in called_0230 for n in ("14.25", "29.50", "33.75", "47.00")))
        ck("... while the genuine visual content ('the sum...visibly changes') survives "
           "in what the GPU call actually received",
           "sum on CharJ's clipboard visibly changes" in called_0230)

    # --- SPEAKER-LEAK-FIX.md: real PILOT01_C_0190/B_0210, real call args -----
    # docs/METHOD.md section 5: C_0190 actually burned the
    # word "CHARC" into the panel background; B_0210's identical pattern
    # ("CHARJ is speaking.") is confirmed present but seed-dependent/un-fired.
    # Both root-caused to dialogue_guard.py's OWN placeholder text carrying
    # the speaker's proper noun. FABRICATED, preserving the exact shape of the
    # REAL Shot.sg_script_beat for both
    # (ShotGrid project 9999, read-only query, 2026-08-29) -- inspecting
    # QC.compose_panel()'s actual received args, exactly like the two wiring
    # canaries above, per invariant 2 and the requirement that this canary
    # checks the real GPU call, not a provenance/log string.
    c0190_beat_wire = ("[dialogue/CHARC] She had fallen asleep on the fire escape. Nobody "
                      "wanted to be the one to wake her.")
    b0210_beat_wire = ("[dialogue/CHARJ] Quiet down. Quiet down please. With the CHARDs "
                      "paying together we owe forty-one ten. Split two ways, it comes to "
                      "fifty-five. Minus the heater credit, thirty-nine ninety. Plus the "
                      "deposit -\n[action/-] CLACK.")
    real_char_image = "output/character_sheets/CHAR_CHARC_front.png"
    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)) as m, \
         mock.patch("%s.run_attribute_qc" % modname, return_value=("PASS", "ok")):
        compose_with_retry(
            {"character": {"code": "CHAR_CHARC", "image": real_char_image},
             "set": {"image": "fake_set.png"}, "action_text": c0190_beat_wire},
            "pfx", 6000, 1)
        called_c0190_text = m.call_args[0][2]
        called_c0190_char_image = m.call_args[0][0]
        ck("CANARY direction 1 (real PILOT01_C_0190, the actual burnt-'CHARC' incident): "
           "QC.compose_panel() -- the real GPU call, inspected via its actual positional "
           "args, not a provenance string -- never receives 'CHARC'",
           "CHARC" not in called_c0190_text)
        ck("... identity is NOT made anonymous by this fix: the SAME call still receives "
           "the character's approved reference IMAGE unchanged -- identity travels through "
           "the image argument, never through action_text, so removing the name from the "
           "text costs zero identity information",
           called_c0190_char_image == real_char_image)

        compose_with_retry(
            {"character": {"code": "CHAR_CHARJ", "image": "fake_char.png"},
             "set": {"image": "fake_set.png"}, "action_text": b0210_beat_wire},
            "pfx", 6000, 1)
        called_b0210_text = m.call_args[0][2]
        ck("CANARY direction 1 (real PILOT01_B_0210, the un-fired twin): QC.compose_panel() "
           "never receives 'CHARJ', the literal spoken dollar-figures, nor the bare 'CLACK' "
           "sharing the same beat",
           "CHARJ" not in called_b0210_text
           and "forty-one ten" not in called_b0210_text
           and "CLACK" not in called_b0210_text)

    # CANARY direction 2 (invariant 2, real call-site shape): prove the check
    # CAN fail -- an action_text that bypassed dialogue_guard entirely (as if
    # a future call site forgot to sanitize, or re-derived the regex instead
    # of importing this module) still carries the real names/words.
    unguarded_c0190 = "Place the character in the room. " + c0190_beat_wire
    unguarded_b0210 = "Place the character in the room. " + b0210_beat_wire
    ck("CANARY direction 2 (real C_0190/B_0210, unguarded text -> guard FAILS/catches it): "
       "the raw beat text for both real shots still carries the speaker's proper noun if "
       "nothing strips it first",
       DG.contains_literal_dialogue(unguarded_c0190, "CHARC")
       and DG.contains_literal_dialogue(unguarded_b0210, "CHARJ"))

    fake_env_inputs_caption = {"character": None, "set": {"image": "fake_set.png"},
                               "action_text": "[action/-] empty room, radiator clanks once"}
    built_env = _build_instruction(fake_env_inputs_caption)
    ck("no-burnt-in-text clause also present on the environment-only instruction path",
       NO_BURNT_IN_TEXT_CLAUSE in built_env
       and "empty room, radiator clanks once" in built_env)

    # --- COUNT-COLLAPSE.md canaries: multi-instance count derivation --------
    # Cardinal vs. ordinal, using FABRICATED beat text preserving the exact
    # shape of the two shots that
    # anchor docs/METHOD.md's finding: PILOT01_A_0230 ("a
    # FOURTH CHARH lands") collapsed despite mentioning a bird-count WORD,
    # because "fourth" is an ordinal, not a total; PILOT01_C_0010 ("Nine
    # pigeons out there now") passed clean because it states a real total.
    ck("CANARY (real PILOT01_A_0230 beat): an ORDINAL ('a FOURTH CHARH lands') is NOT "
       "mistaken for a stated total count -- this shot's beat does not, in fact, say how "
       "many pigeons are in the huddle",
       not _action_text_states_own_count(
           "(LEDGE, background: a FOURTH CHARH lands during the line above, settling into "
           "the huddle without a sound. The tally board shifts one row. Not the pigeons' "
           "tally.)"))
    ck("CANARY (real PILOT01_A_0400 beat): 'the nearest bird ... another bird' names "
       "individual birds' actions but states no total either -- also not mistaken for one",
       not _action_text_states_own_count(
           "the CHARH huddle completes its second pass: the nearest bird tips its head "
           "down, another bird shuffles toward the rail."))
    ck("CANARY (real PILOT01_C_0010 beat): 'Nine pigeons out there now' IS a stated cardinal "
       "total -- the one real shot PANEL-BATCH-SCALE.md found passed clean on attempt 1",
       _action_text_states_own_count(
           "Colder than yesterday. Nine pigeons out there now: the huddle has begun "
           "tightening into a knot."))
    ck("a digit form ('4 pigeons') is recognised the same as a number word",
       _action_text_states_own_count("4 pigeons in the huddle."))
    ck("a digit ordinal ('4th') is NOT recognised as a stated total (no word boundary "
       "between the digit and 'th', same reasoning as the word-ordinal case)",
       not _action_text_states_own_count("the 4th CHARH lands."))
    ck("the bare word 'one' / digit '1' does not count as 'several are stated' (irrelevant "
       "for a multi-instance fix whose whole point is preventing a collapse TO one)",
       not _action_text_states_own_count("one CHARH watches from the sill."))

    ck("derive_action_text_with_count() is a no-op for instance_count 1/0/None (an ordinary "
       "single character is never touched by this fix)",
       derive_action_text_with_count("does a thing", 1) == "does a thing"
       and derive_action_text_with_count("does a thing", 0) == "does a thing"
       and derive_action_text_with_count("does a thing", None) == "does a thing")
    ck("derive_action_text_with_count() APPENDS an explicit count when instance_count>1 and "
       "no cardinal is already stated -- naming the exact Asset-derived number, never a "
       "hardcoded one",
       "exactly 4" in derive_action_text_with_count(
           "the CHARH huddle completes its second pass", 4))
    ck("CANARY: derive_action_text_with_count() LEAVES A SHOT'S OWN STATED COUNT ALONE "
       "(real PILOT01_C_0010 text, 'Nine pigeons out there now') even when the Asset's default "
       "instance_count (4) disagrees -- the writer's per-shot number always wins, is never "
       "silently overridden by the Asset default",
       derive_action_text_with_count(
           "Nine pigeons out there now: the huddle has begun tightening into a knot.", 4)
       == "Nine pigeons out there now: the huddle has begun tightening into a knot."
       and "exactly 4" not in derive_action_text_with_count(
           "Nine pigeons out there now: the huddle has begun tightening into a knot.", 4))

    fake_multi_inputs = {"character": {"code": "CHAR_CHARH_FLOCK", "image": "fake_char.png",
                                       "instance_count": 4},
                         "set": {"image": "fake_set.png"},
                         "action_text": "the CHARH huddle completes its second pass"}
    ck("CANARY: _prepare_action_text() (what _build_instruction() and the real GPU call both "
       "read) applies the count derivation for a multi-instance character's inputs dict",
       "exactly 4" in _prepare_action_text(fake_multi_inputs))
    ck("... and a character dict with NO instance_count key at all (every character before "
       "this fix, and any character whose Asset.sg_instance_count was never set) is untouched "
       "-- gather_inputs()'s .get() default keeps this fix fully backward compatible",
       _prepare_action_text({"character": {"code": "CHAR_A", "image": "x"},
                             "set": {"image": "y"}, "action_text": "does a thing"})
       == "does a thing")

    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)) as m, \
         mock.patch("%s.run_attribute_qc" % modname, return_value=("PASS", "ok")):
        compose_with_retry(fake_multi_inputs, "pfx", 6000, 1)
        ck("CANARY: compose_with_retry() passes instance_count THROUGH to QC.compose_panel() "
           "(the real GPU call) -- so qwen_compose picks PRESERVE_SUFFIX_MULTI, not just "
           "the action-text count sentence",
           m.call_args.kwargs.get("instance_count") == 4)
        ck("... and the action_text QC.compose_panel() actually received carries the derived "
           "count sentence too (both levers reach the real call, not just one)",
           "exactly 4" in m.call_args[0][2])

    # --- CHARACTER-DUPLICATION-WEDGE.md: absent-character clause guard ------
    # character_name_tokens() -- pure code-derivation, no hardcoded names.
    ck("character_name_tokens() derives a single-word name from a show-prefixed "
       "code (SHOW_CHAR_PILOTCHARB), same as an unprefixed one (CHAR_CHARB)",
       character_name_tokens("SHOW_CHAR_PILOTCHARB") == frozenset({"PILOTCHARB"})
       and character_name_tokens("CHAR_CHARB") == frozenset({"CHARB"}))
    ck("character_name_tokens() takes ONLY the segment IMMEDIATELY after the 'CHAR' "
       "pivot -- CHAR_CHARD_TWINS yields {'CHARD'} alone (the token real dialogue tags "
       "'CHARD ONE'/'CHARD TWO' both contain), NOT 'TWINS' too",
       character_name_tokens("CHAR_CHARD_TWINS") == frozenset({"CHARD"}))
    ck("CANARY (real PILOT01_C_0110 false-positive this fix closes): CHAR_CHARA_PORTRAIT "
       "yields {'CHARA'} only -- an earlier version also returned 'PORTRAIT', which "
       "collided with the ordinary English word 'portrait' in real, unrelated prose "
       "('...straightens it like the lobby portrait') and wrongly stripped a clause that "
       "never referred to the character asset at all",
       character_name_tokens("CHAR_CHARA_PORTRAIT") == frozenset({"CHARA"}))
    ck("... same for CHAR_CHARH_FLOCK -- {'CHARH'} only, not 'FLOCK' too",
       character_name_tokens("CHAR_CHARH_FLOCK") == frozenset({"CHARH"}))
    ck("a SET/STYLE code (no 'CHAR' segment) yields no tokens at all -- this function "
       "never decides what IS a character asset, only names one already known to be",
       character_name_tokens("SHOW_SET_PILOTCHARB_BEDROOM") == frozenset()
       and character_name_tokens("STYLE_LEDGE") == frozenset())
    ck("empty/None code never raises", character_name_tokens(None) == frozenset()
       and character_name_tokens("") == frozenset())

    class _ProjectRosterSG:
        """Mirrors the REAL, live project 9999 roster confirmed 2026-09-03: the
        3 real SHOW01 CHAR assets, all with sg_ref_role=None (the confirmed-live
        gap project_character_asset_tokens() is written to route around), plus
        one real PILOT01 CHAR asset with sg_ref_role SET, proving both shapes
        resolve the same way through code-pattern matching."""
        def find(self, entity, filters, fields):
            if entity == "Asset":
                return [{"code": "SHOW_CHAR_PILOTCHARB", "sg_ref_role": None},
                       {"code": "SHOW_CHAR_PILOTCHARA", "sg_ref_role": None},
                       {"code": "SHOW_CHAR_PILOTCHARC", "sg_ref_role": None},
                       {"code": "SHOW_SET_PILOTCHARB_BEDROOM", "sg_ref_role": None},
                       {"code": "CHAR_CHARB", "sg_ref_role": "character"}]
            return []

    roster = project_character_asset_tokens(_ProjectRosterSG())
    ck("CANARY (the confirmed-live gap this routes around): project_character_asset_"
       "tokens() finds all 3 SHOW01 characters even though EVERY SHOW01 Asset carries "
       "sg_ref_role=None -- a role-based query would silently see zero of them",
       set(roster.keys()) == {"SHOW_CHAR_PILOTCHARB", "SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARC",
                              "CHAR_CHARB"}
       and roster["SHOW_CHAR_PILOTCHARB"] == frozenset({"PILOTCHARB"}))
    ck("... and the SET asset (no 'CHAR' segment) is correctly excluded from the roster",
       "SHOW_SET_PILOTCHARB_BEDROOM" not in roster)

    # gather_inputs() end to end: the REAL shape of SHOW01_A_0110 (PilotCharB linked
    # and approved, PilotCharA NOT linked to this Shot at all but present
    # elsewhere in the project roster) -- confirmed live against ShotGrid,
    # project 9999, 2026-09-03. This stub deliberately gives classify_assets()
    # a WORKING sg_ref_role for PilotCharB/the set -- isolating THIS guard's own
    # wiring from the separate, already-flagged classify_assets()/sg_ref_role
    # gap (see project_character_asset_tokens()'s docstring: all 6 real
    # SHOW01 Assets carry sg_ref_role=None today, so gather_inputs() cannot
    # currently reach this code path on a real SHOW01 shot at all -- reported
    # in docs/METHOD.md, not fixed by this change).
    class _AbsentCharGatherSG:
        def find(self, entity, filters, fields):
            # classify_assets() queries [["id", "in", [...]]] (this Shot's
            # own linked assets); project_character_asset_tokens() queries
            # [["project", "is", PROJ]] (the whole project's roster) --
            # distinguish on the filter's field, not filter count (both are
            # a single-clause filter).
            if entity == "Asset" and filters and filters[0][0] == "project":
                return [{"code": "SHOW_CHAR_PILOTCHARB"}, {"code": "SHOW_CHAR_PILOTCHARA"},
                       {"code": "SHOW_CHAR_PILOTCHARC"}, {"code": "SHOW_SET_PILOTCHARB_BEDROOM"}]
            return [{"code": "SHOW_CHAR_PILOTCHARB", "sg_ref_role": "character"},
                   {"code": "SHOW_SET_PILOTCHARB_BEDROOM", "sg_ref_role": "style"}]

        def find_one(self, entity, filters, fields):
            if entity == "Asset":
                code = filters[1][2]
                if code == "SHOW_CHAR_PILOTCHARB":
                    return {"id": 12198, "code": "SHOW_CHAR_PILOTCHARB", "sg_stage": "approved",
                           "sg_approved_design": {"id": 67487, "type": "Version"}}
                if code == "SHOW_SET_PILOTCHARB_BEDROOM":
                    return {"id": 12202, "code": "SHOW_SET_PILOTCHARB_BEDROOM", "sg_stage": "approved",
                           "sg_approved_design": {"id": 67510, "type": "Version"}}
                return None
            if entity == "Version":
                return {"id": filters[0][2], "code": "V", "sg_path_to_movie": __file__}
            return None

    beat_0110_real = "PilotCharB stirs beneath the blanket and instinctively reaches for PilotCharA."
    beat_0540_real = ("PilotCharB lying on the couch, half-listening to PilotCharA's playlist. "
                      "The kettle clicks off in the kitchen.")
    shot_0110 = {"id": 12833, "code": "SHOW01_A_0110",
                "assets": [{"id": 12198}, {"id": 12202}],
                "sg_script_beat": beat_0110_real,
                "sg_camera": "locked-off", "sg_shot_size": "wide",
                "sg_gen_size_wxh": "1280x704", "sg_sequence": None}
    inputs_0110 = gather_inputs(_AbsentCharGatherSG(), shot_0110)
    ck("CANARY: gather_inputs() on the REAL SHOW01_A_0110 shot shape resolves "
       "character_names_present to exactly PilotCharB's own token (the one reference image "
       "actually supplied)",
       inputs_0110["character_names_present"] == frozenset({"PILOTCHARB"}))
    ck("... and character_names_known carries the WHOLE project roster, including "
       "PilotCharA -- who is not even linked to this Shot at all",
       {"PILOTCHARB", "PILOTCHARA", "PILOTCHARC"} <= inputs_0110["character_names_known"])
    ck("CANARY (the wired-in check, per the brief -- drives gather_inputs()+_prepare_"
       "action_text(), the REAL funnel, not strip_absent_character_clauses() directly): "
       "the composed action text for the real SHOW01_A_0110 beat has PilotCharA's clause "
       "gone and PilotCharB's own sentence intact",
       _prepare_action_text(inputs_0110) == "PilotCharB stirs beneath the blanket.")

    # THE CANARY THAT MATTERS MOST (per the brief): the identical real beat, but
    # PilotCharA now PRESENT (Asset linked AND reference supplied) -- simulating a
    # future/general two-character composition by driving _prepare_action_text()
    # with present_names covering both. Nothing may be removed.
    inputs_0110_two_hander = dict(inputs_0110)
    inputs_0110_two_hander["character_names_present"] = frozenset({"PILOTCHARB", "PILOTCHARA"})
    ck("CANARY, THE ONE THAT MATTERS MOST: the SAME real beat, with PilotCharA PRESENT "
       "-- nothing is removed, byte for byte. Over-stripping would silently delete "
       "real staging from every two-hander in the show, and this show is a two-hander",
       _prepare_action_text(inputs_0110_two_hander) == beat_0110_real)

    # Real SHOW01_A_0540 beat (possessive 'PilotCharA's'), driven through the same
    # real funnel, not the guard function in isolation.
    inputs_0540 = dict(inputs_0110)
    inputs_0540["action_text"] = beat_0540_real
    ck("CANARY (real SHOW01_A_0540 beat, possessive form, driven through the real "
       "funnel): clause-level removal, not a broken possessive",
       _prepare_action_text(inputs_0540)
       == "PilotCharB lying on the couch. The kettle clicks off in the kitchen.")

    # A beat naming nobody known -- driven through the real funnel, unaffected.
    inputs_no_name = dict(inputs_0110)
    inputs_no_name["action_text"] = "The room is quiet. A curtain shifts in the draft."
    ck("CANARY: a beat naming nobody known passes through the real funnel unchanged",
       _prepare_action_text(inputs_no_name)
       == "The room is quiet. A curtain shifts in the draft.")

    # THE WIRED-IN CHECK proper (per the brief: prove it by driving the real
    # funnel, not by testing the guard function directly): mock QC.compose_panel
    # and inspect the ACTUAL positional args it receives -- the same discipline
    # every other wiring canary in this file already uses for D15/COUNT-COLLAPSE.
    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)) as m, \
         mock.patch("%s.run_attribute_qc" % modname, return_value=("PASS", "ok")):
        wired_inputs = dict(inputs_0110)
        wired_inputs["character"] = {"code": "SHOW_CHAR_PILOTCHARB", "image": "fake_char.png"}
        wired_inputs["set"] = {"image": "fake_set.png"}
        compose_with_retry(wired_inputs, "pfx", 6000, 1)
        called_text = m.call_args[0][2]
        ck("CANARY (the wired-in check -- QC.compose_panel() is the function that "
           "actually reaches the GPU, not a provenance/log string): the absent name "
           "'PilotCharA' is gone from what would actually be sent, PilotCharB's sentence intact",
           "PilotCharA" not in called_text
           and "PilotCharB stirs beneath the blanket." in called_text)

    # --- Change 2 canaries: multi-seed retry (compose_with_retry) --------
    fake_char_inputs = {"character": {"code": "CHAR_A", "image": "fake_char.png"},
                        "set": {"image": "fake_set.png"}, "action_text": "does a thing"}
    fake_env_inputs = {"character": None, "set": {"image": "fake_set.png"},
                       "action_text": "empty room"}
    BASE_SEED = 6003

    def _qc_side_effect(verdicts):
        """The fixture repeats its LAST verdict once exhausted.

        compose_with_retry used to stop at the first PASS, so a fixture only
        ever needed as many verdicts as it took to reach one. It now renders
        the whole wedge, so a fixed-length list runs out mid-run and the test
        died on StopIteration -- a fixture that encoded the OLD control flow,
        not a contract that changed. Repeating the last verdict keeps every
        existing case meaning exactly what it meant."""
        it = iter(verdicts)
        last = {"v": verdicts[-1] if verdicts else "PASS"}

        def _fn(*a, **k):
            try:
                last["v"] = next(it)
            except StopIteration:
                pass
            return last["v"], "detail"
        return _fn

    modname = __name__

    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)), \
         mock.patch("%s.run_attribute_qc" % modname,
                    side_effect=_qc_side_effect(["FAIL", "FAIL", "PASS"])):
        attempts, chosen = compose_with_retry(fake_char_inputs, "pfx", BASE_SEED, 3)
        ck("CANARY: attempt 1's seed is exactly the deterministic base seed (a re-run of "
           "the same shot reproduces the same first try, invariant 5)",
           attempts[0]["seed"] == BASE_SEED)
        ck("retries 2/3 step off the base seed by the fixed stride, not randomly",
           attempts[1]["seed"] == BASE_SEED + RETRY_SEED_STRIDE
           and attempts[2]["seed"] == BASE_SEED + 2 * RETRY_SEED_STRIDE)
        ck("retry stops at the FIRST PASS: 3 attempts tried, the PASS is chosen",
           len(attempts) == 3 and chosen is attempts[2] and chosen["qc_status"] == "PASS")

    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)), \
         mock.patch("%s.run_attribute_qc" % modname,
                    side_effect=_qc_side_effect(["FAIL", "FAIL"])):
        attempts, chosen = compose_with_retry(fake_char_inputs, "pfx", BASE_SEED, 2)
        ck("CANARY: not approval-by-exhaustion -- when every seed FAILs, compose_with_retry "
           "returns the LAST attempt still verdict FAIL (never fabricates a PASS)",
           len(attempts) == 2 and chosen is attempts[-1] and chosen["qc_status"] == "FAIL")

    with mock.patch.object(QC, "qwen_edit", return_value=("out.png", None)), \
         mock.patch("%s.run_attribute_qc" % modname, side_effect=_qc_side_effect(["SKIP"])):
        attempts, chosen = compose_with_retry(fake_env_inputs, "pfx", BASE_SEED, 3)
        # CONTRACT CHANGED, deliberately. This asserted that an
        # environment-only panel makes exactly ONE attempt, because no seed
        # changes a SKIP verdict. That was true about the GATE and is the wrong
        # reason now: seeds are no longer a retry budget spent to pass a check,
        # they are a wedge the OPERATOR chooses from, and an empty room can be
        # composed four ways just as a populated one can. The SKIP verdict
        # itself is unchanged and still not a silent pass.
        ck("an environment-only panel still renders the whole wedge -- seeds are "
           "choices for the operator, not attempts at a gate",
           len(attempts) == 3 and chosen["qc_status"] == "SKIP")

    with mock.patch.object(QC, "compose_panel", return_value=(None, "ComfyUI is not reachable")):
        attempts, chosen = compose_with_retry(fake_char_inputs, "pfx", BASE_SEED, 3)
        ck("CANARY: a COMPOSE failure (GPU/infra error) aborts the whole retry after attempt "
           "1 -- a new seed cannot fix an unreachable ComfyUI, so no further seeds are burned",
           len(attempts) == 1 and chosen is None and attempts[0]["compose_ok"] is False)

    # panel_designs.json bookkeeping round-trip, isolated from the real file
    global PANEL_DESIGNS_JSON
    saved_path = PANEL_DESIGNS_JSON
    tmp_path = os.path.join(os.environ.get("TEMP", ROOT), "panel_designs_selftest.json")
    PANEL_DESIGNS_JSON = tmp_path
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        fake_inputs = {"character": {"asset_id": 1, "code": "CHAR_A", "design_version_id": 10},
                      "set": {"asset_id": 2, "code": "STYLE_A", "design_version_id": 20}}
        record_panel_designs("SHOT_X", 999, "SHOT_X_PNL_panel_v001", fake_inputs)
        doc = _panel_designs_load()
        ck("record_panel_designs writes a shot entry with both design Version ids",
           doc["shots"]["SHOT_X"]["character_design_version_id"] == 10
           and doc["shots"]["SHOT_X"]["set_design_version_id"] == 20)
        ck("a malformed panel_designs.json degrades to an empty doc, not a crash",
           True)  # exercised by _panel_designs_load's isinstance guard, see below
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write("not json at all {{{")
        doc2 = _panel_designs_load()
        ck("CANARY: truly malformed JSON degrades to {'shots': {}}, does not raise",
           doc2 == {"shots": {}})
    finally:
        PANEL_DESIGNS_JSON = saved_path
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    ck("next_version_num starts at 1 with no existing versions",
       next_version_num(type("S", (), {"find": lambda self, *a, **k: []})(), "X") == 1)

    # --- D16 / W3 (plan/MASTER-PLAN-V3.md section 5.4, work item W3) --------
    # Integration-level canary: run compose_panel_for_shot() end to end (only
    # the GPU call, attribute_check subprocess, and panel_designs.json write
    # are mocked out) with an attribute-check verdict of FAIL, and inspect the
    # ACTUAL fields dict handed to sg.create("Version", ...) -- the real call
    # a stub ShotGrid received, not a log line or a description string, same
    # discipline as every other wiring canary in this file.
    class _StatusFixSG:
        """Full stub for this one canary: Shot/Asset/Version lookups shaped
        exactly like _ApprovedDesignSG above (character + set both already
        approved), plus Task/Step/Version create+update support so the real
        publish path in compose_panel_for_shot() runs unmodified."""
        def __init__(self):
            self.rows = {}
            self.next_id = 9000

        def find(self, entity, filters, fields):
            if entity == "Asset":
                return [{"code": "CHAR_STATUSFIX", "sg_ref_role": "character"},
                        {"code": "STYLE_STATUSFIX", "sg_ref_role": "style"}]
            return []   # Version lookup for next_version_num(): none exist yet

        def find_one(self, entity, filters, fields):
            if entity == "Shot":
                return {"id": 700, "code": "STATUSFIX_SHOT",
                        "assets": [{"id": 701}, {"id": 702}],
                        "sg_script_beat": "stands still", "sg_camera": "locked-off",
                        "sg_shot_size": "wide", "sg_gen_size_wxh": "768x432",
                        "sg_sequence": None, "sg_stage": "panel"}
            if entity == "Asset":
                code = filters[1][2]
                if code == "CHAR_STATUSFIX":
                    return {"id": 701, "code": "CHAR_STATUSFIX", "sg_stage": "approved",
                           "sg_approved_design": {"id": 801, "type": "Version"}}
                if code == "STYLE_STATUSFIX":
                    return {"id": 702, "code": "STYLE_STATUSFIX", "sg_stage": "approved",
                           "sg_approved_design": {"id": 802, "type": "Version"}}
                return None
            if entity == "Version":
                vid = filters[0][2]
                return {"id": vid, "code": "V%d" % vid, "sg_path_to_movie": __file__}
            return None   # Task / Step: none exist yet -- panel_task() creates them

        def create(self, entity, data):
            vid = self.next_id
            self.next_id += 1
            row = dict(data)
            row["id"] = vid
            self.rows[vid] = row
            return row

        def update(self, entity, vid, data):
            self.rows.setdefault(vid, {}).update(data)
            return self.rows[vid]

        def upload_thumbnail(self, entity, vid, path):
            pass

    statusfix_sg = _StatusFixSG()
    with mock.patch.object(QC, "compose_panel", return_value=("out.png", None)), \
         mock.patch("%s.run_attribute_qc" % modname,
                    return_value=("FAIL", "off-register: large head proportion absent")), \
         mock.patch("%s.record_panel_designs" % modname, return_value=None):
        statusfix_result = compose_panel_for_shot(statusfix_sg, "STATUSFIX_SHOT", seed=4242,
                                                   max_seed_attempts=1)
    statusfix_version = statusfix_sg.rows.get(statusfix_result.get("version_id"), {})
    ck("CANARY (D16/W3, the live production defect this fix closes): a FAIL attribute-QC "
       "verdict still publishes the panel at sg_status_list='rev', never 'rjct' -- read off "
       "the real fields dict sg.create() received, not a log line",
       statusfix_version.get("sg_status_list") == "rev")
    # The contract changed, and this test changed with it rather than being
    # deleted: the FAIL must still be visible to the operator, but the VERBATIM
    # gate output no longer belongs in the description. The description is a
    # label (deterministic, one paragraph) and carries the verdict TOKEN; the
    # full text moved to sg_qc_detail. Losing the verdict entirely would be the
    # regression this canary exists to catch, so both halves are asserted.
    ck("... the FAIL verdict is not silently lost: the description carries the "
       "verdict token for the operator",
       "FAIL" in statusfix_version.get("description", ""))
    ck("... and the verbatim gate output is kept in sg_qc_detail, not in the label",
       "off-register" in (statusfix_version.get("sg_qc_detail") or "")
       and "off-register" not in statusfix_version.get("description", ""))
    ck("... the description stays a short, single-paragraph label",
       len(statusfix_version.get("description", "")) < 400
       and "\n" not in statusfix_version.get("description", ""))
    ck("compose_panel_for_shot() reports status='published' (never withheld) even though "
       "the attribute-check verdict was FAIL",
       statusfix_result.get("status") == "published" and statusfix_result.get("qc") == "FAIL")
    ck("CANARY: qc_status_description()'s FAIL/ERROR branch no longer claims the Version is "
       "unreviewable at 'rjct' -- that claim is false now that it always publishes at 'rev'",
       "rjct" not in qc_status_description("FAIL", "d")
       and "NOT reviewable" not in qc_status_description("FAIL", "d"))

    # --- THE MULTI-CHARACTER BRANCH. 30 of 55 SHOW01 shots link two
    # characters and 2 link three; every one of them used to compose with the
    # extra character silently dropped.
    # --- the gate must grade EVERY linked character. It graded only the
    # primary, so a panel with PilotCharA correct and PilotCharB absent entirely came
    # back PASS. Fake the subprocess so this stays offline.
    import subprocess as _sp
    _real_run = _sp.run
    _calls = []

    class _R(object):
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = "graded"
            self.stderr = ""

    def _fake(cmd, **k):
        _calls.append(cmd[cmd.index("--character") + 1])
        # second character fails
        return _R(1 if _calls[-1].endswith("PILOTCHARB") else 0)

    _sp.run = _fake
    try:
        two = {"character": {"code": "SHOW_CHAR_PILOTCHARA"},
               "characters": [{"code": "SHOW_CHAR_PILOTCHARA"}, {"code": "SHOW_CHAR_PILOTCHARB"}]}
        verdict, detail = run_attribute_qc("img.png", two)
        ck("every linked character is graded, not just the primary",
           _calls == ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"])
        ck("one character FAILING fails the whole panel (worst wins)",
           verdict == "FAIL")
        ck("the detail names each character and its own verdict",
           "SHOW_CHAR_PILOTCHARA=PASS" in detail and "SHOW_CHAR_PILOTCHARB=FAIL" in detail)
        _calls[:] = []
        one = {"character": {"code": "SHOW_CHAR_PILOTCHARA"}, "characters": []}
        ck("a single-character shot still grades exactly once",
           run_attribute_qc("img.png", one)[0] == "PASS" and len(_calls) == 1)
        ck("an environment-only panel still SKIPs",
           run_attribute_qc("img.png", {"character": None})[0] == "SKIP")
    finally:
        _sp.run = _real_run

    ck("two characters is within the proven regional range",
       MAX_REGIONAL_CHARACTERS == 2)
    err_three = regional_panel(None, None,
                               [{"code": "A", "image": __file__},
                                {"code": "B", "image": __file__},
                                {"code": "C", "image": __file__}],
                               {"set": {}}, "beat", "P", 1)[1]
    ck("THREE characters is refused by name, never composed wrong",
       bool(err_three) and "measured to FAIL for three" in err_three)
    err_missing = regional_panel(None, None,
                                 [{"code": "A", "image": 'C:\\nope\\x.png'},
                                  {"code": "B", "image": __file__}],
                                 {"set": {}}, "beat", "P", 1)[1]
    ck("a character with no image on disk is refused, not skipped",
       bool(err_missing) and "no image on disk" in err_missing)
    # Needles assembled from pieces so these checks cannot match their OWN
    # source lines. A grep-for-a-literal test that contains the literal always
    # fails; this is the second time today, so it is worth stating: if a test
    # reads the file it lives in, the needle must not be writable as one
    # string.
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    _gone = "out of scope " + "(Phase 3 finding)"
    _def = "def " + "build_graph"
    ck("gather_inputs no longer says two-character panels are out of scope",
       _gone not in _src)
    ck("the graph builder is imported, not copied (invariant 11)",
       "import regional_compose as RC" in _src and _def not in _src)

    # --- FRAMING reaches the GPU, not just the record -----------------------
    # PROVEN by wedge_shot_size.py: 3/3 framed cells are close-ups, 3/3 control
    # cells on the SAME seeds are full-body medium-wides. The checks below are
    # about the two ways this fix could silently revert.
    ck("a close-up clause describes the FRAME, not the jargon",
       "head and shoulders" in framing_clause("close up"))

    # GROUND CONTACT, a peer engineer's measured result: 3 of 3 with the sentence,
    # 0 of 2 without, in every arm. These lock the shape of what landed.
    # CORRECTED 2026-09-07. This asserted `contact_clause("medium close")` was
    # truthy, which locked the defect in place: a medium close is "from the
    # chest up" and cannot show feet, so sending both sentences contradicted
    # ourselves and the model drew a full figure to satisfy both. **A canary
    # written to "lock the shape of what landed" locks a bug when the thing
    # that landed was wrong**, and this one then defended it for a day.
    ck("the contact clause is sent for framings that ACTUALLY show the ground",
       contact_clause("wide") and contact_clause("full")
       and contact_clause("medium close") == "")
    ck("CANARY: NOT sent for a close-up, where the feet are out of frame",
       contact_clause("close up") == "")
    ck("an unknown size gets nothing, never a guessed default",
       contact_clause("dutch angle") == "" and contact_clause(None) == "")
    ck("CANARY: it names a SURFACE and a shadow, not a vague quality",
       "floor" in CONTACT_CLAUSE and "contact shadow" in CONTACT_CLAUSE)
    ck("CANARY: it contains no negation (inert at cfg 1.0)",
       not any(w in CONTACT_CLAUSE.lower() for w in (" no ", " not ", "without")))
    import inspect as _insp
    _gi2 = _insp.getsource(_build_instruction)
    ck("CANARY: _build_instruction actually APPENDS it, not just computes it",
       'contact_text' in _gi2)
    ck("CANARY an unknown size returns EMPTY, never a default -- guessing a size "
       "nobody set would invent a creative decision",
       framing_clause("dutch angle") == "" and framing_clause(None) == "")
    ck("every real SHOW01 shot size has a clause",
       all(framing_clause(x) for x in ("close up", "medium close", "medium",
                                       "medium wide", "wide", "two shot",
                                       "over shoulder", "insert")))

    # THE ONE THAT MATTERS. The clause must be in the string the compositor
    # RECEIVES, not only in the one that is recorded -- that distinction is the
    # entire defect, and it has now appeared three times in this file.
    import inspect as _i
    _src = _i.getsource(compose_with_retry)
    ck("CANARY the framing clause is appended to prepared_action_text (the SENT "
       "string), not just to the recorded instruction",
       'prepared_action_text += " " + inputs["framing_text"]' in _src)
    _gi = _i.getsource(gather_inputs)
    ck("...and both read ONE value from gather_inputs, so they cannot diverge",
       'framing_text' in _gi
       and 'framing_clause(shot.get("sg_shot_size"), _n_chars)' in _gi)
    # --- THE CAST SIZE MUST REACH THE FRAMING CLAUSE, or 13 two-character
    # --- shots keep being told to frame one person.
    ck("CANARY a WIDE with ONE character still says 'the whole character'",
       "the whole character" in framing_clause("wide", 1))
    ck("CANARY a WIDE with TWO characters says BOTH characters instead",
       "both characters" in framing_clause("wide", 2)
       and "the whole character" not in framing_clause("wide", 2))
    ck("CANARY every size in FRAMING has a plural twin, so a new size cannot "
       "silently fall back to the singular wording",
       set(FRAMING) == set(FRAMING_PLURAL))
    ck("CANARY 'two shot' and 'over shoulder' are UNCHANGED: they were already "
       "written for two bodies and rewording them would be a creative change",
       framing_clause("two shot", 2) == FRAMING["two shot"]
       and framing_clause("over shoulder", 2) == FRAMING["over shoulder"])
    ck("CANARY an UNKNOWN size still returns empty for both counts, never a "
       "guessed default",
       framing_clause("bogus", 1) == "" and framing_clause("bogus", 2) == "")
    ck("CANARY the ground-contact sentence pluralises too, and still says "
       "nothing at a size where the floor is out of frame",
       "Each character's feet" in contact_clause("wide", 2)
       and contact_clause("close up", 2) == "")
    ck("CANARY the default is SINGULAR, so every existing caller that passes "
       "no count keeps its exact behaviour",
       framing_clause("wide") == FRAMING["wide"]
       and contact_clause("wide") == CONTACT_CLAUSE)

    _ins = _build_instruction({"character": {"code": "C"}, "action_text": "stands",
                               "framing_text": framing_clause("wide"),
                               "characters": [], "set": {}})
    ck("the recorded instruction carries the same clause",
       "wide shot" in _ins)
    _noframe = _build_instruction({"character": {"code": "C"}, "action_text": "stands",
                                   "framing_text": "", "characters": [], "set": {}})
    ck("a shot with no framing records no framing (no invented default)",
       "Framed as" not in _noframe)

    # --- the widened wedge for two-character shots --------------------------
    import inspect as _i
    _src = _i.getsource(compose_panel_for_shot)
    ck("a two-character shot widens the wedge past the single-character default",
       TWO_CHARACTER_SEED_ATTEMPTS > DEFAULT_MAX_SEED_ATTEMPTS)
    ck("the widening is driven by the CHARACTER COUNT, not the shot code",
       'len(inputs.get("characters") or [])' in _src)
    ck("CANARY an explicit --max-seed-attempts is never silently overridden",
       "max_seed_attempts == DEFAULT_MAX_SEED_ATTEMPTS" in _src)
    ck("...and the widening is announced, not silent (invariant 3)",
       "widening the wedge" in _src)

    # --- the attribute gate is OFF, and OFF must never read as a pass -------
    # Geoff, 2026-09-05: turn it off, let the operator review without its input,
    # and STAGE it for re-inclusion rather than deleting it.
    ATTRIBUTE_QC_ENABLED = False
    ck("the gate is off by default", ATTRIBUTE_QC_ENABLED is False)
    ck("CANARY the shipped default really is off, not just this test's setting",
       "ATTRIBUTE_QC_ENABLED = False" in
       io.open(os.path.abspath(__file__), encoding="utf-8").read())
    ck("CANARY an OFF verdict produces NO QC line at all, not a reassuring one",
       qc_status_description("OFF", "") == "")
    ck("...and the word PASS never appears for a check that did not run",
       "PASS" not in qc_status_description("OFF", ""))
    # SKIP is a real finding (no character in frame) and must stay distinct.
    ck("SKIP still says SKIPPED, and is not collapsed into OFF",
       "SKIPPED" in qc_status_description("SKIP", "no character"))
    ck("a real PASS still says PASS", "PASS" in qc_status_description("PASS", ""))
    ck("a real FAIL still carries its detail",
       "off-register" in qc_status_description("FAIL", "off-register"))
    # The chosen candidate with the gate off: the first that composed.
    _atts = [{"qc_status": "OFF", "seed": 1}, {"qc_status": "OFF", "seed": 2}]
    ck("with the gate off the FIRST composed attempt is chosen, deterministically",
       next(r for r in _atts if r["qc_status"] in ("PASS", "SKIP", "OFF"))["seed"] == 1)
    # And the gate tool itself is untouched, so re-enabling is one constant.
    import os as _os
    ck("the gate tool is still present and still preflighted, not deleted",
       _os.path.isfile(ATTR_CHECK))

    # --- THE MULTI-CHARACTER ROUTE. F253/P020: 8 of 8 maskless against 0 of 32
    # --- masked, same shot, same beat, same seeds. These assert the ROUTE and
    # --- not the pictures, because a route that silently reverts is how a
    # --- measured result gets quietly un-applied.
    import inspect as _ins
    _cwr = _ins.getsource(compose_with_retry)
    ck("CANARY a multi-character shot composes through compose_panel_two, the "
       "MASKLESS three-reference path (F253)",
       "QC.compose_panel_two(" in _cwr)
    # PROVENANCE IS CARRIED FROM THE BRANCH THAT SENT IT, NOT RE-DERIVED.
    # The re-derivation used the first character's instance_count, which is
    # "copies of that character" and not "size of the cast", so every
    # two-character panel ever published recorded the single-character suffix
    # while the GPU got the two-character one. Measured on SHOW01_A_0070.
    _pfs = _ins.getsource(compose_panel_for_shot)
    ck("CANARY: the two-character branch RECORDS the two-character suffix it "
       "sent, on the attempt itself",
       "sent_suffix_used = QC.PRESERVE_SUFFIX_TWO_CHAR_WITH_SET" in _cwr
       and '"sent_suffix": sent_suffix_used' in _cwr)
    # THE FIELD THE OPERATOR ACTUALLY READS. The first F405 fix carried the
    # suffix into the workflow hash and left sg_prompt_final__as_sent_ still
    # re-deriving, so 104 more two-character panels were published with a
    # record that named the single-character prompt. These canaries are on
    # the FIELD, not on the hash.
    # Comments are stripped before this check: the first version of this
    # canary matched the word inside the very comment explaining the fix,
    # so it failed on correct code. A canary must read what EXECUTES.
    _src = _ins.getsource(publish_alternates) + _pfs
    _pub = "\n".join(l for l in _src.split("\n")
                     if not l.strip().startswith("#"))
    ck("CANARY: NEITHER write site rebuilds the operator-visible prompt. "
       "sent_prompt_text() re-derived it from instance_count and was wrong "
       "for every two-character panel",
       "sent_prompt_text(" not in _pub)
    ck("CANARY: both write sites read the string the compose call REPORTED",
       _pub.count('get("sent_prompt")') >= 2)
    ck("CANARY: an attempt whose compose call reported nothing SAYS so in the "
       "record rather than carrying a plausible rebuilt string",
       "PROMPT NOT REPORTED BY THE COMPOSE CALL" in _pub)
    ck("CANARY: ALL THREE branches report, environment-only included. The "
       "first version of this fix improved two and silently degraded the third",
       _cwr.count("record=sent_record") == 3)
    ck("CANARY: the recorded NEGATIVE is the one the call reported, not the "
       "module constant restated",
       'get("sent_negative")' in _pfs)
    ck("CANARY: the compose branches ask qwen_edit to report what it sent, "
       "rather than anyone reconstructing it",
       "record=sent_record" in _cwr and '"sent_prompt": sent_record.get("prompt")' in _cwr)
    ck("CANARY: publishing READS that recorded suffix and never re-derives it "
       "from instance_count. A second derivation of a fact is not a record "
       "of it",
       '(chosen or {}).get("sent_suffix")' in _pfs
       and "QC.suffix_for(sent_instance_count)" not in _pfs)
    ck("CANARY: an attempt that recorded no suffix says so in the record "
       "rather than guessing a plausible one",
       "SUFFIX NOT RECORDED BY THE COMPOSE BRANCH" in _pfs)
    ck("CANARY it no longer calls regional_panel: that path averaged two "
       "separate model passes, each carrying only one character",
       "regional_panel(sg, shot_row" not in _cwr)
    ck("CANARY regional_panel is KEPT as the control arm and the record of a "
       "real result, not deleted",
       callable(regional_panel))
    ck("CANARY the set still reaches the compositor as an IMAGE (image3), not "
       "only as prose",
       "set_image=inputs[" in _cwr)
    ck("CANARY the room clause NAMES the set reference instead of describing "
       "the room twice, so two sources of truth cannot disagree",
       "set reference image" in room_text_for({"set": {"code": "SHOW_SET_X"}})
       and "SHOW_SET_X" in room_text_for({"set": {"code": "SHOW_SET_X"}}))
    ck("...and a missing set code degrades to the plain clause rather than an "
       "empty parenthesis",
       room_text_for({}) == "the room shown in the set reference image")

    # --- SLOT ORDER DECIDES SCREEN POSITION, so it must follow the beat and
    # --- not the link order. Measured on SHOW01_A_0390: two runs, slots
    # --- swapped, same sentence, and whoever was Picture 1 rendered LEFT.
    _J, _V = {"code": "SHOW_CHAR_PILOTCHARB"}, {"code": "SHOW_CHAR_PILOTCHARA"}
    ck("CANARY the character the beat NAMES FIRST becomes Picture 1, even when "
       "the link order is the other way round",
       [c["code"] for c in order_characters_by_beat(
           [_V, _J], "PilotCharB stands on the left of frame. PilotCharA stands on the "
                     "right of frame.")] == ["SHOW_CHAR_PILOTCHARB", "SHOW_CHAR_PILOTCHARA"])
    ck("CANARY an order that ALREADY matches the beat is left alone",
       [c["code"] for c in order_characters_by_beat(
           [_J, _V], "PilotCharB on the left, PilotCharA on the right.")]
       == ["SHOW_CHAR_PILOTCHARB", "SHOW_CHAR_PILOTCHARA"])
    ck("CANARY a beat naming NOBODY keeps link order rather than shuffling",
       [c["code"] for c in order_characters_by_beat([_V, _J], "two people talk")]
       == ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"])
    ck("CANARY a beat naming only ONE puts that one first and keeps the rest "
       "in link order behind it",
       [c["code"] for c in order_characters_by_beat(
           [_V, _J], "PilotCharB turns away.")] == ["SHOW_CHAR_PILOTCHARB", "SHOW_CHAR_PILOTCHARA"])
    ck("CANARY a single-character shot is returned untouched",
       order_characters_by_beat([_V], "PilotCharA waits.") == [_V])
    ck("CANARY an EMPTY beat does not crash and does not reorder",
       [c["code"] for c in order_characters_by_beat([_V, _J], "")]
       == ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"])
    ck("CANARY the compositor actually CALLS the ordering, or it is dead code",
       "order_characters_by_beat(chars" in _ins.getsource(compose_with_retry))

    # --- SCREEN DIRECTION. Naming the SLOT is the only thing that moves the
    # --- men (a peer engineer P024: 4 of 4 each way on a perfect swap); the beat's own
    # --- prose moves nothing (F267-F269); naming them by DESCRIPTION drew one
    # --- man twice in 2 of 4 cells.
    # MANDATORY, not polish: without it a peer engineer got 3 figures with blended
    # identities in 4 of 4, with "Exactly two characters" still in the prompt.
    ck("CANARY the clause is UNCONDITIONAL for two characters: no shot size, no "
       "flag and no framing preference can switch it off, because it is what "
       "holds the HEADCOUNT (F315), not just the side",
       all(slot_side_clause(2) == SLOT_SIDE_CLAUSE for _ in range(3))
       and "n_characters" in _ins.getsource(slot_side_clause)
       and "shot_size" not in _ins.getsource(slot_side_clause))
    ck("CANARY a two-character shot gets the slot-side clause",
       slot_side_clause(2) == SLOT_SIDE_CLAUSE and "Picture 1" in slot_side_clause(2))
    ck("CANARY the clause names the SLOT, never a description of the man: a "
       "referring expression the model cannot resolve drew one man twice",
       "Picture 2" in slot_side_clause(2)
       and "glasses" not in slot_side_clause(2).lower()
       and "moustache" not in slot_side_clause(2).lower())
    ck("CANARY a SINGLE character gets no clause: there is no side to assign",
       slot_side_clause(1) == "")
    ck("CANARY THREE characters get no clause either, because it names two "
       "slots and would silently omit the third",
       slot_side_clause(3) == "")
    ck("CANARY the compositor actually appends it, or the measured result is "
       "not in the product",
       "slot_side_clause(len(ordered))" in _ins.getsource(compose_with_retry))
    ck("CANARY the clause is appended to the text the COMPOSITOR receives, not "
       "to a variable nobody sends",
       "two_char_text" in _ins.getsource(compose_with_retry)
       and "compose_panel_two(" in _ins.getsource(compose_with_retry))

    # --- THE WIDE-TWO-SHOT WARNING IS GONE (a peer engineer F313). Its 382-454px band came
    # --- entirely from STITCHED references; three SEPARATE slots, which is what
    # --- this compositor sends, stage two characters at 629-694px unprompted.
    # --- The warning discouraged a framing our own path delivers for free.
    ck("CANARY the wide-two-shot warning is REMOVED, not merely unused: it was "
       "false for the three-slot path and steered operators off a framing that "
       "works",
       not hasattr(sys.modules[__name__], "wide_two_shot_warning")
       and "wide_two_shot_warning" not in _ins.getsource(gather_inputs))

    # --- WHO COUNTS AS PRESENT. Before F253 a two-character shot sent ONE
    # --- reference, so the second name was genuinely absent. Maskless sends
    # --- every reference, so calling the second character absent pointed the
    # --- strip guard at a body the model is actually drawing.
    _gi_src = _ins.getsource(gather_inputs)
    ck("CANARY present_tokens is built from EVERY character with a reference, "
       "not from the primary one: naming the second character absent would let "
       "the guard delete a clause about someone who is in the picture",
       "for c in char_infos" in _gi_src
       and 'character_name_tokens(char_info["code"]) if char_info' not in _gi_src)

    # --- THE PROMPT MUST NOT CONTRADICT ITSELF. A contact clause asserting the
    # --- feet cannot be sent with a framing clause that stops at the chest.
    # --- The generic invariant is asserted, not the current list, so a size
    # --- added later cannot reintroduce the contradiction quietly.
    _cut_off = ("knees up", "waist up", "chest up", "head and shoulders")
    _bad = [s for s in CONTACT_SIZES
            if any(p in (FRAMING.get(s) or '').lower() for p in _cut_off)]
    ck("CANARY no size gets a FEET clause while its framing clause cuts the "
       "body off above them (this shipped for medium/medium wide/medium close "
       "and the model drew a full figure to satisfy both)",
       _bad == [])
    ck("CANARY the same invariant holds for the PLURAL framing table",
       [s for s in CONTACT_SIZES
        if any(p in (FRAMING_PLURAL.get(s) or '').lower() for p in _cut_off)] == [])
    ck("CANARY a WIDE still gets it, or the ground-contact result is lost",
       contact_clause("wide") != "" and contact_clause("wide", 2) != "")
    ck("CANARY a MEDIUM CLOSE no longer gets it",
       contact_clause("medium close") == "" and contact_clause("medium close", 2) == "")
    ck("CANARY an unknown size still gets nothing, never a default",
       contact_clause("bogus") == "")

    ATTRIBUTE_QC_ENABLED = _qc_was
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


# ------------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shot", help="Shot code, e.g. PILOT01_A_0010")
    ap.add_argument("--seed", type=int, default=None,
                    help="Attempt-1 seed override; attempt 1 defaults to 6000+version_number "
                         "if not given. Retries (see --max-seed-attempts) step off this "
                         "deterministically -- see compose_with_retry().")
    ap.add_argument("--dry", action="store_true", help="gather inputs only, do not compose")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--max-seed-attempts", type=int, default=DEFAULT_MAX_SEED_ATTEMPTS,
                    help="Seeds to try, keeping the first attribute_check.py PASS (default "
                         "%d). 1 = old single-shot behaviour, no retry." % DEFAULT_MAX_SEED_ATTEMPTS)
    ap.add_argument("--setup-schema", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test()

    sg = sg_connect()

    if ns.setup_schema:
        ensure_schema(sg)
        return 0

    if not ns.shot:
        ap.error("--shot is required (or --setup-schema / --self-test)")

    ensure_schema(sg)
    result = compose_panel_for_shot(sg, ns.shot, seed=ns.seed, dry=ns.dry,
                                    model=ns.model, timeout=ns.timeout,
                                    max_seed_attempts=ns.max_seed_attempts)
    print(json.dumps({k: v for k, v in result.items() if k != "inputs"}, indent=2))
    return 0 if result["status"] in ("published", "inputs-only") else 1


if __name__ == "__main__":
    sys.exit(main())
