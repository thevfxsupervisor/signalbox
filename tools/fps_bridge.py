#!/usr/bin/env python3
"""Phase 6: bridge A14B's native 16fps/81-frame clip to the pipeline's 24fps timeline.

THE PROBLEM (MASTER-PLAN-V2.md section 3, "Frame-rate fact"): A14B (i2v) is
16 fps native, 81 frames = 5.06s. Every downstream stage this pipeline already
has -- conform.py's EDL/OTIO export, captions_sg.py's caption timing,
episode_qc.py's per-clip check -- assumes 24 fps and reads its frame-count
target from Shot.sg_gen_frames (defaulting to 121 across all three tools, per
grep). A raw A14B render published as-is would silently play 1.5x too fast
against those timelines and would fail episode_qc.py's own fps==24 (+-0.01)
and frame-count (+-1) checks outright -- so this bridge is not optional
polish, it is what makes an A14B render a legal Version at all.

THE CHOICE: RIFE vs ffmpeg. MASTER-PLAN-V2 prefers RIFE (MIT-licensed,
"already licence-cleared in docs/FINISHING-OPTIONS.md" per the phase brief).
CHECKED LIVE on this box before writing a line of this module:
  - docs/FINISHING-OPTIONS.md does not exist anywhere in this tree (grepped;
    the only two hits for "RIFE" in the whole repo are wan-prompt.md, unrelated,
    and MASTER-PLAN-V2.md's own mention). The licence claim could not be
    re-verified from a primary source on this box and is not assumed true.
  - No RIFE custom node is installed: ComfyUI's custom_nodes/ holds exactly
    one node, ComfyUI-GGUF (Phase 1's). No frame-interpolation node exists.
  - models/frame_interpolation/ exists but contains only ComfyUI's own
    placeholder file ("put_frame_interpolation_models_here", 0 bytes) -- no
    RIFE weights were ever downloaded.
  - Installing + proving a new custom node means running it, which means
    ComfyUI on the GPU -- exactly what this session does not have (the seat
    is held by the character-sheet rebuild) and is forbidden from starting.
So RIFE is NOT available on this box right now, full stop, and this module
implements the PLAN'S OWN FALLBACK instead: ffmpeg retime. That fallback is
not "just relabel the fps" (see WHY NOT A NAIVE RETIME below) -- it uses
ffmpeg's own motion-compensated interpolation (minterpolate, block-based
motion estimation + occlusion-aware blending), which is a real interpolator,
just not a learned one. Quality note, stated plainly: minterpolate can produce
warping/ghosting on fast, complex motion where a learned model (RIFE) would
hold up better; at the Lightning 4-step, mostly-locked-off camera motion this
pipeline is producing (see the wedge prompt in a14b_wedge.py), the difference
is expected to be minor, but it has NOT been judged on a real render yet
because no real A14B render exists in this session (no GPU). Swapping this
module's INTERNAL command for a RIFE call later requires zero change to
bridge_fps()'s signature or to any caller -- that seam is deliberate.

WHY NOT A NAIVE RETIME (setpts alone, or -r 24 alone). Two failure modes,
both real:
  - `-r 24` on a 16fps source without a motion filter just changes ffmpeg's
    frame-duplication/drop decision at demux time -- some frames repeat,
    motion still reads as 16fps-choppy, and the exact output frame count is
    at ffmpeg's mercy, not this module's.
  - `setpts` alone changes playback SPEED (a 5.06s clip becomes some other
    duration) without adding any frames at all -- it cannot, by itself, turn
    81 frames into ~121.
This module instead: (1) time-stretches the clip's PTS by whatever factor is
needed so its duration exactly equals target_frames/24s (a deliberate,
declared speed change -- see WHAT THIS DOES TO MOTION below), (2) resamples
that stretched timeline through minterpolate at exactly 24fps, synthesizing
new in-between frames rather than duplicating, (3) pads with cloned tail
frames if minterpolate's own rounding came up short, then hard-trims to
EXACTLY target_frames. Step 3 is what makes the frame count exact-by-
construction rather than "usually within the +-1 the acceptance test asks
for" -- the test only requires +-1; this module does not rely on luck to
clear it.

WHAT THIS DOES TO MOTION, stated for whoever reviews a real render later:
  - When target_frames ~= source_duration * 24 (the common case: A14B's
    native 81 frames/16fps = 5.0625s, and this pipeline's default
    sg_gen_frames is 121, i.e. 121/24 = 5.0417s -- a 0.36% stretch), the
    speed change is imperceptible; the visible effect is purely "more frames
    per second of the same performance," which is what frame interpolation
    is supposed to look like.
  - When a shot's sg_gen_frames diverges meaningfully from ~121 (a
    deliberately shorter or longer cut of the same 81-frame generation),
    this module WILL change apparent playback speed to hit that duration
    exactly, because the source only contains 5.06s of unique motion. That
    is a real, named trade-off, not a bug: the alternative (declining to hit
    the target frame count) breaks every downstream frame-count check in the
    pipeline. If a genuinely different duration is wanted, the correct fix is
    a longer/shorter A14B render (a different `length` on WanImageToVideo),
    not a bigger retime here -- this module does not decide that; it is only
    ever handed whatever target_frames the caller (video_from_panel.py, from
    the shot's own sg_gen_frames) asks for.

    python fps_bridge.py --in raw16.mp4 --out bridged24.mp4 --target-frames 121
    python fps_bridge.py --self-test        offline synthetic clip, no GPU
"""
import argparse
import json
import os
import subprocess
import sys

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

import timeline as TL          # invariant 11: ONE timeline fps
DST_FPS = TL.FPS               # was a hardcoded 24.0
FRAME_TOL = 1     # matches episode_qc.py's FRAME_TOL -- same contract, same number


class BridgeError(Exception):
    pass


def _run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout or "", r.stderr or ""


def probe(path):
    """-> (frames, fps, duration_s) via ffprobe, decode-counted (not container
    metadata -- same discipline as episode_qc.py's count_decoded_frames, for
    the same reason: a container's own frame-count header can lie)."""
    rc, out, err = _run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                         "-count_frames",
                         "-show_entries",
                         "stream=nb_read_frames,r_frame_rate,avg_frame_rate,duration",
                         "-of", "json", path])
    if rc != 0:
        raise BridgeError("ffprobe failed on %s: %s" % (path, err.strip()[:300]))
    try:
        s = json.loads(out)["streams"][0]
    except (ValueError, KeyError, IndexError):
        raise BridgeError("ffprobe returned no usable stream info for %s" % path)
    frames = int(s.get("nb_read_frames") or 0)
    raw_fps = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
    num, den = raw_fps.split("/")
    fps = (float(num) / float(den)) if float(den) else 0.0
    dur = float(s.get("duration") or (frames / fps if fps else 0.0))
    if frames <= 0 or fps <= 0:
        raise BridgeError("could not determine frames/fps for %s (frames=%r fps=%r)"
                          % (path, frames, fps))
    return frames, fps, dur


def native_conform(src, dst, target_frames, log=print):
    """Cut a native clip to length WITHOUT touching motion speed.

    This is what replaces bridge_fps() on the MVP timeline (Geoff, 2026-09-06:
    deliver at 16fps). The generator gives 81 frames at 16fps; a shot that
    wants fewer is a TRIM, which is what editing does. No setpts, no
    minterpolate, no invented frames, and motion is exactly what the model
    produced.

    A shot that wants MORE than the generation has is REFUSED BY NAME rather
    than slowed to fit. That refusal is the point of this function: the old
    path silently time-stretched, so SHOW01_A_0090 was playing 1.59x slow and
    _0030 1.66x fast, and nothing in the record said so. A shot that needs to
    be longer is a generation question (ask for more frames), not a retime.
    """
    if target_frames < 1:
        raise BridgeError("target_frames must be >= 1, got %r" % target_frames)
    if not os.path.exists(src):
        raise BridgeError("source clip does not exist: %s" % src)
    src_frames, src_fps, _ = probe(src)
    if target_frames > src_frames:
        raise BridgeError(
            "REFUSING to stretch: this shot wants %d frames and the generation "
            "has %d. Slowing the motion to fit is what the old 24fps bridge did "
            "silently. Either shorten the shot to %.2fs, or generate more frames "
            "(GENVIDEO_NATIVE_FRAMES), which is untested above %d."
            % (target_frames, src_frames, src_frames / float(src_fps or 16.0),
               src_frames))

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    if os.path.exists(dst):
        raise BridgeError("refusing to overwrite existing conformed output: %s" % dst)

    log("[fps_bridge] native conform: %d frames -> %d (trim, no retime, no "
        "interpolation)" % (src_frames, target_frames))
    vf = "trim=start_frame=0:end_frame=%d,setpts=PTS-STARTPTS" % target_frames
    cmd = [FFMPEG, "-y", "-i", src, "-filter:v", vf, "-an",
           "-fps_mode", "cfr", "-r", str(TL.FPS), "-frames:v", str(target_frames),
           "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-crf", "18", "-movflags", "+faststart", dst]
    rc, out, err = _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise BridgeError("ffmpeg conform failed (rc=%d): %s" % (rc, err.strip()[-500:]))
    got, got_fps, _ = probe(dst)
    if got != target_frames:
        raise BridgeError("conform produced %d frames, expected %d" % (got, target_frames))
    log("[fps_bridge] OK: %s -> %s (%d frames @ %gfps, motion unchanged)"
        % (src, dst, got, TL.FPS))
    return dst


def bridge_fps(src, dst, target_frames, dst_fps=DST_FPS, log=print):
    """16fps (or whatever the source actually is) -> dst_fps, EXACTLY
    target_frames frames, by construction (see module docstring, step 3).
    Raises BridgeError loudly on any ffmpeg/ffprobe failure or a source that
    cannot be read -- this must never publish a video that silently has the
    wrong frame count."""
    if target_frames < 1:
        raise BridgeError("target_frames must be >= 1, got %r" % target_frames)
    if not os.path.exists(src):
        raise BridgeError("source clip does not exist: %s" % src)

    src_frames, src_fps, src_dur = probe(src)
    if src_dur <= 0:
        src_dur = src_frames / src_fps
    target_dur = target_frames / dst_fps
    stretch = target_dur / src_dur   # setpts multiplier; see WHAT THIS DOES TO MOTION

    log("[fps_bridge] src: %d frames @ %.4gfps (%.4fs) -> target: %d frames @ %gfps (%.4fs); "
        "stretch factor %.4f"
        % (src_frames, src_fps, src_dur, target_frames, dst_fps, target_dur, stretch))

    # INTERPOLATION MODE. Default 'dup', which DUPLICATES the nearest real
    # frame rather than synthesising a blended one.
    #
    # WHY THE DEFAULT CHANGED, 2026-09-06. The old chain ran
    # mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1, real motion-compensated
    # interpolation, and this module's own docstring already warned it "can
    # produce artifacts". Geoff watched the cut and called it blurry, so I went
    # and looked: on SHOW01_A_0040, edge energy across six consecutive frames
    # ran [1048, 905, 876, 1030, 1021, 894], roughly every third frame softer,
    # which is exactly the 16->24 cadence of one synthetic frame per two real
    # ones. On the frames themselves the smear lands on the HANDS and the rim
    # of the pasta bowl: ghosted fingers, doubled bowl edge. That is the
    # moving part, which is precisely where the eye goes.
    #
    # 'dup' cannot blur, because it never invents a pixel. The cost is judder
    # instead of smooth motion, and drawn animation carries that far better
    # than it carries melted hands: animation on twos is the normal look.
    #
    # NOT the final answer. A learned interpolator (RIFE, or FILM which
    # docs/FINISHING-OPTIONS.md records as Apache-2.0) would give smooth AND
    # sharp. ComfyUI's models/frame_interpolation/ is still empty here, so
    # that is a roadmap item, not a Tuesday one.
    #
    # GENVIDEO_FPS_INTERP=mci restores the old behaviour without a code edit.
    mode = os.environ.get("GENVIDEO_FPS_INTERP", "dup").strip().lower()
    if mode == "mci":
        mi = "minterpolate=fps=%g:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1" % dst_fps
    elif mode == "blend":
        mi = "minterpolate=fps=%g:mi_mode=blend" % dst_fps
    else:
        mode = "dup"
        mi = "minterpolate=fps=%g:mi_mode=dup" % dst_fps
    log("[fps_bridge] interpolation mode: %s%s"
        % (mode, "" if mode == "dup" else " (GENVIDEO_FPS_INTERP override)"))

    vf = (
        "setpts=%.10f*PTS,"
        "%s,"
        "tpad=stop_mode=clone:stop_duration=5,"
        "trim=start_frame=0:end_frame=%d,"
        "setpts=PTS-STARTPTS"
        % (stretch, mi, target_frames)
    )
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    if os.path.exists(dst):
        raise BridgeError("refusing to overwrite existing bridged output: %s" % dst)
    cmd = [FFMPEG, "-y", "-i", src, "-filter:v", vf, "-an",
          "-fps_mode", "cfr", "-r", str(dst_fps), "-frames:v", str(target_frames),
          "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
          "-crf", "18", "-movflags", "+faststart", dst]
    rc, out, err = _run(cmd, timeout=900)
    if rc != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise BridgeError("ffmpeg bridge failed (rc=%d): %s" % (rc, err.strip()[-500:]))

    out_frames, out_fps, _ = probe(dst)
    if abs(out_frames - target_frames) > FRAME_TOL:
        raise BridgeError(
            "bridge produced %d frames, wanted %d (+-%d) -- the tpad/trim step should have "
            "made this exact; this is a bug, not a rounding slip"
            % (out_frames, target_frames, FRAME_TOL))
    if abs(out_fps - dst_fps) > 0.05:
        raise BridgeError("bridged output reports %.4gfps, wanted %g" % (out_fps, dst_fps))
    log("[fps_bridge] OK: %s -> %s (%d frames @ %.4gfps)" % (src, dst, out_frames, out_fps))
    return {"frames": out_frames, "fps": out_fps, "target_frames": target_frames,
           "stretch": stretch, "src_frames": src_frames, "src_fps": src_fps}


# ---------------------------------------------------------------------- self-test
def _make_synthetic_16fps_81frame(path):
    """A real, decodable 16fps/81-frame clip built with testsrc -- no GPU, no
    A14B render needed. This is the stub the module docstring and the phase
    report both call out by name: labelled synthetic, stands in for a real
    A14B render only for the arithmetic/mechanism proof."""
    if os.path.exists(path):
        os.remove(path)
    dur = 81 / 16.0
    cmd = [FFMPEG, "-y", "-f", "lavfi",
          "-i", "testsrc=size=320x180:rate=16:duration=%.10f" % dur,
          "-frames:v", "81", "-c:v", "libx264", "-pix_fmt", "yuv420p", path]
    rc, out, err = _run(cmd, timeout=60)
    if rc != 0 or not os.path.exists(path):
        raise BridgeError("could not build synthetic fixture: %s" % err[-300:])


def self_test():
    import tempfile
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="fps_bridge_selftest_")
    src = os.path.join(tmp, "synthetic_a14b_16fps_81f.mp4")
    log = []
    try:
        _make_synthetic_16fps_81frame(src)
        sf, sfps, sdur = probe(src)
        ck("synthetic fixture is labelled 16fps/81 frames, and IS decodably 16fps/81 frames "
           "(sf=%d sfps=%.4g)" % (sf, sfps), sf == 81 and abs(sfps - 16.0) < 0.05)

        # --- the headline arithmetic test: default sg_gen_frames=121 ---
        dst121 = os.path.join(tmp, "bridged_121.mp4")
        r = bridge_fps(src, dst121, target_frames=121, log=log.append)
        ck("CANARY-PROVING PASS: bridged clip is exactly 121 frames "
           "(the default Shot.sg_gen_frames across conform.py/captions_sg.py/episode_qc.py)",
           r["frames"] == 121)
        ck("bridged clip reports exactly the TIMELINE fps (was hardcoded 24.0)",
           abs(r["fps"] - TL.FPS) < 0.01)
        f2, fps2, _ = probe(dst121)
        ck("independent re-probe on disk agrees (121 frames, timeline fps)",
           f2 == 121 and abs(fps2 - TL.FPS) < 0.01)
        ck("+-1 acceptance contract: |121 - Shot.sg_gen_frames(121)| <= 1 (it is 0)",
           abs(r["frames"] - 121) <= FRAME_TOL)

        # --- a second, different target: proves this isn't hardcoded to 121 ---
        dst150 = os.path.join(tmp, "bridged_150.mp4")
        r2 = bridge_fps(src, dst150, target_frames=150, log=log.append)
        ck("a DIFFERENT target_frames (150, a longer cut) is also hit exactly -- not "
           "special-cased on 121", r2["frames"] == 150)
        ck("the 150-frame case reports a larger stretch factor than the 121-frame case "
           "(named trade-off in the docstring: bigger target = more retime, honestly)",
           r2["stretch"] > r["stretch"])

        # --- CANARY: refuse to silently overwrite ---
        try:
            bridge_fps(src, dst121, target_frames=121, log=log.append)
            ck("CANARY: refuses to overwrite an existing bridged output", False)
        except BridgeError as exc:
            ck("CANARY: refuses to overwrite an existing bridged output",
               "overwrite" in str(exc))

        # --- native_conform: TRIM to length, never retime. The MVP path.
        nc_out = os.path.join(tmp, "conformed.mp4")
        native_conform(src, nc_out, 40, log=lambda m: None)
        nf, nfps, _ = probe(nc_out)
        ck("native_conform trims to the exact frame count", nf == 40)
        ck("and stamps the timeline fps, not a hardcoded one",
           abs(nfps - TL.FPS) < 0.01)

        refused = ""
        try:
            native_conform(src, os.path.join(tmp, "toolong.mp4"), 99999,
                           log=lambda m: None)
        except BridgeError as exc:
            refused = str(exc)
        ck("CANARY: asking for MORE frames than exist is REFUSED, not slowed",
           "REFUSING to stretch" in refused)
        ck("the refusal names what to do instead of just failing",
           "shorten the shot" in refused or "generate more frames" in refused)

        # --- CANARY: a source that does not exist must fail loudly, not silently ---
        try:
            bridge_fps(os.path.join(tmp, "nope.mp4"), os.path.join(tmp, "x.mp4"), 121)
            ck("CANARY: missing source file refused loudly", False)
        except BridgeError as exc:
            ck("CANARY: missing source file refused loudly", "does not exist" in str(exc))

        # --- CANARY: the exactness check itself can fail (prove the check is not decoration) ---
        # Feed it an ALREADY-24fps clip and ask for a target the trim step cannot reach
        # because target_frames=0 is rejected up front -- prove that refusal instead,
        # since bridge_fps's own tpad/trim make an in-range miss unreachable by design.
        try:
            bridge_fps(src, os.path.join(tmp, "y.mp4"), target_frames=0, log=log.append)
            ck("CANARY: target_frames=0 refused", False)
        except BridgeError as exc:
            ck("CANARY: target_frames=0 refused", "target_frames" in str(exc))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src")
    ap.add_argument("--out", dest="dst")
    ap.add_argument("--target-frames", type=int)
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if not (ns.src and ns.dst and ns.target_frames):
        sys.exit("need --in, --out, --target-frames (or --self-test)")
    try:
        bridge_fps(ns.src, ns.dst, ns.target_frames)
    except BridgeError as exc:
        sys.exit("REFUSED: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
