#!/usr/bin/env python3
"""Connect the SCRIPT to the SHOTS, in ShotGrid, and cut subtitles from that.

Two gaps close here, and they are really the same gap:

  0/124 shots carry the line they are meant to play  (Stage 1)
  0/124 shots carry sg_caption_text                  (Stage 12)

Geoff asked for subtitles until real voice recordings exist, so captions are on
the critical path. But a caption is only honest if it is attached to the shot
that actually plays the line, and until now the script and the shot list were
two unconnected artifacts joined by nothing but the order they were written in.

THE JOIN KEY. Not a guess: every scene heading in the script names a set, and
every shot links the matching STYLE_ asset that set was built as.

    INT. CHARB'S FLAT      -> STYLE_CHARB_FLAT
    INT. TENEMENT BACKDROP  -> STYLE_TENEMENT_CHAT
    INT. LOBBY              -> STYLE_LOBBY
    INT. HALLWAY            -> STYLE_HALLWAY

A scene in the script therefore corresponds to a contiguous RUN of shots
carrying that style, in cut order. Cutaway styles (the ledge, inserts, the title
card) are not sets of their own and never break a run; they inherit the scene
they are cut into, which is what a cutaway is.

Within a scene, lines are distributed across the run by DURATION, not by shot
count, so a six second shot can hold a long line and a one second insert does
not get one. A shot linking the speaker's CHAR_ asset wins the line over one
that does not, which is how a reverse gets the right side of a conversation.

This is an alignment, not an oracle. It will be wrong somewhere. That is exactly
why it writes sg_caption_text into ShotGrid rather than into a file: the place
it is wrong is the place a human can fix it, in the interface, and the fix
survives because the next SRT is cut from ShotGrid state, not from this script.

    python captions_sg.py --align          parse script, write captions to ShotGrid
    python captions_sg.py --srt PILOT01_A   cut SRT/VTT for a sequence FROM ShotGrid
    python captions_sg.py --srt-episode    cut the whole episode
    python captions_sg.py --self-test      alignment logic against fixtures
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_context as EPCTX                                # noqa: E402

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EP = EPCTX.resolve_episode()
# Falls back to the original hardcoded PILOT-SCRIPT.md path if the registry
# entry (or the built-in default) does not name one -- keeps this identical
# for PILOT01 even if episodes.json is ever edited incompletely.
SCRIPT = EP.script or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "creative", "PILOT-SCRIPT.md")
OUT = os.path.join(ROOT, "build", "out", "captions")
PROJ = {"type": "Project", "id": 9999}
import timeline as _TL          # invariant 11: ONE timeline fps
FPS = _TL.FPS                   # was a hardcoded 24.0

# Scene heading fragment -> the STYLE_ asset that set is built as.
SET_STYLE = [
    ("CHARB", "STYLE_CHARB_FLAT"),
    ("TENEMENT BACKDROP", "STYLE_TENEMENT_CHAT"),
    ("LOBBY", "STYLE_LOBBY"),
    ("HALLWAY", "STYLE_HALLWAY"),
]
# Styles that are a cut inside a scene, not a scene of their own.
CUTAWAY = {"STYLE_LEDGE", "STYLE_INSERTS", "STYLE_TITLE_CARD"}

SPEAKER_ASSET = {
    "CHARB": "CHAR_CHARB", "CHARJ": "CHAR_CHARJ", "CHARL": "CHAR_CHARL",
    "CHARC": "CHAR_CHARC", "CHARF": "CHAR_CHARF",
    "MRS CHARF": "CHAR_CHARF", "CHARK": "CHAR_CHARK",
    "CHARD ONE": "CHAR_CHARD_TWINS", "CHARD TWO": "CHAR_CHARD_TWINS",
}

# A speaker cue: ALLCAPS name, optionally "(TEXT-VOICE)" / "(V.O.)" / "(O.S.)".
CUE = re.compile(r"^([A-Z][A-Z0-9 '\.\-]{1,28}?)(?:\s*\(([^)]*)\))?\s*$")

# Headings in the appendix that are not screenplay.
APPENDIX = ("CAST", "SET LIST", "PRODUCTION")

# "(est. 1:30)" in a scene heading: the authored duration of that scene.
EST = re.compile(r"est\.?\s*(\d+):(\d{2})", re.I)


def scene_est(heading):
    m = EST.search(heading or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 0


def scene_style(heading):
    up = (heading or "").upper()
    for frag, style in SET_STYLE:
        if frag in up:
            return style
    return None


# --- 1. the script ----------------------------------------------------------

def parse_script(path):
    """-> [ {heading, style, act, beats:[{speaker, mode, text}]} ] in script order.

    A beat is a SPOKEN line. Stage directions (*italic*) are deliberately not
    captions: they are not spoken, and a subtitle track that narrates its own
    blocking is worse than no subtitle track at all."""
    if not os.path.exists(path):
        raise SystemExit("script not found: %s" % path)
    lines = io.open(path, encoding="utf-8").read().splitlines()
    scenes, act, cur = [], "", None
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("## "):
            act = ln[3:].strip()
            if act.upper().startswith(APPENDIX):
                break
            i += 1
            continue
        if ln.startswith("### "):
            heading = ln[4:].strip()
            cur = {"heading": heading, "style": scene_style(heading),
                   "act": act, "beats": [], "est": scene_est(heading)}
            scenes.append(cur)
            i += 1
            continue
        m = CUE.match(ln) if (cur and ln.strip() and not ln.startswith("*")) else None
        if m:
            speaker = m.group(1).strip()
            mode = (m.group(2) or "").strip()
            j, buf = i + 1, []
            while j < len(lines):
                nxt = lines[j].rstrip()
                if not nxt.strip():
                    break
                if nxt.lstrip().startswith("*"):      # parenthetical or direction
                    j += 1
                    continue
                buf.append(nxt.strip())
                j += 1
            text = " ".join(buf).strip()
            if text and len(speaker) > 1:
                cur["beats"].append({"speaker": speaker, "mode": mode, "text": text})
            i = j
            continue
        i += 1
    return [s for s in scenes if s["beats"] or s["style"]]


# --- 2. the shots -----------------------------------------------------------

def shot_runs(shots):
    """Segment shots (already in cut order) into contiguous scene runs by primary
    style. A cutaway inherits the run it sits in and never starts one."""
    runs, cur = [], None
    for s in shots:
        styles = [a for a in s["_styles"] if a not in CUTAWAY]
        primary = styles[0] if styles else None
        if primary is None:
            if cur is not None:
                cur["shots"].append(s)
                continue
            primary = "UNSET"
        if cur is None or cur["style"] != primary:
            cur = {"style": primary, "shots": [s]}
            runs.append(cur)
        else:
            cur["shots"].append(s)
    return runs


def split_run(run, scenes):
    """One shot run, several consecutive scenes in the SAME set: split it.

    Act three is three scenes that all play in CharB's flat (DAY, EVENING, LATE
    NIGHT). The set never changes, so the shots form one unbroken 32-shot run
    that no single scene can claim, and a style-only match drops two thirds of
    the act on the floor.

    The script already carries the tie-breaker: every heading is stamped with an
    est. duration. Split the run in proportion to those, which is the same
    contract the animatic uses - authored timing decides, and generation
    conforms to it."""
    shots = run["shots"]
    weights = [max(s.get("est", 0) or 0, 1) for s in scenes]
    total_w = float(sum(weights))
    total_f = sum(max(s["_frames"], 1) for s in shots) or 1
    out, idx, acc = [], 0, 0.0
    for k, sc in enumerate(scenes):
        acc += weights[k] / total_w
        # last scene always takes the remainder, so no shot is ever lost to rounding
        stop = len(shots) if k == len(scenes) - 1 else idx
        if k < len(scenes) - 1:
            seen = 0
            stop = idx
            for j in range(idx, len(shots)):
                seen += max(shots[j]["_frames"], 1)
                stop = j + 1
                if (sum(max(x["_frames"], 1) for x in shots[:idx]) + seen) / total_f >= acc:
                    break
            stop = max(stop, idx + 1) if idx < len(shots) else idx
        piece = shots[idx:stop]
        if piece:
            out.append((sc, {"style": run["style"], "shots": piece}))
        idx = stop
    return out


def align(scenes, runs):
    """Walk both in order, matching on style. Returns the pairs and, just as
    importantly, what did NOT match. An alignment that silently drops half the
    script while reporting success is the failure mode worth engineering against.

    Consecutive scenes sharing a set are matched as a CLUSTER to one run and then
    split by authored duration; see split_run."""
    pairs, unmatched, used = [], [], set()
    ri = 0
    # group consecutive same-style scenes into clusters
    clusters = []
    for sc in scenes:
        if clusters and sc["style"] and clusters[-1][0]["style"] == sc["style"]:
            clusters[-1].append(sc)
        else:
            clusters.append([sc])
    for group in clusters:
        style = group[0]["style"]
        if not style:
            for sc in group:
                unmatched.append((sc, "no set style in heading"))
            continue
        hit = None
        for k in range(ri, len(runs)):
            if runs[k]["style"] == style and k not in used:
                hit = k
                break
        if hit is None:
            for sc in group:
                unmatched.append((sc, "no unused %s run at or after cut position" % style))
            continue
        if len(group) == 1:
            pairs.append((group[0], runs[hit]))
        else:
            pairs.extend(split_run(runs[hit], group))
        used.add(hit)
        ri = hit + 1
    orphans = [r for k, r in enumerate(runs) if k not in used]
    return pairs, unmatched, orphans


def assign_beats(scene, run):
    """Distribute a scene's lines over its shots by DURATION share, preferring a
    shot that links the speaker's character asset."""
    shots, beats = run["shots"], scene["beats"]
    out = dict((s["id"], []) for s in shots)
    if not beats or not shots:
        return out
    total = sum(max(s["_frames"], 1) for s in shots)
    pos, acc = [], 0
    for s in shots:
        f = max(s["_frames"], 1)
        pos.append((acc + f / 2.0) / total)
        acc += f
    for bi, b in enumerate(beats):
        want = (bi + 0.5) / len(beats)
        char = SPEAKER_ASSET.get(b["speaker"].split("(")[0].strip().upper())
        best, best_score = 0, None
        for k, s in enumerate(shots):
            score = abs(pos[k] - want)
            if char and char in s["_chars"]:
                score -= 0.15                 # the speaker is on screen
            if len(out[s["id"]]) >= 2:
                score += 0.10                 # spread rather than stack
            if best_score is None or score < best_score:
                best, best_score = k, score
        out[shots[best]["id"]].append(b)
    return out


# --- 3. SRT cut from ShotGrid state ----------------------------------------

def tc(ms, sep=","):
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d%s%03d" % (h, m, s, sep, ms)


def cues_from_shots(shots):
    """Cues on the real timeline: each shot occupies its own duration in cut
    order, and its caption splits that window between its lines."""
    cues, t = [], 0.0
    for s in shots:
        dur = max(s["_frames"], 1) / FPS * 1000.0
        text = (s.get("sg_caption_text") or "").strip()
        if text:
            parts = [p for p in text.split("\n") if p.strip()]
            share = dur / max(len(parts), 1)
            for k, p in enumerate(parts):
                cues.append((t + k * share, t + (k + 1) * share, p.strip()))
        t += dur
    return cues


def render(cues, vtt=False):
    out = ["WEBVTT", ""] if vtt else []
    sep = "." if vtt else ","
    for i, (a, b, txt) in enumerate(cues, 1):
        if not vtt:
            out.append(str(i))
        out.append("%s --> %s" % (tc(a, sep), tc(b, sep)))
        out.append(txt)
        out.append("")
    return "\n".join(out)


# --- ShotGrid ---------------------------------------------------------------

def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def load_shots(sg, sequence=None):
    # EPISODE shots only. GENVID_* are generator test shots that live in the
    # same project and are not part of any cut; letting them form a scene run
    # shifts every later alignment by one.
    filt = [["project", "is", PROJ], ["code", "starts_with", EP.code]]
    if sequence:
        filt.append(["sg_sequence.Sequence.code", "is", sequence])
    rows = sg.find("Shot", filt,
                   ["code", "sg_sequence", "sg_cut_order", "assets",
                    "sg_caption_text", "sg_gen_frames"])
    for s in rows:
        names = [a["name"] for a in (s.get("assets") or [])]
        s["_styles"] = [n for n in names if n.startswith("STYLE_")]
        s["_chars"] = [n for n in names if n.startswith("CHAR_")]
        s["_frames"] = int(s.get("sg_gen_frames") or 121)
        s["_seq"] = (s.get("sg_sequence") or {}).get("name") or ""
    rows.sort(key=lambda s: (s["_seq"], s.get("sg_cut_order") or 0))
    return rows


def cmd_align(sg, dry):
    scenes = parse_script(SCRIPT)
    shots = load_shots(sg)
    runs = shot_runs(shots)
    pairs, miss, orphans = align(scenes, runs)
    print("[captions] script scenes %d   shot runs %d   aligned %d"
          % (len(scenes), len(runs), len(pairs)))
    for sc, why in miss:
        print("  UNMATCHED SCENE: %-44s %s" % (sc["heading"][:44], why))
    for r in orphans:
        print("  SHOT RUN WITH NO SCENE: style=%-22s %d shot(s) from %s"
              % (r["style"], len(r["shots"]), r["shots"][0]["code"]))

    batch, n_lines, n_shots = [], 0, 0
    for sc, run in pairs:
        got = assign_beats(sc, run)
        for s in run["shots"]:
            beats = got.get(s["id"]) or []
            if not beats:
                continue
            text = "\n".join("%s: %s" % (b["speaker"], b["text"]) for b in beats)
            # NOTE (fixed 2026-08-29, PHASE-4-FIX1.md): this batch used to also
            # write "%s / %s" % (sc["act"], sc["heading"]) into
            # Shot.sg_script_beat. DO NOT reintroduce that write. That field is
            # exclusively owned by script_to_beats.py (Phase 4's beat-of-record
            # and staleness-watch target) as of Phase 4. This tool's own write
            # to it silently overwrote real Phase 4 beat text on 67/121 shots
            # in production on 2026-08-27 -- see PHASE-4-VERIFY.md #5 and
            # PHASE-4-FIX1.md for the full incident. The act/heading label had
            # no downstream reader (grepped: nothing but this writer itself
            # ever read it back) and is trivially re-derivable from `sc` here
            # if a future tool genuinely needs it -- give it its OWN field via
            # schema_field_create (read back the real code, ShotGrid renames
            # them) rather than sharing this one.
            batch.append({"request_type": "update", "entity_type": "Shot",
                          "entity_id": s["id"],
                          "data": {"sg_caption_text": text}})
            n_lines += len(beats)
            n_shots += 1
    print("[captions] %d line(s) placed on %d shot(s)" % (n_lines, n_shots))
    if dry:
        print("[captions] DRY RUN, nothing written")
        return 0
    for i in range(0, len(batch), 50):
        sg.batch(batch[i:i + 50])
    print("[captions] written. Corrections belong in the Shot field, not this script.")
    return 0


def cmd_srt(sg, sequence, episode):
    shots = load_shots(sg, None if episode else sequence)
    cues = cues_from_shots(shots)
    if not cues:
        print("[captions] no shot carries sg_caption_text yet. Run --align first.")
        return 1
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    base = os.path.join(OUT, EP.code if episode else sequence)
    io.open(base + ".srt", "w", encoding="utf-8").write(render(cues))
    io.open(base + ".vtt", "w", encoding="utf-8").write(render(cues, vtt=True))
    print("[captions] %d cue(s) over %.1fs -> %s.srt / .vtt"
          % (len(cues), cues[-1][1] / 1000.0, base))
    return 0


# --- self-test --------------------------------------------------------------

def self_test():
    fails = []

    def ck(name, cond):
        print("  %-54s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("scene heading maps to set style",
       scene_style("INT. CHARB'S FLAT - NIGHT (est. 0:45)") == "STYLE_CHARB_FLAT")
    ck("unknown heading maps to nothing", scene_style("EXT. MARS - DAY") is None)

    def mk(i, st, ch=(), f=121):
        return {"id": i, "code": "S%d" % i, "_styles": list(st),
                "_chars": list(ch), "_frames": f}

    runs = shot_runs([mk(1, ["STYLE_CHARB_FLAT"]), mk(2, ["STYLE_LEDGE"]),
                      mk(3, ["STYLE_CHARB_FLAT"]), mk(4, ["STYLE_LOBBY"])])
    ck("cutaway does not split a scene run",
       len(runs) == 2 and len(runs[0]["shots"]) == 3)
    ck("a real set change does start a run", runs[1]["style"] == "STYLE_LOBBY")

    sc = {"heading": "INT. LOBBY - NIGHT", "style": "STYLE_LOBBY", "act": "ACT ONE",
          "beats": [{"speaker": "CHARJ", "mode": "", "text": "Page nine."},
                    {"speaker": "CHARB", "mode": "", "text": "Which part?"}]}
    run = {"style": "STYLE_LOBBY",
           "shots": [mk(1, ["STYLE_LOBBY"], ["CHAR_CHARJ"]),
                     mk(2, ["STYLE_LOBBY"], ["CHAR_CHARB"])]}
    got = assign_beats(sc, run)
    ck("speaker on screen pulls the line to that shot",
       any(b["speaker"] == "CHARJ" for b in got[1])
       and any(b["speaker"] == "CHARB" for b in got[2]))

    # CANARY: alignment must REFUSE to invent a match, and must say so.
    pairs, miss, orph = align(
        [{"heading": "INT. NOWHERE", "style": "STYLE_NOPE", "act": "A",
          "beats": [{"speaker": "X", "mode": "", "text": "hi"}]}],
        [{"style": "STYLE_LOBBY", "shots": [mk(1, ["STYLE_LOBBY"])]}])
    ck("CANARY unmatched scene is reported, not dropped",
       len(pairs) == 0 and len(miss) == 1 and len(orph) == 1)

    cues = cues_from_shots([{"_frames": 24, "sg_caption_text": "A: one\nB: two"},
                            {"_frames": 24, "sg_caption_text": ""}])
    # These were written against a hardcoded 24fps timeline: 24 frames was
    # asserted to be exactly 1000ms. That is only true AT 24fps, and the MVP
    # now delivers at 16 (see timeline.py), where 24 frames is 1500ms. The
    # deploy preflight caught this the moment the rate changed, which is the
    # gate doing its job, so the expectations are derived from FPS now rather
    # than restated as a constant.
    _one_shot_ms = 24 / FPS * 1000.0
    ck("two lines split one shot's window",
       len(cues) == 2 and abs(cues[1][1] - _one_shot_ms) < 1)
    ck("an uncaptioned shot still advances the clock",
       cues_from_shots([{"_frames": 24, "sg_caption_text": ""},
                        {"_frames": 24, "sg_caption_text": "A: late"}])[0][0]
       >= _one_shot_ms - 1)
    ck("timecode formats as SRT", tc(3661000) == "01:01:01,000")

    ck("est duration parses from a heading",
       scene_est("INT. CHARB'S FLAT - DAY (est. 1:30)") == 90)
    ck("a heading with no est is zero, not a crash", scene_est("INT. X - DAY") == 0)

    # CANARY: three scenes in ONE set must SPLIT the shared run, not collapse to
    # one scene with the other two silently dropped. This is the exact bug the
    # first live run exposed on act three, where 32 shots served 3 scenes.
    def scn(name, est):
        return {"heading": name, "style": "STYLE_CHARB_FLAT", "act": "3", "est": est,
                "beats": [{"speaker": "CHARB", "mode": "", "text": name}]}
    trio = [scn("A", 60), scn("B", 60), scn("C", 60)]
    big = {"style": "STYLE_CHARB_FLAT",
           "shots": [mk(i, ["STYLE_CHARB_FLAT"]) for i in range(1, 10)]}
    p3, m3, o3 = align(trio, [big])
    ck("CANARY same-set scenes split one run instead of dropping",
       len(p3) == 3 and len(m3) == 0)
    ck("the split covers every shot exactly once",
       sorted(sh["id"] for _, r in p3 for sh in r["shots"]) == list(range(1, 10)))
    ck("an uneven split follows the authored durations",
       len(align([scn("A", 120), scn("B", 60)], [big])[0][0][1]["shots"]) == 6)

    if os.path.exists(SCRIPT):
        real = parse_script(SCRIPT)
        nb = sum(len(s["beats"]) for s in real)
        ck("real script parses to scenes carrying lines", len(real) >= 8 and nb >= 40)
        ck("stage directions are not captured as dialogue",
           not any(b["text"].startswith("*") for s in real for b in s["beats"]))
        ck("appendix is not parsed as a scene",
           not any(s["heading"].upper().startswith(APPENDIX) for s in real))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--align", action="store_true")
    ap.add_argument("--srt", metavar="SEQUENCE")
    ap.add_argument("--srt-episode", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    sg = sg_connect()
    if ns.align:
        return cmd_align(sg, ns.dry_run)
    if ns.srt or ns.srt_episode:
        return cmd_srt(sg, ns.srt, ns.srt_episode)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
