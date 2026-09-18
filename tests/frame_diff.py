#!/usr/bin/env python3
"""Measure the difference between two frame sequences, with mean, spread and n.

Used for two things in Phase 2, and they are different questions:

  1. NOISE FLOOR: run the same inputs twice, independently, and measure how far
     apart they land. That is the smallest difference that means anything.
  2. PARAMETER REACHES MODEL: change one parameter with the seed held and
     measure. If the difference does not clear the floor from (1), the
     parameter did not reach the model and any story about its effect is noise.

A single mean is not a measurement. Every report carries mean, standard
deviation, min, max and n, because "the images differ by 4.1" with no spread
hides whether that is every frame differing a little or one frame differing a
lot.

Usage:
    python frame_diff.py --a DIR --b DIR [--pattern-a GLOB] [--pattern-b GLOB]
    python frame_diff.py --canary

Exit: 0 ok, 1 sequences not comparable, 2 canary failed.
"""
import argparse
import glob
import os
import sys

import numpy as np
from PIL import Image


def load(path):
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float64)


def compare(files_a, files_b):
    """Per-frame mean absolute difference, on a 0-255 scale."""
    diffs = []
    for fa, fb in zip(files_a, files_b):
        a, b = load(fa), load(fb)
        if a.shape != b.shape:
            raise ValueError("shape mismatch: %s %s vs %s %s"
                             % (os.path.basename(fa), a.shape, os.path.basename(fb), b.shape))
        diffs.append(float(np.abs(a - b).mean()))
    return diffs


def report(label, diffs):
    arr = np.array(diffs)
    print("%-28s n=%-3d mean=%8.5f  sd=%8.5f  min=%8.5f  max=%8.5f"
          % (label, arr.size, arr.mean(), arr.std(ddof=1) if arr.size > 1 else 0.0,
             arr.min(), arr.max()))
    return arr


def canary():
    """Prove the metric can both see a difference AND report zero for identity."""
    rng = np.random.default_rng(0)
    base = rng.integers(0, 255, size=(16, 16, 3)).astype(np.float64)

    same = float(np.abs(base - base).mean())
    if same != 0.0:
        print("CANARY FAILED: identical images reported a nonzero difference (%.6f)." % same)
        return False
    print("canary: identical input -> 0.000000 (metric does not invent differences)")

    nudged = base.copy()
    nudged[0, 0, 0] = (nudged[0, 0, 0] + 10) % 256
    one_px = float(np.abs(base - nudged).mean())
    if one_px <= 0.0:
        print("CANARY FAILED: a one-pixel change was reported as no difference.")
        return False
    print("canary: one pixel changed by 10 -> %.6f (metric detects a real change)" % one_px)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a")
    ap.add_argument("--b")
    ap.add_argument("--pattern-a", default="*.png")
    ap.add_argument("--pattern-b", default="*.png")
    ap.add_argument("--label", default="A vs B")
    ap.add_argument("--canary", action="store_true")
    ns = ap.parse_args()

    if ns.canary:
        if not canary():
            return 2
        if not (ns.a and ns.b):
            return 0

    if not (ns.a and ns.b):
        sys.stderr.write(__doc__)
        return 2

    fa = sorted(glob.glob(os.path.join(ns.a, ns.pattern_a)))
    fb = sorted(glob.glob(os.path.join(ns.b, ns.pattern_b)))
    if not fa or not fb:
        print("no frames matched (a=%d, b=%d)" % (len(fa), len(fb)))
        return 1
    if len(fa) != len(fb):
        print("frame count mismatch: a=%d b=%d - refusing to compare truncated sequences"
              % (len(fa), len(fb)))
        return 1

    diffs = compare(fa, fb)
    report(ns.label, diffs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
