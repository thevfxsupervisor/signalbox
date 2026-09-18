#!/usr/bin/env python3
"""Did the model REPAINT the room, or just RE-GRADE it? They score the same.

WHY THIS EXISTS. Comparing two checkpoints on P013, the raw measure this
project uses everywhere (mean absolute pixel difference against the source
plate) ranked the OLD checkpoint best by a margin well over the 5.0 noise
floor. Acting on that number would have rejected the new one.

**Nearly all of the penalty was a global colour grade.** The new checkpoint
rendered the same room, same furniture, same layout, in a colder cast. A
crude pixel difference cannot tell that apart from a room that was actually
redrawn, because both move every pixel a little.

THE DISCRIMINATOR. Match the candidate's per-channel mean and standard
deviation to the reference, THEN measure. What survives is structure. What
collapses was tone, and tone is recoverable with a grade; a repainted room
is not. Report BOTH numbers, never the matched one alone: a large raw
difference is still a real difference the operator sees on screen, and this
tool exists to say which KIND it is, not to explain one away.

SAME FRAMING ONLY. THIS IS THE LIMIT AND IT IS EASY TO MISS. The measure
assumes the candidate and the reference SHOW THE SAME VIEW. Comparing a
composed PANEL (an over-shoulder, a close-up) against a wide set plate reports
40-plus and calls it "structural" every time, because the camera moved, not
because the room did. Measured 2026-09-07 on SHOW01_A_0070: every candidate
scored 38 to 48 against the approved room and the number said nothing.

What IS valid across framings is the RELATIVE reading: score the same candidate
against the OLD design and the NEW one, and the smaller number says which room
it actually drew. That is how v001 was shown to have reproduced the superseded
room (34.3 old against 44.2 new) while v008 had not (57.9 old against 43.0 new).

So: absolute numbers only within one framing; across framings, compare two
references and read the difference.

REGION. Compare a region the edit was not asked to change (default: the left
half, where a composited character does not sit). A character legitimately
occupying part of the frame is not drift, and including it makes every arm
look equally bad.

    python set_preservation.py --ref set.png --cand a.png b.png
    python set_preservation.py --ref set.png --cand a.png --region full
    python set_preservation.py --self-test
"""
import argparse
import os
import sys


def _arr(path, size=None):
    import numpy as np
    from PIL import Image
    im = Image.open(path).convert("RGB")
    if size and im.size != size:
        im = im.resize(size)
    return np.asarray(im, dtype="float32")


def region(a, which="left"):
    """-> the slice of the array to judge on. 'left' is the default because a
    composited character sits on the right in this project's compose graph."""
    if which == "full":
        return a
    if which == "left":
        return a[:, :a.shape[1] // 2]
    if which == "right":
        return a[:, a.shape[1] // 2:]
    raise ValueError("unknown region %r" % which)


def tone_match(a, ref):
    """-> `a` re-graded to the reference's per-channel mean and deviation.

    This is the whole measurement. Anything still different afterwards is
    structure, because a global grade cannot move a wall."""
    import numpy as np
    sd = a.std((0, 1))
    return (a - a.mean((0, 1))) / (sd + 1e-6) * ref.std((0, 1)) + ref.mean((0, 1))


def compare(cand, ref, which="left"):
    """-> dict with the raw and tone-matched distances, and the verdict."""
    import numpy as np
    r = region(ref, which)
    c = region(cand, which)
    raw = float(np.abs(c - r).mean())
    matched = float(np.abs(tone_match(c, r) - r).mean())
    return {"raw": raw, "matched": matched, "recovered": raw - matched,
            "kind": classify(raw, matched)}


def classify(raw, matched, floor=5.0):
    """-> 'clean' | 'tone' | 'structural' | 'mixed'.

    The floor is this project's measured noise floor. Deliberately says
    'mixed' rather than picking a side when both halves are real: a caller
    that wants one word can have a wrong one somewhere else."""
    if raw <= floor:
        return "clean"
    if matched <= floor:
        return "tone"
    if raw - matched <= floor:
        return "structural"
    return "mixed"


def self_test():
    import numpy as np
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    rng = np.random.RandomState(0)
    ref = rng.uniform(40, 200, (64, 64, 3)).astype("float32")

    ck("an identical image is clean", compare(ref.copy(), ref)["kind"] == "clean")

    # A pure grade: shift and scale every channel. Structure is untouched.
    graded = ref * 0.7 + 40.0
    g = compare(graded, ref)
    ck("CANARY: a pure GRADE is called tone, not structure", g["kind"] == "tone")
    ck("CANARY: a pure grade is LARGE raw and near-zero matched",
       g["raw"] > 5.0 and g["matched"] < 1.0)

    # A structural change: repaint a block. Tone matching cannot undo it.
    painted = ref.copy()
    painted[:32, :16] = 255.0
    s = compare(painted, ref)
    ck("CANARY: a REPAINTED block survives tone matching",
       s["kind"] == "structural" and s["matched"] > 5.0)

    # The failure this tool was built for: a grade that HIDES as drift.
    ck("CANARY: a grade and a repaint of equal RAW size are told apart",
       classify(20.0, 0.5) == "tone" and classify(20.0, 18.0) == "structural")

    # Region: a change confined to the right half must not count on the left.
    right_only = ref.copy()
    right_only[:, 32:] = 0.0
    ck("CANARY: the left region ignores a right-half change",
       compare(right_only, ref, "left")["kind"] == "clean")
    ck("the same change IS seen on the full frame",
       compare(right_only, ref, "full")["kind"] != "clean")

    # tone_match must not be a no-op, which is how this check could pass hollow.
    ck("CANARY: tone_match actually moves a graded image toward the reference",
       float(np.abs(tone_match(graded, ref) - ref).mean())
       < float(np.abs(graded - ref).mean()))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", help="the source plate the edit was told to keep")
    ap.add_argument("--cand", nargs="*", default=[], help="render(s) to judge")
    ap.add_argument("--region", default="left", choices=("left", "right", "full"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.ref or not a.cand:
        ap.error("--ref and at least one --cand are required")
    ref = _arr(a.ref)
    size = (ref.shape[1], ref.shape[0])
    print("%-34s %8s %8s  %s" % ("render", "raw", "matched", "kind"))
    for c in a.cand:
        d = compare(_arr(c, size), ref, a.region)
        print("%-34s %8.2f %8.2f  %s"
              % (os.path.basename(c)[:34], d["raw"], d["matched"], d["kind"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
