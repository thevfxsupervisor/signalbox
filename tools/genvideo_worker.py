#!/usr/bin/env python3
"""The gen-video production worker: ShotGrid fields in, reviewed Versions out.

The operator's whole interface is ShotGrid:

  1. Fill the sg_gen_* fields on a Shot in GENVIDEO_TEST (prompt, kind, size,
     frames, steps, cfg, seed) and link reference Assets via the Shot's normal
     `assets` field.
  2. Flip `sg_gen_status` to **queued**. That flip IS the button.
  3. The worker generates (image-to-video FROM the first linked reference, or
     text-to-video when no reference is linked), publishes a Version set to
     `rev`, and flips the Shot to **review**.
  4. Reviewer leaves a Note on the Version and/or sets it to `rrq`. The worker
     sees it, regenerates with a bumped seed as the NEXT version (never an
     overwrite), republishes, back to review. Every Note is kept VERBATIM in
     the ledger and in sg_gen_log.
  5. A Version reaching an approved status closes the Shot: sg_gen_status=done.
     Nothing ships anywhere; approval ends the loop, it does not deliver.

Design rules inherited from the build (each was earned today, not decorative):
  - GPU guard before any generation; this box is shared with Fusion Render Node.
  - ComfyUI started on demand, torn down when the queue is empty, teardown
    verified by port+process+nvidia-smi. Never left running (Geoff's rule).
  - Prompt-negation audit on every positive prompt; a negation REFUSES the job
    with the reason in sg_gen_log rather than generating garbage.
  - Wall-clock + expected-file-count verification via comfyui_execute.py
    (disk mode); a cache hit cannot be mistaken for a generation because every
    version gets a fresh seed and prefix.
  - Version numbers come from the ledger's highest-ever-seen, so a deleted
    Version cannot cause a number to be reused while Notes about it exist.
  - cfg default is 5.0: measured correct for TI2V-5B with no distill LoRA.

Usage:
    python genvideo_worker.py --once        one full pass (poll, work, teardown)
    python genvideo_worker.py --dry-run     poll and report, change nothing
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------- constants
PROJECT_ID = 9999
STEP = "CMP"
PY = sys.executable
ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was a sibling-relative guess
# (os.path.dirname(__file__)/../tests) that only holds when tools/ and
# tests/ sit next to each other -- see episode_assemble.py's identical fix
# for the full explanation. tests/ has no deploy seam of its own yet, so
# this points at the one stable location every other module already uses.
sys.path.insert(0, os.path.join(ROOT, "build", "tests"))
import review_ledger as RL                                    # noqa: E402
import shotgun_api3                                           # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sg_provenance as PROV                                   # noqa: E402
import sg_publish as PUB                                        # noqa: E402
import dialogue_guard as DG                                     # noqa: E402  D15, invariant 11
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools",
# "comfyui", "comfyui_execute.py") -- a hardcoded guess at the deployed
# location, independent of where this file itself actually ran from. Self-
# relative now, so the wrapper resolves inside whichever release deployed it.
WRAPPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comfyui", "comfyui_execute.py")
TPL_T2V = os.path.join(ROOT, "workflows", "wan22_ti2v_5b_t2v.api.json")
TPL_I2V = os.path.join(ROOT, "workflows", "wan22_ti2v_5b_i2v.api.json")
TESTS = os.path.join(ROOT, "build", "tests")
OUT_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\output"
IN_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\input"
OUT_PUB = os.path.join(ROOT, "output", "published")
LEDGER_DIR = os.path.join(ROOT, "output", "ledger")
COMFY_EXE = r"C:\ComfyUI_windows_portable\python_embeded\python.exe"
COMFY_MAIN = r"C:\ComfyUI_windows_portable\ComfyUI\main.py"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# THE SEVEN-STATUS SHAPE, not the five. poll_feedback() uses this to decide a
# Shot is DONE, and it was missing appcbb and appgra while every other owner of
# the vocabulary carried them (sg_publish.SUPERSEDABLE,
# note_triage.APPROVED_VERSION_STATUSES, genvideo_service.DECIDED_VERSION_STATUSES,
# sg_review_housekeeping.APPROVED, and this file own _lost_an_alternate default).
# A video approved as appcbb or appgra fell through every branch and left the
# Shot at sg_gen_status=review FOREVER, with no error. Latent rather than live:
# 0 Versions carry those statuses today, and both are valid values an operator
# can pick at any moment. Found by a grooming sweep 2026-09-09.
#
# The five-status shape is still correct for APPROVED_BOARD and for the panel
# vocabulary; this one is not a panel gate.
APPROVED = {"apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra"}
NEEDS_REVISION = {"rrq", "rjct", "tekfix"}

DEFAULTS = {"size": "512x288", "frames": 25, "steps": 10, "cfg": 5.0, "seed": 1000,
            "kind": "video",
            # Universal negative baseline from build/reference/skills/generative-ai/wan-prompt.md
            # ("Always include a negative prompt. WAN responds strongly to them.")
            "negative": ("bright colors, overexposed, static, blurred details, subtitles, style, "
                         "artwork, painting, picture, still, overall gray, worst quality, low quality, "
                         "JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, "
                         "poorly drawn faces, deformed, disfigured, malformed limbs, fused fingers, "
                         "still picture, cluttered background, three legs, walking backwards")}


# ---------------------------------------------------------------- fleet safety
# Several worker nodes may share one ShotGrid queue. WORKER_ID names this node
# inside sg_gen_log so a claim can be attributed. Override order: --worker-id
# on the CLI, then GENVIDEO_WORKER_ID, then hostname (unique per box, always
# set, needs no configuration).
WORKER_ID = os.environ.get("GENVIDEO_WORKER_ID", socket.gethostname())
if "--worker-id" in sys.argv[:-1]:
    # [:-1] so a trailing bare "--worker-id" with no value cannot IndexError.
    _wid = sys.argv[sys.argv.index("--worker-id") + 1]
    if _wid.startswith("--"):
        # The "value" is another flag: --worker-id was typed with no value
        # mid-line. Silently claiming shots as "--dry-run" would poison every
        # claim tag in sg_gen_log, so refuse to start instead.
        sys.exit("--worker-id needs a value, got flag %r" % _wid)
    WORKER_ID = _wid

# Capacity envelope measured by p6_envelope.py on THIS node. A missing file
# means the sweep has not run yet; that is fatal only when ENVELOPE_STRICT,
# because refusing every job on an unmeasured node would silently halt work.
ENVELOPE_PATH = os.path.join(ROOT, "build", "p6_envelope.json")
ENVELOPE_STRICT = False


def claim_shot(sg, shot):
    """Compare-and-claim a queued Shot. ShotGrid has no atomic test-and-set,
    so we write our tag into sg_gen_log with the queued->generating flip, give
    any racing node's write time to land, then re-read: whichever node's tag
    SURVIVES owns the Shot. Losing the race is normal fleet behavior (the
    other node generates it), not an error."""
    tag = "[claim %s]" % WORKER_ID
    sg.update("Shot", shot["id"],
              {"sg_gen_status": "generating", "sg_gen_log": tag})
    time.sleep(1.0)  # let a racing node's update land before the re-read
    cur = sg.find_one("Shot", [["id", "is", shot["id"]]], ["sg_gen_log"]) or {}
    got = cur.get("sg_gen_log") or ""
    if tag not in got:
        log("  %s claimed by another node (%r) - skipping" % (shot["code"], got[:80]))
        return False
    return True


def load_envelope():
    """Measured capacity cells from p6_envelope.py, or None when the sweep
    has not been run on this node."""
    if not os.path.exists(ENVELOPE_PATH):
        return None
    with open(ENVELOPE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def envelope_check(size, frames):
    """Return (ok, note). note is a warning to log when ok, or the refusal
    text destined for sg_gen_log when not ok.

    Distance between configs is pixel volume (width*height*frames) because
    VRAM and wall clock scale with the latent volume, not the aspect ratio.
    Refuse when the nearest measured cell FAILED, or when the requested
    volume exceeds every OK cell (nothing that large ever succeeded here).
    A config with no exact measured cell is unmeasured territory: refused
    only when ENVELOPE_STRICT, else warn and proceed."""
    cells = load_envelope()
    if cells is None:
        if ENVELOPE_STRICT:
            return False, ("ENVELOPE REFUSAL: %s missing and strict mode on; "
                           "run p6_envelope.py on this node first" % ENVELOPE_PATH)
        return True, "envelope not measured on this node; proceeding (strict off)"
    m = re.fullmatch(r"(\d+)x(\d+)", size)
    if not m:
        return False, "ENVELOPE REFUSAL: unparseable size %r" % (size,)
    volume = int(m.group(1)) * int(m.group(2)) * max(int(frames), 1)

    def cell_volume(c):
        w, h = c["size"].split("x")
        return int(w) * int(h) * max(int(c["frames"]), 1)

    ok_cells = [c for c in cells if c.get("ok")]
    if not ok_cells:
        return False, ("ENVELOPE REFUSAL: no measured cell has ever succeeded "
                       "on this node; see %s" % ENVELOPE_PATH)
    nearest = min(cells, key=lambda c: abs(cell_volume(c) - volume))
    if not nearest.get("ok"):
        # Checked BEFORE the volume ceiling: a measured failure right next to
        # this config outranks any extrapolation from cells that passed.
        return False, ("ENVELOPE REFUSAL: nearest measured cell %s x %d frames "
                       "FAILED on this node (%s)"
                       % (nearest["size"], nearest["frames"],
                          (nearest.get("error") or "no error text")[:120]))
    if volume > max(cell_volume(c) for c in ok_cells):
        return False, ("ENVELOPE REFUSAL: %s x %d frames (volume %d) exceeds "
                       "every OK cell in %s" % (size, frames, volume, ENVELOPE_PATH))
    exact = any(c["size"] == size and int(c["frames"]) == int(frames) for c in cells)
    if not exact:
        if ENVELOPE_STRICT:
            return False, ("ENVELOPE REFUSAL: %s x %d frames not measured and "
                           "strict mode on" % (size, frames))
        return True, ("%s x %d frames not measured; nearest cell OK, proceeding "
                      "(strict off)" % (size, frames))
    return True, None


def sg_connect():
    return shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                                script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                                api_key=os.environ["SHOTGRID_SCRIPT_KEY"])


def log(msg):
    print("[worker] %s" % msg, flush=True)


# ---------------------------------------------------------------- comfy lifecycle
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
    subprocess.Popen([COMFY_EXE, "-s", COMFY_MAIN, "--windows-standalone-build",
                      "--listen", "127.0.0.1", "--port", "8188"],
                     stdout=open(os.path.join(LEDGER_DIR, "comfy_worker.log"), "a"),
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


# ---------------------------------------------------------------- generation
# --- STYLE_FROM_SEQUENCE -----------------------------------------------------
# The palette lives on the Sequence so one field edit restyles every shot under
# it. Missing fields are not an error: a sequence with no style set simply gets
# the shot prompt verbatim, which is what an un-styled test shot should do.
def sequence_style(sg, shot):
    seq = shot.get("sg_sequence")
    if not seq:
        return "", "", ""
    try:
        s = sg.find_one("Sequence", [["id", "is", seq["id"]]],
                        ["sg_style_prefix", "sg_style_suffix", "sg_style_negative"])
    except Exception:
        return "", "", ""
    if not s:
        return "", "", ""
    return (s.get("sg_style_prefix") or "",
            s.get("sg_style_suffix") or "",
            s.get("sg_style_negative") or "")


def compose_styled(prompt, prefix, suffix):
    """Idempotent: a prompt already carrying the prefix is not double-styled."""
    out = (prompt or "").strip()
    if prefix and prefix.strip().lower() not in out.lower():
        out = prefix.strip() + " " + out
    if suffix and suffix.strip().lower() not in out.lower():
        out = out.rstrip(" ,.") + suffix
    return " ".join(out.split())


# --- PROVENANCE_V1 -----------------------------------------------------------
# Real ShotGrid field names. ShotGrid rewrites field codes on creation
# (sg_prompt_final -> sg_prompt_final__as_sent_), and writing the name we asked
# for instead of the name we got would fail silently, which is the worst kind.
V_PROMPT = "sg_prompt_final__as_sent_"
V_NEG = "sg_negative_final__as_sent_"
V_REFS = "sg_reference_assets"
V_MODEL = "sg_model"
V_WORKFLOW = "sg_workflow_template"
V_SECONDS = "sg_gen_seconds"
V_STAGE = "sg_stage"
S_PROMPT = "sg_prompt_final__composed_"
S_NEG = "sg_negative_final__composed_"

MODEL_NAME = "wan2.2_ti2v_5B_fp16"


def asset_fragments(sg, shot):
    """The reusable prompt-fragment library: any linked Asset carrying
    sg_prompt_fragment contributes it, ordered by sg_fragment_order."""
    links = shot.get("assets") or []
    if not links:
        return "", ""
    try:
        rows = sg.find("Asset", [["id", "in", [a["id"] for a in links]]],
                       ["code", "sg_prompt_fragment", "sg_negative_fragment",
                        "sg_fragment_order"])
    except Exception:
        return "", ""
    rows.sort(key=lambda r: (r.get("sg_fragment_order") or 999, r.get("code") or ""))
    pos = [r["sg_prompt_fragment"].strip() for r in rows if r.get("sg_prompt_fragment")]
    neg = [r["sg_negative_fragment"].strip() for r in rows if r.get("sg_negative_fragment")]
    return ", ".join(pos), ", ".join(neg)


# --- D6_COMPONENTS ------------------------------------------------------------
# classify_assets() lives in sg_provenance.py (invariant 11's "one place, import
# it, never copy" spirit) - use PROV.classify_assets(sg, shot) directly.


def style_component_text(style):
    prefix, suffix, _negative = style
    if not prefix and not suffix:
        return "none (no sg_style_prefix/suffix set on this shot's Sequence)"
    return "prefix=%r suffix=%r" % (prefix, suffix)


def write_provenance(sg, version_id, shot_id, prompt, negative, params, ref_links, took):
    """One place that records how an artifact was made. Failing to write
    provenance must not fail the job, but it MUST be visible in the log."""
    data = {
        V_PROMPT: prompt, V_NEG: negative,
        "sg_gen_seed": params.get("seed"), "sg_gen_steps": params.get("steps"),
        "sg_gen_cfg": params.get("cfg"),
        "sg_gen_size_wxh": "%sx%s" % (params.get("width"), params.get("height")),
        V_MODEL: MODEL_NAME,
        V_WORKFLOW: os.path.basename(params.get("workflow") or ""),
        V_SECONDS: round(float(took or 0.0), 2),
        V_STAGE: "video" if params.get("kind") != "still" else "keyframe",
    }
    if ref_links:
        data[V_REFS] = ref_links
    try:
        sg.update("Version", version_id, data)
    except Exception as exc:
        log("  PROVENANCE WRITE FAILED on Version %s: %s" % (version_id, type(exc).__name__))
        return False
    try:
        sg.update("Shot", shot_id, {S_PROMPT: prompt, S_NEG: negative})
    except Exception:
        pass
    return True


def audit_prompt(prompt):
    r = subprocess.run([PY, os.path.join(TESTS, "prompt_negation_audit.py"),
                        "--prompt", prompt], capture_output=True, text=True)
    return r.returncode == 0, r.stdout.strip()


APPROVED_BOARD = ("apr", "ad", "fin", "paf", "dlvr")


def approved_board(sg, shot):
    """KEYFRAME_V1: the approved board for this shot, as a LoadImage filename.

    An approved board beats a show-level Asset sheet as an i2v start frame: it
    is this shot's framing, already reviewed and already on-model. Starting from
    it is what stops identity drifting between takes.

    Returns (filename, note, version_id) or (None, None, None) when no board is
    approved, in which case the caller falls back to the Asset reference.
    version_id is the anchor for D6 provenance (sg_anchor_version)."""
    vs = sg.find("Version",
                 [["project", "is", {"type": "Project", "id": PROJECT_ID}],
                  ["entity", "is", {"type": "Shot", "id": shot["id"]}],
                  ["sg_stage", "is", "board"]],
                 ["code", "sg_status_list", "sg_path_to_movie", "created_at"])
    ok = [v for v in vs if (v.get("sg_status_list") or "") in APPROVED_BOARD
          and (v.get("sg_path_to_movie") or "")]
    if not ok:
        return None, None, None
    ok.sort(key=lambda v: v.get("created_at") or 0)
    src = ok[-1]["sg_path_to_movie"]
    if not os.path.exists(src):
        log("  approved board %s has no file on disk; falling back to the asset ref"
            % ok[-1]["code"])
        return None, None, None
    local = "board_%s.png" % re.sub(r"[^A-Za-z0-9._-]", "_", shot["code"])
    dest = os.path.join(IN_COMFY, local)
    try:
        import shutil
        shutil.copyfile(src, dest)
    except OSError as exc:
        log("  could not stage board (%s); falling back to the asset ref" % exc)
        return None, None, None
    return local, "approved board %s" % ok[-1]["code"], ok[-1]["id"]


def fetch_reference(sg, shot):
    """Download the newest image Attachment of the first linked Asset into
    ComfyUI's input dir. Returns (filename, note, anchor_version_id); filename
    is None when there is nothing to reference. anchor_version_id is set only
    when the reference IS a ShotGrid Version (an approved board) - an Asset's
    raw Attachment is not a Version, so it cannot anchor sg_anchor_version.

    An approved board for THIS shot wins over the Asset sheet; see approved_board."""
    kf, kf_note, kf_vid = approved_board(sg, shot)
    if kf:
        return kf, kf_note, kf_vid
    assets = shot.get("assets") or []
    if not assets:
        return None, None, None
    ref = assets[0]
    atts = sg.find("Attachment",
                   [["attachment_links", "in", [{"type": "Asset", "id": ref["id"]}]]],
                   ["this_file", "created_at"],
                   order=[{"field_name": "created_at", "direction": "desc"}])
    for att in atts:
        f = att.get("this_file") or {}
        url, name = f.get("url"), (f.get("name") or "")
        if url and name.lower().endswith((".png", ".jpg", ".jpeg")):
            local = "sgref_%d_%s" % (ref["id"], re.sub(r"[^A-Za-z0-9._-]", "_", name))
            dest = os.path.join(IN_COMFY, local)
            if not os.path.exists(dest):
                with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as out:
                    out.write(r.read())
            return local, ref.get("name") or ref.get("code"), None
    return None, ref.get("name") or ref.get("code"), None


def generate(shot_fields, prefix, seed, ref_filename, style=("", "", "")):
    size = (shot_fields.get("sg_gen_size_wxh") or DEFAULTS["size"]).lower().replace(" ", "")
    m = re.fullmatch(r"(\d+)x(\d+)", size)
    if not m:
        return False, "bad sg_gen_size_wxh %r, want like 512x288" % size
    width, height = m.group(1), m.group(2)
    kind = shot_fields.get("sg_gen_kind") or DEFAULTS["kind"]
    frames = 1 if kind == "still" else int(shot_fields.get("sg_gen_frames") or DEFAULTS["frames"])
    if (frames - 1) % 4 != 0:
        return False, "frames=%d invalid: (frames-1) must divide by 4" % frames
    env_ok, env_note = envelope_check("%sx%s" % (width, height), frames)
    if not env_ok:
        # Refuse BEFORE spending GPU time: the envelope already measured
        # this config (or its neighborhood) failing on this node.
        return False, env_note
    if env_note:
        log("envelope: %s" % env_note)
    steps = int(shot_fields.get("sg_gen_steps") or DEFAULTS["steps"])
    cfg = float(shot_fields.get("sg_gen_cfg") or DEFAULTS["cfg"])
    # D15: sg_gen_prompt is normally hand-authored or LLM-revised (prompt_
    # revision.py), never script-beat-formatted -- but that field is also the
    # apply target of prompt_revision.py's note-driven revision loop, whose
    # gathered components include the shot's own sg_script_beat (dialogue and
    # all). Stripping here, at the point this text actually becomes a
    # generation call, is defense in depth that does not depend on every
    # upstream writer of sg_gen_prompt behaving (invariant 11).
    prompt = DG.strip_dialogue_for_prompt(shot_fields.get("sg_gen_prompt") or "").strip()
    if not prompt:
        return False, "sg_gen_prompt is empty"
    st_prefix, st_suffix, st_negative = style
    frag_pos, frag_neg = shot_fields.get("_fragments", ("", ""))
    if frag_pos:
        prompt = prompt.rstrip(" ,.") + ", " + frag_pos
    prompt = compose_styled(prompt, st_prefix, st_suffix)
    negative = (shot_fields.get("sg_gen_negative_prompt")
                or st_negative or DEFAULTS["negative"]).strip()
    if frag_neg:
        negative = negative.rstrip(" ,.") + ", " + frag_neg

    if ref_filename and "preserve all appearance" not in prompt.lower():
        # wan-prompt.md, I2V mode: "Always add 'Preserve all appearance and
        # environment from source image.'" The reference carries subject and
        # style; the prompt should carry motion, and this line stops the model
        # drifting off the reference it was given.
        prompt = prompt.rstrip(". ") + ". Preserve all appearance and environment from source image."

    ok, audit = audit_prompt(prompt)
    if not ok:
        return False, "REFUSED by prompt audit:\n%s" % audit

    tpl = TPL_I2V if ref_filename else TPL_T2V
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", tpl,
           "--comfy-output-dir", OUT_COMFY, "--verify-mode", "disk",
           "--output-prefix", prefix, "--seed-base", str(seed),
           "--expect-outputs", str(frames), "--timeout", "900",
           "--set", "prompt=%s" % prompt,
           "--set", "negative_prompt=%s" % negative,
           "--set", "width=%s" % width, "--set", "height=%s" % height,
           "--set", "length=%d" % frames, "--set", "steps=%d" % steps,
           "--set", "cfg=%s" % cfg]
    if ref_filename:
        cmd += ["--set", "start_image=%s" % ref_filename]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
    took = time.time() - t0
    if "PASS:" not in r.stdout:
        return False, "generation FAILED after %.1fs:\n%s" % (took, r.stdout[-800:])
    return True, {"frames": frames, "kind": kind, "took": took,
                  "width": width, "height": height, "steps": steps,
                  "cfg": cfg, "seed": seed,
                  "prompt": prompt, "negative": negative, "workflow": tpl}


def encode(prefix, kind, code):
    os.makedirs(OUT_PUB, exist_ok=True)
    if kind == "still":
        src = os.path.join(OUT_COMFY, "%s_w000_00001_.png" % prefix)
        dst = os.path.join(OUT_PUB, code + ".png")
        if os.path.exists(dst):
            return None, "refusing to overwrite %s" % dst
        import shutil
        shutil.copyfile(src, dst)
        return dst, None
    dst = os.path.join(OUT_PUB, code + ".mp4")
    if os.path.exists(dst):
        return None, "refusing to overwrite %s" % dst
    r = subprocess.run([FFMPEG, "-y", "-framerate", "24",
                        "-i", os.path.join(OUT_COMFY, prefix + "_w000_%05d_.png"),
                        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                        "-crf", "18", "-movflags", "+faststart", dst],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        return None, "encode failed"
    return dst, None


# ---------------------------------------------------------------- shotgrid I/O
def publish(sg, project, shot, task, n, media, params, ref_note, *,
           character, set_, action, camera, style, workflow_hash, anchor_version_id):
    code = "%s_%s_gen_v%03d" % (shot["code"], STEP, n)
    desc = ("Auto-published by genvideo_worker. %s %sx%s, %d frame(s), %d steps, cfg %s, seed %d.%s"
            % (params["kind"], params["width"], params["height"], params["frames"],
               params["steps"], params["cfg"], params["seed"],
               (" Reference: %s (i2v)." % ref_note) if ref_note else " No reference (t2v)."))
    v = PUB.publish_version(
        sg, project=project, entity={"type": "Shot", "id": shot["id"]}, code=code,
        media_path=media, sg_task=task, description=desc,
        first_frame=1001, last_frame=1000 + params["frames"],
        character=character, set_=set_, action=action, camera=camera, style=style,
        workflow_hash=workflow_hash, anchor_version_id=anchor_version_id, log=log)
    return v, code


def process_shot(sg, project, shot):
    # Claim FIRST: on a fleet several nodes see the same queued Shot;
    # only the claim winner may create Tasks, spend GPU, or publish.
    if not claim_shot(sg, shot):
        return

    # E2E-DEFECT-FIX (defect 1, defense in depth): this worker has no concept
    # of an approved panel and, for video, would happily i2v/t2v off an
    # approved board or a raw Asset reference (fetch_reference() below) --
    # the exact V1 failure. video_from_panel.py (Phase 6) is now the sole,
    # panel-anchored video generator for this project; the structural fix for
    # the race that let this worker claim a still-queued shot mid-render
    # lives there (a fast, render-free refusal sweep that runs before any
    # rendering starts -- see its module docstring and
    # docs/METHOD.md). This check is the SECOND, independent
    # layer: even if that ordering were ever defeated again (it already was
    # once, live, 2026-08-29 -- Version 67180, PILOT01_A_0120, anchored on the
    # STYLE_CHARB_FLAT set asset), this worker itself must never be able to
    # produce an anchor-less video. Boards (a different trigger entirely --
    # Shot.sg_stage=='board', dispatched by animatic.py --boards, never by
    # this function) and keyframes (kind='still', no anchor concept applies)
    # are untouched: this refuses VIDEO only.
    kind = shot.get("sg_gen_kind") or DEFAULTS["kind"]
    if kind != "still":
        # DEFER, DO NOT REFUSE, WHEN THE PANEL PIPELINE CAN HANDLE IT.
        #
        # Refusing writes sg_gen_status='refused', which takes the shot OUT of
        # the queue, so video_from_panel never sees it. Both generators run in
        # the same cycle (watch_panel_video_queue is step 0, this worker is
        # step 1, and step 1 fires whenever ANY shot is queued or in review,
        # which is always: 24 sit in review permanently). So this was a race
        # whose LOSER destroyed the work.
        #
        # MEASURED 2026-09-06 on SHOW01_A_0040. Queued once, video_from_panel
        # won and rendered v001. Queued again, this worker won and wrote
        # 'refused', and the shot had an approved panel the whole time, so the
        # refusal text ("Approve a panel Version for this shot") was advising a
        # fix that was already in place. From the operator's side, queueing a
        # shot worked or silently did not, depending on timing.
        #
        # The defence-in-depth intent is UNCHANGED and still correct: this
        # worker must never produce an anchor-less video. It just no longer
        # takes the shot away from the tool that can do it properly. Refusal
        # is still loud and still correct when there IS no approved panel,
        # because then nothing else will pick it up and silence would strand
        # the shot instead.
        if shot.get("sg_approved_panel"):
            # RELEASE THE CLAIM. claim_shot() already flipped this Shot to
            # 'generating' and stamped its tag in sg_gen_log, well before this
            # check runs. Returning without undoing that leaves the shot
            # CLAIMED BY A TOOL THAT WILL NEVER RENDER IT: video_from_panel
            # only ever looks for 'queued', so the shot sits at 'generating'
            # forever and the operator waits on a render that is not running.
            #
            # I INTRODUCED THIS an hour ago while fixing the opposite bug. The
            # old code path refused, which was destructive but at least
            # TERMINAL; deferring without releasing was worse, because a
            # stuck 'generating' looks like work in progress. Measured on
            # SHOW01_A_0040: claimed, deferred, then 22 minutes at
            # 'generating' with ComfyUI's queue empty.
            sg.update("Shot", shot["id"],
                      {"sg_gen_status": "queued", "sg_gen_log": ""})
            log("  %s: video kind with an approved panel, released back to "
                "'queued' for video_from_panel" % shot["code"])
            return
        msg = ("REFUSED: sg_gen_kind=%r requests video, but genvideo_worker.py no longer "
               "generates video for this project -- video_from_panel.py (Phase 6) is the "
               "sole, panel-anchored video generator now. Approve a panel Version for this "
               "shot and let the panel pipeline queue it instead." % kind)
        sg.update("Shot", shot["id"], {"sg_gen_status": "refused", "sg_gen_log": msg})
        log("  %s -> %s" % (shot["code"], msg))
        return

    ledger_path = os.path.join(LEDGER_DIR, "%s.json" % shot["code"])
    led = RL.load(ledger_path)
    led["shot"] = shot["code"]
    n = RL.next_version_number(led)
    seed = int(shot.get("sg_gen_seed") or DEFAULTS["seed"]) + (n - 1) * 100
    prefix = "%s_v%03d" % (shot["code"].lower(), n)

    task = sg.find_one("Task", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                                ["step.Step.short_name", "is", STEP]], ["content"])
    if task is None:
        step = sg.find_one("Step", [["short_name", "is", STEP], ["entity_type", "is", "Shot"]], [])
        task = sg.create("Task", {"project": project,
                                  "entity": {"type": "Shot", "id": shot["id"]},
                                  "step": step, "content": "Comp"})

    # sg_gen_status was already flipped to generating by claim_shot above.
    ref_file, ref_note, anchor_vid = fetch_reference(sg, shot)
    style = sequence_style(sg, shot)
    shot["_fragments"] = asset_fragments(sg, shot)
    ok, result = generate(shot, prefix, seed, ref_file, style)
    if not ok:
        sg.update("Shot", shot["id"], {"sg_gen_status": "error", "sg_gen_log": result})
        log("  %s -> ERROR: %s" % (shot["code"], result.splitlines()[0]))
        return
    media, err = encode(prefix, result["kind"],
                        "%s_%s_gen_v%03d" % (shot["code"], STEP, n))
    if err:
        sg.update("Shot", shot["id"], {"sg_gen_status": "error", "sg_gen_log": err})
        return
    # --- D6_COMPONENTS: computed BEFORE publish() so publish_version() (the
    # one contract, sg_publish.py) can write D6 provenance in the same call
    # as the create+upload, instead of a second PROV.write_provenance pass
    # after the fact. The sha256 covers exactly the substitutions this
    # generation actually used, so two shots with different prompts/params
    # hash differently even off the same template.
    subs = {"prompt": result["prompt"], "negative_prompt": result["negative"],
            "width": result["width"], "height": result["height"],
            "length": result["frames"], "steps": result["steps"], "cfg": result["cfg"]}
    if ref_file:
        subs["start_image"] = ref_file
    wf_hash = PROV.workflow_hash_from_template(result["workflow"], subs)
    char_codes, set_codes, other_codes = PROV.classify_assets(sg, shot)
    action_text = (shot.get("sg_gen_prompt") or "").strip() or "(sg_gen_prompt was empty)"
    if other_codes:
        # Nothing linked goes unrecorded even if its role could not be
        # classified as character or set.
        set_codes = set_codes + ["(unclassified: %s)" % ", ".join(other_codes)]

    v, code = publish(
        sg, project, shot, task, n, media, result, ref_note,
        character=", ".join(char_codes) or "none linked",
        set_=", ".join(set_codes) or "none linked",
        action=action_text,
        camera=("n/a - no camera field on Shot yet (Phase 5 panel stage); "
                "frame %sx%s, %d frame(s)" % (result["width"], result["height"], result["frames"])),
        style=style_component_text(style),
        workflow_hash=wf_hash,
        anchor_version_id=anchor_vid)
    write_provenance(sg, v["id"], shot["id"], result.get("prompt", ""),
                     result.get("negative", ""), result,
                     [{"type": "Asset", "id": a["id"]} for a in (shot.get("assets") or [])],
                     result.get("took"))
    RL.append(led, "version", version_number=n, code=code, sg_id=v["id"],
              seed=result["seed"], reference=ref_note, movie=os.path.basename(media))
    RL.append(led, "status", status="rev", sg_id=v["id"])
    RL.save(ledger_path, led)
    entry = ("v%03d published as Version %d (%s, seed %d, %.1fs)%s"
             % (n, v["id"], result["kind"], result["seed"], result["took"],
                (", ref " + ref_note) if ref_note else ""))
    sg.update("Shot", shot["id"], {"sg_gen_status": "review", "sg_gen_log": entry})
    log("  %s -> %s" % (shot["code"], entry))


# --- TRIAGE_V1 ---------------------------------------------------------------
def _lost_an_alternate(versions, cur_status, approved=("apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra")):
    """True when the latest Version is rejected AND a sibling is approved.

    That combination means the review already CHOSE, so the rejection is
    bookkeeping rather than a request. Kept as a named predicate because the
    caller reads better as a question than as a status expression, and because
    this is the exact condition that queued 13 unasked regenerations."""
    if cur_status not in ("rjct",):
        return False
    return any((v.get("sg_status_list") or v.get("status")) in approved for v in versions)


def triage_note(text):
    """Classify a review note. Imported from note_triage so there is ONE
    classifier: a second copy here would drift from the one that has the
    regression fixtures, and the fixtures are the only reason to trust it.

    An ACTIONABLE note is a revision request; there is no other live
    outcome (post/take/asset-addressable removed 2026-09-08, Geoff's
    instruction -- see note_triage.py)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import note_triage
        cls, _why = note_triage.classify(text or "")
        return cls, note_triage.VERDICT.get(cls, "not reviewed")
    except Exception as exc:
        # A classifier that cannot load must NOT silently drop the note.
        # Fall back to the one live class rather than guess.
        log("  note triage unavailable (%s); treating as a full revision"
            % type(exc).__name__)
        return "prompt-addressable", "revise prompt"


# THE THIRD DOOR INTO THE SAME ROOM, and the one that reaches the GPU.
#
# F444 already records this shape: three code paths each decide separately what
# a Note means, so a rule added to one is silently absent from the others.
# `note_triage.is_actionable()` is the ONE definition; `revision_requested()`
# is a second published door onto the same rule (see its own docstring).
# poll_feedback() below used to read EVERY Note on the tracked Version with no
# status filter, and `RL.note_requests_change()` was a bare keyword match.
#
# F464 (2026-09-08): the first fix here was PARTIAL and was reported closed
# anyway, which was wrong. `_is_pipeline_note()` (deleted below) replicated
# only two of is_actionable()'s three conditions -- is_pipeline_record() and
# the ACTIONABLE_NOTE_STATUSES whitelist -- and left out the third: a note
# linked only to an ALREADY-APPROVED Version with no `rrq` set is a RECORD,
# not an open request, because the operator has not yet said the review
# session is finished (note_triage.py ~line 271). This worker had no Version-
# status map in hand, so it could not apply that branch. `_note_is_actionable`
# below calls the real function instead of re-deriving a piece of its logic.
#
# MEASURED 2026-09-08: the refusal Note that genvideo_service writes
# ("... Fix the shot's inputs, then set the Panel Task back to 'rdy'") returns
# True from note_requests_change(), because it contains the word "fix". Landing
# on a tracked Version, it would have been filed as a revision request and
# queued a full regeneration -- the pipeline asking itself for GPU time to
# implement its own error message. That is why note_requests_change() is not
# consulted at all any more on this path -- see the comment in poll_feedback().
def _note_is_actionable(note, vstatus):
    """-> (bool, why). ONE implementation, note_triage.is_actionable(), called
    exactly as prompt_revision.py's callers call it (invariant 11); a local
    reimplementation of any part of the rule is how the three doors drifted
    apart in the first place (F444, F464). Fails OPEN on import failure: the
    note is treated as a real, open one, because dropping an operator's
    request is worse than acting on our own record."""
    try:
        # tools/ is already on sys.path from this module's import block.
        import note_triage
    except Exception:                                             # noqa: BLE001
        return True, "note_triage unavailable; failing open (fetch-and-act)"
    return note_triage.is_actionable(note, vstatus)


def poll_feedback(sg, project, shot):
    """A Shot in review: ingest Notes and status flips on its newest Version."""
    ledger_path = os.path.join(LEDGER_DIR, "%s.json" % shot["code"])
    led = RL.load(ledger_path)
    versions = [e for e in led["entries"] if e["kind"] == "version"]
    if not versions:
        return
    vid = versions[-1]["sg_id"]
    changed = False
    # Fetched ONCE, ahead of the note loop, and reused both as the
    # is_actionable() Version-status map and for the status-change record
    # below -- the "one batched query rather than per note" F464 asked for.
    cur = sg.find_one("Version", [["id", "is", vid]], ["sg_status_list"])["sg_status_list"]
    vstatus = {vid: cur}
    # `subject`, `sg_status_list` and `note_links` are FETCHED, not optional:
    # note_triage.is_actionable() reads all three (note_links is what lets it
    # see the approved-Version-with-no-rrq branch, F464's actual gap). A guard
    # whose inputs are not queried is a guard that silently stopped guarding.
    notes = sg.find("Note", [["note_links", "in", [{"type": "Version", "id": vid}]]],
                    ["content", "user", "created_at", "subject", "sg_status_list",
                     "note_links"])
    for nt in notes:
        if nt["id"] in led["seen_note_ids"]:
            continue
        live, _why = _note_is_actionable(nt, vstatus)
        if not live:
            # NOT RECORDED AS SEEN EITHER. The ledger is a record of REVIEW
            # traffic; a note that is not actionable (the pipeline's own
            # record, or one still awaiting the operator's rrq) is not review
            # traffic yet, and re-checking it next poll costs nothing.
            continue
        led["seen_note_ids"].append(nt["id"])
        body = nt.get("content") or ""
        # TRIAGE_V1: reaching here already proves is_actionable() is True, and
        # an actionable note IS a revision request, unconditionally (Geoff,
        # 2026-09-08 -- see triage_note()'s docstring). RL.note_requests_change()'s
        # keyword match is no longer consulted on this path: it is the "third
        # and cruder predicate" F464 named, and it now has zero remaining
        # callers project-wide (left defined in review_ledger.py, unused).
        RL.append(led, "note", sg_id=vid, note_id=nt["id"],
                  author=(nt.get("user") or {}).get("name"),
                  content_verbatim=body,
                  requests_change=True)
        changed = True
    stats = [e for e in led["entries"] if e["kind"] == "status" and e.get("sg_id") == vid]
    if not stats or stats[-1]["status"] != cur:
        RL.append(led, "status", status=cur, sg_id=vid)
        changed = True
    if changed:
        RL.save(ledger_path, led)
    if cur in APPROVED:
        sg.update("Shot", shot["id"],
                  {"sg_gen_status": "done",
                   "sg_gen_log": "v%03d approved (%s). Shot closed. Nothing shipped."
                                 % (versions[-1]["version_number"], cur)})
        log("  %s -> DONE (approved)" % shot["code"])
    elif _lost_an_alternate(versions, cur):
        # AN ALTERNATE THAT LOST IS NOT A REVISION REQUEST. Measured 2026-09-06:
        # approving 15 video candidates made housekeeping auto-reject their 24fps
        # siblings ("an alternate; a sibling at this stage is approved"), and this
        # branch then read each rejection as "revision requested" and queued the
        # shot for a full GPU regeneration. 13 shots in one pass, unasked.
        #
        # Worse, it closes a loop: regenerate -> new candidate -> approve ->
        # sibling auto-rejected -> regenerate. The discriminator needs no new
        # field: if this shot ALREADY has an approved Version at this stage, a
        # rejected sibling is a settled decision, not a request for work.
        #
        # This is "rev != revision requested" (Geoff's rule) one layer down: a
        # HOUSEKEEPING rejection is not an OPERATOR rejection.
        log("  %s -> rejected sibling, but an approved Version exists at this "
            "stage: that is an alternate that lost, NOT a revision request. "
            "Leaving it alone." % shot["code"])
    elif cur in NEEDS_REVISION or any(
            e.get("requests_change") for e in led["entries"]
            if e["kind"] == "note" and e.get("sg_id") == vid):
        last_note = next((e["content_verbatim"] for e in reversed(led["entries"])
                          if e["kind"] == "note" and e.get("sg_id") == vid), "")
        # TRIAGE_V1: an actionable note IS a revision request, unconditionally
        # (post/take/asset-addressable removed 2026-09-08, Geoff's direct
        # instruction -- see note_triage.py). There is no cheaper alternative
        # outcome to route to any more, so every live revision goes through
        # the SAME path Phase 7's LLM-proposal loop already gates behind a
        # human (or a deliberate per-shot sg_auto_apply_proposals opt-in)
        # before any GPU time is spent - invariant 7, "no self-approval."
        # sg_gen_status is deliberately left untouched here; the
        # classification is still recorded (so the ShotGrid page filter
        # keeps working), and prompt_revision.service_cycle() (run from
        # genvideo_service.py) is what requeues once a human accepts.
        cls, verdict = triage_note(last_note)
        data = {"sg_note_class": cls, "sg_review_verdict": verdict,
                "sg_gen_log": ("v%03d: %s -> %s. NOT auto-regenerated; routed to the "
                              "Phase 7 LLM proposal loop (prompt_revision.py) instead. "
                              "Note: %r"
                              % (versions[-1]["version_number"], cls, verdict,
                                 last_note[:300]))}
        sg.update("Shot", shot["id"], data)
        log("  %s -> prompt-addressable note routed to the proposal loop (no auto-regen)"
            % shot["code"])


# ---------------------------------------------------------------- main
def self_test():
    """Offline canaries. NO ShotGrid, NO GPU.

    This module had NO self-test until 2026-09-06, which had two consequences,
    both bad and both silent. `--self-test` was an unrecognised argument, so
    asking this tool to test itself ran a LIVE production pass instead. And
    deploy.py discovers preflight targets by looking for the string
    "--self-test", so the worker, a core production module, was never
    preflighted at all."""
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    import inspect as _inspect
    APPROVED_V = [{"sg_status_list": "apr"}, {"sg_status_list": "rjct"}]
    NO_WINNER = [{"sg_status_list": "rjct"}, {"sg_status_list": "rjct"}]

    ck("CANARY: a rejected sibling WITH an approved winner is an alternate that lost",
       _lost_an_alternate(APPROVED_V, "rjct") is True)
    ck("CANARY: a rejection with NO approved sibling is still a real revision request",
       _lost_an_alternate(NO_WINNER, "rjct") is False)
    ck("an approved current status is never an alternate-that-lost",
       _lost_an_alternate(APPROVED_V, "apr") is False)
    ck("a version list with no statuses does not crash the predicate",
       _lost_an_alternate([{}], "rjct") is False)
    ck("the predicate reads either field name ShotGrid may hand back",
       _lost_an_alternate([{"status": "apr"}], "rjct") is True)

    # --- THE THIRD DOOR: a pipeline record must not reach the GPU queue ----
    # The literals are typed out rather than read from note_triage, so this
    # cannot pass by agreeing with the code it is testing.
    _refusal = {"id": 1, "subject": "[auto] Panel composition refused for X",
                "sg_status_list": "urr",
                "content": "panel_compose.py refused ... Fix the shot's inputs.",
                "note_links": [{"type": "Version", "id": 900}]}
    _V = {900: "rev"}   # an ordinary pending-review Version, for most cases below
    ck("CANARY: the refusal Note genvideo_service writes is recognised as a "
       "pipeline record here too", _note_is_actionable(_refusal, _V)[0] is False)
    ck("CANARY: ...and the check is NOT vacuous -- that same body really does "
       "trip the keyword heuristic that would have queued a regeneration",
       RL.note_requests_change(_refusal["content"]) is True)
    ck("CANARY: the 'urr' status ALONE is enough, with no [auto] subject",
       _note_is_actionable({"subject": "operator note", "sg_status_list": "urr",
                            "note_links": [{"type": "Version", "id": 900}]}, _V)[0]
       is False)
    ck("CANARY: the [auto] subject ALONE is enough, at an open status",
       _note_is_actionable({"subject": "[auto] x", "sg_status_list": "opn",
                            "note_links": [{"type": "Version", "id": 900}]}, _V)[0]
       is False)
    ck("an ORDINARY open operator note on a live Version is untouched by all of this",
       _note_is_actionable({"subject": "Geoffrey's Note on X", "sg_status_list": "opn",
                            "content": "please make it darker",
                            "note_links": [{"type": "Version", "id": 900}]}, _V)[0]
       is True)
    ck("a note with NO status and no [auto] prefix, linked to no Version, "
       "is still a real note",
       _note_is_actionable({"content": "please make it darker"}, {})[0] is True)
    # --- F464's actual gap: approved-Version-with-no-rrq is not the same as
    # the pipeline-record / note-status checks above; it needs the Version's
    # OWN status, which is exactly what _is_pipeline_note() could not see.
    _approved = {900: "apr"}
    ck("CANARY: F464 -- a note on an ALREADY-APPROVED Version with no rrq is a "
       "record, not a request (the review session may still be open)",
       _note_is_actionable({"subject": "operator note", "sg_status_list": "opn",
                            "content": "actually make the door bigger",
                            "note_links": [{"type": "Version", "id": 900}]},
                           _approved)[0] is False)
    ck("CANARY: ...and the SAME note becomes actionable the moment the operator "
       "sets the Version to rrq, with no change to the note itself",
       _note_is_actionable({"subject": "operator note", "sg_status_list": "opn",
                            "content": "actually make the door bigger",
                            "note_links": [{"type": "Version", "id": 900}]},
                           {900: "rrq"})[0] is True)
    # note_links appears once already in the query FILTER (["note_links",
    # "in", ...]), so a plain substring check cannot fail if it is dropped
    # from the FIELDS list; count occurrences (filter + fields = 2) instead.
    ck("CANARY: poll_feedback FETCHES subject, sg_status_list AND note_links, or "
       "note_triage.is_actionable() cannot see the approved-Version branch",
       "sg_status_list" in _inspect.getsource(poll_feedback)
       and '"subject"' in _inspect.getsource(poll_feedback)
       and _inspect.getsource(poll_feedback).count('"note_links"') >= 2)
    ck("CANARY: poll_feedback actually CALLS the guard",
       "_note_is_actionable(nt, vstatus)" in _inspect.getsource(poll_feedback))
    # The exact call form, not just the string "note_triage.is_actionable(":
    # the function's own docstring names it too, and a docstring is not proof
    # of what the code does (finishing-standard: a docstring is not a comment).
    ck("CANARY: the guard is not a local reimplementation -- it calls the ONE "
       "definition, note_triage.is_actionable()",
       "return note_triage.is_actionable(note, vstatus)"
       in _inspect.getsource(_note_is_actionable))

    print(chr(10) + ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    if "--self-test" in sys.argv[1:]:
        return self_test()
    dry = "--dry-run" in sys.argv[1:]
    os.makedirs(LEDGER_DIR, exist_ok=True)
    sg = sg_connect()
    project = {"type": "Project", "id": PROJECT_ID}
    fields = ["code", "assets", "sg_sequence", "sg_gen_prompt", "sg_gen_negative_prompt", "sg_gen_kind",
              "sg_gen_size_wxh", "sg_gen_frames", "sg_gen_steps", "sg_gen_cfg",
              "sg_gen_seed", "sg_gen_status",
              # REQUIRED by the video defer-instead-of-refuse guard above. A
              # field absent from this list comes back None, so the guard
              # would read "no approved panel" for every shot and refuse them
              # all, which is the bug it exists to prevent. Do not drop it.
              "sg_approved_panel"]

    in_review = sg.find("Shot", [["project", "is", project],
                                 ["sg_gen_status", "is", "review"]], fields)
    log("shots in review: %d" % len(in_review))
    for s in in_review:
        if dry:
            log("  (dry) would poll feedback on %s" % s["code"])
        else:
            poll_feedback(sg, project, s)

    queued = sg.find("Shot", [["project", "is", project],
                              ["sg_gen_status", "is", "queued"]], fields)
    log("shots queued: %d" % len(queued))
    if dry:
        for s in queued:
            log("  (dry) would generate %s: %r" % (s["code"], (s.get("sg_gen_prompt") or "")[:60]))
        return 0
    if queued:
        if not comfy_start():
            log("cannot start ComfyUI; leaving queue for next pass")
            return 1
        for s in queued:
            process_shot(sg, project, s)
        comfy_stop()
    log("pass complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
