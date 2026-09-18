#!/usr/bin/env python3
"""Episode assembler: conform approved Versions into one reviewable episode cut.

What it does, in order:

  1. Pull the Shots of --sequence CODE, or the explicit list given to
     --shots CODE,CODE (for a partial scene whose sequence is not finished),
     or every Shot with --project-wide, in
     cut order: sg_cut_order when the field exists and is set, else creation
     order. Shots without a cut order sort AFTER shots with one, by id.
  2. For each Shot take the LATEST APPROVED Version (approved as defined by
     review_ledger.classify - provisional appcbb/appgra NEVER counts) and its
     sg_path_to_movie. The file must exist on disk AND ffprobe must parse it.
     Anything else REFUSES with the shot named, because a silently dropped
     shot becomes a missing shot in the cut that nobody notices until the
     screening.
  3. Concatenate with the ffmpeg concat demuxer, re-encoding to H.264 High
     yuv420p 24fps so mixed sources become one uniform stream.
  4. Optional 2s slate up front (drawtext with an explicit fontfile - this box
     has no fontconfig, so ffmpeg cannot find a font by name). Optional
     --audio BED.wav laid under the picture; the generated clips carry no
     audio track, so "mixed under" means the bed is the only audio, cut to
     picture length.
  4b. CAPTIONS, wired in here (was the standing gap ACT-B-COMPLETE.md found -
     assembly used to only concatenate). Same contract as conform.py: TIMING
     COMES FROM SHOTGRID. Shot.sg_caption_text and Shot.sg_gen_frames for the
     SAME picked shots, in the SAME cut order, are handed straight to
     captions_sg.cues_from_shots() (imported, never copied - invariant 11) so
     a caption can never point at a shot the cut does not contain. If any
     picked shot carries caption text the cues are rendered to an SRT sidecar
     written BESIDE the mp4 (a burned-in caption cannot be corrected; a
     sidecar can - same reasoning as animatic.py's episode stitch) and burned
     in via animatic.subtitles_vf() - the SAME ffmpeg burn path animatic.py's
     episode cut uses, not a second implementation, so the MarginV=52 fix for
     the burnt-in-slug collision comes along for free. A cut order shift by
     the slate is accounted for (cues start after the slate, not during it).
     A sequence where no picked shot has caption text assembles exactly as
     before - captions are additive, never a new refusal reason. --no-captions
     opts out explicitly.
  5. Write output\\episodes\\<sequence>_cut_v###.mp4. The number is one past
     the highest ever seen in BOTH the ledger and the directory, and ffmpeg
     runs with -n, so an existing cut is never overwritten.
  6. Unless --no-publish, create a Version at status "rev" so the cut goes
     through the same review loop as everything else. It lands on the Sequence
     entity, or, for a --shots cut (which no single Sequence owns), on the
     shots' Episode.

Env (publish only): SHOTGRID_SITE_URL / SHOTGRID_SCRIPT_NAME /
SHOTGRID_SCRIPT_KEY. Read via os.environ - absence is a loud KeyError,
never a default.

Usage:
    python episode_assemble.py --sequence SEQ010 [--slate] [--audio BED.wav]
    python episode_assemble.py --sequence SEQ010 --no-captions
    python episode_assemble.py --shots SH010,SH020 --label MYSCENE
    python episode_assemble.py --project-wide --no-publish
    python episode_assemble.py --self-test        (mocks + tiny clips, no SG)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

# ---------------------------------------------------------------- constants
PROJECT_ID = 9999
ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): this used to be
# os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tests") --
# a SIBLING-relative guess that only works when tools/ and tests/ happen to
# sit next to each other (true in the repo, and true of the old flat
# build\tools\ / build\tests\ layout). tools/deploy.py's release directories
# nest one level deeper (build\releases\<id>\tools\), so that guess would
# silently start missing tests\review_ledger.py -- a module this file calls
# at runtime, not just in its self-test. tests/ has no deploy seam of its own
# yet (see DEPLOY-SEAM.md), so this now points at the one stable location
# every OTHER module that reaches into tests/ already uses (character_sheets.py,
# panel_compose.py, video_from_panel.py, prompt_revision.py, finishing.py) --
# invariant 11, one implementation of "where is tests/", not two.
sys.path.insert(0, os.path.join(ROOT, "build", "tests"))
import review_ledger as RL                                    # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sg_provenance as PROV                                   # noqa: E402
import sg_publish as PUB                                        # noqa: E402
import captions_sg as CAP                                       # noqa: E402  invariant 11
import animatic as ANIM                                         # noqa: E402  invariant 11 (burn path)

# Superseding statuses come from sg_publish, the ONE definition
# (invariant 11). This module used to carry its own copy; a third copy
# was about to be added in video_from_panel, which is when it moved.
SUPERSEDABLE = PUB.SUPERSEDABLE
OUT_EPISODES = os.path.join(ROOT, "output", "episodes")
LEDGER_DIR = os.path.join(ROOT, "output", "ledger")
FFBIN = os.environ.get("FFMPEG_DIR", "")
FFMPEG = os.path.join(FFBIN, "ffmpeg.exe") if FFBIN else "ffmpeg"
FFPROBE = os.path.join(FFBIN, "ffprobe.exe") if FFBIN else "ffprobe"
# No fontconfig on this box: drawtext must be handed a file, and the ffmpeg
# filter parser wants the drive colon escaped even inside quotes.
FONT_FF = "C\\:/Windows/Fonts/consola.ttf"
import timeline as _TL          # invariant 11: ONE timeline fps
FPS = _TL.FPS                   # was a hardcoded 24
SLATE_SECONDS = 2


def log(msg):
    print("[assemble] %s" % msg, flush=True)


def sg_connect():
    # Imported here, not at module top, so --self-test runs on a box with no
    # shotgun_api3 and no credentials. Publishing is the only caller.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


# ---------------------------------------------------------------- ffprobe
def ffprobe_video_stream(path):
    """First video stream as a dict, or None if ffprobe cannot parse the file.
    None is a REFUSAL signal upstream: an unparseable movie must not be cut in."""
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height",
                        "-of", "json", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        streams = json.loads(r.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    return streams[0] if streams else None


def count_frames(path):
    """Decode-and-count, not metadata: nb_read_frames is what a player will
    actually show, which is the number that matters for a cut."""
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-count_frames", "-show_entries", "stream=nb_read_frames",
                        "-of", "json", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        streams = json.loads(r.stdout).get("streams") or []
        return int(streams[0]["nb_read_frames"])
    except (json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None


def has_audio_stream(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name", "-of", "json", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return False
    try:
        return bool(json.loads(r.stdout).get("streams"))
    except json.JSONDecodeError:
        return False


# ---------------------------------------------------------------- shot / version pick
def fetch_shots(sg, project, seq_code=None, shot_codes=None):
    """Shots in cut order. sg_cut_order is optional on this site, so the field
    is requested optimistically and dropped on a schema fault rather than
    hard-coding whether it exists today. sg_caption_text/sg_gen_frames ride
    along the same way, for the same reason - captions.py/conform.py's own
    lesson - a caption field missing on some site must not break assembly
    itself, only silently produce a cut with no subtitles (see caption_cues)."""
    filters = [["project", "is", project]]
    if shot_codes:
        # AN EXPLICIT SHOT LIST. Without this the assembler could only take a
        # whole Sequence or the whole project, which is why the 2026-09-05
        # demo cut had to be built as two sequence cuts concatenated by hand.
        # A sequence whose OTHER shots are not finished cannot be cut at all,
        # correctly, since a missing approval is a refusal: so showing three
        # finished shots out of eleven needed this.
        filters.append(["code", "in", list(shot_codes)])
    elif seq_code:
        filters.append(["sg_sequence.Sequence.code", "is", seq_code])
    try:
        shots = sg.find("Shot", filters,
                        ["code", "sg_cut_order", "sg_caption_text", "sg_gen_frames"])
    except Exception:
        try:
            shots = sg.find("Shot", filters, ["code", "sg_cut_order"])
        except Exception:
            shots = sg.find("Shot", filters, ["code"])

    def key(s):
        cut = s.get("sg_cut_order")
        if isinstance(cut, (int, float)):
            return (0, cut, s["id"])
        return (1, 0, s["id"])          # no cut order: creation order, after ordered shots
    return sorted(shots, key=key)


def latest_approved_version(sg, shot):
    """Newest Version whose status classifies as approved. Sorted client-side
    so the mock in --self-test and the real API behave identically. A newer
    provisional (appcbb/appgra) does NOT shadow an older approval - it simply
    is not approved, per review_ledger rule 3."""
    versions = sg.find("Version",
                       [["entity", "is", {"type": "Shot", "id": shot["id"]}]],
                       ["code", "sg_status_list", "sg_path_to_movie", "created_at"])
    versions.sort(key=lambda v: v["created_at"], reverse=True)
    for v in versions:
        if RL.classify(v.get("sg_status_list")) == "approved":
            return v
    return None


def pick_movies(sg, shots):
    """(picks, problems). picks is [(shot, version)] in cut order; problems is
    a list of human-readable refusal lines, one per bad shot, ALWAYS naming
    the shot. Any problem refuses the whole assembly."""
    picks, problems = [], []
    for shot in shots:
        code = shot["code"]
        v = latest_approved_version(sg, shot)
        if v is None:
            problems.append("%s: no approved Version (provisional/pending do not count)" % code)
            continue
        movie = v.get("sg_path_to_movie")
        if not movie:
            problems.append("%s: approved Version %s has no sg_path_to_movie" % (code, v["code"]))
            continue
        if not os.path.exists(movie):
            problems.append("%s: movie missing on disk: %s" % (code, movie))
            continue
        if ffprobe_video_stream(movie) is None:
            problems.append("%s: ffprobe cannot parse %s" % (code, movie))
            continue
        picks.append((shot, v))
    if not picks and not problems:
        problems.append("no Shots found to assemble")
    return picks, problems


# ---------------------------------------------------------------- assembly
def next_cut_number(out_dir, label, led):
    """One past the highest EVER seen: ledger entries AND files on disk both
    count, so neither a deleted file nor a wiped ledger can reuse a number
    that a reviewer may have a note about."""
    highest = RL.next_version_number(led) - 1
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            m = re.fullmatch(re.escape(label) + r"_cut_v(\d+)\.mp4", name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def build_slate(tmp_dir, width, height, lines):
    """2s slate at the cut's own size so the concat demuxer sees one uniform
    stream. Text is sanitized to characters the drawtext parser cannot
    misread; losing punctuation off a slate beats a filtergraph parse error."""
    draw = []
    for i, line in enumerate(lines):
        text = re.sub(r"[^A-Za-z0-9 _.\-]", "_", line)
        draw.append("drawtext=fontfile='%s':text='%s':fontcolor=white:"
                    "fontsize=%d:x=(w-text_w)/2:y=%d"
                    % (FONT_FF, text, max(12, height // 12),
                       int(height * (0.25 + 0.16 * i))))
    slate = os.path.join(tmp_dir, "slate.mp4")
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i",
                        "color=c=black:size=%dx%d:rate=%d" % (width, height, FPS),
                        "-frames:v", str(FPS * SLATE_SECONDS),
                        "-vf", ",".join(draw),
                        "-c:v", "libx264", "-profile:v", "high",
                        "-pix_fmt", "yuv420p", "-crf", "18", slate],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(slate):
        raise RuntimeError("slate build failed:\n%s" % r.stderr[-800:])
    return slate


def concat_line(path):
    # concat demuxer list syntax: forward slashes and single quotes survive
    # spaces in this Dropbox path; an embedded quote is escaped the sh way.
    return "file '%s'" % path.replace("\\", "/").replace("'", "'\\''")


# ---------------------------------------------------------------- captions
def measured_frames(path):
    """The clip's real duration, expressed in CAPTION-timeline frames, or None.

    NOT the file's frame COUNT. That was my first fix and it was the wrong half:
    a frame count means nothing without the rate it plays at, and these clips
    are 24fps files while the caption maths divides by 16. 121 frames read as
    7.562s when the clip is 5.042s long. Measuring frames instead of the field
    changed nothing, because both were then divided by the same wrong rate.

    So: measure SECONDS, the only fps-independent fact, and convert into
    whatever rate the caption timeline uses. Then the clip's real length is
    what times the caption, whatever the file's rate happens to be.

    Captions are timed off this and NOT off sg_gen_frames. That field used to
    be the duration of record, and the docstring below still said so, because
    fps_bridge time-stretched every clip to hit it. F017 removed the stretch:
    length is a TRIM now and is clamped to the model's native 81 frames, so a
    shot asking for 121 delivers 81 and records the shortfall.

    Nobody re-read the captions when that landed. Measured 2026-09-06 on the
    demo cut: cues ran to 16.687s against an 11.25s picture, every caption after
    the first was late, and 5.4s of subtitle hung off the end. Geoff found it by
    watching: "the subtitles don't align with the timing of the cuts".

    A field describing an INTENT cannot time a picture describing a DELIVERY.
    """
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_frames,r_frame_rate,duration",
                        "-of", "json", path], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        st = (json.loads(r.stdout).get("streams") or [None])[0]
    except json.JSONDecodeError:
        return None
    if not st:
        return None
    secs = None
    try:
        secs = float(st.get("duration"))
    except (TypeError, ValueError):
        # No stream duration: fall back to count over its OWN rate, never ours.
        n, rate = st.get("nb_frames"), (st.get("r_frame_rate") or "")
        if n and str(n).isdigit() and "/" in rate:
            num, den = rate.split("/")
            if float(den) and float(num):
                secs = int(n) * float(den) / float(num)
    if not secs or secs <= 0:
        return None
    return max(1, int(round(secs * CAP.FPS)))


def caption_cues(picks, offset_ms=0):
    """Cues for exactly the shots this cut picked, in exactly the order it
    picked them - reusing captions_sg.cues_from_shots() (invariant 11) rather
    than re-deriving timing here. Same contract as conform.py: SHOTGRID'S
    sg_gen_frames is the duration of record, not whatever a file happens to
    decode to (video_from_panel.py/fps_bridge.py conform generation TO that
    field already, so this stays consistent with the picture it is captioning).

    offset_ms shifts every cue later - used to push captions past an optional
    slate, so a cue never lands on the slate instead of the shot it belongs to.
    Returns [] (never None) when no picked shot carries caption text, which is
    the "assemble cleanly, no subtitles" case."""
    rows = []
    for shot, ver in picks:
        real = measured_frames(ver.get("sg_path_to_movie") or "")
        if real is None:
            real = int(shot.get("sg_gen_frames") or 121)
            log("captions: WARNING, could not measure %s, timing from "
                "sg_gen_frames which may not match the picture"
                % (shot.get("code") or "?"))
        rows.append({"_frames": real,
                     "sg_caption_text": shot.get("sg_caption_text") or ""})
    cues = CAP.cues_from_shots(rows)
    if offset_ms:
        cues = [(a + offset_ms, b + offset_ms, txt) for a, b, txt in cues]
    return cues


def write_caption_srt(out_dir, cut_code, cues):
    """SRT written BESIDE the mp4, same name, same reasoning as animatic.py's
    episode stitch: a burned-in caption cannot be corrected, a sidecar can.
    Returns the path, or None if there is nothing to caption."""
    if not cues:
        return None
    path = os.path.join(out_dir, cut_code + ".srt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(CAP.render(cues))
    return path


def assemble(sg, label, shots, project, out_dir, ledger_dir,
             audio=None, slate=False, publish=True, project_wide=False,
             captions=True, entity_override=None):
    """Returns (out_path, problems). problems non-empty means REFUSED and
    out_path is None; nothing was written and nothing was published."""
    picks, problems = pick_movies(sg, shots)
    if problems:
        return None, problems

    if audio:
        if not os.path.exists(audio):
            return None, ["audio bed missing on disk: %s" % audio]
        if not has_audio_stream(audio):
            return None, ["audio bed has no audio stream (ffprobe): %s" % audio]

    # Resolve the publish target BEFORE writing anything. Finding out the
    # Sequence does not exist after the cut is on disk and in the ledger would
    # make the "nothing was written" refusal contract a lie.
    entity = None
    if publish and not project_wide:
        if entity_override is not None:
            # A --shots cut spans an arbitrary set of shots, so no single
            # Sequence owns it and `label` is a name the operator chose, not a
            # Sequence code. Without this the publish refused outright and the
            # cut could not reach ShotGrid at all, breaking the standing rule
            # that the record lives there. Episode is the right home:
            # publishing a PARTIAL episode there is authorised (Geoff,
            # 2026-09-05) and it is what the demo cut already does.
            entity = entity_override
        else:
            seq = sg.find_one("Sequence", [["project", "is", project],
                                           ["code", "is", label]], ["code"])
            if seq is None:
                return None, ["cannot publish: Sequence %r not found in project %d"
                              % (label, project["id"])]
            entity = {"type": "Sequence", "id": seq["id"]}

    os.makedirs(out_dir, exist_ok=True)
    ledger_path = os.path.join(ledger_dir, "episode_%s.json" % label)
    led = RL.load(ledger_path)
    led["shot"] = "EPISODE:%s" % label
    n = next_cut_number(out_dir, label, led)
    cut_code = "%s_cut_v%03d" % (label, n)
    out_path = os.path.join(out_dir, cut_code + ".mp4")
    if os.path.exists(out_path):
        # next_cut_number already skipped every existing file; reaching here
        # means a race or a bug, and overwriting a cut is never the answer.
        return None, ["refusing to overwrite existing %s" % out_path]

    # Captions (4b): computed BEFORE ffmpeg runs, so a single pass burns them
    # in rather than a second re-encode. offset_ms pushes cues past the slate
    # (if any) so a cue never lands on slate frames instead of its shot's.
    srt_path = None
    cues = []
    if captions:
        offset_ms = SLATE_SECONDS * 1000 if slate else 0
        cues = caption_cues(picks, offset_ms=offset_ms)
        srt_path = write_caption_srt(out_dir, cut_code, cues)
    # LETTERBOX, not an overlay on the picture (Geoff, 2026-09-06). Captions
    # go in a black bar BELOW the frame and the cut's version name sits in a
    # bar above it, so nothing is burned over the image itself. A burned-in
    # caption cannot be removed later, and a reviewer reading a caption is not
    # watching the acting underneath it.
    # GENVIDEO_CAPTIONS_OVERLAY=1 restores the old draw-on-picture behaviour.
    if os.environ.get("GENVIDEO_CAPTIONS_OVERLAY", "") == "1":
        vf = ANIM.subtitles_vf(srt_path)
    else:
        vf = ANIM.letterbox_vf(srt_path, slug=cut_code)
    if srt_path:
        log("captions: %d cue(s) -> %s" % (len(cues), srt_path))
    else:
        log("captions: no picked shot carries sg_caption_text, cutting without subtitles")

    tmp_dir = tempfile.mkdtemp(prefix="episode_assemble_")
    try:
        entries = []
        if slate:
            first = ffprobe_video_stream(picks[0][1]["sg_path_to_movie"])
            slate_path = build_slate(tmp_dir, int(first["width"]), int(first["height"]),
                                     [label, "CUT v%03d" % n,
                                      datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                      "%d shots" % len(picks)])
            entries.append(slate_path)
        entries.extend(v["sg_path_to_movie"] for _, v in picks)
        list_path = os.path.join(tmp_dir, "concat.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(concat_line(p) for p in entries) + "\n")

        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-n",
               "-f", "concat", "-safe", "0", "-i", list_path]
        if audio:
            # The clips are silent (encoded from PNG sequences), so the bed is
            # mapped in whole rather than amixed with nothing; -shortest cuts
            # it to picture length.
            cmd += ["-i", audio, "-map", "0:v:0", "-map", "1:a:0",
                    "-c:a", "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += ["-map", "0:v:0", "-an"]
        if vf:
            # SAME burn path animatic.py's episode cut uses (invariant 11) -
            # not a second subtitles filter implementation.
            cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-crf", "18", "-movflags", "+faststart", out_path]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            return None, ["ffmpeg concat failed:\n%s" % r.stderr[-800:]]
        if ffprobe_video_stream(out_path) is None:
            return None, ["output written but ffprobe cannot parse it: %s" % out_path]
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

    total = count_frames(out_path)
    RL.append(led, "episode_cut", version_number=n, code=cut_code,
              movie=os.path.basename(out_path), frames=total,
              slate=bool(slate), audio=os.path.basename(audio) if audio else None,
              captions=bool(srt_path), caption_cues=len(cues),
              shots=[s["code"] for s, _ in picks],
              versions=[v["code"] for _, v in picks])
    RL.save(ledger_path, led)

    if publish:
        # entity was resolved up top, before any write. None means a
        # project-wide cut: no single Sequence owns it, so the Version hangs
        # off the project alone; it still lands in review at "rev".
        desc = ("Episode cut assembled from %d approved shot Version(s): %s.%s%s%s"
                % (len(picks), ", ".join(v["code"] for _, v in picks),
                   " 2s slate." if slate else "",
                   (" Audio bed: %s." % os.path.basename(audio)) if audio else "",
                   (" %d caption cue(s) burned in (edit-time graphic, D15)."
                    % len(cues)) if srt_path else " No captions (no shot carried text)."))
        # --- D6_COMPONENTS: an episode cut concatenates many approved shot
        # Versions - there is no single character/set/action/camera/style and
        # no single anchor, same reasoning as animatic.py's cmd_episode. State
        # that plainly rather than leaving the fields blank.
        wf_hash = PROV.workflow_hash_from_text(
            "episode_cut:" + ",".join(v["code"] for _, v in picks)
            + "|slate=%s|audio=%s|captions=%s" % (bool(slate), bool(audio), bool(srt_path)))
        # SUPERSEDE THE PREVIOUS APPROVED CUT ON THIS ENTITY FIRST.
        #
        # sg_review_housekeeping rejects any Version at 'rev' whose
        # (entity, stage) already has an approved sibling, calling it "an
        # alternate; a sibling at this stage is approved". It runs LIVE
        # (dry=False) inside genvideo_service.watch_review_housekeeping, so
        # without this the SECOND cut ever published on an entity is moved to
        # 'rjct' automatically, BEFORE a human ever sees it. The approve, note,
        # re-cut loop would silently reject its own output on the first
        # iteration.
        #
        # This is the same defect that hit panels, and the same fix:
        # un-approve the thing being superseded before the replacement lands.
        # Nothing else un-approves, which is the recorded gap
        # "un-approval not handled".
        # THE SUPERSEDE BLOCK IS GONE, 2026-09-06. It rejected every approved
        # cut on this entity before publishing a new one, and it existed only
        # to dodge the review sweeper's auto-reject. Geoff removed the reason:
        # a cut is not one of a batch of alternates, so the sweeper no longer
        # touches episode or sequence stages at all (see
        # sg_review_housekeeping.ALTERNATE_STAGES). Un-approving somebody's
        # approved cut to publish an unrelated one was never the intent, and
        # with the sweeper corrected it is pure damage.

        ver = PUB.publish_version(
            sg, project=project, entity=entity, code=cut_code,
            media_path=out_path, description=desc,
            first_frame=1001, last_frame=1000 + (total or 0),
            character="aggregate of %d shot(s): %s"
                      % (len(picks), ", ".join(v["code"] for _, v in picks)[:800]),
            set_="aggregate - see linked shot Versions, no single set",
            action="episode/sequence cut: concatenation of approved shot Versions "
                   "in cut order%s%s" % (" with 2s slate" if slate else "",
                                        " with burned-in subtitles" if srt_path else ""),
            camera="n/a - assembly of pre-approved shots, no camera move of its own",
            style="n/a - inherits each shot's own style, not restyled here",
            workflow_hash=wf_hash, anchor_version_id=None, log=log,
            # A CUT IS A DELIVERABLE, NOT A TEST CELL. sg_review_housekeeping
            # sweeps any stage-less Version to rjct as a leftover from before
            # the stage vocabulary, so publishing without this silently
            # retires the one artefact the whole assembly exists to produce.
            # Measured 2026-09-05: both SHOW01 sequence cuts were
            # auto-retired this way (then, to the status this collapsed
            # from 2026-09-08; now, to rjct -- same bug, same fix, same
            # reason text on the row).
            stage="episode")
        RL.append(led, "status", status="rev", sg_id=ver["id"])
        RL.save(ledger_path, led)
        log("published Version %d (%s) on %s at rev"
            % (ver["id"], cut_code, "project" if entity is None else label))
    return out_path, []


# ---------------------------------------------------------------- self-test
class _MockSG:
    """Just enough shotgun_api3 surface for assemble(). No network ever."""

    def __init__(self, shots, versions_by_shot, sequences):
        self.shots = shots
        self.versions_by_shot = versions_by_shot
        self.sequences = sequences
        self.created = []
        self.uploaded = []
        # Without this the supersede step raises AttributeError, my own
        # except clause swallows it as a warning, and the self-test passes
        # over a step that never ran.
        self.updated = []

    def find(self, etype, filters, fields=None, order=None):
        if etype == "Shot":
            rows = [dict(s) for s in self.shots]
            # Honour a ["code", "in", [...]] filter the way the real API does.
            # The mock used to ignore filters entirely and return every shot,
            # which meant no test could tell a working scope filter from a
            # broken one: --shots would have "passed" while selecting the
            # whole sequence.
            for f in filters:
                if len(f) == 3 and f[0] == "code" and f[1] == "in":
                    wanted = set(f[2])
                    rows = [r for r in rows if r.get("code") in wanted]
            return rows
        if etype == "Version":
            sid = next(f[2]["id"] for f in filters if f[0] == "entity")
            return [dict(v) for v in self.versions_by_shot.get(sid, [])]
        return []

    def find_one(self, etype, filters, fields=None):
        if etype == "Sequence":
            code = next(f[2] for f in filters if f[0] == "code")
            for s in self.sequences:
                if s["code"] == code:
                    return dict(s)
        return None

    def create(self, etype, data):
        rec = dict(data)
        rec["type"] = etype
        rec["id"] = 9000 + len(self.created)
        self.created.append(rec)
        return rec

    def upload(self, etype, eid, path, field_name=None, display_name=None):
        self.uploaded.append((etype, eid, path, field_name))

    def update(self, etype, eid, data):
        # Enough surface for sg_provenance.write_provenance() to exercise for
        # real in the self-test, same principle as create()/upload() above.
        self.updated.append((etype, eid, dict(data)))
        for rec in self.created:
            if rec["type"] == etype and rec["id"] == eid:
                rec.update(data)
                return rec
        # Also serves the supersede step, which updates PRE-EXISTING Versions
        # rather than ones this run created. A second update() defined later
        # in this class silently shadowed a first one added for exactly that,
        # and the supersede canary is what caught it.
        for vs in self.versions_by_shot.values():
            for v in vs:
                if v.get("id") == eid:
                    v.update(data)
                    return dict(v)
        raise ValueError("mock update() on unknown %s %s" % (etype, eid))


def _make_clip(path):
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "smptebars=size=320x180:rate=%d" % FPS,
                        "-frames:v", "12", "-c:v", "libx264", "-profile:v", "high",
                        "-pix_fmt", "yuv420p", "-crf", "18", path],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("test clip build failed:\n%s" % r.stderr[-400:])


def self_test():
    import shutil
    tmp = tempfile.mkdtemp(prefix="episode_assemble_selftest_")
    out_dir = os.path.join(tmp, "episodes")
    led_dir = os.path.join(tmp, "ledger")
    project = {"type": "Project", "id": PROJECT_ID}
    # publish_version() durable-copies into PUB.PUBLISHED_DIR - redirect that
    # to a tmp dir for the self-test so it never writes test clips into the
    # real deployed output\published\ folder.
    saved_published_dir = PUB.PUBLISHED_DIR
    PUB.PUBLISHED_DIR = os.path.join(tmp, "published")
    try:
        clips = {}
        for name in ("a", "b", "c"):
            p = os.path.join(tmp, "clip_%s.mp4" % name)
            _make_clip(p)
            assert count_frames(p) == 12, "test clip %s is not 12 frames" % name
        for name in ("a", "b", "c"):
            clips[name] = os.path.join(tmp, "clip_%s.mp4" % name)
        print("self-test: 3 color-bar clips of 12 frames each generated")

        # CANARY: CUES FOLLOW THE FILE, NOT THE FIELD. The clips are 12 frames.
        # The shots LIE and claim 121, which is exactly the real defect: F017
        # made length a trim clamped to 81 native frames, so sg_gen_frames
        # became an intent while captions still timed off it. On the live demo
        # cut that put cues at 16.687s against an 11.25s picture and Geoff saw
        # the captions drift out of sync with the cuts.
        _liar = [({"code": "LIE_%s" % n, "sg_gen_frames": 121,
                   "sg_caption_text": "line for %s" % n},
                  {"sg_path_to_movie": clips[n]}) for n in ("a", "b", "c")]
        # And the REAL defect, which the 12-frame clips above cannot show
        # because they are already at the caption timeline's rate: a clip whose
        # FILE RATE differs from the caption timeline. The live clips are 24fps
        # while captions count at 16, so 121 frames read as 7.562s for a clip
        # that is 5.042s long. Counting frames instead of the field fixed
        # nothing, because both were divided by the same wrong rate. SECONDS
        # are the only fps-independent fact.
        _odd = os.path.join(tmp, "clip_24fps.mp4")
        subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "smptebars=size=320x180:rate=24",
                        "-frames:v", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        _odd], check=True)
        _mf = measured_frames(_odd)
        assert _mf is not None and abs(_mf - CAP.FPS) <= 1, (
            "a 1.000s clip at 24fps measured %s caption-frames; expected about "
            "%s. Duration is being read as a frame COUNT again." % (_mf, CAP.FPS))
        print("self-test: CANARY, a 1s clip at 24fps measures %d caption-frames "
              "at %d fps, not its 24-frame count" % (_mf, CAP.FPS))

        _cues = caption_cues(_liar)
        _end_ms = max(b for _a, b, _t in _cues)
        _true_ms = 3 * 12 / float(FPS) * 1000.0
        _field_ms = 3 * 121 / float(FPS) * 1000.0
        assert abs(_end_ms - _true_ms) < 50, (
            "cues end at %.0fms; the FILES are %.0fms and the lying FIELD says "
            "%.0fms. Captions are timing off the field again."
            % (_end_ms, _true_ms, _field_ms))
        print("self-test: CANARY, cues timed off the 12-frame FILES (%.0fms), "
              "not the 121-frame FIELD (%.0fms)" % (_end_ms, _field_ms))

        # Shots deliberately listed OUT of cut order; fetch_shots must fix it.
        shots = [{"type": "Shot", "id": 3, "code": "SEQT_SH030", "sg_cut_order": 30},
                 {"type": "Shot", "id": 1, "code": "SEQT_SH010", "sg_cut_order": 10},
                 {"type": "Shot", "id": 2, "code": "SEQT_SH020", "sg_cut_order": 20}]
        versions = {
            # SH010: newer PROVISIONAL must NOT shadow the older approval.
            1: [{"id": 101, "code": "SEQT_SH010_CMP_gen_v001", "sg_status_list": "apr",
                 "sg_path_to_movie": clips["a"], "created_at": 1},
                {"id": 102, "code": "SEQT_SH010_CMP_gen_v002", "sg_status_list": "appcbb",
                 "sg_path_to_movie": clips["a"], "created_at": 2}],
            2: [{"id": 201, "code": "SEQT_SH020_CMP_gen_v001", "sg_status_list": "apr",
                 "sg_path_to_movie": clips["b"], "created_at": 1}],
            3: [{"id": 301, "code": "SEQT_SH030_CMP_gen_v001", "sg_status_list": "fin",
                 "sg_path_to_movie": clips["c"], "created_at": 1}],
        }
        # A cut ALREADY APPROVED on the Sequence entity (id 55), which is what
        # the second and every later cut has to supersede.
        versions[55] = [
            {"id": 5501, "code": "SEQT_cut_v000", "sg_status_list": "apr",
             "sg_stage": "episode", "sg_path_to_movie": None, "created_at": 1},
            {"id": 5502, "code": "SEQT_cut_reject", "sg_status_list": "rjct",
             "sg_stage": "episode", "sg_path_to_movie": None, "created_at": 1},
        ]
        sg = _MockSG(shots, versions, [{"type": "Sequence", "id": 55, "code": "SEQT"}])

        ordered = fetch_shots(sg, project, "SEQT")
        assert [s["code"] for s in ordered] == ["SEQT_SH010", "SEQT_SH020", "SEQT_SH030"], \
            "cut order sort failed: %r" % [s["code"] for s in ordered]
        print("self-test: shots sorted into cut order by sg_cut_order")

        # --shots: an explicit list, so a partial scene can be cut out of a
        # sequence whose other shots are not finished. Without it the only
        # scopes were a whole Sequence or the whole project, and a sequence
        # with one unapproved shot refuses entirely (correctly).
        picked3 = fetch_shots(sg, project, None, ["SEQT_SH030", "SEQT_SH010"])
        got3 = [x["code"] for x in picked3]
        assert got3 == ["SEQT_SH010", "SEQT_SH030"], ("--shots must return CUT ORDER, not the order the codes were typed: %r" % got3)
        print("self-test: --shots selects a subset and re-sorts it into cut order")
        assert len(fetch_shots(sg, project, None, ["SEQT_SH010"])) == 1
        assert len(fetch_shots(sg, project, None)) == 3, "no shot list must still mean the whole scope"
        print("self-test: a one-shot list works, and no list still means everything")

        picked = latest_approved_version(sg, {"id": 1})
        assert picked["code"] == "SEQT_SH010_CMP_gen_v001", \
            "PROVISIONAL appcbb SHADOWED THE APPROVAL: picked %r" % picked["code"]
        print("self-test: newer appcbb did not shadow the older apr")

        out, problems = assemble(sg, "SEQT", ordered, project, out_dir, led_dir,
                                 publish=True)
        assert not problems, "unexpected refusal: %r" % problems
        got = count_frames(out)
        assert got == 3 * 12, "concat frame count wrong: expected 36, got %r" % got
        print("self-test: concat of 3x12-frame clips = 36 frames (ffprobe counted)")
        assert sg.created and sg.created[0]["sg_status_list"] == "rev"
        assert sg.created[0]["entity"] == {"type": "Sequence", "id": 55}
        assert sg.uploaded and sg.uploaded[0][3] == "sg_uploaded_movie"
        print("self-test: Version published on Sequence at status rev (mock)")
        # CANARY: remove stage="episode" above and this fails. A stage-less
        # cut is swept to rjct by sg_review_housekeeping, never reviewed.
        # CANARY, INVERTED 2026-09-06, and the old version of it encoded the
        # bug. It asserted that publishing a cut REJECTS every approved cut on
        # the same entity, which is exactly the overreach Geoff called out: a
        # cut is not one of a batch of alternates, so nothing about publishing
        # one should un-approve another. Two cuts on one entity are usually two
        # different SCENES. The sweeper no longer touches this stage either.
        assert not any(u[1] in (5501, 5502) for u in sg.updated),             ("publishing a cut MODIFIED another cut on the same entity: %r. "
             "Approving or publishing one cut must never touch another."
             % [u for u in sg.updated if u[1] in (5501, 5502)])
        print("self-test: CANARY, publishing a cut left the prior approved cut "
              "and the rejected one alone")

        got_stage = sg.created[0].get("sg_stage")
        assert got_stage == "episode", ("cut published with sg_stage=%r: housekeeping will OMIT it" % got_stage)
        print("self-test: cut carries sg_stage=episode, housekeeping keeps it")
        led = RL.load(os.path.join(led_dir, "episode_SEQT.json"))
        cut = next(e for e in led["entries"] if e["kind"] == "episode_cut")
        assert cut["shots"] == ["SEQT_SH010", "SEQT_SH020", "SEQT_SH030"], \
            "ledger recorded wrong shot order: %r" % cut["shots"]
        print("self-test: ledger records the cut with shots in order")

        out2, problems = assemble(sg, "SEQT", ordered, project, out_dir, led_dir,
                                  slate=True, publish=False)
        assert not problems, "slate pass refused: %r" % problems
        assert os.path.basename(out2) == "SEQT_cut_v002.mp4", \
            "versioning failed: %r" % out2
        assert os.path.exists(out), "v001 was clobbered by v002"
        got2 = count_frames(out2)
        assert got2 == 36 + FPS * SLATE_SECONDS, \
            "slate cut frame count wrong: expected 84, got %r" % got2
        print("self-test: v002 written (v001 untouched), 2s slate = 84 frames total")

        # CANARY: a bad publish target must refuse BEFORE anything is written.
        # If this ever writes NOSUCH_cut_v001.mp4 or a ledger, the "nothing
        # was written" refusal contract is broken again.
        out_ns, problems = assemble(sg, "NOSUCH", ordered, project, out_dir, led_dir,
                                    publish=True)
        assert out_ns is None and problems and "NOSUCH" in problems[0], \
            "unknown Sequence did not refuse: %r" % problems
        assert not os.path.exists(os.path.join(out_dir, "NOSUCH_cut_v001.mp4")), \
            "refused publish still wrote a cut file"
        assert not os.path.exists(os.path.join(led_dir, "episode_NOSUCH.json")), \
            "refused publish still wrote a ledger"
        print("self-test: unknown Sequence refused before any file or ledger write")

        # CANARY: the refusal path must actually fire and must name the shot.
        versions[2][0]["sg_path_to_movie"] = os.path.join(tmp, "DOES_NOT_EXIST.mp4")
        versions[3] = [{"id": 302, "code": "SEQT_SH030_CMP_gen_v001",
                        "sg_status_list": "rev",
                        "sg_path_to_movie": clips["c"], "created_at": 1}]
        out3, problems = assemble(sg, "SEQT", ordered, project, out_dir, led_dir,
                                  publish=False)
        assert out3 is None, "REFUSAL DID NOT FIRE: got output %r" % out3
        blob = "\n".join(problems)
        assert "SEQT_SH020" in blob, "missing-file refusal did not name the shot: %r" % problems
        assert "SEQT_SH030" in blob, "unapproved refusal did not name the shot: %r" % problems
        assert not os.path.exists(os.path.join(out_dir, "SEQT_cut_v003.mp4")), \
            "refused assembly still wrote a file"
        print("self-test: missing movie and unapproved shot both REFUSED, by name:")
        for p in problems:
            print("    " + p)
        print("self-test PASSED")
        return 0
    finally:
        PUB.PUBLISHED_DIR = saved_published_dir
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Assemble approved Versions into an episode cut.")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--sequence", help="Sequence code to assemble")
    scope.add_argument("--project-wide", action="store_true",
                       help="assemble every Shot in the project")
    scope.add_argument("--shots", metavar="CODES",
                       help="comma-separated Shot codes to assemble, in cut "
                            "order; for a partial scene whose sequence is not "
                            "finished")
    ap.add_argument("--label", metavar="NAME",
                    help="name for a --shots cut (default SCENE); becomes the "
                         "cut code and the ledger key")
    ap.add_argument("--audio", help="WAV bed laid under the picture")
    ap.add_argument("--slate", action="store_true", help="2s slate up front")
    ap.add_argument("--no-publish", action="store_true",
                    help="write the file but create no ShotGrid Version")
    ap.add_argument("--no-captions", action="store_true",
                    help="opt out of burning in sg_caption_text (on by default "
                        "whenever a picked shot carries caption text)")
    ap.add_argument("--self-test", action="store_true",
                    help="mocked end-to-end test, no ShotGrid, no GPU")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.sequence and not args.project_wide and not args.shots:
        ap.error("one of --sequence, --shots or --project-wide is required")

    shot_codes = None
    if args.shots:
        shot_codes = [c.strip() for c in args.shots.split(",") if c.strip()]
        if not shot_codes:
            ap.error("--shots was given but parsed to no shot codes")
    if args.sequence:
        label = args.sequence
    elif shot_codes:
        label = args.label or "SCENE"
    else:
        label = "PROJECTWIDE"
    sg = sg_connect()
    project = {"type": "Project", "id": PROJECT_ID}
    shots = fetch_shots(sg, project, args.sequence, shot_codes)
    if shot_codes:
        found = {sh["code"] for sh in shots}
        missing = [c for c in shot_codes if c not in found]
        if missing:
            # Naming them beats cutting a short scene and never saying why.
            log("REFUSED: --shots named %d code(s) that do not exist in this "
                "project: %s" % (len(missing), ", ".join(missing)))
            return 2
    log("shots in scope: %d" % len(shots))
    if not shots:
        log("REFUSED: no shots found for %r" % label)
        return 2
    entity_override = None
    if shot_codes and not args.no_publish:
        # PREFER THE SEQUENCE. Geoff, 2026-09-06: "the better solution is to
        # publish sequences to sequence entity and not episode entity."
        #
        # The old code went straight to the Episode on the grounds that "a
        # --shots cut has no single Sequence". Usually it does: a hand-picked
        # run of shots is nearly always inside one Sequence, and it was never
        # checked. Piling every ad-hoc cut onto the Episode is what put two
        # unrelated scenes on one entity.
        rows = sg.find("Shot", [["id", "in", [sh["id"] for sh in shots]]],
                       ["code", "sg_sequence", "sg_episode"]) or []
        seq_ids = set()
        for r in rows:
            sq = r.get("sg_sequence")
            seq_ids.add(sq["id"] if sq else None)
        if len(seq_ids) == 1 and None not in seq_ids:
            sid = seq_ids.pop()
            entity_override = {"type": "Sequence", "id": sid}
            name = next((r["sg_sequence"].get("name") for r in rows
                         if r.get("sg_sequence")), sid)
            log("publishing on Sequence %s (all %d shot(s) share it)" % (name, len(rows)))
        else:
            ep = next((r.get("sg_episode") for r in rows if r.get("sg_episode")), None)
            if not ep:
                log("REFUSED: these shots span %d sequence(s) and none carries an "
                    "sg_episode to fall back to. Re-run with --no-publish, or link "
                    "them." % len(seq_ids))
                return 2
            entity_override = {"type": "Episode", "id": ep["id"]}
            log("publishing on Episode %s: the shots span %d sequences, so there is "
                "no single Sequence to own this cut" % (ep.get("name"), len(seq_ids)))

    out, problems = assemble(sg, label, shots, project, OUT_EPISODES, LEDGER_DIR,
                             entity_override=entity_override,
                             audio=args.audio, slate=args.slate,
                             publish=not args.no_publish,
                             project_wide=args.project_wide,
                             captions=not args.no_captions)
    if problems:
        log("REFUSED - fix these and rerun (no partial cut was written):")
        for p in problems:
            log("  " + p)
        return 2
    log("episode cut written: %s (%s frames)" % (out, count_frames(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
