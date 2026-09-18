"""Compose a two-figure OpenPose control image from the openposes.com library keypoints.

Geoff supplied the pack: https://openposes.com/ , direct zip at
https://openposes-storage.s3.ca-central-1.amazonaws.com/poses.zip
46 poses, each as a rendered PNG and a `pose_keypoints_2d` JSON (BODY-18, 18 joints x [x, y, conf])
on a 768x768 canvas. Categories: standing 19, sitting 9, dance 5, jumping 5, laying 3, flexing 3,
tpose 2.

WHY COMPOSE RATHER THAN USE THE PNGs DIRECTLY. Two reasons, both measured.
  1. F397: figure HEIGHT in the control image is what sets rendered figure scale, and that is the
     property we need to set deliberately. A library PNG has whatever height it was drawn at.
  2. The beat needs TWO figures in one frame at chosen positions, and the library ships one figure
     per file.
Working from the JSON keeps the library's real joint geometry and lets us set only placement and
size, which is exactly the split we want.

THE TURN. The library poses are frontal. F355 measured that turning the two figures TOWARD each
other is what makes the offered bowl appear at all, so a straight frontal drop-in risks
reintroducing the face-the-camera defect. `squash` compresses the pose horizontally about its own
centre line, which is what a body actually does in projection as it turns away from frontal. That
is an approximation of a turn, not a real one: a true profile would also occlude the far limbs, and
this cannot do that. Stated here so no finding built on it overclaims.

Renders in the OpenPose BODY-18 convention using the same draw code as the hand-authored skeletons
(`build_pose_skeleton.py`), so library and hand-built control images differ ONLY in their joints.
"""
import json
import os
import sys

from PIL import Image

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import build_pose_skeleton as BPS

LIB = r"C:\example\scratch\openposes_lib"
W, H = 1280, 704


def load(name):
    """-> {joint_name: (x, y)} in the library's own 768x768 canvas coordinates."""
    with open(os.path.join(LIB, "%s.json" % name), encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        doc = doc[0]
    flat = doc["people"][0]["pose_keypoints_2d"]
    assert len(flat) == 54, "expected 18 BODY-18 joints, got %d values" % len(flat)
    pts = [(flat[i * 3], flat[i * 3 + 1]) for i in range(18)]
    return {n: pts[i] for n, i in BPS.IDX.items()}


def place(k, cx, feet_y, height, squash=1.0, mirror=False):
    """Scale a pose to `height` (crown to lowest ankle), centre it on `cx`, stand it on `feet_y`.

    `squash` < 1 compresses horizontally about the figure's own centre, approximating a turn away
    from frontal. `mirror` flips it left to right so two figures can face each other.
    """
    ys = [p[1] for p in k.values()]
    top, bottom = min(ys), max(k["rank"][1], k["lank"][1])
    span = bottom - top
    assert span > 1, "degenerate pose, crown to ankle span %.2f" % span
    s = float(height) / span
    xs = [p[0] for p in k.values()]
    mid = (min(xs) + max(xs)) / 2.0
    out = {}
    for n, (x, y) in k.items():
        dx = (x - mid) * s * squash
        if mirror:
            dx = -dx
        out[n] = (cx + dx, feet_y - (bottom - y) * s)
    return out


def compose(left, right, out_path, height=None, feet_y=None,
            left_cx=None, right_cx=None, squash=1.0, mirror_right=True):
    """Two library poses on one black canvas, at chosen size and position."""
    height = height or int(H * 0.42)
    feet_y = feet_y or int(H * 0.93)
    left_cx = left_cx if left_cx is not None else W * 0.36
    right_cx = right_cx if right_cx is not None else W * 0.64
    im = Image.new("RGB", (W, H), (0, 0, 0))
    BPS.draw(im, place(load(left), left_cx, feet_y, height, squash, mirror=False))
    BPS.draw(im, place(load(right), right_cx, feet_y, height, squash, mirror=mirror_right))
    im.save(out_path)
    return out_path


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "standing_03"
    b = sys.argv[2] if len(sys.argv) > 2 else "standing_19"
    p = compose(a, b, os.path.join(LIB, "composed_%s_%s.png" % (a, b)))
    print("wrote %s from %s and %s" % (p, a, b))
