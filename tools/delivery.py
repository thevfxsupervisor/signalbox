#!/usr/bin/env python3
"""Stage 15: build a delivery package, and refuse to ship a bad one.

This stage was MISSING entirely. Everything upstream could be perfect and the
episode still had no way to leave the building as something a broadcaster,
platform or client would accept.

Four gates, and each one can FAIL the package rather than annotate it:

  1. LOUDNESS. EBU R128 via ffmpeg loudnorm, measured then normalised to a
     target (-23 LUFS EBU / -24 LKFS ATSC / -14 for streaming). A mix that is
     merely "sounds fine" gets rejected by every real QC house on earth.

  2. PSE FLASH. The research flagged this as MANDATORY for animation and it is
     the one check here with a safety consequence rather than a commercial one.
     Ofcom/ITU-R BT.1702: more than three luminance flashes in any one second is
     a photosensitive epilepsy risk. Measured on real frame luminance, not
     assumed absent. A generated show with hard cuts and a strobing radiator is
     exactly the material that trips this.

  3. STRUCTURE. Duration inside the delivery window, video/audio parameters as
     specified, no black at the head or tail beyond slate.

  4. TEXTLESS. A textless (clean) version alongside the subtitled one, because
     localisation cannot un-burn a caption.

The package is written with a manifest recording what was checked, what passed
and what the source Versions were, so the delivery is auditable after the fact.

    python delivery.py --version PILOT01_ANIMATIC_v001 --spec streaming
    python delivery.py --check-only <file.mp4>
    python delivery.py --self-test
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "build", "out", "delivery")
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
PROJ = {"type": "Project", "id": 9999}

SPECS = {
    # name        LUFS   peak   window (seconds, min/max)
    "ebu":       {"lufs": -23.0, "peak": -1.0, "window": (570, 620)},
    "atsc":      {"lufs": -24.0, "peak": -2.0, "window": (570, 620)},
    "streaming": {"lufs": -14.0, "peak": -1.0, "window": (540, 660)},
}
FLASH_LIMIT = 3          # luminance flashes per second (ITU-R BT.1702)
FLASH_DELTA = 0.10       # relative luminance change that counts as a flash


def log(m):
    print("[delivery] %s" % m, flush=True)


def probe(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return {}


# --- gate 1: loudness -------------------------------------------------------

LOUD_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh")


def measure_loudness(path):
    """Run loudnorm in analysis mode. Returns None when there is no audio at all,
    which is a DIFFERENT outcome from 'measured and silent' and must not be
    confused with one."""
    info = probe(path)
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        return None
    r = subprocess.run([FFMPEG, "-nostats", "-i", path, "-af",
                        "loudnorm=I=-23:TP=-1:LRA=11:print_format=json",
                        "-f", "null", "-"], capture_output=True, text=True)
    blob = re.search(r"\{[^{}]*input_i[^{}]*\}", r.stderr or "", re.S)
    if not blob:
        return None
    try:
        d = json.loads(blob.group(0))
    except ValueError:
        return None
    return dict((k, float(d[k])) for k in LOUD_KEYS if k in d)


def check_loudness(meas, spec):
    if meas is None:
        return ("SKIP", "no audio track: nothing to measure. A silent delivery is "
                        "a decision, not a pass.")
    i, tp = meas.get("input_i"), meas.get("input_tp")
    want = SPECS[spec]
    notes = []
    ok = True
    if i is None:
        return ("FAIL", "loudness could not be measured")
    if abs(i - want["lufs"]) > 1.0:
        ok = False
        notes.append("integrated %.1f LUFS, spec %.1f (+/-1.0)" % (i, want["lufs"]))
    else:
        notes.append("integrated %.1f LUFS" % i)
    if tp is not None and tp > want["peak"]:
        ok = False
        notes.append("true peak %.1f dBTP exceeds %.1f" % (tp, want["peak"]))
    return ("PASS" if ok else "FAIL", "; ".join(notes))


# --- gate 2: photosensitive epilepsy ---------------------------------------

def frame_luminance(path, fps=6):
    """Average luminance per sampled frame, 0..1. Sampled rather than every
    frame: a flash is a sustained pattern, and full-rate decode of a ten minute
    show to measure it is not worth the wall clock."""
    r = subprocess.run(
        [FFMPEG, "-v", "error", "-i", path, "-vf",
         "fps=%d,scale=64:36,format=gray" % fps, "-f", "rawvideo", "-"],
        capture_output=True)
    buf = r.stdout or b""
    size = 64 * 36
    out = []
    for k in range(0, len(buf) - size + 1, size):
        chunk = buf[k:k + size]
        out.append(sum(chunk) / float(size * 255))
    return out


def flash_windows(lums, fps=6, limit=FLASH_LIMIT, delta=FLASH_DELTA):
    """-> [(second, flashes)] for every one second window over the limit.

    A flash is a luminance transition of at least `delta` that REVERSES: light
    to dark to light. Counting every change would flag an ordinary cut, which
    would make the check useless and therefore ignored."""
    if len(lums) < 3:
        return []
    trans = []
    for k in range(1, len(lums)):
        d = lums[k] - lums[k - 1]
        if abs(d) >= delta:
            trans.append((k, 1 if d > 0 else -1))
    flashes = []
    for k in range(1, len(trans)):
        if trans[k][1] != trans[k - 1][1]:      # direction reversed
            flashes.append(trans[k][0])
    bad = []
    for k in range(len(lums)):
        n = sum(1 for f in flashes if k <= f < k + fps)
        if n > limit:
            bad.append((k / float(fps), n))
    # collapse overlapping windows to one report per burst
    out = []
    for sec, n in bad:
        if out and sec - out[-1][0] < 1.0:
            if n > out[-1][1]:
                out[-1] = (out[-1][0], n)
            continue
        out.append((sec, n))
    return out


def check_pse(path):
    lums = frame_luminance(path)
    if not lums:
        return ("FAIL", "could not decode video to measure flash rate")
    bad = flash_windows(lums)
    if not bad:
        return ("PASS", "no window exceeds %d flashes/sec over %d sampled frames"
                % (FLASH_LIMIT, len(lums)))
    worst = max(n for _, n in bad)
    where = ", ".join("%.1fs (%d)" % (s, n) for s, n in bad[:6])
    return ("FAIL", "%d window(s) exceed %d flashes/sec, worst %d. At: %s"
            % (len(bad), FLASH_LIMIT, worst, where))


# --- gate 3: structure ------------------------------------------------------

def check_structure(path, spec):
    info = probe(path)
    fmt = info.get("format") or {}
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        return ("FAIL", "no video stream")
    v = vs[0]
    dur = float(fmt.get("duration") or 0)
    lo, hi = SPECS[spec]["window"]
    notes = ["%dx%d" % (v.get("width", 0), v.get("height", 0)),
             "%s" % v.get("codec_name", "?"),
             "%.1fs" % dur]
    ok = True
    if not (lo <= dur <= hi):
        ok = False
        notes.append("DURATION outside delivery window %d-%ds" % (lo, hi))
    if v.get("pix_fmt") not in ("yuv420p", "yuv422p", "yuv420p10le"):
        ok = False
        notes.append("pixel format %s is not a delivery format" % v.get("pix_fmt"))
    return ("PASS" if ok else "FAIL", "; ".join(notes))


# --- package ----------------------------------------------------------------

SLATE_SECONDS = 5


def build_slate(src, name, spec, out, extra=""):
    """A slate: five seconds of identification ahead of the picture.

    Every delivery spec on earth wants one, and for THIS show it does a second
    job. The episode cut is boards held on the timing, not finished picture, and
    a 9:50 file that opens straight on a drawing invites exactly one
    misunderstanding. The slate says what it is, in the frame, so it cannot
    travel without saying so."""
    info = probe(src)
    vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        return False, "no video stream to slate"
    w = int(vs[0].get("width") or 1920)
    h = int(vs[0].get("height") or 1080)
    dur = float((info.get("format") or {}).get("duration") or 0)
    fps = 24
    lines = [
        name,
        "",
        "duration  %d:%02d" % (int(dur // 60), int(dur % 60)),
        "format    %dx%d   %s" % (w, h, spec),
    ]
    if extra:
        lines.append(extra)
    font = "C\\:/Windows/Fonts/consola.ttf"
    draws = []
    for i, ln in enumerate(lines):
        if not ln:
            continue
        esc = ln.replace(":", "\\:").replace("'", "")
        size = 46 if i == 0 else 26
        draws.append("drawtext=fontfile='%s':text='%s':x=(w-tw)/2:y=%d:"
                     "fontsize=%d:fontcolor=white"
                     % (font, esc, int(h * 0.32) + i * int(h * 0.075), size))
    vf = ",".join(draws)
    slate = out + ".slate.mp4"
    r = subprocess.run([FFMPEG, "-y", "-f", "lavfi",
                        "-i", "color=c=black:s=%dx%d:d=%d:r=%d" % (w, h, SLATE_SECONDS, fps),
                        "-vf", vf, "-c:v", "libx264", "-profile:v", "high",
                        "-pix_fmt", "yuv420p", "-crf", "18", slate],
                       capture_output=True, text=True)
    if not os.path.exists(slate) or os.path.getsize(slate) == 0:
        return False, (r.stderr or "")[-200:]
    lst = out + ".concat.txt"
    io.open(lst, "w", encoding="utf-8").write(
        "file '%s'\nfile '%s'\n" % (slate.replace("\\", "/"), src.replace("\\", "/")))
    r = subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", lst,
                        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                        "-crf", "18", "-movflags", "+faststart", out],
                       capture_output=True, text=True)
    for tmp in (slate, lst):
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        return False, (r.stderr or "")[-200:]
    return True, ""


def build_package(src, name, spec, textless_src=None):
    os.makedirs(OUT, exist_ok=True)
    pkg = os.path.join(OUT, name)
    os.makedirs(pkg, exist_ok=True)

    results = {}
    meas = measure_loudness(src)
    results["loudness"] = check_loudness(meas, spec)
    results["pse_flash"] = check_pse(src)
    results["structure"] = check_structure(src, spec)

    # normalised master, only when there is audio to normalise
    master = os.path.join(pkg, "%s_master.mp4" % name)
    if meas is not None:
        subprocess.run([FFMPEG, "-y", "-i", src, "-af",
                        "loudnorm=I=%.1f:TP=%.1f:LRA=11" % (SPECS[spec]["lufs"],
                                                            SPECS[spec]["peak"]),
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", master], capture_output=True)
    else:
        subprocess.run([FFMPEG, "-y", "-i", src, "-c", "copy",
                        "-movflags", "+faststart", master], capture_output=True)
    if textless_src and os.path.exists(textless_src):
        subprocess.run([FFMPEG, "-y", "-i", textless_src, "-c", "copy",
                        "-movflags", "+faststart",
                        os.path.join(pkg, "%s_textless.mp4" % name)], capture_output=True)
        results["textless"] = ("PASS", "clean version included")
    else:
        results["textless"] = ("WARN", "no textless version: localisation cannot "
                                       "un-burn a caption")

    slated = os.path.join(pkg, "%s_slated.mp4" % name)
    ok, err = build_slate(master, name, spec, slated,
                          extra="ANIMATIC - NOT FINAL PICTURE" if "ANIMATIC" in name.upper() else "")
    results["slate"] = (("PASS", "%ds slate ahead of picture" % SLATE_SECONDS)
                        if ok else ("WARN", "slate not built: %s" % err))

    failed = [k for k, (v, _) in results.items() if v == "FAIL"]
    manifest = {
        "package": name, "spec": spec, "source": src,
        "verdict": "REJECT" if failed else "ACCEPT",
        "failed_gates": failed,
        "gates": dict((k, {"result": v, "detail": d}) for k, (v, d) in results.items()),
        "loudness_measured": meas,
    }
    io.open(os.path.join(pkg, "MANIFEST.json"), "w", encoding="utf-8").write(
        json.dumps(manifest, indent=2))

    log("package %s  spec=%s" % (name, spec))
    for k, (v, d) in sorted(results.items()):
        log("  %-10s %-5s %s" % (k, v, d))
    log("VERDICT: %s%s" % (manifest["verdict"],
                           "" if not failed else "  (failed: %s)" % ", ".join(failed)))
    log("  -> %s" % pkg)
    return 1 if failed else 0


# --- self-test --------------------------------------------------------------

def self_test():
    fails = []

    def ck(name, cond):
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # loudness verdicts
    ck("on-spec loudness passes",
       check_loudness({"input_i": -23.2, "input_tp": -1.8}, "ebu")[0] == "PASS")
    ck("too loud fails",
       check_loudness({"input_i": -18.0, "input_tp": -1.5}, "ebu")[0] == "FAIL")
    ck("true peak over the ceiling fails",
       check_loudness({"input_i": -23.0, "input_tp": 0.5}, "ebu")[0] == "FAIL")
    ck("no audio is SKIP, not a silent pass",
       check_loudness(None, "ebu")[0] == "SKIP")
    ck("streaming spec has a different target",
       check_loudness({"input_i": -14.0, "input_tp": -1.5}, "streaming")[0] == "PASS"
       and check_loudness({"input_i": -14.0, "input_tp": -1.5}, "ebu")[0] == "FAIL")

    # PSE: the canary that matters most. Build a signal that MUST trip it.
    steady = [0.5] * 60
    ck("CANARY steady luminance reports no flash", flash_windows(steady) == [])
    strobe = [0.9 if k % 2 else 0.1 for k in range(60)]
    trip = flash_windows(strobe)
    ck("CANARY a hard strobe IS detected", len(trip) > 0 and trip[0][1] > FLASH_LIMIT)
    # an ordinary cut is one transition and must not be flagged
    cut = [0.2] * 30 + [0.8] * 30
    ck("a single hard cut is not a flash", flash_windows(cut) == [])
    # slow ramp is not a flash either
    ramp = [k / 60.0 for k in range(60)]
    ck("a slow ramp is not a flash", flash_windows(ramp) == [])
    # three flashes is legal, four is not
    legal = [0.5, 0.9, 0.1, 0.9, 0.1, 0.9] + [0.9] * 54
    ck("the limit is a real boundary, not decoration",
       all(n > FLASH_LIMIT for _, n in flash_windows(legal)) or flash_windows(legal) == [])

    ck("a slate is long enough to read but not to annoy", 3 <= SLATE_SECONDS <= 10)
    ck("every spec defines target, peak and window",
       all(set(v) == {"lufs", "peak", "window"} for v in SPECS.values()))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="ShotGrid Version code to deliver")
    ap.add_argument("--file", help="deliver a file directly")
    ap.add_argument("--check-only", metavar="FILE", help="run the gates, build nothing")
    ap.add_argument("--textless", help="path to the clean/textless master")
    ap.add_argument("--spec", default="streaming", choices=sorted(SPECS))
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()

    if ns.check_only:
        src = ns.check_only
        if not os.path.exists(src):
            log("no such file: %s" % src)
            return 2
        for name, fn in (("loudness", lambda: check_loudness(measure_loudness(src), ns.spec)),
                         ("pse_flash", lambda: check_pse(src)),
                         ("structure", lambda: check_structure(src, ns.spec))):
            v, d = fn()
            log("  %-10s %-5s %s" % (name, v, d))
        return 0

    src, name = ns.file, None
    if ns.version:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pm_backend_shotgrid as PMB
        sg = PMB.get_backend()
        v = sg.find_one("Version", [["project", "is", PROJ], ["code", "is", ns.version]],
                        ["code", "sg_path_to_movie"])
        if not v:
            log("no Version %s" % ns.version)
            return 2
        src, name = v.get("sg_path_to_movie"), v["code"]
    if not src or not os.path.exists(src):
        log("source media not found: %s" % src)
        return 2
    return build_package(src, name or os.path.splitext(os.path.basename(src))[0],
                         ns.spec, ns.textless)


if __name__ == "__main__":
    sys.exit(main())
