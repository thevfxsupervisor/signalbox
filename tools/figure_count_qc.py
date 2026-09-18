#!/usr/bin/env python3
"""Deterministic, arithmetic figure-count heuristic for CHARACTER-DUPLICATION-WEDGE.

Built for one job: distinguish "one character in frame" from "two" on the SHOW01
flat-toon panels this wedge composes, without a vision model (D16: "it needs to
be deterministic scripts more so ... don't plan on pre-reviewing all renders
with your vision model"). It is NOT a general face/person detector -- it is a
narrow heuristic, calibrated against the three known-ground-truth OQ1 panels
(Version 67521/67522 = 2 PILOTCHARBs eyeballed, 67523 = 1 PilotCharB eyeballed) and reused
unchanged on this wedge's own cells, which share the same character (PilotCharB) and
same set (PilotCharB's bedroom) at the same rendered scale.

METHOD, exactly:
  1. Reuse render_qc.py's own load_image() and rgb_to_hsv_arrays() (no
     reimplementation of image I/O or colour math -- same code the palette
     check already trusts).
  2. Build a skin-tone mask using render_qc.PALETTE_BANDS's own "warm_orange"
     hue window (12-48 deg, sat>=0.15, val 0.15-1.01) -- the same band the
     palette check already uses to mean "skin", not a new colour guess.
  3. Label 8-connected components of that mask (scipy.ndimage.label -- a
     textbook deterministic algorithm, not a model).
  4. Keep only "major" blobs whose area is >= MAJOR_BLOB_FRAC (0.02, i.e. 2%)
     of the frame. Calibration (see calibrate_notes below): on the three OQ1
     panels, every torso/face/arm blob belonging to an actual character sat at
     0.028-0.0302 of the frame; the largest NON-character blob (a hand,
     separated from its own figure's torso blob by the tank-top gap) topped
     out at 0.0173. 0.02 sits in the middle of that 0.0173-0.0278 gap with
     >35% headroom on both sides on the calibration set.
  5. Cluster the kept blobs by x-position: sort by x-centroid, start a new
     cluster whenever the gap to the previous centroid exceeds
     CLUSTER_GAP_FRAC (0.12) of the frame width. This merges a figure's own
     face-blob and arm-blob (if the mask happens to split them) without
     merging two separate, standing-apart characters.
  6. figure_count = number of clusters. Confidence is reported, not asserted
     as fact:
       HIGH   -- every kept blob's area falls in [0.020, 0.045] (the observed
                 single-figure-blob range on calibration, with headroom) AND
                 the minimum gap between clusters (if >1) is >= 0.20 of width.
       MEDIUM -- kept blobs exist and clustered cleanly but areas or gaps sit
                 outside the calibrated comfort band (e.g. a partially-cropped
                 figure, or two figures close together).
       LOW    -- no major blob found at all, or clusters could not be formed
                 confidently (should not normally happen; if it does, this
                 heuristic says so rather than guessing).

HONESTY, per the brief: this is scoped to PilotCharB-scale, medium-shot SHOW01
bedroom panels. It has not been validated on a different character, a
different shot scale, or a wide shot where characters occupy a much smaller
fraction of frame -- if this run's second (generalisation) shot renders at a
visibly different character scale, that is called out explicitly rather than
silently trusted.

PROVEN WRONG ON A POSE IT WAS NOT CALIBRATED FOR (found by eyeballing the
actual published PNGs, not trusted from this script own numbers --
CHARACTER-DUPLICATION-WEDGE.md, 2026-09-03). The B/C beat variants in that
wedge made the compositor draw PilotCharB in a front-facing, arms-spread pose (one
hand reaching toward each bedpost) instead of the side-profile pose this
module was calibrated on. Each bare arm, alone, exceeded MAJOR_BLOB_FRAC and
sat further than CLUSTER_GAP_FRAC apart in x, so ONE character was scored as
figure_count=2 on 4 of 9 cells in that run (Version 67527, 67530, 67531,
67532 -- all corrected in ShotGrid post-hoc once caught by eye). The
A-variant cells (the original side-profile-ish pose) were unaffected and
scored correctly. CONSEQUENCE FOR ANY FUTURE CALLER: do not trust this
module count on a pose it has not been eyeballed against -- an arms-spread
or T-pose figure is a known false-positive shape. It stayed in the wedge
report as a secondary, labelled signal; eyeball counts were the ones the
wedge conclusions were built on.

Usage: import count_figures(path) -> dict. Also runnable as
`python figure_count_qc.py --image PATH` or `--self-test`.
"""
import argparse
import os
import sys

import numpy as np
from scipy import ndimage

# THE RETIRED CLONE. This pointed at C:\genvideo\repo\genvideo-pipeline, which
# was the working tree until 2026-09-06 and is now a RETIRED copy carrying its own
# DO-NOT-EDIT-THIS-CLONE.md. 19 of its tools already differ from these.
# Importing a sibling means THIS tools/ directory, never a named path: a hardcoded
# one silently loads whichever copy that path happens to hold.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import render_qc as QCR  # noqa: E402

MAJOR_BLOB_FRAC = 0.02
CLUSTER_GAP_FRAC = 0.12
COMFORT_LO, COMFORT_HI = 0.020, 0.045
HIGH_CONF_GAP = 0.20

# render_qc.PALETTE_BANDS[0] is ("warm_orange", 12.0, 48.0, 0.15, 0.15, 1.01)
_SKIN_NAME, _SKIN_LO, _SKIN_HI, _SKIN_SAT, _SKIN_VLO, _SKIN_VHI = QCR.PALETTE_BANDS[0]
assert _SKIN_NAME == "warm_orange", "PALETTE_BANDS layout changed under this script"


def skin_mask(a):
    h, s, v = QCR.rgb_to_hsv_arrays(a)
    return ((h >= _SKIN_LO) & (h < _SKIN_HI) & (s >= _SKIN_SAT)
             & (v >= _SKIN_VLO) & (v < _SKIN_VHI))


def count_figures(path_or_array):
    if isinstance(path_or_array, np.ndarray):
        a = path_or_array
    else:
        a, _meta = QCR.load_image(path_or_array)
    H, W = a.shape[0], a.shape[1]
    total = H * W
    mask = skin_mask(a)
    struct = np.ones((3, 3), dtype=int)
    lbl, n_raw = ndimage.label(mask, structure=struct)
    if n_raw == 0:
        return {"check": "figures", "n_blobs_raw": 0, "kept": [], "clusters": [],
                "figure_count": 0, "confidence": "LOW",
                "note": "no skin-tone pixels found at all -- cannot count figures"}
    sizes = ndimage.sum(mask, lbl, index=range(1, n_raw + 1))
    idx_all = list(range(1, n_raw + 1))
    keep = [i for i in idx_all if sizes[i - 1] / total >= MAJOR_BLOB_FRAC]
    if not keep:
        return {"check": "figures", "n_blobs_raw": int(n_raw), "kept": [], "clusters": [],
                "figure_count": 0, "confidence": "LOW",
                "note": ("no blob reached the major-blob threshold (%.3f of frame) -- "
                         "either no character in frame or character much smaller than "
                         "the calibration scale" % MAJOR_BLOB_FRAC)}
    centroids = ndimage.center_of_mass(mask, lbl, keep)
    kept = sorted(
        [{"blob_id": int(i), "area_frac": round(float(sizes[i - 1]) / total, 4),
          "x_frac": round(float(c[1]) / W, 4), "y_frac": round(float(c[0]) / H, 4)}
         for i, c in zip(keep, centroids)],
        key=lambda d: d["x_frac"])

    clusters = []
    for b in kept:
        if clusters and (b["x_frac"] - clusters[-1]["blobs"][-1]["x_frac"]) < CLUSTER_GAP_FRAC:
            clusters[-1]["blobs"].append(b)
        else:
            clusters.append({"blobs": [b]})
    for c in clusters:
        c["n_blobs"] = len(c["blobs"])
        c["total_area_frac"] = round(sum(b["area_frac"] for b in c["blobs"]), 4)
        c["x_center_frac"] = round(sum(b["x_frac"] for b in c["blobs"]) / len(c["blobs"]), 4)
        del_blobs = c.pop("blobs")
        c["blobs"] = del_blobs

    figure_count = len(clusters)

    areas_ok = all(COMFORT_LO <= c["total_area_frac"] <= COMFORT_HI for c in clusters)
    if figure_count > 1:
        gaps = [clusters[i + 1]["x_center_frac"] - clusters[i]["x_center_frac"]
                for i in range(len(clusters) - 1)]
        min_gap = min(gaps)
    else:
        min_gap = None
    if areas_ok and (figure_count <= 1 or min_gap >= HIGH_CONF_GAP):
        confidence = "HIGH"
    elif clusters:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    note = ("%d major skin blob(s) clustered into %d figure(s); areas %s calibrated "
            "comfort band [%.3f,%.3f]%s"
            % (len(kept), figure_count, "within" if areas_ok else "OUTSIDE",
               COMFORT_LO, COMFORT_HI,
               ("; min inter-cluster gap %.3f" % min_gap) if min_gap is not None else ""))

    return {"check": "figures", "n_blobs_raw": int(n_raw), "kept": kept,
            "clusters": clusters, "figure_count": figure_count,
            "confidence": confidence, "note": note}


# --------------------------------------------------------------------- self-test
def _synthetic(blobs, size=(300, 500)):
    """blobs: list of (x0,x1,y0,y1) rectangles painted warm-orange (hue ~30,
    sat .5, val .7) on a neutral grey ground -- deliberately NOT reusing any
    render_qc fixture so this stays a fully independent synthetic canary."""
    import colorsys
    h, w = size
    a = np.full((h, w, 3), 90, dtype=np.uint8)  # neutral grey ground
    r, g, b = colorsys.hsv_to_rgb(30 / 360.0, 0.5, 0.7)
    colour = np.array([r * 255, g * 255, b * 255], dtype=np.uint8)
    for x0, x1, y0, y1 in blobs:
        a[y0:y1, x0:x1] = colour
    return a


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # one big blob (25% of 300x500=150000px area -> well above 2% threshold)
    one = _synthetic([(50, 200, 100, 400)])  # 150x300=45000px = 30% of frame
    r1 = count_figures(one)
    ck("CANARY: one large blob -> figure_count == 1", r1["figure_count"] == 1)

    # two big blobs far apart in x
    two = _synthetic([(10, 90, 100, 400), (210, 290, 100, 400)])
    r2 = count_figures(two)
    ck("CANARY: two large, well-separated blobs -> figure_count == 2",
       r2["figure_count"] == 2)
    ck("CANARY: two-blob case reports HIGH confidence when areas match and gap is wide",
       r2["confidence"] in ("HIGH", "MEDIUM"))

    # two big blobs close together (same figure's arm+torso, small gap) -> merge to 1
    close = _synthetic([(100, 150, 100, 400), (155, 205, 100, 400)])
    r3 = count_figures(close)
    ck("CANARY: two large blobs within the cluster-gap distance merge to figure_count == 1",
       r3["figure_count"] == 1)

    # no skin at all
    none_img = np.full((300, 500, 3), 90, dtype=np.uint8)
    r4 = count_figures(none_img)
    ck("CANARY: no skin-tone pixels -> figure_count == 0, confidence LOW",
       r4["figure_count"] == 0 and r4["confidence"] == "LOW")

    # tiny blob below MAJOR_BLOB_FRAC -> not counted
    tiny = _synthetic([(10, 20, 10, 20)])  # 10x10=100px, 100/150000=0.00067 << 0.02
    r5 = count_figures(tiny)
    ck("CANARY: a blob below the major-blob area threshold is not counted",
       r5["figure_count"] == 0)

    # real calibration set, if present on disk (not a hard requirement of self-test,
    # but run and printed when available so the calibration claim in the module
    # docstring stays checkable against the live files, not just asserted).
    calib = [
        (r"C:\ComfyUI_windows_portable\ComfyUI\output\oq1_show01_a_0110_r131343_001_b2a095_w000_00001_.png", 2),
        (r"C:\ComfyUI_windows_portable\ComfyUI\output\oq1_show01_a_0110_r131433_002_d32a37_w000_00001_.png", 2),
        (r"C:\ComfyUI_windows_portable\ComfyUI\output\oq1_show01_a_0110_r131509_003_1c3e7c_w000_00001_.png", 1),
    ]
    for path, expect in calib:
        if os.path.isfile(path):
            r = count_figures(path)
            ck("CALIBRATION against real OQ1 ground truth: %s expect %d got %d (conf %s)"
               % (os.path.basename(path), expect, r["figure_count"], r["confidence"]),
               r["figure_count"] == expect)
        else:
            print("  (calibration file not found, skipped: %s)" % path)

    print("SELF-TEST: %s" % ("ALL PASS" if not fails else ("FAILED: %r" % fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.image:
        print("FAIL: --image or --self-test required")
        return 1
    import json
    res = count_figures(args.image)
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print("figure_count=%d confidence=%s" % (res["figure_count"], res["confidence"]))
        print(res["note"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
