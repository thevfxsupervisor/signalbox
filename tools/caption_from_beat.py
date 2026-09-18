#!/usr/bin/env python3
"""Populate Shot.sg_caption_text from the dialogue already written in the beat.

WHY THIS EXISTS
    The subtitle path was already complete and live: episode_assemble ->
    caption_cues -> captions_sg.cues_from_shots() burns cues at edit time from
    Shot.sg_caption_text. The ONLY thing missing was the text. Every shot in
    SHOW01 had sg_caption_text = None, so every cut assembled silently and
    reported, correctly, "no picked shot carries caption text".

    captions_gen.py is NOT the tool for this. It is CSV-driven against the
    PILOT-SHOTS.csv contract, which belongs to PILOT01, a DEAD episode. Wiring
    it would have pointed the MVP at the wrong show's data source. The live
    control surface is the ShotGrid field, so this writes that.

THE BEAT ALREADY CONTAINS THE DIALOGUE, in the form episode_ingest wrote it:

    PILOTCHARA: "Hey, PilotCharB. PilotCharB."
    PilotCharA places the bowl down. PILOTCHARA: "We like pasta, right?" PILOTCHARB (groans): "What?"
    PILOTCHARA (whispering): "Imagine that we live in a world..."

So this is extraction, not authoring. A beat with no quoted dialogue yields
NOTHING and the shot keeps a null caption, which is the honest result for the
silent action shots that open this episode.
"""
import argparse, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Not speakers. CONT'D is a script artefact; the others are shot metadata that
# can appear uppercase in a beat.
NOT_A_SPEAKER = {"CONT", "CONT'D", "INT", "EXT", "V.O", "O.S", "MORE", "FADE",
                 "CUT", "BEAT", "NOTE", "TODO"}


def clean_speaker(raw):
    """PILOTCHARA (CONT'D) -> PILOTCHARA. Strip trailing punctuation and spaces."""
    s = (raw or "").strip().strip(".").strip()
    s = re.sub(r"\s+", " ", s)
    for suffix in (" CONT'D", " CONT"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return s.strip()


# A speaker tag sitting immediately before an opening quote.
SPEAKER_TAG = re.compile(
    r"(?P<speaker>[A-Z][A-Z0-9' .]{1,24}?)"
    r"(?:\s*\((?P<paren>[^)]{1,40})\))?"
    r"\s*:\s*$")

QUOTE_CHARS = '"' + chr(0x201C) + chr(0x201D)


def extract(beat):
    """-> list of (speaker, text). Empty list when the beat has no dialogue.

    QUOTES ARE PAIRED LEFT TO RIGHT, not matched by a regex that scans for a
    closer. The regex form truncated real dialogue: on
    `PILOTCHARA: "He said "hello" to me, then left."` it stopped at the first
    closer it met and captured only "He said". Pairing is dumber and cannot
    silently swallow the rest of a line.

    AN UNATTRIBUTED QUOTE INHERITS THE LAST SPEAKER. Screenplay beats put a
    continuation in a bare second quote:

        PILOTCHARA: "...An event." Beat. "I don't know, man."

    The first version dropped everything after the first cue, which lost a
    line of real dialogue on three SHOW01 shots (_0210, _0240, _0310).

    KNOWN LIMIT, documented rather than papered over: a quote nested inside a
    quote is ambiguous under pairing and will split into two cues. Screenplay
    dialogue rarely nests double quotes, and the failure is now visible rather
    than a silent truncation."""
    if not beat:
        return []

    positions = [i for i, ch in enumerate(beat) if ch in QUOTE_CHARS]
    out = []
    last_speaker = None
    # Pair them: (0,1), (2,3), ... A trailing unpaired quote is ignored.
    for a, b in zip(positions[0::2], positions[1::2]):
        text = re.sub(r"\s+", " ", beat[a + 1:b]).strip()
        if not text:
            continue
        m = SPEAKER_TAG.search(beat[:a])
        speaker = clean_speaker(m.group("speaker")) if m else None
        if speaker in NOT_A_SPEAKER:
            speaker = None
        if not speaker:
            # No tag of its own: a continuation of whoever spoke last.
            speaker = last_speaker
        if not speaker:
            # A quote with no speaker anywhere before it is not dialogue.
            # This is what keeps `On the screen: "Directed by ..."` silent.
            continue
        last_speaker = speaker
        out.append((speaker, text))
    return out


def caption_for(*beats):
    """-> the sg_caption_text string, or None when the shot is silent.

    Takes the candidate beat fields IN PRIORITY ORDER and uses the first that
    actually yields dialogue.

    THE ORDER IS THE WHOLE POINT, and getting it backwards silently captions
    NOTHING. sg_action_beat is written FOR THE IMAGE GENERATOR and deliberately
    has no dialogue in it: the real _0070 action beat renders "Hey, PilotCharB" as
    "lips parted as if speaking softly". sg_script_beat is the SCRIPT and is
    where the spoken line lives. Picture reads action first; captions must read
    script first. A first pass here used the picture order and reported all 55
    shots silent.

    One line per cue, "SPEAKER: text", which is the form captions_sg renders
    and the form conform.py burns. A parenthetical is deliberately DROPPED: it
    is a performance note for a human, not something to put on screen."""
    for beat in beats:
        cues = extract(beat)
        if cues:
            return chr(10).join("%s: %s" % (sp, tx) for sp, tx in cues)
    return None


# ------------------------------------------------------------------ ShotGrid
def run(sg, project, episode="SHOW01", apply=False, force=False, log=print):
    shots = sg.find("Shot",
                    [["project", "is", project],
                     ["code", "starts_with", episode + "_"]],
                    ["code", "sg_script_beat", "sg_action_beat",
                     "sg_caption_text", "sg_cut_order"])
    shots.sort(key=lambda s: (s.get("sg_cut_order") is None,
                              s.get("sg_cut_order") or 0, s["id"]))
    wrote = silent = kept = 0
    stale = []
    for s in shots:
        # SCRIPT FIRST. See caption_for(): the action beat is the picture
        # brief and has the dialogue paraphrased out of it by design.
        cap = caption_for(s.get("sg_script_beat") or "",
                          s.get("sg_action_beat") or "")
        cur = s.get("sg_caption_text")
        if cap is None:
            silent += 1
            # A SHOT THAT NOW HAS NO DIALOGUE BUT STILL CARRIES A CAPTION.
            # The first version returned here unconditionally, so a beat
            # edited to remove its dialogue kept the old subtitle FOREVER,
            # and --force could not clear it either: no code path ever wrote
            # an empty caption. A wrong subtitle burned into a cut is worse
            # than a missing one, so this is now at least always REPORTED.
            if cur:
                stale.append(s["code"])
                log("  %s STALE: has a caption, beat no longer has dialogue%s"
                    % (s["code"], "" if force else " (use --force to clear)"))
                if force:
                    if apply:
                        sg.update("Shot", s["id"], {"sg_caption_text": None})
                    wrote += 1
            continue
        if cur and not force:
            kept += 1
            continue
        if cur == cap:
            kept += 1
            continue
        log("  %s <- %s" % (s["code"], cap.replace(chr(10), " / ")[:90]))
        if apply:
            sg.update("Shot", s["id"], {"sg_caption_text": cap})
        wrote += 1
    if stale:
        log("%d shot(s) carry a STALE caption whose beat lost its dialogue: %s"
            % (len(stale), ", ".join(stale[:8])))
    log("%s %d shot(s); %d silent (no dialogue in the beat); %d already set"
        % ("wrote" if apply else "WOULD write", wrote, silent, kept))
    return wrote, silent, kept


# ------------------------------------------------------------------ self-test
def self_test():
    # 1. The real beats from SHOW01, including the silent opening.
    assert caption_for("PilotCharA, 31, sits alone in his bedroom in the middle "
                       "of the night, staring at his computer.") is None, \
        "a silent action beat must yield NO caption, not an empty string"
    print("self-test: silent action beat -> None")

    got = caption_for('PILOTCHARA: "Hey, PilotCharB. PilotCharB."')
    assert got == "PILOTCHARA: Hey, PilotCharB. PilotCharB.", repr(got)
    print("self-test: single line of dialogue extracted")

    got = caption_for("PilotCharA places the bowl of pasta on the side table. "
                      'PILOTCHARA: "We like pasta, right?" PILOTCHARB (groans): "What?"')
    assert got == ("PILOTCHARA: We like pasta, right?" + chr(10) + "PILOTCHARB: What?"), repr(got)
    print("self-test: two speakers in one beat, action text discarded, "
          "parenthetical dropped")

    got = caption_for('PILOTCHARA (whispering): "Imagine that we live in a world '
                      'where it is really hard to make pasta."')
    assert got.startswith("PILOTCHARA: Imagine that we live"), repr(got)
    assert "whispering" not in got, "the parenthetical reached the screen: " + got
    print("self-test: parenthetical is a performance note, never a subtitle")

    got = caption_for("PILOTCHARA (CONT" + chr(39) + 'D): "The kettle is louder than the argument."')
    assert got == "PILOTCHARA: The kettle is louder than the argument.", repr(got)
    print("self-test: CONT'D stripped from the speaker")

    # 2. CANARY: an ordinary capitalised sentence is NOT dialogue. Without the
    #    uppercase-speaker rule this matches and puts narration on screen.
    assert caption_for('On the screen: "Directed by A. Placeholder."') is None, \
        "screen text was captioned as dialogue: nobody SAYS it"
    print("self-test: on-screen text is not dialogue (nobody speaks it)")

    # 3. Structurally the same shape as a real production beat that opened one
    #    episode in production; content fabricated for this public snapshot.
    assert caption_for('On the screen: "Directed by A. Placeholder." He '
                       "wearily looks at the screen, holding his head with "
                       "both hands. Beat.") is None
    print("self-test: an on-screen-text-only beat stays silent")

    # 3b. THREE DEFECTS A REVIEW AGENT FOUND, each with a matching-shape beat
    #     (content fabricated for this public snapshot; all three were live
    #     in ShotGrid before this was fixed).
    #
    # (i) An unattributed continuation quote.
    got = caption_for('PILOTCHARA: "I keep meaning to fix the fence and it' + chr(39) + 's never '
                      'the week for it. Someday. Maybe next spring." Beat. '
                      '"I don' + chr(39) + 't know, man."')
    assert got == ("PILOTCHARA: I keep meaning to fix the fence and it" + chr(39) + "s never the "
                   "week for it. Someday. Maybe next spring." + chr(10) +
                   "PILOTCHARA: I don" + chr(39) + "t know, man."), repr(got)
    print("self-test: a bare continuation quote inherits the last speaker")

    # (ii) Nested quotes truncated the line to its first two words.
    got = caption_for('PILOTCHARA: "He said ' + chr(34) + 'hello' + chr(34) +
                      ' to me, then left."')
    assert got is not None and got != "PILOTCHARA: He said", \
        "nested quotes still truncate the line to " + repr(got)
    assert "to me" in got, "the tail of the line was swallowed: " + repr(got)
    print("self-test: nested quotes no longer swallow the rest of the line")

    # (iii) A numbered speaker, which a screenplay uses freely.
    got = caption_for('GUARD 1: "Halt!"')
    assert got == "GUARD 1: Halt!", repr(got)
    print("self-test: a numbered speaker tag is recognised")

    # And the exclusion that must SURVIVE all of the above: a quote with no
    # speaker anywhere before it is not dialogue, however many quotes follow.
    assert caption_for('On the screen: "Directed by A. Placeholder." '
                       'Later, a note: "Call your mother."') is None, \
        "unattributed screen text was captioned"
    print("self-test: unattributed quotes stay silent even in a run of them")

    # 4. CANARY, the defect that made a first pass report all shots silent.
    #    Structurally the same shape as the real _0070 fields (content
    #    fabricated for this public snapshot). The action beat is a picture
    #    brief with the speech paraphrased out; if it is consulted first it
    #    masks the script and nothing is captioned.
    real_action = ("PilotCharA sets two mugs on the counter, glances at the clock, and "
                   "wipes a hand across the fogged window before stepping back.")
    real_script = 'PILOTCHARA: "Wake up, PilotCharB. Come on, PilotCharB."'
    assert caption_for(real_action) is None, \
        "the action beat should carry no dialogue at all"
    assert caption_for(real_script, real_action) == "PILOTCHARA: Wake up, PilotCharB. Come on, PilotCharB.", \
        "script-first order broken"
    # The real protection is not the ORDER, it is that every candidate is tried
    # and a barren one is skipped. The original bug was `a or b`: it picks the
    # first NON-EMPTY FIELD and never looks at the other, and the action beat
    # is non-empty and dialogue-free, so it won on all shots.
    assert caption_for(real_action, real_script) == "PILOTCHARA: Wake up, PilotCharB. Come on, PilotCharB.", \
        ("a non-empty but dialogue-free beat masked the script: this is the "
         "`a or b` bug that reported all shots silent")
    assert caption_for(real_action or real_script) is None, \
        "sanity: the OLD one-field expression really did yield a silent shot"
    print("self-test: a non-empty dialogue-free beat cannot mask the script "
          "(the 55-silent bug)")

    # 5. Idempotence: running twice writes nothing the second time.
    class _SG:
        def __init__(self):
            self.rows = [{"id": 1, "code": "SHOW01_A_0070", "sg_cut_order": 7,
                          "sg_action_beat": real_action,
                          "sg_script_beat": real_script,
                          "sg_caption_text": None},
                         {"id": 2, "code": "SHOW01_A_0010", "sg_cut_order": 1,
                          "sg_action_beat": None,
                          "sg_script_beat": "PilotCharA sits alone.",
                          "sg_caption_text": None}]
            self.updates = []

        def find(self, _e, _f, _fields):
            return [dict(r) for r in self.rows]

        def update(self, _e, eid, data):
            self.updates.append((eid, data))
            for r in self.rows:
                if r["id"] == eid:
                    r.update(data)

    sg = _SG()
    w, s, k = run(sg, {"type": "Project", "id": 1}, apply=True, log=lambda m: None)
    assert (w, s, k) == (1, 1, 0), (w, s, k)
    assert sg.updates == [(1, {"sg_caption_text": "PILOTCHARA: Wake up, PilotCharB. Come on, PilotCharB."})], sg.updates
    w2, s2, k2 = run(sg, {"type": "Project", "id": 1}, apply=True, log=lambda m: None)
    assert (w2, s2, k2) == (0, 1, 1), (w2, s2, k2)
    assert len(sg.updates) == 1, "second run wrote again: not idempotent"
    print("self-test: idempotent, second run writes nothing")

    print("self-test PASSED")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--episode", default="SHOW01")
    ap.add_argument("--apply", action="store_true",
                    help="write to ShotGrid (default is a dry run)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a caption that is already set")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as B
    be = B.get_backend()
    sg = getattr(be, "sg", None) or getattr(be, "_sg", None)
    project = {"type": "Project", "id": int(os.environ.get("SG_PROJECT_ID", "9999"))}
    run(sg, project, episode=a.episode, apply=a.apply, force=a.force)
    if not a.apply:
        print("DRY RUN, nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
