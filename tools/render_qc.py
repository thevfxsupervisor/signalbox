#!/usr/bin/env python3
r"""Deterministic, arithmetic-only QC for a rendered still. No model of any kind.

WHY THIS EXISTS -- Geoff, 2026-09-03, verbatim:

    "in the long term we can't rely on claude vision to evaluate and gate keep
     things. it needs to be deterministic scripts more so. claude -p (or
     eventually possibly local free models running in ollama) can be used for
     incorporating notes into new prompts, but don't plan on pre-reviewing all
     renders with your vision model, too expensive at scale (maybe eventually
     something like qwen image interrogator). just publish them all and let the
     operator review."

The cost argument is the whole argument. A 121-shot episode with retries is
thousands of renders; `tests/attribute_check.py` spends THREE `claude -p` calls
per render (--repeat 3, unanimous vote) on a judgement the operator makes anyway
when the Version lands in their review queue. This module spends none. It reads
pixels, does arithmetic, and attaches the resulting NUMBERS to the published
Version so the operator sees them while reviewing.

WHAT THIS IS NOT. It is not a pass/fail gate on subjective quality, and it must
never become one. "3 of 4 view panels are within 2% of each other" is a fact and
belongs here. "This turnaround is bad" is the operator's sentence, not this
tool's. So the only HARD FAILs below are mechanical -- unreadable file, wrong
resolution, degenerate/blank output. Everything aesthetic is reported as a
number with the measurement that produced it, and the exit code stays 0.

THE FOUR CHECKS, and the measured failure each one exists for:

 1. DUPLICATE VIEWS IN A TURNAROUND -- the highest-value check, because it
    catches a defect that is invisible in a thumbnail and fatal to a turnaround.
    Measured in pass 3 (docs/METHOD.md, "Gap 5"): four draws of one
    recipe, differing only in seed, produced "anything from three identical
    front views to a correct four-view sheet". P3_20 is three near-identical
    fronts; P3_21 is front/profile/front; P3_11's middle view duplicates its
    front; P3_10's third view is a MIRRORED front. Turnaround quality is a seed
    property, so "a batch that needs turnarounds needs a retry-and-check policy"
    -- and this is the check that policy needs. Panels are segmented, normalised
    and compared pairwise by mean absolute pixel difference AND by a 64-bit
    perceptual (DCT) hash, each also against the horizontally-flipped panel so a
    mirrored repeat is not scored as a distinct view.

 2. INVENTED TEXT -- OCR, via tesseract. Anima renders unprompted text wherever
    the prompt describes a printed surface, and the 10-step speed recipe makes
    it LEGIBLE where 40 steps leaves scribble (ANIMA-PASS3.md, "Unprompted
    text"; MEMORY: anima-invents-text-on-printed-surfaces). No prompt rule fixes
    it and no guard exists, so the operator has to be told where it is. Reported
    as strings + per-token confidence, never as a verdict -- see
    check_text()'s own honesty note about the false-positive rate measured
    across all 71 bake-off cells.

 3. PALETTE ADHERENCE -- percentage of pixels outside the design language's
    stated envelope. Pass 3 measured an off-palette mustard-yellow that a
    STRENGTHENED positive palette clause made worse rather than better (P3_23:
    the yellow "relocates and grows", and a crimson blob appears in the hair).
    Neither colour is in the register. See PALETTE_BANDS for exactly how the
    register's colour WORDS were turned into hue windows, and why that mapping
    is the first thing to tune.

 4. DEGENERATE OUTPUT -- truncated/unreadable file, wrong resolution, near-zero
    variance, large flat regions. These are the only hard FAILs.

COST. ~0.15s of numpy per 1280x704 image plus ~0.5s of tesseract (two page-
segmentation passes as one subprocess each). No model is loaded, nothing is
downloaded, no network call is made, and no `claude` subprocess is spawned.

OCR INSTALL (recorded exactly, because "it works on one machine" is not an install
step) -- see docs/METHOD.md for the full account:
    tesseract 5.4.0.20240606 (UB-Mannheim build), extracted with 7-Zip from
        tesseract-ocr-w64-setup-5.4.0.20240606.exe to C:\genvideo\ocr\tesseract\
        -- NOT installed via winget: that manifest is machine-scope and raises a
        UAC prompt this session cannot answer. Extracting the NSIS installer
        gives an identical, fully portable tree with no elevation.
    pytesseract 0.3.13 (+ packaging 26.3), into C:\genvideo\venv via
        `uv pip install --python C:\genvideo\venv\Scripts\python.exe pytesseract`
Override the binary with $TESSERACT_EXE or --tesseract.

Usage:
    python render_qc.py --image PATH.png
    python render_qc.py --image PATH.png --views 3 --expect-size 1280x704
    python render_qc.py --image PATH.png --json
    python render_qc.py --glob "output/anima_bakeoff/*.png" --views 3 --tsv
    python render_qc.py --self-test        offline, no SG, no network

Exit codes: 0 checks ran and no MECHANICAL failure (aesthetic numbers, however
bad, do not change this); 1 a mechanical FAIL (unreadable, wrong resolution,
degenerate); 2 a check could not RUN at all (invariant 3: loud failures -- a
check that cannot run says so and exits non-zero, it never returns a passing
default).
"""
import argparse
import glob as globmod
import json
import os
import sys

import numpy as np
from PIL import Image

EXIT_OK, EXIT_FAIL, EXIT_ERROR = 0, 1, 2

# tesseract is resolved explicitly rather than trusted to be on PATH, for the
# same reason attribute_check.resolve_claude_bin() does it: this module will be
# called from a service/scheduled-task shell that never saw an interactive PATH
# edit, and a stale binary earlier on PATH is a worse silent failure than a
# redundant explicit path. The portable extract is tried FIRST, then the two
# standard installer locations, then PATH.
KNOWN_TESSERACT_PATHS = [
    r"C:\genvideo\ocr\tesseract\tesseract.exe",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def log(m):
    print("[renderqc] %s" % m, flush=True)


class QCError(Exception):
    """A check could not RUN. Distinct from a check that ran and failed."""


# --------------------------------------------------------------------- loading
def load_image(path):
    """-> (uint8 HxWx3 array, meta dict). Raises QCError on anything that means
    "this file is not a usable image": missing, empty, undecodable, truncated.
    Truncation is caught by forcing a FULL decode (Image.load()) rather than by
    Image.open(), which only reads the header -- a half-written PNG opens fine
    and reports the right size, which is exactly the silent pass this must not
    give. PIL's LOAD_TRUNCATED_IMAGES is deliberately left off."""
    if not isinstance(path, (str, bytes, os.PathLike)):
        raise QCError("not an image path at all: %r" % (path,))
    if not os.path.isfile(path):
        raise QCError("image does not exist on disk: %s" % path)
    size_bytes = os.path.getsize(path)
    if size_bytes == 0:
        raise QCError("image file is zero bytes: %s" % path)
    try:
        im = Image.open(path)
        fmt = im.format
        im.load()                       # forces the full decode; truncation raises here
        rgb = im.convert("RGB")
    except QCError:
        raise
    except Exception as exc:            # noqa: BLE001 - PIL raises many types
        raise QCError("could not decode %s: %s: %s"
                      % (path, type(exc).__name__, exc)) from exc
    a = np.asarray(rgb, dtype=np.uint8)
    if a.ndim != 3 or a.shape[2] != 3 or a.shape[0] < 2 or a.shape[1] < 2:
        raise QCError("decoded to an unusable array shape %r: %s" % (a.shape,))
    return a, {"path": path, "bytes": int(size_bytes), "format": fmt,
               "width": int(a.shape[1]), "height": int(a.shape[0])}


# ------------------------------------------------------------ shared primitives
def background_colour(a):
    """Median of a 4px border. Character sheets are rendered on a plain ground
    ("plain white background for character sheets, subject isolated" - the style
    register), so the border median is the ground even when the figure touches
    an edge. Median, not mean, so a figure crossing one border does not drag it."""
    h, w, _ = a.shape
    b = min(4, h // 2, w // 2) or 1
    border = np.concatenate([a[:b].reshape(-1, 3), a[h - b:].reshape(-1, 3),
                             a[:, :b].reshape(-1, 3), a[:, w - b:].reshape(-1, 3)])
    return np.median(border, axis=0)


def ink_mask(a, tol=28):
    """True where a pixel differs from the ground by more than `tol` in any
    channel -- i.e. "there is drawing here". Chebyshev distance, not Euclidean:
    a hue shift at constant luminance is still ink."""
    bg = background_colour(a)
    return np.abs(a.astype(np.int16) - bg.astype(np.int16)).max(axis=2) > tol


def _runs(flags):
    out, s = [], None
    for i, v in enumerate(flags):
        if v and s is None:
            s = i
        elif not v and s is not None:
            out.append((s, i))
            s = None
    if s is not None:
        out.append((s, len(flags)))
    return out


def _smooth(p, k):
    k = max(1, int(k) | 1)
    return np.convolve(p, np.ones(k) / k, mode="same")


def segment_panels(a, expect_views=None, col_thresh=0.006,
                   min_gap_frac=0.012, min_width_frac=0.06):
    """Split a contact sheet into its view panels. -> (list[(x0, x1)], method).

    TWO METHODS, and which one ran is reported, because they are not equally
    trustworthy and the caller deserves to know which produced the numbers:

      "gap"   -- columns whose ink fraction is essentially zero are treated as
                 the space between figures. This is the accurate one: it finds
                 each figure's TRUE extent, so the normalised crops line up and
                 the distances mean what they say. It fails, by under-splitting,
                 when two views touch or overlap -- which real sheets do (P3_22's
                 four views segment as ONE block; P3_17's three as two).
      "equal" -- fall back: cut the content bounding box into `expect_views`
                 parts, each cut SNAPPED to the lowest-ink column within +/-40%
                 of a slot width. Always returns the asked-for count. Less exact
                 crops, so its distances run a few points higher than "gap"'s on
                 the same image; still separated the known-duplicate cells from
                 the known-good ones across the whole bake-off corpus.

    With expect_views set, "gap" is used only if it agrees with the expectation;
    a disagreement means views are touching, and equal-split is the honest
    recovery. With expect_views unset, whatever "gap" finds is returned as-is --
    the count itself is then a reported fact, not a silent assumption."""
    m = ink_mask(a)
    h, w = m.shape
    if not m.any():
        return [], "none (no ink)"

    prof = m.mean(axis=0)
    blocks = _runs(prof > col_thresh)
    min_gap = max(2, int(w * min_gap_frac))
    merged = []
    for r in blocks:
        if merged and r[0] - merged[-1][1] < min_gap:
            merged[-1] = (merged[-1][0], r[1])
        else:
            merged.append((r[0], r[1]))
    gap_panels = [(x0, x1) for x0, x1 in merged if x1 - x0 >= int(w * min_width_frac)]

    if expect_views is None:
        return gap_panels, "gap"
    if len(gap_panels) == expect_views:
        return gap_panels, "gap"

    cols = np.where(m.any(axis=0))[0]
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    span = x1 - x0
    if span < expect_views * 4:
        return gap_panels, "gap"
    sp = _smooth(m[:, x0:x1].mean(axis=0), span / 64.0)
    cuts = []
    for i in range(1, expect_views):
        c = i * span / expect_views
        lo = max(1, int(c - span / (2.5 * expect_views)))
        hi = min(span - 1, int(c + span / (2.5 * expect_views)))
        cuts.append(x0 + lo + int(np.argmin(sp[lo:hi])) if hi > lo else x0 + int(c))
    bounds = [x0] + cuts + [x1]
    return [(bounds[i], bounds[i + 1]) for i in range(expect_views)], "equal"


def crop_panel(a, m, x0, x1, pad=2):
    """Crop one panel to its own vertical ink extent, so a short profile view and
    a tall front view are compared on their subjects rather than on how much
    empty ground each happens to sit in."""
    sub = m[:, x0:x1]
    rows = np.where(sub.any(axis=1))[0]
    if len(rows) == 0:
        return None
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    return a[max(0, y0 - pad):min(a.shape[0], y1 + pad), x0:x1]


NORM_W, NORM_H = 128, 192


def normalise_panel(c, w=NORM_W, h=NORM_H):
    """Aspect-preserving fit onto a ground-coloured canvas. NOT a plain resize:
    stretching a narrow profile view to a front view's aspect makes two genuinely
    different views look alike, which is a false NEGATIVE on the one check that
    matters most here."""
    im = Image.fromarray(c)
    sc = min(w / im.width, h / im.height)
    nw, nh = max(1, int(im.width * sc)), max(1, int(im.height * sc))
    canvas = Image.new("RGB", (w, h), tuple(int(v) for v in background_colour(c)))
    canvas.paste(im.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
    return np.asarray(canvas, dtype=np.uint8)


_DCT32 = None


def _dct_matrix(n=32):
    global _DCT32
    if _DCT32 is None or _DCT32.shape[0] != n:
        k = np.arange(n)
        mat = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n)) * np.sqrt(2.0 / n)
        mat[0] /= np.sqrt(2)
        _DCT32 = mat
    return _DCT32


def phash(c):
    """64-bit perceptual hash: 32x32 greyscale -> DCT-II -> low-frequency 8x8 ->
    threshold at the median (DC excluded). Plain numpy, no extra dependency.
    Complements mean-absolute-difference: MAD is sensitive to a palette shift
    that leaves the drawing identical; pHash is sensitive to the drawing."""
    g = np.asarray(Image.fromarray(c).convert("L").resize((32, 32), Image.LANCZOS),
                   dtype=np.float64)
    d = _dct_matrix(32)
    block = (d @ g @ d.T)[:8, :8].flatten()
    return block > np.median(block[1:])


def hamming(x, y):
    return int(np.count_nonzero(x != y))


def mad(x, y):
    return float(np.abs(x.astype(np.int16) - y.astype(np.int16)).mean())


# ------------------------------------------------------------- 1. view duplicates
# Measured across the bake-off corpus against ANIMA-PASS3.md's and
# ANIMA-LORA-WEDGE.md's own per-cell verdicts, each reached by a human opening
# the image:
#     pairs a human called a repeated view   pHash 2, 2, 2, 2, 4, 8
#     sheets a human called three distinct   pHash minimum 14, 16, 18, 24
#     the awkward middle                     P3_19's two fronts at 12
# The bands nearly touch at 12-14. There is no threshold that separates them
# cleanly, so this is a FLAG, not a FAIL: at <= NEAR_DUPLICATE_PHASH the pair is
# called out for the operator's eye, and every raw distance is printed either
# way so the operator can see how close a call it was. Nothing here changes the
# exit code.
NEAR_DUPLICATE_PHASH = 10

# THE MIRROR SIGNAL IS REPORTED SEPARATELY AND LABELLED WEAKER, because that is
# what measuring it showed. P3_10's actual defect -- "view 3 is a mirrored second
# front, not a back" -- does NOT come back close on either comparison (16 plain,
# 30 mirrored): a re-imagined mirrored front is not a mirrored COPY. Meanwhile a
# genuine back view of a roughly symmetric character DOES score close against a
# mirrored front (P3_19: 6). So on real renders the mirror test has, so far,
# produced one false positive and zero true positives, while catching an exact
# mirrored duplicate perfectly in synthesis. It is kept because an exact mirrored
# repeat is a real thing a sampler can emit and this is the only check that would
# see it -- but it is never merged into the primary flag.


def check_views(a, expect_views=None):
    bounds, method = segment_panels(a, expect_views)
    m = ink_mask(a)
    panels, kept = [], []
    for x0, x1 in bounds:
        c = crop_panel(a, m, x0, x1)
        if c is not None and c.size:
            panels.append(normalise_panel(c))
            kept.append([int(x0), int(x1)])

    res = {"check": "views", "status": "OK", "method": method,
           "panels_found": len(panels), "panels_expected": expect_views,
           "panel_bounds": kept, "pairs": [], "near_duplicate_pairs": [],
           "mirrored_repeat_pairs": [], "min_phash": None,
           "min_phash_mirrored": None, "note": ""}
    if len(panels) < 2:
        res["note"] = ("only %d panel(s) segmented -- not a multi-view sheet, or the "
                       "views touch and could not be split" % len(panels))
        return res

    hashes = [phash(p) for p in panels]
    flipped = [p[:, ::-1] for p in panels]
    fhashes = [phash(p) for p in flipped]
    for i in range(len(panels)):
        for j in range(i + 1, len(panels)):
            d_same, h_same = mad(panels[i], panels[j]), hamming(hashes[i], hashes[j])
            d_mir, h_mir = mad(panels[i], flipped[j]), hamming(hashes[i], fhashes[j])
            mirrored = h_mir < h_same
            pair = {"a": i + 1, "b": j + 1,
                    "mad": round(d_same, 2), "phash": h_same,
                    "mad_mirrored": round(d_mir, 2), "phash_mirrored": h_mir,
                    "best_phash": min(h_same, h_mir), "mirrored_is_closer": bool(mirrored)}
            res["pairs"].append(pair)
            if h_same <= NEAR_DUPLICATE_PHASH:
                res["near_duplicate_pairs"].append(pair)
            elif h_mir <= NEAR_DUPLICATE_PHASH:
                res["mirrored_repeat_pairs"].append(pair)
    res["min_phash"] = min(p["phash"] for p in res["pairs"])
    res["min_phash_mirrored"] = min(p["phash_mirrored"] for p in res["pairs"])
    if expect_views is not None and len(panels) != expect_views:
        res["note"] = ("segmented %d panel(s) where %d views were expected"
                       % (len(panels), expect_views))
    return res


# ------------------------------------------------------------------- 2. OCR text
def resolve_tesseract(explicit=None):
    """Find a real tesseract. Never falls through to "assume PATH and let the
    subprocess raise something opaque" -- the caller gets a QCError naming every
    place that was looked in, so a missing OCR install is a reported blocker and
    not a check that quietly reports "no text found"."""
    tried = []
    for cand in ([explicit] if explicit else []) + [os.environ.get("TESSERACT_EXE")] \
            + KNOWN_TESSERACT_PATHS:
        if not cand:
            continue
        tried.append(cand)
        if os.path.isfile(cand):
            return cand
    import shutil
    tried.append("PATH lookup")
    found = shutil.which("tesseract")
    if found:
        return found
    raise QCError("could not resolve the `tesseract` binary. Tried: %s. Install it "
                  "(see this module's docstring) or pass --tesseract/$TESSERACT_EXE."
                  % ", ".join(tried))


# Page-segmentation modes. 11 = sparse text, 12 = sparse text with OSD. The
# DEFAULT mode (3, "fully automatic page segmentation") finds NOTHING on an
# illustration -- measured: zero tokens on P3_10, whose tee carries the legible
# "23WAR"/"E3WAR" that ANIMA-PASS3.md flags as the most operationally important
# finding of the pass. Two sparse passes are run and their results merged; a
# token found by BOTH is reported as agreeing, which is the closest thing to a
# corroborating signal available without a second engine.
OCR_PSM_MODES = (11, 12)
OCR_MIN_CORE_CHARS = 3


def check_text(a, tesseract=None, enabled=True):
    """OCR the whole image and report what came back, with confidences.

    HONESTY ABOUT WHAT THIS NUMBER IS WORTH, measured over all 71 bake-off cells
    against ANIMA-PASS3.md's per-cell "unprompted text" table:
      - It FINDS the real thing. P3_13's reported "SOPEK" comes back as SOREK at
        confidence 56, agreeing across both passes; P3_10's "23WAR" as SWAR at 8.
      - It also fires on cells the report records as text-free: P3_21 (a clean
        film-reel graphic) yields "208" at confidence 60. Line art, hatching and
        folds read as glyphs to a text engine.
    So: token confidence alone does NOT separate invented lettering from OCR
    noise on this material, and this check therefore reports and never judges.
    Exit code is unaffected by anything it finds. What it buys the operator is a
    pointer -- "there is something letter-shaped at these coordinates" -- on a
    defect that is otherwise only visible at full zoom."""
    res = {"check": "text", "status": "OK", "engine": None, "tokens": [],
           "n_tokens": 0, "max_conf": None, "agreeing": [], "note": ""}
    if not enabled:
        res["status"] = "SKIPPED"
        res["note"] = "OCR disabled by --no-ocr: this render was NOT checked for text"
        return res

    exe = resolve_tesseract(tesseract)          # raises QCError -> caller exits 2
    try:
        import pytesseract
    except ImportError as exc:
        raise QCError("pytesseract is not installed in this interpreter (%s). "
                      "See this module's docstring for the exact install." % sys.executable) from exc
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        ver = str(pytesseract.get_tesseract_version())
    except Exception as exc:                    # noqa: BLE001
        raise QCError("tesseract at %s would not report a version: %s: %s"
                      % (exe, type(exc).__name__, exc)) from exc
    res["engine"] = "tesseract %s via pytesseract (%s)" % (ver, exe)

    img = Image.fromarray(a)
    seen = {}
    for psm in OCR_PSM_MODES:
        try:
            d = pytesseract.image_to_data(img, config="--psm %d" % psm,
                                          output_type=pytesseract.Output.DICT)
        except Exception as exc:                # noqa: BLE001
            raise QCError("tesseract failed on psm %d: %s: %s"
                          % (psm, type(exc).__name__, exc)) from exc
        for i in range(len(d["text"])):
            raw = (d["text"][i] or "").strip()
            conf = float(d["conf"][i])
            if not raw or conf < 0:
                continue
            core = "".join(ch for ch in raw if ch.isalnum()).upper()
            if len(core) < OCR_MIN_CORE_CHARS:
                continue
            box = [int(d["left"][i]), int(d["top"][i]), int(d["width"][i]), int(d["height"][i])]
            prev = seen.get(core)
            if prev is None:
                seen[core] = {"text": core, "conf": round(conf, 1), "psm": [psm], "box": box}
            else:
                prev["conf"] = round(max(prev["conf"], conf), 1)
                if psm not in prev["psm"]:
                    prev["psm"].append(psm)
    toks = sorted(seen.values(), key=lambda t: -t["conf"])
    res["tokens"] = toks
    res["n_tokens"] = len(toks)
    res["max_conf"] = toks[0]["conf"] if toks else None
    res["agreeing"] = [t["text"] for t in toks if len(t["psm"]) > 1]
    return res


# -------------------------------------------------------------- 3. palette
# THE DESIGN LANGUAGE STATES COLOUR IN WORDS, NOT IN HEX. plan/THE-SHOW-PROGRAM.md
# line 85: "Limited saturated palette: warm orange, dusty purple, muted denim
# blue, teal accents"; plus "bold clean outlines in dark warm brown rather than
# pure black" and "plain white background for character sheets". There is no hex
# anywhere in THE-SHOW-DESIGN-LANGUAGE.md or the ShotGrid Asset fragments -- I
# looked, and its absence is the honest limit of this check.
#
# So the words are translated here into HSV hue windows, and THAT TRANSLATION IS
# THE ONLY SUBJECTIVE THING IN THIS FILE. It is deliberately in one table so it
# is the first and easiest thing to tune, and so a disagreement about the number
# is a disagreement about six rows rather than about the code. Replace it the
# moment the design language gains real swatches.
#
# UNHUED PIXELS ARE IN-ENVELOPE, and there are two kinds. Both exclusions were
# forced by measuring the first version of this check against the real cells,
# and without them the number is not merely noisy, it is dominated by noise:
#   - low saturation (s < NEUTRAL_SAT): the register's "plain white background
#     for character sheets" and "big white sclera". Counting the ground as a
#     palette error makes every sheet read as a failure.
#   - low value (v < DARK_V): the register's "bold clean outlines in dark warm
#     brown" and its cel shadows. HUE IS NOT MEANINGFUL AT NEAR-BLACK -- a
#     one-count channel difference swings it by a hundred degrees, and (72,48,48)
#     computes as saturation 0.33 at hue 0, so it escapes the saturation floor
#     and lands in "crimson". Measured on P3_13: before this floor existed the
#     check reported 22.4% off-palette, of which 12.8 percentage points were the
#     outline colour alone -- the real off-palette fills were buried underneath.
# So the envelope is judged on the COLOURED pixels, which is what the register's
# clause is about ("limited SATURATED palette"), and off_palette_pct_of_coloured
# is the number to read.
# hue_lo > hue_hi means the band WRAPS through 0. dark_warm_brown is the one
# that does, and deliberately: measured on the real cells, Anima's outline is a
# dark plum-maroon around hue 340 as often as a brown around hue 20 (pass 3's
# own words for P3_18: "a thin dark MAROON of uniform weight rather than a bold
# warm-brown"). At v < 0.45 this tool cannot tell maroon-brown from brown-brown
# with any authority, and pretending it can put 23 percentage points of pure
# outline into L12's "off-palette" number and buried the fills that actually
# matter. The outline's exact hue is a real register question -- it is just not
# one arithmetic on a 24-bit render can settle, so it is excluded rather than
# guessed at. Every band's saturation floor equals NEUTRAL_SAT on purpose: a
# higher floor leaves a crack between "too grey to have a hue" and "saturated
# enough to be judged", and pale skin fell straight into it.
PALETTE_BANDS = [
    # name,               hue_lo, hue_hi, min_sat, min_val, max_val
    ("warm_orange",         12.0,   48.0,   0.15,   0.15,   1.01),
    ("dark_warm_brown",    315.0,   60.0,   0.15,   0.02,   0.45),
    ("teal",               155.0,  200.0,   0.15,   0.05,   1.01),
    ("denim_blue",         200.0,  255.0,   0.15,   0.05,   1.01),
    ("dusty_purple",       255.0,  310.0,   0.15,   0.05,   1.01),
]
NEUTRAL_SAT = 0.15
DARK_V = 0.20


def rgb_to_hsv_arrays(a):
    f = a.astype(np.float32) / 255.0
    mx = f.max(axis=2)
    mn = f.min(axis=2)
    diff = mx - mn
    h = np.zeros_like(mx)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    nz = diff > 1e-6
    idx = nz & (mx == r)
    h[idx] = (60.0 * ((g[idx] - b[idx]) / diff[idx])) % 360.0
    idx = nz & (mx == g)
    h[idx] = 60.0 * ((b[idx] - r[idx]) / diff[idx]) + 120.0
    idx = nz & (mx == b)
    h[idx] = 60.0 * ((r[idx] - g[idx]) / diff[idx]) + 240.0
    s = np.zeros_like(mx)
    s[mx > 1e-6] = diff[mx > 1e-6] / mx[mx > 1e-6]
    return h % 360.0, s, mx


# Names for what falls OUTSIDE the envelope, so "12% off-palette" is actionable
# rather than merely alarming -- pass 3's two named offenders are the mustard
# yellow and the crimson, and the operator should be able to see which it is.
OFF_BANDS = [("yellow_green", 48.0, 100.0), ("green", 100.0, 155.0),
             ("cyan_gap", 200.0, 200.0), ("magenta_pink", 310.0, 345.0),
             ("red_crimson_a", 345.0, 360.0), ("red_crimson_b", 0.0, 12.0)]


def check_palette(a):
    h, s, v = rgb_to_hsv_arrays(a)
    total = h.size
    unhued = (s < NEUTRAL_SAT) | (v < DARK_V)
    coloured = ~unhued
    coloured_n = int(coloured.sum())
    inb = unhued.copy()
    shares = {"unhued_ground_and_outline": round(100.0 * unhued.sum() / total, 2)}
    for name, lo, hi, msat, mval, xval in PALETTE_BANDS:
        hue_in = ((h >= lo) & (h < hi)) if lo < hi else ((h >= lo) | (h < hi))
        band = hue_in & (s >= msat) & (v >= mval) & (v < xval) & coloured
        shares[name] = round(100.0 * band.sum() / total, 2)
        inb |= band
    off = ~inb
    off_n = int(off.sum())
    breakdown = {}
    for name, lo, hi in OFF_BANDS:
        if hi <= lo:
            continue
        sel = off & (h >= lo) & (h < hi)
        pct = round(100.0 * sel.sum() / total, 2)
        if pct > 0.0:
            key = "red_crimson" if name.startswith("red_crimson") else name
            breakdown[key] = round(breakdown.get(key, 0.0) + pct, 2)
    return {"check": "palette", "status": "OK",
            "off_palette_pct": round(100.0 * off_n / total, 2),
            "off_palette_pct_of_coloured": (round(100.0 * off_n / coloured_n, 2)
                                            if coloured_n else 0.0),
            "coloured_pct": round(100.0 * coloured_n / total, 2),
            "in_band_pct": shares, "off_breakdown": breakdown,
            "bands": [b[0] for b in PALETTE_BANDS],
            "neutral_sat_floor": NEUTRAL_SAT, "dark_value_floor": DARK_V}


# ------------------------------------------------------------- 4. degenerate
# The only hard FAILs in this module. Chosen to be unarguable: an image that
# trips one of these is not a render an operator could review even in principle.
MIN_LUMA_SD = 2.0          # a near-uniform image
MAX_FLAT_TILE_FRAC = 0.98  # essentially the whole frame is featureless
TILE = 32


def check_degenerate(a):
    lum = (0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]).astype(np.float32)
    sd = float(lum.std())
    h, w = lum.shape
    th, tw = h // TILE, w // TILE
    if th and tw:
        tiles = lum[:th * TILE, :tw * TILE].reshape(th, TILE, tw, TILE).transpose(0, 2, 1, 3)
        tsd = tiles.reshape(th * tw, -1).std(axis=1)
        flat = float((tsd < 1.0).mean())
    else:
        flat = 1.0 if sd < MIN_LUMA_SD else 0.0
    uniq = int(len(np.unique(a.reshape(-1, 3)[::7], axis=0)))
    res = {"check": "degenerate", "status": "OK", "luma_sd": round(sd, 2),
           "flat_tile_frac": round(flat, 4), "sampled_unique_colours": uniq,
           "reasons": []}
    if sd < MIN_LUMA_SD:
        res["reasons"].append("luma standard deviation %.2f < %.1f -- the frame is "
                              "effectively uniform" % (sd, MIN_LUMA_SD))
    if flat > MAX_FLAT_TILE_FRAC:
        res["reasons"].append("%.1f%% of %dpx tiles are featureless (> %.0f%%)"
                              % (100 * flat, TILE, 100 * MAX_FLAT_TILE_FRAC))
    if res["reasons"]:
        res["status"] = "FAIL"
    return res


def check_resolution(meta, expect_size):
    res = {"check": "resolution", "status": "OK",
           "width": meta["width"], "height": meta["height"],
           "expected": None, "reasons": []}
    if not expect_size:
        res["note"] = "no --expect-size given, nothing to compare against"
        return res
    ew, eh = expect_size
    res["expected"] = [ew, eh]
    if (meta["width"], meta["height"]) != (ew, eh):
        res["status"] = "FAIL"
        res["reasons"].append("rendered %dx%d, expected %dx%d"
                              % (meta["width"], meta["height"], ew, eh))
    return res


# ------------------------------------------------------------------------ run
def run_qc(path, expect_views=None, expect_size=None, ocr=True, tesseract=None):
    """-> report dict. Never raises for an image-level problem: a QCError from
    any check is captured into that check's own entry with status "ERROR", so
    the report is always complete and the caller can see WHICH check died."""
    report = {"image": path, "tool": "render_qc", "checks": {},
              "overall": "OK", "mechanical_fail": False, "check_error": False}
    try:
        a, meta = load_image(path)
    except QCError as exc:
        report["checks"]["file"] = {"check": "file", "status": "FAIL",
                                    "reasons": [str(exc)]}
        report["overall"] = "FAIL"
        report["mechanical_fail"] = True
        return report
    report["checks"]["file"] = {"check": "file", "status": "OK", **meta}

    for name, fn in (("resolution", lambda: check_resolution(meta, expect_size)),
                     ("degenerate", lambda: check_degenerate(a)),
                     ("views", lambda: check_views(a, expect_views)),
                     ("palette", lambda: check_palette(a)),
                     ("text", lambda: check_text(a, tesseract=tesseract, enabled=ocr))):
        try:
            report["checks"][name] = fn()
        except QCError as exc:
            report["checks"][name] = {"check": name, "status": "ERROR", "reasons": [str(exc)]}
        except Exception as exc:                # noqa: BLE001
            report["checks"][name] = {"check": name, "status": "ERROR",
                                      "reasons": ["%s: %s" % (type(exc).__name__, exc)]}

    for c in report["checks"].values():
        if c.get("status") == "FAIL":
            report["mechanical_fail"] = True
        if c.get("status") == "ERROR":
            report["check_error"] = True
    report["overall"] = ("ERROR" if report["check_error"]
                         else "FAIL" if report["mechanical_fail"] else "OK")
    return report


def exit_code(report):
    if report.get("check_error"):
        return EXIT_ERROR
    if report.get("mechanical_fail"):
        return EXIT_FAIL
    return EXIT_OK


# ------------------------------------------------------------------- reporting
def description_block(report, max_tokens=6):
    """The compact block that goes on the published Version's description, via
    sg_publish.publish_version(description=...). Written to be judgeable in ten
    seconds (Geoff: "I don't want a text report, I want an easy overview"), and
    to state facts rather than verdicts on everything except the mechanical
    checks. Kept short deliberately: it shares a field with the publisher's own
    prose."""
    c = report["checks"]
    lines = ["[render_qc %s] deterministic checks, no vision model." % report["overall"]]

    f = c.get("file", {})
    if f.get("status") == "FAIL":
        lines.append("file: FAIL -- %s" % "; ".join(f.get("reasons", [])))
        return "\n".join(lines)

    d, r = c.get("degenerate", {}), c.get("resolution", {})
    px = "pixels: %dx%d" % (f.get("width", 0), f.get("height", 0))
    if r.get("expected"):
        px += " (expected %dx%d%s)" % (r["expected"][0], r["expected"][1],
                                       "" if r.get("status") == "OK" else " -- MISMATCH")
    if d.get("status") in ("OK", "FAIL"):
        px += ", luma sd %.1f, %.0f%% flat tiles" % (d.get("luma_sd", 0),
                                                     100 * d.get("flat_tile_frac", 0))
    if d.get("status") == "FAIL":
        px += " -- DEGENERATE: %s" % "; ".join(d.get("reasons", []))
    lines.append(px)

    v = c.get("views", {})
    if v.get("status") == "ERROR":
        lines.append("views: CHECK COULD NOT RUN -- %s" % "; ".join(v.get("reasons", [])))
    elif v.get("panels_found", 0) < 2:
        lines.append("views: %d panel(s) segmented (%s) -- %s"
                     % (v.get("panels_found", 0), v.get("method"), v.get("note", "")))
    else:
        near = v.get("near_duplicate_pairs") or []
        head = ("views: %d panels (%s split), closest pair pHash %d/64"
                % (v["panels_found"], v["method"], v["min_phash"]))
        if near:
            head += "; NEAR-DUPLICATE (<=%d): " % NEAR_DUPLICATE_PHASH + ", ".join(
                "%d-%d pHash %d mad %.1f" % (p["a"], p["b"], p["phash"], p["mad"])
                for p in near)
        else:
            head += "; no pair within %d -- views read as distinct" % NEAR_DUPLICATE_PHASH
        lines.append(head)
        mirr = v.get("mirrored_repeat_pairs") or []
        if mirr:
            lines.append("       possible MIRRORED repeat (weaker signal -- a genuine back "
                         "view of a symmetric figure also scores here): "
                         + ", ".join("%d-%d mirrored pHash %d vs %d plain"
                                     % (p["a"], p["b"], p["phash_mirrored"], p["phash"])
                                     for p in mirr))
        if v.get("note"):
            lines.append("       %s" % v["note"])

    t = c.get("text", {})
    if t.get("status") == "ERROR":
        lines.append("text: OCR COULD NOT RUN -- %s" % "; ".join(t.get("reasons", [])))
    elif t.get("status") == "SKIPPED":
        lines.append("text: NOT CHECKED (%s)" % t.get("note", "disabled"))
    elif not t.get("tokens"):
        lines.append("text: OCR found no token of >=%d characters" % OCR_MIN_CORE_CHARS)
    else:
        shown = ", ".join("%s(%.0f)" % (x["text"], x["conf"]) for x in t["tokens"][:max_tokens])
        lines.append("text: %d OCR token(s), max conf %.0f: %s%s  [reported, not judged -- "
                     "line art reads as glyphs; see render_qc.check_text]"
                     % (t["n_tokens"], t["max_conf"], shown,
                        " ..." if t["n_tokens"] > max_tokens else ""))

    p = c.get("palette", {})
    if p.get("status") == "ERROR":
        lines.append("palette: CHECK COULD NOT RUN -- %s" % "; ".join(p.get("reasons", [])))
    else:
        bd = ", ".join("%s %.1f%%" % (k, val) for k, val in
                       sorted(p.get("off_breakdown", {}).items(), key=lambda kv: -kv[1]))
        lines.append("palette: %.1f%% of the COLOURED pixels are outside the stated envelope "
                     "(%.1f%% of the whole frame; %.0f%% of the frame is ground/outline and "
                     "carries no hue)%s"
                     % (p.get("off_palette_pct_of_coloured", 0.0), p.get("off_palette_pct", 0.0),
                        100.0 - p.get("coloured_pct", 0.0), (" [%s]" % bd) if bd else ""))
    return "\n".join(lines)


def append_qc_to_description(description, report):
    """The wiring helper publishers call. sg_publish.publish_version() is NOT
    modified and NOT imported here: it takes `description` as a plain string and
    that is exactly the seam this uses, so the publish contract keeps working
    for the seven tools that already import it and gains no dependency on numpy
    or pytesseract. Order matters -- the publisher's own prose stays first, so
    the ShotGrid list view still shows what the Version IS before what QC made
    of it."""
    block = description_block(report)
    return ("%s\n\n%s" % (description.rstrip(), block)) if description else block


def format_report(report):
    out = ["%s -> %s" % (report["image"], report["overall"])]
    for name, c in report["checks"].items():
        out.append("  %-11s %s" % (name, c.get("status")))
        for k, val in c.items():
            if k in ("check", "status"):
                continue
            if k == "pairs":
                for p in val:
                    out.append("      pair %d-%d  mad=%6.2f pHash=%2d | mirrored mad=%6.2f "
                               "pHash=%2d%s" % (p["a"], p["b"], p["mad"], p["phash"],
                                                p["mad_mirrored"], p["phash_mirrored"],
                                                "  <- MIRRORED CLOSER" if p["mirrored_is_closer"] else ""))
                continue
            if k == "tokens":
                for t in val[:12]:
                    out.append("      token %-12s conf=%5.1f psm=%s box=%s"
                               % (t["text"], t["conf"], t["psm"], t["box"]))
                continue
            if k in ("near_duplicate_pairs", "mirrored_repeat_pairs", "panel_bounds"):
                continue
            out.append("      %-22s %s" % (k, val))
    return "\n".join(out)


# ------------------------------------------------------------------ self-test
def _fixture_panels(n, identical, size=(1280, 704), seed=7):
    """Synthesise a contact sheet: n figure blobs on a white ground, separated by
    clear gaps. `identical` draws the SAME figure n times (the duplicate-view
    canary); otherwise each figure is genuinely different in shape and colour.
    Synthesised rather than taken from a real render so the test does not depend
    on any particular file surviving on disk."""
    from PIL import ImageDraw
    w, h = size
    im = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(seed)
    slot = w // n
    # Genuinely different SILHOUETTES, not just different colours: a perceptual
    # hash is computed on luminance structure, so five recoloured ellipses would
    # be five near-identical hashes and the "distinct panels do not fire" test
    # would be measuring nothing.
    forms = ["tall_oval", "wide_box", "triangle", "stack", "ring"]
    for i in range(n):
        form = forms[0] if identical else forms[i % len(forms)]
        col = (232, 120, 40) if identical else [(232, 120, 40), (70, 105, 165),
                                                (120, 70, 150), (40, 150, 150),
                                                (200, 90, 60)][i % 5]
        cx, cy = slot * i + slot // 2, h // 2
        ol = (70, 45, 30)
        if form == "tall_oval":
            d.ellipse([cx - 50, cy - 230, cx + 50, cy + 230], fill=col, outline=ol, width=6)
            d.ellipse([cx - 30, cy - 210, cx + 30, cy - 130], fill=(255, 255, 255),
                      outline=ol, width=4)
        elif form == "wide_box":
            d.rectangle([cx - 150, cy - 90, cx + 150, cy + 90], fill=col, outline=ol, width=6)
            d.line([cx - 150, cy, cx + 150, cy], fill=ol, width=8)
        elif form == "triangle":
            d.polygon([(cx, cy - 240), (cx - 160, cy + 220), (cx + 160, cy + 220)],
                      fill=col, outline=ol)
            d.ellipse([cx - 40, cy + 60, cx + 40, cy + 140], fill=(255, 255, 255), outline=ol, width=4)
        elif form == "stack":
            for k, yy in enumerate(range(cy - 220, cy + 200, 110)):
                d.rectangle([cx - 60 - 20 * k, yy, cx + 60 + 20 * k, yy + 80],
                            fill=col, outline=ol, width=5)
        else:                                   # ring
            d.ellipse([cx - 170, cy - 170, cx + 170, cy + 170], fill=col, outline=ol, width=8)
            d.ellipse([cx - 80, cy - 80, cx + 80, cy + 80], fill=(255, 255, 255), outline=ol, width=8)
        # An OFF-CENTRE mark on every figure, so each panel is chiral. Without
        # it every form here is left-right symmetric, a horizontal flip is a
        # no-op, and the mirrored-duplicate canary would be measuring nothing.
        d.rectangle([cx + 60, cy - 250, cx + 130, cy - 200], fill=(30, 30, 30), outline=ol, width=3)
        if not identical:
            d.rectangle([cx - 40, cy + 240, cx + 40, cy + 280],
                        fill=tuple(int(x) for x in rng.integers(60, 200, 3)),
                        outline=ol, width=4)
    return np.array(im, dtype=np.uint8)


def _fixture_text(word="23WAR", size=(640, 320)):
    from PIL import ImageDraw, ImageFont
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    font = None
    for loader in (lambda: ImageFont.truetype("arial.ttf", 96),
                   lambda: ImageFont.truetype("DejaVuSans.ttf", 96),
                   lambda: ImageFont.load_default(size=96)):
        try:
            font = loader()
            break
        except Exception:                       # noqa: BLE001
            continue
    d.text((40, 100), word, fill=(20, 20, 20), font=font)
    return np.asarray(im, dtype=np.uint8)


def _fixture_flat(colour, size=(320, 320)):
    return np.tile(np.asarray(colour, dtype=np.uint8), (size[1], size[0], 1))


def _fixture_offpalette(size=(320, 320)):
    """Half mustard yellow (hue ~52), half crimson (hue ~350) -- pass 3's two
    named offenders, neither of which is in the register."""
    a = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    a[:, :size[0] // 2] = (215, 190, 40)
    a[:, size[0] // 2:] = (190, 30, 60)
    return a


def _fixture_inpalette(size=(320, 320)):
    """Four register colours in quarters: warm orange, denim blue, dusty purple,
    teal -- plus the dark warm brown outline colour as a border."""
    a = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    q = size[0] // 4
    a[:, 0 * q:1 * q] = (232, 120, 40)
    a[:, 1 * q:2 * q] = (70, 105, 165)
    a[:, 2 * q:3 * q] = (120, 70, 150)
    a[:, 3 * q:] = (40, 150, 150)
    a[:8] = (70, 45, 30)
    return a


def self_test():
    import tempfile
    fails = []

    def ck(name, cond):
        print("  %-74s %s" % (name[:74], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="renderqc_")

    def write(a, name):
        p = os.path.join(tmp, name)
        Image.fromarray(a).save(p)
        return p

    # ---------------------------------------------------------- primitives
    a = _fixture_panels(3, identical=False)
    ck("background_colour finds the white ground of a synthesised sheet",
       tuple(int(v) for v in background_colour(a)) == (255, 255, 255))
    ck("ink_mask marks the figures and not the ground",
       0.01 < float(ink_mask(a).mean()) < 0.6)
    ck("phash returns 64 bits", phash(_fixture_flat((200, 100, 50))).size == 64)
    ck("phash of an image against itself is distance 0",
       hamming(phash(a[:, :400]), phash(a[:, :400])) == 0)

    # ------------------------------------- CANARY 1: three identical panels
    # The known seed failure, per ANIMA-PASS3.md gap 5: a turnaround that is
    # actually three copies of the front. A check that cannot see this is
    # decoration (invariant 2).
    dup = _fixture_panels(3, identical=True)
    rdup = check_views(dup, expect_views=3)
    ck("CANARY: three identical panels -> 3 panels segmented",
       rdup["panels_found"] == 3)
    ck("CANARY: three identical panels -> every pair flagged near-duplicate",
       len(rdup["near_duplicate_pairs"]) == 3)
    ck("CANARY: three identical panels -> pHash distance 0 on every pair",
       all(p["best_phash"] == 0 for p in rdup["pairs"]))

    # ---------------------------- the mirror of it: distinct panels must NOT fire
    dis = _fixture_panels(3, identical=False)
    rdis = check_views(dis, expect_views=3)
    ck("three genuinely different panels -> 3 segmented, none flagged "
       "(a check that always fires is as useless as one that never does)",
       rdis["panels_found"] == 3 and not rdis["near_duplicate_pairs"])
    ck("three different panels -> min pHash comfortably above the flag threshold",
       rdis["min_phash"] > NEAR_DUPLICATE_PHASH)

    # ----------------------------- CANARY 1b: a MIRRORED repeat (P3_10's defect)
    mir = dis.copy()
    slot = mir.shape[1] // 3
    mir[:, 2 * slot:3 * slot] = mir[:, 0:slot][:, ::-1]
    rmir = check_views(mir, expect_views=3)
    flagged_mirror = rmir["mirrored_repeat_pairs"]
    ck("CANARY: a panel replaced by an exact MIRRORED copy of another is caught "
       "by the mirror comparison", bool(flagged_mirror))
    ck("...and reported under mirrored_repeat_pairs, NOT merged into the primary "
       "near-duplicate flag -- the two signals are not equally trustworthy on "
       "real renders and are kept apart",
       bool(flagged_mirror) and flagged_mirror[0]["phash"] > NEAR_DUPLICATE_PHASH
       and not rmir["near_duplicate_pairs"])
    ck("the mirrored flag shows up in the description block, labelled as the "
       "weaker signal", "MIRRORED repeat" in description_block(
           {"overall": "OK", "checks": {"file": {"status": "OK", "width": 1, "height": 1},
                                        "views": rmir}}))

    # --------------------------------- segmentation fallback when views touch
    touch = _fixture_panels(3, identical=False)
    touch = np.concatenate([touch[:, 60:460], touch[:, 460:860], touch[:, 860:1260]], axis=1)
    bounds_gap, method_gap = segment_panels(touch, expect_views=None)
    bounds_n, method_n = segment_panels(touch, expect_views=3)
    ck("segment_panels honours an explicit view count even when gap splitting "
       "disagrees, and says which method it used",
       len(bounds_n) == 3 and method_n in ("gap", "equal"))
    ck("segment_panels with no expectation reports whatever it found, without "
       "inventing a count", isinstance(bounds_gap, list) and method_gap in ("gap", "none (no ink)"))

    # ------------------------------------------ CANARY 2: rendered text + OCR
    txt = _fixture_text("23WAR")
    try:
        rtxt = check_text(txt)
        ocr_ran = rtxt["status"] == "OK"
    except QCError as exc:
        rtxt, ocr_ran = {"tokens": [], "note": str(exc)}, False
    ck("OCR engine resolved and ran (if this fails, the text check is a "
       "BLOCKER, not a silent pass -- see the docstring's install steps)", ocr_ran)
    if ocr_ran:
        found = " ".join(t["text"] for t in rtxt["tokens"])
        ck("CANARY: an image with '23WAR' rendered on it produces OCR tokens",
           rtxt["n_tokens"] > 0)
        ck("CANARY: the detected text actually contains the rendered word "
           "(found: %r)" % found[:60], "WAR" in found or "23WAR" in found)
        blank_txt = check_text(_fixture_flat((255, 255, 255)))
        ck("a blank white image produces no OCR tokens -- the canary above is "
           "not just OCR firing on anything", blank_txt["n_tokens"] == 0)

    # --------------------------------------- OCR unavailability is LOUD (inv. 3)
    saved = list(KNOWN_TESSERACT_PATHS)
    saved_env = os.environ.pop("TESSERACT_EXE", None)
    try:
        globals()["KNOWN_TESSERACT_PATHS"] = [r"C:\nope\not_a_real_tesseract.exe"]
        import shutil as _sh
        real_which = _sh.which
        _sh.which = lambda n: None
        try:
            resolve_tesseract()
            ck("CANARY: an unresolvable tesseract raises QCError rather than "
               "returning a check that reports 'no text found'", False)
        except QCError as exc:
            ck("CANARY: an unresolvable tesseract raises QCError rather than "
               "returning a check that reports 'no text found'",
               "could not resolve" in str(exc))
        r_err = run_qc(write(dis, "ocrfail.png"), expect_views=3)
        ck("CANARY: with OCR unresolvable, run_qc's text check is ERROR and the "
           "run exits %d -- never 0 with the check silently missing" % EXIT_ERROR,
           r_err["checks"]["text"]["status"] == "ERROR" and exit_code(r_err) == EXIT_ERROR)
        ck("the OTHER checks still ran and reported while OCR was broken",
           r_err["checks"]["views"]["status"] == "OK"
           and r_err["checks"]["palette"]["status"] == "OK")
        ck("description_block names the OCR failure instead of omitting the line",
           "OCR COULD NOT RUN" in description_block(r_err))
        _sh.which = real_which
    finally:
        globals()["KNOWN_TESSERACT_PATHS"] = saved
        if saved_env is not None:
            os.environ["TESSERACT_EXE"] = saved_env

    # ------------------------------------------- CANARY 3: off-palette image
    roff = check_palette(_fixture_offpalette())
    ck("CANARY: an all mustard-yellow / crimson image is ~100%% off-palette "
       "(%.1f%%)" % roff["off_palette_pct"], roff["off_palette_pct"] > 95.0)
    ck("CANARY: the off-palette breakdown NAMES yellow and red, not just a total",
       "yellow_green" in roff["off_breakdown"] and "red_crimson" in roff["off_breakdown"])
    rin = check_palette(_fixture_inpalette())
    ck("the four register colours + brown outline measure ~0%% off-palette "
       "(%.1f%%) -- the canary above is a real discriminator, not a check that "
       "always fires" % rin["off_palette_pct"], rin["off_palette_pct"] < 2.0)
    rwhite = check_palette(_fixture_flat((255, 255, 255)))
    ck("a plain white ground is IN envelope (0%% off) -- neutrals must not be "
       "counted as palette errors or every sheet reads as a failure",
       rwhite["off_palette_pct"] < 0.01)
    # The dark-value exclusion is the one leniency in this check, so it gets a
    # canary on BOTH sides: the outline it exists for must pass, and a bright
    # crimson at the same hue must still fail. Otherwise it is a hole, not a floor.
    rdark = check_palette(_fixture_flat((80, 50, 60)))       # dark plum-maroon outline
    ck("a dark plum-maroon OUTLINE colour is in envelope (hue is not meaningful "
       "at v=%.2f) -- %.1f%% off" % (80 / 255.0, rdark["off_palette_pct"]),
       rdark["off_palette_pct"] < 1.0)
    rbright = check_palette(_fixture_flat((225, 60, 95)))    # same hue, bright
    ck("CANARY: the SAME hue at full brightness is still off-palette (%.1f%%) -- "
       "the dark-value floor is a floor, not a way for crimson to get in"
       % rbright["off_palette_pct"], rbright["off_palette_pct"] > 95.0)
    ck("check_palette reports the coloured-pixel denominator as well as the "
       "whole-frame one",
       "off_palette_pct_of_coloured" in rin and "coloured_pct" in rin)

    # -------------------------------------------- CANARY 4: degenerate output
    rblank = check_degenerate(_fixture_flat((255, 255, 255)))
    ck("CANARY: a blank image FAILs the degenerate check", rblank["status"] == "FAIL")
    ck("CANARY: the blank failure names the measurement that produced it",
       any("standard deviation" in r for r in rblank["reasons"]))
    rreal = check_degenerate(dis)
    ck("a real drawing PASSes the degenerate check", rreal["status"] == "OK")

    # ------------------------------------------ CANARY 4b: truncated / unreadable
    good = write(dis, "good.png")
    trunc = os.path.join(tmp, "trunc.png")
    raw = open(good, "rb").read()
    open(trunc, "wb").write(raw[:len(raw) // 3])
    try:
        load_image(trunc)
        ck("CANARY: a truncated PNG raises QCError from load_image", False)
    except QCError:
        ck("CANARY: a truncated PNG raises QCError from load_image", True)
    rtr = run_qc(trunc, ocr=False)
    ck("CANARY: run_qc on a truncated file is FAIL and exits %d" % EXIT_FAIL,
       rtr["overall"] == "FAIL" and exit_code(rtr) == EXIT_FAIL)
    empty = os.path.join(tmp, "empty.png")
    open(empty, "wb").close()
    ck("CANARY: a zero-byte file is FAIL, not an exception and not a pass",
       exit_code(run_qc(empty, ocr=False)) == EXIT_FAIL)
    ck("CANARY: a missing file is FAIL, not a pass",
       exit_code(run_qc(os.path.join(tmp, "nope.png"), ocr=False)) == EXIT_FAIL)

    # ------------------------------------------- CANARY 4c: wrong resolution
    rres = run_qc(good, expect_size=(999, 111), ocr=False)
    ck("CANARY: a resolution mismatch is a mechanical FAIL, exit %d" % EXIT_FAIL,
       rres["checks"]["resolution"]["status"] == "FAIL" and exit_code(rres) == EXIT_FAIL)
    rres_ok = run_qc(good, expect_size=(dis.shape[1], dis.shape[0]), ocr=False)
    ck("the matching resolution passes -- the canary above is a discriminator",
       rres_ok["checks"]["resolution"]["status"] == "OK")

    # ------------------------------ THE CENTRAL CONTRACT: aesthetics never FAIL
    # This is the invariant Geoff's decision actually rests on: the numbers are
    # for the operator, and no amount of ugly changes the exit code.
    ugly = _fixture_panels(3, identical=True).copy()
    ugly[:, :, 1] = np.clip(ugly[:, :, 1].astype(np.int16) - 60, 0, 255).astype(np.uint8)
    p_ugly = write(ugly, "ugly.png")
    rugly = run_qc(p_ugly, expect_views=3, ocr=False)
    ck("a sheet with THREE IDENTICAL PANELS and an off-palette shift still "
       "exits 0 -- duplicate views and colour are reported, never gated "
       "(Geoff: 'just publish them all and let the operator review')",
       exit_code(rugly) == EXIT_OK)
    ck("...and the near-duplicate finding is nonetheless present in the report",
       len(rugly["checks"]["views"]["near_duplicate_pairs"]) == 3)
    ck("...and visible in the one-screen description block",
       "NEAR-DUPLICATE" in description_block(rugly))

    # --------------------------------------------------- description / wiring
    blk = description_block(rugly)
    ck("description_block reports pixels, views and palette on separate lines",
       "pixels:" in blk and "views:" in blk and "palette:" in blk)
    ck("description_block leads with the overall status so it reads in a glance",
       blk.startswith("[render_qc "))
    joined = append_qc_to_description("Character sheet, 3/6 views.", rugly)
    ck("append_qc_to_description keeps the publisher's own prose FIRST",
       joined.startswith("Character sheet, 3/6 views.") and "[render_qc" in joined)
    ck("append_qc_to_description handles an empty base description",
       append_qc_to_description("", rugly).startswith("[render_qc"))
    # ------------------------------------------------------- no model, no net
    # The load-bearing claim of this whole module is "no model of any kind, no
    # network, no `claude` subprocess", and that claim is worth a test rather
    # than a promise in a docstring. Checked over the module's actual import
    # graph via ast, not by grepping its own prose -- the docstring necessarily
    # NAMES the things it forbids, so a text search would fail on itself.
    import ast
    tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())
    def _names(nodes):
        got = set()
        for node in nodes:
            if isinstance(node, ast.Import):
                got.update(al.name.split(".")[0] for al in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                got.add(node.module.split(".")[0])
        return got

    imported = _names(ast.walk(tree))
    toplevel = _names(tree.body)
    banned = {"requests", "urllib", "http", "socket", "shotgun_api3", "torch",
              "transformers", "anthropic", "openai", "easyocr", "cv2"}
    ck("module imports nothing from %s -- no model, no network, no ShotGrid "
       "(imports: %s)" % (", ".join(sorted(banned)), ", ".join(sorted(imported))),
       not (imported & banned))
    ck("module never spawns a `claude` subprocess (no subprocess import at all; "
       "tesseract is reached through pytesseract)", "subprocess" not in imported)
    ck("module does NOT import sg_publish -- the wiring goes the other way, so "
       "the publish contract that seven tools depend on gains no numpy or "
       "pytesseract dependency", "sg_publish" not in imported)
    ck("pytesseract is imported lazily, inside check_text -- so a box without "
       "OCR can still run every other check (top-level imports: %s)"
       % ", ".join(sorted(toplevel)), "pytesseract" not in toplevel)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


# ------------------------------------------------------------------------ CLI
def parse_size(s):
    if not s:
        return None
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except Exception:                           # noqa: BLE001
        raise argparse.ArgumentTypeError("--expect-size wants WxH, e.g. 1280x704")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", help="path to the render to check")
    ap.add_argument("--glob", help="check every file matching this pattern")
    ap.add_argument("--views", type=int, help="how many view panels the sheet should "
                    "have. Given, it forces that split when gap segmentation "
                    "disagrees (views touching); omitted, the found count is "
                    "reported as-is.")
    ap.add_argument("--expect-size", type=parse_size, help="WxH; a mismatch is a "
                    "mechanical FAIL")
    ap.add_argument("--tesseract", help="explicit path to tesseract(.exe)")
    ap.add_argument("--no-ocr", action="store_true", help="skip the text check. "
                    "Recorded as NOT CHECKED in the report and in the Version "
                    "description -- never as 'no text found'.")
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--tsv", action="store_true", help="one line per image: the "
                    "headline numbers, for sweeping a directory")
    ap.add_argument("--description", action="store_true",
                    help="print only the block that goes on the ShotGrid Version")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test()
    paths = []
    if ns.image:
        paths.append(ns.image)
    if ns.glob:
        paths.extend(sorted(globmod.glob(ns.glob)))
    if not paths:
        ap.error("--image or --glob is required (or use --self-test)")

    worst = EXIT_OK
    reports = []
    if ns.tsv:
        print("\t".join(["image", "overall", "panels", "method", "min_phash",
                         "near_dup_pairs", "off_pal_pct_coloured", "off_pal_pct_frame",
                         "ocr_tokens", "ocr_max_conf", "luma_sd"]))
    for p in paths:
        rep = run_qc(p, expect_views=ns.views, expect_size=ns.expect_size,
                     ocr=not ns.no_ocr, tesseract=ns.tesseract)
        reports.append(rep)
        worst = max(worst, exit_code(rep))
        if ns.tsv:
            v, t, pal, d = (rep["checks"].get(k, {}) for k in ("views", "text", "palette", "degenerate"))
            print("\t".join(str(x) for x in [
                os.path.basename(p), rep["overall"], v.get("panels_found"), v.get("method"),
                v.get("min_phash"), len(v.get("near_duplicate_pairs") or []),
                pal.get("off_palette_pct_of_coloured"), pal.get("off_palette_pct"),
                t.get("n_tokens"), t.get("max_conf"), d.get("luma_sd")]))
        elif ns.description:
            print(description_block(rep))
            print()
        elif not ns.json:
            print(format_report(rep))
    if ns.json:
        print(json.dumps(reports if len(reports) > 1 else reports[0], indent=1))
    return worst


if __name__ == "__main__":
    sys.exit(main())
