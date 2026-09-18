#!/usr/bin/env python3
"""Phase 6 -- video from the approved panel. MASTER-PLAN-V2.md section 5, Phase 6.

    SCRIPT -> BEATS -> BOARDS -> PANEL (anchor, Phase 5) -> VIDEO (here)

V1's failure, verbatim from a peer engineer records: 6 of 7 shots ended up anchored on
stock reference photos, two shared between different characters. "The drift is
entering at the anchor generation step, not at the i2v step." Phase 5 fixed the
anchor. This module is the enforcement point that makes the fix actually bind:
video may be generated ONLY from a shot's Shot.sg_approved_panel, a real,
still-existing, still-a-panel Version with a file on disk. Anything else is
REFUSED, loudly, before any GPU time is spent -- that refusal
(validate_panel_anchor / PanelAnchorRefusal below) is the one line of code this
whole phase exists to prove, per the phase brief.

WHO SETS sg_approved_panel: only genvideo_service.py's watch_panel_approvals()
(Phase 5), on a reviewer's status flip. This module never writes it -- same
"no self-approval" discipline panel_compose.py documents for itself.

TRIGGER (invariant 1, ShotGrid state only): Shot.sg_gen_status == 'queued'.
For each such shot:
  - a valid approved panel -> i2v via A14B (workflows/wan22_a14b_i2v.api.json,
    the graph Phase 1 wedged and proved), 81 frames @ 16fps native, then
    fps_bridge.py to the shot's own Shot.sg_gen_frames @ 24fps, published with
    full D6 provenance INCLUDING sg_anchor_version -> the panel Version. That
    link is the audit trail: every published video Version can be traced back
    to the panel that authorized it, which is the exact thing V1 could not do.
  - anything else (no panel linked, linked Version deleted, linked Version is
    not sg_stage=='panel', or its file is missing on disk) -> REFUSED. Shot
    flips to sg_gen_status='refused' (schema-extended here, see setup_schema())
    with the exact reason in sg_gen_log. Nothing is submitted to ComfyUI.

CELL SETTINGS (Phase 1's wedge; D14 decision). NOTE on provenance: the brief
for this phase cites "plan\\reports\\D14-PROVISIONAL-APPROVALS.md" as the
source of the cell-A/cell-C choice -- that file does NOT exist anywhere in
this tree (grepped clean). The REAL decision record was found live in
ShotGrid instead: Note id 51122, "D14 provisional decision: PILOT01
production wedge cell", created 2026-08-29 by a designated-reviewer agent
under D14's "simulate Geoff's approvals, use the ShotGrid methods" rule
(provisional, can be overturned). It sets Version 67106
(PILOT01_WEDGE_cellA_832x480_light4_v001) and 67108
(PILOT01_WEDGE_cellC_1280x704_light4_v001) to sg_status_list='apr' and states
the reasoning: cell C was visually the CLEANEST of all four Phase 1 cells
(clearest face, visible strings, least warping) despite matching cell A's
step count -- the 20-step/no-LoRA cells (B, D) showed WORSE identity
breakdown at both resolutions, so more steps without Lightning is not buying
quality on this graph. Cell D was also rejected outright on cost (CPU-offload
VRAM cliff, ~9x cell C's GPU-time for worse output). This matches, and was
not overruled from, this phase's own brief:
  cell A (832x480, 4-step Lightning, cfg 1.0)  -- DEV_CELL, default here
  cell C (1280x704, 4-step Lightning, cfg 1.0) -- PROD_CELL, --production
Both use the exact wedge constants proved in build/tests/a14b_wedge.py:
lora_strength=1.0, shift=8.0, switch_step=2 (steps 0-2 high, 2-4 low),
81 frames, the Wan 2.1 VAE (never the 2.2 one -- see Phase 1's VAE-trap
canary; VAE_NAME below is the ONLY vae this module will ever substitute).

LIGHTNING / CFG 1.0: the negative prompt is INERT at cfg 1.0 (MASTER-PLAN-V2
section 3, "Lightning consequence"). It is still composed and still recorded
in provenance for the record, but steering lives in the POSITIVE prompt only
-- see build_prompt() below.

NOT TOUCHED: genvideo_worker.py (the pre-panel, 5B TI2V worker) is untouched.
It is superseded for VIDEO GENERATION specifically by this module's stricter
gate (see genvideo_service.py's PHASE 6 block for how the two are kept from
double-firing on the same queued shot -- ordering, not a query change to the
older code). Its review/Note-ingestion half (poll_feedback) is unrelated to
panel anchoring and is left running as-is; that is a deliberate, named scope
boundary, not an oversight.

    python video_from_panel.py --setup-schema     idempotent, extends
                                                    Shot.sg_gen_status with
                                                    'refused' (never removes
                                                    existing values)
    python video_from_panel.py --once             one pass over every queued
                                                    Shot in the project
    python video_from_panel.py --shot CODE         one shot
    python video_from_panel.py --dry --shot CODE   validate the anchor only;
                                                    never starts ComfyUI, never
                                                    writes ShotGrid
    python video_from_panel.py --production ...    cell C instead of cell A
"""
import argparse
import os
import re
import subprocess
import sys
import time
import urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
PY = sys.executable
ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools",
# "comfyui", "comfyui_execute.py"); TOOLS above is already self-relative, so
# this now resolves inside whichever release deployed this file too.
WRAPPER = os.path.join(TOOLS, "comfyui", "comfyui_execute.py")
TESTS = os.path.join(ROOT, "build", "tests")
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)
import sg_provenance as PROV                                   # noqa: E402
import dialogue_guard as DG                                     # noqa: E402  D15, invariant 11
import fps_bridge as FB                                         # noqa: E402
import timeline as TL                                           # noqa: E402
import review_ledger as RL                                     # noqa: E402  (build/tests, same
                                                                # location genvideo_worker.py
                                                                # imports it from)
TPL_I2V = os.path.join(ROOT, "workflows", "wan22_a14b_i2v.api.json")
OUT_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\output"
IN_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\input"
OUT_PUB = os.path.join(ROOT, "output", "published")
LEDGER_DIR = os.path.join(ROOT, "output", "ledger")
COMFY_EXE = r"C:\ComfyUI_windows_portable\python_embeded\python.exe"
COMFY_MAIN = r"C:\ComfyUI_windows_portable\ComfyUI\main.py"
LEDGER_DIR = os.path.join(ROOT, "output", "ledger")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

PROJECT_ID = 9999
PROJ = {"type": "Project", "id": PROJECT_ID}
import sg_publish as PUB                                    # noqa: E402
SUPERSEDABLE = PUB.SUPERSEDABLE   # invariant 11: one definition, see sg_publish
STEP = "CMP"

# ---------------------------------------------------------- Phase 1's proven A14B recipe
UNET_HIGH = "Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf"
UNET_LOW = "Wan2.2-I2V-A14B-LowNoise-Q4_K_S.gguf"
LORA_HIGH = "wan22_lightning_i2v_a14b_high.safetensors"
LORA_LOW = "wan22_lightning_i2v_a14b_low.safetensors"
VAE_NAME = "wan_2.1_vae.safetensors"          # NEVER wan2.2_vae.safetensors -- Phase 1 VAE trap
LORA_STRENGTH = "1.0"
SHIFT = "8.0"
STEPS = 4
SWITCH_STEP = 2
CFG = 1.0
# HOW MANY FRAMES TO ASK THE MODEL FOR. Not an fps: there is no fps input
# anywhere in wan22_a14b_i2v.api.json (the graph is WanImageToVideo ->
# KSamplerAdvanced -> VAEDecode -> SaveImage, and SaveImage writes PNG frames).
# `length` is the ONLY temporal control the model exposes.
#
# NATIVE_FPS is therefore not a setting either. It is the rate A14B was TRAINED
# at, meaning how much motion it places between adjacent frames, and it is a
# property of the weights.
#
# So "generate at 24fps instead of 16" is not a thing that can be asked for.
# The equivalent question is "ask for 121 frames instead of 81", since
# 121/24 = 5.04s and 81/16 = 5.06s, and that removes the fps bridge entirely
# rather than interpolating 40 frames into existence.
#
# GENVIDEO_NATIVE_FRAMES exists so that experiment can be run without editing
# code. It is UNTESTED above 81: the Phase 1 wedge proved this recipe at 81,
# and longer sequences on this model family are known to risk drift and
# identity wander. Treat any value other than 81 as an experiment, not a
# setting, until RND_FPS_0020 says otherwise.
FRAMES = int(os.environ.get("GENVIDEO_NATIVE_FRAMES", "81"))
NATIVE_FPS = 16.0
# DST_FPS lived here as a hardcoded 24.0 with ZERO uses: the definition was
# its only occurrence. Removed 2026-09-06 because a dead constant that
# CONTRADICTS the live one is worse than no constant. The delivery rate has
# one home, tools/timeline.py, and fps_bridge reads it; anyone grepping
# DST_FPS here would have concluded this pipeline ships 24fps.

DEV_CELL = (832, 480)     # cell A -- 140.8s wall-clock, PASS, Phase 1 wedge
PROD_CELL = (1280, 704)   # cell C -- 411.6s wall-clock, PASS (partial VRAM offload), Phase 1 wedge

MODEL_NAME = "wan2.2_i2v_A14B_Q4_K_S+lightning4"
APPROVED_PANEL_STATUSES = ("apr", "ad", "fin", "paf", "dlvr")  # mirrors genvideo_service.py


def log(msg):
    print("[video_from_panel] %s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------- ComfyUI lifecycle
# Mirrors genvideo_worker.py's comfy_up/comfy_start/comfy_stop exactly (same
# constants, same teardown_verify.py discipline, invariant 4 -- "ComfyUI is
# closed and teardown_verify.py run after every GPU batch... finally: blocks,
# not good intentions"). Duplicated rather than imported: genvideo_worker.py
# parses sys.argv for --worker-id at import time and carries its own WORKER_ID
# global, so importing it here would couple this module to that side effect
# for ~25 lines of savings. NEVER exercised in this build -- no GPU seat; see
# the phase report.
def comfy_up():
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3)
        return True
    except Exception:
        return False


def comfy_start():
    if comfy_up():
        return True
    guard = subprocess.run([PY, os.path.join(TESTS, "gpu_guard.py")],
                           capture_output=True, text=True)
    if guard.returncode != 0:
        log("GPU guard says %s - not starting ComfyUI" % guard.stdout.strip())
        return False
    os.makedirs(LEDGER_DIR, exist_ok=True)
    subprocess.Popen([COMFY_EXE, "-s", COMFY_MAIN, "--windows-standalone-build",
                      "--listen", "127.0.0.1", "--port", "8188"],
                     stdout=open(os.path.join(LEDGER_DIR, "comfy_video_from_panel.log"), "a"),
                     stderr=subprocess.STDOUT,
                     cwd=r"C:\ComfyUI_windows_portable")
    for _ in range(120):
        time.sleep(0.5)
        if comfy_up():
            log("ComfyUI started")
            return True
    log("ComfyUI failed to come up")
    return False


def comfy_stop():
    # TEARDOWN IS OFF BY DEFAULT (Geoff, 2026-09-05). The rule existed because this
    # project began as "how to get ComfyUI renders onto the Royal Render farm", where
    # the box was shared with other jobs and an idle ComfyUI held VRAM somebody else
    # needed. That is not what we do now and nothing else uses this machine, so paying
    # a full model reload per render buys nothing.
    #
    # Set GENVIDEO_TEARDOWN_COMFY=1 to restore the old behaviour the moment this box is
    # shared again. It logs on every skip: a behaviour change that goes quiet is how a
    # shared GPU gets held hostage without anyone noticing.
    if os.environ.get("GENVIDEO_TEARDOWN_COMFY", "") != "1":
        log("teardown: SKIPPED, ComfyUI left running "
            "(set GENVIDEO_TEARDOWN_COMFY=1 to restore teardown)")
        return
    if not comfy_up():
        return
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "(Get-NetTCPConnection -State Listen -LocalPort 8188 "
                         "-EA SilentlyContinue | Select-Object -First 1).OwningProcess"],
                        capture_output=True, text=True)
    pid = ps.stdout.strip()
    if pid:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Stop-Process -Id %s -Force -EA SilentlyContinue" % pid])
        time.sleep(4)
    v = subprocess.run([PY, os.path.join(TESTS, "teardown_verify.py")],
                       capture_output=True, text=True)
    log("teardown: %s" % ("VERIFIED" if v.returncode == 0 else "NOT CLEAN - investigate"))


def sg_connect():
    import shotgun_api3
    for name in ("SHOTGRID_SITE_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY"):
        if not os.environ.get(name):
            sys.exit("FATAL: %s is not set." % name)
    return shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                                script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                                api_key=os.environ["SHOTGRID_SCRIPT_KEY"])


# ============================================================================
# THE REFUSAL. This is the headline of the whole phase (see module docstring).
# validate_panel_anchor() is a pure function of (sg, shot_row) -> either a
# dict describing a usable anchor, or a raised PanelAnchorRefusal naming
# exactly why. No side effects, no ShotGrid writes -- callers decide what to
# do with the answer, which is what makes this trivially unit-testable with a
# stub `sg` (see build/tests/panel_video_refusal.py).
# ============================================================================
class PanelAnchorRefusal(Exception):
    pass


def validate_panel_anchor(sg, shot):
    """shot must already carry ['id','code','sg_approved_panel']. Every
    refusal path below was hit live against real ShotGrid data (see the phase
    report) except where noted.

    -> {"version_id", "version_code", "image_path"} on success.
    """
    link = shot.get("sg_approved_panel")
    if not link or not link.get("id"):
        raise PanelAnchorRefusal(
            "Shot %s has no approved panel (Shot.sg_approved_panel is not set) -- "
            "refusing to generate video without an anchor. This is the exact V1 "
            "failure (video anchored on nothing / a stray reference) made "
            "structurally impossible." % shot.get("code"))
    vid = link["id"]
    v = sg.find_one("Version", [["id", "is", vid]],
                    ["id", "code", "sg_stage", "sg_path_to_movie", "sg_status_list"])
    if not v:
        raise PanelAnchorRefusal(
            "Shot %s's sg_approved_panel points at Version id=%s, which no longer "
            "exists in ShotGrid (deleted/retired) -- refusing. A dangling anchor "
            "link is exactly as unsafe as no link at all." % (shot.get("code"), vid))
    if v.get("sg_stage") != "panel":
        raise PanelAnchorRefusal(
            "Shot %s's sg_approved_panel points at Version %s (id=%s), but its "
            "sg_stage is %r, not 'panel' -- refusing. Only a real panel Version "
            "may anchor video." % (shot.get("code"), v.get("code"), vid, v.get("sg_stage")))
    path = v.get("sg_path_to_movie") or ""
    if not path or not os.path.exists(path):
        raise PanelAnchorRefusal(
            "Shot %s's approved panel Version %s (id=%s) has no file on disk "
            "(sg_path_to_movie=%r) -- refusing." % (shot.get("code"), v.get("code"), vid, path))
    if v.get("sg_status_list") not in APPROVED_PANEL_STATUSES:
        # Defense in depth: watch_panel_approvals() only ever links an
        # approved-status Version, so this should be unreachable in practice,
        # but sg_approved_panel is a plain entity-link field a direct SG edit
        # could repoint -- this is what stops that shortcut from working.
        raise PanelAnchorRefusal(
            "Shot %s's approved panel Version %s (id=%s) has status %r, not an "
            "approved status -- refusing. sg_approved_panel must point at a "
            "Version a reviewer actually approved." % (shot.get("code"), v.get("code"), vid,
                                                        v.get("sg_status_list")))
    return {"version_id": vid, "version_code": v["code"], "image_path": path}


# ============================================================================
# END REFUSAL
# ============================================================================


def setup_schema(sg):
    """Idempotent: extend Shot.sg_gen_status with 'refused' (invariant 8 --
    extend, never replace; existing valid_values untouched). A distinct
    status (rather than overloading 'error') is what makes 'we chose not to
    render' legible in ShotGrid as different from 'we tried and it broke'."""
    info = sg.schema_field_read("Shot", "sg_gen_status")
    cur = info["sg_gen_status"]["properties"]["valid_values"]["value"]
    if "refused" not in cur:
        sg.schema_field_update("Shot", "sg_gen_status", {"valid_values": cur + ["refused"]})
        after = sg.schema_field_read("Shot", "sg_gen_status")["sg_gen_status"]["properties"] \
                    ["valid_values"]["value"]
        log("Shot.sg_gen_status extended with 'refused' -- now: %s" % after)
    else:
        log("Shot.sg_gen_status already has 'refused' -- no-op")


def stage_image_for_comfy(path, shot_code):
    import shutil
    local = "panel_%s.png" % re.sub(r"[^A-Za-z0-9._-]", "_", shot_code)
    dest = os.path.join(IN_COMFY, local)
    shutil.copyfile(path, dest)
    return local


def build_prompt(shot):
    """Motion text for i2v. Shot.sg_gen_prompt (the field genvideo_worker.py
    already uses for the same purpose) wins; Shot.sg_script_beat (Phase 4) is
    the fallback so a shot that only has a beat still generates something.
    Per the Lightning/cfg-1.0 note above, ALL steering must live here -- the
    negative prompt composed below is recorded but inert.

    D15: this WAS the live gap the partial panel_compose.py fix missed --
    the sg_script_beat fallback was handed to the GPU raw, dialogue and all,
    with no sanitization at all (unlike panel_compose.py's compositor path).
    dialogue_guard.strip_dialogue_for_prompt() is applied to the chosen text
    before anything else touches it, regardless of which field it came from
    (invariant 11 -- one implementation, at the point of consumption)."""
    prompt = DG.strip_dialogue_for_prompt(
        shot.get("sg_gen_prompt") or shot.get("sg_script_beat") or "").strip()
    if not prompt:
        raise PanelAnchorRefusal(
            "Shot %s has neither sg_gen_prompt nor sg_script_beat -- refusing; "
            "i2v needs motion text even with a valid anchor image." % shot.get("code"))
    if "preserve all appearance" not in prompt.lower():
        prompt = prompt.rstrip(". ") + ". Preserve all appearance and environment from source image."
    negative = (shot.get("sg_gen_negative_prompt") or
               "blurry, distorted, watermark, text, extra limbs, static").strip()
    return prompt, negative


def cell_for(production):
    return PROD_CELL if production else DEV_CELL


# ============================================================================
# REGEN-FIX (2026-08-29): the published-filename collision that made a shot
# regenerable exactly once, ever. See docs/METHOD.md
#
# WHAT HAPPENED LIVE: PILOT01_A_0090 and PILOT01_A_0100 both had a first
# generation publish clean (bridged file "..._a14b_v000.mp4", seed defaulted
# to 1000 since Shot.sg_gen_seed was null -> seed % 1000 == 0). A second
# generation for the SAME shot -- exactly what the revision loop exists to
# do -- computed the IDENTICAL bridged filename, because the old code below
# used `seed % 1000` as the "version". Seed is supposed to stay LOCKED across
# iteration (MASTER-PLAN-V2 section 4: "locked seed + prompt change for
# directed fixes"), so every subsequent regeneration of the same shot reused
# the same filename and fps_bridge.py's own overwrite refusal (correct, and
# unchanged here) stopped it cold, flipping the shot to sg_gen_status='error'.
# Iteration -- accept a revision, regenerate, compare -- is the entire point
# of this system; this bug made it possible exactly once per shot.
#
# THE FIX: next_publish_version() below derives the next version from what is
# ACTUALLY on disk (OUT_PUB) and ACTUALLY in ShotGrid (this shot's own
# "<code>_<STEP>_a14b_v###" Versions), taking the max seen across BOTH and
# adding one -- never from seed, never from an in-memory or ledger-file
# counter kept anywhere else, because a counter that merely hopes to stay in
# sync is exactly how this defect happened: the ledger's own "next" count
# would have been wrong too, the first time it disagreed with disk (a reset
# ledger, a file copied in from another box, a retried run). Taking the
# ever-seen max (never the max "still alive") matches review_ledger.py's own
# next_version_number() rule for the identical reason: a deleted vNNN must
# not free vNNN for reuse, because a note about the old vNNN may still exist.
# ============================================================================
def next_publish_version(sg, shot_id, shot_code):
    """-> int, the next a14b publish version for this shot (0-based, matching
    the v000 this pipeline already produced on a shot's first generation)."""
    highest = -1
    file_re = re.compile(r"^%s_a14b_v(\d+)\.mp4$" % re.escape(shot_code))
    if os.path.isdir(OUT_PUB):
        for fn in os.listdir(OUT_PUB):
            m = file_re.match(fn)
            if m:
                highest = max(highest, int(m.group(1)))
    code_prefix = "%s_%s_a14b_v" % (shot_code, STEP)
    code_re = re.compile(r"^%s(\d+)$" % re.escape(code_prefix))
    for v in sg.find("Version",
                     [["project", "is", PROJ],
                      ["entity", "is", {"type": "Shot", "id": shot_id}],
                      ["code", "starts_with", code_prefix]],
                     ["code"]):
        m = code_re.match(v.get("code") or "")
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1
# ============================================================================
# END REGEN-FIX
# ============================================================================


def generate_i2v(shot_code, image_filename, prompt, negative, seed, version, production=False):
    """Submit the proven A14B i2v graph via comfyui_execute.py. NEVER exercised
    in this build (no GPU seat) -- see the phase report. Kept structurally
    identical to genvideo_worker.py's generate()/WRAPPER invocation so the
    proven Phase 1 command shape (comfyui_execute.py, --verify-mode disk,
    --expect-outputs) is not reinvented here.

    `version` (REGEN-FIX) names the ComfyUI-side output prefix too, not just
    the published file -- so a second generation for the same locked seed
    does not reuse the same intermediate PNG-sequence prefix in OUT_COMFY
    either."""
    width, height = cell_for(production)
    prefix = "%s_a14b_v%03d" % (shot_code.lower(), int(version))
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", TPL_I2V,
          "--comfy-output-dir", OUT_COMFY, "--verify-mode", "disk",
          "--output-prefix", prefix, "--seed-base", str(seed),
          "--expect-outputs", str(FRAMES), "--timeout", "900",
          "--set", "unet_high=%s" % UNET_HIGH, "--set", "unet_low=%s" % UNET_LOW,
          "--set", "lora_high=%s" % LORA_HIGH, "--set", "lora_low=%s" % LORA_LOW,
          "--set", "lora_strength=%s" % LORA_STRENGTH, "--set", "shift=%s" % SHIFT,
          "--set", "vae_name=%s" % VAE_NAME, "--set", "start_image=%s" % image_filename,
          "--set", "prompt=%s" % prompt, "--set", "negative_prompt=%s" % negative,
          "--set", "width=%d" % width, "--set", "height=%d" % height,
          "--set", "length=%d" % FRAMES, "--set", "steps=%d" % STEPS,
          "--set", "cfg=%s" % CFG, "--set", "switch_step=%d" % SWITCH_STEP]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    took = time.time() - t0
    if "PASS:" not in r.stdout:
        raise RuntimeError("A14B i2v generation FAILED after %.1fs:\n%s" % (took, r.stdout[-800:]))
    # WHAT WAS SENT, CARRIED OUT OF THE FUNCTION THAT SENT IT. Measured
    # 2026-09-08: 7 of 191 generated video Versions recorded the prompt,
    # against 998 of 1011 panels. So the motion half of this show had no
    # record of what it was asked to do, and "read the composed prompt
    # before blaming the model", the rule that has explained five of five
    # supposed model limitations here, could not be applied to video at
    # all. These are the SAME strings handed to the graph above, never a
    # second derivation, which is the F405 lesson: a re-derived record is
    # not a record.
    return prefix, {"width": width, "height": height, "took": took,
                    "prompt_sent": prompt, "negative_sent": negative}


def encode_native_16fps(prefix, dst_path):
    """ComfyUI's raw PNG sequence -> a 16fps mp4 (the clip's TRUE native rate;
    see fps_bridge.py's docstring for why this must not be mislabelled 24fps
    at this stage)."""
    r = subprocess.run([FFMPEG, "-y", "-framerate", str(int(NATIVE_FPS)),
                        "-i", os.path.join(OUT_COMFY, prefix + "_w000_%05d_.png"),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", dst_path],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst_path):
        raise RuntimeError("native-fps encode failed: %s" % r.stderr[-400:])
    return dst_path


def publish(sg, shot, task, media_path, anchor, params, frames_final, seed, version):
    # REGEN-FIX: `version` comes from next_publish_version() (disk + ShotGrid,
    # never a clock or an in-memory counter) so this code cannot collide with
    # a prior generation's Version the way the old time.time()-derived tag
    # could, and now matches the bridged filename's own version number.
    code = "%s_%s_a14b_v%03d" % (shot["code"], STEP, version)

    # SUPERSEDE THE PRIOR APPROVED VIDEO ON THIS SHOT, BEFORE PUBLISHING.
    #
    # sg_review_housekeeping rejects any 'rev' Version whose (entity, stage)
    # already has an approved sibling, and it runs LIVE inside the service. So
    # re-queueing a shot that ALREADY has an approved video produced a
    # candidate that was auto-rejected within one cycle, before anyone looked
    # at it. Measured 2026-09-06 on SHOW01_A_0040: the re-render for the
    # interpolation fix landed as v001 'rjct' with v000 still 'apr'.
    #
    # This is the THIRD surface of one defect. Panels were fixed in
    # prompt_revision.apply_for_shot, cuts in episode_assemble, and this is
    # the video path. Same shape every time: nothing un-approves what it
    # replaces, so an approval silently arms a trap for its own successor.
    try:
        prior = sg.find("Version",
                        [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                         ["sg_stage", "is", "video"]],
                        ["code", "sg_status_list"]) or []
        for pv in prior:
            if pv.get("sg_status_list") in SUPERSEDABLE:
                sg.update("Version", pv["id"], {"sg_status_list": "rjct"})
                log("superseded %s (%s -> rjct): a newer video is being published "
                    "for this shot" % (pv.get("code"), pv.get("sg_status_list")))
    except Exception as exc:                                  # noqa: BLE001
        # Loud, never fatal: failing to supersede costs an auto-reject later,
        # losing the render we just paid GPU time for costs more.
        log("WARNING: could not supersede prior videos (%s: %s). The new "
            "Version may be auto-rejected by review housekeeping."
            % (type(exc).__name__, str(exc)[:120]))
    shortfall = params.get("shortfall") or 0.0
    speed_note = ("  MOTION SPEED IS NEUTRAL (native %g fps, trimmed not retimed)."
                  % TL.FPS)
    if shortfall > 0.01:
        speed_note += (" Shot asked for %.2fs more than the generation contains; "
                       "delivered the full generation rather than slowing it."
                       % shortfall)
    # THE DESCRIPTION IS BUILT FROM WHAT ACTUALLY HAPPENED, NOT FROM A
    # SENTENCE ABOUT WHAT USED TO HAPPEN. Until 2026-09-08 this glued a
    # hardcoded clause claiming the clip was retimed from 16 to 24 fps by
    # minterpolate onto EVERY publish, and then appended speed_note saying
    # the opposite. Both sentences shipped in the same description. Geoff
    # read it and asked why new videos were still doing fps shifts; ffprobe
    # on the file he named says 832x480, r_frame_rate 16/1, 49 frames, so
    # nothing was retimed and the clause was pure fiction. Same family as
    # F310, F405 and F445: a record assembled beside the code that acts
    # rather than by it. A misleading record costs more than an absent one,
    # because it sends the reader looking for a defect that is not there.
    desc = ("Phase 6: i2v from approved panel %s. A14B %dx%d, 4-step Lightning, cfg 1.0, "
           "%d frames at %g fps native. seed %d."
           % (anchor["version_code"], params["width"], params["height"],
              frames_final, TL.FPS, seed) + speed_note)
    v = sg.create("Version", {
        "project": PROJ, "entity": {"type": "Shot", "id": shot["id"]},
        "sg_task": task, "code": code, "description": desc,
        "sg_status_list": "rev", "sg_stage": "video",
        "sg_first_frame": 1001, "sg_last_frame": 1000 + frames_final,
        "sg_path_to_movie": media_path,
        "sg_gen_seed": seed, "sg_gen_steps": STEPS, "sg_gen_cfg": CFG,
        "sg_gen_size_wxh": "%dx%d" % (params["width"], params["height"]),
        "sg_model": MODEL_NAME,
        "sg_workflow_template": os.path.basename(TPL_I2V),
        "sg_gen_seconds": round(float(params.get("took") or 0.0), 2),
        # The prompt the GPU actually received, read from the generate call
        # rather than rebuilt here. Absent only if this Version was published
        # by a path that did not generate (and then it says so, rather than
        # being silently blank).
        "sg_prompt_final__as_sent_": (
            params.get("prompt_sent")
            or "[NOT RECORDED BY THE GENERATE STEP -- do not read this as what was sent]"),
    })
    sg.upload("Version", v["id"], media_path, field_name="sg_uploaded_movie", display_name=code)
    return v, code


def process_shot(sg, shot, production=False, dry=False):
    """One queued shot -> refused, or generated+published. Never raises past
    here except for genuinely unexpected errors; refusal is caught and turned
    into a loud, recorded state (invariant 3)."""
    try:
        anchor = validate_panel_anchor(sg, shot)
    except PanelAnchorRefusal as exc:
        log("REFUSED %s: %s" % (shot["code"], exc))
        if not dry:
            sg.update("Shot", shot["id"],
                      {"sg_gen_status": "refused", "sg_gen_log": "REFUSED: %s" % exc})
        return "refused", str(exc)

    if dry:
        log("(dry) %s: anchor OK -> %s (Version %s)"
            % (shot["code"], anchor["image_path"], anchor["version_id"]))
        return "would-generate", anchor

    try:
        prompt, negative = build_prompt(shot)
    except PanelAnchorRefusal as exc:
        log("REFUSED %s: %s" % (shot["code"], exc))
        sg.update("Shot", shot["id"],
                  {"sg_gen_status": "refused", "sg_gen_log": "REFUSED: %s" % exc})
        return "refused", str(exc)

    sg.update("Shot", shot["id"],
              {"sg_gen_status": "generating",
               "sg_gen_log": "i2v from panel %s" % anchor["version_code"]})

    seed = int(shot.get("sg_gen_seed") or 1000)
    task = sg.find_one("Task", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                                ["step.Step.short_name", "is", STEP]], ["content"])
    if task is None:
        step = sg.find_one("Step", [["short_name", "is", STEP], ["entity_type", "is", "Shot"]], [])
        task = sg.create("Task", {"project": PROJ, "entity": {"type": "Shot", "id": shot["id"]},
                                  "step": step, "content": "Comp"})

    # REGEN-FIX (defect 1): derived from disk + ShotGrid, not from seed (the
    # old bug) or from any counter kept only in this process.
    version = next_publish_version(sg, shot["id"], shot["code"])

    try:
        image_filename = stage_image_for_comfy(anchor["image_path"], shot["code"])
        prefix, params = generate_i2v(shot["code"], image_filename, prompt, negative, seed,
                                      version, production=production)
        os.makedirs(OUT_PUB, exist_ok=True)
        native = os.path.join(OUT_PUB, "%s_a14b_v%03d_native16.mp4" % (shot["code"], version))
        bridged = os.path.join(OUT_PUB, "%s_a14b_v%03d.mp4" % (shot["code"], version))
        encode_native_16fps(prefix, native)
        # SHOT LENGTH IS A TRIM, NOT A RETIME, on the native timeline.
        #
        # sg_gen_frames was authored against a 24fps timeline, so it is
        # converted here rather than reinterpreted: 73 frames at 24fps is
        # 3.04s, which is 49 frames at 16. Reading the stored number as
        # 16fps frames would silently make every shot 1.5x longer.
        stored = int(shot.get("sg_gen_frames") or 121)
        if TL.is_native_timeline():
            target_frames = TL.frames_for(stored / 24.0)
            # CLAMP TO WHAT THE MODEL ACTUALLY GENERATED, never slow to fill.
            #
            # Geoff, 2026-09-06: "I'd trust the neutral motion speed from the
            # model and if we need fast/slow action try to prompt for it."
            #
            # sg_gen_frames is a whole number of seconds at 24fps (73/97/121/
            # 145/169/193/217 = 3..9s), estimated from beat length, and 16 of
            # 55 SHOW01 shots ask for LONGER than the 5.0625s the generator
            # produces. The old path bought that length with motion speed:
            # _0090 played 1.59x slow, _0030 1.66x fast. Nothing said so.
            #
            # So a shorter shot is a trim, and a longer one gets the full
            # generation with the shortfall LOGGED and recorded in the
            # Version description. Motion is neutral in every case, which is
            # the property Geoff asked for. Making a genuinely longer shot is
            # a generation question, not a retime one.
            native_frames = TL.NATIVE_FRAMES
            if target_frames > native_frames:
                log("  shot asks for %d frames (%.2fs) but the generation has "
                    "%d (%.2fs). Delivering the full generation at NEUTRAL "
                    "speed rather than slowing it to fill."
                    % (target_frames, target_frames / TL.FPS,
                       native_frames, native_frames / TL.FPS))
                shortfall = (target_frames - native_frames) / TL.FPS
                target_frames = native_frames
            else:
                shortfall = 0.0
                log("  shot length %d frames @24 -> %d frames @%g (trim, "
                    "motion unchanged)" % (stored, target_frames, TL.FPS))
            params["shortfall"] = shortfall
            FB.native_conform(native, bridged, target_frames, log=log)
        else:
            target_frames = stored
            FB.bridge_fps(native, bridged, target_frames, log=log)
    except Exception as exc:
        log("ERROR %s: %s" % (shot["code"], exc))
        sg.update("Shot", shot["id"], {"sg_gen_status": "error", "sg_gen_log": str(exc)[:900]})
        return "error", str(exc)

    v, code = publish(sg, shot, task, bridged, anchor, params, target_frames, seed, version)

    # E2E-DEFECT-FIX (defect 2): everything from here on is bookkeeping AFTER
    # the real work (render + publish) already succeeded. This whole tail
    # used to run with no try/except of its own -- a single bad line here
    # (there was one: the camera= provenance string below had one more %d
    # than it had arguments, a deterministic TypeError on every single
    # panel-anchored publish) raised past process_shot(), past the shots
    # loop in main(), and crashed the whole subprocess with rc=1 AFTER the
    # Version was already created -- "SUBPROCESS FAILED rc=1" for a run that
    # had, in every way that matters, succeeded. Wrapping the tail means a
    # genuinely transient failure here (a dropped ShotGrid connection, per
    # the D13-ANCHOR-SHEETS.md precedent) is now caught, logged with the
    # REAL exception instead of the finally: block's teardown line, and
    # reported as what it actually is: the artifact published, but its
    # bookkeeping did not complete -- not "everything failed."
    try:
        subs = {"unet_high": UNET_HIGH, "unet_low": UNET_LOW, "lora_high": LORA_HIGH,
               "lora_low": LORA_LOW, "lora_strength": LORA_STRENGTH, "shift": SHIFT,
               "vae_name": VAE_NAME, "start_image": image_filename, "prompt": prompt,
               "negative_prompt": negative, "width": str(params["width"]),
               "height": str(params["height"]), "length": str(FRAMES), "steps": str(STEPS),
               "cfg": str(CFG), "switch_step": str(SWITCH_STEP)}
        wf_hash = PROV.workflow_hash_from_template(TPL_I2V, subs)
        char_codes, set_codes, other_codes = PROV.classify_assets(sg, shot)
        if other_codes:
            set_codes = set_codes + ["(unclassified: %s)" % ", ".join(other_codes)]
        PROV.write_provenance(
            sg, v["id"],
            character=", ".join(char_codes) or "none linked",
            set_=", ".join(set_codes) or "none linked",
            action=prompt,
            camera=("%s / %s / %dx%d (A14B cell), delivery raster is the "
                    "cell's own %dx%d" % (shot.get("sg_camera") or "unspecified",
                                          shot.get("sg_shot_size") or "unspecified",
                                          params["width"], params["height"],
                                          params["width"], params["height"])),
            style=("n/a - Lightning cfg=1.0: negative prompt %r is composed but INERT at this cfg; "
                  "all steering is in the positive prompt above" % negative),
            workflow_hash=wf_hash,
            anchor_version_id=anchor["version_id"],
            log=log)

        ledger_path = os.path.join(LEDGER_DIR, "%s.json" % shot["code"])
        led = RL.load(ledger_path)
        # REGEN-FIX: record the SAME version number used for the file and the
        # SG Version code -- previously this counted ledger "version" entries
        # instead, a THIRD, independent counter that could (and did, in
        # effect) disagree with both.
        RL.append(led, "version", version_number=version, code=code, sg_id=v["id"],
                  seed=seed, reference=anchor["version_code"],
                  movie=os.path.basename(bridged))
        RL.append(led, "status", status="rev", sg_id=v["id"])
        RL.save(ledger_path, led)

        entry = ("i2v v published as Version %d from panel %s (seed %d, %.1fs, %d->%d frames)"
                 % (v["id"], anchor["version_code"], seed, params.get("took") or 0.0,
                    FRAMES, target_frames))
        sg.update("Shot", shot["id"], {"sg_gen_status": "review", "sg_gen_log": entry})
        log("  %s -> %s" % (shot["code"], entry))
        return "published", v["id"]
    except Exception as exc:
        msg = ("Version %d published and uploaded, but post-publish bookkeeping "
               "(provenance write and/or the final status flip) FAILED: %s: %s"
               % (v["id"], type(exc).__name__, str(exc)[:500]))
        log("ERROR %s: %s" % (shot["code"], msg))
        try:
            sg.update("Shot", shot["id"], {"sg_gen_status": "error", "sg_gen_log": msg})
        except Exception as exc2:
            log("  FAILED to even record the error on %s: %s: %s"
                % (shot["code"], type(exc2).__name__, str(exc2)[:200]))
        return "error", msg


FIELDS = ["id", "code", "sg_approved_panel", "sg_gen_prompt", "sg_gen_negative_prompt",
         "sg_script_beat", "sg_gen_seed", "sg_gen_frames", "sg_camera", "sg_shot_size",
         "sg_gen_status"]


# ============================================================================
# E2E-DEFECT-FIX (defect 1, CRITICAL): the real, structural fix.
#
# What actually happened live, 2026-08-29 (see docs/METHOD.md
# for the full timeline): the OLD code below this comment used to be a SINGLE
# loop -- `for s in shots: process_shot(sg, s, ...)` -- that validated and
# either refused or rendered EACH shot IN PLACE, one at a time, in whatever
# order ShotGrid returned them. genvideo_service.py's own cycle() ordering
# (Phase 6's watcher before step 1, same file) was correct and is NOT what
# broke: it guarantees this whole subprocess is awaited before the older,
# panel-unaware genvideo_worker.py ever gets to look at sg_gen_status=queued.
# The hole was ONE LEVEL DEEPER, entirely inside this subprocess: when the
# batch was [[eligible shot A], [ineligible shot B]], the old loop rendered A
# first (real wall-clock minutes) and only reached -- and refused -- B
# afterward. For those minutes B sat at 'queued', exactly the value
# genvideo_worker.py's own step 1 watches, in the SAME service cycle, the
# instant this subprocess returns. It returned early that night (rc=1, a
# provenance bug -- see defect 2, now fixed) before ever reaching B, so B was
# never refused at all; genvideo_worker.py claimed it and published Version
# 67180 anchored on a set Asset (STYLE_CHARB_FLAT) -- the exact V1 failure.
#
# The earlier collision_ordering_test in build/tests/panel_video_refusal.py
# "proved" this couldn't happen, but only by mocking away this entire
# subprocess call (an instant fake `run()`), which cannot see -- and did not
# see -- an ordering bug that lives INSIDE what it mocked out.
#
# THE FIX: two strictly separate phases, run_once() below. Phase 1 is a fast,
# render-free sweep over EVERY queued shot: validate_panel_anchor() only,
# refuse (a state write, no GPU) anything ineligible. Phase 2, entered only
# after phase 1 has finished for every shot in the batch, renders the
# eligible ones. By construction, no rendering of any kind -- and therefore
# no wall-clock delay of any kind -- can begin until every ineligible shot in
# the batch has already left 'queued'. A slow render (or a crash mid-render,
# or a dropped network connection) can no longer matter to this race, because
# by the time it starts there is nothing left for it to race against.
# ============================================================================
def run_once(sg, shots, production=False, dry=False):
    if not shots:
        log("nothing queued")
        return 0

    if dry:
        # --dry never writes ShotGrid (module docstring) -- delegate straight
        # to process_shot's own dry branch for every shot, unchanged from
        # before this fix.
        for s in shots:
            process_shot(sg, s, production=production, dry=True)
        return 0

    # PHASE 1 -- fast sweep, no rendering, no GPU. Every shot without a valid
    # anchor is refused HERE, synchronously, before phase 2 does anything.
    to_generate = []
    for s in shots:
        try:
            validate_panel_anchor(sg, s)
            to_generate.append(s)
        except PanelAnchorRefusal as exc:
            log("REFUSED %s: %s" % (s["code"], exc))
            sg.update("Shot", s["id"],
                      {"sg_gen_status": "refused", "sg_gen_log": "REFUSED: %s" % exc})

    if not to_generate:
        return 0   # refuse-only pass: never touches the GPU (module docstring)

    # PHASE 2 -- only the shots phase 1 already proved eligible. Nothing
    # ineligible remains at 'queued' by this point, however long this takes.
    if not comfy_start():
        log("cannot start ComfyUI; leaving queue for next pass")
        return 1
    try:
        for s in to_generate:
            try:
                process_shot(sg, s, production=production, dry=False)
            except Exception as exc:
                # Last-resort net: process_shot() already catches its own
                # render/publish/provenance failures (see its own try/except
                # blocks). If something still escapes here, it must not take
                # the rest of THIS batch down with it -- that is the same
                # class of bug (one shot's crash silently protecting no one
                # after it in the list) that let defect 1 happen, just one
                # layer further out.
                log("ERROR %s: unexpected exception escaped process_shot(): %s: %s"
                    % (s["code"], type(exc).__name__, str(exc)[:400]))
                try:
                    sg.update("Shot", s["id"], {"sg_gen_status": "error",
                              "sg_gen_log": ("unexpected error: %s: %s"
                                             % (type(exc).__name__, str(exc)[:800]))})
                except Exception:
                    pass
    finally:
        comfy_stop()   # invariant 4: finally, not good intentions

    return 0

# ============================================================================
# END E2E-DEFECT-FIX (defect 1)
# ============================================================================


def cmd_self_test():
    """Source-level canaries on the provenance seam, and HONEST about being
    source-level: this module drives a GPU graph, so there is no cheap way to
    execute publish() here. What it CAN prove is that the record and the call
    still read from the same place, which is the exact defect that produced
    F405 (a re-derived record that disagreed with what was sent) and the one
    measured here: 7 of 191 generated video Versions carried a prompt against
    998 of 1011 panels.

    THIS MODULE HAD NO --self-test AT ALL until 2026-09-08, so the deploy
    preflight, which discovers tools by that flag, had never covered the video
    generator."""
    import inspect
    fails = []

    def ck(label, ok):
        print("  %-92s %s" % (label[:92], "ok" if ok else "FAILED"))
        if not ok:
            fails.append(label)

    gen = inspect.getsource(generate_i2v)
    pub = inspect.getsource(publish)
    ck("CANARY: generate_i2v carries the sent prompt OUT, from the same variable "
       "it hands to the graph",
       '"prompt_sent": prompt' in gen and '"negative_sent": negative' in gen)
    ck("CANARY: publish RECORDS that prompt on the Version and does not rebuild it",
       '"sg_prompt_final__as_sent_"' in pub and 'params.get("prompt_sent")' in pub)
    ck("CANARY: a Version published without a recorded prompt SAYS so rather than "
       "carrying a silent empty field",
       "NOT RECORDED BY THE GENERATE STEP" in pub)
    ck("CANARY: publish still records seed, steps and cfg, so adding the prompt "
       "did not displace the existing provenance",
       '"sg_gen_seed"' in pub and '"sg_gen_steps"' in pub and '"sg_gen_cfg"' in pub)
    ck("CANARY: the prompt is still sanitised through dialogue_guard before any "
       "of this, so what is recorded is what was sent, dialogue stripped",
       "DG.strip_dialogue_for_prompt(" in inspect.getsource(build_prompt)
       if "build_prompt" in globals() else True)
    # Needles assembled from chr() so this canary cannot match the comment
    # above publish() that explains what was removed. A docstring is not a
    # comment either, so strip nothing and build the needle instead.
    _pub_code = "".join(l for l in pub.splitlines(True)
                        if not l.lstrip().startswith(chr(35)))
    _retime_words = ("minterpol" + "ate", "brid" + "ged", "24" + "fps")
    ck("CANARY: the published description never claims a retime the pipeline "
       "does not perform",
       not any(w in _pub_code for w in _retime_words))
    ck("CANARY: the description states the frame count and fps it actually "
       "used, from variables rather than a fixed sentence",
       "frames_final, TL.FPS, seed" in _pub_code)
    print()
    print("ALL PASS" if not fails else "FAILED: %d" % len(fails))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup-schema", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--shot")
    ap.add_argument("--production", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()

    # BEFORE sg_connect(): the preflight runs this with no ShotGrid
    # credentials and must not need them.
    if ns.self_test:
        return cmd_self_test()

    sg = sg_connect()
    if ns.setup_schema:
        setup_schema(sg)
        return 0

    if ns.shot:
        shots = [sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", ns.shot]], FIELDS)]
        if not shots[0]:
            sys.exit("shot %s not found" % ns.shot)
    else:
        shots = sg.find("Shot", [["project", "is", PROJ], ["sg_gen_status", "is", "queued"]], FIELDS)

    return run_once(sg, shots, production=ns.production, dry=ns.dry)


if __name__ == "__main__":
    sys.exit(main())
