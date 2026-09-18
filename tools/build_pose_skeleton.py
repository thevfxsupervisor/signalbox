"""Author an OpenPose skeleton for the 0130 beat by hand, as a CONTROL INPUT rather than a matte.

WHY A SKELETON AND NOT A CUT-OUT. F355 measured tonight that the blocking image's ORIENTATION reaches
the action: with frontal cut-outs the bowl the beat asks for never appears, and with the two figures
turned toward each other it appears in most cells, prompt unchanged. So orientation is worth
supplying. But supplying it by matting artwork is what produced the colour-key defect Geoff found
(background showing through the glasses), and Geoff had already retired per-frame compositing once,
on 2026-08-08, after the same class of matte failure in the other direction.

**A skeleton carries orientation and pose with no matte at all.** There is nothing to key, so the
entire defect class disappears rather than being fixed. It is also what the workshop architecture
called a low-fidelity structural harness.

WHAT THIS IS DRAWN FOR: base Qwen-Image through Qwen-Image-InstantX-ControlNet-Union. F231 measured
that the same ControlNet scores 3.11 with NO dose response on the instruction-edit model and controls
scale to within 8 percent on the BASE model, because the edit model concatenates reference tokens
into full self-attention with no spatial control point (F149). So a spatial conditioner has to attach
to the base model, and the base model has no character reference and no set reference. **This
therefore tests GEOMETRY ONLY. The figures will not be PilotCharA and PilotCharB and the room will not be the
approved bedroom.** That limit is registered, not discovered later.

THE BEAT, SHOW01 A_0130: Picture 1 picks up a bowl of pasta and holds it out toward the man in
Picture 2, offering it with both hands, watching his face. So the LEFT figure faces RIGHT with both
arms extended forward at chest height, and the RIGHT figure faces LEFT.

Rendering follows the OpenPose BODY-18 convention (limb and joint colours, joints as discs, limbs as
tapered ellipses on black), because that is what the ControlNet was trained to read. Drawing a
skeleton in some other colour scheme would be a different image that merely looks like a skeleton.
"""
import math
import os

from PIL import Image, ImageDraw

OUT_DIR = os.environ.get("GENVIDEO_SCRATCH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch")) + "/pose_skeleton"
W, H = 1280, 704

# OpenPose BODY-18 joint order.
NAMES = ["nose", "neck", "rsho", "relb", "rwri", "lsho", "lelb", "lwri", "rhip", "rkne", "rank",
         "lhip", "lkne", "lank", "reye", "leye", "rear", "lear"]
IDX = {n: i for i, n in enumerate(NAMES)}

# The canonical 17 limb connections and the canonical 18 joint colours, in the reference order.
LIMBS = [("neck", "rsho"), ("neck", "lsho"), ("rsho", "relb"), ("relb", "rwri"),
         ("lsho", "lelb"), ("lelb", "lwri"), ("neck", "rhip"), ("rhip", "rkne"),
         ("rkne", "rank"), ("neck", "lhip"), ("lhip", "lkne"), ("lkne", "lank"),
         ("neck", "nose"), ("nose", "reye"), ("reye", "rear"), ("nose", "leye"),
         ("leye", "lear")]
COLOURS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0),
           (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255),
           (0, 0, 255), (85, 0, 255), (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]


def figure(cx, feet_y, height, facing, arms):
    """A BODY-18 keypoint dict for one standing figure.

    Proportions are a 7.5-head adult scaled to `height` (nose to ankle), which is the standard
    figure-drawing canon rather than a guess. `facing` is +1 for facing right and -1 for facing left,
    and it compresses the shoulder and hip separation the way a profile actually reads: in a true
    profile the left and right joints project almost on top of each other.
    """
    assert facing in (1, -1)
    h = float(height)
    top = feet_y - h                      # crown
    head = h / 7.5
    nose_y = top + head * 0.55
    neck_y = top + head * 1.05
    hip_y = top + h * 0.53
    knee_y = top + h * 0.76
    sho_w = h * 0.115                     # half-width of the shoulders in a frontal view
    hip_w = h * 0.085
    d = 0.30                              # profile foreshortening of paired joints

    def pair(w, y):
        """Right and left of a symmetric pair, foreshortened toward the facing direction."""
        near = cx + facing * w * d
        far = cx - facing * w * d
        return (near, y), (far, y)

    (rsho, lsho) = pair(sho_w, neck_y + head * 0.15)
    (rhip, lhip) = pair(hip_w, hip_y)

    k = {
        "nose": (cx + facing * head * 0.42, nose_y),
        "neck": (cx, neck_y),
        "rsho": rsho, "lsho": lsho,
        "rhip": rhip, "lhip": lhip,
        "rkne": (rhip[0] + facing * h * 0.012, knee_y),
        "lkne": (lhip[0] + facing * h * 0.012, knee_y),
        "rank": (rhip[0], feet_y),
        "lank": (lhip[0], feet_y),
        # The head is a CLUSTER, not a horizontal bar. The first version put eyes and ears within
        # 0.12 head of the nose vertically, which rendered as a flat smear with no head in it.
        # Eyes sit above the nose, ears above and BEHIND (opposite the facing direction).
        "reye": (cx + facing * head * 0.28, nose_y - head * 0.30),
        "leye": (cx + facing * head * 0.14, nose_y - head * 0.32),
        "rear": (cx - facing * head * 0.06, nose_y - head * 0.34),
        "lear": (cx - facing * head * 0.20, nose_y - head * 0.36),
    }

    upper, fore = h * 0.155, h * 0.145
    if arms == "offering":
        # Both arms forward, elbows CHARJed and DOWN, forearms rising to hands held together at
        # roughly sternum height. The first version put the wrists at shoulder height and let the
        # two arms collapse onto one horizontal bar, which reads as pointing, not offering.
        chest_y = k["rsho"][1] + (hip_y - k["rsho"][1]) * 0.42
        for s, e, w, sgn in (("rsho", "relb", "rwri", 1), ("lsho", "lelb", "lwri", -1)):
            sx, sy = k[s]
            k[e] = (sx + facing * upper * 0.45, sy + upper * 0.90)
            k[w] = (k[e][0] + facing * fore * 1.05, chest_y + sgn * h * 0.018)
    elif arms == "down":
        # Arms hang at the sides. On a profile figure the shoulders nearly coincide, so offsetting
        # the two arms by a few pixels each side of centre makes them CROSS. Hang them along the
        # facing axis instead: near arm slightly forward, far arm slightly back.
        for s, e, w, sgn in (("rsho", "relb", "rwri", 1), ("lsho", "lelb", "lwri", -1)):
            sx, sy = k[s]
            k[e] = (sx + facing * sgn * h * 0.055, sy + upper)
            k[w] = (k[e][0] + facing * sgn * h * 0.030, k[e][1] + fore)
    else:
        raise SystemExit("unknown arm pose %r" % arms)
    return k


def draw(im, k):
    d = ImageDraw.Draw(im)
    for i, (a, b) in enumerate(LIMBS):
        (x0, y0), (x1, y1) = k[a], k[b]
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 1:
            continue
        # OpenPose renders a limb as an ellipse along the bone, not a plain line.
        ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
        limb = Image.new("RGBA", (max(2, int(L)), 9), (0, 0, 0, 0))
        ImageDraw.Draw(limb).ellipse([0, 0, max(1, int(L)) - 1, 8], fill=COLOURS[i] + (255,))
        limb = limb.rotate(-ang, expand=True, resample=Image.BICUBIC)
        im.paste(limb, (int((x0 + x1) / 2 - limb.width / 2),
                        int((y0 + y1) / 2 - limb.height / 2)), limb)
    for n, i in IDX.items():
        x, y = k[n]
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=COLOURS[i])


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    im = Image.new("RGB", (W, H), (0, 0, 0))

    # LEFT figure faces RIGHT and offers; RIGHT figure faces LEFT and stands. Feet at y 660 leaves
    # floor below them, unlike the approved plate, because the base model invents the room.
    PILOTCHARA = figure(cx=430, feet_y=660, height=470, facing=1, arms="offering")
    PILOTCHARB = figure(cx=830, feet_y=660, height=470, facing=-1, arms="down")

    # Assert the geometry BEFORE drawing, because a skeleton that looks plausible at thumbnail size
    # and encodes the wrong thing is exactly the failure this whole evening has been about.
    assert PILOTCHARA["nose"][0] > PILOTCHARA["neck"][0], "the left figure is not facing right"
    assert PILOTCHARB["nose"][0] < PILOTCHARB["neck"][0], "the right figure is not facing left"
    assert PILOTCHARA["rwri"][0] > PILOTCHARA["rsho"][0], "the offering arm does not reach forward"
    assert PILOTCHARA["rwri"][0] < PILOTCHARB["neck"][0], "the offered hands cross into the other figure"
    assert PILOTCHARB["lwri"][1] > PILOTCHARB["lsho"][1], "the standing figure's arms are not down"
    # Each defect the first drawing had gets its own guard, so a regression cannot pass silently.
    for name, k in (("PILOTCHARA", PILOTCHARA), ("PILOTCHARB", PILOTCHARB)):
        head_spread = max(k[n][1] for n in ("nose", "reye", "leye", "rear", "lear")) - \
            min(k[n][1] for n in ("nose", "reye", "leye", "rear", "lear"))
        assert head_spread > 12, \
            "%s's head keypoints span only %.0f px vertically, which renders as a flat bar not a " \
            "head" % (name, head_spread)
        assert k["nose"][1] < k["neck"][1], "%s's nose is not above the neck" % name
    # The standing figure's two arms must not cross each other.
    assert (PILOTCHARB["rwri"][0] - PILOTCHARB["neck"][0]) * (PILOTCHARB["lwri"][0] - PILOTCHARB["neck"][0]) < 0, \
        "the standing figure's wrists are on the same side of its spine, so the arms cross"
    # The offering hands must be BELOW the shoulders (an offer), not level with them (a point).
    assert PILOTCHARA["rwri"][1] > PILOTCHARA["rsho"][1] + 10, \
        "the offering hands are at shoulder height, which reads as pointing rather than offering"
    assert abs(PILOTCHARA["rwri"][1] - PILOTCHARA["lwri"][1]) > 8, \
        "the two offering hands are at identical height, so the arms collapse into one bar"
    for name, k in (("PILOTCHARA", PILOTCHARA), ("PILOTCHARB", PILOTCHARB)):
        for n, (x, y) in k.items():
            assert 0 <= x < W and 0 <= y < H, "%s %s is outside the frame at %.0f,%.0f" % (
                name, n, x, y)
        assert k["rank"][1] == 660, "%s is not standing on the same ground line" % name

    for k in (PILOTCHARA, PILOTCHARB):
        draw(im, k)
    p = os.path.join(OUT_DIR, "pose_0130_offer.png")
    im.save(p)

    gap = PILOTCHARB["neck"][0] - PILOTCHARA["neck"][0]
    reach = PILOTCHARA["rwri"][0] - PILOTCHARA["neck"][0]
    print("wrote %s  (%dx%d)" % (p, W, H))
    print("two figures, %.0f px apart, offering hands reach %.0f px forward (%.0f%% of the gap), "
          "both standing on y=660" % (gap, reach, 100.0 * reach / gap))


if __name__ == "__main__":
    main()
