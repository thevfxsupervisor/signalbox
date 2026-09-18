#!/usr/bin/env python3
r"""Split an action beat into ONE action clause per character, plus the shared staging.

WHY THIS EXISTS, measured 2026-09-04.

Regional composition gives each character its own masked band, and each band
holds exactly ONE character's reference image. But every band was being handed
the SAME text: the whole beat, naming everybody. On SHOW01_A_0300 that meant
PilotCharB's band -- holding PilotCharB's reference -- was instructed "PilotCharA strides back
in and flips on the overhead lights", and the two characters rendered as a
single fused person wearing PilotCharA's shirt with PilotCharB's pyjama trousers.

Four hero shots through the production path came back 1 clean of 4, and the
band text is why: every one of them handed both bands the identical beat.

WHY NOT A REGEX. dialogue_guard already strips absent characters, and it
deliberately REFUSES to do this: its over-stripping guard never removes a
clause that also names a present character, and never removes a leading or
interior clause, because both were confirmed live to orphan a subject
("PilotCharA comes in and kneels on the floor" loses its subject if you remove
the first half). That guard is right and this is a different job: rewriting a
sentence to be about one person is a language task, not a deletion task.

WHAT THE OPERATOR GETS. The split is stored on the Shot as JSON so it is
directly editable, which is the point Geoff has made repeatedly: there should
be entities for the discrete components of a prompt, so the operator can edit
the part that is wrong rather than writing a note about the whole thing. A
stored split is used as-is; only a missing one costs a model call.

D15 APPLIES: no dialogue, no on-screen text, no speech. The beat describes
what a camera would see.

    python beat_split.py --self-test
    python beat_split.py --shot SHOW01_A_0300
    python beat_split.py --shot SHOW01_A_0300 --refresh
"""
import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# attribute_check lives in tests/, not tools/. Imported for resolve_claude_bin
# and extract_inner_json rather than growing a second copy of either
# (invariant 11); extract_inner_json in particular carries today's
# trailing-prose fix and the injection-refusal naming.
#
# SEARCHED, NOT ASSUMED. The repo has tests/ beside tools/, but deploy.py
# stages a release whose layout puts it at <root>/build/tests -- so the
# obvious "../tests" resolved to nothing there and preflight refused to
# activate the release. That refusal was correct and is why this is a search.
def _find_tests_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    # EVERY CANDIDATE IS BUILT WITH os.path.join AND NO LITERAL BACKSLASHES.
    #
    # The line that used to sit here was a hardcoded Windows path written
    # through a shell heredoc, and the heredoc ate two of its escapes before
    # Python ever saw the file: the intended ".../genvideo-pipeline/build/tests"
    # was on disk as "genvideo-pipeline<0x08>uild<0x09>ests" -- a backspace and a
    # tab baked into the source. An r"" prefix cannot help, because the bytes
    # were already wrong when they were written. It looked plausible in a diff
    # and silently removed one of the fallbacks.
    #
    # AND THE FALLBACK IT REMOVED WAS THE ONE THAT MATTERED. A deployed release
    # is <root>/build/releases/<stamp>/tools, and deploy.py publishes the test
    # modules to <root>/build/tests -- NOT inside the release. So the deployed
    # service could never find attribute_check.py and every two-character shot
    # refused with "attribute_check.py not found beside this release".
    # Measured on SHOW01_A_0080 and SHOW01_A_0090. The root is now DERIVED by
    # walking up from this file rather than typed out, so it cannot be
    # mistyped and cannot drift when the release stamp changes.
    # WALK UP AND LOOK, rather than counting directory levels.
    #
    # My first attempt derived the root as "two above the tools dir", which is
    # right for E:/.../build/releases/<stamp>/tools only if you count
    # correctly, and I did not: it produced <root>/build/build/tests. The
    # deploy caught it -- preflight runs each staged module's self-test from
    # inside the staged release, beat_split refused at import, and the release
    # was staged but NOT activated with the previous one left live. That is the
    # deploy seam behaving exactly as designed.
    #
    # Counting levels is brittle because the SAME file runs from three
    # layouts: the repo (tools/ beside tests/), a staged release
    # (<root>/build/releases/<stamp>/tools with tests at <root>/build/tests),
    # and an activated one. Walking up and testing each candidate needs to know
    # none of that.
    seen = os.path.abspath(here)
    for _ in range(6):
        seen = os.path.dirname(seen)
        if not seen or seen == os.path.dirname(seen):
            break
        for cand in (os.path.join(seen, "tests"),
                     os.path.join(seen, "build", "tests")):
            if os.path.isfile(os.path.join(cand, "attribute_check.py")):
                return os.path.abspath(cand)
    raise SystemExit("attribute_check.py not found beside this release; "
                     "beat_split cannot run without it")


sys.path.insert(0, _find_tests_dir())

import attribute_check as AC                                    # noqa: E402

PROJ = {"type": "Project", "id": 9999}
FIELD = "sg_beat_split"          # JSON: {"shared": "...", "actions": {name: "..."}}
MODEL = "sonnet"
TIMEOUT = 180

TEMPLATE = """You are splitting one storyboard action beat into per-character pieces for an \
image compositor that renders each character in a separate region of the frame. Each region is given \
ONE character's reference image and ONE piece of text, and it must not be told about anyone else, \
because a region told about a second person renders that person too and the two characters merge \
into one figure.

THE BEAT: {beat}

THE CHARACTERS IN FRAME: {names}

Return ONLY raw JSON (no markdown fences, no prose before or after) in exactly this shape:
{{"shared": "...", "actions": {{{example}}}}}

Rules, all mandatory:
- "actions" must contain exactly one entry per character listed above, keyed by the lowercase name.
- Each action describes ONLY that character: their posture, position in frame, what they are doing \
and where they are looking. It must NOT name, mention or imply any other character, and must not use \
"the other", "them", "each other" or similar - a region cannot see anyone else, so a reference to \
someone else can only be rendered as a second body.
- If the beat gives a character nothing to do, infer the most likely thing they ARE doing from the \
situation and say it plainly. Never leave an action empty, and never write "not mentioned".
- Rewrite rather than delete. "PilotCharA comes in and kneels beside the bed" becomes, for PilotCharA, \
"kneels on the floor beside the bed, leaning in" - a complete clause with a subject, not a fragment.
- "shared" is what BOTH regions need: the room, the light, the camera framing, the time of day. It \
must not name any character or any character's action.
- "shared" must contain NO PEOPLE AT ALL, not even unnamed ones. Write it as if the room were empty. \
Never say how many figures are in frame, and never write "two figures", "both of them", \
"facing each other", "side by side", or anything else implying more than one person. \
This text is appended to EVERY region, and each region is shown ONE character reference image, \
so a region told the frame holds two figures draws a second person by copying the one it has. \
Say "medium shot at chest height in a dim hallway", never "framing two figures standing close together".
- No dialogue, no speech, no captions. That is D15: quoted dialogue fed to the generator comes back as burnt-in captions, and subtitles are added as graphics at edit time instead.
- Present tense, plain description of what a camera would see. No mood words, no story explanation."""


def log(m):
    print("[beat_split] %s" % m, flush=True)


def build_prompt(beat, names):
    example = ", ".join('"%s": "..."' % n for n in names)
    return TEMPLATE.format(beat=beat.strip(), names=", ".join(names), example=example)


def validate(parsed, names):
    """-> (ok, reason). Every failure is named; nothing is defaulted into
    place, because a silently-defaulted action is exactly the all-bands-get-
    the-same-text bug this module exists to end."""
    if not isinstance(parsed, dict):
        return False, "response was not a JSON object"
    shared = parsed.get("shared")
    if not isinstance(shared, str) or not shared.strip():
        return False, "shared staging missing or empty"
    actions = parsed.get("actions")
    if not isinstance(actions, dict):
        return False, "actions missing or not an object"
    if sorted(actions) != sorted(names):
        return False, ("actions keys %s do not match the characters %s"
                       % (sorted(actions), sorted(names)))
    for n, a in actions.items():
        if not isinstance(a, str) or not a.strip():
            return False, "action for %s is empty" % n
        low = a.lower()
        # THE CANARY THIS MODULE IS FOR. An action naming another character
        # is the defect, not a style problem, so it is a hard failure.
        for other in names:
            if other != n and other in low:
                return False, ("action for %s names %s ('%s') -- a region holds "
                               "one reference and will render the second name "
                               "as a second body" % (n, other, a[:60]))
        for phrase in ("the other", "each other", "them both", "both of them"):
            if phrase in low:
                return False, "action for %s refers to someone else (%r)" % (n, phrase)
    if any(n in (shared or "").lower() for n in names):
        return False, "shared staging names a character; it must be character-free"

    # ...AND IT MUST NOT IMPLY PEOPLE EITHER, WHICH NAMES ALONE DO NOT CATCH.
    #
    # MEASURED 2026-09-05 on SHOW01_A_0390, whose shared staging came back as
    # (fabricated example of the same shape) "wide shot, exterior night setting,
    # harsh single sodium light, camera at ground level framing TWO STANDING
    # FIGURES TURNED TOWARD EACH OTHER". No character is named, so the check
    # above passed it -- and the shared text is appended to EVERY band, so
    # PilotCharA's region, holding only PilotCharA's reference, was told the
    # frame contains two figures turned toward each other. All eight seeds fused.
    #
    # The per-ACTION check a few lines up already rejects "each other". The
    # shared text was simply never given the same treatment, which is the whole
    # bug: the same sentence is forbidden in one field and allowed in the other
    # while both reach the same conditioning.
    #
    # The phrase list is IMPORTED from beat_headcount rather than restated, so
    # the two checks cannot drift (invariant 11) -- that module exists because
    # the identical mistake in a SHOT's beat drew two PILOTCHARAs on 8 of 8 seeds.
    try:
        import beat_headcount as BHC
        implied = BHC.implied_plural(shared)
    except Exception:                                             # noqa: BLE001
        implied = [p for p in ("each other", "two figures", "both figures")
                   if p in (shared or "").lower()]
    extra = [p for p in ("two standing", "two figures", "two people", "both figures")
             if p in (shared or "").lower()]
    implied = sorted(set(implied) | set(extra))
    if implied:
        return False, ("shared staging implies more than one person (%s); it is "
                       "appended to EVERY region, and a region holding one "
                       "reference will render the extra people it is told about"
                       % ", ".join(repr(x) for x in implied))
    return True, ""


def split_beat(beat, names, claude_bin=None, model=MODEL, timeout=TIMEOUT):
    """-> (result_dict, error). Never returns a partially-valid split."""
    if not beat or not beat.strip():
        return None, "empty beat"
    if len(names) < 2:
        return None, "beat_split is for the multi-character case; %d given" % len(names)
    import prompt_revision as PR
    bin_ = AC.resolve_claude_bin(claude_bin)
    out, err = PR.invoke_claude_text(bin_, build_prompt(beat, names), model, timeout)
    if err:
        return None, err
    parsed, perr = AC.extract_inner_json(out)
    if perr:
        return None, perr
    ok, why = validate(parsed, names)
    if not ok:
        return None, why
    return {"shared": parsed["shared"].strip(),
            "actions": dict((k, v.strip()) for k, v in parsed["actions"].items())}, None


def load_stored(sg, shot):
    raw = shot.get(FIELD)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        log("  stored %s on %s is not valid JSON; ignoring it"
            % (FIELD, shot.get("code")))
        return None
    return d if isinstance(d, dict) else None


def ensure_schema(sg):
    """Create Shot.sg_beat_split if it is missing. Additive."""
    try:
        cur = sg.schema_field_read("Shot", FIELD)
    except Exception:                                            # noqa: BLE001
        cur = None
    if cur:
        return False
    sg.schema_field_create("Shot", "text", "beat split",
                           properties={"description":
                                       "Per-character action clauses for regional "
                                       "composition, as JSON. Operator-editable."})
    log("created Shot.%s" % FIELD)
    return True


def for_shot(sg, shot, names, refresh=False):
    """-> (split, error). Uses the stored split unless --refresh; only a
    missing or invalid one costs a model call."""
    if not refresh:
        stored = load_stored(sg, shot)
        if stored:
            ok, why = validate(stored, names)
            if ok:
                return stored, None
            log("  stored split rejected (%s); recomputing" % why)
    split, err = split_beat(shot.get("sg_action_beat") or "", names)
    if err:
        return None, err
    try:
        sg.update("Shot", shot["id"], {FIELD: json.dumps(split, indent=1)})
    except Exception as exc:                                     # noqa: BLE001
        # The split is still usable; only the cache failed.
        log("  could not store the split on %s (%s); using it anyway"
            % (shot.get("code"), exc))
    return split, None


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    names = ["PILOTCHARA", "PILOTCHARB"]
    good = {"shared": "a dim bedroom with two beds, one small side lamp lit, "
                      "camera static and wide",
            "actions": {"PILOTCHARA": "kneels on the floor beside the near bed, "
                                   "leaning forward",
                        "PILOTCHARB": "lies under the sheets in the far bed, head on "
                                "the pillow, eyes closed"}}
    ok, why = validate(good, names)
    ck("a clean split validates (%s)" % why, ok)

    bad_name = json.loads(json.dumps(good))
    bad_name["actions"]["PILOTCHARB"] = "lies asleep while PilotCharA kneels beside him"
    ok2, why2 = validate(bad_name, names)
    ck("an action naming the OTHER character is REFUSED (this is the defect)",
       not ok2 and "names PILOTCHARA" in why2)

    bad_ref = json.loads(json.dumps(good))
    bad_ref["actions"]["PILOTCHARB"] = "lies asleep, turned away from the other person"
    ck("an action referring to 'the other' is refused too",
       not validate(bad_ref, names)[0])

    bad_shared = json.loads(json.dumps(good))
    bad_shared["shared"] = "PilotCharA's bedroom, dim, one lamp"
    ck("shared staging naming a character is refused",
       not validate(bad_shared, names)[0])

    missing = {"shared": "a room", "actions": {"PILOTCHARA": "stands"}}
    ck("a missing character is refused, never defaulted",
       not validate(missing, names)[0])

    empty = json.loads(json.dumps(good))
    empty["actions"]["PILOTCHARB"] = "   "
    ck("an empty action is refused, never passed through",
       not validate(empty, names)[0])

    ck("a non-object response is refused", not validate("nope", names)[0])
    ck("a missing shared is refused",
       not validate({"actions": good["actions"]}, names)[0])

    p = build_prompt("PilotCharA kneels beside the bed. PilotCharB sleeps.", names)
    ck("the prompt names every character it must split",
       "PILOTCHARA" in p and "PILOTCHARB" in p)
    ck("the prompt states the one-reference-per-region reason",
       "merge into one figure" in p)
    ck("the prompt forbids referring to the other character",
       "each other" in p)
    ck("the prompt still carries D15 (dialogue is never fed to the generator)",
       "No dialogue" in p and "burnt-in captions" in p)
    ck("CANARY the anti-artifact text prohibition is GONE (Geoff 2026-09-07: "
       "'let's stop trying to prevent it'). D15 is about DIALOGUE, which is a "
       "graphic added at edit time; it was never about lettering on a shirt",
       "text of any kind on any surface" not in p)
    ck("the prompt says rewrite, never delete",
       "Rewrite rather than delete" in p)

    ck("a single character is refused: this is the multi-character tool",
       split_beat("x", ["PILOTCHARB"])[0] is None)
    ck("an empty beat is refused", split_beat("", names)[0] is None)

    class _Stub(object):
        def __init__(self, stored):
            self.shot = {"id": 1, "code": "S", "sg_action_beat": "b",
                         FIELD: stored}
            self.updates = []

        def update(self, et, i, d):
            self.updates.append(d)

    st = _Stub(json.dumps(good))
    got, err = for_shot(st, st.shot, names)
    ck("a valid stored split is reused with NO model call",
       err is None and got == good and st.updates == [])

    # --- the corruption canary ----------------------------------------------
    # A hardcoded Windows path in this file was written through a shell heredoc
    # that ate its escapes: the intended backslash-b and backslash-t landed
    # BACKSPACE and TAB inside the string. Python parsed it happily, the r""
    # prefix was powerless (the bytes were already wrong), and the deployed
    # service silently lost the one fallback that could find attribute_check.py.
    _src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
    _ctrl = sorted(set(c for c in _src if ord(c) < 32 and c not in (chr(10), chr(13))))
    ck("CANARY no control characters are baked into this file's source",
       not _ctrl)
    ck("...and the tests directory resolves to somewhere that really exists",
       os.path.isdir(_find_tests_dir()))

    # --- shared staging must not imply people ------------------------------
    # FABRICATED string preserving the exact shape of SHOW01_A_0390's stored
    # split (not the real content). It names no character, so the names-only
    # check passed it, and the shared text is appended to EVERY band: PilotCharA's
    # region was told the frame contains two figures turned toward each other.
    # All eight seeds fused.
    fake_shared = ("wide shot, exterior night setting, harsh single sodium light, "
                   "camera at ground level framing two standing figures turned toward each other")
    ok, why = validate({"shared": fake_shared,
                        "actions": {"PILOTCHARA": "stands facing forward, nodding slowly",
                                    "PILOTCHARB": "stands facing forward, arm extended"}},
                       ["PILOTCHARA", "PILOTCHARB"])
    ck("CANARY the SHOW01_A_0390-shaped shared staging is REJECTED", not ok)
    ck("...and the refusal says it reaches every region",
       "EVERY region" in why or "every region" in why)
    ck("...and it names the phrase that did it",
       "each other" in why or "two standing" in why)

    good = {"shared": "a dim bedroom lit by a single low lamp, close framing at bed height",
            "actions": {"PILOTCHARA": "kneels at the bedside, hands raised",
                        "PILOTCHARB": "lies in the bed, eyes open"}}
    ck("CANARY a clean split still PASSES (the check is not just refusing everything)",
       validate(good, ["PILOTCHARA", "PILOTCHARB"])[0])
    # A staging that mentions furniture in the plural is not people.
    ck("plural OBJECTS in the staging are not treated as people",
       validate({"shared": "two lamps and several posters on the wall, night",
                 "actions": {"PILOTCHARA": "stands", "PILOTCHARB": "sits"}},
                ["PILOTCHARA", "PILOTCHARB"])[0])

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shot")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--ensure-schema", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    import sg_provenance as PROV
    sg = PMB.get_backend()
    if a.ensure_schema:
        ensure_schema(sg)
        return 0
    if not a.shot:
        raise SystemExit("--shot is required")
    shot = sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", a.shot]],
                       ["code", "sg_action_beat", "assets", FIELD])
    if not shot:
        raise SystemExit("no Shot %s" % a.shot)
    chars, _s, _o = PROV.classify_assets(sg, shot)
    names = [c.replace("SHOW_CHAR_", "").lower() for c in chars]
    split, err = for_shot(sg, shot, names, refresh=a.refresh)
    if err:
        raise SystemExit("split failed: %s" % err)
    print(json.dumps(split, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
