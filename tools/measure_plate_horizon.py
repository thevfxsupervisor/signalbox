"""Measure a plate's horizon by TESTING whether its camera is level, not by fitting its lines.

This is the tool behind F447, and it exists because fitting failed three times in one hour.

WHAT FAILED, kept here so it is not retried. Fitting the vanishing point of the plate's receding
lines gave a horizon at 0.44 of frame. Splitting the same fit into a ceiling group and a floor group
gave 0.36 versus 0.68. Restricting to the floor planks and splitting left from right gave 0.82
versus 0.74. Three numbers disagreeing by a third of the frame are not a measurement. The causes,
once looked at rather than tuned around: the 'ceiling' segments were mostly poster and picture-frame
edges, which lie in WALL planes and therefore do not share the floor's vanishing point at all; and
the 'floor planks' at bottom-left include the cast light streaks, which converge on the LAMP. Every
one of those fits returned a confident number.

That is F425 repeating: the same doorway in the same plate was once measured at 0.37, then 0.73,
then 0.58 by two automated thresholds that were confidently wrong in OPPOSITE directions, so even
agreement between two methods would not have caught it.

WHAT WORKS INSTEAD: test a property that can FAIL. A level camera has no vertical vanishing point,
so real-world verticals stay exactly vertical in the image; a tilted one splays them systematically
with x. That is a pass/fail question about the plate, not a line fitted through whatever the edge
detector happened to return, and if the plate is level the horizon is then the principal point, the
image centre, with nothing left to fit.

The reported statistics are the honest part. A tight answer over wildly disagreeing inputs is the
failure mode this whole file is about, so the spread and the correlation are printed whether or not
the verdict is clean, and the two halves of the frame are solved INDEPENDENTLY: they are different
parts of the picture, so agreement is corroboration and disagreement is the finding.

Usage:  python measure_plate_horizon.py <plate.png> [annotated_out.png]
"""
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

# A segment must be this tall to vote, so that short cartoon hatching cannot outvote a door jamb.
MIN_RUN_PX = 70
# How far from vertical a segment may lean and still be treated as a real-world vertical.
MAX_LEAN = 0.20
# Above this correlation between lean and x, the splay is systematic and the camera is NOT level.
SPLAY_R = 0.25


def verticals(path):
    """Every near-vertical segment in the image, with its lean measured as dx per unit dy."""
    bgr = cv2.imread(path)
    if bgr is None:
        raise SystemExit("cannot read %s" % path)
    h, w = bgr.shape[:2]
    edges = cv2.Canny(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), 50, 150, apertureSize=3)
    segs = cv2.HoughLinesP(edges, 1, np.pi / 1440.0, threshold=60,
                           minLineLength=MIN_RUN_PX + 10, maxLineGap=5)
    out = []
    if segs is None:
        return out, w, h
    for x1, y1, x2, y2 in np.asarray(segs).reshape(-1, 4).tolist():
        dy, dx = abs(y2 - y1), abs(x2 - x1)
        if dy < MIN_RUN_PX or dx > MAX_LEAN * dy:
            continue
        out.append((x1, y1, x2, y2, (x2 - x1) / float(y2 - y1)))
    return out, w, h


def report(path, out_png=None):
    segs, w, h = verticals(path)
    if len(segs) < 12:
        raise SystemExit("only %d usable verticals: too few to test levelness" % len(segs))
    lean = np.array([s[4] for s in segs])
    xs = np.array([(s[0] + s[2]) / 2.0 for s in segs])

    print("%s  %dx%d" % (path, w, h))
    print("near-vertical segments: %d" % len(segs))
    print("lean dx/dy: median %+.4f  mean %+.4f  IQR %+.4f..%+.4f"
          % (np.median(lean), lean.mean(), np.percentile(lean, 25), np.percentile(lean, 75)))

    A = np.vstack([xs - w / 2.0, np.ones(len(xs))]).T
    (slope, icpt), *_ = np.linalg.lstsq(A, lean, rcond=None)
    r = float(np.corrcoef(xs, lean)[0, 1])
    print("lean vs x: slope %+.3e per px, intercept %+.4f, pearson r %+.3f" % (slope, icpt, r))

    for name, sel in (("left  half", xs < w / 2), ("right half", xs >= w / 2)):
        ll = lean[sel]
        if len(ll) >= 5:
            print("  %s n=%3d  median lean %+.4f  (dx per 100px of dy: %+.1f px)"
                  % (name, len(ll), np.median(ll), np.median(ll) * 100))

    level = abs(r) < SPLAY_R
    if level:
        print("VERDICT: verticals show no systematic splay, so the camera is LEVEL.")
        print("         The horizon is the principal point: y %.1f (0.500 of frame)." % (h / 2.0))
    else:
        print("VERDICT: systematic splay (r %+.3f), so the camera is TILTED and the horizon is NOT"
              % r)
        print("         the image centre. Do not use the level-camera shortcut on this plate.")

    if out_png:
        im = Image.open(path).convert("RGB")
        d = ImageDraw.Draw(im)
        for x1, y1, x2, y2, _ in segs:
            d.line([(x1, y1), (x2, y2)], fill=(0, 255, 0), width=2)
        if level:
            d.line([(0, h / 2.0), (w, h / 2.0)], fill=(255, 0, 255), width=3)
            d.text((8, h / 2.0 - 16), "horizon y=%d (level camera)" % (h / 2), fill=(255, 0, 255))
        im.save(out_png)
        print("wrote", out_png)
    return level


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    report(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
