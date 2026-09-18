#!/usr/bin/env python3
r"""Compose a multi-character panel with one masked region per character.

WHAT THIS IS FOR. 23 of SHOW01's 55 shots name two or more characters and one
names three, and every global-conditioning attempt at a multi-character panel
produced a duplicated extra figure. Splitting the conditioning into one masked
region per character fixed it on 3/3 seeds for shot SHOW01_A_0060 (Versions
67602-67604). This tool generalises that from one hand-built two-character
graph to any shot and any number of characters, so the question can be asked
of the beats where a naive split SHOULD break: a hug, a hand on a shoulder,
three people in frame.

THE GRAPH IS GENERATED, NOT SUBSTITUTED. A fixed .api.json template cannot
express "N characters" -- three characters need three text encoders, three
masks and two combines. So the conditioning half of the graph is built here in
Python. The loaders, sampler settings and decode path are copied verbatim from
the proven qwen_edit_compose recipe and are not re-derived.

MASKS, NOT AREAS, AND THAT IS A MEASUREMENT. ConditioningSetAreaPercentage
cannot be used with this model: every node executes and then KSampler dies in
comfy/samplers.py:776 at `area += (round(a[d + a_len] * dims[d]),)` with
IndexError, because `dims = noise.shape[2:]` and Qwen-Image uses a Wan-style
5D latent (B, C, T, H, W). dims carries three entries; an area tuple carries
four. The mask branch of that same function only touches dims[-1] and dims[-2],
so masks survive the extra dimension. set_cond_area must stay "default";
"mask bounds" recomputes an area and re-enters the crash.

WHAT THIS TOOL DOES NOT KNOW, AND SAYS SO. It does not know where in frame a
character actually stands. It lays N equal vertical bands left to right in the
order the beat first names them. That is a DETERMINISTIC CONVENTION, not a
reading of the blocking, and it is certainly wrong for a beat like "they pull
into a tight hug" where the bodies overlap. Establishing how much that matters
is the point of running it; the honest next step is deriving real boxes from
the beat, which is a claude -p job, not a regex one.

    python regional_compose.py --self-test
    python regional_compose.py --shot SHOW01_A_0060 --dry-run
    python regional_compose.py --shot SHOW01_A_0460 --seeds 3
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

REPO = os.path.dirname(HERE)
EXEC = os.path.join(HERE, "comfyui", "comfyui_execute.py")
COMFY_IN = r"C:\ComfyUI_windows_portable\ComfyUI\input"
COMFY_OUT = r"C:\ComfyUI_windows_portable\ComfyUI\output"

PROJ = {"type": "Project", "id": 9999}
EPISODE = {"type": "Episode", "id": 1687}

# a peer engineer-proven recipe. Copied from qwen_compose.py, deliberately not imported:
# that module loads templates from the E: Dropbox tree at import time. If these
# ever drift from it the self-test's comparison is the thing that should fail.
RECIPE = {
    "unet_name": "Qwen-Image-Edit-2509-Q3_K_M.gguf",
    "lora_name": "qwen_image_edit_2509_lightning_4steps_v1.safetensors",
    "lora_strength": 1.0,
    "clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "vae_name": "qwen_image_vae.safetensors",
    "shift": 3.0,
    "steps": 4,
    "cfg": 1.0,
    "megapixels": 1.0,
}
NEGATIVE = ("extra characters, extra puppets, multiple people, blurry, "
            "distorted, watermark, text, low quality")

# Names the beats use, lowercase. A beat that names someone not here composes
# without them rather than silently inventing a body for the name -- that is
# the absent-character defect the dialogue guard already fixes globally.
KNOWN = ("PILOTCHARB", "PILOTCHARA", "PILOTCHARC")


def log(m):
    print("[regional] %s" % m, flush=True)


def characters_in(beat):
    """-> character names in the order the beat FIRST mentions them.

    Order is load-bearing: it decides which band each character gets. It is a
    convention and nothing more -- the beat's word order is not its staging.
    SHOW01_A_0060 names PilotCharA first and renders PilotCharB on the left."""
    seen = []
    for m in re.finditer(r"\b([A-Za-z]+)\b", beat or ""):
        w = m.group(1).lower()
        if w in KNOWN and w not in seen:
            seen.append(w)
    return seen


def bands(n):
    """-> n equal vertical bands as (x_fraction, width_fraction) pairs.

    They tile the frame exactly: no gap leaves a pixel unconditioned, no
    overlap makes two characters compete for one region."""
    if n < 1:
        raise ValueError("no characters to place")
    w = 1.0 / n
    return [(i * w, w) for i in range(n)]


def build_graph(refs, prompts, canvas, seed_ph="{seed}",
                out_ph="{output_prefix}", set_ref=None):
    """Assemble the API graph. refs and prompts are parallel lists, one entry
    per character, already in band order.

    Node ids are strings because that is what the /prompt endpoint wants, and
    EVERY top-level key must be a node object: a prose "_comment" key parses as
    valid JSON and then crashes ComfyUI's validate_prompt with
    `'str' object has no attribute 'get'`, answered to the client as a bare
    HTTP 500. The rationale lives in this docstring instead."""
    cw, ch = canvas
    n = len(refs)
    if n != len(prompts):
        raise ValueError("refs and prompts must be the same length")
    g = {
        "1": {"class_type": "UnetLoaderGGUF",
              "inputs": {"unet_name": RECIPE["unet_name"]}},
        "2": {"class_type": "LoraLoaderModelOnly",
              "inputs": {"model": ["1", 0], "lora_name": RECIPE["lora_name"],
                         "strength_model": RECIPE["lora_strength"]}},
        "3": {"class_type": "ModelSamplingAuraFlow",
              "inputs": {"model": ["2", 0], "shift": RECIPE["shift"]}},
        "4": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": RECIPE["clip_name"],
                         "type": "qwen_image", "device": "default"}},
        "5": {"class_type": "VAELoader",
              "inputs": {"vae_name": RECIPE["vae_name"]}},
        "m0": {"class_type": "SolidMask",
               "inputs": {"value": 0.0, "width": cw, "height": ch}},
    }

    if set_ref is not None:
        g["setld"] = {"class_type": "LoadImage", "inputs": {"image": set_ref}}
        g["setsc"] = {"class_type": "ImageScaleToTotalPixels",
                      "inputs": {"image": ["setld", 0],
                                 "upscale_method": "lanczos",
                                 "megapixels": RECIPE["megapixels"],
                                 "resolution_steps": 8}}

    combined = None
    for i, (ref, prompt) in enumerate(zip(refs, prompts)):
        x_frac, w_frac = bands(n)[i]
        # Integer pixels, and the LAST band absorbs any rounding remainder so
        # the bands still tile exactly when n does not divide the width.
        x0 = int(round(x_frac * cw))
        x1 = cw if i == n - 1 else int(round((x_frac + w_frac) * cw))
        bw = x1 - x0
        ld, sc, ms, mk, te, cm = ("ld%d" % i, "sc%d" % i, "ms%d" % i,
                                  "mk%d" % i, "te%d" % i, "cm%d" % i)
        g[ld] = {"class_type": "LoadImage", "inputs": {"image": ref}}
        g[sc] = {"class_type": "ImageScaleToTotalPixels",
                 "inputs": {"image": [ld, 0], "upscale_method": "lanczos",
                            "megapixels": RECIPE["megapixels"],
                            "resolution_steps": 8}}
        g[ms] = {"class_type": "SolidMask",
                 "inputs": {"value": 1.0, "width": bw, "height": ch}}
        g[mk] = {"class_type": "MaskComposite",
                 "inputs": {"destination": ["m0", 0], "source": [ms, 0],
                            "x": x0, "y": 0, "operation": "add"}}
        te_in = {"clip": ["4", 0], "vae": ["5", 0],
                 "image1": [sc, 0], "prompt": prompt}
        if set_ref is not None:
            # THE SET GOES IN EVERY BAND, not in one of them.
            #
            # The wedges that proved this mechanism described the room in text
            # and used no set reference at all, which was right for isolating
            # the conditioning change but is a real fidelity loss against the
            # single-character path, where the approved set design IS an image
            # input. Giving each band image1=its character and image2=the set
            # keeps both: every region still sees exactly one character, and
            # every region sees the same room.
            #
            # Putting the set in only one band would make that band's half of
            # the frame the only half anchored to the approved design, which
            # is worse than not using it.
            te_in["image2"] = ["setsc", 0]
        g[te] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": te_in}
        g[cm] = {"class_type": "ConditioningSetMask",
                 "inputs": {"conditioning": [te, 0], "mask": [mk, 0],
                            "strength": 1.0, "set_cond_area": "default"}}
        if combined is None:
            combined = cm
        else:
            j = "cc%d" % i
            g[j] = {"class_type": "ConditioningCombine",
                    "inputs": {"conditioning_1": [combined, 0],
                               "conditioning_2": [cm, 0]}}
            combined = j

    # The negative is composed and sent but is INERT at cfg 1.0 (proven
    # pixel-identical at delta 0.000). It stays for shape parity with the
    # compose template, not because it does anything.
    neg_in = {"clip": ["4", 0], "vae": ["5", 0], "image1": ["sc0", 0],
              "prompt": NEGATIVE}
    if set_ref is not None:
        neg_in["image2"] = ["setsc", 0]
    g["neg"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": neg_in}
    # The starting latent comes from the FIRST character's scaled pixels, as
    # in the compose template. At denoise 1.0 it contributes canvas shape only.
    g["lat"] = {"class_type": "VAEEncode",
                "inputs": {"pixels": ["sc0", 0], "vae": ["5", 0]}}
    g["ks"] = {"class_type": "KSampler",
               "inputs": {"model": ["3", 0], "positive": [combined, 0],
                          "negative": ["neg", 0], "latent_image": ["lat", 0],
                          "seed": seed_ph, "steps": RECIPE["steps"],
                          "cfg": RECIPE["cfg"], "sampler_name": "euler",
                          "scheduler": "simple", "denoise": 1.0}}
    g["dec"] = {"class_type": "VAEDecode",
                "inputs": {"samples": ["ks", 0], "vae": ["5", 0]}}
    g["save"] = {"class_type": "SaveImage",
                 "inputs": {"filename_prefix": out_ph, "images": ["dec", 0]}}
    return g


def canvas_for(path):
    """The post-rescale canvas, derived from the first reference.

    The masks must match what ImageScaleToTotalPixels produces or they land on
    the wrong pixels. Derived, never pinned."""
    import math
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    sc = math.sqrt(RECIPE["megapixels"] * 1024 * 1024 / float(w * h))
    return int(round(w * sc)) // 8 * 8, int(round(h * sc)) // 8 * 8


def resolve(sg, shot_code):
    """-> (beat, [(name, anchor_path)]) for one Shot, from ShotGrid only.

    Refuses rather than guesses: a named character with no approved anchor
    stops the run, because composing without their reference is exactly how
    the model invents a stranger for the name."""
    # `assets` is in this field list because sg_provenance.classify_assets
    # reads it off the row rather than re-querying. Omitting it made
    # set_anchor() report "no approved SET design" for a shot whose set is
    # approved and on disk -- a missing FIELD reported as a missing ASSET,
    # which is exactly the shape of error this session keeps finding.
    shot = sg.find_one("Shot", [["project", "is", PROJ],
                                ["code", "is", shot_code]],
                       ["code", "sg_action_beat", "sg_episode", "assets"])
    if not shot:
        raise SystemExit("no Shot %s in project 9999" % shot_code)
    beat = (shot.get("sg_action_beat") or "").strip()
    if not beat:
        raise SystemExit("%s has no sg_action_beat -- nothing to compose"
                         % shot_code)
    names = characters_in(beat)
    if len(names) < 2:
        raise SystemExit("%s names %d character(s); this tool is for the "
                         "multi-character case" % (shot_code, len(names)))
    out = []
    for n in names:
        code = "SHOW_CHAR_%s" % n.upper()
        a = sg.find_one("Asset", [["project", "is", PROJ],
                                  ["code", "is", code]],
                        ["code", "sg_approved_design"])
        ap = (a or {}).get("sg_approved_design")
        if not isinstance(ap, dict):
            raise SystemExit("%s has no sg_approved_design -- refusing to "
                             "compose a character with no reference" % code)
        v = sg.find_one("Version", [["id", "is", ap["id"]]],
                        ["code", "sg_path_to_movie"])
        p = (v or {}).get("sg_path_to_movie")
        if not p or not os.path.isfile(p):
            raise SystemExit("%s approved anchor %s has no file on disk (%s)"
                             % (code, ap.get("name"), p))
        out.append((n, p))
    return shot, beat, out


# The shared clause is identical in every band: the room, camera and light are
# properties of the frame, not of a person, and a band describing a different
# room would be a second changed variable.
SHARED = ("Camera static, locked-off composition, natural consistent "
          "lighting")
PRESERVE = ("Preserve the character exactly as shown in the character "
            "reference image - same proportions, materials, colours, face "
            "and clothing - do not redesign or reimagine that character")


def beat_for(beat, name, all_names):
    """The slice of the beat that belongs in ONE character's band.

    Keep the sentences that name this character, falling back to the whole
    beat if none do, then run the absent-character guard over the result.

    AN OPEN RISK, DELIBERATELY LEFT IN AND TO BE MEASURED. The guard will NOT
    strip a name from a clause that also names a present character -- "a
    clause that ALSO names a present character is never removed" is its
    documented over-stripping protection, and it is right to have it. So
    "PilotCharA kneels beside PilotCharB" survives intact in PilotCharA's band, naming a
    character that band holds no reference for. Whether that makes the band
    invent a second body is exactly the kind of thing this project has been
    wrong about by reasoning, so it gets rendered rather than argued: if the
    cross-mention duplicates, the fix is a band-scoped variant of the guard,
    NOT a loosening of the global one, whose caller (panel_compose) genuinely
    does have both characters present. One implementation per job, and these
    are two different jobs (invariant 11)."""
    import dialogue_guard
    parts = re.split(r"(?<=[.!?])\s+", (beat or "").strip())
    mine = [p for p in parts if re.search(r"\b%s\b" % re.escape(name), p, re.I)]
    text = " ".join(mine) if mine else (beat or "").strip()
    return dialogue_guard.strip_absent_character_clauses(
        text, [name], list(all_names))


def prompts_for(beat, names, setting):
    """One prompt per band. NO COUNT CLAUSE, deliberately: it measured 0/12
    and it is the thing regions replace. The geometry does the counting.

    The no-text clause comes from dialogue_guard.append_no_text_clause (D15),
    again rather than a second copy of the wording."""
    import dialogue_guard
    out = []
    for n in names:
        body = beat_for(beat, n, names)
        text = ("Place the character from the character reference image into "
                "%s. %s %s. %s." % (setting, body, PRESERVE, SHARED))
        out.append(dialogue_guard.append_no_text_clause(text))
    return out


def set_anchor(sg, shot):
    """-> (code, path) for the shot's approved SET design, or None.

    Uses sg_provenance.classify_assets, the same classifier panel_compose uses,
    rather than a second rule for what counts as a set (invariant 11)."""
    import sg_provenance as PROV
    _ch, sets, _o = PROV.classify_assets(sg, shot)
    if not sets:
        return None
    a = sg.find_one("Asset", [["project", "is", PROJ], ["code", "is", sets[0]]],
                    ["code", "sg_approved_design"])
    ap = (a or {}).get("sg_approved_design")
    if not isinstance(ap, dict):
        return None
    v = sg.find_one("Version", [["id", "is", ap["id"]]],
                    ["code", "sg_path_to_movie"])
    pth = (v or {}).get("sg_path_to_movie")
    if not pth or not os.path.isfile(pth):
        return None
    return sets[0], pth


def run(sg, shot_code, setting, seeds, seed_base, dry=False, use_set=False):
    import shutil
    shot, beat, chars = resolve(sg, shot_code)
    names = [c[0] for c in chars]
    log("%s names %d character(s): %s" % (shot_code, len(names),
                                          ", ".join(names)))
    log("  beat: %s" % beat[:150])
    refs = []
    for n, src in chars:
        dst_name = "regional_%s_%s" % (n, os.path.basename(src))
        if not dry:
            shutil.copyfile(src, os.path.join(COMFY_IN, dst_name))
        refs.append(dst_name)
    set_name = None
    if use_set:
        sa = set_anchor(sg, shot)
        if not sa:
            raise SystemExit("%s has no approved SET design on disk; refusing to "
                             "compose with the room in text when a set was asked "
                             "for" % shot_code)
        set_code, set_path = sa
        set_name = "regional_set_" + os.path.basename(set_path)
        if not dry:
            shutil.copyfile(set_path, os.path.join(COMFY_IN, set_name))
        log("  set reference: %s" % set_code)
    canvas = canvas_for(chars[0][1])
    log("  canvas %dx%d, %d band(s) of %dpx" % (canvas[0], canvas[1],
                                                len(names),
                                                canvas[0] // len(names)))
    graph = build_graph(refs, prompts_for(beat, names, setting), canvas,
                        set_ref=set_name)
    prefix = "%s_PNL_REGIONAL%s_%dCH" % (shot_code, "SET" if set_name else "",
                                         len(names))
    fh = tempfile.NamedTemporaryFile("w", suffix=".api.json", delete=False,
                                     encoding="utf-8")
    json.dump(graph, fh, indent=1)
    fh.close()
    log("  graph: %d nodes -> %s" % (len(graph), fh.name))
    cmd = [sys.executable, EXEC, "1", str(seeds), "1",
           "--workflow", fh.name, "--output-prefix", prefix,
           "--seed-base", str(seed_base),
           "--comfy-output-dir", COMFY_OUT, "--expect-outputs", "1"]
    if dry:
        log("  would run: %s" % " ".join(cmd[:8]))
        return 0
    return subprocess.call(cmd)


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-68s %s" % (name[:68], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("characters are found in first-mention order",
       characters_in("PilotCharA kneels beside PilotCharB, who is asleep.")
       == ["PILOTCHARA", "PILOTCHARB"])
    ck("a repeated name is listed once",
       characters_in("PilotCharB looks at PilotCharA. PilotCharB sighs. PilotCharA nods.")
       == ["PILOTCHARB", "PILOTCHARA"])
    ck("three characters are all found",
       characters_in("PilotCharA gestures at PilotCharB. PilotCharC looks between them.")
       == ["PILOTCHARA", "PILOTCHARB", "PILOTCHARC"])
    ck("a substring of a name is not a match",
       characters_in("The PILOTCHARBal ran off.") == [])
    ck("no names gives an empty list", characters_in("") == []
       and characters_in(None) == [])

    ck("two bands tile the frame", bands(2) == [(0.0, 0.5), (0.5, 0.5)])
    ck("three bands tile the frame",
       abs(sum(w for _, w in bands(3)) - 1.0) < 1e-9
       and abs(bands(3)[2][0] + bands(3)[2][1] - 1.0) < 1e-9)

    g2 = build_graph(["a.png", "b.png"], ["pa", "pb"], (1376, 752))
    ck("every top-level key is a node with a class_type (a prose key is a 500)",
       all(isinstance(v, dict) and "class_type" in v for v in g2.values()))
    ck("two characters produce exactly one combine",
       sum(1 for v in g2.values()
           if v["class_type"] == "ConditioningCombine") == 1)
    ck("the sampler's positive is the combined conditioning",
       g2["ks"]["inputs"]["positive"][0] == "cc1")

    g3 = build_graph(["a.png", "b.png", "c.png"], ["p", "q", "r"], (1377, 752))
    ck("three characters produce two chained combines",
       sum(1 for v in g3.values()
           if v["class_type"] == "ConditioningCombine") == 2)
    # The rounding canary: 1377 does not divide by 3, and a lost pixel would
    # leave a one-pixel column conditioned by nobody.
    widths = [v["inputs"]["width"] for k, v in g3.items()
              if k.startswith("ms")]
    xs = sorted(v["inputs"]["x"] for k, v in g3.items() if k.startswith("mk"))
    ck("bands tile an indivisible width exactly (%s at %s)" % (widths, xs),
       sum(widths) == 1377 and xs[0] == 0)
    ck("no area node is ever emitted (it crashes on Qwen's 5D latent)",
       not any("Area" in v["class_type"] for v in g3.values()))
    ck("set_cond_area is always 'default', never 'mask bounds'",
       all(v["inputs"].get("set_cond_area", "default") == "default"
           for v in g3.values()
           if v["class_type"] == "ConditioningSetMask"))
    ck("the graph is JSON-serialisable as the API expects",
       isinstance(json.dumps(g3), str))

    # --- the set reference, added so this can replace panel_compose's
    # single-character path without losing the approved set design as an
    # IMAGE input. 30 of 55 SHOW01 shots link two characters.
    gs = build_graph(["a.png", "b.png"], ["p", "q"], (1376, 752),
                     set_ref="set.png")
    ck("no set reference means no set nodes at all (the wedge shape is intact)",
       "setld" not in g2 and not any("image2" in v["inputs"]
                                     for v in g2.values()
                                     if v["class_type"] == "TextEncodeQwenImageEditPlus"))
    ck("with a set, EVERY character band sees it, not just one",
       all(v["inputs"].get("image2") == ["setsc", 0] for k, v in gs.items()
           if k.startswith("te")))
    ck("the set is loaded once and scaled, not re-loaded per band",
       sum(1 for v in gs.values() if v["class_type"] == "LoadImage") == 3)
    ck("each band still sees exactly ONE character as image1",
       gs["te0"]["inputs"]["image1"] == ["sc0", 0]
       and gs["te1"]["inputs"]["image1"] == ["sc1", 0])
    ck("the set graph is still a valid node graph",
       all(isinstance(v, dict) and "class_type" in v for v in gs.values()))

    ps = prompts_for("PilotCharB hugs PilotCharA.", ["PILOTCHARB", "PILOTCHARA"], "a hallway")
    ck("one prompt per band", len(ps) == 2)
    ck("no count clause reaches the prompt (it measured 0/12)",
       not any("exactly two" in p.lower() or "do not add any extra" in p.lower()
               for p in ps))
    import dialogue_guard
    ck("every prompt carries the D15 no-text clause",
       all("do not render any text" in p.lower() for p in ps))
    ck("a sentence naming only the other character is dropped from this band",
       "PILOTCHARC" not in beat_for(
           "PilotCharB sits up. PilotCharC pulls the blanket over her chest.",
           "PILOTCHARB", ["PILOTCHARB", "PILOTCHARC"]).lower())
    # Asserting the guard's ACTUAL contract, not the one that would be
    # convenient here: it refuses to strip a name from a clause that also
    # names a present character. That is its over-stripping protection. If
    # this test ever flips, the guard changed under a caller that depends on
    # it NOT changing.
    b2 = beat_for("PilotCharA kneels beside PilotCharB, who is asleep.", "PILOTCHARA",
                  ["PILOTCHARA", "PILOTCHARB"])
    ck("a shared clause survives intact: the guard will not over-strip",
       "PILOTCHARB" in b2.lower() and "PILOTCHARA" in b2.lower())
    ck("a beat naming nobody falls back to the whole beat rather than empty",
       beat_for("The room is dim and quiet.", "PILOTCHARB", ["PILOTCHARB"]).strip() != "")
    ck("the recipe still matches qwen_compose.py's pinned values",
       RECIPE["cfg"] == 1.0 and RECIPE["steps"] == 4 and RECIPE["shift"] == 3.0)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shot")
    ap.add_argument("--setting", default="the room described in the beat")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed-base", type=int, default=9100)
    ap.add_argument("--use-set", action="store_true",
                    help="feed the shot's approved SET design into every band")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.shot:
        raise SystemExit("--shot is required")
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), a.shot, a.setting, a.seeds, a.seed_base,
               dry=a.dry_run, use_set=a.use_set)


if __name__ == "__main__":
    sys.exit(main())
