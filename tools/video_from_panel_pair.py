#!/usr/bin/env python3
r"""VIDEO v2: drive a shot's video from a PAIR of panels, not one.

WHAT THIS ADDS. video_from_panel.py generates from a single approved panel and
lets the model invent where the shot ends. This drives the same A14B two-stage
recipe from `Shot.sg_approved_panel` as the FIRST frame and
`Shot.sg_approved_panel_end` as the LAST, so the end of the shot is a decision
the operator made rather than a place the model drifted to. The end frame is
usually the NEXT shot's approved panel, which is what makes a cut continuous.

`sg_approved_panel_end` already existed on the Shot schema and was set on 0 of
55 shots (queried 2026-09-04). The field was designed for exactly this and had
no mechanism behind it. This is the mechanism.

IT IS A V2, NOT A REPLACEMENT. video_from_panel.py is untouched and remains
the path for a shot with only one approved panel. A shot with no
sg_approved_panel_end cannot use this tool and is refused, loudly, rather than
silently falling back -- a silent fallback would make "I asked for a pair" and
"I got a single" indistinguishable in the record.

THE NODE PACK. ComfyUI-Wan22FMLF (wallen0322), cloned to custom_nodes on
2026-09-04, commit 7140cd2. Audited before loading: no network calls, no
subprocess, no eval/exec; it imports only torch, comfy and node_helpers.
NOTE FOR GEOFF: its pyproject.toml declares `license = {file = "LICENSE"}` and
the repository contains NO LICENSE FILE, so the code is formally
all-rights-reserved. That is a licensing question on client work, not a
technical one, and it is his call.

ITS README SAYS "DO NOT USE QUANTIZED MODELS" AND THAT DOES NOT APPLY HERE.
The wording is a soft recommendation (尽量, "try to"), and structurally it
cannot bind us: the node touches only the VAE, the conditioning and the latent
(vae.encode, node_helpers.conditioning_set_values with concat_latent_image and
concat_mask). It never sees the UNet, so our Q4_K_S GGUF weights are invisible
to it. That is reasoning, though, and reasoning about this project has been
wrong before -- so this tool renders and the operator looks at the result.

A REAL IMPROVEMENT WORTH NAMING. v1 feeds the SAME conditioning to both
sampler stages. This node returns positive_high and positive_low separately,
so the high-noise pass and the low-noise pass are conditioned differently.
That is the pack's own design and v1 has no equivalent.

    python video_from_panel_pair.py --self-test
    python video_from_panel_pair.py --shot SHOW01_A_0010 --dry-run
    python video_from_panel_pair.py --shot SHOW01_A_0010
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EXEC = os.path.join(HERE, "comfyui", "comfyui_execute.py")

# The workflow template lives OUTSIDE the tools directory, so it has to be
# looked for rather than assumed. deploy.py stages tools/ into a versioned
# release tree and runs every module's --self-test there; a path built as
# `dirname(dirname(__file__))/workflows` resolves inside that staging tree and
# does not exist, which is exactly how this module failed preflight the first
# time it was written. Try the repo layout first, then the E: project root
# that qwen_compose.py uses, and report every place looked at if none has it.
_ROOTS = (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    r"C:\example\genvideo-pipeline",
    # THE RETIRED CLONE WAS THE THIRD ENTRY HERE AND IS GONE. It was the working
    # tree until 2026-09-06 and now carries DO-NOT-EDIT-THIS-CLONE.md; 19 of its
    # tools already differ from these. As a LAST fallback it turned a loud "file
    # not found" into a silent load of a stale workflow, which is the worse
    # failure. (It could not even fire: os.path.join("C:", ...) yields the
    # drive-RELATIVE "C:genvideo" + os.sep + "...", not a path to that clone.)
)


def find_workflow(name):
    tried = []
    for r in _ROOTS:
        p = os.path.join(r, "workflows", name)
        tried.append(p)
        if os.path.isfile(p):
            return p
    raise SystemExit("workflow %s not found. Looked in:" + chr(10) + "  "
                     + (chr(10) + "  ").join(tried))


TPL = find_workflow("wan22_a14b_fmlf_v2.api.json")
COMFY_IN = r"C:\ComfyUI_windows_portable\ComfyUI\input"
COMFY_OUT = r"C:\ComfyUI_windows_portable\ComfyUI\output"

PROJ = {"type": "Project", "id": 9999}

# Phase 1's proven A14B recipe, copied from video_from_panel.py and NOT
# re-derived. The VAE line is a trap this project already fell into once.
UNET_HIGH = "Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf"
UNET_LOW = "Wan2.2-I2V-A14B-LowNoise-Q4_K_S.gguf"
LORA_HIGH = "wan22_lightning_i2v_a14b_high.safetensors"
LORA_LOW = "wan22_lightning_i2v_a14b_low.safetensors"
VAE_NAME = "wan_2.1_vae.safetensors"     # NEVER wan2.2_vae -- Phase 1 VAE trap
LORA_STRENGTH = "1.0"
SHIFT = "8.0"
STEPS = 4
SWITCH_STEP = 2
CFG = 1.0
FRAMES = 81                              # A14B native, 16fps
DEV_CELL = (832, 480)
PROD_CELL = (1280, 704)

# The pack's own strength knobs. All left at "full reference strength" for the
# first run: this run establishes whether the mechanism works at all, and a
# tuned knob would confound that with a tuning result. middle_frame_ratio is
# inert while no middle_image is supplied.
KNOBS = {
    "middle_frame_ratio": "0.5",
    "high_noise_mid_strength": "1.0",
    "low_noise_start_strength": "1.0",
    "low_noise_mid_strength": "1.0",
    "low_noise_end_strength": "1.0",
    # >1.001 activates the pack's motion-suppression path. Off, deliberately:
    # it is an extra mechanism and this run is measuring one thing.
    "structural_repulsion_boost": "1.0",
}

# 720x1280 is called out in the pack's README as causing middle-frame flicker.
# Our production cell is 1280x704, which is not that resolution, but the
# neighbouring one -- so it is named here rather than discovered later.
FLICKER_WARN = (720, 1280)

NEGATIVE = ("blurry, distorted, watermark, text, low quality, extra limbs, "
            "morphing, flicker")


def log(m):
    print("[fmlf] %s" % m, flush=True)


def anchor_of(sg, shot, field):
    """-> (version_code, media_path) for one panel field, or None.

    Reads the Version's file from ShotGrid and checks it exists on disk. A
    field pointing at a Version whose media has gone is a REFUSAL, not a
    warning: rendering from a missing anchor is how a run silently produces
    something nobody chose."""
    v = shot.get(field)
    if not isinstance(v, dict):
        return None
    full = sg.find_one("Version", [["id", "is", v["id"]]],
                       ["code", "sg_path_to_movie", "sg_stage",
                        "sg_status_list"])
    if not full:
        raise SystemExit("%s points at Version %s which does not exist"
                         % (field, v.get("id")))
    if full.get("sg_stage") == "wedge":
        raise SystemExit("%s points at %s, which is a WEDGE (an experiment), "
                         "not a panel. Refusing." % (field, full["code"]))
    p = full.get("sg_path_to_movie")
    if not p or not os.path.isfile(p):
        raise SystemExit("%s -> %s has no media on disk (%s). Refusing to "
                         "render from an anchor that is not there."
                         % (field, full["code"], p))
    return full["code"], p


def resolve(sg, shot_code):
    shot = sg.find_one("Shot", [["project", "is", PROJ],
                                ["code", "is", shot_code]],
                       ["code", "sg_approved_panel", "sg_approved_panel_end",
                        "sg_gen_prompt", "sg_action_beat"])
    if not shot:
        raise SystemExit("no Shot %s in project 9999" % shot_code)
    start = anchor_of(sg, shot, "sg_approved_panel")
    end = anchor_of(sg, shot, "sg_approved_panel_end")
    if not start:
        raise SystemExit("%s has no sg_approved_panel. Only the panel-approval "
                         "watcher may set that field; approve a panel first."
                         % shot_code)
    if not end:
        raise SystemExit(
            "%s has no sg_approved_panel_end, so there is no LAST frame and "
            "this tool has nothing to do. That is the whole difference from "
            "video_from_panel.py, which is the right tool for a single "
            "anchor. Refusing rather than quietly rendering a single-anchor "
            "video under a pair-anchor name." % shot_code)
    prompt = (shot.get("sg_gen_prompt") or shot.get("sg_action_beat") or "").strip()
    if not prompt:
        raise SystemExit("%s has neither sg_gen_prompt nor sg_action_beat"
                         % shot_code)
    return shot, start, end, prompt


def run(sg, shot_code, production=False, seed=1, frames=FRAMES, dry=False):
    shot, start, end, prompt = resolve(sg, shot_code)
    w, h = PROD_CELL if production else DEV_CELL
    if (w, h) == FLICKER_WARN or (h, w) == FLICKER_WARN:
        log("WARNING: %dx%d is the resolution the pack's README names as "
            "causing middle-frame flicker." % (w, h))
    log("%s  first=%s  last=%s" % (shot_code, start[0], end[0]))
    log("  %dx%d, %d frames, seed %d" % (w, h, frames, seed))
    names = []
    for tag, (_code, path) in (("start", start), ("end", end)):
        dst = "fmlf_%s_%s" % (tag, os.path.basename(path))
        if not dry:
            shutil.copyfile(path, os.path.join(COMFY_IN, dst))
        names.append(dst)
    subs = {
        "unet_high": UNET_HIGH, "unet_low": UNET_LOW,
        "lora_high": LORA_HIGH, "lora_low": LORA_LOW,
        "lora_strength": LORA_STRENGTH, "shift": SHIFT, "vae_name": VAE_NAME,
        "start_image": names[0], "end_image": names[1],
        "prompt": prompt, "negative_prompt": NEGATIVE,
        "width": str(w), "height": str(h), "length": str(frames),
        "steps": str(STEPS), "cfg": str(CFG),
        "switch_step": str(SWITCH_STEP),
    }
    subs.update(KNOBS)
    prefix = "%s_VID_FMLFv2" % shot_code
    # WEDGE 0, with the seed carried in --seed-base. comfyui_execute.py names
    # outputs "<prefix>_w%03d" from the wedge index, and
    # video_from_panel.encode_native_16fps -- which this pipeline reuses to
    # turn the PNG sequence into an mp4 -- looks for exactly "_w000_%05d_.png".
    # Running wedge 1 produced a perfectly good 81-frame sequence that the
    # encoder then could not find.
    cmd = [sys.executable, EXEC, "0", "0", "1",
           "--workflow", TPL, "--output-prefix", prefix,
           "--seed-base", str(seed), "--comfy-output-dir", COMFY_OUT,
           "--expect-outputs", str(frames), "--timeout", "3600"]
    for k, v in sorted(subs.items()):
        cmd += ["--set", "%s=%s" % (k, v)]
    if dry:
        log("  would run with %d substitutions; prompt: %s"
            % (len(subs), prompt[:100]))
        return 0
    return subprocess.call(cmd)


def self_test():
    import json
    import re
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("the v2 template exists", os.path.isfile(TPL))
    txt = open(TPL, encoding="utf-8").read()
    doc = json.loads(re.sub(r"\{[a-z_0-9]+\}", "0", txt))
    ck("every top-level key is a node with a class_type",
       all(isinstance(v, dict) and "class_type" in v for v in doc.values()))

    need = set(re.findall(r"\{([a-z_0-9]+)\}", txt))
    have = set(["unet_high", "unet_low", "lora_high", "lora_low",
                "lora_strength", "shift", "vae_name", "start_image",
                "end_image", "prompt", "negative_prompt", "width", "height",
                "length", "steps", "cfg", "switch_step"]) | set(KNOBS)
    # seed and output_prefix are reserved by comfyui_execute.py.
    have |= {"seed", "output_prefix"}
    ck("every template placeholder is substituted (missing: %s)"
       % sorted(need - have), not (need - have))

    n12 = doc["12"]
    ck("the pair node replaces WanImageToVideo",
       n12["class_type"] == "WanFirstMiddleLastFrameToVideo")
    ck("both a start and an end image are wired in",
       n12["inputs"]["start_image"] == ["11", 0]
       and n12["inputs"]["end_image"] == ["11b", 0])
    # The wiring canary. The node returns 4 outputs
    # (positive_high, positive_low, negative, latent) where WanImageToVideo
    # returned 3 (positive, negative, latent). Carrying v1's indices over
    # would silently feed the LOW-noise conditioning in as the negative.
    ck("high-noise sampler takes positive_high (index 0) and negative (2)",
       doc["13"]["inputs"]["positive"] == ["12", 0]
       and doc["13"]["inputs"]["negative"] == ["12", 2])
    ck("low-noise sampler takes positive_LOW (index 1), not positive_high",
       doc["14"]["inputs"]["positive"] == ["12", 1])
    ck("the latent comes from index 3, not v1's index 2",
       doc["13"]["inputs"]["latent_image"] == ["12", 3])
    ck("the low-noise stage continues from the high-noise latent",
       doc["14"]["inputs"]["latent_image"] == ["13", 0])

    ck("the VAE is 2.1, never 2.2 (the Phase 1 trap)",
       VAE_NAME == "wan_2.1_vae.safetensors")
    ck("the recipe still matches video_from_panel.py's pinned values",
       STEPS == 4 and SWITCH_STEP == 2 and CFG == 1.0 and SHIFT == "8.0"
       and FRAMES == 81)
    ck("the pack's extra mechanisms are all off for a first run",
       KNOBS["structural_repulsion_boost"] == "1.0"
       and all(v == "1.0" for k, v in KNOBS.items()
               if k.endswith("_strength")))
    ck("the encoder's expected wedge suffix is what we ask the executor for",
       '"0", "0", "1"' in open(os.path.abspath(__file__),
                                encoding="utf-8").read())
    ck("neither production cell is the README's flicker resolution",
       DEV_CELL != FLICKER_WARN and PROD_CELL != FLICKER_WARN
       and DEV_CELL[::-1] != FLICKER_WARN and PROD_CELL[::-1] != FLICKER_WARN)

    class _Stub(object):
        def __init__(self, shot):
            self.shot = shot

        def find_one(self, et, *a, **k):
            if et == "Shot":
                return self.shot
            return {"id": 9, "code": "V", "sg_path_to_movie": __file__,
                    "sg_stage": "panel", "sg_status_list": "apr"}

    def refuses(shot, fragment):
        try:
            resolve(_Stub(shot), "X")
        except SystemExit as exc:
            return fragment in str(exc)
        return False

    ck("a shot with no end panel is REFUSED, not silently downgraded",
       refuses({"code": "X", "sg_approved_panel": {"id": 1},
                "sg_approved_panel_end": None, "sg_gen_prompt": "p"},
               "no sg_approved_panel_end"))
    ck("a shot with no approved panel at all is refused",
       refuses({"code": "X", "sg_approved_panel": None,
                "sg_approved_panel_end": {"id": 2}, "sg_gen_prompt": "p"},
               "no sg_approved_panel"))

    class _WedgeStub(_Stub):
        def find_one(self, et, *a, **k):
            if et == "Shot":
                return self.shot
            return {"id": 9, "code": "W_s9001", "sg_path_to_movie": __file__,
                    "sg_stage": "wedge", "sg_status_list": "rev"}
    try:
        resolve(_WedgeStub({"code": "X", "sg_approved_panel": {"id": 1},
                            "sg_approved_panel_end": {"id": 2},
                            "sg_gen_prompt": "p"}), "X")
        ck("a wedge Version can never be used as a video anchor", False)
    except SystemExit as exc:
        ck("a wedge Version can never be used as a video anchor",
           "WEDGE" in str(exc))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--shot")
    ap.add_argument("--production", action="store_true",
                    help="1280x704 instead of the 832x480 dev cell")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--frames", type=int, default=FRAMES)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.shot:
        raise SystemExit("--shot is required")
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), a.shot, production=a.production,
               seed=a.seed, frames=a.frames, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
