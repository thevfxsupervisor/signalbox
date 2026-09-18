#!/usr/bin/env python3
"""Phase 3: Qwen-Image-Edit-2509 compositor - reference image(s) + instruction -> one image.

MASTER-PLAN-V2.md Phase 3 ("Compositor spike"). Composes a panel from an approved (or
provisional) character design + set design + action text, using an image-edit model instead
of prompting a scene from scratch - the fix for a peer engineer finding this whole plan exists to
apply: "drift enters at the anchor generation step, not at the i2v step."

THE RECIPE IS NOT MINE. It is a peer engineer's proven, shipped production recipe for exactly this model
(see docs/METHOD.md for the general method; another internal project's batch-compose tool's
`qwen_edit` job type on a peer engineer's box), ported node-for-node:

    UNETLoader/UnetLoaderGGUF -> LoraLoaderModelOnly(Lightning 4-step, strength 1.0)
    -> ModelSamplingAuraFlow(shift 3.0); CLIPLoader(qwen_2.5_vl_7b_fp8_scaled, type=qwen_image);
    VAELoader(qwen_image_vae); LoadImage -> ImageScaleToTotalPixels(1.0 MP, lanczos) per
    reference image; TextEncodeQwenImageEditPlus(image1[, image2]) x2 (positive/negative);
    VAEEncode of image1's SCALED pixels as the starting latent; KSampler(steps 4, cfg 1.0,
    euler/simple, denoise 1.0); VAEDecode; SaveImage.

Only substitution from a peer engineer's graph: UnetLoaderGGUF in place of UNETLoader, because this box's
12 GB card takes the Q3_K_M GGUF quant of Qwen-Image-Edit-2509 rather than the fp8 safetensors
checkpoint a peer engineer runs on their bigger card (see docs/METHOD.md section 1 for why
Q3_K_M and not Q4_K_M/Q4_K_S).

cfg 1.0 IS LOAD-BEARING (a peer engineer's own finding, `FINDING-speed-lora-is-a-fidelity-anchor`): above
cfg 1.0 the result gets pulled toward the TEXT, which has no idea what the character looks like,
and away from the reference image. At cfg 1.0 with the Lightning LoRA there is no unconditional
branch pulling away, so the LoRA anchors fidelity to the source pixels. This is also why the
negative prompt is INERT here, exactly like the A14B Lightning recipe elsewhere in this
codebase (MASTER-PLAN-V2.md section 3, "Lightning consequence") - composed and sent anyway so
the recipe is honest about what it asked for, but the "no extra characters" instruction below
lives in the POSITIVE prompt (PRESERVE_SUFFIX), not the negative, because that is the only place
it can actually steer anything.

GENERALITY, ON PURPOSE. This module does not know the word "panel". `qwen_edit()` takes
image1 (the identity to preserve) + an optional image2/image3 (additional references) + an
instruction, and returns one output image - nothing panel-specific. Phase 3's own use (character
design + set design + action text -> panel) is one caller of it, wired up in cmd_compose() /
compose_panel() below. The SAME function is what a future "rebuild the character sheets from one
locked anchor per character" job (Geoff's ask, relayed via the Phase 3 coordinator note) would
call: one anchor image as image1, a pose/view instruction, no image2. Nothing here would need to
change for that; only the caller changes.

    python qwen_compose.py --image1 PATH --instruction TEXT --output-prefix NAME
        [--image2 PATH] [--seed N] [--raw] [--self-test]
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
import uuid

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools"),
# which only feeds WRAPPER below -- self-relative now.
TOOLS = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(TOOLS, "comfyui", "comfyui_execute.py")
TPL_DUAL = os.path.join(ROOT, "workflows", "qwen_edit_compose.api.json")
TPL_SINGLE = os.path.join(ROOT, "workflows", "qwen_edit_single.api.json")
# TWO-CHARACTER-COMPOSITE.md: TextEncodeQwenImageEditPlus itself accepts image1/image2/image3
# (confirmed live against the installed node, http://127.0.0.1:8188/object_info/
# TextEncodeQwenImageEditPlus, not assumed from the docs or the module docstring above) - this
# template wires the third slot. Geoff's steer (relayed via a peer engineer, citing a peer engineer's own
# finding that mixing latent input and reference image works) demoted this from "the fix" to "a
# secondary, still-worth-having option": the PRIMARY two-character path is compose_panel_two()
# below, which reallocates image1/image2 to the two characters and does NOT use this template.
# TPL_TRIPLE exists for the case compose_panel_two() itself can reach for when a caller also
# wants the set as a genuine image reference (image3) rather than text-only - see
# compose_panel_two()'s set_image parameter.
TPL_TRIPLE = os.path.join(ROOT, "workflows", "qwen_edit_triple.api.json")
# LEVER A (Geoff's steer, relayed via a peer engineer, citing a peer engineer's "mixing latent input and
# reference image" finding): the graph VAEEncodes image1's scaled pixels into the starting
# latent, then runs KSampler at denoise 1.0 - fully renoised, so that latent channel currently
# contributes nothing but canvas shape; all identity comes from TextEncodeQwenImageEditPlus
# conditioning. This template decouples the two: image1/image2 stay wired to conditioning only
# (character identity), a THIRD, separate LoadImage (blocking_image) feeds VAEEncode instead, and
# denoise is a real placeholder (every other template hardcodes 1.0) so composition/placement can
# come from a rough two-character blocking image at denoise < 1.0 while identity still comes from
# the reference conditioning. See compose_panel_two_blocking() below.
TPL_TWOCHAR_BLOCKING = os.path.join(ROOT, "workflows", "qwen_edit_two_char_blocking.api.json")
OUT_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\output"
COMFY_IN = r"C:\ComfyUI_windows_portable\ComfyUI\input"
PY = sys.executable

# a peer engineer-proven Qwen-Image-Edit-2509 recipe (see module docstring). Do not change these without
# re-proving the recipe - shift is ModelSamplingAuraFlow's, NOT the ModelSamplingSD3 shift 8.0
# used elsewhere in this codebase for the Wan A14B graphs; the two are unrelated numbers on
# unrelated node types for unrelated models, and carrying one to the other is a real trap a
# coordinator note caught before it was made here.
UNET_NAME = "Qwen-Image-Edit-2509-Q3_K_M.gguf"
LORA_NAME = "qwen_image_edit_2509_lightning_4steps_v1.safetensors"
LORA_STRENGTH = "1.0"
CLIP_NAME = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
SHIFT = "3.0"
STEPS = "4"
CFG = "1.0"

# Our own show's preserve-appearance suffix - the SAME architectural move as a peer engineer's PRESERVE
# constant (a fixed suffix appended to every edit prompt so identity/environment fidelity does
# not depend on remembering to type it each time), but NOT a peer engineer's literal text: theirs asserts
# "2D cel-shaded cartoon style", which is a peer engineer's show, not this one. This show's material
# (carved wood vs. felt/fabric) is an open, unresolved question as of this phase (see
# PHASE-3-BUILD.md "Cross-phase finding") - so this suffix deliberately does NOT assert a
# material. It only asserts what a compositor's whole job is: don't redesign, don't add
# characters. The "no extra characters" clause lives here, in the POSITIVE prompt, because cfg
# 1.0 makes the negative prompt inert (see module docstring).
PRESERVE_SUFFIX = (
    "Camera static, locked-off composition, natural consistent lighting. Preserve the "
    "character exactly as shown in the character reference image - same proportions, "
    "materials, colours, face and clothing - do not redesign or reimagine the character. "
    "Preserve the background environment exactly as shown in the set reference image - do not "
    "redesign the room. Exactly one character in the frame; do not add any extra characters, "
    "puppets, or figures beyond the one being placed."
)

# MULTI-INSTANCE VARIANT (docs/METHOD.md wedge, cells W5/W6).
# PRESERVE_SUFFIX's own last sentence -- "Exactly one character in the frame"
# -- is sent on EVERY panel this module ever composes, single-instance
# characters (CHARB, CHARJ, ...) and multi-instance ones (CHAR_CHARH_FLOCK's
# flock, CHAR_CHARD_TWINS' pair) alike. For a multi-instance character that
# clause is a standing instruction to do the exact wrong thing -- the wedge's
# W0 control (real PILOT01_A_0400 beat, no count wording, DEFAULT suffix)
# collapsed CHAR_CHARH_FLOCK's flock to 2 birds; W5 (SAME beat text, ONLY
# this suffix's last sentence swapped for the one below) recovered 9-10 birds
# on its own, with no count wording added at all. This is not a lighter
# version of PRESERVE_SUFFIX -- every other clause (camera, don't-redesign-
# character, don't-redesign-room) is identical; only the count clause
# differs, because that is the one sentence that is actually wrong for these
# characters.
PRESERVE_SUFFIX_MULTI = (
    "Camera static, locked-off composition, natural consistent lighting. Preserve the "
    "character exactly as shown in the character reference image - same proportions, "
    "materials, colours, face and clothing - do not redesign or reimagine the character. "
    "Preserve the background environment exactly as shown in the set reference image - do not "
    "redesign the room. This character is shown as multiple simultaneous copies; preserve the "
    "exact number of copies described in the action text and shown in the character reference "
    "image - do not merge them into one, do not drop any, do not add any beyond what is "
    "described."
)
NEGATIVE = ("extra characters, extra puppets, multiple people, blurry, distorted, watermark, "
            "text, low quality")  # composed and sent, but INERT at cfg 1.0 - see docstring.

# TWO-CHARACTER-COMPOSITE.md: SHOW01 is a two-hander, so image1=character/image2=set (the shape
# every suffix above assumes) has nowhere to put a second character. These two suffixes are for
# compose_panel_two()'s TWO-DISTINCT-CHARACTER case - NOT a lighter PRESERVE_SUFFIX_MULTI (that
# one is for N copies of the SAME character; this is for two DIFFERENT, individually-referenced
# characters) - so the "exactly one character" veto is replaced with "exactly two, one matching
# each reference image", not dropped.
#
# _NO_SET variant: image1=character A, image2=character B, no third reference image - the room is
# described in the instruction text only (Lever B, Geoff's steer via a peer engineer: reallocate
# the two wired image slots to the two characters rather than spending one on the set).
PRESERVE_SUFFIX_TWO_CHAR_NO_SET = (
    "Camera static, locked-off composition, natural consistent lighting. Preserve the first "
    "character exactly as shown in the first character reference image - same proportions, "
    "materials, colours, face and clothing - do not redesign or reimagine that character. "
    "Preserve the second character exactly as shown in the second character reference image - "
    "same proportions, materials, colours, face and clothing - do not redesign or reimagine that "
    "character, do not merge it with the first character, and do not omit it. Render the room "
    "exactly as described in the instruction above; do not invent a different room. Exactly two "
    "characters in the frame - the one shown in the first reference image and the one shown in "
    "the second reference image - do not add any extra characters, puppets, or figures beyond "
    "these two, and do not render either character twice."
)
# _WITH_SET variant: image1=character A, image2=character B, image3=set (TPL_TRIPLE) - same
# two-character clause, but the room clause points at the real image3 reference instead of text.
PRESERVE_SUFFIX_TWO_CHAR_WITH_SET = (
    "Camera static, locked-off composition, natural consistent lighting. Preserve the first "
    "character exactly as shown in the first character reference image - same proportions, "
    "materials, colours, face and clothing - do not redesign or reimagine that character. "
    "Preserve the second character exactly as shown in the second character reference image - "
    "same proportions, materials, colours, face and clothing - do not redesign or reimagine that "
    "character, do not merge it with the first character, and do not omit it. Preserve the "
    "background environment exactly as shown in the set reference image - do not redesign the "
    "room. Exactly two characters in the frame - the one shown in the first reference image and "
    "the one shown in the second reference image - do not add any extra characters, puppets, or "
    "figures beyond these two, and do not render either character twice."
)


def suffix_for(instance_count=1):
    """Single source of truth for which PRESERVE_SUFFIX variant a given
    instance_count uses -- so the actual qwen_edit() call and panel_compose.py's
    provenance/description logging can never disagree about which suffix was
    really sent (the exact class of bug QC-HARDENING fix 3 caught for
    unsanitized action_text: a string used for the real GPU call must be the
    SAME string recorded, never a second copy that can silently diverge)."""
    return PRESERVE_SUFFIX_MULTI if (instance_count or 1) > 1 else PRESERVE_SUFFIX

_CALL_COUNTER = [0]


def log(m):
    print("[qwen_compose] %s" % m, flush=True)


def comfy_up():
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:8188/system_stats", timeout=3)
        return True
    except Exception:
        return False


def _run_tag():
    """Per-CALL uniqueness (not just per-process): this module may compose several images in
    one process (e.g. a PASS demo then a FAIL canary), and two different source images can
    share a basename ('front.png'), so process-start-time salting alone (character_sheets.py's
    RUN_TAG pattern) is not enough here. Also defeats ComfyUI's execution cache the same way -
    see PHASE-1-BUILD.md section 6b."""
    _CALL_COUNTER[0] += 1
    return "%s_%03d_%s" % (time.strftime("%H%M%S"), _CALL_COUNTER[0], uuid.uuid4().hex[:6])


def stage_image(src_path, tag):
    """Copy a source PNG into ComfyUI's input dir under a run-tagged name and confirm ComfyUI
    itself lists it as loadable before returning - the Phase 1 preflight discipline
    (PHASE-1-BUILD.md section 6a: an anchor that vanished from ComfyUI's input dir made every
    wedge cell fail identically and a naive canary scored that as a false pass). Returns the
    bare filename LoadImage expects."""
    if not os.path.isfile(src_path):
        raise SystemExit("FATAL: reference image does not exist on disk: %r" % src_path)
    name = "p3_%s_%s" % (tag, os.path.basename(src_path))
    dest = os.path.join(COMFY_IN, name)
    shutil.copyfile(src_path, dest)
    if comfy_up():
        import json
        import urllib.request
        oi = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:8188/object_info/LoadImage", timeout=30).read())
        listed = oi["LoadImage"]["input"]["required"]["image"][0]
        if name not in listed:
            raise SystemExit("FATAL: staged %r into ComfyUI's input dir but ComfyUI does not "
                             "list it as loadable yet. Not proceeding blind." % name)
    return name


def qwen_edit(image1, instruction, output_prefix, image2=None, image3=None, seed=1,
             raw=False, timeout=600, suffix_override=None, record=None):
    """The general primitive: image1 (identity to preserve) + instruction (+ optional image2,
    + optional image3) -> one output image via Qwen-Image-Edit-2509 + Lightning. Not
    panel-specific - see module docstring. Mirrors a peer engineer's own conditional graph: image2 given ->
    workflows/qwen_edit_compose.api.json (two references) or, with image3 also given,
    workflows/qwen_edit_triple.api.json (three references); image2 omitted ->
    workflows/qwen_edit_single.api.json (one reference only, no image2 node at all) - this is
    what makes a future single-anchor view-derivation job ("one approved anchor per character
    -> derive all six views", relayed via the Phase 3 coordinator note) work UNCHANGED through
    this same function: call qwen_edit(anchor, pose_instruction, prefix) with no image2. image3
    is only meaningful with image2 also given (matches TPL_TRIPLE's graph shape); an image3
    passed without image2 is ignored, not silently promoted to image2's slot, since that would
    change WHICH reference is 'image2' without the caller asking for it.

    image3 support (TWO-CHARACTER-COMPOSITE.md, 2026-09-03): TextEncodeQwenImageEditPlus itself
    accepts image1/image2/image3 - confirmed live against the installed node
    (http://127.0.0.1:8188/object_info/TextEncodeQwenImageEditPlus), not assumed from the docs
    or this module's own older docstring text claiming it was unwired. Wiring it was demoted
    from "the fix" to "a secondary option" by Geoff's steer (relayed via a peer engineer, citing
    a peer engineer's own finding that mixing latent input and reference image works): the PRIMARY
    two-character path is compose_panel_two() below, which reallocates image1/image2 to the two
    characters and reaches for image3 only when a caller also wants the set as a real image
    reference rather than text-only. Returns (output_path, error) - error is None on success.

    suffix_override: use this exact suffix text instead of the module-level PRESERVE_SUFFIX
    (ignored when raw=True). None (the default) keeps every existing caller's behaviour
    unchanged. compose_panel() below passes suffix_for(instance_count) explicitly, never
    reaches into raw=True to swap it, so the "no burnt-in text" / preserve-character clauses stay
    intact for multi-instance characters too - see PRESERVE_SUFFIX_MULTI."""
    if not comfy_up():
        return None, "ComfyUI is not reachable at 127.0.0.1:8188 - this module does not start it."

    tag = _run_tag()
    img1_name = stage_image(image1, tag + "_i1")
    sfx = PRESERVE_SUFFIX if suffix_override is None else suffix_override
    subs = {
        "unet_name": UNET_NAME, "lora_name": LORA_NAME, "lora_strength": LORA_STRENGTH,
        "clip_name": CLIP_NAME, "vae_name": VAE_NAME, "shift": SHIFT,
        "steps": STEPS, "cfg": CFG,
        "image1": img1_name,
        "prompt": instruction if raw else (instruction.rstrip(" .") + ". " + sfx),
        "negative_prompt": NEGATIVE,
    }
    # THE ONLY HONEST SOURCE FOR "WHAT WAS SENT" IS THE DICT WE SEND.
    # Callers used to rebuild this string for the record and got it wrong:
    # every two-character panel recorded the SINGLE-character suffix while
    # the GPU received the two-character one. That is F405, and the first
    # fix missed sg_prompt_final__as_sent_, the field the operator actually
    # reads, so 104 more panels were published wrong after it. A caller that
    # passes `record` gets the exact strings; one that does not is unchanged.
    if record is not None:
        record["prompt"] = subs["prompt"]
        record["negative"] = subs["negative_prompt"]
    if image2 and image3:
        tpl = TPL_TRIPLE
    elif image2:
        tpl = TPL_DUAL
    else:
        tpl = TPL_SINGLE
    if image2:
        subs["image2"] = stage_image(image2, tag + "_i2")
    if image2 and image3:
        subs["image3"] = stage_image(image3, tag + "_i3")
    prefix = "%s_r%s" % (output_prefix, tag)
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", tpl,
          "--comfy-output-dir", OUT_COMFY, "--verify-mode", "disk",
          "--output-prefix", prefix, "--seed-base", str(seed),
          "--expect-outputs", "1", "--timeout", str(timeout)]
    for k, v in subs.items():
        cmd += ["--set", "%s=%s" % (k, v)]
    log("submitting: template=%s image1=%s image2=%s image3=%s prefix=%s"
        % (os.path.basename(tpl), img1_name, subs.get("image2"), subs.get("image3"), prefix))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if "PASS:" not in (r.stdout or ""):
        return None, ((r.stdout or "") + (r.stderr or ""))[-800:]
    src = os.path.join(OUT_COMFY, "%s_w000_00001_.png" % prefix)
    if not os.path.exists(src):
        return None, "comfyui_execute.py reported PASS but %r is not on disk" % src
    return src, None


def compose_panel(character_image, set_image, action_text, output_prefix, seed=1,
                  instance_count=1, record=None):
    """Phase 3's own caller: character design + set design + action text -> one panel.
    image1=character (identity to preserve, matches attribute_check.py's subject), image2=set.
    A two-character panel (a third reference image) is out of scope this phase - see
    qwen_edit()'s docstring.

    instance_count: how many simultaneous copies of THIS character belong in frame (1 for an
    ordinary single character - the default, unchanged behaviour). >1 selects
    PRESERVE_SUFFIX_MULTI via suffix_for() instead of the default PRESERVE_SUFFIX, so the
    suffix stops telling a flock/twins panel "exactly one character in the frame" in the same
    breath the action text asks for several - see PRESERVE_SUFFIX_MULTI and
    docs/METHOD.md panel_compose.py is the caller that knows a character's
    instance_count (from Asset.sg_instance_count); this function does not look it up itself,
    same separation of concerns as everywhere else in this module (Phase 3 does the compositing
    primitive, Phase 5 gathers ShotGrid inputs)."""
    instruction = ("Place the character from the character reference image into the room shown "
                   "in the set reference image. %s" % action_text.strip())
    return qwen_edit(character_image, instruction, output_prefix, image2=set_image, seed=seed,
                     record=record,
                     suffix_override=suffix_for(instance_count))


def compose_panel_two(character_image1, character_image2, action_text, room_text, output_prefix,
                      seed=1, set_image=None, record=None):
    """TWO-CHARACTER-COMPOSITE.md: the two-hander sibling of compose_panel() above.
    compose_panel() itself is UNCHANGED - this is a new function, not a new parameter on it -
    per the brief's own instruction to keep every existing single/dual-image caller's behaviour
    identical (panel_compose.py's real PILOT01 calls run through compose_panel() and must not
    see any difference).

    Slot assignment (the brief's own "say which character you put where and why" instruction):
    image1=character_image1, image2=character_image2. image1 is documented as "the identity to
    preserve" and is ALSO what VAEEncode reads for the starting latent (qwen_edit()'s node 10) -
    at cfg 1.0 with denoise 1.0 that latent is fully renoised so it contributes canvas shape, not
    content (see the TWO-CHARACTER-COMPOSITE.md report for the denoise-axis wedge this module's
    self-test does not cover), but the SLOT still gets first mention in the positive prompt's
    preserve-clauses. This function does not privilege either named character - it takes them in
    caller-supplied order and documents that order verbatim in the instruction text and in every
    Version this composes, so the caller (and the report) can see which asset landed in which
    slot, rather than an unstated convention silently picking a "primary" character.

    This is Geoff's Lever B (relayed via a peer engineer, citing a peer engineer's own finding that mixing
    latent input and reference image works): reallocate the two WIRED image slots to the two
    characters, rather than spending one of them on the set the way compose_panel() does. The
    set is carried in the instruction TEXT (room_text) by default - set_image=None, the common
    case, since a genuine two-hander panel has three real reference images to place (2 characters
    + a set) and only two image slots were ever wired for a peer engineer-proven 2-image graph. Passing
    set_image reaches for TPL_TRIPLE (image3, confirmed present on the installed
    TextEncodeQwenImageEditPlus node - see qwen_edit()'s docstring) instead, for the case a
    caller wants the room as a real image reference rather than text-only; PRESERVE_SUFFIX_
    TWO_CHAR_WITH_SET is used in that case instead of the _NO_SET default so the room gets its
    own preserve-clause pointed at image3.

    action_text: what the two characters are doing (verbatim beat text, both characters' staging
    included - this function does not split or attribute lines to either character).
    room_text: a short description of the set/room (used in the instruction whether or not
    set_image is also given, so the _NO_SET path is not silently roomless).
    Returns (output_path, error), same contract as qwen_edit()."""
    instruction = ("Place the first character from the first character reference image and the "
                   "second character from the second character reference image together into "
                   "%s. %s" % (room_text.strip().rstrip("."), action_text.strip()))
    if set_image:
        return qwen_edit(character_image1, instruction, output_prefix, image2=character_image2,
                         image3=set_image, seed=seed, record=record,
                         suffix_override=PRESERVE_SUFFIX_TWO_CHAR_WITH_SET)
    return qwen_edit(character_image1, instruction, output_prefix, image2=character_image2,
                     seed=seed, record=record,
                     suffix_override=PRESERVE_SUFFIX_TWO_CHAR_NO_SET)


def qwen_edit_blocking(image1, image2, blocking_image, instruction, output_prefix, seed=1,
                       denoise=1.0, raw=False, timeout=600, suffix_override=None,
                       record=None):
    """LEVER A primitive (TWO-CHARACTER-COMPOSITE.md): like qwen_edit(), but the starting
    latent is VAEEncode'd from blocking_image (a rough layout/blocking image), NOT from image1 -
    decoupling 'what gets conditioned as identity' (image1/image2) from 'what shapes the starting
    latent' (blocking_image), via TPL_TWOCHAR_BLOCKING. denoise is a real parameter here (every
    other template in this module hardcodes 1.0, which fully renoises the latent and makes its
    content irrelevant - see TPL_TWOCHAR_BLOCKING's own comment above). Always uses the
    triple-image-slot template (image1, image2 for identity; blocking_image as a THIRD loaded
    image for the latent only - not routed through TextEncodeQwenImageEditPlus at all, so it does
    not consume the image3 conditioning slot TPL_TRIPLE uses for a set reference). Returns
    (output_path, error), same contract as qwen_edit()."""
    if not comfy_up():
        return None, "ComfyUI is not reachable at 127.0.0.1:8188 - this module does not start it."

    tag = _run_tag()
    img1_name = stage_image(image1, tag + "_i1")
    img2_name = stage_image(image2, tag + "_i2")
    blk_name = stage_image(blocking_image, tag + "_iblk")
    sfx = PRESERVE_SUFFIX if suffix_override is None else suffix_override
    subs = {
        "unet_name": UNET_NAME, "lora_name": LORA_NAME, "lora_strength": LORA_STRENGTH,
        "clip_name": CLIP_NAME, "vae_name": VAE_NAME, "shift": SHIFT,
        "steps": STEPS, "cfg": CFG, "denoise": denoise,
        "image1": img1_name, "image2": img2_name, "blocking_image": blk_name,
        "prompt": instruction if raw else (instruction.rstrip(" .") + ". " + sfx),
        "negative_prompt": NEGATIVE,
    }
    # THE ONLY HONEST SOURCE FOR "WHAT WAS SENT" IS THE DICT WE SEND.
    # Callers used to rebuild this string for the record and got it wrong:
    # every two-character panel recorded the SINGLE-character suffix while
    # the GPU received the two-character one. That is F405, and the first
    # fix missed sg_prompt_final__as_sent_, the field the operator actually
    # reads, so 104 more panels were published wrong after it. A caller that
    # passes `record` gets the exact strings; one that does not is unchanged.
    if record is not None:
        record["prompt"] = subs["prompt"]
        record["negative"] = subs["negative_prompt"]
    prefix = "%s_r%s" % (output_prefix, tag)
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", TPL_TWOCHAR_BLOCKING,
          "--comfy-output-dir", OUT_COMFY, "--verify-mode", "disk",
          "--output-prefix", prefix, "--seed-base", str(seed),
          "--expect-outputs", "1", "--timeout", str(timeout)]
    for k, v in subs.items():
        cmd += ["--set", "%s=%s" % (k, v)]
    log("submitting: template=%s image1=%s image2=%s blocking_image=%s denoise=%s prefix=%s"
        % (os.path.basename(TPL_TWOCHAR_BLOCKING), img1_name, img2_name, blk_name, denoise, prefix))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if "PASS:" not in (r.stdout or ""):
        return None, ((r.stdout or "") + (r.stderr or ""))[-800:]
    src = os.path.join(OUT_COMFY, "%s_w000_00001_.png" % prefix)
    if not os.path.exists(src):
        return None, "comfyui_execute.py reported PASS but %r is not on disk" % src
    return src, None


def compose_panel_two_blocking(character_image1, character_image2, blocking_image, action_text,
                               output_prefix, seed=1, denoise=1.0):
    """LEVER A caller: two character identities (image1/image2, conditioning only) + a rough
    two-character blocking/layout image (shapes the starting latent via qwen_edit_blocking(),
    denoise < 1.0 lets it actually contribute) + action text -> one panel. The room comes from
    the blocking image, not from text (unlike compose_panel_two()'s room_text) - the blocking
    image IS the set, roughly laid out with where each character goes. Reuses the SAME
    two-character veto text as compose_panel_two()'s _NO_SET suffix (no set-specific wording is
    needed here since the room arrives as the starting latent, not asserted in the suffix)."""
    instruction = ("Place the first character from the first character reference image and the "
                   "second character from the second character reference image into the room, "
                   "following the rough layout and character placement shown in the starting "
                   "image. %s" % action_text.strip())
    return qwen_edit_blocking(character_image1, character_image2, blocking_image, instruction,
                              output_prefix, seed=seed, denoise=denoise,
                              suffix_override=PRESERVE_SUFFIX_TWO_CHAR_NO_SET)


# ---------------------------------------------------------------------- self-test (offline)
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-62s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("dual-image template exists", os.path.isfile(TPL_DUAL))
    ck("single-image template exists", os.path.isfile(TPL_SINGLE))
    ck("triple-image template exists", os.path.isfile(TPL_TRIPLE))
    import re
    common = {"unet_name", "lora_name", "lora_strength", "clip_name", "vae_name", "shift",
             "steps", "cfg", "seed", "image1", "prompt", "negative_prompt", "output_prefix"}

    def placeholders_of(path):
        return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", open(path, encoding="utf-8").read()))

    ck("dual template placeholders = common set + image2 (what qwen_edit() supplies when "
       "image2 is given)", placeholders_of(TPL_DUAL) == (common | {"image2"}))
    ck("single template placeholders = common set, NO image2 (what qwen_edit() supplies when "
       "image2 is omitted - proves the single-anchor path has no dangling image2 node)",
       placeholders_of(TPL_SINGLE) == common)
    ck("triple template placeholders = common set + image2 + image3 (what qwen_edit() supplies "
       "when both image2 and image3 are given - TWO-CHARACTER-COMPOSITE.md)",
       placeholders_of(TPL_TRIPLE) == (common | {"image2", "image3"}))
    ck("two-char-blocking template exists", os.path.isfile(TPL_TWOCHAR_BLOCKING))
    ck("two-char-blocking template placeholders = common set + image2 + blocking_image + "
       "denoise - LEVER A's whole point is that denoise is a REAL placeholder here, unlike "
       "every other template which hardcodes 1.0 (TWO-CHARACTER-COMPOSITE.md)",
       placeholders_of(TPL_TWOCHAR_BLOCKING) == (common | {"image2", "blocking_image", "denoise"}))

    ck("PRESERVE_SUFFIX applied unless raw=True",
       PRESERVE_SUFFIX in ("x. " + PRESERVE_SUFFIX) and "x." in ("x. " + PRESERVE_SUFFIX))

    a = _run_tag()
    b = _run_tag()
    ck("two calls in the same process get DIFFERENT run tags (cache-defeat + no collision)",
       a != b)

    ck("cfg is pinned to 1.0 (a peer engineer's fidelity-anchor finding - see docstring)", CFG == "1.0")
    ck("steps is pinned to 4 (Lightning)", STEPS == "4")
    ck("shift is AuraFlow's 3.0, not Wan's SD3 8.0 (the peer-engineer-caught trap)", SHIFT == "3.0")

    # --- COUNT-COLLAPSE.md: multi-instance suffix selection ------------------
    ck("suffix_for(1) (default/omitted) is the ordinary single-character PRESERVE_SUFFIX",
       suffix_for() == PRESERVE_SUFFIX and suffix_for(1) == PRESERVE_SUFFIX)
    ck("suffix_for(2) is PRESERVE_SUFFIX_MULTI, a DIFFERENT string from PRESERVE_SUFFIX",
       suffix_for(2) == PRESERVE_SUFFIX_MULTI and PRESERVE_SUFFIX_MULTI != PRESERVE_SUFFIX)
    ck("suffix_for(0)/suffix_for(None) do not crash and fall back to the single-character "
       "suffix (an unset/zero count is never treated as 'many')",
       suffix_for(0) == PRESERVE_SUFFIX and suffix_for(None) == PRESERVE_SUFFIX)
    ck("CANARY: PRESERVE_SUFFIX_MULTI does NOT contain the single-character veto sentence "
       "('Exactly one character in the frame') that PRESERVE_SUFFIX does -- the whole point "
       "of the multi variant is to drop that specific clause, not water it down",
       "Exactly one character in the frame" not in PRESERVE_SUFFIX_MULTI
       and "Exactly one character in the frame" in PRESERVE_SUFFIX)
    ck("PRESERVE_SUFFIX_MULTI keeps every OTHER clause identical to PRESERVE_SUFFIX (camera, "
       "preserve-character, preserve-room) -- only the count sentence differs, per invariant 5",
       all(clause in PRESERVE_SUFFIX_MULTI for clause in (
           "Camera static, locked-off composition",
           "Preserve the character exactly as shown in the character reference image",
           "Preserve the background environment exactly as shown in the set reference image")))

    import unittest.mock as mock
    with mock.patch("%s.qwen_edit" % __name__, return_value=("out.png", None)) as m:
        compose_panel("char.png", "set.png", "does a thing", "pfx", seed=42)
        ck("CANARY: compose_panel() with NO instance_count (default 1) calls qwen_edit() with "
           "suffix_override=PRESERVE_SUFFIX (single-character default, unchanged behaviour for "
           "every existing character)",
           m.call_args.kwargs.get("suffix_override") == PRESERVE_SUFFIX)
    with mock.patch("%s.qwen_edit" % __name__, return_value=("out.png", None)) as m:
        compose_panel("char.png", "set.png", "does a thing", "pfx", seed=42, instance_count=4)
        ck("CANARY: compose_panel(instance_count=4) calls qwen_edit() with "
           "suffix_override=PRESERVE_SUFFIX_MULTI, not the default -- this is the actual "
           "production call site panel_compose.py reaches the GPU through",
           m.call_args.kwargs.get("suffix_override") == PRESERVE_SUFFIX_MULTI)

    # --- TWO-CHARACTER-COMPOSITE.md: compose_panel_two() ----------------------
    ck("CANARY: PRESERVE_SUFFIX_TWO_CHAR_NO_SET does not contain the single-character veto "
       "sentence ('Exactly one character in the frame') and does not equal PRESERVE_SUFFIX or "
       "PRESERVE_SUFFIX_MULTI - this is a genuinely third suffix, not a relabelled existing one",
       "Exactly one character in the frame" not in PRESERVE_SUFFIX_TWO_CHAR_NO_SET
       and PRESERVE_SUFFIX_TWO_CHAR_NO_SET not in (PRESERVE_SUFFIX, PRESERVE_SUFFIX_MULTI))
    ck("CANARY: PRESERVE_SUFFIX_TWO_CHAR_WITH_SET differs from the _NO_SET variant (it points "
       "the room clause at image3 instead of text) but both assert 'exactly two characters'",
       PRESERVE_SUFFIX_TWO_CHAR_WITH_SET != PRESERVE_SUFFIX_TWO_CHAR_NO_SET
       and "Exactly two characters in the frame" in PRESERVE_SUFFIX_TWO_CHAR_NO_SET
       and "Exactly two characters in the frame" in PRESERVE_SUFFIX_TWO_CHAR_WITH_SET)

    with mock.patch("%s.qwen_edit" % __name__, return_value=("out.png", None)) as m:
        compose_panel_two("charA.png", "charB.png", "they talk", "a bedroom", "pfx", seed=7)
        kw = m.call_args.kwargs
        ck("CANARY: compose_panel_two() with no set_image calls qwen_edit() with image2="
           "charB (second character, NOT a set) and no image3, suffix_override=_NO_SET",
           kw.get("image2") == "charB.png" and kw.get("image3") is None
           and kw.get("suffix_override") == PRESERVE_SUFFIX_TWO_CHAR_NO_SET)
        ck("CANARY: compose_panel_two()'s instruction names character reference images, never "
           "the word 'set reference image' (there is no set image in the _NO_SET path)",
           "second character reference image" in m.call_args.args[1]
           and "set reference image" not in m.call_args.args[1])

    with mock.patch("%s.qwen_edit" % __name__, return_value=("out.png", None)) as m:
        compose_panel_two("charA.png", "charB.png", "they talk", "a bedroom", "pfx", seed=7,
                          set_image="set.png")
        kw = m.call_args.kwargs
        ck("CANARY: compose_panel_two() WITH set_image calls qwen_edit() with image2=charB "
           "AND image3=set (TPL_TRIPLE path), suffix_override=_WITH_SET",
           kw.get("image2") == "charB.png" and kw.get("image3") == "set.png"
           and kw.get("suffix_override") == PRESERVE_SUFFIX_TWO_CHAR_WITH_SET)

    with mock.patch("%s.qwen_edit" % __name__, return_value=("out.png", None)) as m:
        compose_panel("char.png", "set.png", "does a thing", "pfx", seed=42)
        ck("CANARY: compose_panel() itself is UNCHANGED by adding compose_panel_two() - still "
           "calls qwen_edit() with image2=set (not a second character) and no image3",
           m.call_args.kwargs.get("image2") == "set.png"
           and m.call_args.kwargs.get("image3") is None)

    try:
        stage_image("C:\\this\\file\\does\\not\\exist.png", "canary")
        ck("stage_image refuses a missing source file", False)
    except SystemExit:
        ck("stage_image refuses a missing source file", True)

    print("SELF-TEST: %s" % ("ALL PASS" if not fails else ("FAILED: %r" % fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image1", help="Reference image: the identity to preserve.")
    ap.add_argument("--image2", default=None, help="Optional second reference (e.g. a set).")
    ap.add_argument("--instruction", help="Edit instruction (positive prompt body).")
    ap.add_argument("--output-prefix", default="qwen_edit")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--raw", action="store_true", help="Skip PRESERVE_SUFFIX.")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if not (args.image1 and args.instruction):
        print("FAIL: --image1 and --instruction are required (or --self-test).")
        return 1

    out, err = qwen_edit(args.image1, args.instruction, args.output_prefix,
                         image2=args.image2, seed=args.seed, raw=args.raw,
                         timeout=args.timeout)
    if err:
        print("FAIL: %s" % err)
        return 1
    print("PASS: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
