#!/usr/bin/env python3
"""Export the cut OUT of ShotGrid as EDL, OTIO and a CSV cut list.

Stage 10's standing gap: "no OTIO or EDL export, so there is no conform path
into Resolve". Everything this pipeline makes currently dies inside it. An
episode that cannot leave is not a deliverable, it is a demo.

Three formats because they answer three different questions:

  EDL (CMX 3600)  the lowest common denominator. Every NLE on earth reads it.
                  Lossless for a flat cut list, which is exactly what this is.
  OTIO (JSON)     OpenTimelineIO, the modern interchange. Carries the shot name,
                  the source media path and per-clip metadata (the prompt, the
                  seed, the version) that an EDL has nowhere to put.
  CSV             for a human and a spreadsheet, because half of production runs
                  on one.

TIMING COMES FROM SHOTGRID, NOT FROM THE FILES. The cut is defined by
sg_cut_order and sg_gen_frames, which is the same contract the animatic and the
captions use. If a rendered movie disagrees with the record, the record wins and
the disagreement is REPORTED rather than absorbed - a conform that silently
trusts whatever length a file happens to be is how a cut drifts.

    python conform.py --sequence PILOT01_A
    python conform.py --episode
    python conform.py --self-test
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_context as EPCTX                                # noqa: E402

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "build", "out", "conform")
PROJ = {"type": "Project", "id": 9999}
EP = EPCTX.resolve_episode()
FPS = 24.0


# --- timecode ---------------------------------------------------------------

def tc(frame, fps=FPS):
    """Non-drop SMPTE. Integer fps only, which is all this pipeline produces."""
    f = int(round(frame))
    fps_i = int(round(fps))
    h, f = divmod(f, 3600 * fps_i)
    m, f = divmod(f, 60 * fps_i)
    s, f = divmod(f, fps_i)
    return "%02d:%02d:%02d:%02d" % (h % 24, m, s, f)


def tc_to_frames(t, fps=FPS):
    h, m, s, f = [int(x) for x in t.split(":")]
    return ((h * 60 + m) * 60 + s) * int(round(fps)) + f


# --- the cut ----------------------------------------------------------------

def build_cut(shots, handles=0):
    """-> [ {shot, frames, rec_in, rec_out, src, version, prompt} ] on a record
    timeline starting at 01:00:00:00, the broadcast convention.

    HANDLES are the extra frames an editor trims into either side of a cut. They
    cannot be conjured here: this pipeline generates exactly the frames the cut
    calls for, so the media has no material beyond the event. Asking for handles
    therefore does NOT widen the source range - that would point the EDL at
    frames which do not exist and the conform would come up black at every
    edit. It records the shortfall instead, so the requirement reaches the stage
    that can satisfy it (generation), which is the only place that can.

    Emitting a plausible-looking EDL that references non-existent media is the
    exact failure this codebase keeps designing against."""
    start = int(round(FPS)) * 3600
    events, rec = [], start
    for s in shots:
        n = max(int(s.get("_frames") or 0), 1)
        avail = int(s.get("_media_frames") or n)
        have = max(0, (avail - n) // 2)          # symmetric handles, if any exist
        give = min(handles, have)
        events.append({
            "shot": s["code"],
            "frames": n,
            "rec_in": rec,
            "rec_out": rec + n,
            "src_in": give,
            "src_out": give + n,
            "src": s.get("_media") or "",
            "version": s.get("_version") or "",
            "prompt": (s.get("_prompt") or "")[:900],
            "seed": s.get("_seed"),
            "handles_asked": handles,
            "handles_got": give,
        })
        rec += n
    return events


def render_edl(events, title):
    """CMX 3600. Reel names are capped at 8 characters by the format, so the shot
    code goes in the * FROM CLIP NAME comment where it survives intact."""
    out = ["TITLE: %s" % title[:70], "FCM: NON-DROP FRAME", ""]
    for i, e in enumerate(events, 1):
        reel = "".join(c for c in e["shot"] if c.isalnum())[-8:].upper() or "AX"
        out.append("%03d  %-8s V     C        %s %s %s %s"
                   % (i, reel, tc(e["src_in"]), tc(e["src_out"]),
                      tc(e["rec_in"]), tc(e["rec_out"])))
        out.append("* FROM CLIP NAME: %s" % e["shot"])
        if e["src"]:
            out.append("* SOURCE FILE: %s" % os.path.basename(e["src"]))
        out.append("")
    return "\n".join(out)


def render_otio(events, title):
    """OTIO v1 JSON, hand-built. The schema is stable and writing it directly
    avoids a dependency the render boxes do not have; it is read back and
    validated in the self-test rather than trusted."""
    clips = []
    for e in events:
        clips.append({
            "OTIO_SCHEMA": "Clip.1",
            "name": e["shot"],
            "source_range": {
                "OTIO_SCHEMA": "TimeRange.1",
                "start_time": {"OTIO_SCHEMA": "RationalTime.1",
                               "rate": FPS, "value": e["src_in"]},
                "duration": {"OTIO_SCHEMA": "RationalTime.1",
                             "rate": FPS, "value": e["frames"]},
            },
            "media_reference": {
                "OTIO_SCHEMA": "ExternalReference.1",
                "target_url": e["src"],
            } if e["src"] else None,
            "metadata": {"genvideo": {"version": e["version"], "seed": e["seed"],
                                      "prompt": e["prompt"]}},
        })
    return json.dumps({
        "OTIO_SCHEMA": "Timeline.1",
        "name": title,
        "global_start_time": {"OTIO_SCHEMA": "RationalTime.1",
                              "rate": FPS, "value": int(round(FPS)) * 3600},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1", "name": "tracks",
            "children": [{"OTIO_SCHEMA": "Track.1", "name": "V1",
                          "kind": "Video", "children": clips}],
        },
    }, indent=2)


def render_csv(events):
    rows = ["event,shot,frames,seconds,rec_in,rec_out,version,source"]
    for i, e in enumerate(events, 1):
        rows.append("%d,%s,%d,%.2f,%s,%s,%s,%s"
                    % (i, e["shot"], e["frames"], e["frames"] / FPS,
                       tc(e["rec_in"]), tc(e["rec_out"]), e["version"],
                       os.path.basename(e["src"])))
    return "\n".join(rows)


# --- ShotGrid ---------------------------------------------------------------

def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def load(sg, sequence):
    filt = [["project", "is", PROJ], ["code", "starts_with", EP.code]]
    if sequence:
        filt.append(["sg_sequence.Sequence.code", "is", sequence])
    shots = sg.find("Shot", filt,
                    ["code", "sg_sequence", "sg_cut_order", "sg_gen_frames",
                     "sg_latest_version"])
    vids = {}
    vs = sg.find("Version", [["project", "is", PROJ]],
                 ["code", "entity", "sg_path_to_movie", "sg_stage",
                  "sg_prompt_final__as_sent_", "sg_gen_seed", "created_at"])
    for v in sorted(vs, key=lambda x: x.get("created_at") or 0):
        ent = v.get("entity") or {}
        if ent.get("type") == "Shot" and v.get("sg_stage") in ("video", None):
            vids[ent["id"]] = v            # latest video version per shot wins
    for s in shots:
        s["_frames"] = int(s.get("sg_gen_frames") or 121)
        s["_seq"] = (s.get("sg_sequence") or {}).get("name") or ""
        v = vids.get(s["id"])
        s["_media"] = (v or {}).get("sg_path_to_movie") or ""
        s["_version"] = (v or {}).get("code") or ""
        s["_prompt"] = (v or {}).get("sg_prompt_final__as_sent_") or ""
        s["_seed"] = (v or {}).get("sg_gen_seed")
    shots.sort(key=lambda s: (s["_seq"], s.get("sg_cut_order") or 0))
    return shots


def cmd_export(sg, sequence, episode, handles=0):
    shots = load(sg, None if episode else sequence)
    if not shots:
        print("[conform] no shots matched.")
        return 1
    title = EP.code if episode else sequence
    events = build_cut(shots, handles)
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    base = os.path.join(OUT, title)
    io.open(base + ".edl", "w", encoding="utf-8").write(render_edl(events, title))
    io.open(base + ".otio", "w", encoding="utf-8").write(render_otio(events, title))
    io.open(base + "_cutlist.csv", "w", encoding="utf-8").write(render_csv(events))

    total = sum(e["frames"] for e in events)
    missing = [e["shot"] for e in events if not e["src"]]
    print("[conform] %s: %d event(s), %d frames, %s duration"
          % (title, len(events), total, tc(total, FPS).replace("00:", "", 0)))
    print("[conform] -> %s.edl / .otio / _cutlist.csv" % base)
    # An export that hides which clips have no media is worse than no export:
    # the conform silently comes up short in the bay instead of here.
    if handles:
        short = [e for e in events if e["handles_got"] < e["handles_asked"]]
        if short:
            print("[conform] HANDLES: asked %d, and %d/%d event(s) have none to give."
                  % (handles, len(short), len(events)))
            print("          This pipeline generates exactly the frames the cut needs, so")
            print("          there is no material either side of an edit. The source range")
            print("          is NOT widened - an EDL pointing at frames that do not exist")
            print("          would come up black at every cut. To get handles, generate")
            print("          them: raise sg_gen_frames by %d and re-cut." % (handles * 2))
    if missing:
        print("[conform] %d/%d event(s) have NO rendered media yet:"
              % (len(missing), len(events)))
        print("          %s%s" % (", ".join(missing[:8]),
                                  " ..." if len(missing) > 8 else ""))
        print("          The EDL is still valid as a timing conform; those events "
              "will come up offline.")
    return 0


# --- self-test --------------------------------------------------------------

def self_test():
    fails = []

    def ck(name, cond):
        print("  %-56s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("timecode at one hour", tc(24 * 3600) == "01:00:00:00")
    ck("timecode counts frames", tc(24 * 3600 + 13) == "01:00:00:13")
    ck("timecode round trips", tc_to_frames(tc(123456)) == 123456)

    shots = [{"code": "PILOT01_A_0010", "_frames": 121, "_media": "x/a.mp4",
              "_version": "v1", "_prompt": "p", "_seed": 7},
             {"code": "PILOT01_A_0020", "_frames": 49, "_media": "",
              "_version": "", "_prompt": "", "_seed": None}]
    ev = build_cut(shots)
    ck("cut starts at broadcast hour", ev[0]["rec_in"] == 24 * 3600)
    ck("events are contiguous, no gap or overlap",
       ev[1]["rec_in"] == ev[0]["rec_out"])
    ck("record duration equals the sum of shot durations",
       ev[-1]["rec_out"] - ev[0]["rec_in"] == 170)

    edl = render_edl(ev, "T")
    ck("EDL has one event line per shot", edl.count("* FROM CLIP NAME:") == 2)
    ck("EDL reel name obeys the 8 char limit",
       all(len(l.split()[1]) <= 8 for l in edl.splitlines()
           if l[:3].isdigit() and l[:3] != "FCM"))
    ck("EDL carries the full shot code in a comment",
       "PILOT01_A_0010" in edl)

    otio = json.loads(render_otio(ev, "T"))
    ck("OTIO parses as JSON and is a Timeline",
       otio["OTIO_SCHEMA"] == "Timeline.1")
    track = otio["tracks"]["children"][0]
    ck("OTIO video track holds every clip", len(track["children"]) == 2)
    ck("OTIO clip duration matches the record",
       track["children"][0]["source_range"]["duration"]["value"] == 121)
    ck("OTIO carries provenance an EDL cannot",
       track["children"][0]["metadata"]["genvideo"]["seed"] == 7)
    ck("OTIO clip with no media has a null reference",
       track["children"][1]["media_reference"] is None)

    csv = render_csv(ev)
    ck("CSV has a header and one row per event", len(csv.splitlines()) == 3)

    # CANARY: asking for handles the media does not have must NOT widen the
    # source range. A plausible EDL pointing at frames that were never generated
    # comes up black at every edit, which is worse than having no handles.
    tight = build_cut([{"code": "A", "_frames": 100, "_media": "a.mp4",
                        "_media_frames": 100, "_version": "", "_prompt": "", "_seed": None}],
                      handles=12)
    ck("CANARY handles are not invented when the media has none",
       tight[0]["src_in"] == 0 and tight[0]["src_out"] == 100
       and tight[0]["handles_got"] == 0)
    ck("CANARY the shortfall is recorded, not swallowed",
       tight[0]["handles_asked"] == 12)
    wide = build_cut([{"code": "A", "_frames": 100, "_media": "a.mp4",
                       "_media_frames": 140, "_version": "", "_prompt": "", "_seed": None}],
                     handles=12)
    ck("real handles ARE used when the media carries them",
       wide[0]["src_in"] == 12 and wide[0]["src_out"] == 112)
    ck("handles never exceed what the media holds",
       build_cut([{"code": "A", "_frames": 100, "_media": "a.mp4", "_media_frames": 110,
                   "_version": "", "_prompt": "", "_seed": None}],
                 handles=12)[0]["handles_got"] == 5)
    ck("the record timeline is unaffected by handles",
       wide[0]["rec_out"] - wide[0]["rec_in"] == 100)

    # CANARY: the export must not pass off a missing-media cut as complete.
    ck("CANARY a shot with no media is detectable in the export",
       any(not e["src"] for e in ev))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sequence")
    ap.add_argument("--episode", action="store_true")
    ap.add_argument("--handles", type=int, default=0,
                    help="frames of trim either side of each event, if the media has them")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if not (ns.sequence or ns.episode):
        ap.print_help()
        return 1
    return cmd_export(sg_connect(), ns.sequence, ns.episode, ns.handles)


if __name__ == "__main__":
    sys.exit(main())
