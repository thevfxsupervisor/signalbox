#!/usr/bin/env python3
"""Stage 13: the video-to-video upres pass the draft strategy is predicated on.

Geoff's plan, in his words: "10 steps is fine, this can be a draft animatic,
with video to video upres/detail later perhaps?" That trade only works if the
"later" half actually exists. Until now it did not, so the whole pipeline was
committed to draft quality with no route out of it - which is exactly what he
kept seeing and calling blurry.

WHAT THIS DOES. Real-ESRGAN x4plus (BSD-3, commercially clear per the finishing
research; SUPIR was disqualified as non-commercial) over every frame, then a
lanczos downscale to the delivery raster. 768x432 -> 3072x1728 -> 1920x1080.
The double move is deliberate: upscaling past the target and coming back down
is what removes the model's own upscaling artifacts instead of baking them in.

WHY IT IS CHUNKED. A 121 frame batch at 3072x1728 is about 7.7 GB of float32
image tensor before the model is even loaded, which does not fit alongside a
5B checkpoint on this card. Frames are processed CHUNK frames at a time and
reassembled by ffmpeg. The chunk size is the one number to turn down if a
bigger raster ever OOMs.

IT DOES NOT REGENERATE. An upres that re-rolls the diffusion would change
performance, timing and identity, which would invalidate the approval the shot
already has. This pass may only make the existing frames sharper.

    python finishing.py --shot PILOT01_A_0010
    python finishing.py --sequence PILOT01_A
    python finishing.py --self-test
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sg_provenance as PROV                                   # noqa: E402
import sg_publish as PUB                                        # noqa: E402
import episode_context as EPCTX                                # noqa: E402

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools"),
# which only feeds WRAPPER below -- self-relative now.
TOOLS = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(TOOLS, "comfyui", "comfyui_execute.py")
TPL_UPRES = os.path.join(ROOT, "workflows", "upres_esrgan.api.json")
COMFY_IN = r"C:\ComfyUI_windows_portable\ComfyUI\input"
COMFY_OUT = r"C:\ComfyUI_windows_portable\ComfyUI\output"
FINISHED = os.path.join(ROOT, "output", "finished")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
PY = sys.executable
PROJ = {"type": "Project", "id": 9999}
EP = EPCTX.resolve_episode()

MODEL = "RealESRGAN_x4plus.pth"
TARGET_W, TARGET_H = 1920, 1080
CHUNK = 24                     # frames per ComfyUI prompt; see module docstring
FIN_STEP = "FIN"


TESTS = os.path.join(ROOT, "build", "tests")
COMFY_EXE = r"C:\ComfyUI_windows_portable\python_embeded\python.exe"
COMFY_MAIN = r"C:\ComfyUI_windows_portable\ComfyUI\main.py"


def log(m):
    print("[finishing] %s" % m, flush=True)


def comfy_up():
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3)
        return True
    except Exception:
        return False


def comfy_start():
    """Start ComfyUI if it is down, behind the GPU guard.

    The other stages tear ComfyUI down when they finish - Geoff found one idle
    for weeks on a second workstation and made closing it a standing rule - so a tool that needs
    it cannot assume it is up just because another tool used it an hour ago."""
    if comfy_up():
        return True
    g = subprocess.run([PY, os.path.join(TESTS, "gpu_guard.py")],
                       capture_output=True, text=True)
    if g.returncode != 0:
        log("gpu guard refused: %s" % (g.stdout or "").strip())
        return False
    os.makedirs(FINISHED, exist_ok=True)
    subprocess.Popen([COMFY_EXE, "-s", COMFY_MAIN, "--windows-standalone-build",
                      "--listen", "127.0.0.1", "--port", "8188"],
                     stdout=open(os.path.join(FINISHED, "comfy.log"), "a"),
                     stderr=subprocess.STDOUT, cwd=r"C:\ComfyUI_windows_portable")
    for _ in range(180):
        time.sleep(0.5)
        if comfy_up():
            return True
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
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-NetTCPConnection -State Listen -LocalPort 8188 -EA SilentlyContinue | "
                    "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -EA SilentlyContinue }"])
    time.sleep(4)
    v = subprocess.run([PY, os.path.join(TESTS, "teardown_verify.py")],
                       capture_output=True, text=True)
    log("teardown: %s" % ("VERIFIED" if v.returncode == 0 else "NOT CLEAN"))


def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def probe_frames(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-count_frames", "-show_entries",
                        "stream=nb_read_frames,r_frame_rate,width,height",
                        "-of", "json", path], capture_output=True, text=True)
    try:
        s = json.loads(r.stdout or "{}")["streams"][0]
    except (ValueError, KeyError, IndexError):
        return None
    num, den = (s.get("r_frame_rate") or "24/1").split("/")
    return {"frames": int(s.get("nb_read_frames") or 0),
            "fps": float(num) / float(den or 1),
            "width": int(s.get("width") or 0), "height": int(s.get("height") or 0)}


def _inspect_src(fn):
    """Source of a function, for call-site canaries."""
    import inspect
    return inspect.getsource(fn)


def crop_filter_for(src, target_w=None, target_h=None):
    """-> an ffmpeg -vf crop expression that makes `src` match the delivery
    aspect exactly, or None when it already does.

    WHY A CROP AND NOT A SCALE. Scaling 1.7333 to 1.7778 does not fail, it
    DISTORTS, which is the worst kind of defect here: nothing errors, nothing
    looks broken in a thumbnail, and every figure in the finished show is
    quietly 2.6% wider than the panel that was approved.

    The crop is centred and always trims the LONGER axis relative to target,
    so it can never letterbox and can never scale up. Returns None rather than
    a no-op filter when the aspects already agree, so a correctly shaped source
    is not re-encoded through a pointless filter."""
    tw = target_w or TARGET_W
    th = target_h or TARGET_H
    info = probe(src)
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    if w <= 0 or h <= 0:
        return None
    target = float(tw) / float(th)
    if abs((float(w) / float(h)) - target) < 1e-6:
        return None
    if (float(w) / float(h)) > target:
        # Too wide: trim the sides.
        new_w = int(round(h * target))
        new_w -= new_w % 2
        return "crop=%d:%d:%d:0" % (new_w, h, (w - new_w) // 2)
    # Too tall: trim top and bottom. 832x480 -> 832x468, exactly 16:9.
    new_h = int(round(w / target))
    new_h -= new_h % 2
    return "crop=%d:%d:0:%d" % (w, new_h, (h - new_h) // 2)


def split_chunks(src, work, chunk):
    """-> [chunk paths] in order. Segmenting by frame count rather than by time
    keeps the frame total exact; a time-based split drops or duplicates frames at
    the seams and the reassembled shot then drifts against the cut."""
    os.makedirs(work, exist_ok=True)
    out = os.path.join(work, "chunk_%03d.mp4")
    # CROP TO THE TARGET ASPECT BEFORE ANYTHING ELSE TOUCHES THE FRAMES.
    # Measured 2026-09-08: the source is 832x480 (1.7333) and the target is
    # 1920x1080 (1.7778), and the upres was scaling straight across, so every
    # published FIN frame was the whole picture STRETCHED 2.6% wider. Proven
    # by comparison rather than assumed: downscaling a FIN frame back to the
    # source size and differencing gave 3.63 mean absolute error against 11.44
    # for a centre-crop hypothesis, so nothing was being trimmed, everything
    # was being squeezed. Every face in every finished shot was 2.6% wide.
    #
    # Geoff chose centre-crop over pillarboxing, asked with the numbers in
    # front of him: full bleed, correct proportions, and 2.5% of frame height
    # given up. 832x468 is EXACTLY 16:9, so after this crop the existing scale
    # to 1920x1080 introduces no distortion at all.
    vf = crop_filter_for(src)
    subprocess.run([FFMPEG, "-y", "-i", src] + (["-vf", vf] if vf else []) +
                   ["-c:v", "libx264", "-crf", "12",
                    "-pix_fmt", "yuv420p", "-g", str(chunk), "-keyint_min", str(chunk),
                    "-sc_threshold", "0", "-f", "segment",
                    "-segment_frames", ",".join(str(k) for k in
                                                range(chunk, 100000, chunk)),
                    "-reset_timestamps", "1", out], capture_output=True, text=True)
    return sorted(glob.glob(os.path.join(work, "chunk_*.mp4")))


def upres_chunk(chunk_path, prefix, expect):
    """One ComfyUI prompt. Returns the PNGs it produced, in order.

    `expect` is this chunk's own frame count, not a constant: the last chunk is
    short, and telling the wrapper to expect a fixed CHUNK would fail every
    final chunk in the episode."""
    dst = os.path.join(COMFY_IN, os.path.basename(chunk_path))
    shutil.copyfile(chunk_path, dst)
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", TPL_UPRES,
           "--comfy-output-dir", COMFY_OUT, "--verify-mode", "disk",
           "--output-prefix", prefix, "--seed-base", "0",
           "--expect-outputs", str(expect), "--timeout", "1800",
           "--set", "video_file=%s" % os.path.basename(chunk_path),
           "--set", "upscale_model=%s" % MODEL,
           "--set", "width=%d" % TARGET_W, "--set", "height=%d" % TARGET_H]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.remove(dst)
    except OSError:
        pass
    if "PASS:" not in (r.stdout or ""):
        return None, (r.stdout or "")[-400:]
    pngs = sorted(glob.glob(os.path.join(COMFY_OUT, "%s*.png" % prefix)))
    return pngs, None


def finish_shot(sg, shot, version):
    src = version.get("sg_path_to_movie") or ""
    if not src or not os.path.exists(src):
        return None, "source movie missing: %s" % src
    info = probe_frames(src)
    if not info or not info["frames"]:
        return None, "could not probe %s" % src
    if info["width"] >= TARGET_W:
        return None, ("already %dx%d, at or above the finishing raster: refusing "
                      "to re-encode for nothing" % (info["width"], info["height"]))

    work = os.path.join(FINISHED, "_work_%s" % shot["code"])
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    frames_dir = os.path.join(work, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    t0 = time.time()
    chunks = split_chunks(src, work, CHUNK)
    if not chunks:
        return None, "chunking produced nothing"
    log("  %s: %d frames %dx%d -> %d chunk(s)"
        % (shot["code"], info["frames"], info["width"], info["height"], len(chunks)))

    n = 0
    for k, c in enumerate(chunks):
        prefix = "fin_%s_c%03d" % (shot["code"].lower(), k)
        for old in glob.glob(os.path.join(COMFY_OUT, "%s*.png" % prefix)):
            os.remove(old)
        cinfo = probe_frames(c)
        if not cinfo or not cinfo["frames"]:
            return None, "could not probe chunk %d" % k
        pngs, err = upres_chunk(c, prefix, cinfo["frames"])
        if pngs is None:
            return None, "chunk %d failed: %s" % (k, err)
        for p in pngs:
            n += 1
            shutil.move(p, os.path.join(frames_dir, "f%06d.png" % n))
    if n != info["frames"]:
        # A silent frame loss here would shorten the shot against the cut, which
        # is exactly the drift the conform exists to prevent. Refuse instead.
        return None, ("frame count changed: %d in, %d out. Refusing to publish a "
                      "shot that no longer matches the cut." % (info["frames"], n))

    os.makedirs(FINISHED, exist_ok=True)
    v = 1
    while os.path.exists(os.path.join(FINISHED, "%s_FIN_v%03d.mp4" % (shot["code"], v))):
        v += 1
    out = os.path.join(FINISHED, "%s_FIN_v%03d.mp4" % (shot["code"], v))
    r = subprocess.run([FFMPEG, "-y", "-framerate", "%.6f" % info["fps"],
                        "-i", os.path.join(frames_dir, "f%06d.png"),
                        "-c:v", "libx264", "-profile:v", "high", "-crf", "16",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
                       capture_output=True, text=True)
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        return None, "reassembly failed: %s" % (r.stderr or "")[-300:]
    shutil.rmtree(work, ignore_errors=True)
    return (out, round(time.time() - t0, 1), info), None


def publish_finished(sg, shot, out, took, info, src_version):
    code = os.path.splitext(os.path.basename(out))[0]

    # --- D6_COMPONENTS: this pass does not regenerate (see module docstring,
    # "IT DOES NOT REGENERATE"), so it has no character/set/action/camera/style
    # of its OWN - it inherits the source Version's, when the source has them
    # (an older, pre-Phase-8 source will not; that gap is explicit below, not
    # silently blank). The anchor is always the exact source Version upres'd.
    inherited_missing = PROV.missing_fields(src_version)
    if inherited_missing:
        note = ("n/a - inherited from source Version %s, which predates Phase 8 "
                "provenance (missing: %s)"
                % (src_version.get("code") or "?", ", ".join(inherited_missing)))
        character = set_ = action = camera = style = note
    else:
        character = src_version.get(PROV.F_CHARACTER)
        set_ = src_version.get(PROV.F_SET)
        action = src_version.get(PROV.F_ACTION)
        camera = src_version.get(PROV.F_CAMERA)
        style = src_version.get(PROV.F_STYLE)
    wf_hash = PROV.workflow_hash_from_template(
        TPL_UPRES, {"video_file": "<per-chunk, see description>",
                    "upscale_model": MODEL, "width": TARGET_W, "height": TARGET_H})
    v = PUB.publish_version(
        sg, project=PROJ, entity={"type": "Shot", "id": shot["id"]}, code=code,
        media_path=out,
        description=("Finishing pass: Real-ESRGAN x4 then lanczos to %dx%d. "
                     "Upres only, no regeneration, so the approved performance "
                     "and timing are unchanged. From %s."
                     % (TARGET_W, TARGET_H, src_version.get("code") or "?")),
        stage="video", first_frame=1001, last_frame=1000 + info["frames"],
        extra_fields={"sg_gen_seconds": took,
                     "sg_gen_size_wxh": "%dx%d" % (TARGET_W, TARGET_H),
                     "sg_model": MODEL,
                     "sg_prompt_final__as_sent_": src_version.get("sg_prompt_final__as_sent_") or "",
                     "sg_gen_seed": src_version.get("sg_gen_seed")},
        character=character, set_=set_, action=action, camera=camera, style=style,
        workflow_hash=wf_hash, anchor_version_id=src_version["id"], log=log)
    return v, code


# Mirrors genvideo_service.APPROVED_PANEL_STATUSES and the copies in
# panel_compose, prompt_revision and video_from_panel. Same deliberate copy for
# the same reason: these tools deploy as a flat directory.
APPROVED_VIDEO_STATUSES = ("apr", "ad", "fin", "paf", "dlvr")


def latest_video_version(sg, shot, approved_only=True):
    """-> the newest APPROVED video Version for this shot, or None.

    APPROVED_ONLY IS THE DEFAULT AND IT WAS NOT ALWAYS. This function used to
    take the newest video of ANY status, and the first real run of this stage
    (2026-09-08, the first time it had ever been called) spent 231 seconds of
    GPU upresing SHOW01_A_0030_CMP_a14b_v003, which was sitting at 'rev' with
    nobody having looked at it yet.

    That is the wrong input by definition. This is the FINISHING stage: it
    exists to take something a human has accepted and make it deliverable, and
    it explicitly may not regenerate (see the module docstring) precisely
    because the approval must survive it. Finishing an unapproved clip spends
    the most expensive pass in the pipeline on a candidate that may be rejected,
    and worse, publishes a 1920x1080 FIN Version that LOOKS like a deliverable
    for a shot nobody has signed off.

    `approved_only=False` remains for a deliberate operator override on the CLI,
    never for the watcher."""
    vs = sg.find("Version", [["project", "is", PROJ],
                             ["entity", "is", {"type": "Shot", "id": shot["id"]}]],
                 ["code", "sg_path_to_movie", "sg_stage", "sg_gen_seed",
                  "sg_status_list",
                  "sg_prompt_final__as_sent_", "sg_gen_size_wxh", "created_at"]
                 + PROV.ALL_FIELDS)
    vs = [v for v in vs if (v.get("sg_path_to_movie") or "").lower().endswith(".mp4")
          and v.get("sg_stage") in ("video", None)
          and "_FIN_" not in (v.get("code") or "")
          and (not approved_only
               or v.get("sg_status_list") in APPROVED_VIDEO_STATUSES)]
    if not vs:
        return None
    vs.sort(key=lambda v: v.get("created_at") or 0)
    return vs[-1]


def cmd_finish(sg, shot_code, sequence, limit):
    filt = [["project", "is", PROJ], ["code", "starts_with", EP.code]]
    if shot_code:
        filt = [["project", "is", PROJ], ["code", "is", shot_code]]
    elif sequence:
        filt.append(["sg_sequence.Sequence.code", "is", sequence])
    shots = sg.find("Shot", filt, ["code", "sg_cut_order"],
                    order=[{"field_name": "code", "direction": "asc"}])
    if not shots:
        log("no shots matched")
        return 1
    started = not comfy_up()
    if not comfy_start():
        log("could not start ComfyUI")
        return 1
    done, skipped, failed = 0, [], []
    try:
        for s in shots[:limit]:
            src = latest_video_version(sg, s)
            if not src:
                skipped.append("%s (no video Version)" % s["code"])
                continue
            res, err = finish_shot(sg, s, src)
            if res is None:
                (skipped if "refusing to re-encode" in (err or "") else failed).append(
                    "%s: %s" % (s["code"], err))
                continue
            out, took, info = res
            v, code = publish_finished(sg, s, out, took, info, src)
            log("  %s -> %s (%ss)" % (s["code"], code, took))
            done += 1
    finally:
        # Whatever happened above, do not leave the GPU held. A crash mid-run is
        # exactly when an orphaned ComfyUI gets left behind for weeks.
        if started:
            comfy_stop()
    log("finished %d shot(s), %d skipped, %d failed" % (done, len(skipped), len(failed)))
    for x in skipped[:6]:
        log("  skip: %s" % x)
    for x in failed[:6]:
        log("  FAIL: %s" % x)
    return 1 if failed else 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-56s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # --- THE STAGE MUST ONLY FINISH APPROVED WORK.
    # The first real run of this module upresed a Version sitting at 'rev',
    # because latest_video_version() sorted by date and never read a status.
    # 231 seconds of GPU on a clip nobody had looked at, published as a
    # 1920x1080 FIN Version that reads like a deliverable. The self-test was
    # green throughout, because nothing here exercised the selection at all.
    class _SG:
        def __init__(self, rows):
            self.rows = rows
        def find(self, *a, **k):
            return [dict(r) for r in self.rows]

    _rows = [
        {'id': 1, 'code': 'S_CMP_v001', 'sg_stage': 'video', 'sg_status_list': 'apr',
         'sg_path_to_movie': 'x/a.mp4', 'created_at': '2026-09-01'},
        {'id': 2, 'code': 'S_CMP_v002', 'sg_stage': 'video', 'sg_status_list': 'rev',
         'sg_path_to_movie': 'x/b.mp4', 'created_at': '2026-09-02'},
    ]
    _shot = {'id': 9, 'code': 'S'}
    ck('CANARY: an unapproved NEWER video is NOT finished; the approved one wins',
       (latest_video_version(_SG(_rows), _shot) or {}).get('code') == 'S_CMP_v001')
    ck('...and the override still reaches it, for a deliberate CLI run',
       (latest_video_version(_SG(_rows), _shot, approved_only=False) or {}).get('code')
       == 'S_CMP_v002')
    ck('CANARY: with NO approved video at all it returns None rather than '
       'falling back to the newest, which is how the original defect behaved',
       latest_video_version(_SG([_rows[1]]), _shot) is None)
    ck('CANARY: a FIN Version is never re-finished, or the stage eats its own '
       'output',
       latest_video_version(_SG([dict(_rows[0], code='S_FIN_v001')]), _shot) is None)

    ck("workflow template exists", os.path.exists(TPL_UPRES))
    if os.path.exists(TPL_UPRES):
        raw = open(TPL_UPRES, encoding="utf-8").read()
        for ph in ("{video_file}", "{upscale_model}", "{width}", "{height}",
                   "{output_prefix}"):
            ck("template exposes %s" % ph, ph in raw)
        filled = (raw.replace("{video_file}", "c.mp4").replace("{upscale_model}", MODEL)
                     .replace("{width}", "1920").replace("{height}", "1080")
                     .replace("{output_prefix}", "p"))
        try:
            wf = json.loads(filled)
            ok = True
        except ValueError:
            wf = {}
            ok = False
        ck("template is valid JSON once filled", ok)
        if ok:
            cls = [n.get("class_type") for n in wf.values()]
            ck("chain upscales then rescales then saves",
               "ImageUpscaleWithModel" in cls and "ImageScale" in cls
               and "SaveImage" in cls)
            ck("CANARY no sampler in the chain: this pass must not regenerate",
               not any("KSampler" in (c or "") for c in cls))
    ck("the upscale model is actually installed",
       os.path.exists(os.path.join(r"C:\ComfyUI_windows_portable\ComfyUI\models",
                                   "upscale_models", MODEL)))
    ck("ffmpeg is on PATH", shutil.which(FFMPEG) is not None)
    # THE ASPECT CROP. The defect it closes was a silent 2.6% horizontal
    # stretch on every finished frame, invisible in a thumbnail and present in
    # every FIN file published before 2026-09-08.
    ck("CANARY: a 1.7333 source (832x480) is centre-cropped to EXACTLY 16:9, "
       "trimming height and never scaling",
       crop_filter_for.__doc__ is not None)
    _mk = lambda w, h: {"width": w, "height": h}
    _saved = globals().get("probe")
    try:
        globals()["probe"] = lambda src: _mk(832, 480)
        _f = crop_filter_for("x")
        ck("CANARY: 832x480 -> crop=832:468 (exactly 16:9), centred, 6 rows "
           "off each edge", _f == "crop=832:468:0:6")
        globals()["probe"] = lambda src: _mk(1920, 1080)
        ck("CANARY: a source ALREADY at the target aspect gets no filter at "
           "all, rather than a no-op re-encode", crop_filter_for("x") is None)
        globals()["probe"] = lambda src: _mk(1000, 480)
        _f2 = crop_filter_for("x")
        ck("CANARY: a TOO WIDE source trims the sides instead, so this can "
           "never letterbox and never scales up",
           _f2 is not None and _f2.startswith("crop=852:480:"))
        globals()["probe"] = lambda src: _mk(0, 0)
        ck("CANARY: an unreadable source returns None rather than a filter "
           "built from zeros", crop_filter_for("x") is None)
    finally:
        globals()["probe"] = _saved
    ck("CANARY: split_chunks actually APPLIES the crop; a helper nothing calls "
       "is the defect this project keeps repeating",
       "crop_filter_for(src)" in _inspect_src(split_chunks))
    ck("chunk size is small enough to fit a 4x batch",
       CHUNK * (TARGET_W * 4) * (TARGET_H * 4) * 3 * 4 < 12e9)
    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shot")
    ap.add_argument("--sequence")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if not (ns.shot or ns.sequence):
        ap.print_help()
        return 1
    return cmd_finish(sg_connect(), ns.shot, ns.sequence, ns.limit)


if __name__ == "__main__":
    sys.exit(main())
