#!/usr/bin/env python3
"""Phase 4: SCRIPT -> STORY BEATS, reviewable/editable in ShotGrid.

    SCRIPT -> STORY BEATS -> STORYBOARD PANELS -> ... (MASTER-PLAN-V2 section 1)

THE GEOFF GATE. A fresh CustomEntity named "Story Beat" is enabled by Geoff in
Site Preferences (2 minutes, cannot be done by ApiUser). Probed 2026-08-28 with
the existing `ce_probe.py` / `probe_ce.py`: no entity named "Story Beat" exists
anywhere on the site, and every CustomEntity slot the schema will report is
already visible and in use (01/02 = Royal Render, 07 = Reference, a shared
studio site -- do not rename or hijack any of them). So this tool runs the
PLAN'S FALLBACK: beats live in `Shot.sg_script_beat` (already existed) plus an
ordered `beats.json`.

ORIGINAL CLAIM (2026-08-28, WRONG, kept for the record rather than quietly
edited away): "[the field] held only a generic 'PILOT01_A / cut 1 / 121 frames'
placeholder -- never written by captions_sg.py's aligner, safe to repurpose."
CORRECTED 2026-08-29 per independent verification (PHASE-4-VERIFY.md #5, full
incident writeup in PHASE-4-FIX1.md): that claim was false. ShotGrid's
EventLogEntry history proves captions_sg.py::cmd_align DID write this field (an
"ACT / SCENE HEADING" label, e.g. "COLD OPEN / INT. CHARB'S FLAT - NIGHT") on
67 of 121 shots on 2026-08-27, and this tool's first `--link` run on
2026-08-28 silently overwrote that label with real beat text on those 67
shots. No irrecoverable data was lost -- the label was never read back
anywhere (grepped: only the writers referenced the field) and is trivially
re-derivable from the script's own scene headings -- but the field was NOT the
inert placeholder-only field the original claim asserted, and the two tools
had a live, unaddressed write collision on it. Fixed in PHASE-4-FIX1.md:
captions_sg.py::cmd_align no longer writes sg_script_beat at all (it already
correctly writes sg_caption_text, which is its actual job), so this field is
now exclusively owned by this tool. Do not add a second writer to it.

`beat_entity_available()` below is the single seam: if Geoff later enables the
entity, flip storage over there with NO rework of the parsing/alignment logic,
only the write path. Nothing here hard-codes the fallback as if it were the
only design.

REUSE, NOT A SECOND PARSER. captions_sg.py already solved the hard part of this
problem: turning scene headings into the STYLE_ asset the set was built as, and
walking the shot list (already in cut order) into contiguous same-style RUNS,
then aligning script scenes to shot runs -- including the split of one run
across several same-set scenes (Act Three) by authored duration. That logic is
imported and called UNMODIFIED here (`shot_runs`, `align`, `assign_beats`,
`load_shots`, `scene_style`, `scene_est`, the CUE regex, the APPENDIX cutoff).

What captions_sg.parse_script() does NOT give us: it deliberately THROWS AWAY
every stage-direction (*italic*) line, because a caption track should not
narrate blocking. Story beats need the opposite -- "one beat per action or
dialogue cluster" -- so `parse_clusters()` below re-walks the script with the
same scene/heading/CUE/APPENDIX rules (imported, not re-derived) but keeps
BOTH action and dialogue clusters, in document order, shaped exactly like
captions_sg's `scene["beats"]` list (`{speaker, mode, text}`) so the reused
`assign_beats()` distributes them across a shot run with zero changes.

ORPHAN SHOTS. align() can leave a handful of one-shot "orphan" runs uncovered
when the same style recurs later than align()'s forward-only scan expects
(observed on the live 121-shot cut: 3 shots). Per the docstring in
captions_sg.shot_runs, "a cutaway inherits the scene it is cut into" -- the
same idea is applied here: an orphan shot inherits the beat(s) of its NEAREST
covered neighbour in cut order. This is done in THIS file, not by touching
align()/shot_runs(), which stay exactly as captions_sg shipped them.

STALENESS, WITHOUT A REAL PANEL YET. Phase 5 has not run: there is no
`Shot.sg_approved_panel` field and no panel Version to compare a Beat's
`updated_at` against. So `beats.json` also carries a `shot_sync` map: the
sg_script_beat TEXT this tool last wrote to each shot. The service watcher
(genvideo_service.py) treats "current sg_script_beat != shot_sync[code]" as
the stand-in for "beat updated_at > panel created_at" -- it IS exactly that
signal once a beat lives only on the shot: editing the field on the shot in
ShotGrid is editing the beat. When a real panel field lands in Phase 5, the
watcher can additionally compare against the panel's created_at; the content
diff here still works underneath it as the actual edit-detection primitive.

    python script_to_beats.py --parse              parse only, print beat count
    python script_to_beats.py --link                parse, align, write to ShotGrid + beats.json
    python script_to_beats.py --link --dry-run       same, but no writes
    python script_to_beats.py --self-test
"""
import argparse
import hashlib
import io
import json
import os
import sys
import time

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools"),
# which is exactly how this file's own self-test broke -- it silently imported
# a STALE sibling episode_context.py (Sep 3 07:00, no beats_path()) from the
# drifted build\tools directory instead of its own, current one, even when run
# straight from the repo. Self-relative now, like prompt_revision.py and
# video_from_panel.py already were.
TOOLS = os.path.dirname(os.path.abspath(__file__))
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import captions_sg as C  # noqa: E402  (reuse, see module docstring)

SCRIPT = C.SCRIPT
import episode_context as EPCTX  # noqa: E402
# Episode-scoped. Was a single shared path; running this for a second episode
# would have overwritten the first episode's beats. See EPCTX.beats_path().
BEATS_JSON = EPCTX.beats_path(C.EP)
PROJ = C.PROJ
EP = C.EP     # reuse captions_sg's already-resolved Episode (invariant 11:
              # one resolution, imported -- not a second call to resolve_episode())

# Act heading fragment -> which of this episode's acts (EP.acts[i]) it belongs
# to. Verified against live PILOT01 data 2026-08-28: act[0] = cold open + act
# one, act[1] = act two, act[2] = act three + button. The FRAGMENT vocabulary
# (COLD OPEN / ACT ONE.. / BUTTON) is a screenplay-structure convention, not a
# PILOT01 fact, so it stays literal here; what varies per show is which real
# Sequence code each ordinal act resolves to, which now comes from EP.acts.
ACT_SEQUENCE = [
    ("COLD OPEN", 0),
    ("ACT ONE", 0),
    ("ACT TWO", 1),
    ("ACT THREE", 2),
    ("BUTTON", 2),
]

STORY_BEAT_ENTITY_NAME = "Story Beat"


# --- the Geoff gate -----------------------------------------------------

def beat_entity_available(sg):
    """-> the real CustomEntityNN type name if Geoff enabled "Story Beat", else None.

    Never hijacks CustomEntity01/02 (Royal Render) or 07 (Reference, live data on
    a shared studio site). Only matches an entity whose display name IS
    "Story Beat" (case-insensitive, exact), which cannot collide with those."""
    try:
        ents = sg.schema_entity_read()
    except Exception:
        return None
    for name, meta in ents.items():
        if not name.startswith("CustomEntity"):
            continue
        disp = (meta.get("name", {}) or {}).get("value") or ""
        if disp.strip().lower() == STORY_BEAT_ENTITY_NAME.lower():
            return name
    return None


def act_sequence(act):
    up = (act or "").upper()
    for frag, idx in ACT_SEQUENCE:
        if up.startswith(frag):
            return EP.acts[idx] if idx < len(EP.acts) else None
    return None


# --- 1. parse action+dialogue clusters (reuses captions_sg's scene/CUE rules) --

def parse_clusters(path):
    """-> [{heading, style, act, est, beats:[{speaker,mode,text,kind}]}], scene-
    shaped identically to captions_sg.parse_script() so shot_runs/align/
    assign_beats work unmodified, except scene["beats"] holds BOTH action and
    dialogue clusters in document order instead of dialogue only."""
    if not os.path.exists(path):
        raise SystemExit("script not found: %s" % path)
    lines = io.open(path, encoding="utf-8").read().splitlines()
    scenes, act, cur = [], "", None
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if ln.startswith("## "):
            act = ln[3:].strip()
            if act.upper().startswith(C.APPENDIX):
                break
            i += 1
            continue
        if ln.startswith("### "):
            heading = ln[4:].strip()
            cur = {"heading": heading, "style": C.scene_style(heading), "act": act,
                   "beats": [], "est": C.scene_est(heading)}
            scenes.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        stripped = ln.strip()
        m = C.CUE.match(ln) if (stripped and not ln.startswith("*")) else None
        if m:
            speaker = m.group(1).strip()
            mode = (m.group(2) or "").strip()
            j, buf = i + 1, []
            while j < len(lines):
                nxt = lines[j].rstrip()
                if not nxt.strip():
                    break
                if nxt.lstrip().startswith("*"):
                    j += 1
                    continue
                buf.append(nxt.strip())
                j += 1
            text = " ".join(buf).strip()
            if text and len(speaker) > 1:
                cur["beats"].append({"speaker": speaker, "mode": mode, "text": text,
                                     "kind": "dialogue"})
            i = j
            continue
        if stripped.startswith("*"):
            # A stage-direction cluster is a markdown italic PARAGRAPH: the
            # opening '*' sits on its first line and the closing '*' on its
            # LAST line only -- interior lines of a wrapped paragraph carry no
            # asterisk at all. A blank line, a dialogue cue, or the closing
            # '*' ends the cluster.
            j, buf, closed = i, [], False
            while j < len(lines):
                nxt = lines[j].rstrip()
                nxts = nxt.strip()
                if not nxts:
                    break
                if j > i and C.CUE.match(nxt):
                    break
                piece = nxts
                if j == i and piece.startswith("*"):
                    piece = piece[1:]
                if piece.endswith("*"):
                    piece = piece[:-1]
                    closed = True
                buf.append(piece.strip())
                j += 1
                if closed:
                    break
            text = " ".join(b for b in buf if b).strip()
            if text:
                cur["beats"].append({"speaker": "", "mode": "", "text": text,
                                     "kind": "action"})
            i = j if j > i else i + 1
            continue
        i += 1
    return [s for s in scenes if s["beats"] or s["style"]]


# --- 2. align scenes to shot runs, distribute clusters, cover every shot ------

def build_beats(sg):
    """-> (beat_records, shot_beats, diag) where:
      beat_records: [{beat_id, order, sequence, act, heading, kind, speaker,
                       text, status, shots:[codes]}] in document order
      shot_beats:   {shot_id: [beat_id, ...]}
      diag:         alignment diagnostics for the report / tests
    """
    scenes = parse_clusters(SCRIPT)
    shots = C.load_shots(sg)
    runs = C.shot_runs(shots)
    pairs, unmatched, orphans = C.align(scenes, runs)

    beat_records, shot_beats = [], {}
    order = 0
    for sc, run in pairs:
        seqcode = act_sequence(sc["act"]) or (run["shots"][0]["_seq"] if run["shots"] else None)
        got = C.assign_beats(sc, run)          # reused wholesale
        start = len(beat_records)
        for cl in sc["beats"]:
            order += 1
            bid = "%s_B%04d" % (EP.code, order)
            cl["_beat_id"] = bid
            beat_records.append({
                "beat_id": bid, "order": order, "sequence": seqcode,
                "act": sc["act"], "heading": sc["heading"], "kind": cl["kind"],
                "speaker": cl["speaker"] or None, "text": cl["text"],
                "status": "rev", "shots": []})
        by_bid = dict((r["beat_id"], r) for r in beat_records[start:])
        for s in run["shots"]:
            assigned = got.get(s["id"]) or []
            bids = [cl["_beat_id"] for cl in assigned]
            shot_beats[s["id"]] = bids
            for bid in bids:
                by_bid[bid]["shots"].append(s["code"])

    # Any shot left with NO cluster inherits its nearest covered neighbour's
    # beats (cut order) -- the same "a cutaway inherits the scene it is cut
    # into" idea captions_sg documents. Two distinct reasons a shot can arrive
    # here uncovered, both handled the same way:
    #   1. its run had no matching scene at all ("orphan run", not in `pairs`)
    #   2. its run WAS matched, but assign_beats() legitimately ran out of
    #      clusters before it ran out of shots (few beats, many shots in one
    #      scene) -- these shots have a key in shot_beats with an EMPTY list,
    #      which looks identical to "missing" for coverage purposes.
    order_list = shots  # already sorted by (_seq, sg_cut_order) in load_shots
    idx_of = dict((s["id"], k) for k, s in enumerate(order_list))
    by_beat_id = dict((r["beat_id"], r) for r in beat_records)
    covered = set(sid for sid, bids in shot_beats.items() if bids)
    uncovered = [s for s in order_list if not shot_beats.get(s["id"])]
    inherited = []
    for s in uncovered:
        idx = idx_of[s["id"]]
        nearest = None
        for d in range(1, len(order_list)):
            lo, hi = idx - d, idx + d
            if lo >= 0 and order_list[lo]["id"] in covered:
                nearest = order_list[lo]["id"]
                break
            if hi < len(order_list) and order_list[hi]["id"] in covered:
                nearest = order_list[hi]["id"]
                break
        bids = list(shot_beats.get(nearest, []))
        shot_beats[s["id"]] = bids
        for bid in bids:
            by_beat_id[bid]["shots"].append(s["code"])
        covered.add(s["id"])
        inherited.append((s["code"], nearest))

    diag = {"scenes": len(scenes), "runs": len(runs), "pairs": len(pairs),
            "unmatched": [(sc["heading"], why) for sc, why in unmatched],
            "orphan_runs": len(orphans), "inherited": inherited,
            "total_shots": len(shots),
            "covered_shots": sum(1 for bids in shot_beats.values() if bids)}
    return beat_records, shot_beats, shots, diag


# --- 3. write --------------------------------------------------------------

def cmd_link(sg, dry):
    beat_records, shot_beats, shots, diag = build_beats(sg)
    print("[beats] scenes=%d runs=%d pairs=%d unmatched=%d orphan_runs=%d"
          % (diag["scenes"], diag["runs"], diag["pairs"], len(diag["unmatched"]),
             diag["orphan_runs"]))
    for h, why in diag["unmatched"]:
        print("  UNMATCHED SCENE: %-44s %s" % (h[:44], why))
    for code, nearest_id in diag["inherited"]:
        print("  orphan shot %s inherits beat(s) from nearest covered neighbour" % code)
    print("[beats] %d beat(s) parsed, %d/%d shots covered"
          % (len(beat_records), diag["covered_shots"], diag["total_shots"]))

    ce_type = beat_entity_available(sg)
    if ce_type:
        # The detection seam works (self-tested), but the entity-backed WRITE
        # path (create real Beat records on ce_type, an entity-link field on
        # Shot) has never run against a live CustomEntity -- none is enabled
        # on this site as of this build. Refusing loudly beats silently
        # writing the fallback under a misleading "real entity" label, or
        # guessing at an untested schema-mutation path. Implement and test
        # this branch for real once Geoff enables the entity.
        print("[beats] Story Beat entity %s is now enabled, but the entity-backed "
              "write path is NOT implemented (never testable until now). "
              "Refusing rather than silently using the fallback under a "
              "misleading label, or guessing at untested schema writes. "
              "This needs a follow-up build." % ce_type)
        return 3
    print("[beats] storage: FALLBACK (Shot.sg_script_beat + beats.json)")

    by_id = dict((s["id"], s) for s in shots)
    shot_sync = {}
    batch = []
    for sid, bids in shot_beats.items():
        s = by_id[sid]
        recs = [r for r in beat_records if r["beat_id"] in bids]
        text = "\n".join("[%s/%s] %s" % (r["kind"], r["speaker"] or "-", r["text"])
                         for r in recs)
        shot_sync[s["code"]] = {"beat_ids": bids, "synced_text": text}
        batch.append({"request_type": "update", "entity_type": "Shot",
                      "entity_id": sid,
                      "data": {"sg_script_beat": text, "sg_beat_id": ",".join(bids)}})

    doc = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script_sha256": hashlib.sha256(io.open(SCRIPT, "rb").read()).hexdigest(),
        "storage": ("entity:%s" % ce_type) if ce_type else "fallback:Shot.sg_script_beat",
        "beats": beat_records,
        "shot_sync": shot_sync,
    }
    if dry:
        print("[beats] DRY RUN: %d shot(s) would be updated, beats.json not written" % len(batch))
        return 0

    os.makedirs(os.path.dirname(BEATS_JSON), exist_ok=True)
    io.open(BEATS_JSON, "w", encoding="utf-8").write(json.dumps(doc, indent=2))
    for i in range(0, len(batch), 50):
        sg.batch(batch[i:i + 50])
    print("[beats] written: %s (%d beat records)" % (BEATS_JSON, len(beat_records)))
    print("[beats] %d shot(s) updated in ShotGrid (sg_script_beat, sg_beat_id)" % len(batch))
    if diag["covered_shots"] < diag["total_shots"]:
        print("[beats] WARNING: coverage incomplete (%d/%d)"
              % (diag["covered_shots"], diag["total_shots"]))
    return 0


def cmd_parse():
    scenes = parse_clusters(SCRIPT)
    n_action = sum(1 for s in scenes for b in s["beats"] if b["kind"] == "action")
    n_dialogue = sum(1 for s in scenes for b in s["beats"] if b["kind"] == "dialogue")
    print("[beats] %d scene(s), %d action cluster(s), %d dialogue cluster(s), %d total"
          % (len(scenes), n_action, n_dialogue, n_action + n_dialogue))
    for s in scenes:
        print("  %-46s style=%-22s beats=%d" % (s["heading"][:46], s["style"], len(s["beats"])))
    return 0


# --- self-test ---------------------------------------------------------------

def self_test():
    fails = []

    def ck(name, cond):
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("act -> sequence: cold open", act_sequence("COLD OPEN") == "PILOT01_A")
    ck("act -> sequence: act one", act_sequence("ACT ONE - THE CHAT") == "PILOT01_A")
    ck("act -> sequence: act two", act_sequence("ACT TWO - TWELVE FIFTY") == "PILOT01_B")
    ck("act -> sequence: act three", act_sequence("ACT THREE - THE INSPECTION") == "PILOT01_C")
    ck("act -> sequence: button folds into C", act_sequence("BUTTON") == "PILOT01_C")
    ck("unknown act -> None", act_sequence("APPENDIX") is None)

    if os.path.exists(SCRIPT):
        scenes = parse_clusters(SCRIPT)
        ck("real script parses to scenes", len(scenes) >= 8)
        n_action = sum(1 for s in scenes for b in s["beats"] if b["kind"] == "action")
        n_dialogue = sum(1 for s in scenes for b in s["beats"] if b["kind"] == "dialogue")
        ck("real script yields action clusters", n_action > 20)
        ck("real script yields dialogue clusters", n_dialogue > 40)
        ck("dialogue cluster count matches captions_sg's own line count",
           n_dialogue == sum(len(s["beats"]) for s in C.parse_script(SCRIPT)))
        ck("no beat text is empty", not any(not b["text"].strip()
                                            for s in scenes for b in s["beats"]))
        ck("appendix not parsed as a scene",
           not any(s["heading"].upper().startswith(C.APPENDIX) for s in scenes))
        # CANARY: order is preserved within a scene (action/dialogue interleave,
        # not all actions first then all dialogue).
        mixed_scene = next((s for s in scenes if
                            any(b["kind"] == "action" for b in s["beats"]) and
                            any(b["kind"] == "dialogue" for b in s["beats"])), None)
        ck("CANARY a real scene interleaves action and dialogue kinds (not blocked)",
           mixed_scene is not None and
           len(set(b["kind"] for b in mixed_scene["beats"])) == 2 and
           any(mixed_scene["beats"][k]["kind"] != mixed_scene["beats"][k + 1]["kind"]
               for k in range(len(mixed_scene["beats"]) - 1)))

    # beat_entity_available must never match a Royal Render / Reference slot,
    # only an entity literally named "Story Beat".
    class FakeSG:
        def __init__(self, ents):
            self._ents = ents

        def schema_entity_read(self):
            return self._ents

    fake_used = FakeSG({
        "CustomEntity01": {"name": {"value": "RR jobs"}},
        "CustomEntity02": {"name": {"value": "RR sumissions"}},
        "CustomEntity07": {"name": {"value": "Reference"}},
    })
    ck("CANARY no fresh entity -> fallback (does not hijack RR/Reference slots)",
       beat_entity_available(fake_used) is None)
    fake_enabled = FakeSG({
        "CustomEntity01": {"name": {"value": "RR jobs"}},
        "CustomEntity03": {"name": {"value": "Story Beat"}},
    })
    ck("a genuinely enabled 'Story Beat' entity IS detected",
       beat_entity_available(fake_enabled) == "CustomEntity03")

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parse", action="store_true")
    ap.add_argument("--link", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if ns.parse:
        return cmd_parse()
    if ns.link:
        return cmd_link(C.sg_connect(), ns.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
