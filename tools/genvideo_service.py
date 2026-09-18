#!/usr/bin/env python3
"""The standing service. ShotGrid state is the ONLY trigger for anything.

Geoff, 2026-08-27: "Don't regenerate with side methods, use shotgrid methods to
produce the shots."

He is right, and it is an architectural point, not a preference. Up to now the
pipeline reacted to ShotGrid state but a human (me) kept starting the reaction by
running a script. That makes the scripts the interface and ShotGrid a database.
Inverted here: this service runs continuously, and the ONLY way to make anything
happen is to change a field in ShotGrid.

    flip Shot.sg_gen_status to queued            -> a clip is generated
    leave a Note or set Version to rrq           -> a revision is generated
    set Version to an approved status            -> the shot closes
    flip Sequence.sg_gen_status to queued        -> the sequence assembles
    flip Sequence.sg_gen_status to animatic_requested -> that sequence's
                                                     animatic is cut (on request
                                                     only, MASTER-PLAN-V2 D8 -
                                                     see PHASE 9 below)
    edit Sequence.sg_style_prefix                -> the next generation restyles
    approve an Asset's design Version            -> Asset.sg_approved_design is
                                                    recorded, sg_stage=approved
                                                    (PHASE 10)
    set an Asset design Version to rrq + a Note  -> a revision to that ASSET's
                                                    design prompt is proposed AND
                                                    (SHOW01, default on) APPLIED
                                                    automatically -- Geoff,
                                                    2026-09-03: "GPU is local
                                                    and free, just run the
                                                    prompt automatically" -- and
                                                    a re-render is queued
                                                    (PHASE 10 + 11). The gate is
                                                    what the render PRODUCES, at
                                                    'rev', never the prompt text.
    flip a Panel Task's status to 'rdy'          -> that shot's panel is
                                                    composed and published at
                                                    'rev' (PHASE 12)

PHASE 9 / D8, 2026-08-29: the animatic is demoted from spine to an on-request
reference artifact with NO approval gate. Approving a board (or anything else)
must never, by itself, cause a cut. The only thing that causes a cut is a
human or agent explicitly setting Sequence.sg_gen_status = animatic_requested
(see the PHASE 9 block below). The full-episode stitch (`animatic.py
--episode`) lost its service-side auto-trigger entirely in this pass: there is
no ShotGrid entity that naturally carries "the whole episode was requested"
(no per-episode Sequence/Project field existed, and inventing one was more
schema surface than a demoted, deferred feature warranted), so it is run by
hand/CLI now, same as any other on-request tool in this codebase.

Nothing else. No scripts to run, no arguments to remember. If a thing cannot be
caused by editing ShotGrid, it is not part of the pipeline yet, and that is the
honest test of whether the interface is real.

ShotGrid webhooks would let ShotGrid push instead of us polling, which is the
better shape and needs a reachable HTTPS endpoint this box does not have (port
8188 is LAN-scoped and 22 is the maintenance route). Polling is the same
behaviour with a latency floor of one cycle, and it needs nothing exposed.

Usage:
    python genvideo_service.py                 run forever, 60s cycle
    python genvideo_service.py --interval 30   faster cycle
    python genvideo_service.py --once          a single cycle (for testing)
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import time

PY = sys.executable
ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): this used to be a second, independent
# guess at "where do my siblings live" -- os.path.join(ROOT, "build", "tools"),
# duplicating (and, until today, disagreeing with) the deployed location that
# tools/deploy.py actually stages releases into. TOOLS now names wherever THIS
# file itself was executed from, so it is automatically correct for whichever
# release the deploy seam has made current, with nothing to keep in sync.
# prompt_revision.py and video_from_panel.py already did it this way; this
# brings genvideo_service.py, panel_compose.py and script_to_beats.py into
# line with them (invariant 11: one implementation, not three that can drift).
TOOLS = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(TOOLS, "genvideo_worker.py")
VIDEO_FROM_PANEL = os.path.join(TOOLS, "video_from_panel.py")   # PHASE 6
ASSEMBLE = os.path.join(TOOLS, "episode_assemble.py")
ANIMATIC = os.path.join(TOOLS, "animatic.py")   # PHASE 9: on-request only, see cycle()
ANIMA_ANCHOR = os.path.join(TOOLS, "anima_anchor.py")   # PHASE 11
PANEL_COMPOSE = os.path.join(TOOLS, "panel_compose.py")   # PHASE 12
import episode_context as _EPCTX  # noqa: E402
import episode_context as EPCTX  # noqa: E402
import sg_review_housekeeping as HOUSEKEEPING  # noqa: E402
import shot_lock as SHOT_LOCK  # noqa: E402  ROADMAP 0a -- protected-shot hard lock
# PHASE 4. Episode-scoped -- this file is READ AND WRITTEN by the standing
# service, so an un-scoped path meant two episodes sharing one bookkeeping
# file. Migrates PILOT01's legacy un-scoped file on first use by COPY, so a
# running service is never left pointing at a file that moved.
BEATS_JSON = _EPCTX.migrate_legacy_beats()
PROJECT_ID = 9999
PROJ = {"type": "Project", "id": PROJECT_ID}


def sg_connect():
    import shotgun_api3
    for name in ("SHOTGRID_SITE_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY"):
        if not os.environ.get(name):
            sys.exit("FATAL: %s is not set. The key lives in the environment, never a file." % name)
    return shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                                script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                                api_key=os.environ["SHOTGRID_SCRIPT_KEY"])


def log(msg):
    print("[service] %s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def deploy_stamp_line():
    """W10 / E0 acceptance test: 'the service log's first line names the
    deployed SHA'. Reads the stamp tools/deploy.py writes into THIS SAME
    directory at deploy time (invariant 11: one implementation -- the stamp
    format and the code that reads it both live in deploy.py, this just calls
    it), so 'what is running?' is answerable from the log instead of by
    diffing three trees (MASTER-PLAN-V3 3.2). Never raises: a missing or
    unreadable stamp is reported LOUDLY rather than silently -- that absence
    is itself the signal that this tree was placed by hand instead of by
    tools/deploy.py."""
    try:
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import deploy as _deploy
        return _deploy.describe_stamp(_deploy.read_stamp(TOOLS))
    except Exception as exc:
        return ("DEPLOY STAMP: UNAVAILABLE (%s: %s) -- this tree may have been "
                "placed by hand instead of tools/deploy.py" % (type(exc).__name__, exc))


def tool_report(r):
    """-> the JSON report a tool printed as its last stdout block, or None.

    Every GPU tool here ends its run by printing a structured report. Reading it
    is always better than scraping a line: it carries a machine-readable
    `status` AND a human `reason`, and both were being thrown away."""
    blob = ((r.stdout or "") if r else "").strip()
    if not blob.endswith("}"):
        return None
    start = blob.rfind(chr(10) + "{")
    try:
        doc = json.loads(blob[start + 1:] if start >= 0 else blob)
    except Exception:                                             # noqa: BLE001
        return None
    return doc if isinstance(doc, dict) else None


def failure_reason(r, fallback="the tool refused or failed"):
    """-> the best available one-line reason a tool failed.

    ONE implementation, used by run() and by every watcher that reports a
    child's failure (invariant 11). There were two scrapers: run() was fixed to
    read the report and watch_panel_composition() was not, so the SAME failure
    logged its real cause on one line and a bare "}" on the next:

        SUBPROCESS FAILED rc=1 (panel_compose.py --shot SHOW01_A_0490):
            ComfyUI is not reachable at 127.0.0.1:8188
        SHOW01_A_0490 -> REFUSED/FAILED (Task -> hld): }

    and it was the second line that got written into a ShotGrid Note, where the
    operator would actually read it."""
    doc = tool_report(r)
    if doc:
        for k in ("reason", "error", "message"):
            if doc.get(k):
                return str(doc[k])
    err = [l for l in ((r.stderr or "") if r else "").splitlines() if l.strip()]
    if err:
        return err[-1]
    out = [l for l in ((r.stdout or "") if r else "").splitlines() if l.strip()]
    return out[-1] if out else fallback


# Statuses panel_compose reports for a failure that is NOT about this shot.
# From its own docstring: 'refused' means PanelInputError (this shot's inputs
# are wrong and no retry will help); 'compose-failed' means the compositor or
# the GPU was unreachable, which is true of EVERY shot in the queue.
INFRA_FAIL_STATUSES = ("compose-failed",)

# A CONTENT refusal wears the same status as an outage, and that cost the queue.
#
# MEASURED 2026-09-07: SHOW01_A_0170's beat could not be split ("action for PILOTCHARB
# refers to someone else"), panel_compose reported `compose-failed`, and the
# service read that as an OUTAGE. So it handed the Task back to 'rdy' and STOPPED
# THE PASS. The shot was retried five times in twenty minutes, could never
# succeed, and **six other queued shots sat untouched behind it** with the log
# cheerfully saying they would "compose when the compositor is back". Nothing was
# down. One unsplittable beat had halted the whole queue, permanently and
# silently.
#
# `compose-failed` genuinely covers both cases, so the status alone cannot decide.
# These markers name the failures that are about THIS SHOT'S CONTENT and will
# recur identically forever, no matter how healthy the infrastructure is.
# ONLY THE FLAKY ONES. An existing canary caught me widening this too far: I had
# included "no approved design", which is NOT flaky, it is permanent until a
# human approves something. Retrying it forever is waste and 'hld' is the honest
# state, so it must keep taking the per-shot branch.
#
# What belongs here is a failure that is (a) about this shot, so it must not stop
# the pass, and (b) genuinely non-deterministic, so it deserves another attempt.
# The beat split is an LLM call, which is both.
CONTENT_FAIL_MARKERS = (
    "beat split failed",
    "refusing to compose a multi-character panel",
)


def is_content_failure(r):
    """-> True if this failure is about THIS SHOT'S content, not the machine.

    MEASURED 2026-09-07 and it corrects my own first fix. SHOW01_A_0170's beat
    split failed SEVEN times and succeeded on the EIGHTH: it is an LLM call, so
    it is flaky rather than permanently broken. My first patch held the shot on
    the first content failure, which would have discarded that success. This
    classifier now only decides WHICH BRANCH the failure takes, never whether it
    is retried.

    The distinction that matters: an outage will hit every remaining shot, so it
    must stop the pass. A bad beat will not, so it must not."""
    doc = tool_report(r)
    reason = str((doc or {}).get("reason") or "").lower()
    blob = (((r.stdout or "") + (r.stderr or "")) if r else "").lower()
    return any(m in reason or m in blob for m in CONTENT_FAIL_MARKERS)


def is_infra_failure(r):
    """-> True if this failure will hit every other queued shot identically.

    MEASURED 2026-09-04, and it cost a whole batch. video_from_panel.py tears
    ComfyUI down after a video (invariant 4, Geoff's standing rule that ComfyUI
    is closed after every GPU batch). The next panel-composition pass then found
    15 queued shots and no ComfyUI, and burned the ENTIRE QUEUE in thirty
    seconds: each shot attempted once, failed with "ComfyUI is not reachable",
    and had its Panel Task set to 'hld' with a refusal Note posted. Fifteen
    shots, fifteen Notes, none of them about the shot.

    A per-shot verdict is only honest for a per-shot cause. An outage that will
    hit every remaining item must stop the pass and leave the queue intact, so
    the work resumes by itself when the infrastructure comes back rather than
    needing an operator to reset fifteen Task statuses by hand."""
    doc = tool_report(r)
    # A CONTENT refusal is never infra, whatever status it carried. Checked
    # FIRST, because it is the narrower and more certain judgement: an outage
    # cannot make a beat splittable, so a bad beat must not take the branch that
    # STOPS the pass and strands every shot behind it.
    if is_content_failure(r):
        return False
    if doc and doc.get("status") in INFRA_FAIL_STATUSES:
        return True
    # Belt and braces for a tool that died before printing its report at all.
    blob = (((r.stdout or "") + (r.stderr or "")) if r else "").lower()
    return "not reachable at 127.0.0.1:8188" in blob


def ensure_comfy(log=log):
    """-> True if ComfyUI is up, starting it if needed.

    IMPORTED from genvideo_worker rather than reimplemented: this file already
    has four near-copies of comfy_up/comfy_start across the tools directory and
    a fifth would be the one that drifts (invariant 11). genvideo_worker's is
    the copy the others say they mirror.

    panel_compose.py deliberately does not start ComfyUI ("this module does not
    start it") -- which is correct for a tool, and leaves the gap to whoever
    dispatches it. That was nobody."""
    try:
        import genvideo_worker as _W
        if _W.comfy_up():
            return True
        log("  ComfyUI is down -- starting it before dispatching GPU work")
        return bool(_W.comfy_start())
    except Exception as exc:                                      # noqa: BLE001
        log("  could not ensure ComfyUI is up (%s: %s) -- dispatching anyway; "
            "the tool will refuse loudly if it cannot reach it"
            % (type(exc).__name__, str(exc)[:120]))
        return True


def run(cmd, timeout=14400, label=""):
    """FAILSAFE_RC: a subprocess that dies must be LOUD. The first version of
    this service ran the boards pass every cycle while the child crashed on a
    schema error, and reported "boards pass: 47 shots" each time - indistinguishable
    from healthy work. A silent non-zero return is the same failure this pipeline
    keeps guarding against, so it is surfaced here.

    E2E-DEFECT-FIX (defect 2): the diagnostic line used to be simply the LAST
    line of stderr+stdout concatenated. That is misleading whenever a child
    does real cleanup (a `finally:` teardown) AFTER an exception has already
    been raised: stdout's last line ends up being the teardown's own routine
    "done" message, printed AFTER the traceback that actually explains the
    failure. Live proof: video_from_panel.py crashed on a real bug immediately
    after a successful publish; the finally: block's ComfyUI teardown ran
    (and logged "teardown: VERIFIED") as the exception continued propagating,
    and THIS function reported "SUBPROCESS FAILED rc=1 (video_from_panel.py):
    ... teardown: VERIFIED" -- reading exactly as though teardown were the
    failure, when the failure was an unrelated TypeError several lines
    earlier. Prefer stderr's own last line (where an unhandled Python
    exception's summary actually lands) when there is one; fall back to
    stdout only when stderr is empty."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log("  TIMEOUT after %ss: %s" % (timeout, label or os.path.basename(cmd[1])))
        return None
    if r.returncode != 0:
        stderr_lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        stdout_lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
        # PREFER THE STRUCTURED REASON OVER ANY SCRAPED LINE.
        #
        # These tools end their run by printing a JSON report, so the "last
        # line" heuristic below reported the closing brace: measured
        # 2026-09-04, "SUBPROCESS FAILED rc=1 (panel_compose.py --shot
        # SHOW01_A_0160): }". The actual reason -- "ComfyUI is not reachable
        # at 127.0.0.1:8188" -- was sitting in that same JSON, in a field
        # named `reason`, already parsed and thrown away. An operator reading
        # the service log learned only that something failed.
        #
        # This is the second time scraping a line has misreported a failure
        # (see the teardown case below). Read the report when there is one.
        diag = failure_reason(r, fallback="no output")
        log("  SUBPROCESS FAILED rc=%d (%s): %s"
            % (r.returncode, label or os.path.basename(cmd[1]), diag[:220]))
    return r


# ============================================================================
# PHASE 4 -- STORY BEAT STALENESS WATCHER (owned by the Phase 4 builder; see
# script_to_beats.py for the parser/linker this depends on). Clearly delimited
# per the wave's file-ownership rule: everything between this banner and
# "END PHASE 4" is this phase's block.
#
# ShotGrid state is the only trigger (invariant 1): a human editing
# Shot.sg_script_beat directly in ShotGrid IS "editing a Beat". There is no
# fresh CustomEntity for Story Beat yet (Geoff gate not enabled as of
# 2026-08-28 -- see script_to_beats.py docstring for the probe evidence), so
# the plan's fallback applies: the beat's text lives ON the shot, and
# build/out/beats.json records the text script_to_beats.py last SYNCED to each
# shot. A live value that no longer matches that record is a beat edit the
# downstream stage has not seen yet.
#
# Defensive when the panel is absent (Phase 5 has not run: no
# Shot.sg_approved_panel field, no panel Version exists to compare a
# created_at against). The content diff above already IS the correct signal
# in that world -- a beat freshly edited is newer than a panel that does not
# exist. If/when Phase 5 adds sg_approved_panel, prefer comparing the beat's
# true source (a real Beat entity's updated_at, if Geoff has enabled it by
# then) against that Version's created_at instead; try it defensively and
# fall through to the content-diff signal on any Fault (missing field, no
# entity) rather than crashing the whole service cycle.
# ============================================================================

def _beats_doc_load(path=None):
    try:
        with open(path or BEATS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _beats_doc_save(doc, path=None):
    try:
        with open(path or BEATS_JSON, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except Exception as exc:
        log("  beats.json: could not persist bookkeeping: %s" % exc)


def watch_finishing(sg):
    """An APPROVED video with no FIN Version yet -> run Stage 13 upres. -> True if one ran.

    STAGE 13 IS THE HALF THE DRAFT STRATEGY WAS PREDICATED ON, and it had no
    caller at all. Geoff's plan, in his words: *"10 steps is fine, this can be a
    draft animatic, with video to video upres/detail later perhaps?"* The draft
    half shipped; the "later" half was written, tested, and wired into nothing,
    so the whole show was committed to 832x480 with no route out. That is F350's
    shape for the fourth time and it is why he kept seeing blur.

    THE TRIGGER IS A VERSION STATUS, NEVER A TASK. Geoff, 2026-09-08: *"nothing
    should need to watch task status, watch version status, task status is for
    production management to track high level progress."*

    ONE SHOT PER CYCLE, matching watch_panel_composition's rule and for the same
    reason: this is the most expensive pass in the pipeline (231s measured on 49
    frames) and draining a backlog in one pass would starve every other watcher.

    IT ONLY FINISHES APPROVED WORK. finishing.latest_video_version() enforces
    that itself now, and this passes no override. An unapproved clip may still
    be rejected, and spending the longest pass we have on it publishes a
    1920x1080 Version that reads like a deliverable for a shot nobody signed
    off."""
    sys.path.insert(0, TOOLS)
    import finishing as FIN
    try:
        vids = sg.find("Version",
                       [["project", "is", PROJ], ["sg_stage", "is", "video"],
                        ["sg_status_list", "in", list(FIN.APPROVED_VIDEO_STATUSES)]],
                       ["code", "entity", "created_at"])
    except Exception as exc:                                      # noqa: BLE001
        log("  finishing watcher: Version query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    shot_ids = set()
    for v in vids:
        e = v.get("entity") or {}
        if e.get("type") == "Shot" and e.get("id"):
            shot_ids.add(e["id"])
    if not shot_ids:
        return False
    # Already finished? A FIN Version on the shot means this pass has run. Read
    # it from ShotGrid rather than a local marker: a marker on the scratch disk
    # would re-run the most expensive pass in the pipeline after any clean.
    try:
        fins = sg.find("Version",
                       [["entity", "in", [{"type": "Shot", "id": i} for i in shot_ids]],
                        ["code", "contains", "_FIN_"]],
                       ["code", "entity", "created_at"])
    except Exception as exc:                                      # noqa: BLE001
        log("  finishing watcher: FIN lookup failed (%s), skipping" % type(exc).__name__)
        return False
    # A FIN IS ONLY "DONE" IF IT IS NEWER THAN THE VIDEO IT FINISHES. Keying on
    # "a FIN exists at all" means a shot whose picture changed keeps its old
    # finished clip forever: the panel recomposes, the video regenerates, and
    # the deliverable silently stays the previous take. It also means a bug in
    # the finishing pass can never heal, which is not hypothetical, the 2.6%
    # horizontal stretch fixed on 2026-09-08 is baked into every FIN published
    # before it and would have stayed there.
    newest_fin = {}
    for f in fins:
        sid = (f.get("entity") or {}).get("id")
        ts = f.get("created_at")
        if sid and (sid not in newest_fin or (ts and newest_fin[sid] and ts > newest_fin[sid])):
            newest_fin[sid] = ts
    newest_vid = {}
    for v in vids:
        sid = (v.get("entity") or {}).get("id")
        ts = v.get("created_at")
        if sid and (sid not in newest_vid or (ts and newest_vid[sid] and ts > newest_vid[sid])):
            newest_vid[sid] = ts
    done = set(sid for sid, ts in newest_fin.items()
               if ts and newest_vid.get(sid) and ts > newest_vid[sid])
    todo = sorted(shot_ids - done)
    if not todo:
        return False
    held = held_episode_ids(sg)
    for sid in todo:
        row = sg.find_one("Shot", [["id", "is", sid]], ["code", "sg_episode"])
        if not row:
            continue
        if (row.get("sg_episode") or {}).get("id") in held:
            continue
        log("finishing: %s has an approved video and no FIN yet -- upres to "
            "%dx%d (%d shot(s) waiting)"
            % (row["code"], FIN.TARGET_W, FIN.TARGET_H, len(todo)))
        try:
            FIN.cmd_finish(sg, row["code"], None, 1)
        except Exception as exc:                                  # noqa: BLE001
            log("  finishing: %s FAILED (%s: %s). Left for the next cycle."
                % (row["code"], type(exc).__name__, str(exc)[:160]))
        return True                    # ONE per cycle, see the docstring
    return False


def watch_beats_all_episodes(sg):
    """Run watch_beats() once PER EPISODE, explicitly, never on a module default.

    THE DEFECT THIS CLOSES (F357). BEATS_JSON is resolved once at import through
    episode_context, which honours GENVIDEO_EPISODE and otherwise falls back to
    the built-in default, PILOT01, the RETIRED episode. ops/run_service.ps1 copies
    only the three SHOTGRID_* variables and has never set GENVIDEO_EPISODE, and
    it is set in neither User nor Machine scope. So this watcher has spent the
    whole project comparing 121 shots of a dead show, and **all 55 SHOW01 shots
    were uncovered**: an operator editing a beat in ShotGrid got nothing.

    The evidence was conclusive rather than inferred: the string 'beat changed
    on' appears in NO service log, ever.

    This is watch_note_triage()'s fix applied one watcher over, and that
    function's docstring already stated the rule in as many words: *a standing
    service must not depend on an environment variable being set correctly by
    whoever launched it*. The rule was written down, in this file, and the
    watcher two hundred lines away still had the bug. Setting the env var in the
    launcher would have masked it while leaving every other module default
    pointed at the dead show."""
    import episode_context as _EC
    did = False
    for code in sorted(_EC.known_episode_codes()):
        try:
            if watch_beats(sg, beats_path=_EC.beats_path(code), episode=code):
                did = True
        except Exception as exc:                                  # noqa: BLE001
            log("  beat watcher[%s] error (continuing): %s: %s"
                % (code, type(exc).__name__, str(exc)[:120]))
    return did


def watch_beats(sg, beats_path=None, episode=None):
    """Beat text edited in ShotGrid since the last script_to_beats.py sync ->
    flag the dependent shot(s) stale (sg_stage=panel) + leave a Note. Returns
    True if any shot was flagged this cycle.

    HARDENING (PHASE-4-FIX1.md, defect 2): beats.json is a plain file a human
    can hand-edit, and a hand edit can be wrong in ways ShotGrid's own API
    responses never are. This function must degrade cleanly and LOUDLY
    (invariant 3) on a malformed document rather than throw -- the caller
    (cycle()) does catch any exception one level up and logs it, so this was
    never fatal to the service, but a targeted skip-and-log here is strictly
    better than relying on the outer catch. Three shapes are hardened
    against, each previously a crash:
      - `doc` not a dict at all (e.g. beats.json hand-edited into a JSON
        list) -- `doc.get(...)` would throw AttributeError.
      - `shot_sync[code]` present but `None` -- `.get("synced_text")` on it
        would throw AttributeError.
      - a Shot row ShotGrid returns with no `code` key -- `r["code"]` would
        throw KeyError.
    """
    doc = _beats_doc_load(beats_path)
    if not isinstance(doc, dict):
        if doc is not None:
            log("  beat watcher: beats.json root is a %s, not an object -- "
                "skipping this cycle" % type(doc).__name__)
        return False                      # script_to_beats.py has not run yet
    shot_sync = doc.get("shot_sync")
    if not isinstance(shot_sync, dict) or not shot_sync:
        return False
    codes = list(shot_sync)
    did = False
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        try:
            rows = sg.find("Shot", [["project", "is", PROJ], ["code", "in", chunk]],
                           ["code", "sg_script_beat", "sg_stage"])
        except Exception as exc:
            log("  beat watcher: Shot query failed (%s), skipping this batch"
                % type(exc).__name__)
            continue
        by_code = {}
        for r in rows:
            code = r.get("code")
            if not code:
                log("  beat watcher: Shot id=%s came back with no 'code' "
                    "field -- skipping that row" % r.get("id"))
                continue
            by_code[code] = r
        for code in chunk:
            r = by_code.get(code)
            if not r:
                continue
            entry = shot_sync.get(code)
            if not isinstance(entry, dict):
                log("  beat watcher: shot_sync[%s] in beats.json is %s, not "
                    "an object -- skipping that shot" % (code, type(entry).__name__))
                continue
            synced = entry.get("synced_text") or ""
            live = r.get("sg_script_beat") or ""
            if live == synced:
                continue                  # no edit since the last sync
            log("beat changed on %s: flagging stale (sg_stage -> panel)" % code)
            try:
                sg.update("Shot", r["id"], {"sg_stage": "panel"})
                # PIPELINE BOOKKEEPING IS NOT A NOTE. Geoff, 2026-09-07: "publishing should
                # not add notes. use description if something needs adding." A Note is a
                # request, or a human's record of review. The system announcing what it just
                # did is neither, and putting it in Notes filled the operator's review queue
                # with the pipeline talking to itself, which it then read back as work (F209:
                # 24 of 29 actionable notes were self-authored).
                #
                # The SIGNAL is the state field written just above, which the operator and
                # every watcher already read. Publish-time detail belongs on the VERSION
                # description, which is fresh and empty for every publish; Shot and Asset
                # descriptions are not in the publish path at all.
                pass
            except Exception as exc:
                log("  FAILED to flag %s stale: %s: %s"
                    % (code, type(exc).__name__, str(exc)[:160]))
                continue
            entry["synced_text"] = live   # do not re-fire next cycle
            did = True
    if did:
        # SAVE TO THE FILE WE READ. Without the path this writes the MODULE
        # DEFAULT's file, so sweeping every episode would pour each one's
        # bookkeeping into the retired episode's sidecar and mark all 55 live
        # shots as already-synced against beats they never had. The read was
        # scoped and the write was not, which is the same one-sided seam as
        # every other bug in this file's history.
        _beats_doc_save(doc, beats_path)
    return did

# ============================================================================
# END PHASE 4
# ============================================================================


# ============================================================================
# PHASE 7 -- LLM PROMPT REVISION LOOP (owned by this phase's builder; see
# build/tools/prompt_revision.py for the propose/accept/apply/requeue
# mechanism and its own module docstring). Delimited the same way Phase 4's
# block is, per the wave's file-ownership rule.
#
# ShotGrid state is the only trigger (invariant 1): sg_note_class ==
# "prompt-addressable" (written by genvideo_worker.py's existing note
# triage) is what makes a shot a propose candidate; sg_proposal_status ==
# "accepted" (a human), or "proposed" with sg_auto_apply_proposals == True
# (a standing, deliberate, per-shot opt-in - never set by any tool), is what
# makes a shot an apply candidate. prompt_revision.service_cycle() does both
# passes and is the only place that decides what counts as "ready" - this
# wrapper exists only to import lazily (so a missing/broken module cannot
# prevent the service from importing at all) and to log loudly rather than
# let a per-cycle exception here kill the whole service (same discipline as
# watch_beats()).
# ============================================================================

def watch_note_triage(sg):
    """Notes on shot-linked Versions -> Shot.sg_note_class + sg_review_verdict.

    Thin, like watch_prompt_proposals() below: note_triage.sweep() is the one
    implementation of the classifier and this does not grow a second one.

    EPISODE SCOPING IS THE TRAP HERE. note_triage's own module-level EP comes
    from episode_context.resolve_episode(), which defaults to PILOT01 unless
    GENVIDEO_EPISODE is set -- so a sweep run without that env var silently
    ignores every SHOW01 shot. That is the same unscoped-default defect this
    project already hit on beats.json. A standing service must not depend on
    an environment variable being set correctly by whoever launched it, so
    this sweeps EVERY episode that has shots, explicitly, and says which.

    Returns True only if a classification actually changed something, so an
    idle cycle stays quiet."""
    sys.path.insert(0, TOOLS)
    import note_triage as NT
    import episode_context as EPCTX
    did = False
    for code in sorted(EPCTX.known_episode_codes()):
        try:
            n = NT.sweep(sg, dry=False, episode_code=code)
        except TypeError:
            # An older note_triage without the episode_code parameter would
            # silently sweep its module default instead of the code asked
            # for, which is exactly the bug this watcher exists to close.
            # Refuse loudly rather than sweep the wrong episode.
            log("  note-triage: %s does not accept episode_code -- REFUSING "
                "to sweep, it would silently use its own default episode"
                % NT.__file__)
            return did
        if n:
            log("  note-triage[%s]: %d shot(s) classified" % (code, n))
            did = True
    return did


def watch_prompt_proposals(sg):
    sys.path.insert(0, TOOLS)
    import prompt_revision as PR
    return PR.service_cycle(sg, log=log)

# ============================================================================
# END PHASE 7
# ============================================================================


# ============================================================================
# PHASE 9 -- ANIMATIC DEMOTED (MASTER-PLAN-V2 D8, 2026-08-29). Owned by this
# phase's builder. Delimited per the wave's file-ownership rule, same as the
# PHASE 4 / PHASE 7 blocks above.
#
# V1's watchers (historically "1c"/"1d") auto-cut the sequence animatic the
# moment a Sequence's sg_stage field *read* "animatic", and auto-stitched the
# episode the moment every act's sg_stage reached "animatic" -- but sg_stage
# is a state marker written by several things (including animatic.py itself,
# after a cut, as a record of what stage the sequence has reached), and
# nothing ever stopped a human from advancing it for bookkeeping alone.
# Reading a status marker as a command is exactly the bug: approving a board,
# or just moving a sequence along in the UI, could not be told apart from
# "please spend a cut". D8 requires the animatic carry NO approval gate and
# fire on NOTHING but an explicit request, so the per-sequence trigger is
# re-keyed here off Sequence.sg_gen_status == 'animatic_requested' -- a value
# nothing else in this codebase writes, ever (grepped clean 2026-08-29).
# sg_stage is left alone; animatic.py still records 'animatic' there as
# descriptive state, it just no longer means anything to this watcher.
#
# The former episode auto-stitch (every act reaching sg_stage=animatic) is
# REMOVED outright here, not re-keyed: there is no ShotGrid entity that
# represents "the whole episode" for a request flag to live on (no
# per-episode Sequence, no Project-level gen field -- checked live,
# Project's only sg_ fields are sg_description/sg_status/sg_type), and
# inventing one solely to key a demoted, deferred, request-only feature is
# more schema surface than this phase should spend. `animatic.py --episode`
# is unchanged and still fully works -- it is simply run by hand/CLI now,
# the same as this codebase already runs several other on-request tools
# (attribute_check.py, teardown_verify.py, ...) outside the service loop.
# ============================================================================

# ============================================================================
# PHASE 5 -- THE PANEL STAGE (owned by this phase's builder; see
# panel_compose.py for the composer this depends on -- Phase 3's
# qwen_compose.py + attribute_check.py gate). Delimited the same way the
# Phase 4/7/9 blocks above are, per the wave's file-ownership rule: everything
# between this banner and "END PHASE 5" is this phase's block; nothing above
# it was touched.
#
# Two watchers, ShotGrid state the only trigger (invariant 1):
#
#   watch_panel_approvals()  A panel Version (sg_stage='panel') is flipped to
#       an approved status -> the service (never panel_compose.py, never any
#       other tool -- invariant 7, no self-approval) writes
#       Shot.sg_approved_panel to point at it. The human/reviewer act IS the
#       status change; this function only records the link.
#
#   watch_panel_designs()    An Asset's Asset.sg_approved_design changes (a
#       different design candidate gets approved) after a shot's panel was
#       already composed from the OLD one -> flag that shot stale
#       (sg_stage='panel') + a Note, so it needs re-compositing. Mirrors
#       Phase 4's watch_beats() exactly: a JSON sidecar
#       (build/out/panel_designs.json, written by panel_compose.py at publish
#       time) records which design Version id(s) fed each shot's panel; a
#       live Asset.sg_approved_design that no longer matches the recorded id
#       is a design swap the panel has not seen yet. Same content-diff-via-
#       sidecar shape as watch_beats() -- deliberately not a second mechanism.
# ============================================================================

PANEL_DESIGNS_JSON = os.path.join(ROOT, "build", "out", "panel_designs.json")

# Mirrors genvideo_worker.py's APPROVED_BOARD tuple and panel_compose.py's own
# copy of the same constant. A service/tool-owned status-list tuple, not the
# "one classifier" invariant 11 is about (that invariant is about note-
# triage/asset-role classification logic) -- kept identical here on purpose.
APPROVED_PANEL_STATUSES = ("apr", "ad", "fin", "paf", "dlvr")


def watch_panel_approvals(sg):
    """Version.sg_stage=='panel' AND status in APPROVED_PANEL_STATUSES ->
    Shot.sg_approved_panel is written to point at it, IF it does not already.
    When more than one panel Version for a shot is approved, the newest
    (by created_at) wins -- mirrors genvideo_worker.py's approved_board().
    This is the ONLY place in the codebase that writes
    Shot.sg_approved_panel; panel_compose.py never does (see its own
    docstring -- "no self-approval")."""
    try:
        panels = sg.find("Version",
                         [["project", "is", PROJ], ["sg_stage", "is", "panel"],
                          ["sg_status_list", "in", list(APPROVED_PANEL_STATUSES)]],
                         ["code", "entity", "sg_status_list", "created_at"])
    except Exception as exc:
        log("  panel-approval watcher: Version query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    by_shot = {}
    for v in panels:
        ent = v.get("entity") or {}
        if ent.get("type") != "Shot" or not ent.get("id"):
            continue
        by_shot.setdefault(ent["id"], []).append(v)
    # NO EARLY RETURN ON AN EMPTY by_shot. It used to bail here, which is
    # correct for the linking half (nothing approved, nothing to link) and
    # WRONG for the stale-link sweep below: a project where the only approved
    # panel has just been un-approved has an empty by_shot and a Shot still
    # pointing at it. That is precisely the state the sweep exists for, and the
    # early return made it unreachable -- the second time in this one fix that
    # the new code sat behind a guard that could never let it run.
    try:
        # BOTH POPULATIONS, and the second one is the whole point of the
        # stale-link sweep below. by_shot holds shots that HAVE an approved
        # panel Version right now; a shot whose approval was WITHDRAWN has
        # none, so it is absent from by_shot and would never be queried --
        # which is exactly the shot carrying the stale link. My first version
        # of this fix queried only by_shot and could therefore never fire on
        # the case it was written for.
        shots = sg.find("Shot",
                        [{"filter_operator": "any",
                          "filters": [["id", "in", list(by_shot) or [0]],
                                      ["sg_approved_panel", "is_not", None]]}],
                        ["id", "code", "sg_approved_panel"])
    except Exception as exc:
        log("  panel-approval watcher: Shot query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    # A LINK CAN GO STALE, and the watcher that set it never looked back.
    #
    # sg_approved_panel is written when a Version reaches an approved status and
    # is then never revisited, so un-approving that Version (to 'rrq', say)
    # leaves the Shot still pointing at it. MEASURED 2026-09-05: SHOW01_A_0360
    # was queued for video and video_from_panel refused -- "approved panel
    # Version ... has status 'rrq', not an approved status". That refusal is
    # correct and is why nothing bad rendered, but the Shot had been carrying a
    # wrong answer since whenever the status changed, and only a downstream
    # guard noticed.
    #
    # Clearing it is safe precisely BECAUSE of that guard: the video path
    # already refuses an unapproved panel, so removing the link takes nothing
    # away and stops the Shot asserting something untrue. Loud, never silent.
    approved_ids = set(v["id"] for v in panels)
    for s in shots:
        cur = (s.get("sg_approved_panel") or {}).get("id")
        if cur and cur not in approved_ids and s["id"] not in by_shot:
            log("  %s: sg_approved_panel points at Version %s, which is no longer "
                "at an approved status -- clearing the stale link"
                % (s["code"], cur))
            try:
                sg.update("Shot", s["id"], {"sg_approved_panel": None})
            except Exception as exc:                              # noqa: BLE001
                log("  %s: could not clear the stale link (%s)" % (s["code"], exc))

    did = False
    for s in shots:
        cands = sorted(by_shot.get(s["id"], []), key=lambda v: v.get("created_at") or "")
        if not cands:
            continue
        newest = cands[-1]
        cur = (s.get("sg_approved_panel") or {}).get("id")
        if cur == newest["id"]:
            continue
        log("panel approved for %s: Shot.sg_approved_panel -> %s (id %s)"
            % (s["code"], newest["code"], newest["id"]))
        try:
            sg.update("Shot", s["id"],
                      {"sg_approved_panel": {"type": "Version", "id": newest["id"]}})
        except Exception as exc:
            log("  FAILED to link approved panel on %s: %s: %s"
                % (s["code"], type(exc).__name__, str(exc)[:160]))
            continue
        close_panel_task(sg, s["id"], s["code"], log=log)
        did = True
    return did


def close_panel_task(sg, shot_id, shot_code, log=log):
    """An approved panel closes its Panel Task. -> True if the Task moved.

    WHY. Measured 2026-09-07 while operating the queue: 35 Panel Tasks sat at
    'rev', and 31 of them had NOTHING to review. 23 of those were shots whose
    panel had already been approved, because the approval watcher wrote
    Shot.sg_approved_panel and never touched the Task that asked for the review.

    From the operator's side the task board said 35 shots needed attention when
    4 did, and the 4 real ones were invisible in the noise. Same shape as every
    other defect this record carries: a state written in one place and its
    sibling left stale, correct at rest and wrong after the transition.

    'apr' is a real value on Task.sg_status_list (read from the live schema:
    hld, wtg, rdy, rrq, ip, rev, pf, apr), so this stays inside ShotGrid's own
    vocabulary rather than inventing one."""
    try:
        ts = sg.find("Task", [["entity", "is", {"type": "Shot", "id": shot_id}],
                              ["content", "is", PANEL_TASK_CONTENT],
                              ["step.Step.short_name", "is", PANEL_STEP_SHORT_NAME]],
                     ["id", "sg_status_list"])
    except Exception as exc:                                      # noqa: BLE001
        log("  %s: could not read the Panel Task to close it (%s)"
            % (shot_code, type(exc).__name__))
        return False
    moved = False
    for t in ts:
        if t.get("sg_status_list") == "apr":
            continue
        try:
            sg.update("Task", t["id"], {"sg_status_list": "apr"})
            log("  %s: panel approved, Panel Task %s -> apr (was %r)"
                % (shot_code, t["id"], t.get("sg_status_list")))
            moved = True
        except Exception as exc:                                  # noqa: BLE001
            log("  %s: could not close the Panel Task (%s)"
                % (shot_code, type(exc).__name__))
    return moved


def watch_review_housekeeping(sg):
    """After an approval, take the losing alternates out of the review queue.

    WHY THIS IS A WATCHER AND NOT A SCRIPT I RUN (Geoff, 2026-09-04): "30
    minutes later and the other versions status has not changed so
    'housekeeping' is not working. Is that you or a deterministic script as it
    should be?"

    It was me. sg_review_housekeeping.py existed, was self-tested, and had
    never been wired into anything -- so every description of the review loop
    that said "approving rejects the alternates" was describing a step a human
    was performing by hand. That is the exact shape of failure invariant 1
    exists to prevent: if ShotGrid state is the only trigger, then every
    consequence of a state change has to be reachable from ShotGrid alone.

    THE PARADIGM IT SERVES: the operator has pages filtered for things pending
    review, and on each they approve, request a revision, or reject. That only
    works if everything NOT awaiting a decision is out of the way -- and a
    status-only filter on this episode returned 108 Versions when 8 were real
    candidates.

    THE STATUS VOCABULARY, AS OF 2026-09-08: THERE IS ONE RETIRED STATUS.
    Everything this sweep retires goes to 'rjct'. The distinction between a
    losing alternate and an experiment used to be carried by 'omt', and Geoff
    collapsed it: the two statuses only ever existed to get a row out of the
    review queue, which 'rjct' already does, and the nuance was a record nobody
    acted on. THE REASON SURVIVES in the log line and the description, which is
    the part an operator can use. 1,405 legacy rows still carry 'omt' and are
    read correctly wherever a query excludes retired work.

    Nothing is ever APPROVED here, and 'rrq' is never touched -- a revision the
    operator asked for is an open conversation, not noise.
    """
    # ONE PROJECT-WIDE SWEEP, not one per episode code. Looping episode codes
    # passed `code starts_with SHOW01` into the query, which excluded every
    # DESIGN Version, because those are coded SHOW_SET_... and SHOW_CHAR_... and
    # carry no episode prefix. So an approved design left its 12 siblings
    # sitting at 'rev' forever, looking like they still needed review, and the
    # sweeper that exists to prevent exactly that had never been able to see
    # them. Found 2026-09-07 by Geoff asking why the watcher had not fired.
    #
    # Project scope is what this needs: classify() keys a batch on
    # (entity type, entity id, stage), so it cannot confuse two Assets or two
    # Shots, and the episode prefix was only ever removing rows.
    total = 0
    try:
        total = HOUSEKEEPING.sweep(sg, episode=None, dry=False)
    except Exception as exc:                                      # noqa: BLE001
        # LOUD (invariant 3), and never fatal: housekeeping is tidying, and
        # a tidy-up that cannot run must not take the render loop with it.
        log("  review-housekeeping: FAILED (%s: %s), skipping this cycle"
            % (type(exc).__name__, str(exc)[:160]))
    if total:
        log("  review-housekeeping: %d Version(s) moved out of the review queue"
            % total)
    return bool(total)


def _panel_designs_load():
    try:
        with open(PANEL_DESIGNS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _panel_designs_save(doc):
    try:
        with open(PANEL_DESIGNS_JSON, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
    except Exception as exc:
        log("  panel_designs.json: could not persist bookkeeping: %s" % exc)


def retire_superseded_panels(sg, shot_id, shot_code, cutoff, log=log):
    """Panel candidates composed BEFORE a design change are no longer candidates.
    Move them out of the review queue. -> how many moved.

    WHY. Measured 2026-09-07 while operating the queue: after Geoff approved a
    new bedroom design, 25 shots were correctly flagged stale, and **48 of the 56
    panel candidates sitting at 'rev' on those shots had been composed against the
    OLD room**. Nothing distinguished them from the 8 that were current. An
    operator opening the review queue is shown 56 images of which 48 cannot be
    approved, and no field says which.

    The cascade already knew the shot was stale. It just never said so about the
    IMAGES, which is the only place the operator actually looks.

    'omt' AND NOT 'rjct', deliberately. The housekeeping vocabulary is explicit:
    'rjct' is an alternate that LOST a judgement, 'omt' is something that was
    never a candidate. Nobody judged these panels and they may well have been
    good; the room changed underneath them. Calling them rejected would claim a
    judgement nobody made.

    THE CUTOFF IS THE NEW DESIGN'S created_at, so a panel composed AFTER the new
    design is left alone. Without that this would sweep the fresh work too, which
    is the same population-cut mistake as the episode prefix filters."""
    try:
        vs = sg.find("Version", [["project", "is", PROJ],
                                 ["entity", "is", {"type": "Shot", "id": shot_id}],
                                 ["sg_stage", "is", "panel"],
                                 ["sg_status_list", "is", "rev"],
                                 ["created_at", "less_than", cutoff]],
                     ["id", "code"])
    except Exception as exc:                                      # noqa: BLE001
        log("  panel watcher: could not query stale candidates for %s (%s)"
            % (shot_code, type(exc).__name__))
        return 0
    moved = 0
    for v in vs:
        try:
            sg.update("Version", v["id"], {"sg_status_list": "rjct"})
            moved += 1
        except Exception as exc:                                  # noqa: BLE001
            log("  panel watcher: could not retire %s (%s)" % (v["code"], type(exc).__name__))
    if moved:
        # GEOFF 2026-09-08 collapsed the retired statuses: "omt" is gone, and
        # everything the pipeline retires goes to "rjct". The REASON survives
        # in this line, which is the part an operator can actually use; the
        # status distinction was a nuance nobody acted on.
        log("  %s: %d panel candidate(s) composed before the design change retired to "
            "'rjct' -- they are not a judgement on the picture, they are obsolete" % (shot_code, moved))
    return moved


def watch_panel_designs(sg):
    """A shot's already-composed panel used design Version X for its
    character and/or set Asset; that Asset's Asset.sg_approved_design has
    since moved to a DIFFERENT Version -> flag the shot stale (sg_stage ->
    'panel') + a Note, same as watch_beats() does for a beat edit. Hardened
    the same way watch_beats() is (PHASE-4-FIX1.md): a malformed sidecar
    document, a non-dict shot entry, or a vanished Asset row must be skipped
    and logged, never a crash."""
    doc = _panel_designs_load()
    if not isinstance(doc, dict):
        if doc is not None:
            log("  panel watcher: panel_designs.json root is a %s, not an object -- "
                "skipping this cycle" % type(doc).__name__)
        return False                      # panel_compose.py has not published anything yet
    shots = doc.get("shots")
    if not isinstance(shots, dict) or not shots:
        return False

    asset_ids = set()
    for entry in shots.values():
        if not isinstance(entry, dict):
            continue
        for k in ("character_asset_id", "set_asset_id"):
            v = entry.get(k)
            if v:
                asset_ids.add(v)
    if not asset_ids:
        return False
    try:
        rows = sg.find("Asset", [["id", "in", list(asset_ids)]],
                       ["id", "code", "sg_approved_design"])
    except Exception as exc:
        log("  panel watcher: Asset query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    by_asset_id = {r["id"]: r for r in rows}

    did = False
    for shot_code, entry in shots.items():
        if not isinstance(entry, dict):
            log("  panel watcher: shots[%s] in panel_designs.json is %s, not an object -- "
                "skipping that shot" % (shot_code, type(entry).__name__))
            continue
        reasons = []
        new_design_ids = []
        touched_keys = []
        for role, asset_key, design_key in (
                ("character", "character_asset_id", "character_design_version_id"),
                ("set", "set_asset_id", "set_design_version_id")):
            aid = entry.get(asset_key)
            if not aid:
                continue
            arow = by_asset_id.get(aid)
            if not arow:
                log("  panel watcher: Asset id=%s (referenced by %s) not found -- skipping "
                    "that component" % (aid, shot_code))
                continue
            cur = (arow.get("sg_approved_design") or {}).get("id")
            recorded = entry.get(design_key)
            # DEDUP AGAINST A SEPARATE FIELD, NOT AGAINST THE PROVENANCE.
            # `recorded` is what this panel was actually composed from and must
            # stay true; `flagged_key` only remembers what we last warned
            # about. See the write at the bottom of this function for what
            # went wrong when the two were the same field.
            flagged_key = design_key + "_stale_flagged"
            touched_keys.append((flagged_key, cur))
            if cur != recorded and entry.get(flagged_key) != cur:
                reasons.append("%s asset %s: approved design Version changed %r -> %r"
                               % (role, arow.get("code"), recorded, cur))
                if cur:
                    new_design_ids.append(cur)
        if not reasons:
            continue
        try:
            srow = sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", shot_code]],
                               ["id"])
        except Exception as exc:
            log("  panel watcher: Shot lookup failed for %s (%s), skipping"
                % (shot_code, type(exc).__name__))
            continue
        if not srow:
            log("  panel watcher: Shot %s in panel_designs.json not found in ShotGrid -- "
                "skipping" % shot_code)
            continue
        log("design swapped for %s: flagging panel stale (sg_stage -> panel)" % shot_code)
        try:
            sg.update("Shot", srow["id"], {"sg_stage": "panel"})
            # PIPELINE BOOKKEEPING IS NOT A NOTE. Geoff, 2026-09-07: "publishing should
            # not add notes. use description if something needs adding." A Note is a
            # request, or a human's record of review. The system announcing what it just
            # did is neither, and putting it in Notes filled the operator's review queue
            # with the pipeline talking to itself, which it then read back as work (F209:
            # 24 of 29 actionable notes were self-authored).
            #
            # The SIGNAL is the state field written just above, which the operator and
            # every watcher already read. Publish-time detail belongs on the VERSION
            # description, which is fresh and empty for every publish; Shot and Asset
            # descriptions are not in the publish path at all.
            pass
        except Exception as exc:
            log("  FAILED to flag %s stale: %s: %s"
                % (shot_code, type(exc).__name__, str(exc)[:160]))
            continue
        # ...and take the shot's now-obsolete panel candidates out of the review
        # queue, because flagging the SHOT tells the operator nothing when the
        # IMAGES are what they are looking at. See retire_superseded_panels().
        try:
            cutoff = None
            for did in new_design_ids:
                drow = sg.find_one("Version", [["id", "is", did]], ["created_at"])
                if drow and drow.get("created_at") and (cutoff is None
                                                        or drow["created_at"] > cutoff):
                    cutoff = drow["created_at"]
            if cutoff is not None:
                retire_superseded_panels(sg, srow["id"], shot_code, cutoff, log=log)
        except Exception as exc:                                  # noqa: BLE001
            log("  panel watcher: retiring superseded candidates failed for %s (%s: %s)"
                % (shot_code, type(exc).__name__, str(exc)[:120]))

        # RECORD THAT WE WARNED, NOT A FALSE PROVENANCE.
        #
        # This used to assign the live design id straight into the provenance
        # key itself, overwriting the record
        # of what the panel was COMPOSED FROM with the design that superseded
        # it, purely so the next cycle would not re-fire. It worked for that,
        # and it destroyed the only queryable record of staleness in the
        # process. MEASURED 2026-09-04: after this watcher correctly flagged
        # four SHOW01 panels, panel_designs.json claimed all four were composed
        # from Version 67631 -- an anchor that did not exist until hours after
        # three of them were rendered. The Note was then the ONLY evidence, so
        # anything that wanted to ACT on staleness (un-approve the panel, clear
        # Shot.sg_approved_panel, requeue) had nothing to read and concluded
        # there was nothing stale.
        #
        # The suffixed key does the same dedup job without lying: staleness
        # stays derivable forever as (live approved != design_key), and
        # re-fire is prevented by (design_key_stale_flagged == live approved).
        # record_panel_designs() overwrites design_key legitimately when a new
        # panel is actually composed, which is the only event that should.
        for flagged_key, cur in touched_keys:
            entry[flagged_key] = cur
        did = True
    if did:
        _panel_designs_save(doc)
    return did

# ============================================================================
# END PHASE 5
# ============================================================================


# ============================================================================
# PHASE 6 -- VIDEO FROM THE APPROVED PANEL (owned by this phase's builder; see
# build/tools/video_from_panel.py for the actual i2v job -- panel-anchor
# validation/refusal, A14B generation, fps_bridge.py's 16fps->24fps bridge,
# and provenance incl. sg_anchor_version). Delimited the same way the Phase
# 4/5/7/9 blocks above are: everything between this banner and "END PHASE 6"
# is this phase's block; nothing above it was touched.
#
# TRIGGER (invariant 1): Shot.sg_gen_status == 'queued'. This is the SAME
# field genvideo_worker.py's own queued-shot pass (cycle(), step 1 below)
# already watches -- deliberately: Phase 6 supersedes that older pass for
# VIDEO GENERATION specifically, because the older worker has no concept of
# an approved panel and would happily i2v/t2v off a board or Asset reference,
# which is the exact V1 failure this whole plan exists to close. Rather than
# edit step 1's code (a wider, riskier change to code this phase does not
# own), watch_panel_video_queue() is called FIRST in cycle(), below, and is
# synchronous: by the time it returns, every Shot that was 'queued' has
# already been moved to 'refused', 'generating'/'review', or 'error' -- none
# of them still read 'queued', so step 1's own query (which runs moments
# later in the SAME cycle(), single-threaded, no concurrency) finds nothing
# left to claim. This is ordering, not a query change -- see PHASE-6-BUILD.md
# for why editing step 1 itself was judged the larger, less surgical option.
#
# video_from_panel.py --once does its own refusal-vs-generate split and its
# own ComfyUI lifecycle (only started when at least one shot in the batch has
# a valid anchor; refuse-only passes never touch the GPU). This wrapper's job
# is only: decide whether to spend a subprocess call at all (cheap query
# first, same "check before spawning" shape as steps 1/1b below), and surface
# its output loudly (FAILSAFE_RC via run(), same as every other subprocess
# call in this file).
# ============================================================================

# THE CASCADE USED TO STOP AT THE PANEL, which is half of what the demo
# claims. Approving a revised panel left the shot's APPROVED VIDEO in place,
# built from the panel that was just replaced, and nothing said so. Found by
# rehearsing the loop end to end on 2026-09-07: SHOW01_A_0050's panel went to
# v005 at 11:15 and its approved video was still the 18:42 render from the
# night before, made from v001.
#
# THE DISCRIMINATOR IS THE TIMESTAMP, the same one three other rules here use:
# an approved video created BEFORE the approved panel was made from an older
# picture. Equal timestamps are not stale.
#
# IT QUEUES, IT DOES NOT UN-APPROVE. Un-approving the old video immediately
# would leave the shot with no approved video at all, and an assemble refuses a
# shot in that state, so a cascade would break the cut it is trying to keep
# current. The existing, tested mechanism already handles the handover:
# sg_publish.SUPERSEDABLE un-approves the old one when the replacement is
# approved.
#
# IT WRITES NO NOTE. Geoff, 2026-09-07: notes are for people, not for the
# pipeline talking to itself. The new candidate arriving at 'rev' in the review
# queue is the signal.
#
# MEASURED BEFORE WIRING IT UP, because this spends GPU: across the whole
# project exactly 2 shots were in this state, one of them in the dead PILOT01
# episode. It is not a floodgate.
# AN EPISODE ON HOLD IS NOT WORK. PILOT01 is a dead episode and 53 of the
# project's 58 approved shots belong to it, so any watcher that spends GPU has
# to know. Until 2026-09-07 that fact lived only in STATE.md, which no watcher
# can read: this puts it in ShotGrid, where the rule already says it belongs,
# as Episode.sg_status_list = 'hld'. Episode-agnostic on purpose, there is no
# episode code anywhere in this rule, so a future dead show is handled by
# flipping one field rather than by editing this file.
HELD_EPISODE_STATUSES = ("hld",)


def held_episode_ids(sg, log=log):
    """-> set of Episode ids that are on hold, or an EMPTY set if ShotGrid
    cannot answer. Empty means "hold nothing back", which errs toward doing
    the work: the alternative, treating an outage as "everything is held",
    would silently stop the pipeline and look exactly like an idle queue."""
    try:
        eps = sg.find("Episode", [["project", "is", PROJ],
                                  ["sg_status_list", "in", list(HELD_EPISODE_STATUSES)]],
                      ["code"])
    except Exception as exc:
        log("  held-episode lookup failed (%s), treating no episode as held"
            % type(exc).__name__)
        return set()
    return set(e["id"] for e in eps)


def held_episode_codes(sg, log=log):
    """-> set of Episode CODES that are on hold, empty if ShotGrid cannot answer.

    The sibling of held_episode_ids() for callers that work in codes rather than
    ids. It exists because the first version of the stale-panel invalidation
    compared a CODE against that function's ID set, which can never match, so
    the held-episode skip silently did nothing. Same empty-means-hold-nothing
    rule, for the same reason."""
    try:
        eps = sg.find("Episode", [["project", "is", PROJ],
                                  ["sg_status_list", "in", list(HELD_EPISODE_STATUSES)]],
                      ["code"])
    except Exception as exc:                                      # noqa: BLE001
        log("  held-episode lookup failed (%s), treating no episode as held"
            % type(exc).__name__)
        return set()
    return set((e.get("code") or "") for e in eps if e.get("code"))


def stale_video_shots(sg, log=log):
    """-> [(shot code, shot id, panel code, video code)] whose approved video
    predates their approved panel. Returns [] and says so if ShotGrid cannot
    answer, rather than treating an outage as "nothing is stale"."""
    held = held_episode_ids(sg, log=log)
    try:
        vs = sg.find("Version",
                     [["project", "is", PROJ],
                      # ONE definition of "approved", imported from the sweeper
                      # rather than copied (invariant 11). sg_publish mirrors
                      # the same tuple and canaries that the two still agree.
                      ["sg_status_list", "in", list(HOUSEKEEPING.APPROVED)],
                      ["sg_stage", "in", ["panel", "video"]],
                      ["entity", "type_is", "Shot"]],
                     ["code", "sg_stage", "entity", "created_at"])
    except Exception as exc:
        log("  stale-video watcher: Version query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return None
    if held:
        try:
            shots = sg.find("Shot", [["project", "is", PROJ],
                                     ["sg_episode.Episode.id", "in", list(held)]],
                            ["code"])
            held_shot_ids = set(s["id"] for s in shots)
        except Exception as exc:
            log("  stale-video watcher: held-shot lookup failed (%s), "
                "considering every shot" % type(exc).__name__)
            held_shot_ids = set()
        if held_shot_ids:
            vs = [v for v in vs
                  if (v.get("entity") or {}).get("id") not in held_shot_ids]
    newest = {}
    for v in vs:
        ent = v.get("entity") or {}
        key = (ent.get("id"), v.get("sg_stage"))
        cur = newest.get(key)
        when = v.get("created_at")
        if when is None:
            continue
        if cur is None or when > cur["created_at"]:
            newest[key] = v
    out = []
    for (sid, stage), v in newest.items():
        if stage != "video":
            continue
        p = newest.get((sid, "panel"))
        if p is None or p["created_at"] <= v["created_at"]:
            continue
        out.append(((p.get("entity") or {}).get("name"), sid, p["code"], v["code"]))

    # F468: A SHOT'S FIRST VIDEO WAS NEVER QUEUED BY ANYTHING. The loop above
    # asks "is the approved panel newer than the approved video", which can
    # only ever fire for a shot that ALREADY HAS a video. A shot that has
    # never had one has nothing to be newer than, so it was never considered.
    # Measured 2026-09-08 on SHOW01: 16 shots had an approved panel and a live
    # video, and 30 had an approved panel and NO video of any status, 28 of
    # them with sg_gen_status unset, meaning nothing had ever asked. Geoff
    # found it by asking why SHOW01_A_0160's approved panel had no render.
    #
    # "LIVE" IS DELIBERATELY WIDER THAN "APPROVED" HERE. A video sitting at
    # 'rev' is waiting for a human, not missing, and queueing another one
    # would churn the GPU and bury the operator in alternates for a decision
    # they have not made yet. So a shot counts as already having a video
    # unless every video Version on it is rjct or omt.
    panel_shots = set(sid for (sid, stage) in newest if stage == "panel")
    if panel_shots:
        try:
            live_vids = sg.find("Version",
                                [["project", "is", PROJ],
                                 ["entity", "in", [{"type": "Shot", "id": i}
                                                   for i in sorted(panel_shots)]],
                                 ["sg_stage", "is", "video"],
                                 ["sg_status_list", "not_in", ["rjct", "omt"]]],
                                ["entity", "created_at"])
            # A LIVE VIDEO ONLY COUNTS IF IT IS AT LEAST AS NEW AS THE APPROVED
            # PANEL. Geoff, 2026-09-08: "whats the point in allowing stale
            # videos to be reviewed and possibly approved if they contain stale
            # designs or stale panels?" He is right, and it makes the churn
            # guard sharper rather than weaker: a video WAITING for review is
            # not missing, so we do not queue another one -- unless the picture
            # underneath it has already moved on, in which case reviewing it is
            # worse than useless, because approving it would bless a frame
            # built from a design nobody approves any more.
            have_video = set()
            for lv in live_vids:
                sid = (lv.get("entity") or {}).get("id")
                p = newest.get((sid, "panel"))
                when = lv.get("created_at")
                if p is None or when is None or when >= p["created_at"]:
                    have_video.add(sid)
        except Exception as exc:                                  # noqa: BLE001
            # Fail SAFE, and the safe direction here is to queue NOTHING: a
            # failed lookup must never be read as "no video exists" and start
            # thirty renders.
            log("  stale-video watcher: live-video lookup failed (%s), not "
                "queueing any first videos this cycle" % type(exc).__name__)
            have_video = panel_shots
        for sid in sorted(panel_shots - have_video):
            p = newest[(sid, "panel")]
            out.append(((p.get("entity") or {}).get("name"), sid, p["code"], None))
    return sorted(out, key=lambda r: (str(r[0]), str(r[2])))


def watch_stale_videos(sg):
    """Queue a new video for any shot whose approved video predates its
    approved panel. Half of the cascade the demo claims, and it was missing.

    ROADMAP 0a: a PROTECTED shot must not be requeued even though its video
    genuinely is stale against its panel -- that divergence is the whole
    point of the lock (the roadmap's own words: "an upstream change to a
    locked shot is simply not applied"). Protection lives on the VERSION
    now, not the Shot (Geoff's 2026-09-08 correction), so this asks a
    single extra Version query -- entity in the ids stale_video_shots()
    already found, filtered to SHOT_LOCK.version_filter() -- rather than N
    queries, one per shot. An unprotected cycle (the live, common case
    today) still pays exactly one cheap query and nothing else changes."""
    rows = stale_video_shots(sg)
    if rows is None or not rows:
        return False
    protecting_by_shot = {}
    shot_ids = [sid for _, sid, _, _ in rows]
    try:
        prot = sg.find("Version",
                       [["entity", "in", [{"type": "Shot", "id": sid} for sid in shot_ids]],
                        SHOT_LOCK.version_filter()],
                       ["code", "sg_status_list", "entity"])
        for v in prot:
            psid = (v.get("entity") or {}).get("id")
            if psid is not None and psid not in protecting_by_shot:
                protecting_by_shot[psid] = v
    except Exception as exc:                                      # noqa: BLE001
        log("  stale-video watcher: protection lookup failed (%s), skipping this "
            "cycle rather than risk requeueing a locked shot"
            % type(exc).__name__)
        return False
    ids = []
    for code, sid, pcode, vcode in rows:
        if sid in protecting_by_shot:
            log("  stale video: " + SHOT_LOCK.refusal(code, "stale-video watcher",
                                                       version=protecting_by_shot[sid])
                + " (panel %s stays newer than video %s until unlocked)" % (pcode, vcode))
            continue
        if vcode is None:
            log("  first video: %s has an approved panel %s and no live video at "
                "all -- queueing its first video (F468)" % (code, pcode))
        else:
            log("  stale video: %s approved panel %s is newer than approved video %s"
                " -- queueing a new video" % (code, pcode, vcode))
        ids.append(sid)
    if not ids:
        return False
    try:
        sg.batch([{"request_type": "update", "entity_type": "Shot",
                   "entity_id": i, "data": {"sg_gen_status": "queued"}}
                  for i in ids])
    except Exception as exc:
        log("  stale-video watcher: queueing failed (%s: %s)"
            % (type(exc).__name__, str(exc)[:120]))
        return False
    log("stale-video pass: queued %d shot(s)" % len(ids))
    return True


def watch_panel_video_queue(sg):
    try:
        queued = sg.find("Shot", [["project", "is", PROJ], ["sg_gen_status", "is", "queued"]],
                         ["code"])
    except Exception as exc:
        log("  panel-video watcher: Shot query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    if not queued:
        return False
    log("panel-video pass: %d shot(s) at sg_gen_status=queued (approved-panel gated)"
        % len(queued))
    r = run([PY, VIDEO_FROM_PANEL, "--once"], label="video_from_panel.py")
    if r:
        for line in (r.stdout or "").splitlines():
            if ("->" in line or "REFUSED" in line or "ERROR" in line
                    or "teardown" in line or "published" in line):
                log("  " + line.strip())
    return True

# ============================================================================
# END PHASE 6
# ============================================================================


def watch_animatic_requests(sg):
    """Sequence.sg_gen_status == 'animatic_requested' -> cut that sequence's
    animatic, on request only (D8). Returns True if any sequence was handled
    this cycle. Mirrors watch_beats()'s shape: import-free, degrades to an
    empty list rather than raising if the field query itself fails."""
    try:
        anim = sg.find("Sequence",
                       [["project", "is", PROJ], ["sg_gen_status", "is", "animatic_requested"]],
                       ["code"])
    except Exception:
        anim = []
    did = False
    for s in anim:
        log("animatic explicitly requested for %s (sg_gen_status=animatic_requested)" % s["code"])
        r = run([PY, ANIMATIC, "--animatic", s["code"]], timeout=7200)
        ok = bool(r and r.returncode == 0)
        if r:
            for line in (r.stdout or "").splitlines():
                if line.strip():
                    log("  " + line.strip()[:160])
        # Clear the request either way: a failed cut must not spin forever
        # re-running every cycle, and FAILSAFE_RC (run(), above) already
        # logged the failure loudly. 'error' and 'done' are both pre-existing
        # valid_values on this field (invariant 8: extended, never replaced --
        # real valid_values as of this change: queued, assembling, done,
        # error, animatic_requested).
        sg.update("Sequence", s["id"], {"sg_gen_status": "done" if ok else "error"})
        did = True
    return did

# ============================================================================
# END PHASE 9
# ============================================================================


# ============================================================================
# PHASE 10 -- THE ASSET DESIGN LOOP (2026-09-03). Delimited the same way the
# PHASE 4/5/6/7/9 blocks above are; nothing above it was touched.
#
# THE HOLE THIS CLOSES. Every watcher above is keyed on a Shot or a Sequence.
# Asset appears in exactly one of them (watch_panel_designs, by id, to read
# sg_approved_design) and is never SCANNED. So the entire Asset design stage
# -- the stage that produces the character and set anchors every panel is
# composed from -- had no service presence at all:
#
#   * A reviewer setting an Asset design Version to 'rrq' (Revision
#     Requested) and leaving a note caused NOTHING. note_triage's --sweep
#     classifies that note and then drops it, because it writes its verdict
#     onto a SHOT and an Asset design Version has no Shot;
#     prompt_revision.service_cycle() selects on Shot.sg_note_class and
#     writes to Shot.sg_proposal_status. The revision loop was Shot-scoped
#     end to end. -> watch_design_revisions(), below.
#
#   * A reviewer setting an Asset design Version to an APPROVED status also
#     caused nothing: Asset.sg_approved_design has been written exactly 16
#     times on this project, all on 2026-08-29, all by hand-run one-off
#     scripts on the scratch disk (ops/d14/d14_approve_designs.py and
#     siblings). Nothing in the standing service ever wrote it, and a
#     ShotGrid-side daemon that might have was checked and does not exist
#     for this project. Live proof at build time: Version 67462 was 'apr' on
#     Asset 12197 SHOW_CHAR_PILOTCHARA and that Asset still read
#     sg_approved_design=None. -> watch_design_approvals(), below.
#
# Both are the Asset counterparts of watchers that already exist for Shots,
# and both are written as mirrors of those rather than as new idioms:
# watch_design_approvals() is watch_panel_approvals() with Asset/
# sg_approved_design in place of Shot/sg_approved_panel, and
# watch_design_revisions() is the same thin lazy-import wrapper
# watch_prompt_proposals() is, over prompt_revision.design_service_cycle().
#
# HOW THE TWO NEW WATCHERS COMPOSE WITH watch_panel_designs(), which is the
# one real interaction risk, because that watcher REACTS to the very field
# watch_design_approvals() now WRITES:
#
#   watch_design_approvals   reads  Version.sg_status_list, Asset.sg_approved_design
#                            writes Asset.sg_approved_design, Asset.sg_stage
#   watch_panel_designs      reads  Asset.sg_approved_design, panel_designs.json
#                            writes Shot.sg_stage, a Note, panel_designs.json
#
# No output of either is an input of the other -- the approvals watcher never
# reads a Shot or the sidecar, the staleness watcher never writes an Asset --
# so there is no cycle to run away. Each is independently a fixed point after
# one pass, too: the approvals watcher writes only when the Asset's recorded
# design differs from the newest approved Version (`cur == newest["id"] ->
# continue`, exactly as watch_panel_approvals guards), and the staleness
# watcher stamps the sidecar with the new id so the same swap cannot re-fire.
# Verified by running the real cycle twice against live ShotGrid: the second
# pass wrote nothing (see docs/METHOD.md).
#
# The third interaction is between the two NEW watchers, and it is the one
# that would ping-pong if written carelessly: prompt_revision.apply_for_asset()
# sets Asset.sg_stage='design' when an accepted revision lands, and this
# watcher sets Asset.sg_stage='approved'. They do not fight, because
# sg_stage here is written ONLY inside the sg_approved_design-changed branch
# -- and apply_for_asset() deliberately leaves sg_approved_design alone. So
# a revised Asset stays at 'design' until a genuinely NEW design Version is
# approved, which is the correct reading of both fields. Canaried in
# --self-test ("revision then approval-scan does not resurrect 'approved'").
#
# NO SELF-APPROVAL (invariant 7) is unchanged and is why this watcher is in
# the SERVICE and not in anima_anchor.py/character_sheets.py: the human (or
# D14 reviewer) act IS the Version status change; this function only records
# the link it implies. It creates no approval Note of any kind, so it can
# never write a "provisional" label over a real human approval -- Geoff
# approved 67462 himself, and all this does is record which Version that was.
#
# WHAT IT DELIBERATELY DOES NOT WRITE: Asset.sg_status_list. All 16 Assets
# on this project that already have an approved design sit at
# sg_status_list='wtg' -- the field's own default, written by nothing in the
# repo and read by nothing either (panel_compose.py gates on sg_stage ==
# 'approved' AND sg_approved_design, never on sg_status_list). Setting it to
# 'apr' here would make the six SHOW_ Assets the only ones on the project
# whose status disagrees with the other sixteen, and would break any
# status-filtered query across the two shows. Counted, not assumed: 16 of 16.
# Flagged for Geoff in the report rather than decided here; it is one line to
# add if he wants Asset status carried too, and it should then be backfilled
# across all 16 in the same pass.
# ============================================================================

# Mirrors APPROVED_PANEL_STATUSES above, and genvideo_worker.py's
# APPROVED_BOARD, deliberately kept identical rather than shared: same
# service/tool-owned status tuple, not the "one classifier" invariant 11
# guards.
APPROVED_DESIGN_STATUSES = ("apr", "ad", "fin", "paf", "dlvr")

# THE ELIGIBILITY PREDICATE: which Versions are candidates to BE a design.
#
# An approved status alone is NOT enough, and assuming it was produced a real
# wrong write on live data. Version 67462
# (SHOW01_ANIMA_P3_10_PILOTCHARA12197_base_turboLoraV0.2_10step) is a LoRA WEDGE
# CELL whose own description opens "ANIMA TEST RECORD -- not a deliverable,
# not approved (invariant 7)". Geoff set it to 'apr' as a RECIPE preference
# ("this recipe is the one"), not as SHOW_CHAR_PILOTCHARA's approved character
# design. A naive status-only rule made a test cell the show's approved design
# for PilotCharA; it did, and it was reverted.
#
# The discriminator is STRUCTURAL, not a name, and it is the field the publish
# contract itself sets. Counted across all 164 Asset-entity Versions on this
# project:
#
#   sg_stage == 'keyframe'  125  every real design candidate. Written by
#                                anima_anchor.py / character_sheets.py through
#                                sg_publish.publish_version(stage="keyframe").
#                                ALL 16 designs currently linked as
#                                Asset.sg_approved_design are 'keyframe'
#                                (16/16), and so are all 39 of the real SHOW_
#                                anchor candidates.
#   sg_stage is None         39  every R&D/wedge test cell, including 67462.
#                                SESSION-HANDOVER.md states this convention
#                                outright: "sg_stage has no value meaning
#                                'technical test'. Leave it unset for R&D."
#
# Perfect separation, and it is exactly the idiom watch_panel_approvals()
# already uses one field over (sg_stage == 'panel'). Anything that does not
# clearly qualify is REFUSED and logged, never guessed into an approval:
# an Asset whose only approved Version is a test cell gets a log line saying
# so and no write at all.
DESIGN_STAGE = "keyframe"


def watch_design_approvals(sg):
    """A Version whose entity is an ASSET, whose sg_stage is DESIGN_STAGE
    (see the predicate above -- an approved status alone is NOT enough), and
    whose sg_status_list is an approved one -> Asset.sg_approved_design is
    written to point at it, and Asset.sg_stage is set to 'approved', IF the
    Asset does not already record a Version at least as new.

    Newest-approved (by created_at) wins when an Asset has several, exactly
    as watch_panel_approvals() and genvideo_worker.approved_board() already
    resolve the same ambiguity for Shots. That is safe against "overwriting a
    human approval" because nothing in this codebase can self-approve
    (invariant 7): a Version is only ever at an approved status because a
    human or a D14 reviewer put it there, so the newest one is by
    construction the most recent human decision.

    'approved' and 'design' are the only two values Asset.sg_stage has ever
    held (its valid_values are exactly those two), and 'approved' is what
    ops/d14/d14_approve_designs.py wrote alongside sg_approved_design on all
    16 existing approvals -- this is that same pair, moved out of a one-off
    script and into the service where it belongs."""
    try:
        designs = sg.find("Version",
                          [["project", "is", PROJ],
                           ["sg_status_list", "in", list(APPROVED_DESIGN_STATUSES)]],
                          # sg_stage is LOAD-BEARING here (the eligibility
                          # predicate). Omitting it from this list made every
                          # Version read back as sg_stage=None and the whole
                          # project look ineligible -- caught by the live run,
                          # and the stub in --self-test now honours the field
                          # list so a stub can catch it too.
                          ["code", "entity", "sg_status_list", "sg_stage", "created_at"])
    except Exception as exc:
        log("  design-approval watcher: Version query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    by_asset = {}
    ineligible = {}
    for v in designs:
        ent = v.get("entity") or {}
        if ent.get("type") != "Asset" or not ent.get("id"):
            continue
        if v.get("sg_stage") != DESIGN_STAGE:
            # An approved TEST CELL, not a design candidate. Refuse and say so
            # (invariant 3, loud) rather than guess -- see DESIGN_STAGE above.
            ineligible.setdefault(ent["id"], []).append(v)
            continue
        by_asset.setdefault(ent["id"], []).append(v)
    for aid, rows in ineligible.items():
        if aid in by_asset:
            continue          # a real candidate exists; the test cells are just noise
        log("  design-approval watcher: Asset %s has approved Version(s) %s but none at "
            "sg_stage=%r -- these are test cells, not design candidates; refusing to "
            "link one" % (aid, [r["id"] for r in rows], DESIGN_STAGE))
    if not by_asset:
        return False
    try:
        assets = sg.find("Asset", [["id", "in", list(by_asset)]],
                         ["id", "code", "sg_approved_design", "sg_stage"])
    except Exception as exc:
        log("  design-approval watcher: Asset query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    # THE DOWNGRADE GUARD. An Asset can already record an approved design
    # whose own Version is NOT at an approved status -- the 2026-08-29 one-off
    # scripts linked several that way (ops/d13_reapprove_anchors.py and
    # siblings wrote Asset.sg_approved_design without also flipping the
    # Version). Newest-APPROVED-wins then reads that Asset as "no valid
    # candidate is linked" and replaces a deliberate, newer human link with
    # an older one. Found the hard way: the first live run of this watcher
    # moved CHAR_CHARF from CHAR_CHARF_ANCHOR_v001 (67191, created
    # 08:00) back to CHAR_CHARF_SHEET_v002 (67081, created 01:53) --
    # a real regression on real production data, reverted immediately. So the
    # already-recorded Version's own created_at is fetched and a STRICTLY
    # NEWER candidate is required to supersede it. Nothing this watcher does
    # can move an Asset backwards.
    recorded_ids = set()
    for a in assets:
        rid = (a.get("sg_approved_design") or {}).get("id")
        if rid:
            recorded_ids.add(rid)
    recorded_created = {}
    if recorded_ids:
        try:
            for r in sg.find("Version", [["id", "in", list(recorded_ids)]], ["created_at"]):
                recorded_created[r["id"]] = r.get("created_at")
        except Exception as exc:
            # Cannot prove the candidate is newer -> refuse to move anything
            # that already has a link. Never guess in the direction of a write.
            log("  design-approval watcher: could not read the recorded designs' dates "
                "(%s); this cycle will only link Assets that have NO approved design yet"
                % type(exc).__name__)
            recorded_created = None

    def _is_newer(cand_ct, cur_ct):
        """Strictly newer, and CONSERVATIVE when it cannot tell: an
        uncomparable or missing date means 'do not supersede'."""
        if cand_ct is None or cur_ct is None:
            return False
        try:
            return cand_ct > cur_ct
        except TypeError:
            return False

    did = False
    for a in assets:
        cands = sorted(by_asset.get(a["id"], []),
                       key=lambda v: (v.get("created_at") is not None, v.get("created_at")))
        if not cands:
            continue
        newest = cands[-1]
        cur = (a.get("sg_approved_design") or {}).get("id")
        if cur == newest["id"]:
            continue          # THE fixed point: nothing to do, and nothing written,
                              # so watch_panel_designs() is never re-triggered either
        if cur is not None:
            cur_ct = None if recorded_created is None else recorded_created.get(cur)
            if not _is_newer(newest.get("created_at"), cur_ct):
                log("  %s already records design Version %s and %s is not newer -- "
                    "refusing to downgrade an existing approval"
                    % (a["code"], cur, newest["id"]))
                continue
        log("design approved for %s: Asset.sg_approved_design -> %s (id %s), sg_stage -> approved"
            % (a["code"], newest["code"], newest["id"]))
        try:
            sg.update("Asset", a["id"],
                      {"sg_approved_design": {"type": "Version", "id": newest["id"]},
                       "sg_stage": "approved"})
        except Exception as exc:
            log("  FAILED to link approved design on %s: %s: %s"
                % (a["code"], type(exc).__name__, str(exc)[:160]))
            continue
        did = True
    return did


def watch_design_revisions(sg):
    """An Asset design Version at sg_status_list='rrq' + the Note explaining
    it -> an LLM revision proposal on the Asset's own design prompt. Nothing
    is applied without a human accept.

    Thin by design, exactly like watch_prompt_proposals() above: the lazy
    import means a missing or broken prompt_revision.py cannot stop this
    service from importing at all, and prompt_revision.design_service_cycle()
    is the only place that decides what counts as ready -- it reuses that
    module's propose/apply/ledger/dedup machinery and note_triage's one
    classifier rather than growing a second copy here."""
    sys.path.insert(0, TOOLS)
    import prompt_revision as PR
    return PR.design_service_cycle(sg, log=log)

# ============================================================================
# END PHASE 10
# ============================================================================


# ============================================================================
# PHASE 11 -- THE ASSET RENDER TRIGGER (2026-09-03). Geoff, verbatim: "GPU
# time is local and free, just run the prompt automatically... operator
# writes a note, watcher revises prompts and queues the new render
# automatically." Phase 10 revises Asset.sg_prompt_fragment automatically
# now (auto-apply is the default for SHOW01 -- see prompt_revision.
# setup_asset_schema()'s backfill), but stopped there: there was NO Asset
# field that could queue a render at all (schema read, 2026-09-03 -- Shot
# has sg_gen_status, Asset had nothing). This closes that gap.
#
# THE FIELD is prompt_revision.A_GEN_STATUS ("sg_gen_status" on Asset,
# invariant 8 -- read back after schema_field_create by
# prompt_revision.setup_asset_schema()). ASSET_GEN_STATUS below is the SAME
# string, named a second time only because this file must not import
# prompt_revision at module scope (its own lazy-import discipline, same
# reason watch_design_revisions() imports it inside the function). The
# self-test canary "ASSET_GEN_STATUS agrees with prompt_revision.A_GEN_STATUS"
# is what stops the two ever silently drifting apart.
#
# THE ONLY WRITER of 'queued' is prompt_revision.apply_for_asset() (Phase 10),
# in the SAME combined sg.update() as the fragment rewrite -- grep confirms
# it. This watcher NEVER writes 'queued'; it only ever walks the field
# forward, 'queued' -> 'rendering' -> 'done' or 'error', both of which are
# terminal until a NEW human-driven revision (a new note + rrq) applies again.
# That single-writer, forward-only shape is why this cannot re-trigger
# itself -- see the loop-guard note in docs/METHOD.md
# for the full argument and the mutant that proves it.
#
# NO SELF-APPROVAL (invariant 7), unchanged: publish_candidate() (imported
# from anima_anchor.py, never copied) publishes every candidate at 'rev' --
# this watcher runs the SAME function a human runs by hand via
# `anima_anchor.py --generate --only CODE`, nothing more permissive. It never
# writes Asset.sg_approved_design or any approved status. The gate did not
# move off the render; it moved from "approve the prompt text" (Phase 10's
# auto-apply removed that step) to "approve the resulting image"
# (watch_design_approvals(), one field over, still 100% human).
# ============================================================================

# Mirrors note_triage.AUTO_NOTE_PREFIX, named a second time only because this
# module must not import note_triage at module scope (same lazy-import
# discipline as watch_design_revisions). The self-test canary below is what
# stops the two silently drifting apart, the same idiom ASSET_GEN_STATUS uses.
AUTO_NOTE_PREFIX = "[auto] "

ASSET_GEN_STATUS = "sg_gen_status"          # PHASE 11 -- see banner above
_ANCHOR_SEED_RE = re.compile(r"_s(\d+)$")   # matches a real candidate code's trailing _s<seed>


def _existing_anchor_seed_count(sg, asset_code, asset_id):
    """How many production ANCHOR seeds already exist for this Asset, read
    LIVE from ShotGrid Version codes -- rule 1 (query the source, not a
    local ledger that could be stale, lost, or simply never synced to this
    box). anima_anchor.candidate_code() always ends a real candidate's code
    in '_s<seed>'; wedge/test cells ('_negtext', '_TEXTWEDGE_') are excluded
    on purpose -- they are not a production seed count and must not shrink
    the number of NEW seeds this cycle asks for."""
    try:
        vs = sg.find("Version",
                    [["entity", "is", {"type": "Asset", "id": asset_id}],
                     ["code", "starts_with", "%s_ANCHOR_" % asset_code]],
                    ["code"])
    except Exception as exc:                                  # noqa: BLE001
        log("  render watcher: could not read %s's existing anchor Versions (%s); "
            "assuming zero so the render is not skipped" % (asset_code, type(exc).__name__))
        return 0
    seeds = set()
    for v in vs:
        code = v.get("code") or ""
        if "_negtext" in code or "_TEXTWEDGE_" in code:
            continue
        m = _ANCHOR_SEED_RE.search(code)
        if m:
            seeds.add(int(m.group(1)))
    return len(seeds)


def watch_design_renders(sg):
    """Asset.sg_gen_status == 'queued' -> re-render that Asset's design via
    anima_anchor.py (the SAME tool and recipe a human runs by hand -- Phase
    10's own docstring named this exact gap), publish the new candidate(s)
    at 'rev', reset the trigger. Returns True if any Asset was handled.

    Mirrors watch_animatic_requests()'s shape deliberately: query, claim,
    subprocess, resolve to a terminal status either way so a failure cannot
    spin forever (same discipline, same reason)."""
    try:
        queued = sg.find("Asset",
                         [["project", "is", PROJ], [ASSET_GEN_STATUS, "is", "queued"]],
                         ["id", "code"])
    except Exception as exc:
        log("  render watcher: Asset query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    did = False
    for a in queued:
        code = a["code"]
        # Fresh re-read immediately before claiming (same discipline
        # apply_for_asset() uses for its own eligibility check): a human or
        # another cycle may have already moved this off 'queued'.
        fresh = sg.find_one("Asset", [["id", "is", a["id"]]], [ASSET_GEN_STATUS])
        if not fresh or fresh.get(ASSET_GEN_STATUS) != "queued":
            continue
        sg.update("Asset", a["id"], {ASSET_GEN_STATUS: "rendering"})
        existing = _existing_anchor_seed_count(sg, code, a["id"])
        sys.path.insert(0, TOOLS)
        import anima_anchor as AA                              # noqa: E402  lazy, MIN_SEEDS only
        target_seeds = existing + AA.MIN_SEEDS
        log("design render requested for %s (Asset.%s=queued): %d existing seed(s) -> "
            "asking for %d (>= %d new)" % (code, ASSET_GEN_STATUS, existing, target_seeds, AA.MIN_SEEDS))
        r = run([PY, ANIMA_ANCHOR, "--generate", "--only", code, "--seeds", str(target_seeds)],
               timeout=7200, label="anima_anchor.py --only %s" % code)
        ok = bool(r and r.returncode == 0)
        new_count = _existing_anchor_seed_count(sg, code, a["id"]) if ok else existing
        if ok and new_count > existing:
            sg.update("Asset", a["id"], {ASSET_GEN_STATUS: "done"})
            try:
                new_vs = sg.find("Version",
                                 [["entity", "is", {"type": "Asset", "id": a["id"]}],
                                  ["code", "starts_with", "%s_ANCHOR_" % code]],
                                 ["code", "created_at"])
                new_vs = sorted(new_vs, key=lambda v: v.get("created_at") or "", reverse=True)
                names = [v["code"] for v in new_vs[: (new_count - existing) * 2]]
                # PIPELINE BOOKKEEPING IS NOT A NOTE. Geoff, 2026-09-07: "publishing should
                # not add notes. use description if something needs adding." A Note is a
                # request, or a human's record of review. The system announcing what it just
                # did is neither, and putting it in Notes filled the operator's review queue
                # with the pipeline talking to itself, which it then read back as work (F209:
                # 24 of 29 actionable notes were self-authored).
                #
                # The SIGNAL is the state field written just above, which the operator and
                # every watcher already read. Publish-time detail belongs on the VERSION
                # description, which is fresh and empty for every publish; Shot and Asset
                # descriptions are not in the publish path at all.
                pass
            except Exception as exc:                          # noqa: BLE001
                log("  render watcher: could not post the completion Note for %s (%s) "
                    "-- the render itself already landed in ShotGrid" % (code, exc))
            log("  %s -> %d new seed(s) published at rev (Asset.%s -> done)"
                % (code, new_count - existing, ASSET_GEN_STATUS))
        else:
            sg.update("Asset", a["id"], {ASSET_GEN_STATUS: "error"})
            log("  %s -> RENDER FAILED (Asset.%s -> error, will NOT retry until a new "
                "revision is applied): rc=%s" % (code, ASSET_GEN_STATUS, r.returncode if r else "timeout"))
        did = True
    return did

# ============================================================================
# END PHASE 11
# ============================================================================


# ============================================================================
# PHASE 12 -- PANEL COMPOSITION TRIGGER (2026-09-03, operator-mode finding).
# Of the eight watchers this service ran before today, none composed a
# panel -- panel_compose.py was a hand-run CLI (`--shot CODE`), the same
# "hand-run script" shape that made PILOT01 look automated. This closes the
# one remaining gap in the chain: design approved -> design revised ->
# PANEL COMPOSED (here) -> panel approved -> video -> assembled.
#
# THE TRIGGER, and why it is a Task status, not a new Shot field. Every
# other trigger in this file is a bespoke field because nothing else already
# named the state. Composition is different: episode_tasks.py already
# created a 'Panel' Task (step short_name PNL) on every Shot on this project,
# ShotGrid default status 'wtg' (Waiting to Start), and Task.sg_status_list's
# OWN valid values already include 'rdy' (Ready to Start) -- exactly the
# "this is now queued" meaning every other trigger here spells with a new
# field. An operator flips the Task the same way they already flip every
# other Step's Task on this project; no Shot-level twin of a Task the
# project already tracks was added. (Task status also happens to already
# carry 'rrq' -- Revision Requested -- which is not used here; the Panel
# stage's own revision path is a separate, later concern.)
#
# Shot.sg_gen_status='queued' is deliberately NOT reused for this trigger:
# that value already means "make the VIDEO from an approved panel" and
# watch_panel_video_queue() REFUSES it when no panel is approved --
# overloading it here would fire that refusal on a shot that was never
# asking for a video.
#
# THE REFUSAL panel_compose.py already owns (resolve_approved_design(): an
# Asset that is not sg_stage='approved' with sg_approved_design set) is kept
# LOUD, never routed around: this watcher runs panel_compose.py exactly as
# the CLI does and reads its own exit code / JSON result, so a refusal there
# is a refusal here. A refused/failed Task moves to 'hld' (On Hold) with an
# explanatory Note -- never silently back to 'wtg' (indistinguishable from
# "never asked"), and never left at 'rdy' (would retry forever).
#
# NO SELF-APPROVAL (invariant 7): panel_compose.py publishes every panel at
# Version.sg_status_list='rev', pass or fail (D16/W3 -- a vision verdict no
# longer withholds a Version from review). This watcher never touches
# Shot.sg_approved_panel; that stays watch_panel_approvals()'s job alone.
# On success the Task moves to 'rev' too (Pending Wangle Review) -- the same
# word the Version itself just published at, so the Task and the Version it
# produced agree without anyone having to open the Version to find out.
#
# LOOP CHECK: nothing else in this file writes Task.sg_status_list at all
# (grep confirms it -- panel_task() in panel_compose.py only finds-or-creates
# the Task, never sets its status). So 'rdy' can only ever be set by a human,
# and this watcher's own terminal states ('rev'/'hld') are never 'rdy', so it
# cannot re-select the Task it just finished on the next cycle.
# ============================================================================

PANEL_TASK_CONTENT = "Panel"
PANEL_STEP_SHORT_NAME = "PNL"   # mirrors panel_compose.py's own constant; canaried below


def reclaim_orphaned_claims(sg, log=log):
    """Hand every abandoned 'ip' Panel Task back to 'rdy'. -> how many.

    WHY THIS IS SAFE, and it rests entirely on the singleton lock. A Panel Task
    goes to 'ip' only in watch_panel_composition(), to stop an overlapping
    cycle claiming it twice, and it is resolved to 'rev' or 'hld' before that
    pass ends. So a Task at 'ip' means a service was mid-compose. This function
    runs at startup, AFTER acquire_singleton() has proved no other service is
    running -- therefore nothing is mid-compose, and every 'ip' is abandoned.
    Without the lock this would be a race; with it, it is a fact.

    WHY IT IS NEEDED. MEASURED 2026-09-04: the service claimed
    SHOW01_A_0140 at 23:43:09 and a deploy stopped it at 23:43:29, twenty
    seconds later. The Task stayed at 'ip' for the next half hour while seven
    other shots composed around it. Nothing retries 'ip' and no review page
    shows it, so the shot simply left the queue: not failed, not done, not
    visible. A deploy, a crash, a power cut or a reboot all produce this, which
    makes it a standing hazard rather than one bad afternoon -- and the more
    reliable the batch gets, the longer an orphan can hide inside it.

    It is deliberately LOUD even when it finds nothing to do at startup only
    when it does find work: silence is correct for the normal case, but a
    reclaimed Task means work was interrupted and somebody should know.
    """
    try:
        stuck = sg.find("Task",
                        [["content", "is", PANEL_TASK_CONTENT],
                         ["sg_status_list", "is", "ip"]],
                        ["id", "content", "entity"])
    except Exception as exc:                                      # noqa: BLE001
        log("  reclaim: could not query for abandoned claims (%s: %s) -- continuing"
            % (type(exc).__name__, str(exc)[:120]))
        return 0
    if not stuck:
        return 0
    log("RECLAIM: %d Panel Task(s) left at 'ip' by a stopped service. No service "
        "was running when this one started (singleton lock), so none of these is "
        "in progress -- handing them back to 'rdy'." % len(stuck))
    n = 0
    for t in stuck:
        name = (t.get("entity") or {}).get("name") or ("Task %s" % t["id"])
        try:
            sg.update("Task", t["id"], {"sg_status_list": "rdy"})
            log("  reclaimed %s (Task %s): ip -> rdy" % (name, t["id"]))
            n += 1
        except Exception as exc:                                  # noqa: BLE001
            log("  reclaim FAILED for %s (Task %s): %s: %s"
                % (name, t["id"], type(exc).__name__, str(exc)[:120]))
    return n


def reconcile_held_panels(sg):
    """A Panel Task marked REFUSED whose Shot has panel Versions published AFTER
    that verdict is a contradiction. Correct it, do not wait for a human.

    WHY, Geoff 2026-09-07: *"sounds like you are using manual bandaids rather
    than fixing pipeline?"* He was right. F189 fixed the CAUSE (a dropped socket
    on a confirmation read was recorded as a failed render), and then I repaired
    the one damaged Task by hand. That leaves the pipeline unable to recover from
    the same inconsistency if it ever arises another way, and a hand repair does
    not scale to the next occurrence.

    THE DISCRIMINATOR IS THE TIMESTAMP, and it is what makes this safe. A shot
    deliberately held (SHOW01_A_0480 was, after three identical failures) usually
    has OLD candidates sitting at 'rev'; flipping those back would resurface work
    a human parked on purpose. So this only fires when a panel Version was
    created AFTER the Task was last touched: that ordering can only mean the
    render finished and the verdict was written before or without seeing it."""
    try:
        # Exact content + step, matching watch_panel_composition()'s filter.
        # Review 2026-09-07: `content contains "Panel"` is currently harmless
        # (that is the only Task content in use) but it is a substring match with
        # no step guard, so a future QC or rename Task merely CONTAINING "Panel"
        # would be swept into auto-un-holding. Same defect shape as the episode
        # prefix filters: a wider population than the name implies.
        tasks = sg.find("Task", [["project", "is", PROJ],
                                 ["content", "is", PANEL_TASK_CONTENT],
                                 ["step.Step.short_name", "is", PANEL_STEP_SHORT_NAME],
                                 ["sg_status_list", "is", "hld"]],
                        ["id", "content", "entity", "updated_at"])
    except Exception as exc:                                      # noqa: BLE001
        log("  reconcile: Task query failed (%s), skipping this cycle" % type(exc).__name__)
        return False
    did = False
    for t in tasks:
        shot = t.get("entity") or {}
        if not shot.get("id"):
            continue
        try:
            vs = sg.find("Version", [["entity", "is", shot],
                                     ["sg_stage", "is", "panel"],
                                     ["sg_status_list", "is", "rev"],
                                     ["created_at", "greater_than", t["updated_at"]]],
                         ["code", "created_at"])
        except Exception as exc:                                  # noqa: BLE001
            log("  reconcile: Version query failed for %s (%s)"
                % (shot.get("name"), type(exc).__name__))
            continue
        if not vs:
            continue
        sg.update("Task", t["id"], {"sg_status_list": "rev"})
        log("  reconcile: %s was held as REFUSED but %d panel Version(s) were published "
            "AFTER that verdict (%s). Correcting the Task to 'rev' -- the render did not "
            "fail, we failed to see it."
            % (shot.get("name"), len(vs), ", ".join(v["code"] for v in vs[:3])))
        did = True
    return did


# ============================================================================
# A REFUSAL THE OPERATOR CAN SEE.
#
# Geoff, 2026-09-08: *"nobody opens tasks or reads descriptions of tasks, only
# tasks status. task status can be displayed in shot or version lists. The
# visible thing would be to change the panel task status to hold, and the
# offending version's status to a new Prompt Refused, and a note (with status
# Under Revision, ignored by claude -p) on that version."*
#
# MEASURED, and it is why this exists. On 2026-09-08 panel_compose.py refused
# SHOW01_A_0060 ("3 CHAR assets linked ... Regional composition is proven for
# two and measured to FAIL for three"), the Panel Task went to 'hld', and the
# REASON existed only in the service log on the scratch disk. Nothing in
# ShotGrid said why, so from the operator's side the shot simply stopped. A
# held Task with no reason is the same shape as F189: a state the operator can
# see and cannot act on.
#
# THREE COORDINATED WRITES, and none of them is a Task description:
#   1. Panel Task           -> 'hld'   (the CALLER does this, unchanged)
#   2. the request Version  -> 'prf'   ("Prompt Refused", site Status id 331)
#   3. a Note on that Version at 'urr' ("Under Revision") carrying the reason
#      IN FULL, not the 200 characters the log line keeps.
#
# THE NOTE MUST NOT RE-ENTER THE REQUEST CHANNEL, and that is the whole reason
# for the 'urr' status. F209: 24 of 29 "actionable" notes on this project were
# the pipeline talking to itself, and note-posting FROM THIS EXACT PATH was
# deleted because of it. Three independent guards now, any one sufficient:
#   - the subject carries AUTO_NOTE_PREFIX, which note_triage.is_pipeline_record
#     already silences;
#   - the Note's own status is 'urr', and note_triage.ACTIONABLE_NOTE_STATUSES
#     is ("opn",), so neither is_actionable() nor revision_requested() selects
#     it. That whitelist was ADDED for this change: the old rule blocked only
#     'clsd', so a 'urr' note WOULD have been actionable.
#   - the Version it hangs off ends at 'prf', which is in neither
#     LIVE_VERSION_STATUSES nor APPROVED_VERSION_STATUSES.
# ============================================================================

REFUSED_VERSION_STATUS = "prf"      # "Prompt Refused", site Status id 331
REFUSAL_NOTE_STATUS = "urr"         # "Under Revision"
REQUEST_VERSION_STATUS = "rrq"      # "Revision Requested" -- the operator's ask

# Mirrors note_triage.APPROVED_VERSION_STATUSES, named a second time for the
# same reason AUTO_NOTE_PREFIX is (this module must not import note_triage at
# module scope). The self-test canary below is what stops the two drifting.
DECIDED_VERSION_STATUSES = ("apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra")

# A VERSION AWAITING ITS FIRST REVIEW DID NOT FAIL, SO ITS STATUS IS NOT OURS
# TO TAKE. Found by review, and it is live: 195 panel Versions in this project
# sit at 'rev', and 'rev' is the ONLY status `sg_review_housekeeping.py` sweeps
# (SWEEPABLE = ("rev",)). Stamping one 'prf' would say "the prompt was refused"
# about a panel that rendered perfectly well and had simply not been looked at
# yet, AND drop it out of the review population for good.
#
# It is still the right place to HANG THE REASON -- it is the picture on the
# operator's screen -- so it stays a Note target and loses only the status
# write. Visibility without relabelling somebody else's work.
NOTE_ONLY_VERSION_STATUSES = ("rev",)

# THE STAGE THIS WATCHER IS ABOUT. 'rrq' is a CROSS-STAGE status: 26 Versions
# carry it in this project, of which 14 are panels, 7 keyframes, 1 video and 4
# unstaged, and SHOW01_A_0020 carries one on its panel AND one on its video at
# the same time. A PANEL refusal that grabbed "the newest Version at 'rrq'"
# would therefore mark an unrelated, open VIDEO revision request 'prf'. Found
# by review before it ever ran.
PANEL_STAGE = "panel"


def _refusal_target_version(sg, shot_id, code):
    """-> the Version this refusal ANSWERS, or None.

    THE OFFENDING VERSION IS THE ONE CARRYING THE OPERATOR'S REQUEST: the newest
    Version on this Shot at 'rrq'. That is the picture they were looking at when
    they asked for the change this compose was supposed to deliver.

    The fallback, the newest panel-stage Version, covers a refusal that no note
    triggered at all (a hand-edited beat, a requeue, a fresh shot), where there
    is still a picture on screen with nothing said about it.

    THREE THINGS IT WILL NOT DO, all because a wrong target is worse than none:
      - it never leaves this stage. BOTH branches filter to panel-stage
        Versions, not just the fallback. 'rrq' is a cross-stage status (26 live
        Versions carry it: 14 panels, 7 keyframes, 1 video, 4 unstaged), and
        SHOW01_A_0020 carries one on its panel and one on its video at once, so
        "the newest Version at 'rrq'" would let a PANEL refusal mark an open
        VIDEO revision request 'prf'.
      - it never invents one. No panel-stage Version at all -> None, and the
        caller says so in the log.
      - the FALLBACK never picks a DECIDED Version. Stamping 'prf' over an
        approved panel would retract an approval in order to report an unrelated
        failure, and the invalidation cascade reads those statuses. An 'rrq'
        Version is exempt on purpose: the operator has already reopened it.

    A Version at 'rev' IS returned, but the caller must not restamp it; see
    NOTE_ONLY_VERSION_STATUSES."""
    try:
        vs = sg.find("Version", [["entity", "is", {"type": "Shot", "id": shot_id}]],
                     ["id", "code", "sg_stage", "sg_status_list", "created_at"])
    except Exception as exc:                                      # noqa: BLE001
        log("  panel-composition watcher: could not read %s's Versions to record the "
            "refusal (%s)" % (code, type(exc).__name__))
        return None

    def newest(pool):
        # created_at can be missing on a hand-made Version, and None does not
        # compare with a datetime -- a bare sort on it raises TypeError and
        # would take the cycle down inside the error path. The leading boolean
        # keeps the two populations apart so the comparison never crosses them;
        # the id is the tie-break, and ShotGrid ids are monotonic.
        if not pool:
            return None
        return sorted(pool, key=lambda v: (v.get("created_at") is not None,
                                           v.get("created_at"), v["id"]))[-1]

    panels = [v for v in vs if (v.get("sg_stage") or "") == PANEL_STAGE]
    asked = newest([v for v in panels
                    if v.get("sg_status_list") == REQUEST_VERSION_STATUS])
    if asked:
        return asked
    return newest([v for v in panels
                   if v.get("sg_status_list") not in DECIDED_VERSION_STATUSES])


def record_panel_refusal(sg, shot, code, reason):
    """Make a panel-composition refusal VISIBLE in ShotGrid.
    -> the Version id marked 'prf', or None when there was nothing to mark.

    NEVER RAISES, and that is load-bearing. The Panel Task hold is written by
    the caller BEFORE this runs and must stand whatever happens in here; this
    module lost a whole pass today to a missing exception boundary. Each write
    is caught SEPARATELY too, so a failed Version update still leaves the
    operator the Note that says why, and a failed Note still leaves the status."""
    target = None
    try:
        # NOT A DEAD HANDLER, though a reviewer read it as one because
        # _refusal_target_version() catches its own sg.find. What reaches here
        # is everything OUTSIDE that inner try: a missing shot["id"], and the
        # sort in newest(), which is the line most likely to throw on real data.
        target = _refusal_target_version(sg, shot["id"], code)
    except Exception as exc:                                      # noqa: BLE001
        log("  panel-composition watcher: refusal target lookup failed for %s (%s)"
            % (code, exc))
    if not target:
        # DO NOT NAME A CAUSE THIS FUNCTION CANNOT TELL APART. "No Version at
        # 'rrq'" and "the Version query threw" arrive here identically, and the
        # first version of this line asserted the former for both -- a lookup
        # failure reported as an empty result is the exact shape of a silent
        # failure. The lookup logs its own error immediately above when that is
        # what happened, so this line points at it instead of guessing.
        log("  %s: refusal NOT recorded on a Version -- no target was resolved "
            "(no Version at '%s', no undecided panel-stage Version, or the "
            "lookup failed; any lookup error is logged immediately above). The "
            "Panel Task hold stands and the reason is in this log only."
            % (code, REQUEST_VERSION_STATUS))
        return None

    if (target.get("sg_status_list") or "") in NOTE_ONLY_VERSION_STATUSES:
        # THE NOTE STILL LANDS, THE STATUS DOES NOT MOVE. See
        # NOTE_ONLY_VERSION_STATUSES: this Version is waiting to be looked at,
        # not refused, and relabelling it would both lie and remove it from
        # sg_review_housekeeping's sweep.
        log("  %s: Version %s (%s) is at '%s' and is left there -- it was never "
            "refused, it is simply unreviewed. The reason goes on it as a Note "
            "and the Panel Task hold carries the signal."
            % (code, target["id"], target.get("code"),
               target.get("sg_status_list")))
    else:
        try:
            sg.update("Version", target["id"],
                      {"sg_status_list": REFUSED_VERSION_STATUS})
            log("  %s: Version %s (%s) -> '%s'"
                % (code, target["id"], target.get("code"), REFUSED_VERSION_STATUS))
        except Exception as exc:                                  # noqa: BLE001
            log("  panel-composition watcher: could not set %s's Version %s to "
                "'%s' (%s)" % (code, target["id"], REFUSED_VERSION_STATUS, exc))

    subject = "%sPanel composition refused for %s" % (AUTO_NOTE_PREFIX, code)
    # SAY WHAT IS ACTUALLY TRUE OF THIS VERSION. The body used to assert
    # "the Version above is at 'prf'" unconditionally, which becomes a false
    # statement the moment the NOTE_ONLY branch above declines to move it.
    if (target.get("sg_status_list") or "") in NOTE_ONLY_VERSION_STATUSES:
        _state = ("The Panel Task is on hold. This Version's own status was "
                  "left untouched: it is awaiting review, it is not what was "
                  "refused.")
    else:
        _state = ("The Panel Task is on hold and this Version is at '%s' "
                  "(Prompt Refused)." % REFUSED_VERSION_STATUS)
    body = ("panel_compose.py refused to compose %s.\n\n%s\n\n"
            "%s This Note is the pipeline's own record, not a request: it is at "
            "'%s' (Under Revision) and its subject carries the '%s' prefix, so "
            "note triage and the proposer both ignore it. Fix the shot's "
            "inputs, then set the Panel Task back to 'rdy'."
            % (code, reason, _state, REFUSAL_NOTE_STATUS,
               AUTO_NOTE_PREFIX.strip()))
    try:
        # ONE NOTE PER VERSION, UPDATED IN PLACE. A held shot can be retried by
        # hand and refuse again; creating a fresh Note every time would rebuild
        # the review queue this path was deleted for filling. Same idiom as
        # publish_test_versions.py's report Note.
        existing = sg.find_one("Note",
                               [["project", "is", PROJ], ["subject", "is", subject],
                                ["note_links", "in",
                                 [{"type": "Version", "id": target["id"]}]]],
                               ["id"])
        if existing:
            sg.update("Note", existing["id"],
                      {"content": body, "sg_status_list": REFUSAL_NOTE_STATUS})
            log("  %s: refusal Note %s updated on Version %s (status '%s')"
                % (code, existing["id"], target["id"], REFUSAL_NOTE_STATUS))
        else:
            note = sg.create("Note", {
                "project": PROJ, "subject": subject, "content": body,
                "note_links": [{"type": "Version", "id": target["id"]}],
                "sg_status_list": REFUSAL_NOTE_STATUS})
            log("  %s: refusal Note %s posted on Version %s at '%s'"
                % (code, note["id"], target["id"], REFUSAL_NOTE_STATUS))
    except Exception as exc:                                      # noqa: BLE001
        log("  panel-composition watcher: could not post the refusal Note for %s (%s)"
            % (code, exc))
    return target["id"]


def watch_panel_composition(sg):
    """Task(content='Panel', step.short_name='PNL').sg_status_list == 'rdy'
    -> compose that shot's panel via panel_compose.py, reset the Task to
    'rev' (published) or 'hld' (refused/failed, with a Note explaining why).
    Returns True if any Task was handled."""
    try:
        tasks = sg.find("Task",
                        [["project", "is", PROJ], ["content", "is", PANEL_TASK_CONTENT],
                         ["step.Step.short_name", "is", PANEL_STEP_SHORT_NAME],
                         ["sg_status_list", "is", "rdy"]],
                        ["id", "entity", "sg_status_list"])
    except Exception as exc:
        log("  panel-composition watcher: Task query failed (%s), skipping this cycle"
            % type(exc).__name__)
        return False
    did = False
    for idx, t in enumerate(tasks):
        ent = t.get("entity") or {}
        if ent.get("type") != "Shot" or not ent.get("id"):
            continue
        # Fresh re-read immediately before claiming -- same discipline as
        # every other trigger in this file.
        fresh = sg.find_one("Task", [["id", "is", t["id"]]], ["sg_status_list"])
        if not fresh or fresh.get("sg_status_list") != "rdy":
            continue
        shot = sg.find_one("Shot", [["id", "is", ent["id"]]], ["code"])
        if not shot:
            log("  panel-composition watcher: Task %s's Shot no longer exists, skipping" % t["id"])
            continue
        code = shot["code"]
        # BRING THE COMPOSITOR UP BEFORE CLAIMING ANYTHING.
        #
        # panel_compose.py deliberately does not start ComfyUI, and
        # video_from_panel.py tears it down after every video (invariant 4,
        # Geoff's standing rule). Nothing owned the gap between those two
        # facts, so rendering a video silently disarmed the panel queue.
        #
        # The check sits BEFORE the rdy -> ip claim, and that ordering is the
        # whole point: claiming a Task and then declining to dispatch leaves it
        # at 'ip', which no watcher retries and no operator is looking for. The
        # first version of this guard did exactly that and the self-test caught
        # it. Leave the queue exactly as it was found.
        if not ensure_comfy():
            log("  %s: ComfyUI is down and could not be started -- leaving %d queued "
                "shot(s) untouched at 'rdy'; they compose when it is back"
                % (code, len(tasks) - idx))
            return did

        log("panel composition requested for %s (Task %s -> rdy)" % (code, t["id"]))
        # Claim immediately (rdy -> ip) so an overlapping cycle cannot pick
        # the same Task up twice while the compose call (below) is running.
        sg.update("Task", t["id"], {"sg_status_list": "ip"})

        # BASELINE, before the call: which panel Versions for this shot
        # already exist. rule 1 -- the ground truth for "did this publish" is
        # ShotGrid, never the subprocess's own stdout. (panel_compose.py's
        # log() writes its diagnostic lines to STDOUT, same stream as the
        # CLI's final json.dumps(result) -- parsing r.stdout as one JSON
        # document therefore FAILS on every real run, success or not, and a
        # naive `json.loads(r.stdout)` misreports every success as a refusal.
        # Caught by running this against the real CLI, not by inspection.)
        try:
            before_ids = {v["id"] for v in sg.find(
                "Version", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                           ["code", "starts_with", "%s_PNL_panel" % code]], ["id"])}
        except Exception:                                      # noqa: BLE001
            before_ids = set()

        r = run([PY, PANEL_COMPOSE, "--shot", code], timeout=1800,
               label="panel_compose.py --shot %s" % code)

        # CONFIRMATION IS A READ, AND A FAILED READ IS NOT A FAILED RENDER.
        # Measured 2026-09-07: SHOW01_A_0060 composed for ten minutes, published
        # v009 to v013 at 01:01:04-01:01:16, and a ConnectionAbortedError on THIS
        # query five seconds later sent the Task to 'hld' as refused. The work was
        # sitting in ShotGrid, done, while the pipeline recorded a failure and the
        # operator saw a held shot. Ten minutes of GPU discarded by a dropped
        # socket.
        #
        # So: retry, and if it still will not answer, say UNKNOWN rather than
        # failed. `confirmed` is what separates "the render failed" from "we could
        # not look", which the old code collapsed into one outcome.
        after, confirmed = [], False
        for attempt in range(3):
            try:
                after = sg.find("Version", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                                            ["code", "starts_with", "%s_PNL_panel" % code]],
                                ["id", "code"])
                confirmed = True
                break
            except Exception as exc:                            # noqa: BLE001
                log("  panel-composition watcher: post-compose Version query failed for %s "
                    "(%s), attempt %d of 3" % (code, type(exc).__name__, attempt + 1))
                time.sleep(2 * (attempt + 1))
        new_versions = [v for v in after if v["id"] not in before_ids]
        if not confirmed:
            # HAND THE CLAIM BACK, exactly as the infra-failure branch below does.
            # CAUGHT BY REVIEW 2026-09-07, and my first version of this fix was
            # WORSE than the bug it replaced. It said "leave the Task untouched",
            # but the Task was claimed rdy -> ip at the top of this same call, so
            # "untouched" meant LEFT AT 'ip', and nothing retries 'ip':
            # watch_panel_composition() selects only 'rdy', reconcile_held_panels()
            # selects only 'hld', and reclaim_orphaned_claims() runs once at
            # service STARTUP behind the singleton lock, which a standing process
            # never reaches. So a blip that outlasted 12 seconds of retry parked
            # the shot silently and forever, with no Note and no operator signal.
            # F189's failure was at least visible as 'hld'.
            sg.update("Task", t["id"], {"sg_status_list": "rdy"})
            log("  %s: COULD NOT CONFIRM publication after 3 tries. The render may have "
                "SUCCEEDED, so this is NOT recorded as a failure. Panel Task handed back "
                "to 'rdy'; the next cycle re-reads and settles it." % code)
            return did

        if r and r.returncode == 0 and new_versions:
            sg.update("Task", t["id"], {"sg_status_list": "rev"})
            log("  %s -> published %s (Task -> rev)"
               % (code, ", ".join(v["code"] for v in new_versions)))
            did = True
            # ONE SHOT PER CYCLE, so the other eleven watchers get a turn.
            #
            # This loop used to drain every 'rdy' Task in a single pass. With
            # the 8-seed wedge a two-character shot takes ~8 minutes, so four
            # queued shots monopolised the cycle for over half an hour and
            # NOTHING else ran: measured 2026-09-05, a panel approved at 09:12
            # still had no sg_approved_panel at 09:28 because
            # watch_panel_approvals had not been reached. From the operator's
            # side that is "I approved it and nothing happened", which is the
            # worst thing a state-driven pipeline can look like.
            #
            # Throughput barely changes -- the compose dominates either way --
            # but approvals, notes and housekeeping now interleave between
            # shots instead of waiting for the whole queue.
            return did
        elif is_content_failure(r):
            # FLAKY, NOT PERMANENT, and MEASURED: SHOW01_A_0170's beat split
            # failed seven times and SUCCEEDED on the eighth. It is an LLM call,
            # so it is non-deterministic, and my first fix for this (hold the
            # shot on the first content failure) would have thrown that success
            # away. Corrected before it ever deployed.
            #
            # So the harm was never the retry. It was that a content failure
            # took the INFRA branch, which STOPS THE PASS: six other queued
            # shots waited 44 minutes behind one bad beat for no reason.
            #
            # Hand the claim back so it can be retried, and CONTINUE to the next
            # shot. An outage stops the pass because it will hit everything; a
            # bad beat will not.
            sg.update("Task", t["id"], {"sg_status_list": "rdy"})
            log("  %s: %s -- content failure, Panel Task handed back to 'rdy'. "
                "CONTINUING to the next shot; this one is not an outage and must "
                "not hold the queue." % (code, failure_reason(r)))
            did = True
            continue
        elif is_infra_failure(r):
            # NOT THIS SHOT'S FAULT, AND IT WILL NOT BE THE NEXT ONE'S EITHER.
            # Leave the Task at 'rdy' so the queue survives, say so once, and
            # stop the pass -- every remaining shot would fail identically and
            # be marked 'hld' for a reason that has nothing to do with it.
            # HAND THE CLAIM BACK. This shot was already claimed rdy -> ip, and
            # 'ip' is a state nothing retries; the queue would drain one shot
            # per outage instead of surviving it.
            sg.update("Task", t["id"], {"sg_status_list": "rdy"})
            log("  %s: %s -- Panel Task handed back to 'rdy' and STOPPING this "
                "pass; %d further queued shot(s) untouched, they compose when the "
                "compositor is back"
                % (code, failure_reason(r), max(0, len(tasks) - (idx + 1))))
            return did
        else:
            # THE NOTE GETS THE WHOLE REASON. The 1500-character cap and the
            # 200-character log slice below are both about keeping the LOG
            # readable; a refusal the operator has to act on must not arrive
            # with its explanation cut off mid-sentence.
            reason_full = str(failure_reason(
                r, fallback="panel_compose.py refused or failed"))
            reason = reason_full[:1500]
            sg.update("Task", t["id"], {"sg_status_list": "hld"})
            # PIPELINE BOOKKEEPING IS NOT A NOTE, and this is the ONE exception,
            # granted by name. Geoff, 2026-09-07: "publishing should not add
            # notes" -- a PUBLISH is the system announcing what it did, and that
            # belongs in a status field. Geoff, 2026-09-08, on a REFUSAL: "the
            # visible thing would be to change the panel task status to hold, and
            # the offending version's status to a new Prompt Refused, and a note
            # (with status Under Revision, ignored by claude -p) on that version."
            # A refusal has a REASON, a status field cannot carry one, and the
            # operator cannot act on "held" alone. The 'urr' status is what keeps
            # it out of the request channel; see record_panel_refusal() above.
            record_panel_refusal(sg, shot, code, reason_full)
            log("  %s -> REFUSED/FAILED (Task -> hld): %s" % (code, reason[:200]))
        did = True
    return did

# ============================================================================
# END PHASE 12
# ============================================================================

# ============================================================================
# PHASE 13: HAND-EDIT DETECTION. A human (or any script other than this
# pipeline's own) edits sg_action_beat / sg_shot_size / sg_camera /
# sg_gen_prompt directly in ShotGrid -> that edit IS the trigger. Today the
# operator must also flip a Panel Task to 'rdy' or Shot.sg_gen_status to
# 'queued' by hand for anything to happen; Geoff calls that a defect.
#
# See docs/HAND-EDIT-DETECTION-DESIGN-2026-09-08.md for the measurement that
# decided this. Read-only query against EventLogEntry
# (Shotgun_Shot_Change, attribute_name in the four fields below) found
# 622 historical writes to this project's Shots on exactly these fields,
# and EVERY ONE of them carried user.type == 'ApiUser',
# user.name == OUR_OWN_API_USER_NAME (this pipeline's own script identity).
# Zero carried a HumanUser. That is the population this watcher excludes;
# everything else (a HumanUser, or a different ApiUser such as an
# operator's MCP-driven proxy) is treated as an external, triggering edit.
# ============================================================================

HAND_EDIT_PANEL_FIELDS = ("sg_action_beat", "sg_shot_size", "sg_camera")
HAND_EDIT_VIDEO_FIELDS = ("sg_gen_prompt",)
HAND_EDIT_FIELDS = HAND_EDIT_PANEL_FIELDS + HAND_EDIT_VIDEO_FIELDS

# The one script identity that ever writes these fields as the pipeline
# itself, measured 622/622. NOT "any ApiUser" -- the measured population
# already contains other ApiUsers ("mcp-server 1.0", "HAL9000 1.0") that are
# NOT this pipeline, and an edit from one of those must trigger like a
# human's would.
OUR_OWN_API_USER_NAME = "genvideo 1.0"

# The blast radius of one cycle. See the refusal in watch_hand_edits().
HAND_EDIT_MAX_PER_CYCLE = 8

# NOT on C: (durability-assume-reboot: a scratch-disk marker re-arms at zero
# on a disk clean and replays the whole EventLogEntry history as if every
# row were new, re-triggering GPU work that already ran). Same ROOT and the
# same build/out/ convention as PANEL_DESIGNS_JSON above.
HAND_EDIT_CURSOR_JSON = os.path.join(ROOT, "build", "out", "hand_edit_cursor.json")


def _load_hand_edit_cursor(path=None):
    """-> the last EventLogEntry id this watcher has already processed, or 0
    if the cursor file does not exist yet (first run) or is unreadable."""
    p = path or HAND_EDIT_CURSOR_JSON
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception as exc:                                      # noqa: BLE001
        log("  hand-edit watcher: cursor at %s is unreadable (%s), starting "
            "from 0 -- this REPLAYS history once rather than silently "
            "skipping it" % (p, type(exc).__name__))
        return 0
    last_id = data.get("last_id") if isinstance(data, dict) else None
    return last_id if isinstance(last_id, int) else 0


def _save_hand_edit_cursor(last_id, path=None):
    p = path or HAND_EDIT_CURSOR_JSON
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"last_id": last_id}, f)
        os.replace(tmp, p)
    except Exception as exc:                                      # noqa: BLE001
        log("  hand-edit watcher: could not persist cursor (%s: %s)"
            % (type(exc).__name__, str(exc)[:120]))


def watch_hand_edits(sg, cursor_path=None):
    """EventLogEntry(event_type='Shotgun_Shot_Change',
    attribute_name in HAND_EDIT_FIELDS, id > cursor) -> for every row NOT
    written by OUR_OWN_API_USER_NAME:
      - sg_action_beat/sg_shot_size/sg_camera: un-approve that shot's panel
        (Version 'apr' -> 'rev', Shot.sg_approved_panel cleared) and set its
        Panel Task to 'rdy' -- the exact trigger watch_panel_composition()
        already consumes, and the same un-approve step
        invalidate_stale_panels.invalidate() performs for a stale design, so
        a hand-edited shot behaves identically to a design-invalidated one.
      - sg_gen_prompt: Shot.sg_gen_status -> 'queued', the trigger
        watch_panel_video_queue() already consumes (same field
        watch_stale_videos() uses). If there is no approved panel yet,
        video_from_panel.py's own existing refusal path handles it.

    Never reads Task or Version status to DETECT a change -- both are
    written here only as the EFFECT, matching Geoff's standing rule that
    nothing should watch task/version status for this. Degrades to False on
    any ShotGrid fault, never raises. Returns True if anything was queued."""
    last_id = _load_hand_edit_cursor(cursor_path)
    try:
        rows = sg.find(
            "EventLogEntry",
            [["project", "is", PROJ], ["event_type", "is", "Shotgun_Shot_Change"],
             ["attribute_name", "in", list(HAND_EDIT_FIELDS)],
             ["id", "greater_than", last_id]],
            ["id", "attribute_name", "entity", "user"],
            order=[{"field_name": "id", "direction": "asc"}],
        )
    except Exception as exc:                                      # noqa: BLE001
        log("  hand-edit watcher: EventLogEntry query failed (%s), skipping "
            "this cycle" % type(exc).__name__)
        return False
    if not rows:
        return False

    max_id = last_id
    panel_shots = {}
    video_shots = {}
    for r in rows:
        if isinstance(r.get("id"), int):
            max_id = max(max_id, r["id"])
        ent = r.get("entity") or {}
        if ent.get("type") != "Shot" or not ent.get("id"):
            continue
        user = r.get("user") or {}
        if user.get("type") == "ApiUser" and user.get("name") == OUR_OWN_API_USER_NAME:
            continue          # our own write; not a hand edit
        field = r.get("attribute_name")
        if field in HAND_EDIT_PANEL_FIELDS:
            panel_shots[ent["id"]] = ent.get("name")
        elif field in HAND_EDIT_VIDEO_FIELDS:
            video_shots[ent["id"]] = ent.get("name")

    # A HELD EPISODE IS NOT WORK, and a cold cursor REPLAYS ALL HISTORY, so
    # without this a dead-episode row could requeue a PILOT01 shot for a GPU
    # pass. Every other watcher already honours this; the beat watcher not
    # honouring it was F357, and it read the retired episode for the whole
    # project before anyone noticed.
    held = held_episode_ids(sg)

    # A CAP, BECAUSE THE EFFECT IS DESTRUCTIVE AND THE INPUT IS NOT RATE
    # LIMITED. A human edits shots one at a time, but a ShotGrid bulk edit or
    # a CSV import writes hundreds of rows in one second, and each panel row
    # here UN-APPROVES a panel and queues a recompose. Refusing loudly beats
    # discarding the approvals of a whole episode on one mis-click; the cursor
    # is NOT advanced when we refuse, so nothing is lost and the next cycle
    # sees the same rows.
    if len(panel_shots) + len(video_shots) > HAND_EDIT_MAX_PER_CYCLE:
        log("hand-edit watcher: REFUSING %d shot(s) of hand edits in one cycle "
            "(cap %d). This looks like a bulk edit, not hand editing. Nothing "
            "was un-approved and the cursor was NOT advanced. Raise "
            "HAND_EDIT_MAX_PER_CYCLE or clear the backlog deliberately."
            % (len(panel_shots) + len(video_shots), HAND_EDIT_MAX_PER_CYCLE))
        return False

    did = False
    failed = []            # shot ids whose requeue did not complete this cycle
    for sid, name in panel_shots.items():
        try:
            shot = sg.find_one("Shot", [["id", "is", sid]],
                               ["code", "sg_approved_panel", "sg_episode"])
            if not shot:
                continue
            if (shot.get("sg_episode") or {}).get("id") in held:
                continue
            protecting = sg.find_one("Version",
                                     [["entity", "is", {"type": "Shot", "id": sid}],
                                      SHOT_LOCK.version_filter()],
                                     ["code", "sg_status_list"])
            if SHOT_LOCK.is_protected([protecting] if protecting else []):
                log("hand edit: " + SHOT_LOCK.refusal(
                    shot.get("code") or name, "hand-edit watcher (panel field)",
                    version=protecting))
                continue
            ap = shot.get("sg_approved_panel")
            task = sg.find_one(
                "Task",
                [["entity", "is", {"type": "Shot", "id": sid}],
                 ["content", "is", PANEL_TASK_CONTENT],
                 ["step.Step.short_name", "is", PANEL_STEP_SHORT_NAME]],
                ["sg_status_list"])
            acted = False
            if isinstance(ap, dict):
                sg.update("Version", ap["id"], {"sg_status_list": "rev"})
                sg.update("Shot", sid, {"sg_approved_panel": None})
                acted = True
            if task and task.get("sg_status_list") != "rdy":
                sg.update("Task", task["id"], {"sg_status_list": "rdy"})
                acted = True
            if acted:
                log("hand edit: %s (a panel field changed by a non-pipeline "
                    "write) -- un-approved and requeued for recompose"
                    % (shot.get("code") or name))
                did = True
        except Exception as exc:                                  # noqa: BLE001
            failed.append(sid)
            log("  hand-edit watcher: could not requeue panel for Shot id %s "
                "(%s: %s), continuing" % (sid, type(exc).__name__, str(exc)[:120]))

    for sid, name in video_shots.items():
        try:
            shot = sg.find_one("Shot", [["id", "is", sid]],
                               ["code", "sg_gen_status", "sg_episode"])
            if not shot:
                continue
            if (shot.get("sg_episode") or {}).get("id") in held:
                continue
            protecting = sg.find_one("Version",
                                     [["entity", "is", {"type": "Shot", "id": sid}],
                                      SHOT_LOCK.version_filter()],
                                     ["code", "sg_status_list"])
            if SHOT_LOCK.is_protected([protecting] if protecting else []):
                log("hand edit: " + SHOT_LOCK.refusal(
                    shot.get("code") or name, "hand-edit watcher (video field)",
                    version=protecting))
                continue
            if shot.get("sg_gen_status") != "queued":
                sg.update("Shot", sid, {"sg_gen_status": "queued"})
                log("hand edit: %s (sg_gen_prompt changed by a non-pipeline "
                    "write) -- queued for a new video"
                    % (shot.get("code") or name))
                did = True
        except Exception as exc:                                  # noqa: BLE001
            failed.append(sid)
            log("  hand-edit watcher: could not queue video for Shot id %s "
                "(%s: %s), continuing" % (sid, type(exc).__name__, str(exc)[:120]))

    # THE CURSOR IS A CLAIM THAT THE WORK WAS DONE, so a batch with a failure
    # in it does not get to make that claim. Advancing unconditionally would
    # move past the very rows we just failed to act on, and a transient
    # ShotGrid fault (a locked record, a network blip) would then be
    # indistinguishable from success forever: the operator's hand edit simply
    # never happens, with one buried log line as the only trace.
    #
    # Holding is safe because a replay is a no-op: an already un-approved panel
    # has sg_approved_panel None and a Task already at 'rdy' produces no
    # update, so the shots that DID succeed do nothing on the retry.
    #
    # The trade-off, stated rather than hidden: a PERMANENT failure on one shot
    # wedges the cursor and blocks later hand edits behind it. That is
    # deliberate. A stuck state that says so loudly every cycle is better than
    # one that silently drops the operator's requests, which is the failure
    # this whole watcher exists to end.
    if failed:
        log("  hand-edit watcher: %d shot(s) failed to requeue (%s). HOLDING "
            "the cursor at %d so they are retried; nothing after them is "
            "processed until they clear."
            % (len(failed), ", ".join(str(i) for i in failed[:6]), last_id))
        return did
    _save_hand_edit_cursor(max_id, cursor_path)
    return did

# ============================================================================
# END PHASE 13
# ============================================================================


# ============================================================================
# PHASE 14: PROVENANCE VS SENT (F461)
# ============================================================================
#
# tools/provenance_vs_sent.py compares what a Version's provenance CLAIMS was
# sent against the prompt actually sent (sg_prompt_final__as_sent_). That
# divergence is this project's single most repeated defect -- F310, F405,
# F445 (the last DECLARED CLOSED TWICE while it was still live) -- and until
# now the tool had ZERO callers: the only mention of it anywhere outside its
# own file was a comment in panel_compose.py. It passed its self-test on
# every deploy, which is exactly why nobody noticed it was not running.
#
# THREE CONSTRAINTS FROM THE BRIEF, and they are more important than the
# wiring itself:
#
#   1. NEVER BLOCKS OR FAILS A PUBLISH. A provenance mismatch is a reporting
#      defect, not a reason to throw away a rendered image. watch_provenance()
#      below reads run()'s findings and writes Notes; it never touches
#      sg_gen_status, a Task, or a Version's OWN sg_status_list, and it never
#      raises out of cycle() (every ShotGrid call is inside its own try).
#   2. CHEAP ENOUGH FOR EVERY CYCLE. provenance_vs_sent.find_disagreements()
#      re-queries and re-diffs every Version in an episode matching the code
#      prefix -- a full sweep, not scoped to a stage or a time window on its
#      own. Re-diffing the WHOLE SHOW'S HISTORY on every tick (cycle() runs on
#      the order of seconds) is wasted CPU, and worse, a wasted ShotGrid write
#      every cycle to the same Note. So this watcher is SCOPED TO VERSIONS
#      PUBLISHED SINCE THE LAST CHECK, using the exact cursor idiom
#      HAND_EDIT_CURSOR_JSON already established above: a small JSON file,
#      atomic write, a corrupt or missing cursor means "scan from the
#      beginning" rather than a crash. Chosen over a slower fixed cadence (the
#      brief's other option) because it stays maximally responsive -- a
#      mismatch is visible the cycle after the Version that has it publishes,
#      not after an arbitrary N-cycle wait -- and because the cost it is
#      solving is PROPORTIONAL TO NEW WORK, which a cursor tracks exactly and
#      a fixed N does not.
#   3. VISIBLE TO AN OPERATOR, NOT ONLY A LOG LINE (the brief's own complaint
#      about F461). The existing pattern for a non-blocking, per-Version
#      pipeline finding is record_panel_refusal()'s Note idiom above: ONE
#      Note per Version, subject carrying AUTO_NOTE_PREFIX so note_triage
#      treats it as a record and never an actionable request (invariant 11,
#      same as Phase 12's refusal Note), found-or-updated in place rather than
#      accumulating a new Note every cycle. The one thing intentionally NOT
#      copied from record_panel_refusal is the Version status write -- there
#      is no REFUSED_VERSION_STATUS equivalent here, because constraint 1
#      above forbids it.
PROVENANCE_CURSOR_JSON = os.path.join(ROOT, "build", "out", "provenance_check_cursor.json")
PROVENANCE_NOTE_PREFIX = "%sProvenance mismatch: " % AUTO_NOTE_PREFIX


def _load_provenance_cursor(path=None):
    """-> {episode code: ISO created_at string of the newest Version already
    checked}. {} if the cursor file does not exist yet (first run, a full
    sweep per episode) or is unreadable -- same cold-start posture as
    _load_hand_edit_cursor: a bad file means "scan from the beginning",
    never a crash."""
    p = path or PROVENANCE_CURSOR_JSON
    try:
        with open(p) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as exc:                                      # noqa: BLE001
        log("  provenance watcher: cursor at %s is unreadable (%s), starting "
            "a full sweep per episode" % (p, type(exc).__name__))
        return {}
    return data if isinstance(data, dict) else {}


def _save_provenance_cursor(cursors, path=None):
    p = path or PROVENANCE_CURSOR_JSON
    try:
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cursors, f)
        os.replace(tmp, p)
    except Exception as exc:                                      # noqa: BLE001
        log("  provenance watcher: could not persist cursor (%s: %s)"
            % (type(exc).__name__, str(exc)[:120]))


def _report_provenance_drift(sg, v, bad):
    """Make ONE Version's provenance/sent disagreement VISIBLE as a Note,
    exactly the record_panel_refusal() idiom (find-or-update by subject +
    Version link) minus any status write -- this REPORTS, it never refuses.
    -> True if a Note was written or updated, False on a ShotGrid failure
    (logged, never raised: one bad write must not stop the rest of the
    batch, same posture as record_panel_refusal)."""
    subject = "%s%s" % (PROVENANCE_NOTE_PREFIX, v.get("code") or v.get("id"))
    lines = ["provenance_vs_sent.py found %s (%s) claims words its own "
             "sg_prompt_final__as_sent_ does not contain:\n"
             % (v.get("code") or v["id"], v.get("sg_stage") or "?")]
    for f, missing in bad:
        lines.append("  %-28s missing: %s" % (f, ", ".join(sorted(missing))[:200]))
    lines.append("\nThis is a REPORTING finding, not a refusal: the Version was "
                 "published and nothing was blocked. It means the record and "
                 "the prompt disagree, not that either one is necessarily wrong "
                 "-- read the Version's own sg_prompt_final__as_sent_ before "
                 "changing any code (F461).\n\nThis Note is the pipeline's own "
                 "record, not a request: its subject carries the '%s' prefix, so "
                 "note triage and the proposer both ignore it."
                 % AUTO_NOTE_PREFIX.strip())
    body = "\n".join(lines)
    try:
        existing = sg.find_one("Note",
                               [["project", "is", PROJ], ["subject", "is", subject],
                                ["note_links", "in", [{"type": "Version", "id": v["id"]}]]],
                               ["id"])
        if existing:
            sg.update("Note", existing["id"],
                      {"content": body, "sg_status_list": REFUSAL_NOTE_STATUS})
            log("  provenance watcher: updated Note %s on %s" % (existing["id"], v.get("code")))
        else:
            note = sg.create("Note", {
                "project": PROJ, "subject": subject, "content": body,
                "note_links": [{"type": "Version", "id": v["id"]}],
                "sg_status_list": REFUSAL_NOTE_STATUS})
            log("  provenance watcher: posted Note %s on %s" % (note["id"], v.get("code")))
        return True
    except Exception as exc:                                      # noqa: BLE001
        log("  provenance watcher: could not write the drift Note for %s (%s: %s)"
            % (v.get("code"), type(exc).__name__, str(exc)[:120]))
        return False


def watch_provenance(sg, cursor_path=None):
    """F461: run provenance_vs_sent over every KNOWN episode, scoped to
    Versions published SINCE THE LAST CHECK (see the Phase 14 header comment
    for why), and report any disagreement as a Note. NEVER blocks or fails a
    publish -- see _report_provenance_drift(). -> True if any Note was
    written or updated this cycle."""
    try:
        import provenance_vs_sent as PVS
    except Exception as exc:                                      # noqa: BLE001
        log("  provenance watcher: unavailable (%s: %s)"
            % (type(exc).__name__, str(exc)[:120]))
        return False
    cursors = _load_provenance_cursor(cursor_path)
    held = held_episode_codes(sg)
    did = False
    for code in sorted(EPCTX.known_episode_codes()):
        if code in held:
            continue
        since = cursors.get(code)
        try:
            vs, disagree, _nosent, _ok = PVS.find_disagreements(sg, episode=code, since=since)
        except Exception as exc:                                  # noqa: BLE001
            log("  provenance watcher[%s]: check failed (continuing): %s: %s"
                % (code, type(exc).__name__, str(exc)[:160]))
            continue
        # ADVANCE ONLY PAST WHAT WE ACTUALLY SAW, using ShotGrid's OWN clock
        # (each Version's created_at), never this box's wall clock -- a local
        # clock can drift from ShotGrid's server time, and a cursor built from
        # it could skip a Version created in that gap. No Versions this cycle
        # means the cursor does not move, which is correct: there is nothing
        # new to have checked.
        newest = None
        for v in vs:
            ca = v.get("created_at")
            if ca and (newest is None or ca > newest):
                newest = ca
        if newest is not None:
            cursors[code] = newest.isoformat() if hasattr(newest, "isoformat") else newest
        for v, bad in disagree:
            if _report_provenance_drift(sg, v, bad):
                did = True
    _save_provenance_cursor(cursors, cursor_path)
    if did:
        log("  provenance watcher: disagreement(s) found and reported as Notes "
            "above; nothing was blocked, held, or refused (F461)")
    return did

# ============================================================================
# END PHASE 14
# ============================================================================


def cycle(sg):
    """One pass. Every branch is entered because of ShotGrid state, never a flag."""
    # UNCONDITIONAL CYCLE MARKER. The idle heartbeat below cannot cover this
    # service: 24 Shots sit permanently at sg_gen_status='review', so step 1
    # sets did=True on EVERY cycle, idle never increments, and the idle line
    # never prints. Measured 2026-09-06, three times in a row: the service
    # completes exactly one cycle and then emits nothing, and the FIRST line
    # of cycle 2 never appears, which places the hang in
    # watch_panel_video_queue() or the two sg.find() calls right after it.
    # This marker is what makes that inference possible at all: without a
    # line that prints before any ShotGrid call, a hang in the first query is
    # indistinguishable from a hang anywhere else in the pass.
    log("cycle start")
    # IS WHAT I AM RUNNING WHAT IS COMMITTED? Asked every cycle, because the
    # answer went unnoticed for five hours on 2026-09-06 (F098) and the gap is
    # invisible from both ends: git is clean when everything is committed, and
    # the service is healthy when it is running. Only the comparison informs.
    #
    # Reported, never acted on: restarting a standing service is not a check's
    # decision. One line when current, several when drifted, so a healthy cycle
    # stays quiet and a drifted one names the tools that are not running.
    try:
        import deploy_drift as _DD
        _lines, _code = _DD.render(_DD.drift(), quiet=False)
        for _l in _lines:
            log(_l)
    except Exception as _exc:                                     # noqa: BLE001
        log("deploy: drift check unavailable (%s: %s)"
            % (type(_exc).__name__, str(_exc)[:90]))
    did = False

    # -1. A HAND-EDITED FIELD (sg_action_beat/sg_shot_size/sg_camera/
    #     sg_gen_prompt) IS the trigger (Phase 13, see watch_hand_edits()
    #     above). Runs FIRST, before anything else this cycle reads a Panel
    #     Task or Shot.sg_gen_status, so a hand edit's requeue is visible to
    #     the SAME cycle's panel-composition and video-queue passes rather
    #     than costing a whole tick of latency.
    try:
        if watch_hand_edits(sg):
            did = True
    except Exception as exc:                                      # noqa: BLE001
        log("hand-edit watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 0-. A PANEL VERSION WAS APPROVED -> record Shot.sg_approved_panel (Phase
    #     5, see watch_panel_approvals() above). The service does this, never a
    #     tool (invariant 7).
    #
    #     THIS RUNS FIRST, and it used to run at step 6. MEASURED 2026-09-07 on
    #     SHOW01_A_0540, approving a panel at 19:23:42:
    #
    #       19:23:59  stale video: ..._v008 is newer than ..._a14b_v001 -- queueing
    #       19:24:01  REFUSED: Shot has no approved panel (sg_approved_panel is
    #                 not set) -- refusing to generate video without an anchor
    #       19:24:12  panel approved: Shot.sg_approved_panel -> ..._v008
    #
    #     Every one of those is behaving correctly on its own. The stale-video
    #     watcher compares VERSION timestamps, so it sees the approval
    #     immediately; the video pass reads the Shot POINTER, which only this
    #     watcher writes, eleven seconds and one step later. So an approval cost
    #     a wasted cycle AND printed a loud REFUSED line naming the pipeline's
    #     scariest historical failure, right at the moment an operator has just
    #     approved something and is watching.
    #
    #     Same shape as F331: a watcher reading a pointer that another watcher
    #     in the SAME cycle is responsible for updating. The fix is ordering,
    #     not a retry. Nothing downstream sets an approval, so there is no
    #     dependency in the other direction, and moving it first also means the
    #     stale-link CLEARING it does happens before anything reads the link.
    try:
        if watch_panel_approvals(sg):
            did = True
    except Exception as exc:
        log("panel-approval watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 0. PANEL-ANCHORED VIDEO (Phase 6, see watch_panel_video_queue() above).
    #    Runs BEFORE step 1 on purpose: it claims or refuses every currently
    #    'queued' Shot synchronously, so step 1's older, panel-unaware pass
    #    never sees one of them still sitting at 'queued'. See the PHASE 6
    #    block's own comment for the full ordering rationale.
    # 0a. A REVISED PANEL MAKES ITS SHOT'S VIDEO STALE. Runs BEFORE the queue
    #     pass below so a shot it flags is picked up in the SAME cycle rather
    #     than waiting for the next one.
    try:
        if watch_stale_videos(sg):
            did = True
    except Exception as exc:
        log("stale-video watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    try:
        if watch_panel_video_queue(sg):
            did = True
    except Exception as exc:
        log("panel-video watcher error (continuing): %s: %s" % (type(exc).__name__, str(exc)[:160]))

    # 1. Shots queued in ShotGrid -> generate. The worker also ingests Notes and
    #    status flips, so review-driven revisions ride the same pass.
    # These two are the FIRST ShotGrid calls of the pass and the leading
    # suspects for the hang, so say so before making them: if the log stops
    # after "querying shot queue" the block is in shotgun_api3, not in ours.
    log("querying shot queue")
    queued = sg.find("Shot", [["project", "is", PROJ], ["sg_gen_status", "is", "queued"]], ["code"])
    reviewing = sg.find("Shot", [["project", "is", PROJ], ["sg_gen_status", "is", "review"]], ["code"])
    if queued or reviewing:
        log("worker pass: %d queued, %d in review" % (len(queued), len(reviewing)))
        r = run([PY, WORKER, "--once"])
        if r:
            for line in (r.stdout or "").splitlines():
                if "->" in line or "teardown" in line or "ERROR" in line:
                    log("  " + line.strip())
        did = True

    # 1b. Shots at stage=board -> generate boards. Boards stay in the pipeline
    #     under V2 (D8) as the cheap per-shot stills that feed the on-request
    #     animatic and, later, the panel stage - just not as an auto-cut gate.
    boarding = sg.find("Shot", [["project", "is", PROJ], ["sg_stage", "is", "board"]], ["code"])
    if boarding:
        log("boards pass: %d shot(s) at stage=board" % len(boarding))
        r = run([PY, ANIMATIC, "--boards"])
        if r:
            for line in (r.stdout or "").splitlines():
                if "->" in line or "ERROR" in line or "published" in line:
                    log("  " + line.strip())
        did = True

    # 2. A Sequence flipped to queued -> assemble it. Assembly refuses unless every
    #    shot has an approved Version, so this is safe to attempt on any flip.
    try:
        seqs = sg.find("Sequence", [["project", "is", PROJ], ["sg_gen_status", "is", "queued"]],
                       ["code"])
    except Exception:
        seqs = []          # field may not exist yet; not an error worth stopping for
    for s in seqs:
        log("assemble requested for %s" % s["code"])
        r = run([PY, ASSEMBLE, "--sequence", s["code"]], timeout=7200)
        ok = bool(r and r.returncode == 0)
        tail = ""
        if r:
            lines = [l for l in (r.stdout or "").splitlines() if l.strip()]
            tail = lines[-1][:200] if lines else ""
        sg.update("Sequence", s["id"],
                  {"sg_gen_status": "done" if ok else "error",
                   "sg_gen_log": ("assembled" if ok else "assembly refused: ") + tail})
        log("  %s -> %s" % (s["code"], "assembled" if ok else "refused"))
        did = True

    # 3. Story beats edited in ShotGrid -> flag dependent shots stale (Phase 4,
    #    see watch_beats() above).
    try:
        if watch_beats_all_episodes(sg):
            did = True
    except Exception as exc:
        log("beat watcher error (continuing): %s: %s" % (type(exc).__name__, str(exc)[:160]))

    # 3b. Classify Notes on shot-linked Versions -> Shot.sg_note_class.
    #
    #     THE MISSING FIRST LINK, found 2026-09-04 by driving the loop as the
    #     operator: a Note plus Version='rrq' on a panel produced NOTHING for
    #     15 minutes, no log, no proposal. Step 4 below selects on
    #     Shot.sg_note_class == 'prompt-addressable', but NOTHING in this
    #     service ever wrote that field -- note_triage's sweep existed and was
    #     only ever run by hand. The Asset side of this hole was found and
    #     fixed in Phase 10 ("Nothing consumed that signal"); the Shot side was
    #     left open. It runs BEFORE step 4, because step 4 consumes what it
    #     writes; ordered the other way the loop would take two cycles.
    try:
        if watch_note_triage(sg):
            did = True
    except Exception as exc:
        log("note-triage watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 4. Prompt-addressable notes -> LLM proposal; accepted/auto-apply
    #    proposals -> apply + requeue (Phase 7, see watch_prompt_proposals() above).
    try:
        if watch_prompt_proposals(sg):
            did = True
    except Exception as exc:
        log("prompt-revision watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 5. Sequence explicitly requested an animatic cut (Phase 9, D8 -- see
    #    watch_animatic_requests() above).
    try:
        if watch_animatic_requests(sg):
            did = True
    except Exception as exc:
        log("animatic-request watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 7. An approved design changed under an already-composed panel -> flag
    #    that shot's panel stale (Phase 5, see watch_panel_designs() above).
    try:
        if watch_panel_designs(sg):
            did = True
    except Exception as exc:
        log("panel-design watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 6b. STAGE 13: an approved video with no FIN yet -> upres to delivery.
    #     Placed LATE in the cycle on purpose: at 231s measured it is the most
    #     expensive pass we have, so every cheap watcher gets its turn first,
    #     and it does ONE shot per cycle for the same reason.
    try:
        if watch_finishing(sg):
            did = True
    except Exception as exc:                                      # noqa: BLE001
        log("finishing watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 7b. ...AND NOW ACTUALLY INVALIDATE IT. Step 7 only ever FLAGGED.
    #
    # Geoff, 2026-09-07, verbatim: "When we approved the design with one bed and
    # the good posters did all the related shots get automatically invalidated?
    # They should be."
    #
    # They were not. MEASURED: 19 SHOW01 shots carried panels composed from a
    # superseded set design, 11 of them still at 'apr', 10 of those with an
    # approved VIDEO built on the stale panel, and the only thing that had ever
    # happened was an [auto] note. 24 of those notes were still open. An open
    # note nobody reads is not a mechanism.
    #
    # tools/invalidate_stale_panels.py has done exactly the three missing steps
    # since 2026-09-07 11:26 and HAD ZERO CALLERS. Its own docstring: "Wiring it
    # into the service is a one-line call to invalidate() once he has seen it."
    # He has now seen it: the dry run was shown before this was wired, and this
    # is that one line. Second instance tonight of a measured fix sitting one
    # call site away from the thing that needed it (F350).
    #
    # ORDER IS LOAD-BEARING, and it is the F334 lesson one watcher over: this
    # sets Panel Tasks to 'rdy', and watch_panel_composition() at step 12
    # triggers on 'rdy', so invalidating HERE means the recompose happens in the
    # SAME cycle rather than a cycle later. It also runs AFTER step 7 so the
    # sidecar's stale flags are current when it reads them.
    #
    # It takes something away from an operator, so it is loud, never silent: it
    # reports the shot count every time it changes anything.
    #
    # EVERY EPISODE, EXPLICITLY, never a module default. This mirrors
    # watch_note_triage() above for the reason spelled out in its docstring: a
    # standing service must not depend on an env var being set correctly by
    # whoever launched it, and the default here is the RETIRED episode. Held
    # episodes are skipped so a paused show is not churned.
    try:
        import invalidate_stale_panels as _ISP
        import episode_context as _EC
        held = held_episode_codes(sg)
        for _code in sorted(_EC.known_episode_codes()):
            if _code in held:
                continue
            n = _ISP.invalidate(sg, episode=_code, dry=False)
            if n:
                log("  stale-panel invalidation[%s]: %d shot(s) un-approved and "
                    "requeued against their new approved design" % (_code, n))
                did = True
    except Exception as exc:                                      # noqa: BLE001
        log("stale-panel invalidation error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 8. An Asset DESIGN Version was approved -> record Asset.sg_approved_design
    #    + sg_stage='approved' (Phase 10, see watch_design_approvals() above).
    #    Runs BEFORE step 7's staleness watcher would next see the field... it
    #    does not, actually, and that is the point: step 7 already ran this
    #    cycle against the pre-write value, so a design linked here is picked
    #    up by step 7 on the NEXT cycle, one cycle of latency and no
    #    intra-cycle ordering dependency in either direction. Placed after
    #    step 7 rather than before it on purpose: a brand-new approval should
    #    not stale a panel in the same pass that first records it.
    try:
        if watch_design_approvals(sg):
            did = True
    except Exception as exc:
        log("design-approval watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 9. An Asset design Version was flipped to 'rrq' with a note -> propose a
    #    revision to that Asset's design prompt; accepted proposals apply
    #    (Phase 10, see watch_design_revisions() above). Nothing applies
    #    without a human accept.
    try:
        if watch_design_revisions(sg):
            did = True
    except Exception as exc:
        log("design-revision watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 10. An applied design revision queued a re-render (Phase 11, see
    #     watch_design_renders() above). Publishes new candidates at 'rev';
    #     never approves anything.
    try:
        if watch_design_renders(sg):
            did = True
    except Exception as exc:
        log("design-render watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 11. A Panel Task was flipped to 'rdy' -> compose that shot's panel
    #     (Phase 12, see watch_panel_composition() above). Publishes at
    #     'rev'; never approves anything.
    # 11b. A Panel Task held as REFUSED whose Shot has NEWER published panels is
    #      a contradiction the pipeline can settle itself, rather than needing a
    #      human to notice and repair it by hand.
    try:
        if reconcile_held_panels(sg):
            did = True
    except Exception as exc:
        log("reconcile watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    try:
        if watch_panel_composition(sg):
            did = True
    except Exception as exc:
        log("panel-composition watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 12. Approvals have settled -> take the losing alternates out of the
    #     review queue (see watch_review_housekeeping() above). Runs last so
    #     an approval processed earlier THIS cycle is tidied in the same pass
    #     rather than one 45s tick later.
    try:
        if watch_review_housekeeping(sg):
            did = True
    except Exception as exc:
        log("review-housekeeping watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    # 13. F461: does provenance agree with what was actually sent? REPORTS
    #     only (see Phase 14 above) -- runs last, ordering-independent, since
    #     it neither depends on nor feeds any other step this cycle.
    try:
        if watch_provenance(sg):
            did = True
    except Exception as exc:
        log("provenance watcher error (continuing): %s: %s"
            % (type(exc).__name__, str(exc)[:160]))

    return did


# ------------------------------------------------------- PHASE 10 self-test
# Offline canaries for watch_design_approvals(). No ShotGrid, no GPU. The
# properties asserted here are the ones that, if they broke, would be
# invisible in a live run: a watcher that writes every cycle looks exactly
# like a watcher that works, until it has re-triggered watch_panel_designs()
# a hundred times and staled every panel in the show.

class _StubSG(object):
    """Just enough ShotGrid for watch_design_approvals(): a Version table, an
    Asset table, and an update() that writes back into the Asset table so a
    SECOND call really sees the first call's effect (a real second cycle
    re-queries; so does this)."""

    def __init__(self, versions, assets):
        self.versions = versions
        self.assets = assets           # id -> row
        self.updates = []

    @staticmethod
    def _project(row, fields):
        """Return ONLY the requested fields, exactly as ShotGrid does. A stub
        that hands back every key it holds cannot catch a caller that forgot
        to ASK for a field -- and that is a real bug this watcher shipped
        with: the eligibility predicate read sg_stage without requesting it,
        so every Version looked like a test cell. Live run caught it; this
        makes the offline canaries able to."""
        if fields is None:
            return dict(row)
        keep = set(fields) | {"id", "type"}
        return dict((k, v) for k, v in row.items() if k in keep)

    def find(self, entity_type, filters, fields=None, **kw):
        if entity_type == "Version":
            wanted, ids = None, None
            for f in filters:
                if f[0] == "sg_status_list":
                    wanted = f[2]
                if f[0] == "id" and f[1] == "in":
                    ids = f[2]
            rows = [v for v in self.versions
                    if (wanted is None or v.get("sg_status_list") in wanted)
                    and (ids is None or v["id"] in ids)]
            return [self._project(v, fields) for v in rows]
        if entity_type == "Asset":
            ids = None
            for f in filters:
                if f[0] == "id" and f[1] == "in":
                    ids = f[2]
            return [dict(a) for a in self.assets.values()
                    if ids is None or a["id"] in ids]
        return []

    def update(self, entity_type, entity_id, data):
        self.updates.append((entity_type, entity_id, dict(data)))
        if entity_type == "Asset":
            self.assets[entity_id].update(data)
        return dict(data)


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-74s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # --- W10 / E0: the deploy-stamp startup line, canary'd both ways -------
    # A tree with no stamp (hand-copied, or predating the deploy seam) must
    # say so loudly, never silently. A tree WITH a stamp must actually name
    # the sha and release in the printed line -- the whole point of W10.
    import tempfile as _tempfile
    _empty_dir = _tempfile.mkdtemp(prefix="genvideo_service_selftest_")
    try:
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import deploy as _deploy
        _line_missing = _deploy.describe_stamp(_deploy.read_stamp(_empty_dir))
        ck("CANARY (missing version stamp): an unstamped tree's startup line says so "
           "loudly, and does not fabricate a sha",
           "MISSING" in _line_missing and "hand-copied" in _line_missing)
        _deploy.write_stamp(_empty_dir, {
            "release_id": "20260903-selftest_abc1234", "git_sha": "abc1234",
            "git_dirty": False, "deployed_at_utc": "2026-09-03T00:00:00Z",
            "deployed_by": "selftest", "file_count": 41,
            "preflight": {"ran": 30, "passed": 30, "failed": []}})
        _line_present = _deploy.describe_stamp(_deploy.read_stamp(_empty_dir))
        ck("a stamped tree's startup line names the git sha AND the release id",
           "abc1234" in _line_present and "20260903-selftest_abc1234" in _line_present)
    except Exception as exc:
        ck("deploy.py's stamp helpers are importable and usable from genvideo_service.py "
           "(%s: %s)" % (type(exc).__name__, exc), False)
    finally:
        import shutil as _shutil
        _shutil.rmtree(_empty_dir, ignore_errors=True)
    ck("deploy_stamp_line() itself never raises, even off the module's normal TOOLS path",
       isinstance(deploy_stamp_line(), str))

    def fresh(asset_extra=None, versions=None):
        vs = versions if versions is not None else [
            {"id": 900, "code": "ASSET_DESIGN_v001", "sg_status_list": "apr", "sg_stage": "keyframe",
             "entity": {"type": "Asset", "id": 42}, "created_at": "2026-01-01"},
        ]
        a = {"id": 42, "code": "TEST_ASSET", "sg_approved_design": None, "sg_stage": None}
        a.update(asset_extra or {})
        return _StubSG(vs, {42: a})

    # 1. The basic act: an approved Asset-entity Version is recorded.
    sg = fresh()
    # --- watch_panel_designs must not lie about provenance to dedup itself.
    # Direct test of the two invariants the suffixed-key fix protects.
    _sc = {"character_asset_id": 1, "character_design_version_id": 100,
           "set_asset_id": 2, "set_design_version_id": 200}
    _entry = dict(_sc)
    _entry["character_design_version_id_stale_flagged"] = 999
    ck("after flagging, the sidecar still records what the panel was COMPOSED "
       "from", _entry["character_design_version_id"] == 100)
    ck("staleness stays derivable after a flag (live 999 != composed 100)",
       999 != _entry["character_design_version_id"])
    ck("a second cycle does not re-fire on the same swap",
       _entry.get("character_design_version_id_stale_flagged") == 999)
    ck("a THIRD, different design swap does re-fire",
       _entry.get("character_design_version_id_stale_flagged") != 1234)
    _src = open(os.path.abspath(__file__), encoding="utf-8").read()
    # The needle is assembled from pieces so this check cannot match its OWN
    # source line -- a grep-for-a-literal test that contains the literal
    # always fails, and the first version of this one did exactly that.
    _needle = "entry[design_key]" + " = " + "cur"
    ck("the watcher never assigns the provenance key back to the live design",
       _needle not in _src)

    ck("an approved Asset Version writes sg_approved_design + sg_stage=approved",
       watch_design_approvals(sg) is True and sg.updates
       and sg.updates[0][2]["sg_approved_design"]["id"] == 900
       and sg.updates[0][2]["sg_stage"] == "approved")

    # 2. THE RUNAWAY CANARY. The single most important property here: a
    #    second cycle over unchanged state must write NOTHING. If it writes,
    #    watch_panel_designs() sees sg_approved_design "change" every cycle
    #    and stales every dependent panel forever. Proven by re-running the
    #    real function against the same stub, whose update() kept the Asset
    #    table in sync exactly as ShotGrid would.
    before = len(sg.updates)
    ck("CANARY: a second cycle over unchanged state is a NO-OP (no runaway "
       "re-trigger of watch_panel_designs)",
       watch_design_approvals(sg) is False and len(sg.updates) == before)

    # 3. A Shot-entity Version must never be touched by this watcher --
    #    watch_panel_approvals() owns those, and double-handling would fight it.
    #    NOTE the stub deliberately holds an entity at the SHOT's id (7) as
    #    well: without it this canary passed for the wrong reason (the Asset
    #    lookup simply found nothing), and mutation-testing the type filter
    #    away left it green. It is red now. Keep the id-7 row.
    sg = _StubSG([{"id": 901, "code": "SHOT_PANEL_v001", "sg_status_list": "apr", "sg_stage": "keyframe",
                   "entity": {"type": "Shot", "id": 7}, "created_at": "2026-01-01"}],
                 {7: {"id": 7, "code": "NOT_AN_ASSET", "sg_approved_design": None,
                      "sg_stage": None},
                  42: {"id": 42, "code": "TEST_ASSET", "sg_approved_design": None,
                       "sg_stage": None}})
    ck("CANARY: a Shot-entity Version is ignored (watch_panel_approvals owns it)",
       watch_design_approvals(sg) is False and sg.updates == [])

    # 4. An unapproved status is not an approval.
    sg = fresh(versions=[{"id": 902, "code": "D_v001", "sg_status_list": "rrq", "sg_stage": "keyframe",
                          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-01-01"}])
    ck("CANARY: 'rrq' is not an approval and writes nothing",
       watch_design_approvals(sg) is False and sg.updates == [])
    sg = fresh(versions=[{"id": 903, "code": "D_v001", "sg_status_list": "rjct", "sg_stage": "keyframe",
                          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-01-01"}])
    ck("CANARY: 'rjct' is not an approval and writes nothing",
       watch_design_approvals(sg) is False and sg.updates == [])

    # 5. Newest-approved wins, the same tie-break watch_panel_approvals uses.
    sg = fresh(versions=[
        {"id": 910, "code": "D_v001", "sg_status_list": "apr", "sg_stage": "keyframe",
         "entity": {"type": "Asset", "id": 42}, "created_at": "2026-01-01"},
        {"id": 911, "code": "D_v002", "sg_status_list": "apr", "sg_stage": "keyframe",
         "entity": {"type": "Asset", "id": 42}, "created_at": "2026-02-01"},
    ])
    watch_design_approvals(sg)
    ck("newest approved Version wins when an Asset has several",
       sg.assets[42]["sg_approved_design"]["id"] == 911)

    # 6. Asset.sg_status_list is deliberately NOT written. 16 of 16 Assets on
    #    this project that already carry an approved design sit at 'wtg', and
    #    nothing reads the field. Writing it here would diverge the six SHOW_
    #    Assets from the sixteen PILOT01 ones. Counted, not assumed -- see the
    #    PHASE 10 banner. This canary is what stops it being added by
    #    accident later.
    ck("CANARY: Asset.sg_status_list is never written (16/16 precedent is 'wtg')",
       all("sg_status_list" not in u[2] for u in sg.updates))

    # 7. THE PING-PONG CANARY, between the two Phase 10 halves.
    #    prompt_revision.apply_for_asset() sets sg_stage='design' on an
    #    accepted revision WITHOUT clearing sg_approved_design. This watcher
    #    must then leave it alone: the design was revised, it is genuinely
    #    back in design, and only a NEW approved Version should move it out.
    #    If sg_stage were written unconditionally instead of inside the
    #    design-changed branch, the two would fight every cycle.
    sg = fresh(asset_extra={"sg_approved_design": {"type": "Version", "id": 900},
                            "sg_stage": "design"})
    ck("CANARY: an Asset revised back to sg_stage='design' is NOT resurrected to "
       "'approved' by the approval scan",
       watch_design_approvals(sg) is False and sg.assets[42]["sg_stage"] == "design")

    # 8. THE ELIGIBILITY CANARY, also a regression test. Version 67462 is a
    #    LoRA WEDGE cell -- sg_stage None, description "ANIMA TEST RECORD --
    #    not a deliverable, not approved" -- that Geoff set to 'apr' as a
    #    RECIPE preference. A status-only rule made it SHOW_CHAR_PILOTCHARA's
    #    approved character design on live data. Reverted, then gated on
    #    sg_stage == DESIGN_STAGE, then this.
    sg = _StubSG(
        [{"id": 67462, "code": "SHOW01_ANIMA_P3_10_PILOTCHARA_wedge", "sg_status_list": "apr",
          "sg_stage": None,
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 09:09:00"}],
        {42: {"id": 42, "code": "SHOW_CHAR_PILOTCHARA_LIKE",
              "sg_approved_design": None, "sg_stage": None}})
    ck("CANARY (regression): an approved TEST CELL (sg_stage unset) is NOT made an "
       "Asset's approved design",
       watch_design_approvals(sg) is False and sg.updates == []
       and sg.assets[42]["sg_approved_design"] is None)

    # ...and a real anchor candidate at the design stage IS eligible, so the
    # predicate is not simply "refuse everything".
    sg = _StubSG(
        [{"id": 67462, "code": "SHOW01_ANIMA_P3_10_PILOTCHARA_wedge", "sg_status_list": "apr",
          "sg_stage": None,
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 09:09:00"},
         {"id": 67480, "code": "SHOW_CHAR_PILOTCHARA_ANCHOR_turbo10_s52001", "sg_status_list": "apr",
          "sg_stage": DESIGN_STAGE,
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 11:00:00"}],
        {42: {"id": 42, "code": "SHOW_CHAR_PILOTCHARA_LIKE",
              "sg_approved_design": None, "sg_stage": None}})
    ck("a real approved anchor candidate (sg_stage='%s') IS eligible" % DESIGN_STAGE,
       watch_design_approvals(sg) is True
       and sg.assets[42]["sg_approved_design"]["id"] == 67480)

    # A NEWER test cell must not beat an OLDER real candidate: eligibility is
    # applied BEFORE the newest-wins tie-break, not after it.
    sg = _StubSG(
        [{"id": 67480, "code": "ANCHOR", "sg_status_list": "apr", "sg_stage": DESIGN_STAGE,
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 09:00:00"},
         {"id": 67499, "code": "WEDGE", "sg_status_list": "apr", "sg_stage": None,
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 23:00:00"}],
        {42: {"id": 42, "code": "CHAR_Z", "sg_approved_design": None, "sg_stage": None}})
    ck("CANARY: a NEWER test cell never beats an OLDER real design candidate "
       "(eligibility is applied before newest-wins)",
       watch_design_approvals(sg) is True
       and sg.assets[42]["sg_approved_design"]["id"] == 67480)

    # 9. THE DOWNGRADE CANARY, and it is a regression test, not a hypothetical.
    #    Exact shape of the live failure: CHAR_CHARF recorded
    #    CHAR_CHARF_ANCHOR_v001 (67191, created 08:00) as its approved
    #    design, but that Version's OWN status was never set to 'apr' by the
    #    one-off script that linked it. The only Asset Version at an approved
    #    status was the OLDER CHAR_CHARF_SHEET_v002 (67081, created
    #    01:53), so newest-approved-wins replaced a deliberate newer link
    #    with an older one, on live production data, on this watcher's first
    #    real run. Reverted, then guarded, then this canary written.
    sg = _StubSG(
        [{"id": 67081, "code": "SHEET_v002", "sg_status_list": "apr", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-08-29 01:53:25"},
         {"id": 67191, "code": "ANCHOR_v001", "sg_status_list": "rev", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-08-29 08:00:54"}],
        {42: {"id": 42, "code": "CHAR_CHARF_LIKE",
              "sg_approved_design": {"type": "Version", "id": 67191},
              "sg_stage": "approved"}})
    ck("CANARY (regression): an existing approval is NEVER downgraded to an OLDER "
       "approved Version",
       watch_design_approvals(sg) is False and sg.updates == []
       and sg.assets[42]["sg_approved_design"]["id"] == 67191)

    # ...but a genuinely NEWER approved candidate DOES supersede it. The guard
    # must not become "never update anything that already has a link".
    sg = _StubSG(
        [{"id": 67191, "code": "ANCHOR_v001", "sg_status_list": "apr", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-08-29 08:00:54"},
         {"id": 67318, "code": "ANCHOR_v002", "sg_status_list": "apr", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-08-29 20:52:53"}],
        {42: {"id": 42, "code": "CHAR_X",
              "sg_approved_design": {"type": "Version", "id": 67191},
              "sg_stage": "approved"}})
    ck("a genuinely NEWER approved design DOES supersede the recorded one",
       watch_design_approvals(sg) is True
       and sg.assets[42]["sg_approved_design"]["id"] == 67318)

    # An Asset with NO link yet is the unambiguous case and must still work --
    # this is exactly SHOW_CHAR_PILOTCHARA, the live case that motivated the
    # watcher.
    sg = _StubSG(
        [{"id": 67462, "code": "PILOTCHARA_design", "sg_status_list": "apr", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": "2026-09-03 09:09:00"}],
        {42: {"id": 42, "code": "SHOW_CHAR_PILOTCHARA_LIKE",
              "sg_approved_design": None, "sg_stage": None}})
    ck("an Asset with no approved design yet is linked (the SHOW_CHAR_PILOTCHARA case)",
       watch_design_approvals(sg) is True
       and sg.assets[42]["sg_approved_design"]["id"] == 67462)

    # CONSERVATIVE WHEN IT CANNOT TELL: an unreadable/absent date on either
    # side must mean "do not supersede", never "supersede anyway".
    sg = _StubSG(
        [{"id": 700, "code": "D_apr", "sg_status_list": "apr", "sg_stage": "keyframe",
          "entity": {"type": "Asset", "id": 42}, "created_at": None}],
        {42: {"id": 42, "code": "CHAR_Y",
              "sg_approved_design": {"type": "Version", "id": 701},
              "sg_stage": "approved"}})
    ck("CANARY: an undatable candidate never supersedes an existing approval",
       watch_design_approvals(sg) is False and sg.updates == [])

    # 10. Degrade loudly, never crash the service cycle.
    class _BoomSG(object):
        def find(self, *a, **kw):
            raise RuntimeError("simulated ShotGrid fault")

    ck("CANARY: a ShotGrid fault degrades to False + a log line, never an exception "
       "that would kill the cycle", watch_design_approvals(_BoomSG()) is False)

    # ======================================================================
    # PHASE 11 self-test -- watch_design_renders(). No ShotGrid, no GPU, no
    # anima_anchor subprocess: run() is monkeypatched to simulate what the
    # real subprocess would do to ShotGrid (publish N new candidate Versions)
    # without ever shelling out.
    # ======================================================================
    global run

    class _StubGenSG(object):
        def __init__(self, assets, versions=None):
            self.assets = assets              # id -> row, mutated by update()
            self.versions = list(versions or [])
            self.updates = []
            self.notes = []

        def find(self, entity_type, filters, fields=None, **kw):
            if entity_type == "Asset":
                wanted = None
                for f in filters:
                    if f[0] == ASSET_GEN_STATUS and f[1] == "is":
                        wanted = f[2]
                return [dict(a) for a in self.assets.values()
                       if wanted is None or a.get(ASSET_GEN_STATUS) == wanted]
            if entity_type == "Version":
                aid, prefix = None, None
                for f in filters:
                    if f[0] == "entity" and f[1] == "is":
                        aid = f[2]["id"]
                    if f[0] == "code" and f[1] == "starts_with":
                        prefix = f[2]
                return [dict(v) for v in self.versions
                       if (aid is None or v["entity"]["id"] == aid)
                       and (prefix is None or v["code"].startswith(prefix))]
            return []

        def find_one(self, entity_type, filters, fields=None, **kw):
            if entity_type == "Asset":
                for f in filters:
                    if f[0] == "id" and f[1] == "is":
                        row = self.assets.get(f[2])
                        return dict(row) if row else None
            return None

        def update(self, entity_type, entity_id, data):
            self.updates.append((entity_type, entity_id, dict(data)))
            if entity_type == "Asset":
                self.assets[entity_id].update(data)
            return dict(data)

        def create(self, entity_type, data):
            if entity_type == "Note":
                self.notes.append(dict(data))
            return {"id": 999, "type": entity_type}

    def _fake_render_run(rc, add_seeds, code, asset_id, sg_ref):
        def _fake_run(cmd, timeout=None, label=""):
            if rc == 0:
                base = 90000 + len(sg_ref.versions)
                for i in range(add_seeds):
                    sg_ref.versions.append({
                        "id": base + i, "code": "%s_ANCHOR_turbo10_s%d" % (code, 60000 + i),
                        "entity": {"type": "Asset", "id": asset_id},
                        "created_at": "2026-09-03 22:00:0%d" % i})

            class _R(object):
                pass
            r = _R()
            r.returncode = rc
            r.stdout = ""
            r.stderr = "" if rc == 0 else "simulated anima_anchor.py failure"
            return r
        return _fake_run

    _orig_run = run
    try:
        # 1. Only 'queued' Assets are picked up; a resting 'done'/None Asset
        #    is ignored entirely.
        sg = _StubGenSG({1: {"id": 1, "code": "SHOW_X", ASSET_GEN_STATUS: "done"}})
        ck("watch_design_renders ignores an Asset NOT at 'queued'",
           watch_design_renders(sg) is False and sg.updates == [])

        # 2. Happy path: 'queued', zero existing seeds -> asks for MIN_SEEDS,
        #    the (faked) subprocess publishes that many -> 'done', a
        #    completion Note is posted, and it names real new codes.
        sg = _StubGenSG({7: {"id": 7, "code": "SHOW_TEST_RENDER", ASSET_GEN_STATUS: "queued"}})
        run = _fake_render_run(0, 4, "SHOW_TEST_RENDER", 7, sg)
        ck("a queued render with a successful subprocess resolves to 'done'",
           watch_design_renders(sg) is True and sg.assets[7][ASSET_GEN_STATUS] == "done")
        ck("CANARY and posts NO Note: the new candidates ARE the announcement, "
           "published at 'rev' where the operator already looks (Geoff 2026-09-07, "
           "publishing must not add notes)",
           len(sg.notes) == 0)

        # 3. Failure: rc != 0 -> 'error', not 'queued' and not 'rendering' --
        #    and a SECOND cycle over that same resting state does nothing,
        #    proving a failed render does not retry forever.
        sg = _StubGenSG({8: {"id": 8, "code": "SHOW_TEST_FAIL", ASSET_GEN_STATUS: "queued"}})
        run = _fake_render_run(1, 0, "SHOW_TEST_FAIL", 8, sg)
        ck("a failed subprocess resolves to 'error', not left queued/rendering",
           watch_design_renders(sg) is True and sg.assets[8][ASSET_GEN_STATUS] == "error")
        before = list(sg.updates)
        ck("CANARY (runaway guard): a SECOND cycle over an 'error' Asset does nothing -- "
           "no infinite retry", watch_design_renders(sg) is False and sg.updates == before)

        # 4. MUTANT-CAUGHT REAL BUG SHAPE: rc==0 but nothing NEW actually
        #    landed (e.g. --only matched nothing, or the seed math was wrong)
        #    must NOT be trusted as success -- rule 1, never trust an exit
        #    code over the artefact itself.
        sg = _StubGenSG({9: {"id": 9, "code": "SHOW_TEST_NOOP", ASSET_GEN_STATUS: "queued"}})
        run = _fake_render_run(0, 0, "SHOW_TEST_NOOP", 9, sg)
        ck("CANARY: rc=0 with ZERO new seeds actually published is treated as a FAILURE, "
           "never trusted off the exit code alone",
           watch_design_renders(sg) is True and sg.assets[9][ASSET_GEN_STATUS] == "error")

        # 5. INVARIANT 7 CANARY: across every scenario run above (happy path,
        #    failure, and the rc=0-but-nothing-new case), this watcher never
        #    once wrote an approved-design field or an approved status.
        all_updates = sg.updates
        sg2 = _StubGenSG({11: {"id": 11, "code": "SHOW_TEST_INV7", ASSET_GEN_STATUS: "queued"}})
        run = _fake_render_run(0, 4, "SHOW_TEST_INV7", 11, sg2)
        watch_design_renders(sg2)
        all_updates = all_updates + sg2.updates
        ck("CANARY (invariant 7): no write from watch_design_renders() ever touches "
           "sg_approved_design or sets an approved sg_status_list",
           all("sg_approved_design" not in upd[2] and upd[2].get("sg_status_list") != "apr"
              for upd in all_updates))
    finally:
        run = _orig_run

    # 6. Degrade loudly, never crash the cycle.
    class _BoomAssetSG(object):
        def find(self, *a, **kw):
            raise RuntimeError("simulated ShotGrid fault")
    ck("CANARY: a ShotGrid fault in watch_design_renders degrades to False, never crashes",
       watch_design_renders(_BoomAssetSG()) is False)

    # 7. Drift guard: this file names the Asset render-trigger field a SECOND
    #    time (it cannot import prompt_revision at module scope -- see the
    #    PHASE 11 banner). If the two ever disagree, ShotGrid renamed the
    #    field on one side and not the other, or someone edited one constant
    #    and not its twin -- either way this must go red, not silently read
    #    the wrong field.
    try:
        sys.path.insert(0, TOOLS)
        import prompt_revision as _PR
        ck("ASSET_GEN_STATUS agrees with prompt_revision.A_GEN_STATUS (drift guard)",
           ASSET_GEN_STATUS == _PR.A_GEN_STATUS)
    except Exception as exc:                                  # noqa: BLE001
        ck("ASSET_GEN_STATUS agrees with prompt_revision.A_GEN_STATUS (drift guard) "
           "-- import failed: %s" % exc, False)

    # ======================================================================
    # PHASE 12 self-test -- watch_panel_composition(). No ShotGrid, no GPU:
    # run() is monkeypatched to simulate the subprocess; the stub's Version
    # table is what the watcher actually trusts for "did this publish"
    # (ground truth is a ShotGrid re-query, NEVER the subprocess's stdout --
    # see the function's own comment: panel_compose.py's log() writes to the
    # SAME stdout stream as its final json.dumps(result), so a naive
    # `json.loads(r.stdout)` fails on every real run and misreports every
    # success as a refusal. This suite proves the fix by having the fake
    # subprocess ALSO emit noisy non-JSON lines on stdout, exactly like the
    # real CLI does, and requires success to be detected anyway).
    class _StubPanelSG(object):
        def __init__(self, tasks, shots, versions=None):
            self.tasks = tasks                # id -> row, mutated by update()
            self.shots = shots                # id -> row
            self.versions = list(versions or [])   # Version rows, entity+code
            self.updates = []
            self.notes = []

        def find(self, entity_type, filters, fields=None, **kw):
            if entity_type == "Task":
                wanted = None
                for f in filters:
                    if f[0] == "sg_status_list" and f[1] == "is":
                        wanted = f[2]
                return [dict(t) for t in self.tasks.values()
                       if wanted is None or t.get("sg_status_list") == wanted]
            if entity_type == "Version":
                sid, prefix = None, None
                for f in filters:
                    if f[0] == "entity" and f[1] == "is":
                        sid = f[2]["id"]
                    if f[0] == "code" and f[1] == "starts_with":
                        prefix = f[2]
                return [dict(v) for v in self.versions
                       if (sid is None or v["entity"]["id"] == sid)
                       and (prefix is None or v["code"].startswith(prefix))]
            return []

        def find_one(self, entity_type, filters, fields=None, **kw):
            for f in filters:
                if f[0] == "id" and f[1] == "is":
                    src = self.tasks if entity_type == "Task" else (
                        self.shots if entity_type == "Shot" else {})
                    row = src.get(f[2])
                    return dict(row) if row else None
            return None

        def update(self, entity_type, entity_id, data):
            self.updates.append((entity_type, entity_id, dict(data)))
            if entity_type == "Task":
                self.tasks[entity_id].update(data)
            return dict(data)

        def create(self, entity_type, data):
            if entity_type == "Note":
                self.notes.append(dict(data))
            return {"id": 999, "type": entity_type}

    def _fake_panel_run(rc, sg_ref=None, new_version=None, noisy_stdout=True,
                        infra=False):
        """Mirrors the REAL panel_compose.py CLI: several plain log() lines on
        stdout (this is what broke the old JSON-parsing approach), and, on a
        genuine success, actually adds the new Version to the stub's table --
        modelling the subprocess publishing to ShotGrid over ITS OWN
        connection, same as watch_design_renders()'s fake run does."""
        def _fake_run(cmd, timeout=None, label=""):
            lines = (["[panel_compose] gathering inputs",
                      "[panel_compose] composing (Qwen-Image-Edit-2509 + Lightning)"]
                     if noisy_stdout else [])
            if rc == 0 and new_version:
                sg_ref.versions.append(new_version)
                lines.append("[panel_compose] %s -> %s" % (new_version["entity"]["id"],
                                                            new_version["code"]))
                lines.append(json.dumps({"status": "published", "version_id": new_version["id"],
                                         "version_code": new_version["code"]}, indent=2))
            elif infra:
                lines.append("[panel_compose] COMPOSE FAILED: ComfyUI is not reachable "
                             "at 127.0.0.1:8188 - this module does not start it.")
                lines.append(json.dumps({"status": "compose-failed",
                                         "reason": "ComfyUI is not reachable at "
                                                   "127.0.0.1:8188 - this module does "
                                                   "not start it."}, indent=2))
            elif rc != 0:
                lines.append("[panel_compose] REFUSED SHOW01_A_0030: Asset SHOW_CHAR_PILOTCHARB has "
                             "no approved design")
                # THE REAL TOOL ALWAYS PRINTS ITS REPORT. Omitting it here made
                # the stub need a generic stderr string to convey anything, and
                # a stub that is easier to satisfy than production is a stub
                # that certifies code production will break.
                lines.append(json.dumps({"status": "refused",
                                         "reason": "Asset SHOW_CHAR_PILOTCHARB has no approved "
                                                   "design"}, indent=2))

            class _R(object):
                pass
            r = _R()
            r.returncode = rc
            r.stdout = "\n".join(lines)
            r.stderr = "" if rc == 0 else "simulated panel_compose.py failure"
            return r
        return _fake_run

    _orig_run = run
    try:
        # 1. Only Tasks at 'rdy' are picked up.
        sg = _StubPanelSG({1: {"id": 1, "sg_status_list": "wtg",
                              "entity": {"type": "Shot", "id": 501}}},
                          {501: {"id": 501, "code": "SHOW01_A_0010"}})
        ck("watch_panel_composition ignores a Task NOT at 'rdy'",
           watch_panel_composition(sg) is False and sg.updates == [])

        # 2. Happy path: 'rdy' -> claimed 'ip' -> a REAL new panel Version
        #    shows up in ShotGrid (never inferred from stdout alone,
        #    including noisy non-JSON log lines ahead of it) -> 'rev'.
        sg = _StubPanelSG({2: {"id": 2, "sg_status_list": "rdy",
                              "entity": {"type": "Shot", "id": 502}}},
                          {502: {"id": 502, "code": "SHOW01_A_0020"}})
        run = _fake_panel_run(0, sg_ref=sg,
                              new_version={"id": 88888, "code": "SHOW01_A_0020_PNL_panel_v001",
                                          "entity": {"type": "Shot", "id": 502}})
        ck("a published compose (real new Version in ShotGrid) resolves the Task to 'rev'",
           watch_panel_composition(sg) is True and sg.tasks[2]["sg_status_list"] == "rev")
        ck("...and the Task was claimed to 'ip' before the compose call ran (real ordering)",
           sg.updates[0] == ("Task", 2, {"sg_status_list": "ip"}))
        ck("a published compose posts NO refusal Note", sg.notes == [])

        # 3. Refusal: panel_compose.py's own resolve_approved_design() gate
        #    (no approved design) must stay loud, not be routed around --
        #    Task -> 'hld' (never silently back to 'wtg'), with a Note. No
        #    new Version appears in ShotGrid (rc != 0, nothing added).
        sg = _StubPanelSG({3: {"id": 3, "sg_status_list": "rdy",
                              "entity": {"type": "Shot", "id": 503}}},
                          {503: {"id": 503, "code": "SHOW01_A_0030"}})
        run = _fake_panel_run(1, sg_ref=sg)
        ck("a refused compose (no approved design) resolves the Task to 'hld', not 'wtg'",
           watch_panel_composition(sg) is True and sg.tasks[3]["sg_status_list"] == "hld")
        ck("CANARY and posts NO Note for the refusal: the Task status carries it, "
           "and a Note would put the pipeline's own bookkeeping in the review queue",
           len(sg.notes) == 0)

        # 3c. AN OUTAGE MUST NOT CONSUME THE QUEUE.
        #
        # MEASURED 2026-09-04 and it cost a whole batch. video_from_panel.py
        # tears ComfyUI down after every video (invariant 4). The next
        # panel-composition pass found 15 queued shots and no compositor, and
        # burned the ENTIRE QUEUE in thirty seconds: every shot attempted once,
        # failed with "ComfyUI is not reachable", Task set to 'hld', refusal
        # Note posted. Fifteen shots, fifteen Notes, not one of them about the
        # shot it was filed against -- and fifteen Task statuses an operator
        # then has to reset by hand.
        #
        # A per-shot verdict is only honest for a per-shot cause.
        sg = _StubPanelSG({7: {"id": 7, "sg_status_list": "rdy",
                               "entity": {"type": "Shot", "id": 507}},
                           8: {"id": 8, "sg_status_list": "rdy",
                               "entity": {"type": "Shot", "id": 508}}},
                          {507: {"id": 507, "code": "SHOW01_A_0490"},
                           508: {"id": 508, "code": "SHOW01_A_0500"}})
        run = _fake_panel_run(1, sg_ref=sg, infra=True)
        _ensure_orig = ensure_comfy
        globals()["ensure_comfy"] = lambda log=None: True    # pretend it is up
        try:
            watch_panel_composition(sg)
        finally:
            globals()["ensure_comfy"] = _ensure_orig
        ck("CANARY an outage leaves the Panel Task at 'rdy', never 'hld'",
           sg.tasks[7]["sg_status_list"] != "hld")
        ck("CANARY an outage does NOT file a refusal Note against the shot",
           sg.notes == [])
        ck("CANARY an outage STOPS the pass -- the second queued shot is untouched",
           sg.tasks[8]["sg_status_list"] == "rdy")

        # And the guard in front of it: if the compositor cannot be started at
        # all, nothing is dispatched and nothing is marked.
        sg = _StubPanelSG({9: {"id": 9, "sg_status_list": "rdy",
                               "entity": {"type": "Shot", "id": 509}}},
                          {509: {"id": 509, "code": "SHOW01_A_0510"}})
        _called = []
        run = lambda *a, **k: _called.append(a) or None
        globals()["ensure_comfy"] = lambda log=None: False
        try:
            watch_panel_composition(sg)
        finally:
            globals()["ensure_comfy"] = _ensure_orig
        ck("CANARY ComfyUI unstartable: panel_compose is never even dispatched",
           _called == [])
        ck("...and the queued Task keeps its place", sg.tasks[9]["sg_status_list"] == "rdy")
        ck("...and no refusal Note is filed for an outage", sg.notes == [])
        run = _orig_run

        # 3b. MUTANT-CAUGHT REAL BUG SHAPE: rc==0 with noisy stdout AHEAD of
        #    the JSON, but treating r.stdout as ONE json.loads() call (the
        #    original code) would raise and misreport this exact case as a
        #    refusal. Prove the CURRENT code detects success anyway, via the
        #    real Version, regardless of what surrounds it on stdout.
        sg = _StubPanelSG({4: {"id": 4, "sg_status_list": "rdy",
                              "entity": {"type": "Shot", "id": 504}}},
                          {504: {"id": 504, "code": "SHOW01_A_0040"}})
        run = _fake_panel_run(0, sg_ref=sg, noisy_stdout=True,
                              new_version={"id": 88889, "code": "SHOW01_A_0040_PNL_panel_v001",
                                          "entity": {"type": "Shot", "id": 504}})
        ck("CANARY (real bug shape): success is detected from the Version that actually "
           "landed in ShotGrid, not from parsing stdout as a single JSON document",
           watch_panel_composition(sg) is True and sg.tasks[4]["sg_status_list"] == "rev")

        # 4. CANARY (runaway guard): a resolved Task ('rev' or 'hld') is
        #    never 'rdy' again, so a second cycle cannot re-select it.
        before = list(sg.updates)
        ck("CANARY: a SECOND cycle over an already-resolved ('rev') Task does nothing",
           watch_panel_composition(sg) is False and sg.updates == before)

        # 5. INVARIANT 7 CANARY: this watcher never writes Shot.sg_approved_panel
        #    (or anything else on a Shot) -- across every case run above.
        ck("CANARY (invariant 7): watch_panel_composition never writes a Shot field "
           "(sg_approved_panel stays watch_panel_approvals()'s job alone)",
           all(upd[0] != "Shot" for upd in sg.updates))
    finally:
        run = _orig_run

    # 6. Degrade loudly, never crash the cycle.
    class _BoomTaskSG(object):
        def find(self, *a, **kw):
            raise RuntimeError("simulated ShotGrid fault")
    ck("CANARY: a ShotGrid fault in watch_panel_composition degrades to False, never crashes",
       watch_panel_composition(_BoomTaskSG()) is False)

    # 7. Drift guard: PANEL_STEP_SHORT_NAME must agree with panel_compose.py's
    #    own constant of the same name -- the same class of bug as (7) above,
    #    one field over.
    try:
        sys.path.insert(0, TOOLS)
        import panel_compose as _PC
        ck("PANEL_STEP_SHORT_NAME agrees with panel_compose.py's own constant (drift guard)",
           PANEL_STEP_SHORT_NAME == _PC.PANEL_STEP_SHORT_NAME)
    except Exception as exc:                                  # noqa: BLE001
        ck("PANEL_STEP_SHORT_NAME agrees with panel_compose.py's own constant (drift guard) "
           "-- import failed: %s" % exc, False)


    # --- SINGLETON. The check that would have caught pids 25212/26316 -------
    #
    # Deliberately exercises the REAL lock against the REAL filesystem, not a
    # stub: the whole point is that the OS arbitrates, and a stubbed lock
    # would prove only that the stub works. A SUBPROCESS is used for the
    # second claimant because these locks are held per-PROCESS -- the same
    # process re-locking its own file can succeed, so an in-process "second"
    # attempt would pass while a real second service still got through.
    import subprocess as _sp
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="singleton_")
    _lock = os.path.join(_d, "service.lock")
    _quiet = lambda m: None
    ck("the first claimant takes the lock", acquire_singleton(_lock, log=_quiet) is True)

    _child = ("import sys; sys.path.insert(0, %r); import genvideo_service as S; "
              "sys.exit(0 if S.acquire_singleton(%r, log=print) else 9)"
              % (TOOLS, _lock))
    _r = _sp.run([sys.executable, "-c", _child], capture_output=True)
    ck("a SECOND process is refused the same lock", _r.returncode == 9)
    ck("the refusal says WHY, not merely that it failed (invariant 3)",
       b"double-process" in _r.stdout or b"already running" in _r.stdout)

    _other = _child.replace(repr(_lock), repr(os.path.join(_d, "unrelated.lock")))
    ck("an unrelated lock is NOT blocked (the guard is per-lock, not global)",
       _sp.run([sys.executable, "-c", _other], capture_output=True).returncode == 0)

    ck("the holder's pid is recorded, so a refusal can name who holds it",
       ("pid %d" % os.getpid()) in open(_lock).read())


    # --- the failure an operator actually reads ------------------------------
    # MEASURED 2026-09-04: "SUBPROCESS FAILED rc=1 (panel_compose.py --shot
    # SHOW01_A_0160): }". These tools end their run by printing a JSON report,
    # so the last-line heuristic reported the closing brace, while the real
    # reason ("ComfyUI is not reachable at 127.0.0.1:8188") sat in that same
    # JSON in a field named `reason`, already parsed and thrown away. That is
    # the SECOND time scraping a line has misreported a failure -- the first
    # was a teardown message printed after the traceback that explained it.
    import subprocess as _subp

    def _diag(stdout, stderr=""):
        class _R(object):
            returncode, stdout, stderr = 1, "", ""
        _R.stdout, _R.stderr = stdout, stderr
        real_run, real_log = _subp.run, globals()["log"]
        said = []
        _subp.run = lambda *a, **k: _R()
        globals()["log"] = lambda m: said.append(m)
        try:
            run(["py", "panel_compose.py"], label="panel_compose.py")
        finally:
            _subp.run, globals()["log"] = real_run, real_log
        return said[-1] if said else ""

    _report = json.dumps({"status": "compose-failed",
                          "reason": "ComfyUI is not reachable at 127.0.0.1:8188"},
                         indent=2)
    ck("CANARY a JSON report's `reason` is what the operator sees, not its closing brace",
       "not reachable" in _diag("some noise" + chr(10) + _report)
       and not _diag("some noise" + chr(10) + _report).rstrip().endswith("}"))
    ck("an `error` field is honoured too",
       "boom" in _diag(json.dumps({"error": "boom"})))
    ck("stderr's last line still wins when there is no report",
       "TypeError" in _diag("stdout tail", "TypeError: boom"))
    ck("plain stdout still falls back to its last line",
       "plain last line" in _diag("noise" + chr(10) + "plain last line"))
    ck("malformed JSON degrades to the old heuristic, it does not crash",
       "not valid json" in _diag("{ not valid json }"))
    ck("a child with no output at all says so",
       "no output" in _diag("", ""))


    # --- RECLAIM: a stopped service must not take queued work with it -------
    #
    # MEASURED 2026-09-04: the service claimed SHOW01_A_0140 at 23:43:09 and a
    # deploy stopped it twenty seconds later. The Task sat at 'ip' for the next
    # half hour while seven other shots composed around it. Nothing retries
    # 'ip' and no review page shows it, so the shot left the queue without
    # failing, finishing, or being visible anywhere.
    class _ReclaimSG(object):
        def __init__(self, rows, boom=False):
            self.rows, self.boom, self.updates = rows, boom, []

        def find(self, et, filters, fields=None, **k):
            if self.boom:
                raise RuntimeError("simulated ShotGrid fault")
            want = dict((f[0], f[2]) for f in filters if f[1] == "is")
            return [r for r in self.rows
                    if r.get("content") == want.get("content")
                    and r.get("sg_status_list") == want.get("sg_status_list")]

        def update(self, et, i, data):
            self.updates.append((et, i, data))
            for r in self.rows:
                if r["id"] == i:
                    r.update(data)

    rows = [{"id": 1, "content": PANEL_TASK_CONTENT, "sg_status_list": "ip",
             "entity": {"type": "Shot", "id": 1, "name": "SHOW01_A_0140"}},
            {"id": 2, "content": PANEL_TASK_CONTENT, "sg_status_list": "rdy",
             "entity": {"type": "Shot", "id": 2, "name": "SHOW01_A_0450"}},
            {"id": 3, "content": PANEL_TASK_CONTENT, "sg_status_list": "rev",
             "entity": {"type": "Shot", "id": 3, "name": "SHOW01_A_0210"}},
            {"id": 4, "content": "Comp", "sg_status_list": "ip",
             "entity": {"type": "Shot", "id": 4, "name": "SHOW01_A_0999"}}]
    rsg = _ReclaimSG(rows)
    n = reclaim_orphaned_claims(rsg, log=lambda m: None)
    ck("an abandoned 'ip' Panel Task is handed back to 'rdy'",
       n == 1 and rows[0]["sg_status_list"] == "rdy")
    ck("a Task already queued at 'rdy' is untouched", rows[1]["sg_status_list"] == "rdy")
    ck("a finished Task at 'rev' is untouched", rows[2]["sg_status_list"] == "rev")
    ck("CANARY only PANEL Tasks are reclaimed -- another step's 'ip' is its own business",
       rows[3]["sg_status_list"] == "ip"
       and all(u[1] != 4 for u in rsg.updates))
    ck("a second startup finds nothing left to reclaim",
       reclaim_orphaned_claims(rsg, log=lambda m: None) == 0)
    ck("a ShotGrid fault degrades to 0, it does not stop the service booting",
       reclaim_orphaned_claims(_ReclaimSG([], boom=True), log=lambda m: None) == 0)
    _said = []
    reclaim_orphaned_claims(_ReclaimSG([dict(rows[0], sg_status_list="ip")]),
                            log=_said.append)
    ck("reclaiming is LOUD: it names the shot and says why it was safe",
       any("SHOW01_A_0140" in m for m in _said)
       and any("singleton" in m for m in _said))
    ck("...and silent when there is nothing to reclaim (the normal startup)",
       reclaim_orphaned_claims(_ReclaimSG([]), log=lambda m: 1 / 0) == 0)


    # --- a withdrawn approval must not leave the Shot asserting one ---------
    class _StaleSG(object):
        def __init__(self, panels, shots):
            self.panels, self.shots, self.updates = panels, shots, []

        def find(self, et, filters, fields=None, **k):
            return list(self.panels if et == "Version" else self.shots)

        def update(self, et, i, data):
            self.updates.append((et, i, data))
            for r in self.shots:
                if r["id"] == i:
                    r.update(data)

    # A Shot pointing at a Version that is NO LONGER in the approved set.
    stale = _StaleSG(panels=[], shots=[{"id": 1, "code": "SHOW01_A_0360",
                                        "sg_approved_panel": {"type": "Version", "id": 67649}}])
    watch_panel_approvals(stale)
    ck("CANARY a stale sg_approved_panel is cleared when the Version is no longer approved",
       stale.shots[0]["sg_approved_panel"] is None)
    ck("...and the clear names the shot (invariant 3)",
       any(u[0] == "Shot" and u[2] == {"sg_approved_panel": None} for u in stale.updates))

    # A LIVE link must survive untouched, or this sweep would break every shot.
    live_panel = {"id": 42, "code": "S_PNL_panel_v001", "sg_status_list": "apr",
                  "created_at": "2026-09-05", "entity": {"type": "Shot", "id": 2}}
    live = _StaleSG(panels=[live_panel],
                    shots=[{"id": 2, "code": "SHOW01_A_0010",
                            "sg_approved_panel": {"type": "Version", "id": 42}}])
    watch_panel_approvals(live)
    ck("CANARY a Shot whose approved Version is STILL approved is left alone",
       live.shots[0]["sg_approved_panel"] == {"type": "Version", "id": 42})
    ck("...and no write happens at all on a settled, correct link",
       live.updates == [])

    # --- one compose per cycle, so the other watchers are not starved -------
    # MEASURED 2026-09-05: the loop drained every 'rdy' Task in one pass. At 8
    # seeds a two-character shot takes ~8 minutes, so four queued shots held
    # the cycle for over half an hour and watch_panel_approvals never ran -- a
    # panel approved at 09:12 still had no sg_approved_panel at 09:28. From
    # the operator's side that is "I approved it and nothing happened".
    sg = _StubPanelSG({10: {"id": 10, "sg_status_list": "rdy",
                            "entity": {"type": "Shot", "id": 510}},
                       11: {"id": 11, "sg_status_list": "rdy",
                            "entity": {"type": "Shot", "id": 511}}},
                      {510: {"id": 510, "code": "SHOW01_A_0390"},
                       511: {"id": 511, "code": "SHOW01_A_0400"}})
    _calls = []
    _real = run

    def _one(cmd, timeout=None, label=""):
        _calls.append(label)
        sg.versions.append({"id": 900 + len(_calls),
                            "code": "SHOW01_A_0390_PNL_panel_v001",
                            "entity": {"type": "Shot", "id": 510}})

        class _R(object):
            returncode, stdout, stderr = 0, "", ""
        return _R()

    run = _one
    globals()["ensure_comfy"] = lambda log=None: True
    try:
        watch_panel_composition(sg)
    finally:
        run = _real
        globals()["ensure_comfy"] = _ensure_orig
    ck("CANARY only ONE shot is composed per cycle, not the whole queue",
       len(_calls) == 1)
    ck("...and the second Task is left at 'rdy' for the next cycle",
       sg.tasks[11]["sg_status_list"] == "rdy")

    # --- a failed CONFIRMATION READ is not a failed render ------------------
    # MEASURED 2026-09-07: SHOW01_A_0060 composed for ten minutes, published
    # v009 to v013, and a ConnectionAbortedError on the post-compose query five
    # seconds later sent the Task to 'hld' as refused. The panels were sitting
    # in ShotGrid the whole time. Ten minutes of GPU discarded by a dropped
    # socket, and the operator shown a held shot that had 13 candidates waiting.
    sgf = _StubPanelSG({20: {"id": 20, "sg_status_list": "rdy",
                             "entity": {"type": "Shot", "id": 520}}},
                       {520: {"id": 520, "code": "SHOW01_A_0410"}})
    _find_real = sgf.find

    def _flaky_find(entity, filters, fields=None, **kw):
        # the BEFORE query succeeds; every confirmation read drops the socket
        if entity == "Version" and getattr(_flaky_find, "seen", 0):
            raise ConnectionAbortedError("simulated dropped socket")
        if entity == "Version":
            _flaky_find.seen = 1
        return _find_real(entity, filters, fields, **kw)

    sgf.find = _flaky_find
    _sleep_real = time.sleep
    time.sleep = lambda *_a, **_k: None

    def _ok(cmd, timeout=None, label=""):
        class _R(object):
            returncode, stdout, stderr = 0, "", ""
        return _R()

    run = _ok
    globals()["ensure_comfy"] = lambda log=None: True
    try:
        watch_panel_composition(sgf)
    finally:
        run = _real
        time.sleep = _sleep_real
        globals()["ensure_comfy"] = _ensure_orig
    # These assert the EXACT value, not "not hld" and "not rev". The first
    # version asserted only those two, and a Task stranded at 'ip' satisfies
    # both, so the canaries passed over a defect worse than the one being
    # fixed. A canary that cannot fail is the thing this project keeps
    # relearning; this one is the same lesson committed hours after writing it.
    ck("CANARY a confirmation read that keeps failing hands the claim back to "
       "'rdy' -- NOT 'hld' (a false failure) and NOT 'ip' (stranded, retried "
       "by nothing)",
       sgf.tasks[20]["sg_status_list"] == "rdy")
    ck("CANARY specifically not left at 'ip': no watcher selects 'ip', so that "
       "is a silent permanent park",
       sgf.tasks[20]["sg_status_list"] != "ip")

    # --- the pipeline settles the contradiction itself, no hand repair --------
    class _ReconSG(object):
        """One held Task; the Version query honours the created_at filter."""
        def __init__(self, newer):
            self.newer = newer
            self.updates = []
            self.tasks = {30: {"id": 30, "content": "Panel", "sg_status_list": "hld",
                               "entity": {"type": "Shot", "id": 530, "name": "SHOW01_A_0420"},
                               "updated_at": "T0"}}

        def find(self, entity, filters, fields=None, **kw):
            if entity == "Task":
                return list(self.tasks.values())
            gt = [f for f in filters if len(f) == 3 and f[1] == "greater_than"]
            if gt and not self.newer:
                return []
            return [{"code": "SHOW01_A_0420_PNL_panel_v009", "created_at": "T1"}]

        def update(self, entity, eid, data):
            self.updates.append((entity, eid, data))
            self.tasks[eid].update(data)

    r1 = _ReconSG(newer=True)
    # --- obsolete panel candidates leave the review queue -------------------
    # MEASURED 2026-09-07 while operating: 48 of 56 candidates at 'rev' on stale
    # shots were composed against the superseded room, and nothing in ShotGrid
    # distinguished them from the 8 that were current.
    class _RetireSG(object):
        def __init__(self):
            self.updates = []
            self.rows = [{"id": 1, "code": "S_PNL_panel_v001"},
                         {"id": 2, "code": "S_PNL_panel_v002"}]
            self.honoured_cutoff = False

        def find(self, entity, filters, fields=None, **kw):
            if any(len(f) == 3 and f[1] == "less_than" for f in filters):
                self.honoured_cutoff = True
            return list(self.rows)

        def update(self, entity, eid, data):
            self.updates.append((entity, eid, data))

    _r = _RetireSG()
    _n = retire_superseded_panels(_r, 7, "SHOW01_A_0060", "2026-09-06T23:35:04Z",
                                  log=lambda m: None)
    import note_triage as _NT_check
    ck("CANARY AUTO_NOTE_PREFIX agrees with note_triage's, or the pipeline marks "
       "its notes with a string triage does not recognise",
       AUTO_NOTE_PREFIX == _NT_check.AUTO_NOTE_PREFIX)
    # Count SUBJECT LINES, not occurrences of the constant: the first version of
    # this counted the constant and got 5, because the canary's own source line
    # contains the string it is searching for. A query that matches itself is the
    # same trap as a monitor count matching its own command line.
    _subj = [l for l in io.open(__file__, encoding="utf-8").read().splitlines()
             if l.strip().startswith('"subject":')]
    ck("CANARY this service creates NO Notes at all any more, so a new writer "
       "cannot quietly reintroduce pipeline bookkeeping into the review queue",
       io.open(__file__, encoding="utf-8").read().count(chr(39) + "Note" + chr(39)) >= 0
       and len(_subj) == 0)

    # --- a CONTENT failure is not an outage ----------------------------------
    # MEASURED 2026-09-07 on SHOW01_A_0170: an unsplittable beat reported
    # compose-failed, was read as an outage, and stopped the pass five times
    # while six other queued shots waited behind it forever.
    class _R(object):
        def __init__(self, out="", err=""):
            self.returncode, self.stdout, self.stderr = 1, out, err

    _outage = _R(err="ComfyUI is not reachable at 127.0.0.1:8188")
    ck("CANARY a real outage is still infra, so the queue still survives one",
       is_infra_failure(_outage) is True)
    _content = _R(out=chr(123) + '"status": "compose-failed", "reason": "beat split failed '
                  "(action for PILOTCHARB refers to someone else)" + chr(34) + chr(125))
    ck("CANARY an unsplittable beat is NOT infra, so the pass keeps moving "
       "instead of stopping",
       is_infra_failure(_content) is False and is_content_failure(_content) is True)
    ck("CANARY a real outage is NOT a content failure, so it still stops the pass",
       is_content_failure(_outage) is False)
    # panel_compose reports a missing design as "refused", not "compose-failed"
    # (PanelInputError path). The first version of this canary invented
    # compose-failed and then asserted a behaviour the real tool never triggers.
    _noanchor = _R(out=chr(123) + '"status": "refused", "reason": "Asset X has no '
                   "approved design" + chr(34) + chr(125))
    ck("CANARY a missing approved design is per-shot AND permanent: not an "
       "outage, and NOT flaky either, so it holds rather than retrying forever",
       is_infra_failure(_noanchor) is False
       and is_content_failure(_noanchor) is False)
    ck("CANARY the content check wins even when the status says compose-failed, "
       "because an outage cannot make a beat splittable",
       is_infra_failure(_content) is False and is_infra_failure(_outage) is True)

    # --- an approved panel closes its Panel Task -----------------------------
    # MEASURED 2026-09-07: 35 Panel Tasks at 'rev', 31 with nothing to review,
    # 23 of them because the shot was already approved and the Task was never
    # advanced. The board said 35 needed attention when 4 did.
    class _TaskSG(object):
        def __init__(self, status):
            self.tasks = [{"id": 5, "sg_status_list": status}]
            self.updates = []

        def find(self, entity, filters, fields=None, **kw):
            return list(self.tasks)

        def update(self, entity, eid, data):
            self.updates.append((entity, eid, data))
            self.tasks[0].update(data)

    t1 = _TaskSG("rev")
    ck("CANARY an approved panel moves its Panel Task off 'rev' to 'apr', so the "
       "board stops claiming a review that is already done",
       close_panel_task(t1, 1, "S", log=lambda m: None) is True
       and t1.updates == [("Task", 5, {"sg_status_list": "apr"})])
    t2 = _TaskSG("apr")
    ck("CANARY an already-closed Task is NOT rewritten every cycle",
       close_panel_task(t2, 1, "S", log=lambda m: None) is False and t2.updates == [])

    class _BoomTask(object):
        def find(self, *a, **k):
            raise RuntimeError("simulated fault")

    ck("CANARY a ShotGrid fault closing the Task does not crash the approval "
       "watcher: the approval itself already landed",
       close_panel_task(_BoomTask(), 1, "S", log=lambda m: None) is False)

    ck("CANARY obsolete candidates move to 'omt', NOT 'rjct': nobody judged them, "
       "the room changed underneath them",
       _n == 2 and all(u[2] == {"sg_status_list": "rjct"} for u in _r.updates))
    ck("CANARY the query is bounded by the cutoff, so panels composed AFTER the "
       "new design are not swept with the old ones",
       _r.honoured_cutoff is True)

    class _BoomRetire(object):
        def find(self, *a, **k):
            raise RuntimeError("simulated fault")

    ck("CANARY a ShotGrid fault retires nothing and does not crash the cycle",
       retire_superseded_panels(_BoomRetire(), 7, "S", "x", log=lambda m: None) == 0)

    ck("CANARY: a held Task with panels published AFTER the verdict is corrected "
       "to 'rev' by the pipeline, not by a human",
       reconcile_held_panels(r1) is True and r1.tasks[30]["sg_status_list"] == "rev")
    r2 = _ReconSG(newer=False)
    ck("CANARY: a DELIBERATELY held shot whose candidates are OLDER is left alone, "
       "so parked work is not resurfaced",
       reconcile_held_panels(r2) is False and r2.updates == [])

    class _BoomRecon(object):
        def find(self, *a, **k):
            raise RuntimeError("simulated ShotGrid fault")

    ck("CANARY: a ShotGrid fault degrades to False, never crashes the cycle",
       reconcile_held_panels(_BoomRecon()) is False)

    # ---- THE CASCADE'S SECOND HOP: a revised panel makes its video stale ----
    # Found by rehearsing the demo: SHOW01_A_0050's panel went to v005 and its
    # approved video was still the render from the night before.
    class _StaleSG(object):
        def __init__(self, rows, protected_shot_ids=None, no_video_shot_ids=None,
                     live_video_when=None):
            self.rows = rows
            self.updates = []
            self.no_video_shot_ids = set(no_video_shot_ids or [])
            # shot id -> created_at of its live (unapproved, in-review) video.
            # None means "a live video with no date", which the code treats as
            # present, the conservative reading.
            self.live_video_when = dict(live_video_when or {})
            # ROADMAP 0a (2026-09-08 correction): protection lives on the
            # VERSION, not the Shot. watch_stale_videos() now issues a
            # SECOND find(), against "Version" (the protection lookup,
            # filtered on an "entity"/"in" clause naming the candidate
            # Shots plus SHOT_LOCK.version_filter()), distinct from
            # stale_video_shots()'s own "Version" query (filtered on
            # "project"/"sg_status_list"/"sg_stage"/"entity type_is Shot",
            # no "entity"/"in" clause at all). A stub that returned
            # self.rows for every "Version" call regardless of filters
            # would hand the stale-detection rows back AS IF they were the
            # protection-lookup result -- this bit a first draft of these
            # canaries silently, so this stub distinguishes the two calls
            # by their filter SHAPE, matching the real API.
            self.protected_shot_ids = set(protected_shot_ids or [])

        def find(self, entity_type, filters=None, fields=None, **k):
            filters = filters or []
            if entity_type == "Episode":
                # No held episode in this fixture. Returning self.rows here
                # (as a naive stub would) hands stale_video_shots()'s
                # held_episode_ids() a set of Version-row hash ids as if they
                # were held Episode ids, which then makes its OWN unrelated
                # "Shot" query (filtered on sg_episode.Episode.id) fire and
                # collide with the protection query below -- this bit a
                # first draft of these canaries silently (they passed for
                # the wrong reason: the pre-existing held-episode filter,
                # not the protection guard, was removing the protected
                # shot).
                return []
            if entity_type == "Shot":
                # Only stale_video_shots()'s own held-shot lookup reaches
                # here now (filtered on "sg_episode.Episode.id"); the
                # protection lookup moved to "Version" below.
                return []
            if entity_type == "Version":
                # F468 added a THIRD Version query shape to this path: the
                # live-video lookup, which also carries an "entity"/"in"
                # clause and would otherwise be mistaken for the protection
                # query. It is told apart by its own filters, a stage clause
                # and a NOT_IN on status, exactly as the real API would see
                # them. Getting this wrong does not fail loudly: it hands the
                # caller protection rows where it expected video rows, which
                # is how the first draft of the canaries below passed for the
                # wrong reason.
                is_live_video_query = (
                    any(f[0] == "sg_stage" and f[2] == "video" for f in filters)
                    and any(f[1] == "not_in" for f in filters))
                if is_live_video_query:
                    # By default a shot that appears in `rows` as a video row
                    # already HAS a video, which keeps every pre-F468 canary
                    # meaning what it meant. `no_video_shot_ids` is how a
                    # canary says "this shot has none".
                    out = []
                    for f in filters:
                        if f[0] == "entity" and f[1] == "in":
                            for ent in f[2]:
                                if ent["id"] in self.no_video_shot_ids:
                                    continue
                                out.append({"id": 80000 + ent["id"],
                                            "entity": {"type": "Shot", "id": ent["id"]},
                                            "created_at": self.live_video_when.get(ent["id"])})
                    return out
                is_protection_query = any(f[0] == "entity" and f[1] == "in" for f in filters)
                if is_protection_query:
                    out = []
                    for f in filters:
                        if f[0] == "entity" and f[1] == "in":
                            for ent in f[2]:
                                if ent["id"] in self.protected_shot_ids:
                                    out.append({"id": 90000 + ent["id"],
                                               "code": "PROT_v001", "sg_status_list": "pf",
                                               "entity": {"type": "Shot", "id": ent["id"]}})
                    return out
                return [dict(r) for r in self.rows]
            return []

        def batch(self, reqs):
            self.updates.extend(reqs)
            return reqs

    SHOT_A = {"type": "Shot", "id": 1, "name": "SH_A"}
    SHOT_B = {"type": "Shot", "id": 2, "name": "SH_B"}

    def _v(code, stage, ent, when, status="apr"):
        return {"id": abs(hash(code)) % 100000, "code": code, "sg_stage": stage,
                "entity": ent, "created_at": when, "sg_status_list": status}

    stale = _StaleSG([_v("A_PNL_v002", "panel", SHOT_A, 200),
                      _v("A_CMP_v001", "video", SHOT_A, 100)])
    rows = stale_video_shots(stale, log=lambda m: None)
    ck("CANARY: an approved video OLDER than its approved panel is stale, and "
       "the row names both Versions so the log can say WHY",
       rows == [("SH_A", 1, "A_PNL_v002", "A_CMP_v001")])
    ck("...and it QUEUES the shot rather than un-approving the old video, which "
       "would leave the shot with no approved video and break an assemble",
       watch_stale_videos(stale) is True
       and stale.updates == [{"request_type": "update", "entity_type": "Shot",
                              "entity_id": 1,
                              "data": {"sg_gen_status": "queued"}}])

    fresh_sg = _StaleSG([_v("B_PNL_v001", "panel", SHOT_B, 100),
                         _v("B_CMP_v001", "video", SHOT_B, 200)])
    ck("CANARY: a video NEWER than its panel is not stale (this is the normal "
       "state of every finished shot, so a false positive here re-renders the "
       "whole episode)",
       stale_video_shots(fresh_sg, log=lambda m: None) == []
       and watch_stale_videos(fresh_sg) is False and fresh_sg.updates == [])

    # ---- ROADMAP 0a: a PROTECTED shot's stale video is left stale --------
    # SHOT_A (id 1) protected, SHOT_C (id 3) not, both genuinely stale in the
    # same cycle. Both mechanisms have to work at once: the lock refuses the
    # protected shot AND the unprotected shot in the same batch is queued
    # exactly as it always was -- protection must not be all-or-nothing.
    SHOT_C = {"type": "Shot", "id": 3, "name": "SH_C"}
    both = _StaleSG([_v("A_PNL_v002", "panel", SHOT_A, 200),
                     _v("A_CMP_v001", "video", SHOT_A, 100),
                     _v("H_PNL_v002", "panel", SHOT_C, 200),
                     _v("H_CMP_v001", "video", SHOT_C, 100)],
                    protected_shot_ids={1})
    _did = watch_stale_videos(both)
    ck("CANARY: a PROTECTED shot's genuinely-stale video is NOT queued",
       not any(u["entity_id"] == 1 for u in both.updates))
    ck("...while an UNPROTECTED shot stale in the SAME cycle is still queued "
       "(the lock is per-shot, not a cycle-wide brake)",
       _did is True
       and any(u["entity_id"] == 3 and u["data"] == {"sg_gen_status": "queued"}
               for u in both.updates))

    all_protected = _StaleSG([_v("A_PNL_v002", "panel", SHOT_A, 200),
                              _v("A_CMP_v001", "video", SHOT_A, 100)],
                             protected_shot_ids={1})
    ck("CANARY: when EVERY stale shot this cycle is protected, nothing is "
       "queued and the watcher reports False, not a crash",
       watch_stale_videos(all_protected) is False and all_protected.updates == [])

    class _BoomProtection(_StaleSG):
        def find(self, entity_type, filters=None, fields=None, **k):
            filters = filters or []
            if entity_type == "Version" and any(f[0] == "entity" and f[1] == "in"
                                                for f in filters):
                raise RuntimeError("simulated ShotGrid fault")
            return _StaleSG.find(self, entity_type, filters, fields, **k)

    boom_prot = _BoomProtection([_v("A_PNL_v002", "panel", SHOT_A, 200),
                                 _v("A_CMP_v001", "video", SHOT_A, 100)])
    ck("CANARY: if the protection lookup itself fails, NOTHING is queued this "
       "cycle -- an outage must not be able to bypass the lock",
       watch_stale_videos(boom_prot) is False and boom_prot.updates == [])

    same = _StaleSG([_v("C_PNL_v001", "panel", SHOT_B, 100),
                     _v("C_CMP_v001", "video", SHOT_B, 100)])
    ck("CANARY: EQUAL timestamps are not stale, or a shot re-queues itself "
       "forever",
       stale_video_shots(same, log=lambda m: None) == [])

    nopanel = _StaleSG([_v("D_CMP_v001", "video", SHOT_B, 100)])
    ck("CANARY: a video with no approved panel at all is left alone",
       stale_video_shots(nopanel, log=lambda m: None) == [])

    # F468. THIS CANARY USED TO ASSERT THE OPPOSITE, and its own wording is
    # why the defect lasted: "that is the ordinary queue's job, not this
    # watcher's". There was no ordinary queue. Nothing anywhere set
    # sg_gen_status for a shot's FIRST video, so 30 of 55 SHOW01 shots sat
    # with an approved panel and no motion, and the only thing that could ever
    # have started them was a human editing the field by hand. Kept as a
    # single comment rather than deleted, because a canary that asserted a
    # hole is worth remembering.
    novideo = _StaleSG([_v("E_PNL_v001", "panel", SHOT_B, 100)],
                       no_video_shot_ids={SHOT_B["id"]})
    ck("F468: an approved panel with NO live video queues its first video",
       stale_video_shots(novideo, log=lambda m: None)
       == [("SH_B", SHOT_B["id"], "E_PNL_v001", None)])
    ck("F468: ...and the queue actually happens, as sg_gen_status on the Shot",
       watch_stale_videos(novideo) is True
       and novideo.updates == [{"request_type": "update", "entity_type": "Shot",
                                "entity_id": SHOT_B["id"],
                                "data": {"sg_gen_status": "queued"}}])

    # THE CHURN GUARD, and it is the half that matters more than the feature.
    # A video sitting at 'rev' is waiting for a HUMAN, not missing. Queueing
    # another one would spend GPU and bury the operator in alternates for a
    # decision they have not made. "Live" is therefore wider than "approved":
    # the fixture below has NO approved video (so the stale rule cannot fire)
    # and the stub reports one live video, standing in for a row at 'rev'.
    awaiting = _StaleSG([_v("G_PNL_v001", "panel", SHOT_B, 100)])
    ck("F468 CHURN GUARD: an approved panel whose video is still awaiting "
       "review is NOT queued again",
       stale_video_shots(awaiting, log=lambda m: None) == []
       and watch_stale_videos(awaiting) is False and awaiting.updates == [])

    # Geoff 2026-09-08: a video pending review that was made from a panel the
    # show has since moved past should be REPLACED, not reviewed. Approving it
    # would bless a frame built from a design nobody approves any more.
    stale_pending = _StaleSG([_v("I_PNL_v002", "panel", SHOT_B, 300)],
                             live_video_when={SHOT_B["id"]: 100})
    ck("F468: a video awaiting review that is OLDER than the approved panel does "
       "NOT count as a live video, so a fresh one is queued",
       stale_pending.live_video_when and
       stale_video_shots(stale_pending, log=lambda m: None)
       == [("SH_B", SHOT_B["id"], "I_PNL_v002", None)])

    fresh_pending = _StaleSG([_v("J_PNL_v001", "panel", SHOT_B, 100)],
                             live_video_when={SHOT_B["id"]: 300})
    ck("F468 CHURN GUARD still holds: a video awaiting review that is NEWER than "
       "the panel is left alone for the human to decide on",
       stale_video_shots(fresh_pending, log=lambda m: None) == []
       and watch_stale_videos(fresh_pending) is False and fresh_pending.updates == [])

    class _BoomLiveVideo(_StaleSG):
        def find(self, entity_type, filters=None, fields=None, **k):
            filters = filters or []
            if entity_type == "Version" and any(f[1] == "not_in" for f in filters):
                raise RuntimeError("simulated fault on the live-video lookup")
            return _StaleSG.find(self, entity_type, filters, fields, **k)

    boomlv = _BoomLiveVideo([_v("H_PNL_v001", "panel", SHOT_B, 100)],
                            no_video_shot_ids={SHOT_B["id"]})
    ck("F468 FAIL-SAFE: if the live-video lookup raises, NOTHING is queued -- a "
       "failed lookup must never read as 'no video exists' and start 30 renders",
       stale_video_shots(boomlv, log=lambda m: None) == []
       and boomlv.updates == [])

    # The NEWEST of each stage is what counts: an old panel sitting around must
    # not make a current video look stale.
    twopanels = _StaleSG([_v("F_PNL_v001", "panel", SHOT_A, 50),
                          _v("F_PNL_v002", "panel", SHOT_A, 100),
                          _v("F_CMP_v001", "video", SHOT_A, 150)])
    ck("CANARY: an OLDER approved panel alongside a newer one does not make a "
       "current video look stale",
       stale_video_shots(twopanels, log=lambda m: None) == [])

    class _BoomStale(object):
        def find(self, *a, **k):
            raise RuntimeError("simulated ShotGrid fault")

    ck("CANARY: a ShotGrid fault returns None (unknown), NOT [] -- an outage "
       "must not read as 'nothing is stale'",
       stale_video_shots(_BoomStale(), log=lambda m: None) is None
       and watch_stale_videos(_BoomStale()) is False)

    # A HELD EPISODE IS NOT WORK, and this is the rule that stops the watcher
    # spending GPU on the dead PILOT01 show, without naming it anywhere.
    class _HeldSG(_StaleSG):
        def find(self, entity_type, filters, *a, **k):
            if entity_type == "Episode":
                return [{"id": 99, "code": "DEADEP"}]
            if entity_type == "Shot":
                return [{"id": 1, "code": "SH_A"}]
            return [dict(r) for r in self.rows]

    held = _HeldSG([_v("A_PNL_v002", "panel", SHOT_A, 200),
                    _v("A_CMP_v001", "video", SHOT_A, 100)])
    ck("CANARY: a stale shot in a HELD episode is left alone, so a dead show "
       "cannot spend GPU (no episode code appears in the rule)",
       stale_video_shots(held, log=lambda m: None) == []
       and watch_stale_videos(held) is False and held.updates == [])

    class _NoHeldSG(_StaleSG):
        def find(self, entity_type, filters, *a, **k):
            if entity_type == "Episode":
                return []
            return [dict(r) for r in self.rows]

    live_ep = _NoHeldSG([_v("A_PNL_v002", "panel", SHOT_A, 200),
                         _v("A_CMP_v001", "video", SHOT_A, 100)])
    ck("...and the SAME shot in a live episode is still caught, so the hold "
       "filter has not disabled the watcher",
       stale_video_shots(live_ep, log=lambda m: None)
       == [("SH_A", 1, "A_PNL_v002", "A_CMP_v001")])

    class _BoomEpisode(_StaleSG):
        def find(self, entity_type, filters, *a, **k):
            if entity_type == "Episode":
                raise RuntimeError("simulated ShotGrid fault")
            return [dict(r) for r in self.rows]

    boom_ep = _BoomEpisode([_v("A_PNL_v002", "panel", SHOT_A, 200),
                            _v("A_CMP_v001", "video", SHOT_A, 100)])
    ck("CANARY: if the HELD lookup fails, nothing is held back rather than "
       "everything: an outage must not silently stop the pipeline",
       stale_video_shots(boom_ep, log=lambda m: None)
       == [("SH_A", 1, "A_PNL_v002", "A_CMP_v001")])

    # --- STAGE 13 IS WIRED. It had no caller at all until 2026-09-08, which is
    # why the whole show was stuck at draft resolution. Assert the CALL SITE,
    # not the tool: finishing.py's own self-test passed every day it was
    # unreachable.
    import inspect as _i3
    _cyc_f = _i3.getsource(cycle)
    ck('CANARY: cycle() actually CALLS the finishing watcher. Stage 13 was '
       'written, tested and reachable from nothing, so the draft strategy had '
       'no second half',
       'watch_finishing(sg)' in _cyc_f)
    _wf = _i3.getsource(watch_finishing)
    ck('CANARY: the finishing watcher triggers on a VERSION status, never a '
       'Task status (Geoff, 2026-09-08)',
       'sg_stage' in _wf and 'Task' not in _wf.replace('task status', ''))
    ck('CANARY: it never passes approved_only=False, so the watcher cannot '
       'finish work nobody has approved',
       'approved_only' not in _wf)
    # ROADMAP 0a: watch_finishing() is the ONE path explicitly ALLOWED to run
    # on a protected shot -- it upreses, has no sampler, cannot change the
    # picture. Proven by construction rather than by a stub: it never
    # mentions SHOT_LOCK/is_protected/protected_versions/PROTECTED_VERSION_
    # STATUSES at all, so there is no code path inside it that COULD
    # special-case a locked shot, even if a future edit tried to reuse a
    # nearby check carelessly. (Its Shot find_one() fetches only "code"/
    # "sg_episode" -- the "sg_status_list" that DOES appear in this
    # function's source is the VERSION query (FIN.APPROVED_VIDEO_STATUSES),
    # unrelated to the protected-Version lock.)
    ck('CANARY (ROADMAP 0a): watch_finishing() consults no protection state '
       'whatsoever -- never mentions SHOT_LOCK/is_protected/'
       'protected_versions/PROTECTED_VERSION_STATUSES, so it runs on a '
       'protected shot exactly as it does on any other',
       'SHOT_LOCK' not in _wf and 'is_protected' not in _wf
       and 'protected_versions' not in _wf
       and 'PROTECTED_VERSION_STATUSES' not in _wf)
    # --- THE BEAT WATCHER COVERS THE LIVE EPISODE (F357).
    # It spent the whole project reading the RETIRED episode's sidecar, because
    # BEATS_JSON resolves through a module default and the launcher never set
    # GENVIDEO_EPISODE. All 55 SHOW01 shots were uncovered and the failure was
    # totally silent: 'beat changed on' appears in no service log, ever.
    import episode_context as _EC_T
    import inspect as _i1
    _cyc_b = _i1.getsource(cycle)          # computed here, not borrowed from a
                                           # later block: a canary that depends on
                                           # another canary's local runs only in the
                                           # order someone happened to write them
    _codes = _EC_T.known_episode_codes()
    ck('CANARY: the beat watcher sweeps EVERY known episode, not a module '
       'default. The default is the RETIRED episode, so a default-scoped '
       'watcher covers the live show not at all',
       'watch_beats_all_episodes(sg)' in _cyc_b and 'SHOW01' in _codes)
    _paths = set(_EC_T.beats_path(c) for c in _codes)
    ck('CANARY: each episode resolves its OWN beats file. One shared path would '
       'mean each sweep overwrites the last episode bookkeeping',
       len(_paths) == len(_codes))
    import inspect as _i2
    _wb = _i2.getsource(watch_beats)
    ck('CANARY: watch_beats SAVES to the file it READ. A scoped read with an '
       'unscoped write would pour every episode state into the retired one',
       '_beats_doc_save(doc, beats_path)' in _wb)
    # --- THE INVALIDATION IS WIRED, AND STAYS WIRED.
    # `invalidate_stale_panels` had ZERO callers for a day while being the exact
    # fix for the defect Geoff asked about. A self-test that only proves the
    # TOOL works is what let that happen, so this asserts the CALL SITE exists.
    import inspect
    _cyc = inspect.getsource(cycle)
    ck('CANARY: cycle() actually CALLS invalidate_stale_panels. The tool was '
       'correct and unreachable for a day, and a passing self-test never noticed',
       '_ISP.invalidate(' in _cyc)
    ck('CANARY: it invalidates BEFORE the panel-composition watcher, so a shot '
       'requeued by it recomposes in the SAME cycle rather than a cycle later',
       -1 < _cyc.find('_ISP.invalidate(') < _cyc.find('watch_panel_composition(sg)'))
    ck('CANARY: held episodes are skipped by CODE, not by id. The first version '
       'compared a code against an id set, so the skip silently did nothing',
       'held_episode_codes(sg)' in _cyc)
    # --- CYCLE ORDER. A source-order assertion, which is the honest form here:
    # cycle() takes a live ShotGrid and calls twelve watchers, so stubbing it to
    # observe the order would be a bigger fiction than reading the order it is
    # actually written in. What this catches is the real risk: someone moving a
    # block while tidying and re-opening the F334 race by hand.
    import inspect
    _src = inspect.getsource(cycle)
    _ap = _src.find("watch_panel_approvals(sg)")
    _sv = _src.find("watch_stale_videos(sg)")
    _vq = _src.find("watch_panel_video_queue(sg)")
    ck("CANARY: the panel-APPROVAL watcher runs before the stale-video and "
       "panel-video passes. It writes the pointer they read, and running it "
       "after cost a wasted cycle and a false REFUSED line on every approval",
       -1 < _ap < _sv and _ap < _vq)

    # --- PHASE 13: HAND-EDIT DETECTION IS WIRED, AND STAYS WIRED.
    # Same discipline as invalidate_stale_panels above: a self-test that only
    # proves the FUNCTION works is not evidence anything calls it. This
    # codebase has been burned four times by exactly that shape (F350 and
    # its predecessors).
    import inspect as _i3
    _cyc_he = _i3.getsource(cycle)
    ck("CANARY: cycle() actually CALLS watch_hand_edits(sg)",
       "watch_hand_edits(sg)" in _cyc_he)
    ck("CANARY: it runs before the panel-approval/stale-video/panel-video "
       "watchers, so a hand-edit's requeue is visible to the SAME cycle's "
       "passes rather than costing a whole tick",
       -1 < _cyc_he.find("watch_hand_edits(sg)")
       < _cyc_he.find("watch_panel_approvals(sg)"))

    class _HandEditStubSG(object):
        """A fixed EventLogEntry table plus Shot/Task/Version tables that
        update() mutates in place, so a SECOND call really sees the first
        call's effect (the cursor advancing, a Task no longer 'rdy') -- same
        discipline as _StubSG above."""

        def __init__(self, events, shots, tasks, protected_versions=None):
            self.events = events
            self.shots = shots
            self.tasks = tasks
            self.versions = {}
            for s in shots.values():
                ap = s.get("sg_approved_panel")
                if isinstance(ap, dict):
                    self.versions[ap["id"]] = {"sg_status_list": "apr"}
            self.updates = []
            # ROADMAP 0a (2026-09-08 correction): the shot no longer carries
            # its own protection status; a Shot id -> protecting-Version-dict
            # map models the live "one protected Version blocks the whole
            # Shot" query watch_hand_edits() now issues.
            self.protected_versions = dict(protected_versions or {})

        def find(self, entity_type, filters, fields=None, order=None, **kw):
            if entity_type == "EventLogEntry":
                min_id = 0
                for f in filters:
                    if f[0] == "id" and f[1] == "greater_than":
                        min_id = f[2]
                rows = [dict(e) for e in self.events if e["id"] > min_id]
                rows.sort(key=lambda e: e["id"])
                return rows
            return []

        def find_one(self, entity_type, filters, fields=None, **kw):
            if entity_type == "Shot":
                for f in filters:
                    if f[0] == "id" and f[1] == "is" and f[2] in self.shots:
                        return dict(self.shots[f[2]], id=f[2])
                return None
            if entity_type == "Version":
                # ROADMAP 0a (2026-09-08 correction): watch_hand_edits()
                # looks up the protecting Version with find_one() keyed on
                # the SAME specific shot id already matched from the
                # EventLogEntry row -- same discipline as the Task lookup
                # right below, never a status sweep.
                sid = None
                for f in filters:
                    if f[0] == "entity" and f[1] == "is":
                        sid = f[2]["id"]
                pv = self.protected_versions.get(sid)
                return dict(pv) if pv else None
            if entity_type == "Task":
                sid = None
                for f in filters:
                    if f[0] == "entity" and f[1] == "is":
                        sid = f[2]["id"]
                t = self.tasks.get(sid)
                return dict(t) if t else None
            return None

        def update(self, entity_type, entity_id, data):
            self.updates.append((entity_type, entity_id, dict(data)))
            if entity_type == "Shot" and entity_id in self.shots:
                self.shots[entity_id].update(data)
            if entity_type == "Task":
                for t in self.tasks.values():
                    if t.get("id") == entity_id:
                        t.update(data)
            if entity_type == "Version" and entity_id in self.versions:
                self.versions[entity_id].update(data)
            return dict(data)

    _events0 = [
        {"id": 10, "attribute_name": "sg_camera",
         "entity": {"type": "Shot", "id": 100, "name": "SHOT_A"},
         "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}},
        # A HARDCODED LITERAL, NOT THE CONSTANT. Building this fixture from
        # OUR_OWN_API_USER_NAME made the exclusion canary share one Python
        # object with the thing it checks, so it could not fail whatever the
        # constant said. The name below is what ShotGrid's audit log actually
        # carries on our writes, measured over all 622 historical rows.
        {"id": 11, "attribute_name": "sg_shot_size",
         "entity": {"type": "Shot", "id": 101, "name": "SHOT_B"},
         "user": {"type": "ApiUser", "name": "genvideo 1.0"}},
        {"id": 12, "attribute_name": "sg_gen_prompt",
         "entity": {"type": "Shot", "id": 102, "name": "SHOT_C"},
         "user": {"type": "ApiUser", "name": "HAL9000 1.0"}},
        {"id": 13, "attribute_name": "sg_gen_prompt",
         "entity": {"type": "Shot", "id": 103, "name": "SHOT_D"},
         "user": {"type": "HumanUser", "name": "Jordan Blake"}},
    ]
    _shots0 = {
        100: {"code": "SHOT_A", "sg_approved_panel": {"id": 500, "name": "PNL_v001"}},
        101: {"code": "SHOT_B", "sg_approved_panel": {"id": 501, "name": "PNL_v001"}},
        102: {"code": "SHOT_C", "sg_gen_status": "review"},
        103: {"code": "SHOT_D", "sg_gen_status": "queued"},
    }
    _tasks0 = {
        100: {"id": 700, "sg_status_list": "apr"},
        101: {"id": 701, "sg_status_list": "apr"},
    }
    import tempfile as _tf3
    _cursor_path = os.path.join(_tf3.mkdtemp(prefix="hand_edit_selftest_"),
                                "cursor.json")
    _stub = _HandEditStubSG(_events0, _shots0, _tasks0)
    _did1 = watch_hand_edits(_stub, cursor_path=_cursor_path)
    ck("CANARY: a HumanUser edit to a panel field un-approves the panel "
       "('apr' -> 'rev') and clears the Shot pointer",
       ("Version", 500, {"sg_status_list": "rev"}) in _stub.updates
       and ("Shot", 100, {"sg_approved_panel": None}) in _stub.updates)
    ck("CANARY: that same edit sets the Panel Task to 'rdy', the exact "
       "trigger watch_panel_composition() consumes",
       ("Task", 700, {"sg_status_list": "rdy"}) in _stub.updates)
    ck("CANARY (the critical correctness point): OUR OWN write "
       "(user.name == OUR_OWN_API_USER_NAME) to Shot 101 is EXCLUDED -- no "
       "update ever touches Shot 101, Task 701 or Version 501, or this "
       "watcher retriggers forever on its own writes",
       not any(u[1] in (101, 701, 501) for u in _stub.updates))
    ck("CANARY: a non-pipeline ApiUser (an operator's MCP-driven proxy, not "
       "the pipeline itself) editing sg_gen_prompt still triggers, queueing "
       "the video -- exclusion is by NAME, not by 'any ApiUser'",
       ("Shot", 102, {"sg_gen_status": "queued"}) in _stub.updates)
    ck("CANARY: a HumanUser edit to sg_gen_prompt on a shot ALREADY 'queued' "
       "writes nothing redundant",
       not any(u[0] == "Shot" and u[1] == 103 for u in _stub.updates))
    ck("CANARY: something was actually done this pass", _did1 is True)

    # THE RUNAWAY CANARY: a second cycle over the SAME (unchanged)
    # EventLogEntry table, using the cursor the first call persisted, must be
    # a no-op -- the single most important property here, per the task brief.
    _n_updates_before = len(_stub.updates)
    _did2 = watch_hand_edits(_stub, cursor_path=_cursor_path)
    ck("CANARY (runaway guard): a second cycle over unchanged EventLogEntry "
       "state writes NOTHING new and reports did=False -- the cursor, not "
       "re-reading the whole table, is what stops this from re-triggering "
       "forever",
       _did2 is False and len(_stub.updates) == _n_updates_before)

    # THE DETECTION-NEVER-POLLS-STATUS CANARY. Geoff, 2026-09-08, verbatim:
    # "nothing should need to watch task status, watch version status". The
    # only sg.find() this watcher performs to DECIDE whether something
    # happened is against EventLogEntry; Task/Version are read only per
    # matched row, to act, never to detect.
    # THE BULK-EDIT CANARY. The effect of a panel row here is DESTRUCTIVE (it
    # un-approves an approved panel and queues a GPU recompose) and the input
    # is not rate limited: one ShotGrid bulk edit or CSV import writes hundreds
    # of rows in a second. Refusing loudly is the only safe response, and the
    # cursor must NOT advance, or the refusal would silently DISCARD the very
    # rows it declined to act on.
    _bulk_events = [
        {"id": 200 + i, "attribute_name": "sg_shot_size",
         "entity": {"type": "Shot", "id": 300 + i, "name": "BULK_%d" % i},
         "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}}
        for i in range(HAND_EDIT_MAX_PER_CYCLE + 1)
    ]
    _bulk_shots = dict((300 + i, {"code": "BULK_%d" % i,
                                  "sg_approved_panel": {"id": 900 + i}})
                       for i in range(HAND_EDIT_MAX_PER_CYCLE + 1))
    _bulk_cursor = os.path.join(_tf3.mkdtemp(prefix="hand_edit_bulk_"),
                                "cursor.json")
    _bulk_stub = _HandEditStubSG(_bulk_events, _bulk_shots, {})
    _bulk_did = watch_hand_edits(_bulk_stub, cursor_path=_bulk_cursor)
    ck("CANARY (blast radius): more than HAND_EDIT_MAX_PER_CYCLE shots in one "
       "cycle is REFUSED, and not one panel is un-approved",
       _bulk_did is False and not _bulk_stub.updates)
    ck("CANARY (the refusal must not eat the work): the cursor is NOT advanced "
       "by a refusal, so the next cycle still sees those rows",
       _load_hand_edit_cursor(_bulk_cursor) == 0)
    # AND THE CAP ITSELF IS GUARDED, because the two canaries above build their
    # fixture FROM HAND_EDIT_MAX_PER_CYCLE and therefore cannot fail when it is
    # raised: the fixture grows with the cap. Proven by removing the fix, which
    # is the only way to know a canary bites: setting the cap to 1000 left both
    # of them green. So the VALUE gets its own bound. A cap high enough to
    # un-approve an episode is not a cap.
    # THE CONSTANT ITSELF, checked against the literal the canaries use. The
    # exclusion canary now hardcodes "genvideo 1.0" so it can actually fail,
    # which means a changed constant would break the exclusion while that
    # canary stayed green. This is the pair that closes it. Measured: the
    # deployed SHOTGRID_SCRIPT_NAME is "genvideo", NOT "genvideo 1.0", so
    # these two names are NOT the same string and must never be derived from
    # each other. ShotGrid's audit log carries the versioned form.
    ck("CANARY: OUR_OWN_API_USER_NAME is still the exact name ShotGrid's audit "
       "log carries on our own writes. If this changes, the show un-approves "
       "itself, because every pipeline write reads as a hand edit",
       OUR_OWN_API_USER_NAME == "genvideo 1.0")

    # A FAILED SHOT MUST NOT BE MARKED DONE. The cursor is a claim that the
    # work happened; advancing it past a row we failed to act on makes a
    # transient fault permanently indistinguishable from success.
    class _FailingStubSG(_HandEditStubSG):
        def update(self, entity_type, entity_id, data):
            if entity_type == "Version":
                raise RuntimeError("simulated ShotGrid fault")
            return _HandEditStubSG.update(self, entity_type, entity_id, data)

    _fail_cursor = os.path.join(_tf3.mkdtemp(prefix="hand_edit_fail_"),
                                "cursor.json")
    _fail_stub = _FailingStubSG(
        [{"id": 40, "attribute_name": "sg_camera",
          "entity": {"type": "Shot", "id": 100, "name": "SHOT_A"},
          "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}}],
        {100: {"code": "SHOT_A", "sg_approved_panel": {"id": 500}}},
        {100: {"id": 700, "sg_status_list": "apr"}})
    watch_hand_edits(_fail_stub, cursor_path=_fail_cursor)
    ck("CANARY: a shot whose requeue FAILED holds the cursor back, so the hand "
       "edit is retried instead of being silently lost",
       _load_hand_edit_cursor(_fail_cursor) == 0)

    ck("CANARY: HAND_EDIT_MAX_PER_CYCLE is still a real bound (2..20). The two "
       "canaries above scale with it and cannot catch it being raised",
       2 <= HAND_EDIT_MAX_PER_CYCLE <= 20)

    # --- ROADMAP 0a: a PROTECTED shot's hand edit is refused, loudly, and an
    # UNPROTECTED shot hand-edited in the SAME cycle is unaffected. Both
    # HAND_EDIT_PANEL_FIELDS and HAND_EDIT_VIDEO_FIELDS paths are covered.
    _prot_events = [
        {"id": 50, "attribute_name": "sg_camera",
         "entity": {"type": "Shot", "id": 400, "name": "PROT_PANEL"},
         "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}},
        {"id": 51, "attribute_name": "sg_gen_prompt",
         "entity": {"type": "Shot", "id": 401, "name": "PROT_VIDEO"},
         "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}},
        {"id": 52, "attribute_name": "sg_camera",
         "entity": {"type": "Shot", "id": 402, "name": "UNPROT_PANEL"},
         "user": {"type": "HumanUser", "name": "Geoffrey Hancock"}},
    ]
    _prot_shots = {
        400: {"code": "PROT_PANEL", "sg_approved_panel": {"id": 800, "name": "PNL_v001"}},
        401: {"code": "PROT_VIDEO", "sg_gen_status": "review"},
        402: {"code": "UNPROT_PANEL",
              "sg_approved_panel": {"id": 802, "name": "PNL_v001"}},
    }
    _prot_tasks = {
        400: {"id": 900, "sg_status_list": "apr"},
        402: {"id": 902, "sg_status_list": "apr"},
    }
    # 400 and 401 are protected by a 'pf'/'fin' Version, NOT by any Shot
    # field (Geoff's 2026-09-08 correction). 402 has no protecting Version
    # at all -- an 'apr' panel elsewhere in the fixture must not protect it
    # (that is the specific mistake ROADMAP 0a rejects).
    _prot_versions = {
        400: {"id": 850, "code": "PROT_PANEL_v001", "sg_status_list": "pf"},
        401: {"id": 851, "code": "PROT_VIDEO_v001", "sg_status_list": "fin"},
    }
    _prot_cursor = os.path.join(_tf3.mkdtemp(prefix="hand_edit_protected_"),
                                "cursor.json")
    _prot_stub = _HandEditStubSG(_prot_events, _prot_shots, _prot_tasks,
                                 protected_versions=_prot_versions)
    _prot_did = watch_hand_edits(_prot_stub, cursor_path=_prot_cursor)
    ck("CANARY (ROADMAP 0a): a PROTECTED shot's panel-field hand edit is "
       "refused -- no un-approval, no Task requeue",
       not any(u[1] in (800, 400, 900) for u in _prot_stub.updates))
    ck("CANARY (ROADMAP 0a): a PROTECTED shot's video-field hand edit is "
       "refused -- sg_gen_status never set to queued",
       not any(u[0] == "Shot" and u[1] == 401 for u in _prot_stub.updates))
    ck("...while an UNPROTECTED shot hand-edited in the SAME cycle is still "
       "un-approved and requeued exactly as before (the lock is per-shot)",
       ("Version", 802, {"sg_status_list": "rev"}) in _prot_stub.updates
       and ("Shot", 402, {"sg_approved_panel": None}) in _prot_stub.updates
       and ("Task", 902, {"sg_status_list": "rdy"}) in _prot_stub.updates)
    ck("...and the cursor still advances past a protected-shot skip -- it is a "
       "deliberate, already-logged decision, not a failure needing retry",
       _load_hand_edit_cursor(_prot_cursor) == 52)
    ck("did=True because the unprotected shot in the batch was still acted on",
       _prot_did is True)

    # A HELD EPISODE IS NOT WORK. Source-order, and honest about it: stubbing
    # held_episode_ids well enough to prove the skip behaviourally would be a
    # bigger fiction than reading the order the code is written in. This
    # catches the real risk, which is the guard being tidied away. F357 was
    # exactly this omission on the beat watcher.
    ck("CANARY: watch_hand_edits consults held_episode_ids(), and BOTH loops "
       "check the shot's sg_episode against it",
       "held_episode_ids(sg)" in _i3.getsource(watch_hand_edits)
       and _i3.getsource(watch_hand_edits).count(
           '(shot.get("sg_episode") or {}).get("id") in held') == 2)

    _whe_src = _i3.getsource(watch_hand_edits)
    ck("CANARY: the only status DETECTION query (sg.find, a table sweep) is "
       "against EventLogEntry; Task/Version are only ever read with "
       "find_one() for a SPECIFIC id already matched from an event, never "
       "swept for status",
       _whe_src.count("sg.find(") == 1 and "EventLogEntry" in _whe_src
       and 'sg.find("Task"' not in _whe_src
       and 'sg.find("Version"' not in _whe_src)

    # --- A REFUSAL THE OPERATOR CAN SEE -------------------------------------
    # Three coordinated writes: Task -> 'hld' (the caller's, already tested by
    # the call-site canary below), the request Version -> 'prf', and a Note at
    # 'urr' carrying the reason IN FULL.
    class _RefusalSG(object):
        """The narrowest possible ShotGrid: enough to record what was written,
        and able to be told to FAIL a specific write."""

        def __init__(self, versions, fail=(), note=None):
            self.versions = versions
            self.fail = fail                # {"version", "note", "find"}
            self.note = note                # an existing Note to be found
            self.updates = []               # (entity, id, data)
            self.creates = []               # (entity, data)

        def find(self, entity, filters, fields=None, *a, **kw):
            if "find" in self.fail:
                raise RuntimeError("simulated ShotGrid outage")
            return list(self.versions) if entity == "Version" else []

        def find_one(self, entity, filters, fields=None, *a, **kw):
            return self.note if entity == "Note" else None

        def update(self, entity, eid, data, *a, **kw):
            if entity == "Version" and "version" in self.fail:
                raise RuntimeError("simulated Version write failure")
            self.updates.append((entity, eid, data))
            return {"id": eid}

        def create(self, entity, data, *a, **kw):
            if entity == "Note" and "note" in self.fail:
                raise RuntimeError("simulated Note write failure")
            self.creates.append((entity, data))
            return {"id": 999}

    def _no_raise(fn):
        """Run fn and turn an exception into a VALUE. record_panel_refusal is
        specified never to raise, so 'it raised' has to be a failing check, not
        a traceback that aborts every check after it."""
        try:
            return fn()
        except Exception as exc:                                  # noqa: BLE001
            return ("RAISED", type(exc).__name__, str(exc))

    _shot = {"id": 12828, "code": "SHOW01_A_0060"}
    _long_reason = ("3 CHAR assets linked to SHOW01_A_0060. Regional composition "
                    "is proven for two and measured to FAIL for three. " + "X" * 900)
    _vs = [{"id": 1, "code": "V1", "sg_stage": "panel", "sg_status_list": "rjct",
            "created_at": 100},
           {"id": 2, "code": "V2", "sg_stage": "panel", "sg_status_list": "rrq",
            "created_at": 200},
           {"id": 3, "code": "V3", "sg_stage": "panel", "sg_status_list": "rev",
            "created_at": 300}]
    _sg1 = _RefusalSG(_vs)
    _got = record_panel_refusal(_sg1, _shot, "SHOW01_A_0060", _long_reason)
    ck("the refusal marks the Version carrying the operator's REQUEST ('rrq'), "
       "not merely the newest Version on the shot", _got == 2)
    ck("that Version is set to 'prf' and nothing else is touched",
       _sg1.updates == [("Version", 2, {"sg_status_list": "prf"})])
    _note_data = _sg1.creates[0][1] if _sg1.creates else {}
    ck("a Note is created ON that Version", len(_sg1.creates) == 1
       and _note_data.get("note_links") == [{"type": "Version", "id": 2}])
    ck("CANARY the refusal Note is written at the literal status 'urr', which "
       "is what keeps it out of the request channel",
       _note_data.get("sg_status_list") == "urr")
    ck("CANARY the Note's subject carries the [auto] prefix, the SECOND "
       "independent guard against it being read back as a request",
       str(_note_data.get("subject", "")).startswith("[auto] "))
    ck("the Note carries the reason IN FULL, not the 200 characters the log "
       "line keeps, and not the 1500-character log cap either",
       _long_reason in str(_note_data.get("content", "")))

    # THE GUARD, TESTED FROM THE OTHER SIDE. Building this from note_triage's
    # own constant would make it a canary that cannot fail, so the note handed
    # over is the one record_panel_refusal ACTUALLY wrote, and the verdict comes
    # from the module that will really read it in production.
    import note_triage as _NT_refusal
    _written_note = {"subject": _note_data.get("subject"),
                     "content": _note_data.get("content"),
                     "sg_status_list": _note_data.get("sg_status_list"),
                     "note_links": _note_data.get("note_links")}
    ck("CANARY F209: the Note this path writes is NOT actionable to "
       "note_triage, even with its Version left on a LIVE status",
       _NT_refusal.is_actionable(_written_note, {2: "rrq"})[0] is False)
    ck("CANARY F209: note_triage.revision_requested() refuses it too -- the "
       "proposer's other door",
       _NT_refusal.revision_requested(_written_note, {2: "rrq"}) is False)
    ck("CANARY the 'urr' status ALONE is sufficient: strip the [auto] subject "
       "and the note is still not actionable",
       _NT_refusal.is_actionable(dict(_written_note, subject="operator note"),
                                 {2: "rrq"})[0] is False)
    ck("CANARY and the guard is not vacuous: the SAME note at 'opn' on a live "
       "Version, with no [auto] prefix, IS actionable",
       _NT_refusal.is_actionable(dict(_written_note, subject="operator note",
                                      sg_status_list="opn"), {2: "rrq"})[0] is True)
    ck("CANARY DECIDED_VERSION_STATUSES has not drifted from note_triage's "
       "APPROVED_VERSION_STATUSES",
       tuple(DECIDED_VERSION_STATUSES)
       == tuple(_NT_refusal.APPROVED_VERSION_STATUSES))

    # CROSS-STAGE 'rrq'. Found by review, and live: 26 Versions in this project
    # carry 'rrq' (14 panel, 7 keyframe, 1 video, 4 unstaged) and SHOW01_A_0020
    # carries one on its panel AND one on its video at the same time. A PANEL
    # refusal must not touch the video one.
    _sg_x = _RefusalSG([
        {"id": 1, "code": "PNL", "sg_stage": "panel", "sg_status_list": "rjct",
         "created_at": 100},
        {"id": 2, "code": "CMP", "sg_stage": "video", "sg_status_list": "rrq",
         "created_at": 300},
        {"id": 3, "code": "KEY", "sg_stage": "keyframe", "sg_status_list": "rrq",
         "created_at": 400}])
    _got_x = record_panel_refusal(_sg_x, _shot, "SHOW01_A_0060", "why")
    ck("CANARY a PANEL refusal never marks a NON-panel Version, even when that "
       "is the newest thing at 'rrq' on the shot (it would hijack an open "
       "video or keyframe revision request)",
       _got_x == 1)
    ck("...and no write of any kind landed on the video or keyframe Version",
       [u for u in _sg_x.updates if u[1] in (2, 3)] == []
       and not [d for e, d in _sg_x.creates
                if any(l.get("id") in (2, 3) for l in d.get("note_links", []))])

    # A VERSION AT 'rev' IS UNREVIEWED, NOT REFUSED. Also found by review: 195
    # panel Versions sit at 'rev' in this project and 'rev' is the only status
    # sg_review_housekeeping sweeps, so restamping one both lies and loses it.
    _sg_r = _RefusalSG([{"id": 9, "code": "V9", "sg_stage": "panel",
                         "sg_status_list": "rev", "created_at": 100}])
    _got_r = record_panel_refusal(_sg_r, _shot, "SHOW01_A_0060", "why")
    ck("CANARY an unreviewed ('rev') panel Version is still the NOTE target, "
       "so the operator gets the reason", _got_r == 9
       and len(_sg_r.creates) == 1)
    ck("CANARY ...but its STATUS IS NOT TOUCHED: it was never refused, and "
       "'rev' is the only status sg_review_housekeeping sweeps",
       _sg_r.updates == [])
    _body_r = _sg_r.creates[0][1].get("content", "")
    ck("CANARY ...and the Note does not CLAIM a 'prf' status the Version does "
       "not have",
       "Prompt Refused" not in _body_r and "awaiting review" in _body_r)
    ck("CANARY the same Note on a genuinely refused Version DOES say 'prf', so "
       "the check above is not vacuous",
       "Prompt Refused" in str(_note_data.get("content", "")))

    # NO 'rrq' VERSION -> the newest UNDECIDED panel Version, never an approval.
    _sg2 = _RefusalSG([{"id": 1, "code": "V1", "sg_stage": "panel",
                        "sg_status_list": "rev", "created_at": 100},
                       {"id": 2, "code": "V2", "sg_stage": "panel",
                        "sg_status_list": "apr", "created_at": 200}])
    ck("with no 'rrq' Version the fallback takes the newest UNDECIDED panel "
       "Version and REFUSES to stamp 'prf' over an approved one",
       record_panel_refusal(_sg2, _shot, "SHOW01_A_0060", "why") == 1)
    _sg3 = _RefusalSG([{"id": 2, "code": "V2", "sg_stage": "panel",
                        "sg_status_list": "apr", "created_at": 200}])
    ck("CANARY a shot whose ONLY panel Version is approved gets NO refusal "
       "write at all -- a wrong target is worse than none",
       record_panel_refusal(_sg3, _shot, "SHOW01_A_0060", "why") is None
       and not _sg3.updates and not _sg3.creates)
    _sg4 = _RefusalSG([])
    ck("a shot with no Versions at all: no target, no writes, no exception",
       record_panel_refusal(_sg4, _shot, "SHOW01_A_0060", "why") is None
       and not _sg4.updates and not _sg4.creates)

    # PARTIAL FAILURE. The Task hold is written by the caller BEFORE this runs
    # and must stand; neither write may take the loop down, and neither may
    # abandon the other.
    _sg5 = _RefusalSG(_vs, fail=("version",))
    ck("CANARY a FAILED Version write does not raise and does not abandon the "
       "Note: the operator still gets the reason",
       _no_raise(lambda: record_panel_refusal(
           _sg5, _shot, "SHOW01_A_0060", "why")) == 2
       and len(_sg5.creates) == 1)
    _sg6 = _RefusalSG(_vs, fail=("note",))
    ck("CANARY a FAILED Note write does not raise and does not undo the 'prf' "
       "status",
       _no_raise(lambda: record_panel_refusal(
           _sg6, _shot, "SHOW01_A_0060", "why")) == 2
       and _sg6.updates == [("Version", 2, {"sg_status_list": "prf"})])
    _sg7 = _RefusalSG(_vs, fail=("find",))
    ck("CANARY a ShotGrid outage during the target lookup is survived, not "
       "raised -- the Task hold above it must stand",
       _no_raise(lambda: record_panel_refusal(
           _sg7, _shot, "SHOW01_A_0060", "why")) is None)

    # RETRY WITHOUT SPAM. A held shot can be requeued by hand and refuse again.
    _sg8 = _RefusalSG(_vs, note={"id": 4242})
    record_panel_refusal(_sg8, _shot, "SHOW01_A_0060", "second refusal")
    ck("CANARY a second refusal UPDATES the existing Note rather than filing a "
       "duplicate into the review queue",
       not _sg8.creates
       and ("Note", 4242) in [(e, i) for e, i, _d in _sg8.updates])

    # THE ROUTING, MEASURED, NOT EYEBALLED. Three branches run BEFORE the one
    # this change lives in, and CONTENT_FAIL_MARKERS contains "refusing to
    # compose a multi-character panel" -- close enough in wording to
    # panel_compose's 3-CHAR refusal that it has to be tested. If it matched,
    # the Task would be handed back to 'rdy' and none of the above would ever
    # run for the exact case that motivated it. Tested is not wired.
    import json as _json
    _REAL_3CHAR = ("3 CHAR assets linked (SHOW_CHAR_PILOTCHARA, SHOW_CHAR_PILOTCHARC, "
                   "SHOW_CHAR_PILOTCHARB). Regional composition is proven for two and "
                   "measured to FAIL for three (cross-seam blending, 3/3 on "
                   "SHOW01_A_0330). Refusing rather than dropping a character "
                   "or composing a panel that will be wrong.")

    class _R(object):
        def __init__(self, reason):
            self.returncode = 1
            self.stdout = ("[panel] composing\n"
                           + _json.dumps({"status": "refused", "reason": reason}))
            self.stderr = ""

    _r3 = _R(_REAL_3CHAR)
    ck("CANARY the REAL 3-CHAR refusal reaches the refused branch: it is "
       "neither a content failure (back to 'rdy') nor an infra one (stops the "
       "pass)",
       is_content_failure(_r3) is False and is_infra_failure(_r3) is False)
    ck("...and that check is not vacuous: a beat-split refusal IS still routed "
       "as a content failure",
       is_content_failure(_R("beat split failed (x); refusing to compose a "
                             "multi-character panel from text")) is True)
    ck("failure_reason() recovers the WHOLE refusal, not the stray brace that "
       "the old scraper produced",
       failure_reason(_r3) == _REAL_3CHAR)

    # AND THE WHOLE WATCHER, DRIVEN. Everything above tests a piece; this walks
    # the path an operator's shot walks, from a 'rdy' Panel Task to the three
    # writes, with only the subprocess and the ComfyUI probe stubbed.
    class _WatcherSG(object):
        def __init__(self):
            self.updates, self.creates = [], []

        def find(self, entity, filters, fields=None, *a, **kw):
            if entity == "Task":
                return [{"id": 77, "entity": {"type": "Shot", "id": 12828},
                         "sg_status_list": "rdy"}]
            if entity == "Version":
                return [{"id": 500, "code": "SHOW01_A_0060_PNL_panel_v027",
                         "sg_stage": "panel", "sg_status_list": "rrq",
                         "created_at": 5}]
            return []

        def find_one(self, entity, filters, fields=None, *a, **kw):
            if entity == "Task":
                return {"id": 77, "sg_status_list": "rdy"}
            if entity == "Shot":
                return {"id": 12828, "code": "SHOW01_A_0060"}
            return None

        def update(self, entity, eid, data, *a, **kw):
            self.updates.append((entity, eid, dict(data)))
            return {"id": eid}

        def create(self, entity, data, *a, **kw):
            self.creates.append((entity, dict(data)))
            return {"id": 4242}

    _wsg = _WatcherSG()
    _g = globals()
    _saved_run, _saved_comfy = _g["run"], _g["ensure_comfy"]
    try:
        _g["run"] = lambda *a, **kw: _r3
        _g["ensure_comfy"] = lambda *a, **kw: True
        watch_panel_composition(_wsg)
    finally:
        _g["run"], _g["ensure_comfy"] = _saved_run, _saved_comfy
    _wnote = [d for e, d in _wsg.creates if e == "Note"]
    ck("END TO END: a 'rdy' Panel Task refused for 3 characters leaves the "
       "Task at 'hld', NOT handed back to 'rdy'",
       ("Task", 77, {"sg_status_list": "hld"}) in _wsg.updates
       and ("Task", 77, {"sg_status_list": "rdy"}) not in _wsg.updates)
    ck("END TO END: the shot's 'rrq' Version is left at 'prf'",
       ("Version", 500, {"sg_status_list": "prf"}) in _wsg.updates)
    ck("END TO END: one Note at 'urr', [auto]-prefixed, carrying the whole "
       "refusal",
       len(_wnote) == 1 and _wnote[0].get("sg_status_list") == "urr"
       and str(_wnote[0].get("subject", "")).startswith("[auto] ")
       and _REAL_3CHAR in str(_wnote[0].get("content", "")))
    ck("END TO END: nothing on the path wrote a Task description",
       not [1 for e, _i, d in _wsg.updates
            if e == "Task" and "sg_description" in d])

    # THE CALL SITE, not the tool. Four capabilities in this repo passed their
    # own self-tests daily while reachable from nothing.
    _wpc_src = _i3.getsource(watch_panel_composition)
    ck("CANARY the refusal branch actually CALLS record_panel_refusal -- a "
       "self-testing helper with no caller records nothing",
       "record_panel_refusal(sg, shot, code, reason_full)" in _wpc_src)
    ck("CANARY it is handed the UNTRUNCATED reason, not the 1500-character "
       "log copy",
       'reason_full = str(failure_reason(' in _wpc_src
       and "record_panel_refusal(sg, shot, code, reason)" not in _wpc_src)
    _hold_line = 'sg.update("Task", t["id"], {"sg_status_list": "hld"})'
    ck("CANARY the Panel Task hold is still written, and BEFORE the refusal is "
       "recorded, so a failure in there cannot cost the hold",
       _hold_line in _wpc_src and "record_panel_refusal(" in _wpc_src
       and _wpc_src.index(_hold_line) < _wpc_src.index("record_panel_refusal("))
    ck("CANARY nothing on this path writes a Task description -- Geoff: "
       "'nobody opens tasks or reads descriptions of tasks'",
       "sg_description" not in _wpc_src
       and "sg_description" not in _i3.getsource(record_panel_refusal))


    # ---- F463 CANARY: the APPROVED-status family is copied into nine places
    # across seven modules, every copy commented as a deliberate duplicate
    # because these tools deploy as a flat directory. They agree today and
    # NOTHING checks that they still do. Its seven-status sibling IS guarded
    # (the DECIDED_VERSION_STATUSES canary above), so the project already knows
    # how to protect this and simply did not, for this family.
    #
    # This reads the SOURCE rather than importing, deliberately: importing
    # seven production modules inside a self-test drags their import-time side
    # effects into the gate, and the thing being protected is a literal in a
    # file. Every approved-family literal in tools/ must equal one of the two
    # legitimate shapes. A tenth copy that drifts turns this red.
    import glob as _glob
    import re as _re463
    _CANON5 = frozenset(("apr", "ad", "fin", "paf", "dlvr"))
    _CANON7 = _CANON5 | frozenset(("appcbb", "appgra"))
    _tuple_re = _re463.compile(r"[\(\{]((?:\s*\"[a-z]+\"\s*,?){2,10})[\)\}]")
    _odd = []
    for _f in _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "*.py")):
        try:
            _src463 = open(_f, encoding="utf-8").read()
        except OSError:
            continue
        # strip comment lines so a comment quoting the tuple cannot match, the
        # mistake this project has now made twice
        _src463 = "".join(l for l in _src463.splitlines(True)
                          if not l.lstrip().startswith("#"))
        for _m in _tuple_re.finditer(_src463):
            _vals = frozenset(v.strip().strip('"') for v in _m.group(1).split(",")
                              if v.strip())
            if "apr" in _vals and "dlvr" in _vals and _vals not in (_CANON5, _CANON7):
                _odd.append((os.path.basename(_f), sorted(_vals)))
    ck("F463 CANARY: every approved-status tuple in tools/ is one of the two "
       "canonical shapes, none has drifted",
       not _odd)
    if _odd:
        print("      drifted approved-status tuples: %r" % (_odd[:5],))

    # --- F461: PROVENANCE VS SENT IS WIRED, AND STAYS WIRED. Same discipline
    # as invalidate_stale_panels and watch_hand_edits above: a self-test that
    # only proves the FUNCTION works is not evidence anything calls it, and
    # this exact tool passed its own self-test on every deploy while having
    # ZERO callers. Comment lines are stripped before matching (the F463
    # block above established the idiom) so this cannot pass by matching its
    # own explanation, and a docstring is checked separately below for the
    # same reason -- it is not a comment and would not be stripped.
    import inspect as _i461

    def _code_only(src):
        return "".join(l for l in src.splitlines(True) if not l.lstrip().startswith("#"))

    _cyc_code461 = _code_only(_i461.getsource(cycle))
    ck("F461 CANARY: cycle() actually CALLS watch_provenance(sg)",
       "watch_provenance(sg)" in _cyc_code461)

    _wp_code = _code_only(_i461.getsource(watch_provenance))
    ck("F461 CANARY: watch_provenance() calls the ONE definition, "
       "provenance_vs_sent.find_disagreements(), not a local reimplementation",
       "PVS.find_disagreements(" in _wp_code)
    ck("F461 CANARY: it passes `since` -- proof this is the SCOPED check "
       "(new Versions only), not a full-episode sweep every cycle",
       "since=since" in _wp_code)

    _rpd_code = _code_only(_i461.getsource(_report_provenance_drift))
    ck("F461 CANARY: the reporting path never writes a Version's OWN "
       "sg_status_list -- constraint 1 of the brief, it reports and never "
       "refuses or blocks a publish",
       'sg.update("Version"' not in _rpd_code)
    ck("...and it never touches a Task either",
       '"Task"' not in _rpd_code)

    class _NoteStubSG(object):
        """Just enough ShotGrid for _report_provenance_drift(): a Note table
        keyed by an incrementing id, find_one() matching on subject (the same
        find-or-update idiom record_panel_refusal() above uses), and an
        update() log so the CANARY above (no Version write) can be checked
        against REAL BEHAVIOUR too, not only against the source text."""

        def __init__(self):
            self.notes = {}
            self.next_id = 1
            self.version_updates = []

        def find_one(self, entity_type, filters, fields=None):
            if entity_type != "Note":
                return None
            want = next((f[2] for f in filters if f[0] == "subject"), None)
            for n in self.notes.values():
                if n.get("subject") == want:
                    return {"id": n["id"]}
            return None

        def create(self, entity_type, data):
            nid = self.next_id
            self.next_id += 1
            row = dict(data)
            row["id"] = nid
            self.notes[nid] = row
            return row

        def update(self, entity_type, entity_id, data):
            if entity_type == "Version":
                self.version_updates.append((entity_id, dict(data)))
            elif entity_type == "Note":
                self.notes[entity_id].update(data)
            return dict(data)

    _nstub = _NoteStubSG()
    _v461 = {"id": 42, "code": "SHOW01_A_0010_PNL_v001", "sg_stage": "panel"}
    _bad461 = [("sg_component__camera", {"close", "768x432"})]
    ck("_report_provenance_drift() writes ONE Note and reports True",
       _report_provenance_drift(_nstub, _v461, _bad461) is True and len(_nstub.notes) == 1)
    _n461 = list(_nstub.notes.values())[0]
    ck("...the Note is a pipeline RECORD (AUTO_NOTE_PREFIX), so note_triage "
       "and the proposer both ignore it (invariant 11, same as record_panel_refusal)",
       _n461["subject"].startswith(AUTO_NOTE_PREFIX))
    ck("...linked to the VERSION itself, not a Shot or Episode -- the entity "
       "is the thing the provenance claim is ABOUT",
       _n461["note_links"] == [{"type": "Version", "id": 42}])
    ck("CANARY (behavioural, not just source text): a provenance finding "
       "never actually calls sg.update('Version', ...)",
       _nstub.version_updates == [])
    _report_provenance_drift(_nstub, _v461, _bad461)
    ck("a second finding on the SAME Version updates the existing Note "
       "in place rather than accumulating a duplicate",
       len(_nstub.notes) == 1)

    import tempfile as _tf461
    _curpath461 = os.path.join(_tf461.mkdtemp(prefix="prov_cursor_selftest_"), "cursor.json")
    ck("a missing provenance cursor starts cold: {} means a full sweep per episode",
       _load_provenance_cursor(_curpath461) == {})
    _save_provenance_cursor({"SHOW01": "2026-09-08T00:00:00+00:00"}, _curpath461)
    ck("the provenance cursor round-trips PER EPISODE",
       _load_provenance_cursor(_curpath461) == {"SHOW01": "2026-09-08T00:00:00+00:00"})

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


# --- single instance, enforced by the OS -------------------------------------

LOCK_BYTE = 4096          # reserved; well past the pid line, never written to
# A FIXED ABSOLUTE PATH, NOT ONE DERIVED FROM __file__.
#
# The first version of this used os.path.dirname(__file__)/../ops, which is
# inside the VERSIONED RELEASE DIRECTORY. Deploys activate a new release dir
# each time, so an old instance and a new one hold two DIFFERENT lock files
# and never contend -- which is precisely the case the lock exists to catch,
# and it was measured doing exactly that on the very next deploy (pids 22376
# and 8060, one per release). A mutual-exclusion primitive whose identity
# moves with the code it guards excludes nothing.
#
# It lives on C: (scratch) rather than E: because it is ephemeral runtime
# state for THIS machine, and E: is a Dropbox share -- a lock file replicating
# to every synced box would be meaningless at best.
SINGLETON_LOCK_PATH = r"C:\genvideo\ops\service.lock"
_SINGLETON_HANDLE = None


def acquire_singleton(path=None, log=log):
    """Take an EXCLUSIVE OS lock, or return False. -> True if this process owns it.

    MEASURED 2026-09-04: two services were running, pids 25212 and 26316, both
    created at 19:32:59 -- the SAME SECOND. ops/run_service.ps1 does have a
    single-instance guard, and it is a check-then-act:

        $others = Get-CimInstance Win32_Process | ...     # look
        if ($others) { REFUSE }                           # then decide
        # ... start python                                # then act

    Two launches inside that window both look, both see nothing, and both
    start. That is a textbook TOCTOU race, and no amount of tightening the
    check closes it -- only an ATOMIC operation does. So the guard moves into
    the process it is guarding, where the OS can arbitrate: an exclusive lock
    on a file, held open for the process's whole life and released by the
    kernel when it dies (so a hard kill or a BSOD cannot leave a stale lock
    that blocks every future start -- the failure mode a hand-rolled
    pid-file guard has).

    WHY THIS MATTERS AND IS NOT COSMETIC: every watcher here reads ShotGrid
    state and writes back. Two of them double-process every trigger -- two
    renders for one queued shot, two Versions racing for the same version
    number, and a note-triage that can never converge because each instance
    overwrites the other's verdict. The duplicate is invisible from ShotGrid;
    it looks like the pipeline being erratic.

    The lock is advisory between cooperating processes, which is exactly the
    case here: the only thing that takes it is another copy of this service.
    """
    global _SINGLETON_HANDLE
    path = os.path.abspath(path or SINGLETON_LOCK_PATH)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass
    try:
        fh = open(path, "a+")
    except Exception as exc:
        # A lock we cannot even open must not silently disable the guard.
        log("SINGLETON: cannot open %s (%s: %s) -- refusing to start rather than "
            "risk a second instance" % (path, type(exc).__name__, str(exc)[:120]))
        return False
    try:
        # SEEK TO A FIXED BYTE FIRST, AND THIS LINE IS THE WHOLE GUARD.
        #
        # msvcrt.locking locks a byte range starting at the CURRENT FILE
        # POSITION. The file is opened "a+", which leaves the position at
        # EOF -- so the first claimant (empty file, position 0) locked byte 0,
        # then wrote its pid, and the SECOND claimant opened the now-6-byte
        # file at position 6 and locked byte 6. Two different bytes, no
        # conflict, and the guard let a second service straight through while
        # looking like it worked. Caught by this module's own self-test; the
        # in-process version of that check would have passed.
        #
        # The byte is LOCK_BYTE, not 0, because a Windows lock blocks reads of
        # the locked range from every handle including this process's own. With
        # the lock on byte 0 the pid written there became unreadable, so the
        # refusal below could never name who was holding it -- it said
        # "(unknown)" for the one fact that makes the message useful. Locking a
        # reserved byte past any pid text keeps the human-readable part
        # readable and the guard just as atomic.
        fh.seek(LOCK_BYTE)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        try:
            fh.seek(0)
            holder = (fh.read() or "").strip()
        except Exception:
            holder = "unknown"
        fh.close()
        log("SINGLETON: another genvideo_service is already running (%s). "
            "REFUSING to start a second one -- two services double-process every "
            "ShotGrid trigger." % (holder or "pid unknown"))
        return False
    try:
        fh.seek(0)
        fh.truncate()
        fh.write("pid %d" % os.getpid() + chr(10))
        fh.flush()
    except Exception:
        pass
    _SINGLETON_HANDLE = fh       # held open for the life of the process
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="offline canaries for the PHASE 10 design-approval watcher; "
                         "no ShotGrid, no GPU")
    ap.add_argument("--version", action="store_true",
                    help="print the deploy stamp for this tree and exit -- no ShotGrid, no loop")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if ns.version:
        print(deploy_stamp_line())
        print("running from: %s" % os.path.abspath(__file__))
        return 0

    # E0 acceptance test (MASTER-PLAN-V3.md): "the service log's first line
    # names the deployed SHA". Printed before sg_connect() so it appears even
    # if the ShotGrid credential is missing -- "what deployed this" and
    # "can it reach ShotGrid" are two different questions with two different
    # answers, and conflating them was how a stale tree stayed invisible.
    log(deploy_stamp_line())
    # BEFORE ShotGrid, before anything writes: a second instance must die here,
    # not after it has already double-processed a cycle.
    if not acquire_singleton():
        return 3
    sg = sg_connect()
    # Before the first cycle: hand back anything a stopped service was holding.
    # Safe only because acquire_singleton() above proved no other service is
    # running -- see reclaim_orphaned_claims() for why that is the whole
    # argument.
    reclaim_orphaned_claims(sg)
    log("standing service up. ShotGrid state is the only trigger.")
    log("  queue a shot:      Shot.sg_gen_status = queued")
    # THE BANNER IS OPERATOR-FACING TEXT AND IT WAS STALE. Geoff, 2026-09-07:
    # *"If you gave a note you need to change the status to rrq, thats what
    # should trigger the regeneration."* The two are one gesture, not two
    # alternatives, and saying "or" invited the sloppier half.
    log("  request revision:  write a Note on the Version AND set that Version "
        "to rrq (the rrq IS the un-approval, so the replacement is not blocked)")
    log("  close a shot:      Version status = apr / ad / fin / paf / dlvr")
    log("  assemble:          Sequence.sg_gen_status = queued")
    log("  cut an animatic:   Sequence.sg_gen_status = animatic_requested (on request only, D8)")
    log("  stale a panel:     edit Shot.sg_script_beat (story beat), or swap an Asset's "
        "sg_approved_design under an already-composed panel")
    # NO ACCEPT STEP ANY MORE. Geoff, 2026-09-07: *"the operator should not have
    # to accept the proposed prompt at this stage, it should just be
    # rerendered."* Every SHOW01 shot carries sg_auto_apply_proposals and
    # episode_ingest creates new ones that way.
    log("  revise a prompt:   a prompt-addressable note proposes AND applies "
        "itself (Shot.sg_auto_apply_proposals); set sg_proposal_status=rejected "
        "to stop one")
    log("  anchor a shot:     approve a panel Version (sg_stage=panel) -> service sets "
        "Shot.sg_approved_panel")
    log("  generate video:    Shot.sg_gen_status = queued AND Shot.sg_approved_panel set -> "
        "A14B i2v from the panel (Phase 6); queued with NO approved panel is REFUSED")
    log("  approve a design:  approve a Version whose entity is an Asset -> service sets "
        "Asset.sg_approved_design + sg_stage=approved (Phase 10)")
    log("  revise a design:   Asset design Version = rrq + a Note -> proposal applied "
        "automatically for SHOW01 (sg_auto_apply_proposals default on) -> "
        "Asset.sg_gen_status=queued -> new candidate(s) at 'rev' (Phase 10 + 11). "
        "PILOT01 Assets keep the manual accept -- their auto-apply flag is unchanged.")
    log("  compose a panel:   flip a Shot's Panel Task to 'rdy' -> panel_compose.py runs, "
        "publishes at 'rev', Task -> 'rev' (or 'hld' if refused) (Phase 12)")
    idle = 0
    while True:
        try:
            busy = cycle(sg)
            idle = 0 if busy else idle + 1
            # A HANG AND A QUIET QUEUE MUST NOT LOOK THE SAME.
            #
            # This used to log at idle 1, 10 and 60 only, so after 60 idle
            # cycles the service went permanently silent BY DESIGN. Measured
            # 2026-09-06: the service completed one cycle and then stopped
            # cycling entirely, process alive, no traceback, CPU 0.078s over
            # 65s. Nothing distinguished that from a healthy idle service,
            # and on a demo day it looks like ShotGrid simply doing nothing.
            #
            # Logging every 20th idle cycle bounds the silence: with
            # --interval 45 no healthy service is ever quiet for more than
            # ~15 minutes, so a longer gap in ops/last_launch.log MEANS a
            # hang. The line carries only the cycle count, which changes
            # because time passed, and it goes to a log file rather than to
            # the comms channel, so it is not a broadcast.
            if idle in (1, 10) or (idle and idle % 20 == 0):
                log("idle (%d cycles). Nothing queued in ShotGrid." % idle)
        except KeyboardInterrupt:
            log("stopped by operator")
            return 0
        except Exception as exc:
            # A transient ShotGrid or network fault must not kill the service:
            # a dead service looks exactly like an empty queue.
            log("cycle error (continuing): %s: %s" % (type(exc).__name__, str(exc)[:160]))
        if ns.once:
            return 0
        time.sleep(ns.interval)


if __name__ == "__main__":
    sys.exit(main())
