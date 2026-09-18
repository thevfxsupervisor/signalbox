#!/usr/bin/env python3
r"""Which two-character shots are ALREADY singles in everything but the cast list?

THE DECISION THIS SERVES. 32 of 55 SHOW01 shots link two characters and none of
them composes correctly; two-character composition is a named, open problem in
the field (docs/TWO-CHARACTER-PROBLEM.md) and shipping teams design around it
rather than solve it. Reframing a two-hander as a single, an over-shoulder or a
reaction is real production practice.

But "go through 32 shots and decide" is a big ask, and most of it is mechanical.
This does the mechanical part: for each two-character shot, look at what the
ACTION BEAT actually says happens, and sort the shots by how much of the beat
needs both characters visible.

  SINGLE-READY   the beat describes only ONE character doing anything. The
                 other is linked but does nothing in this frame. These are
                 already singles; the cast list is just wrong.
  REACTION       the beat is one character reacting to another's line or act.
                 Standard coverage: hold on the listener, the speaker is
                 off-camera. Free to reframe, and it is what dialogue coverage
                 does anyway.
  CONTACT        the beat has the characters physically interacting -- touching,
                 handing something over, embracing. A single cannot carry it.
  BOTH-ACTIVE    both characters act, without contact. An over-shoulder or a
                 two-shot; reframing costs something.

THIS IS A SORTING TOOL, NOT A DECISION. It prints its reasoning for every shot
and changes nothing. The classification is by phrase, so it is legible and
arguable: a director can disagree with any single row by reading why it landed
there. That is deliberate -- a model-based judgement here would be a confident
opinion about someone else's storytelling, dressed as analysis.

    python twochar_reframe_audit.py --episode SHOW01
    python twochar_reframe_audit.py --episode SHOW01 --verbose
    python twochar_reframe_audit.py --self-test
"""
import argparse
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJ = {"type": "Project", "id": 9999}

SINGLE_READY, REACTION, CONTACT, BOTH_ACTIVE = (
    "SINGLE-READY", "REACTION", "CONTACT", "BOTH-ACTIVE")

# Physical interaction: a single cannot carry these, so they are checked first
# and win outright. Deliberately narrow -- a false CONTACT only costs a shot
# staying a two-hander, a missed one costs a reframe that breaks the story.
# REGEXES, NOT LITERAL SUBSTRINGS. The first version used literals and missed
# two of the first six shots I spot-checked: "offers" did not match "offering",
# and "holds out" did not match "holds it out". A word-form variant or one
# intervening word was enough. Verbs here are matched on their stem.
CONTACT_PATTERNS = [
    r"\bhands? (?:it |the |him |her |them )", r"\bhand(?:s|ing|ed) (?:him|her|it|them)\b",
    r"\boffer(?:s|ing|ed)?\b", r"\bhold(?:s|ing)? (?:\w+ )?out\b",
    r"\breach(?:es|ing)? (?:for|out|toward)", r"\bgrab(?:s|bing|bed)?\b",
    r"\btak(?:es|ing) the\b", r"\bpass(?:es|ing) (?:him|her|it|the)\b",
    r"\bhug(?:s|ging|ged)?\b", r"\bembrac(?:e|es|ing)\b",
    r"\bshov(?:e|es|ing)\b", r"\bpush(?:es|ing)\b", r"\bpull(?:s|ing)\b",
    r"\bshak(?:es|ing) (?:his|her|their) hand", r"\brest(?:s|ing)? a hand\b",
    r"\bput(?:s|ting)? a hand\b", r"\bhands? on\b",
    r"\bset(?:s|ting)? (?:the|it|a) [\w ]{0,40}down\b", r"\bleans? in close\b",
]
# One character reacting to the other. The other can be off-camera.
# "turns to" was here and matched "turns toward the door", which is not a
# reaction to anyone. Reaction patterns must name a PERSON being reacted to,
# or be unambiguous on their own.
REACTION_PATTERNS = [
    r"\breact(?:s|ing)?\b", r"\breali[sz]e(?:s|ing)?\b",
    r"\bwatch(?:es|ing)? (?:him|her|them|his|the other)", r"\blisten(?:s|ing)?\b",
    r"\bstar(?:es|ing) at (?:him|her|them)", r"\bturns to (?:him|her|them|face)\b",
    r"\bit hits (?:him|her)\b", r"\btaking it in\b", r"\babsorbing\b",
]


def norm(t):
    return " ".join((t or "").lower().split())


def names_present(text, names):
    """-> the character names the beat actually mentions."""
    t = norm(text)
    return [n for n in names if re.search(r"\b%s\b" % re.escape(n), t)]


def classify(beat, names):
    """-> (bucket, why). names are lowercase short names, e.g. ['PILOTCHARA','PILOTCHARB'].

    Order matters and is the whole argument: CONTACT wins because a single
    cannot show two people touching; then SINGLE-READY, because a beat naming
    one character is already a single whatever else it says; then REACTION."""
    t = norm(beat)
    if not t:
        return BOTH_ACTIVE, "no beat text; cannot tell, left as a two-hander"

    hits = [m.group(0) for p in CONTACT_PATTERNS
            for m in [re.search(p, t)] if m]
    if hits:
        return CONTACT, "physical interaction (%s)" % ", ".join(hits[:2])

    mentioned = names_present(t, names)
    if len(mentioned) <= 1:
        who = mentioned[0] if mentioned else "nobody"
        return (SINGLE_READY,
                "the beat describes %s only; the other character does nothing "
                "in this frame" % who)

    rhits = [m.group(0) for p in REACTION_PATTERNS
             for m in [re.search(p, t)] if m]
    if rhits:
        return REACTION, ("one character reacting (%s); the other can be "
                          "off-camera" % rhits[0])
    return BOTH_ACTIVE, "both characters act, no contact clause found"


def audit(shots, assets):
    """-> [(code, bucket, why, names, beat)] in cut order."""
    out = []
    for s in shots:
        chars = [assets[a["id"]] for a in (s.get("assets") or [])
                 if "CHAR" in (assets.get(a["id"]) or "")]
        if len(chars) < 2:
            continue
        names = [c.replace("SHOW_CHAR_", "").lower() for c in chars]
        beat = s.get("sg_action_beat") or s.get("sg_script_beat") or ""
        bucket, why = classify(beat, names)
        out.append((s["code"], bucket, why, names, beat))
    return out


def run(sg, episode="SHOW01", verbose=False):
    shots = sg.find("Shot", [["project", "is", PROJ],
                             ["code", "starts_with", episode]],
                    ["code", "assets", "sg_action_beat", "sg_script_beat",
                     "sg_cut_order", "sg_shot_size"],
                    order=[{"field_name": "sg_cut_order", "direction": "asc"}])
    assets = dict((a["id"], a["code"]) for a in
                  sg.find("Asset", [["project", "is", PROJ]], ["code"]))
    rows = audit(shots, assets)
    counts = Counter(r[1] for r in rows)
    print("[reframe] %d two-character shot(s) in %s" % (len(rows), episode))
    print("")
    for bucket, blurb in ((SINGLE_READY, "already a single; the cast list is just wrong"),
                          (REACTION, "hold on the listener, other character off-camera"),
                          (BOTH_ACTIVE, "both act; an over-shoulder, or accept a two-shot"),
                          (CONTACT, "physical interaction; a single cannot carry it")):
        n = counts.get(bucket, 0)
        print("  %-13s %2d   %s" % (bucket, n, blurb))
    print("")
    for bucket in (SINGLE_READY, REACTION, BOTH_ACTIVE, CONTACT):
        picked = [r for r in rows if r[1] == bucket]
        if not picked:
            continue
        print("=== %s (%d)" % (bucket, len(picked)))
        for code, _b, why, names, beat in picked:
            print("  %-18s %s" % (code, why))
            if verbose:
                print("      %s" % beat[:160])
        print("")
    free = counts.get(SINGLE_READY, 0) + counts.get(REACTION, 0)
    print("[reframe] %d of %d could be single-character coverage without changing "
          "what happens in the scene. %d need a real two-shot."
          % (free, len(rows), counts.get(CONTACT, 0) + counts.get(BOTH_ACTIVE, 0)))
    print("[reframe] Nothing was changed. Every row shows why it landed where it "
          "did, so any of them can be argued with by reading the beat.")
    return 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    N = ["PILOTCHARA", "PILOTCHARB"]

    # FABRICATED beats exercising the same classify() code paths as the
    # original real SHOW01 fixtures did (not actual show content).
    b_single = ("PilotCharA stares at the floor for a long moment, then exhales "
                "slowly and turns his face toward the window, saying nothing.")
    ck("a beat naming one character is SINGLE-READY",
       classify(b_single, N)[0] == SINGLE_READY)
    ck("...and it says which character it described",
       "PILOTCHARA" in classify(b_single, N)[1])

    b_contact = ("PilotCharA sets the mug of coffee down on the counter, then "
                 "turns to PilotCharB and rests a hand on his shoulder. PilotCharB flinches at the touch.")
    ck("CANARY a beat with physical interaction is CONTACT, not reframeable",
       classify(b_contact, N)[0] == CONTACT)

    b_react = "PilotCharB realizes what PilotCharA is not saying, and it hits him harder than the words would have."
    ck("a reaction beat is REACTION",
       classify(b_react, N)[0] == REACTION)
    ck("...and it says the other character can be off-camera",
       "off-camera" in classify(b_react, N)[1])

    b_both = "PilotCharA yells something wordless at the rafters. PilotCharB crosses his arms and stares him down without moving."
    ck("two characters acting without contact is BOTH-ACTIVE",
       classify(b_both, N)[0] == BOTH_ACTIVE)

    # ORDERING: contact must beat everything, or a hug gets reframed as a single.
    ck("CANARY contact wins over a one-name beat -- a single cannot show a touch",
       classify("PilotCharA rests a hand on the shoulder.", N)[0] == CONTACT)
    ck("CANARY contact wins over a reaction phrase too",
       classify("PilotCharB watches, then hugs him.", N)[0] == CONTACT)

    # THE TWO THE FIRST VERSION GOT WRONG, FABRICATED but shape-preserving, now permanent canaries.
    b_offer = ("PilotCharA picks up the small wrapped gift from the shelf and holds "
               "it out toward PilotCharB, offering it with both hands, watching for "
               "his reaction.")
    ck("CANARY 'offering' and 'holds it out' are CONTACT, which literals missed",
       classify(b_offer, N)[0] == CONTACT)
    b_door = ("PilotCharA gets up out of the booth and turns toward the exit. Behind him, "
              "PilotCharB leans forward and points at the floor, calling for him to wait.")
    ck("CANARY 'turns toward the exit' is NOT a reaction to a person",
       classify(b_door, N)[0] == BOTH_ACTIVE)

    ck("an empty beat is left as a two-hander, never guessed into a single",
       classify("", N)[0] == BOTH_ACTIVE and classify(None, N)[0] == BOTH_ACTIVE)
    ck("a beat naming NOBODY is single-ready but says so",
       "nobody" in classify("The room is empty and quiet.", N)[1])
    ck("name matching is word-bounded, not substring",
       names_present("PILOTCHARBets hang on the door", N) == [])
    ck("case and spacing do not matter",
       classify("PILOTCHARA   NODS  slowly.", N)[0] == SINGLE_READY)

    class _Stub(object):
        def find(self, et, *a, **k):
            if et == "Asset":
                return [{"id": 1, "code": "SHOW_CHAR_PILOTCHARA"},
                        {"id": 2, "code": "SHOW_CHAR_PILOTCHARB"}]
            return [{"code": "S1", "assets": [{"id": 1}, {"id": 2}],
                     "sg_action_beat": b_single, "sg_script_beat": ""},
                    {"code": "S2", "assets": [{"id": 1}],
                     "sg_action_beat": b_single, "sg_script_beat": ""}]
    rows = audit(_Stub().find("Shot"), {1: "SHOW_CHAR_PILOTCHARA", 2: "SHOW_CHAR_PILOTCHARB"})
    ck("only TWO-character shots are audited", [r[0] for r in rows] == ["S1"])

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default="SHOW01")
    ap.add_argument("--verbose", action="store_true", help="print each beat")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), episode=a.episode, verbose=a.verbose)


if __name__ == "__main__":
    sys.exit(main())
