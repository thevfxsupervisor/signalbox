#!/usr/bin/env python3
r"""Automated per-clip and per-episode QC for gen-video episode builds.

Reads the episode's shot CSV (shot code + expected frame count per clip),
finds each clip on disk, and runs every check we know how to automate:

  HARD failures (nonzero exit; the episode must not ship):
    - file missing or 0 bytes
    - ffprobe cannot read the file at all (unplayable)
    - wrong codec / pixel format / frame rate (we require h264 / yuv420p / 24)
    - DECODED frame count differs from the CSV's expected frames by more than
      1 frame. We count frames by actually decoding (-count_frames), not by
      trusting metadata, because a truncated +faststart mp4 keeps its moov
      atom and its metadata still claims the full duration. Only a decode
      exposes the missing tail.
    - decode errors while counting (truncated / corrupt bitstream)

  SOFT warnings (listed in the report, exit stays 0):
    - black interval(s) found by blackdetect
    - frozen/static run found by freezedetect
    - audio expected but no audio stream, or the stream is pure silence
      (astats overall RMS below the silence floor)
    - implausibly low bits-per-pixel (file "exists" but cannot possibly hold
      real picture data at that size)
    - a CSV row whose frame count breaks the Wan constraint (frames-1) % 4 == 0
      (the clip may be fine, but the CSV that drove generation is suspect)

Also writes an SRT captions stub from the CSV: one entry per shot spanning
the shot's slot in the episode timeline, with the shot code + duration as
placeholder text, so editorial has timed caption slots to fill in.

Output: a markdown report per episode next to the clips (or --report PATH),
plus one append-only "qc" entry in the review ledger so QC verdicts live in
the same verbatim history as reviews. Hard failure strings go in verbatim.

The --self-test GENERATES each bad case with ffmpeg in a temp dir (a black
clip, a frozen clip, a silent clip, a truncated file, a wrong-fps clip, an
empty file) and proves every detector actually fires, then proves a clean
moving color-bars clip passes with zero findings. A check that cannot fail
is not a check; every detector here has a canary.

Usage (bare "python" on this box is a broken Store stub; use the venv):
    C:\genvideo\venv\Scripts\python.exe episode_qc.py --clips-dir DIR
        --csv SHOTS.CSV [--episode NAME] [--fps 24] [--expect-audio]
        [--report PATH] [--srt PATH] [--ledger-dir DIR]
    C:\genvideo\venv\Scripts\python.exe episode_qc.py --self-test
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- constants
ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was a sibling-relative guess
# (os.path.dirname(__file__)/../tests) that only holds when tools/ and
# tests/ sit next to each other -- see episode_assemble.py's identical fix
# for the full explanation. tests/ has no deploy seam of its own yet, so
# this points at the one stable location every other module already uses.
sys.path.insert(0, os.path.join(ROOT, "build", "tests"))
import review_ledger as RL                                    # noqa: E402
LEDGER_DIR = os.path.join(ROOT, "output", "ledger")
FFBIN = os.environ.get("FFMPEG_DIR", "")
FFMPEG = os.path.join(FFBIN, "ffmpeg.exe") if FFBIN else "ffmpeg"
FFPROBE = os.path.join(FFBIN, "ffprobe.exe") if FFBIN else "ffprobe"

REQ_CODEC = "h264"        # what genvideo_worker's encode() produces; anything
REQ_PIXFMT = "yuv420p"    # else means a clip came from outside the pipeline
FRAME_TOL = 1             # +-1 frame slack on the decoded count
MIN_BPP = 0.01            # bits per pixel per frame below this cannot be real
                          # picture; even a solid-color x264 encode sits near it
BLACK_MIN_S = 0.4         # blackdetect: shorter than this is a flash, not a hole
BLACK_PIX_TH = 0.10       # luminance threshold for "black" pixels
FREEZE_MIN_S = 0.4        # freezedetect: minimum frozen run to report
FREEZE_NOISE = "-50dB"    # stricter than ffmpeg's -60dB default: a truly static
                          # x264 clip decodes to identical frames (MAFD ~ -inf)
                          # while testsrc-style motion sits around -40dB, so
                          # -50dB separates the two with margin on both sides
SILENCE_DB = -60.0        # astats overall RMS below this = "audio is silence"


def log(msg):
    print("[qc] %s" % msg, flush=True)


def run(cmd):
    """One place for subprocess so every call gets the same text handling.
    errors='replace' because ffmpeg banners are not guaranteed UTF-8."""
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, r.stdout or "", r.stderr or ""


# ---------------------------------------------------------------- detectors
def probe_streams(path):
    """ffprobe metadata, or None when the container is unreadable."""
    rc, out, _ = run([FFPROBE, "-v", "error", "-print_format", "json",
                      "-show_format", "-show_streams", path])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def parse_fps(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        m = re.fullmatch(r"(\d+)/(\d+)", raw)
        if m and int(m.group(2)) != 0:
            return int(m.group(1)) / float(m.group(2))
    return None


def count_decoded_frames(path):
    """Decode the whole video stream and count what actually comes out.
    Returns (frames_or_None, first_error_line). Metadata is NOT trusted:
    a truncated +faststart mp4 still reports full nb_frames in its moov."""
    rc, out, err = run([FFPROBE, "-v", "error", "-count_frames",
                        "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames",
                        "-of", "json", path])
    frames = None
    try:
        frames = int(json.loads(out)["streams"][0]["nb_read_frames"])
    except (ValueError, KeyError, IndexError, TypeError):
        frames = None
    err_line = err.strip().splitlines()[0] if err.strip() else ""
    if rc != 0 and not err_line:
        err_line = "ffprobe exited %d" % rc
    return frames, err_line


def detect_black_and_freeze(path):
    """One decode pass with both detectors chained. Returns
    (black_intervals[(start,dur)], freeze_starts[start], pass_failed).
    pass_failed is surfaced (not swallowed) because a dead detector pass
    returning empty lists would make every clip look clean forever."""
    vf = ("blackdetect=d=%s:pix_th=%s,freezedetect=n=%s:d=%s"
          % (BLACK_MIN_S, BLACK_PIX_TH, FREEZE_NOISE, FREEZE_MIN_S))
    rc, _, err = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
                      "-vf", vf, "-an", "-f", "null", "-"])
    blacks = [(float(a), float(b)) for a, b in
              re.findall(r"black_start:([\d.]+).*?black_duration:([\d.]+)", err)]
    # freeze_end/duration never print when the clip is frozen through EOF,
    # so freeze_start is the only line we can rely on.
    freezes = [float(a) for a in
               re.findall(r"freeze_start:\s*([\d.]+)", err)]
    return blacks, freezes, rc != 0


def audio_rms_db(path):
    """Overall RMS of the first audio stream via astats, or None if it
    cannot be measured. Caller has already confirmed the stream exists."""
    _, _, err = run([FFMPEG, "-hide_banner", "-nostats", "-i", path,
                     "-map", "0:a:0", "-vn",
                     "-af", "astats=measure_perchannel=none",
                     "-f", "null", "-"])
    vals = re.findall(r"RMS level dB:\s*(-?[\d.]+|-inf|inf)", err)
    if not vals:
        return None
    return float(vals[-1])


def bpp_plausible(size_bytes, width, height, frames):
    """Bits per pixel per frame. Below MIN_BPP the file cannot be holding
    real picture data (an encoder writing actual content never gets there)."""
    bpp = (size_bytes * 8.0) / float(max(width, 1) * max(height, 1) * max(frames, 1))
    return bpp >= MIN_BPP, bpp


# ---------------------------------------------------------------- per-clip QC
def qc_clip(path, expected_frames, expect_audio, fps=24.0):
    """Run every check on one clip. Returns {clip, hard[], warn[], info{}}."""
    res = {"clip": path, "hard": [], "warn": [], "info": {}}
    if not os.path.exists(path):
        res["hard"].append("clip file missing: %s" % path)
        return res
    size = os.path.getsize(path)
    res["info"]["size"] = size
    if size == 0:
        res["hard"].append("file is 0 bytes")
        return res

    meta = probe_streams(path)
    if meta is None:
        res["hard"].append("ffprobe cannot read the file (unplayable container)")
        return res
    vstreams = [s for s in meta.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in meta.get("streams", []) if s.get("codec_type") == "audio"]
    if not vstreams:
        res["hard"].append("no video stream")
        return res
    v = vstreams[0]

    codec = v.get("codec_name")
    if codec != REQ_CODEC:
        res["hard"].append("codec is %r, require %r" % (codec, REQ_CODEC))
    pixfmt = v.get("pix_fmt")
    if pixfmt != REQ_PIXFMT:
        res["hard"].append("pixel format is %r, require %r" % (pixfmt, REQ_PIXFMT))
    got_fps = parse_fps(v)
    res["info"]["fps"] = got_fps
    if got_fps is None or abs(got_fps - fps) > 0.01:
        res["hard"].append("fps is %s, require %g" % (got_fps, fps))

    decoded, decode_err = count_decoded_frames(path)
    res["info"]["frames"] = decoded
    if decode_err:
        res["hard"].append("decode errors (truncated or corrupt): %s" % decode_err)
    if decoded is None:
        if not decode_err:
            res["hard"].append("could not count decoded frames")
    else:
        if abs(decoded - int(expected_frames)) > FRAME_TOL:
            res["hard"].append("frame count %d, expected %d (+-%d)"
                               % (decoded, int(expected_frames), FRAME_TOL))
        width, height = int(v.get("width") or 0), int(v.get("height") or 0)
        ok, bpp = bpp_plausible(size, width, height, decoded)
        res["info"]["bpp"] = round(bpp, 4)
        if not ok:
            res["warn"].append("implausible size: %.4f bits/pixel/frame "
                               "(below %.3f), file may be empty picture" % (bpp, MIN_BPP))

    blacks, freezes, det_failed = detect_black_and_freeze(path)
    if det_failed:
        res["warn"].append("black/freeze detection pass exited nonzero; "
                           "its findings for this clip are incomplete")
    for start, dur in blacks:
        res["warn"].append("black interval at %.2fs for %.2fs" % (start, dur))
    for start in freezes:
        res["warn"].append("frozen/static run starting at %.2fs" % start)

    if expect_audio:
        if not astreams:
            res["warn"].append("audio expected but no audio stream present")
        else:
            rms = audio_rms_db(path)
            if rms is None:
                res["warn"].append("audio expected but astats could not measure it")
            elif rms < SILENCE_DB:
                res["warn"].append("audio is silence (overall RMS %.1f dB, floor %.0f dB)"
                                   % (rms, SILENCE_DB))
    return res


# ---------------------------------------------------------------- CSV + SRT
TRUE_WORDS = {"1", "true", "yes", "y"}


def read_shot_csv(path, expect_audio_default):
    """Rows: {shot, frames, audio, file}. Column names are matched loosely so
    the CSV from episode assembly does not have to be regenerated to QC it.
    Returns (rows, csv_warnings)."""
    rows, warns = [], []
    try:
        with open(path, "rb") as fh:
            fh.read().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise SystemExit("CSV %s is not UTF-8 text (%s) - refusing to guess an encoding" % (path, e))
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for i, raw in enumerate(reader, 2):     # 2: header is line 1
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in raw.items()}
            shot = next((row[k] for k in ("shot", "code", "shot_code") if row.get(k)), None)
            # duration_frames is what PILOT-SHOTS.csv actually calls it, and that
            # CSV is the project's shot contract. Omitting it meant QC could not
            # read the only shot list this show has.
            frames_raw = next((row[k] for k in ("frames", "frame_count", "sg_gen_frames",
                                                "duration_frames")
                               if row.get(k)), None)
            if not shot or not frames_raw:
                raise SystemExit("CSV line %d: need shot code and frames columns, got %r"
                                 % (i, raw))
            frames = int(frames_raw)
            if frames < 1:
                raise SystemExit("CSV line %d: frames=%d is not a clip" % (i, frames))
            if (frames - 1) % 4 != 0:
                warns.append("CSV line %d (%s): frames=%d breaks the Wan constraint "
                             "(frames-1) %% 4 == 0; the CSV that drove generation is suspect"
                             % (i, shot, frames))
            audio_raw = next((row[k] for k in ("audio", "expect_audio", "has_audio")
                              if k in row and row[k] != ""), None)
            audio = (audio_raw.lower() in TRUE_WORDS) if audio_raw is not None \
                else expect_audio_default
            file_ = next((row[k] for k in ("file", "clip", "path") if row.get(k)), None)
            rows.append({"shot": shot, "frames": frames, "audio": audio, "file": file_})
    if not rows:
        raise SystemExit("CSV %s has no data rows" % path)
    return rows, warns


def resolve_clip(clips_dir, row):
    """Explicit file column wins; else newest published version by the
    <SHOT>_CMP_<desc>_v### naming; else <shot>.mp4 (which may not exist,
    and qc_clip reports that as a hard failure)."""
    if row["file"]:
        p = row["file"]
        return p if os.path.isabs(p) else os.path.join(clips_dir, p)
    hits = glob.glob(os.path.join(clips_dir, "%s_CMP_*_v*.mp4" % row["shot"]))
    if hits:
        def vnum(p):
            m = re.search(r"_v(\d+)\.mp4$", p, re.IGNORECASE)
            return int(m.group(1)) if m else -1
        return max(hits, key=vnum)
    return os.path.join(clips_dir, "%s.mp4" % row["shot"])


def srt_timestamp(seconds):
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def build_srt(rows, fps):
    """Captions stub: one entry per shot spanning its slot on the episode
    timeline. Frame counts are accumulated as integers so a long episode
    cannot drift from float rounding. Placeholder text = shot code + duration."""
    lines, cum = [], 0
    for i, r in enumerate(rows, 1):
        start = cum / fps
        cum += r["frames"]
        end = cum / fps
        lines += [str(i),
                  "%s --> %s" % (srt_timestamp(start), srt_timestamp(end)),
                  "%s (%d frames, %.3fs)" % (r["shot"], r["frames"], r["frames"] / fps),
                  ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- report
def write_report(path, episode, clips_dir, csv_path, fps, results, csv_warns):
    hard = [(r["shot"], h) for r in results for h in r["qc"]["hard"]]
    warns = [(r["shot"], w) for r in results for w in r["qc"]["warn"]]
    warns += [("(csv)", w) for w in csv_warns]
    verdict = "FAIL" if hard else "PASS"
    out = ["# Episode QC report: %s" % episode, "",
           "- Clips dir: `%s`" % clips_dir,
           "- Shot CSV: `%s`" % csv_path,
           "- Required: %s / %s / %g fps, decoded frames within +-%d of CSV"
           % (REQ_CODEC, REQ_PIXFMT, fps, FRAME_TOL),
           "- Clips checked: %d" % len(results), "",
           "## Verdict: %s (%d hard failure(s), %d warning(s))"
           % (verdict, len(hard), len(warns)), ""]
    out.append("## Hard failures")
    out += ["- **%s**: %s" % (s, h) for s, h in hard] or ["- none"]
    out += ["", "## Warnings (soft)"]
    out += ["- %s: %s" % (s, w) for s, w in warns] or ["- none"]
    out += ["", "## Per-clip results", "",
            "| shot | clip | frames (got/expected) | hard | warn |",
            "|---|---|---|---|---|"]
    for r in results:
        q = r["qc"]
        out.append("| %s | %s | %s/%d | %d | %d |"
                   % (r["shot"], os.path.basename(q["clip"]),
                      q["info"].get("frames"), r["frames"],
                      len(q["hard"]), len(q["warn"])))
    out.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return verdict, hard, warns


# ---------------------------------------------------------------- episode run
def run_qc(clips_dir, csv_path, episode, fps, expect_audio_default,
           report_path, srt_path, ledger_dir):
    if shutil.which(FFMPEG) is None or shutil.which(FFPROBE) is None:
        raise SystemExit("ffmpeg/ffprobe not found (set FFMPEG_DIR or put them on PATH)")
    rows, csv_warns = read_shot_csv(csv_path, expect_audio_default)
    results = []
    for r in rows:
        clip = resolve_clip(clips_dir, r)
        log("checking %s -> %s" % (r["shot"], os.path.basename(clip)))
        r["qc"] = qc_clip(clip, r["frames"], r["audio"], fps=fps)
        results.append(r)

    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write(build_srt(rows, fps))
    verdict, hard, warns = write_report(report_path, episode, clips_dir,
                                        csv_path, fps, results, csv_warns)

    # QC verdicts belong in the same append-only verbatim history as reviews:
    # when a shipped episode turns out broken, the exact failure strings from
    # the run that let it through (or blocked it) must still exist.
    led_path = os.path.join(ledger_dir, "qc_%s.json" % episode)
    led = RL.load(led_path)
    if not led.get("shot"):
        led["shot"] = "EPISODE:%s" % episode
    RL.append(led, "qc", episode=episode, verdict=verdict,
              clips_checked=len(results), report=report_path,
              hard_failures_verbatim=["%s: %s" % (s, h) for s, h in hard],
              warnings_verbatim=["%s: %s" % (s, w) for s, w in warns])
    RL.save(led_path, led)

    log("report: %s" % report_path)
    log("captions stub: %s" % srt_path)
    log("verdict: %s (%d hard, %d warnings)" % (verdict, len(hard), len(warns)))
    return 1 if hard else 0


# ---------------------------------------------------------------- self-test
def self_test():
    """Canary pattern: generate each bad case for real and prove the detector
    fires, then prove a clean clip passes. Runs entirely in a temp dir with a
    temp ledger; touches no GPU, no network, no ShotGrid."""
    import tempfile
    if shutil.which(FFMPEG) is None:
        print("self-test FAILED: ffmpeg not found (set FFMPEG_DIR or put it on PATH)")
        return 1
    tmp = tempfile.mkdtemp(prefix="episode_qc_selftest_")
    N = 49                       # Wan-valid: (49-1) % 4 == 0

    def gen(name, inputs, extra, vcodec="libx264", pixfmt="yuv420p"):
        dst = os.path.join(tmp, name)
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        for i in inputs:
            cmd += ["-f", "lavfi", "-i", i]
        codec_args = ["-c:v", vcodec, "-pix_fmt", pixfmt]
        if vcodec == "libx264":
            codec_args += ["-crf", "18"]   # crf is an x264 option; other
        cmd += extra + codec_args + [      # encoders would reject/ignore it
            "-frames:v", str(N), "-movflags", "+faststart", dst]
        rc, _, err = run(cmd)
        assert rc == 0 and os.path.getsize(dst) > 0, \
            "fixture %s failed to encode: %s" % (name, err[-300:])
        return dst

    try:
        clean = gen("clean.mp4",
                    ["testsrc=duration=3:size=320x240:rate=24",
                     "sine=frequency=440:sample_rate=48000:duration=3"],
                    ["-c:a", "aac", "-shortest"])
        black = gen("black.mp4", ["color=c=black:s=320x240:r=24:d=3"], [])
        frozen = gen("frozen.mp4", ["color=c=red:s=320x240:r=24:d=3"], [])
        noaudio = gen("noaudio.mp4", ["testsrc=duration=3:size=320x240:rate=24"], [])
        silent = gen("silent.mp4",
                     ["testsrc=duration=3:size=320x240:rate=24",
                      "anullsrc=r=48000:cl=stereo:d=3"],
                     ["-c:a", "aac", "-shortest"])
        wrongfps = gen("wrongfps.mp4", ["testsrc=duration=3:size=320x240:rate=30"], [])
        wrongcodec = gen("wrongcodec.mp4",
                         ["testsrc=duration=3:size=320x240:rate=24"], [],
                         vcodec="mpeg4")
        wrongpix = gen("wrongpix.mp4",
                       ["testsrc=duration=3:size=320x240:rate=24"], [],
                       pixfmt="yuv444p")
        # Audio-only mp4: proves the no-video-stream hard check can fire.
        audioonly = os.path.join(tmp, "audioonly.mp4")
        rc, _, err = run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          "sine=frequency=440:sample_rate=48000:duration=1",
                          "-c:a", "aac", audioonly])
        assert rc == 0, "audio-only fixture failed: %s" % err[-300:]
        # Garbage bytes named .mp4: proves the unreadable-container check fires.
        garbage = os.path.join(tmp, "garbage.mp4")
        with open(garbage, "wb") as fh:
            fh.write(b"this is not an mp4, it is a canary " * 64)
        truncated = os.path.join(tmp, "truncated.mp4")
        with open(clean, "rb") as fh:
            blob = fh.read()
        with open(truncated, "wb") as fh:
            fh.write(blob[:int(len(blob) * 0.6)])   # keep moov (+faststart puts
        empty = os.path.join(tmp, "empty.mp4")      # it first), lose the tail
        open(empty, "wb").close()

        # 1. Clean moving color bars with real audio: zero findings.
        r = qc_clip(clean, N, expect_audio=True)
        assert not r["hard"] and not r["warn"], \
            "clean clip should pass, got %r %r" % (r["hard"], r["warn"])
        print("self-test: clean color-bars clip passes with zero findings")

        # 2. Same clean clip against a wrong expected frame count: fires.
        r = qc_clip(clean, 100, expect_audio=True)
        assert any("frame count" in h for h in r["hard"]), r
        print("self-test: canary duration mismatch -> hard failure fires")

        # 3. Black clip: blackdetect fires.
        r = qc_clip(black, N, expect_audio=False)
        assert any("black interval" in w for w in r["warn"]), r
        print("self-test: canary black clip -> blackdetect fires")

        # 4. Frozen NON-black clip: freezedetect fires, blackdetect does not
        #    (proves the two detectors are actually distinct).
        r = qc_clip(frozen, N, expect_audio=False)
        assert any("frozen" in w for w in r["warn"]), r
        assert not any("black interval" in w for w in r["warn"]), r
        print("self-test: canary frozen clip -> freezedetect fires, blackdetect stays quiet")

        # 5. Audio expected but absent / pure silence.
        r = qc_clip(noaudio, N, expect_audio=True)
        assert any("no audio stream" in w for w in r["warn"]), r
        print("self-test: canary missing audio -> audio-presence check fires")
        r = qc_clip(silent, N, expect_audio=True)
        assert any("silence" in w for w in r["warn"]), r
        print("self-test: canary silent audio -> astats silence check fires")

        # 6. Truncated file: metadata still claims N frames (faststart moov
        #    survived), only decoding exposes it.
        r = qc_clip(truncated, N, expect_audio=False)
        assert r["hard"], "truncated clip must hard-fail, got %r" % r
        print("self-test: canary truncated file -> hard failure fires (%s)"
              % r["hard"][0])

        # 7. Wrong frame rate and empty file.
        r = qc_clip(wrongfps, N, expect_audio=False)
        assert any("fps" in h for h in r["hard"]), r
        print("self-test: canary 30fps clip -> fps check fires")
        r = qc_clip(empty, N, expect_audio=False)
        assert any("0 bytes" in h for h in r["hard"]), r
        print("self-test: canary empty file -> size check fires")

        # 7b. Wrong codec / wrong pixel format / no video stream / unreadable
        #     container: the remaining hard checks, each proven able to fire.
        r = qc_clip(wrongcodec, N, expect_audio=False)
        assert any("codec is" in h for h in r["hard"]), r
        print("self-test: canary mpeg4 clip -> codec check fires")
        r = qc_clip(wrongpix, N, expect_audio=False)
        assert any("pixel format is" in h for h in r["hard"]), r
        print("self-test: canary yuv444p clip -> pixel-format check fires")
        r = qc_clip(audioonly, N, expect_audio=False)
        assert any("no video stream" in h for h in r["hard"]), r
        print("self-test: canary audio-only file -> no-video-stream check fires")
        r = qc_clip(garbage, N, expect_audio=False)
        assert any("unplayable" in h for h in r["hard"]), r
        print("self-test: canary garbage bytes -> unreadable-container check fires")

        # 8. Bits-per-pixel plausibility, both directions.
        ok, _ = bpp_plausible(100, 320, 240, 49)        # 100 bytes of "video"
        assert not ok, "tiny file must be implausible"
        ok, _ = bpp_plausible(60000, 320, 240, 49)
        assert ok, "a real-sized file must be plausible"
        print("self-test: canary bits/pixel sanity fires low, passes normal")

        # 9. CSV Wan-constraint warning and SRT stub content.
        csv_bad = os.path.join(tmp, "bad.csv")
        with open(csv_bad, "w", encoding="utf-8", newline="") as fh:
            fh.write("shot,frames,audio,file\nEP01_SH010,50,no,clean.mp4\n")
        _, warns = read_shot_csv(csv_bad, False)
        assert any("Wan constraint" in w for w in warns), warns
        print("self-test: canary frames=50 in CSV -> Wan constraint warning fires")

        srt = build_srt([{"shot": "EP01_SH010", "frames": 49},
                         {"shot": "EP01_SH020", "frames": 25}], 24.0)
        assert "1\n00:00:00,000 --> 00:00:02,042\nEP01_SH010 (49 frames, 2.042s)" in srt
        assert "2\n00:00:02,042 --> 00:00:03,083\nEP01_SH020 (25 frames, 1.042s)" in srt
        print("self-test: SRT stub carries shot codes on the accumulated timeline")

        # 10. End-to-end episode run: one pass, one fail, one missing clip.
        #     Exit must be nonzero, report must say FAIL, ledger must record it.
        csv_ep = os.path.join(tmp, "ep_test.csv")
        with open(csv_ep, "w", encoding="utf-8", newline="") as fh:
            fh.write("shot,frames,audio,file\n"
                     "EP01_SH010,49,yes,clean.mp4\n"
                     "EP01_SH020,49,no,black.mp4\n"
                     "EP01_SH030,49,no,truncated.mp4\n"
                     "EP01_SH040,49,no,\n")           # no file, no match: missing
        report = os.path.join(tmp, "ep_test_qc_report.md")
        srt_path = os.path.join(tmp, "ep_test.srt")
        code = run_qc(tmp, csv_ep, "ep_test", 24.0, False, report, srt_path, tmp)
        assert code == 1, "hard failures must exit nonzero, got %d" % code
        with open(report, "r", encoding="utf-8") as fh:
            body = fh.read()
        assert "Verdict: FAIL" in body and "EP01_SH030" in body and "EP01_SH040" in body
        assert "black interval" in body        # soft warning listed separately
        assert os.path.exists(srt_path)
        led = RL.load(os.path.join(tmp, "qc_ep_test.json"))
        qcs = [e for e in led["entries"] if e["kind"] == "qc"]
        assert qcs and qcs[-1]["verdict"] == "FAIL" and qcs[-1]["hard_failures_verbatim"]
        print("self-test: end-to-end broken episode -> exit 1, FAIL report, ledger entry")

        # 11. End-to-end all-clean episode: exit 0, PASS.
        csv_ok = os.path.join(tmp, "ep_ok.csv")
        with open(csv_ok, "w", encoding="utf-8", newline="") as fh:
            fh.write("shot,frames,audio,file\nEP01_SH010,49,yes,clean.mp4\n")
        code = run_qc(tmp, csv_ok, "ep_ok", 24.0, False,
                      os.path.join(tmp, "ep_ok_qc_report.md"),
                      os.path.join(tmp, "ep_ok.srt"), tmp)
        assert code == 0, "clean episode must exit 0, got %d" % code
        print("self-test: end-to-end clean episode -> exit 0, PASS report")

        print("self-test PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser(description="Per-clip and per-episode QC.")
    p.add_argument("--clips-dir", help="directory holding the episode's clips")
    p.add_argument("--csv", help="shot CSV: shot code + expected frames per clip")
    p.add_argument("--episode", help="episode name (default: CSV basename)")
    p.add_argument("--fps", type=float, default=24.0)
    p.add_argument("--expect-audio", action="store_true",
                   help="expect audio on every clip unless the CSV says otherwise")
    p.add_argument("--report", help="markdown report path")
    p.add_argument("--srt", help="captions stub path")
    p.add_argument("--ledger-dir", default=LEDGER_DIR)
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()
    if a.self_test:
        return self_test()
    if not a.clips_dir or not a.csv:
        p.error("--clips-dir and --csv are required (or use --self-test)")
    episode = a.episode or os.path.splitext(os.path.basename(a.csv))[0]
    report = a.report or os.path.join(a.clips_dir, "%s_qc_report.md" % episode)
    srt = a.srt or os.path.join(a.clips_dir, "%s_captions_stub.srt" % episode)
    return run_qc(a.clips_dir, a.csv, episode, a.fps, a.expect_audio,
                  report, srt, a.ledger_dir)


if __name__ == "__main__":
    sys.exit(main())
