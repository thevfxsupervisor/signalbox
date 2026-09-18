#!/usr/bin/env python3
"""audio_bed.py: build a TEMP audio bed for an assembled episode cut.

Takes the picture (an assembled EPISODE.mp4), lays an audio bed under it,
and writes a versioned EPISODE_audio_v###.mp4. Temp-mix quality by design:
single-pass loudnorm, aac, no stems kept.

Bed sources:
  --music FILE        wav/m4a music bed, looped (or trimmed) to picture
                      length with a 2 s fade-out ending at picture end.
  --tone              no music: gentle pink room-tone (ffmpeg anoisesrc)
                      at -45 dB so the cut never plays dead silent.
  (neither)           digital silence; only useful with dialogue.

  --dialogue-csv FILE columns: start_seconds,file,gain_db. Each clip is
                      gained, delayed to its timestamp, and amix'ed over
                      the bed (relative paths resolve against the CSV's
                      own directory). A start_seconds at or beyond the
                      picture end is REFUSED with the row named - that
                      refusal is the self-test canary.

Always:
  - final mix loudnorm'ed to -16 LUFS, single pass (fine for temp; note a
    tone-only bed is deliberately brought UP to target by this).
  - output duration must equal picture duration within 0.1 s, ffprobe'd
    on both files; on mismatch the output is deleted and the run refused.
  - --out must end _audio_v###.mp4 and must not already exist (versioned,
    never overwrite - same rule as genvideo_worker's encode step).

Conventions inherited from build/tools/genvideo_worker.py: pinned ffmpeg
path, refuse rather than guess, no GPU, no ComfyUI, no ShotGrid.

Usage:
    python audio_bed.py --video EP.mp4 --out EP_audio_v001.mp4 --tone
    python audio_bed.py --video EP.mp4 --out EP_audio_v002.mp4 \
        --music bed.m4a --dialogue-csv lines.csv
    python audio_bed.py --self-test
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

FFDIR = os.environ.get("FFMPEG_DIR", "")
FFMPEG = os.path.join(FFDIR, "ffmpeg.exe") if FFDIR else "ffmpeg"
FFPROBE = os.path.join(FFDIR, "ffprobe.exe") if FFDIR else "ffprobe"

TARGET_LUFS = -16.0   # temp delivery target for the whole mix
DUR_TOL = 0.1         # seconds; output vs picture duration gate
FADE_S = 2.0          # music fade-out length
TONE_DB = -45.0       # room-tone level before loudnorm
SR = 48000


class Refusal(Exception):
    """A named, deliberate refusal. Message lands on stderr, exit 2."""


def log(msg):
    print("[audio_bed] %s" % msg, flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(path, stream):
    """Duration in seconds of stream 'v:0' or 'a:0' in path, falling back
    to the container duration when the stream does not report one.
    Raises Refusal when the stream is absent - doubling as the stream
    presence assertion."""
    r = run([FFPROBE, "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration:format=duration",
             "-of", "json", path])
    if r.returncode != 0:
        raise Refusal("ffprobe failed on %s: %s" % (path, r.stderr.strip()[:300]))
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise Refusal("no %s stream present in %s" % (stream, path))
    d = streams[0].get("duration")
    if d in (None, "N/A"):
        d = (data.get("format") or {}).get("duration")
    try:
        return float(d)
    except (TypeError, ValueError):
        raise Refusal("no readable duration for stream %s of %s" % (stream, path))


def parse_dialogue(csv_path, picture_dur):
    """Rows of (start_seconds, abs_file_path, gain_db). Every bad row is a
    named refusal, and the beyond-picture-end refusal is the CANARY the
    self-test proves able to fire."""
    if not os.path.exists(csv_path):
        raise Refusal("dialogue csv not found: %s" % csv_path)
    base = os.path.dirname(os.path.abspath(csv_path))
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        need = {"start_seconds", "file", "gain_db"}
        got = set(reader.fieldnames or [])
        if not need <= got:
            raise Refusal("dialogue csv %s missing column(s): %s (need %s)"
                          % (csv_path, ", ".join(sorted(need - got)),
                             ", ".join(sorted(need))))
        for i, row in enumerate(reader, start=1):
            name = "row %d" % i
            fname = (row.get("file") or "").strip()
            if not fname:
                raise Refusal("dialogue %s: empty file column" % name)
            try:
                start = float((row.get("start_seconds") or "").strip())
            except ValueError:
                raise Refusal("dialogue %s (%s): unparseable start_seconds %r"
                              % (name, fname, row.get("start_seconds")))
            graw = (row.get("gain_db") or "").strip()
            try:
                gain = float(graw) if graw else 0.0
            except ValueError:
                raise Refusal("dialogue %s (%s): unparseable gain_db %r"
                              % (name, fname, graw))
            path = fname if os.path.isabs(fname) else os.path.join(base, fname)
            if not os.path.exists(path):
                raise Refusal("dialogue %s: file not found: %s" % (name, path))
            if start < 0:
                raise Refusal("dialogue %s (%s): negative start_seconds %.3f"
                              % (name, fname, start))
            if start >= picture_dur:
                # CANARY: refuse instead of silently truncating a line that
                # could never be heard - almost always a timing typo.
                raise Refusal("dialogue %s (%s) starts at %.3fs, beyond picture "
                              "end %.3fs - fix the timestamp"
                              % (name, fname, start, picture_dur))
            rows.append((start, path, gain))
    return rows


def build_cmd(video, out, music, tone, rows, dur):
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", video]
    fc = []
    if music:
        # -stream_loop -1 loops a too-short bed forever; atrim cuts it (and
        # a too-long bed) to picture, then the fade lands at picture end.
        cmd += ["-stream_loop", "-1", "-i", music]
        fc.append("[1:a]aresample=%d,atrim=0:%.6f,asetpts=PTS-STARTPTS,"
                  "afade=t=out:st=%.6f:d=%.6f[bed]"
                  % (SR, dur, max(0.0, dur - FADE_S), min(FADE_S, dur)))
    elif tone:
        fc.append("anoisesrc=color=pink:sample_rate=%d:duration=%.6f,"
                  "volume=%.1fdB[bed]" % (SR, dur, TONE_DB))
    else:
        fc.append("anullsrc=r=%d:cl=stereo,atrim=0:%.6f[bed]" % (SR, dur))
    first_dlg = 2 if music else 1
    dlabels = []
    for i, (start, path, gain) in enumerate(rows):
        cmd += ["-i", path]
        fc.append("[%d:a]aresample=%d,volume=%.2fdB,adelay=%d:all=1[d%d]"
                  % (first_dlg + i, SR, gain, int(round(start * 1000)), i))
        dlabels.append("[d%d]" % i)
    if dlabels:
        # normalize=0: amix must not duck the bed by the input count.
        fc.append("[bed]%samix=inputs=%d:duration=longest:normalize=0[mix]"
                  % ("".join(dlabels), 1 + len(dlabels)))
        mix = "[mix]"
    else:
        mix = "[bed]"
    fc.append("%satrim=0:%.6f,apad=whole_dur=%.6f,"
              "loudnorm=I=%.1f:TP=-1.5:LRA=11,aresample=%d[aout]"
              % (mix, dur, dur, TARGET_LUFS, SR))
    cmd += ["-filter_complex", ";".join(fc),
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(SR),
            "-movflags", "+faststart", out]
    return cmd


def build(args):
    for exe in (FFMPEG, FFPROBE):
        if not os.path.exists(exe):
            raise Refusal("missing binary: %s" % exe)
    if not os.path.exists(args.video):
        raise Refusal("--video not found: %s" % args.video)
    if args.music and args.tone:
        raise Refusal("--tone is the no-music fallback; give one or the other")
    if args.music:
        if not os.path.exists(args.music):
            raise Refusal("--music not found: %s" % args.music)
        if not args.music.lower().endswith((".wav", ".m4a")):
            raise Refusal("--music must be .wav or .m4a, got %s"
                          % os.path.basename(args.music))
    if not re.search(r"_audio_v\d{3}\.mp4$", os.path.basename(args.out),
                     re.IGNORECASE):
        raise Refusal("--out must be named like EPISODE_audio_v001.mp4 "
                      "(versioned), got %r" % os.path.basename(args.out))
    if os.path.exists(args.out):
        raise Refusal("refusing to overwrite %s - bump the version number"
                      % args.out)
    dur = probe_duration(args.video, "v:0")
    rows = parse_dialogue(args.dialogue_csv, dur) if args.dialogue_csv else []
    if not (args.music or args.tone or rows):
        raise Refusal("nothing to mix: give --music, --tone, and/or "
                      "--dialogue-csv")
    bed = ("music %s" % os.path.basename(args.music)) if args.music \
        else ("room tone %.0f dB" % TONE_DB) if args.tone else "silence"
    log("picture %.3fs, bed: %s, dialogue clips: %d" % (dur, bed, len(rows)))
    r = run(build_cmd(args.video, args.out, args.music, args.tone, rows, dur))
    if r.returncode != 0 or not os.path.exists(args.out) \
            or os.path.getsize(args.out) == 0:
        if os.path.exists(args.out):
            os.remove(args.out)
        raise Refusal("ffmpeg mix failed:\n%s" % r.stderr[-900:])
    # Duration gate: picture governs. Check BOTH output streams so a
    # short audio track cannot hide behind a correct container length.
    for label, stream in (("video", "v:0"), ("audio", "a:0")):
        got = probe_duration(args.out, stream)
        if abs(got - dur) > DUR_TOL:
            os.remove(args.out)
            raise Refusal("duration mismatch: output %s stream %.3fs vs "
                          "picture %.3fs (tolerance %.1fs) - output deleted"
                          % (label, got, dur, DUR_TOL))
    log("OK: %s (loudnorm %.0f LUFS, duration matches picture within %.1fs)"
        % (args.out, TARGET_LUFS, DUR_TOL))


# ---------------------------------------------------------------- self-test
def self_test():
    for exe in (FFMPEG, FFPROBE):
        if not os.path.exists(exe):
            print("[self-test] FAIL: missing binary %s" % exe, flush=True)
            return 1
    tmp = tempfile.mkdtemp(prefix="audio_bed_selftest_")
    tool = os.path.abspath(__file__)
    fails = []

    def check(label, cond, detail=""):
        print("[self-test] %s: %s%s"
              % ("PASS" if cond else "FAIL", label,
                 ("" if cond or not detail else " - " + detail)), flush=True)
        if not cond:
            fails.append(label)

    def ff(*a):
        r = run([FFMPEG, "-hide_banner", "-nostdin", "-y"] + list(a))
        if r.returncode != 0:
            raise SystemExit("self-test asset generation failed:\n%s"
                             % r.stderr[-500:])

    def tool_run(out, *extra):
        return run([sys.executable, tool, "--video", video, "--out", out]
                   + list(extra))

    def mean_volume_db(path, t0, t1):
        """mean_volume (dB) of the output audio between t0 and t1, via
        ffmpeg volumedetect. Digital silence reads as about -91 dB (or
        -inf, mapped to -120)."""
        r = run([FFMPEG, "-hide_banner", "-nostdin", "-i", path,
                 "-map", "a:0", "-af",
                 "atrim=%.3f:%.3f,volumedetect" % (t0, t1), "-f", "null", "-"])
        m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", r.stderr)
        if not m:
            return None
        return -120.0 if m.group(1) == "-inf" else float(m.group(1))

    def assert_good(label, out, cp):
        ok, detail = True, ""
        if cp.returncode != 0:
            ok, detail = False, "exit %d: %s" % (cp.returncode, cp.stderr[-300:])
        else:
            try:
                vd = probe_duration(out, "v:0")   # raises if stream absent
                ad = probe_duration(out, "a:0")
                if abs(vd - pic_dur) > DUR_TOL or abs(ad - pic_dur) > DUR_TOL:
                    ok, detail = False, ("durations v=%.3f a=%.3f vs picture "
                                         "%.3f" % (vd, ad, pic_dur))
            except Refusal as e:
                ok, detail = False, str(e)
        check(label, ok, detail)

    try:
        # Tiny assets: 4 s bars picture (no audio), 1.5 s sine music (short
        # on purpose so mode B proves looping), 0.5 s sine "dialogue".
        video = os.path.join(tmp, "EP.mp4")
        music = os.path.join(tmp, "music.wav")
        ff("-f", "lavfi", "-i", "smptebars=size=320x180:rate=24", "-t", "4",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", video)
        ff("-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000",
           "-t", "1.5", "-c:a", "pcm_s16le", music)
        ff("-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
           "-t", "0.5", "-c:a", "pcm_s16le", os.path.join(tmp, "line.wav"))
        pic_dur = probe_duration(video, "v:0")
        check("assets generated (picture %.3fs)" % pic_dur,
              3.9 < pic_dur < 4.1)

        csv_ok = os.path.join(tmp, "dialogue.csv")
        with open(csv_ok, "w", encoding="utf-8", newline="") as fh:
            fh.write("start_seconds,file,gain_db\n"
                     "0.5,line.wav,-3\n"
                     "2.0,line.wav,0\n")
        csv_bad = os.path.join(tmp, "dialogue_bad.csv")
        with open(csv_bad, "w", encoding="utf-8", newline="") as fh:
            fh.write("start_seconds,file,gain_db\n"
                     "0.5,line.wav,0\n"
                     "99.0,line.wav,0\n")

        # Mode A: room tone only.
        out_a = os.path.join(tmp, "EP_audio_v001.mp4")
        assert_good("mode A tone bed", out_a, tool_run(out_a, "--tone"))

        # Mode B: short music looped + faded to picture.
        out_b = os.path.join(tmp, "EP_audio_v002.mp4")
        assert_good("mode B music bed (1.5s music looped to 4s picture)",
                    out_b, tool_run(out_b, "--music", music))
        # Duration alone cannot prove looping: apad would fill a broken
        # (un-looped) bed with silence to picture length. So listen: the
        # 1.6-2.0s window is past the 1.5s un-looped music end and before
        # the fade starts (4s - 2s = 2.0s). Looped music reads well above
        # -60 dB there; a dead loop reads as digital silence (about -91).
        lvl = mean_volume_db(out_b, 1.6, 2.0) if os.path.exists(out_b) else None
        check("mode B loop audible past un-looped music end (%s)"
              % ("%.1f dB" % lvl if lvl is not None else "unreadable"),
              lvl is not None and lvl > -60.0)

        # Mode C: music + dialogue at timestamps.
        out_c = os.path.join(tmp, "EP_audio_v003.mp4")
        assert_good("mode C music + dialogue csv", out_c,
                    tool_run(out_c, "--music", music,
                             "--dialogue-csv", csv_ok))

        # Mode D: dialogue over silence (no music, no tone).
        out_d = os.path.join(tmp, "EP_audio_v004.mp4")
        assert_good("mode D dialogue over silence", out_d,
                    tool_run(out_d, "--dialogue-csv", csv_ok))

        # Overwrite refusal: v001 exists from mode A.
        cp = tool_run(out_a, "--tone")
        check("overwrite refused on existing v001",
              cp.returncode != 0 and "refusing to overwrite" in cp.stderr,
              "exit %d, stderr %r" % (cp.returncode, cp.stderr[-200:]))

        # CANARY: dialogue timestamp beyond picture end must be REFUSED with
        # the row named, and no output written. If the guard in
        # parse_dialogue were removed, ffmpeg would happily mix and trim the
        # 99 s line away, the tool would exit 0, and THIS check would fail -
        # proving the canary can fire.
        out_e = os.path.join(tmp, "EP_audio_v005.mp4")
        cp = tool_run(out_e, "--music", music, "--dialogue-csv", csv_bad)
        named = "row 2" in cp.stderr and "99.000" in cp.stderr
        check("CANARY: beyond-picture-end dialogue refused, row named",
              cp.returncode != 0 and named and not os.path.exists(out_e),
              "exit %d, stderr %r" % (cp.returncode, cp.stderr[-300:]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("[self-test] %s" % ("ALL PASS" if not fails
                              else "FAILED: %s" % "; ".join(fails)), flush=True)
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(
        description="Build a temp audio bed under an assembled episode cut.")
    ap.add_argument("--video", help="assembled picture cut, EPISODE.mp4")
    ap.add_argument("--out", help="versioned output, EPISODE_audio_v###.mp4 "
                                  "(refuses to overwrite)")
    ap.add_argument("--music", help="wav/m4a music bed; looped/trimmed to "
                                    "picture with a 2s fade-out")
    ap.add_argument("--tone", action="store_true",
                    help="no music: pink room tone at -45 dB")
    ap.add_argument("--dialogue-csv", dest="dialogue_csv",
                    help="csv of start_seconds,file,gain_db dialogue clips")
    ap.add_argument("--self-test", action="store_true", dest="self_test",
                    help="generate tiny assets, run all modes + canary")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.video or not args.out:
        ap.error("--video and --out are required (or use --self-test)")
    try:
        build(args)
    except Refusal as e:
        print("REFUSED: %s" % e, file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
