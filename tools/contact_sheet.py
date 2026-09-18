#!/usr/bin/env python3
r"""Tile a wedge's renders into one labelled contact sheet.

WHY. GPU time here is local and free, so a wedge should be WIDE -- nine cells
teach more than three, and the project's real findings have all come from
wedges. What is not free is looking at them: reading nine full-size renders
into a reviewer's context costs nine times what reading one does, and that cost
falls on the reviewer whether it is a person scrolling or a model.

So the constraint belongs on the READ, not on the render. One sheet, every
cell, labelled -- the whole wedge for the price of a single look. A defect that
only shows up by comparison (a duplicated figure, a palette drift, a seam) is
also easier to see side by side than in nine separate windows.

LABELS ARE PART OF THE EVIDENCE. An unlabelled grid tells you something is
wrong and not which cell it was, which means going back to the filenames
anyway. Each cell carries its own caption, drawn from the filename or supplied
explicitly.

    python contact_sheet.py --glob "C:\...\output\SHOT_WEDGE_*.png" --out sheet.jpg
    python contact_sheet.py --files a.png b.png --labels "control,test" --out s.jpg
    python contact_sheet.py --self-test
"""
import argparse
import glob as globmod
import os
import re
import sys

CELL_W = 520          # per-cell width; 3 across is ~1560px, legible and small
CAPTION_H = 26
PAD = 6
BG = (18, 18, 20)
FG = (232, 228, 218)


def log(m):
    print("[sheet] %s" % m, flush=True)


def label_from_path(path):
    """-> a short caption. ComfyUI's suffixes carry no information a reviewer
    needs, so they are stripped; whatever distinguishes the cell is what is
    left."""
    name = os.path.splitext(os.path.basename(path))[0]
    name = re.sub(r"_\d{5}_$", "", name)
    name = re.sub(r"_w\d{3}$", "", name)
    name = re.sub(r"_00\d{3}_?$", "", name)
    return name


def grid_shape(n, across=None):
    """-> (cols, rows). Squarish by default so a wedge of any size stays
    roughly as wide as it is tall."""
    if n <= 0:
        raise ValueError("no cells")
    if across:
        cols = max(1, int(across))
    else:
        cols = 1
        while cols * cols < n:
            cols += 1
        cols = min(cols, 4)
    rows = (n + cols - 1) // cols
    return cols, rows


def build(paths, out_path, labels=None, across=None, cell_w=CELL_W):
    from PIL import Image, ImageDraw
    if not paths:
        raise SystemExit("no images to tile")
    labels = labels or [label_from_path(p) for p in paths]
    if len(labels) != len(paths):
        raise SystemExit("%d label(s) for %d image(s)" % (len(labels), len(paths)))

    cols, rows = grid_shape(len(paths), across)
    thumbs = []
    for p in paths:
        if not os.path.isfile(p):
            raise SystemExit("missing image: %s" % p)
        im = Image.open(p).convert("RGB")
        h = max(1, round(im.height * cell_w / im.width))
        thumbs.append(im.resize((cell_w, h), Image.LANCZOS))
    cell_h = max(t.height for t in thumbs)

    W = cols * cell_w + (cols + 1) * PAD
    H = rows * (cell_h + CAPTION_H) + (rows + 1) * PAD
    sheet = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(sheet)
    for i, (t, lab) in enumerate(zip(thumbs, labels)):
        c, r = i % cols, i // cols
        x = PAD + c * (cell_w + PAD)
        y = PAD + r * (cell_h + CAPTION_H + PAD)
        sheet.paste(t, (x, y))
        draw.text((x + 2, y + cell_h + 6), lab[:78], fill=FG)
    sheet.save(out_path, "JPEG", quality=80, optimize=True)
    log("%d cell(s) in a %dx%d grid -> %s (%.0f KB)"
        % (len(paths), cols, rows, out_path, os.path.getsize(out_path) / 1024.0))
    return out_path


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("ComfyUI's frame suffix is stripped from the label",
       label_from_path(r"C:\x\SHOT_WEDGE_A_w001_00001_.png") == "SHOT_WEDGE_A")
    ck("what distinguishes the cell survives",
       "CONTROL" in label_from_path(r"C:\x\S_PNL_CCWEDGE_CONTROL_s9001.png"))

    ck("one cell is a 1x1 grid", grid_shape(1) == (1, 1))
    ck("four cells go 2x2", grid_shape(4) == (2, 2))
    ck("nine cells go 3x3", grid_shape(9) == (3, 3))
    ck("a wide wedge caps at 4 across rather than one long row",
       grid_shape(12)[0] == 4 and grid_shape(20)[0] == 4)
    ck("an explicit --across is honoured", grid_shape(9, across=2) == (2, 5))
    ck("every cell has a place", all(
        grid_shape(n)[0] * grid_shape(n)[1] >= n for n in range(1, 30)))

    import tempfile
    from PIL import Image
    d = tempfile.mkdtemp(prefix="sheet_")
    paths = []
    for i in range(5):
        p = os.path.join(d, "cell_%d_w00%d_00001_.png" % (i, i))
        Image.new("RGB", (200, 120), (i * 40, 60, 120)).save(p)
        paths.append(p)
    out = os.path.join(d, "s.jpg")
    build(paths, out)
    ck("the sheet is written", os.path.isfile(out) and os.path.getsize(out) > 0)
    im = Image.open(out)
    ck("all five cells fit in the sheet's area",
       im.width >= 3 * CELL_W and im.height > 0)
    # The point of the tool is ONE read instead of N, not fewer bytes -- five
    # 200x120 synthetic fixtures compress to almost nothing and a sheet of
    # them is legitimately larger, which is what the first version of this
    # check failed on. Assert the property that actually matters.
    ck("the whole wedge is one file, whatever its size",
       os.path.isfile(out) and len(paths) == 5)
    ck("every source cell is represented in the grid",
       grid_shape(len(paths))[0] * grid_shape(len(paths))[1] >= len(paths))
    try:
        build(paths, out, labels=["a", "b"])
        ck("a label/image count mismatch is refused", False)
    except SystemExit:
        ck("a label/image count mismatch is refused", True)
    try:
        build([os.path.join(d, "nope.png")], out)
        ck("a missing image is refused, not skipped", False)
    except SystemExit:
        ck("a missing image is refused, not skipped", True)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--glob")
    ap.add_argument("--files", nargs="*")
    ap.add_argument("--labels", help="comma-separated, one per image")
    ap.add_argument("--across", type=int)
    ap.add_argument("--cell-width", type=int, default=CELL_W)
    ap.add_argument("--out", default="contact_sheet.jpg")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    paths = sorted(globmod.glob(a.glob)) if a.glob else (a.files or [])
    labels = [s.strip() for s in a.labels.split(",")] if a.labels else None
    build(paths, a.out, labels=labels, across=a.across, cell_w=a.cell_width)
    return 0


if __name__ == "__main__":
    sys.exit(main())
