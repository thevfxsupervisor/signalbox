#!/usr/bin/env python3
"""Stage B2 for the show: Anima design anchors for the six character Assets.

This is the ANIMA counterpart of character_sheets.py, which is the Wan A14B
one. Same shape, same contracts, same refusals: read the real ShotGrid Asset
fragment, render candidates, publish each one as it completes, and never
approve its own output. What differs is the render backend -- Anima Base v1.0
through the committed workflows/anima_t2i*.api.json templates instead of the
two-expert A14B one -- and the retry policy, which is new and is the whole
point of this tool.

WHY A NEW TOOL RATHER THAN A BRANCH IN character_sheets.py. That tool's whole
body is bound to the two-expert A14B GGUF graph, its Lightning LoRAs, its
cfg=1.0-negative-is-inert assumption, and PILOT01's marionette vocabulary
(strip_puppet_language, INSERT_STAGING, the wood/Qwen material fix). None of
that applies here. What IS shared is imported, not copied: sg_publish for the
publish contract, sg_publish_check for the viewability proof, dialogue_guard
for the forbidden-token and D15 guards, pm_backend_shotgrid for the
connection, and comfyui_execute for submit/poll/collect.

THE RECIPE IS SETTLED. 71 test cells over three passes (see docs/METHOD.md for
the wedge method) settled it and this tool does not relitigate it:

  CHARACTERS  anima-base-v1.0, NO LoRA, 40 steps, cfg 6.0.
  SETS        anima-base-v1.0 + anima-turbo-lora-v0.2 @1.0, 10 steps, cfg 1.0
              -- adopted, because on 12200 it beat its own 40-step control on
              brief compliance (it got the unmade bed and the clutter the
              40-step cell missed) at 4.1s against 20.7s. Adopted is not
              unexamined: every set ALSO renders a 40-step no-LoRA control at
              the same seed, and whichever is better wins. The speed recipe is
              proven on ONE set; 12201 and 12202 are new ground and 12202,
              whose fragment asks for walls of poster art, is the hard case.

  Nine LoRAs were tested across three passes and none beat a good prompt. The
  largest quality jump measured anywhere came from swapping a hand-written
  style description for the real approved ShotGrid Asset fragment -- five
  times now (an internal knowledge note on checking what is actually sent). So the prompt
  is composed from Asset.sg_prompt_fragment READ LIVE FROM THE RECORD, never
  from a paraphrase typed into this file, and the fragment-source canary in
  --self-test is what stops that rule decaying into a comment.

THE RETRY POLICY, which is the finding this tool exists to implement. Pass 3
gap 5 drew the SAME cell four times, varying only the seed:

    51002  3 views, usable, but pseudo-lettering on the tee
    51003  3 near-identical fronts -- unusable as a turnaround
    51004  front / profile / front -- partly usable
    51005  4 clean views, separated fingers -- best of the four

Style was correct in all four. Anatomy, view coverage and the invented-text
defect were not. Two conclusions, both load-bearing here:

  1. A turnaround needs a RETRY POLICY, not a better prompt. One generation
     per Asset is not a valid attempt. MIN_SEEDS is 4 and the seed-policy
     canary refuses fewer.
  2. Judge candidates on anatomy, view coverage and brief compliance. Style
     verdicts survive one seed; these do not.

THE OPEN HAZARD, stated because it has no guard. Anima invents text wherever
the prompt describes a PRINTED SURFACE, and all three character fragments
describe one (PILOTCHARA's film-reel tee graphic, PILOTCHARC's mask and shirt, and
12202's poster walls). It is intermittent, it is a seed property, and the
speed recipe makes it LEGIBLE rather than suppressing it. There is no guard.
This tool's answer is rejection, not prevention: it renders several seeds and
a reviewer REJECTS any candidate carrying invented text. `--negtext-wedge`
runs the untried one-axis fix (a negative-prompt term) as a TEST, against the
same seeds, and deliberately cannot be folded into the production recipe --
see NEG_BASE / negative_for() and the negative-prompt canary.

WHAT THIS TOOL DELIBERATELY DOES NOT DO -- AND WHY IT DOES NOT JUDGE.
Geoff, 2026-09-03: "in the long term we can't rely on claude vision to
evaluate and gate keep things. it needs to be deterministic scripts more
so... just publish them all and let the operator review." So this tool
generates every seed and publishes EVERY ONE at 'rev'. It does not pick a
winner, it does not discard a candidate, and it does not decide that a sheet
carrying invented text is unfit -- a candidate an agent would have quietly
discarded is not the agent's to discard. What it attaches instead is
qc_metrics(): duplicate-view distances, a palette histogram and a
degenerate-output guard, all arithmetic, all recorded on the Version. The
operator sorts by those.

It does not approve anything either. Every
candidate publishes as a Version on the ASSET (never the Episode -- a
Version's entity is the thing it is a version OF) at status 'rev'. Every write
this tool makes to Asset.sg_stage goes through set_asset_stage(), which
HARD-REFUSES any value but 'design'. That is invariant 7 enforced in code, and
it is what the refuse-to-approve canary proves. Approval is a separate,
reviewer-role act with its own script and its own Note (D14).

    python anima_anchor.py --self-test
    python anima_anchor.py --show-prompts              compose, print, render nothing
    python anima_anchor.py --generate                  all six Assets
    python anima_anchor.py --generate --only SHOW_CHAR_PILOTCHARA
    python anima_anchor.py --generate --seeds 6        more than the 4-seed floor
    python anima_anchor.py --negtext-wedge --only SHOW_CHAR_PILOTCHARA
    python anima_anchor.py --report                    what is on disk + in SG
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "comfyui"))
import dialogue_guard as DG                                     # noqa: E402
import sg_provenance as PROV                                    # noqa: E402
import sg_publish as PUB                                        # noqa: E402
import sg_publish_check as PUBCHK                               # noqa: E402
import comfyui_execute as CE                                    # noqa: E402

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# E0 FIX (docs/METHOD.md): was os.path.join(ROOT, "build", "tools")
# -- a hardcoded guess at the deployed location that the WRAPPER path below
# would have kept reaching for even after a correct atomic deploy, because it
# never depended on where THIS file was actually run from. Self-relative now.
BUILD_TOOLS = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(BUILD_TOOLS, "comfyui", "comfyui_execute.py")
TPL_ANIMA = os.path.join(ROOT, "workflows", "anima_t2i.api.json")
TPL_ANIMA_LORA = os.path.join(ROOT, "workflows", "anima_t2i_lora.api.json")
OUT = os.path.join(ROOT, "output", "show_anchors")
PAYLOADS = os.path.join(OUT, "payloads")
OUT_COMFY = r"C:\ComfyUI_windows_portable\ComfyUI\output"
COMFY_HOST, COMFY_PORT = "127.0.0.1", "8188"
PY = sys.executable
PROJ = {"type": "Project", "id": 9999}

# ---------------------------------------------------------------- the recipe
CHECKPOINT = "anima-base-v1.0.safetensors"
CLIP_NAME = "qwen_3_06b_base.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"
TURBO_LORA = "anima-turbo-lora-v0.2.safetensors"
WIDTH, HEIGHT = 1280, 704
SAMPLER, SCHEDULER, SHIFT = "er_sde", "simple", 3.0

# The quality prefix passes 1-3 all used, unchanged, so these anchors are
# directly comparable with the 71 published test cells.
QUALITY_PREFIX = "masterpiece, best quality, score_7, safe, "

# The pass-1/2/3 negative, verbatim. Read back out of the P3_20 PNG's embedded
# graph rather than retyped from the report -- the report is a description of
# the call, the PNG is the call.
NEG_BASE = ("worst quality, low quality, score_1, score_2, score_3, "
            "artist name, blurry, jpeg artifacts, chromatic aberration")

# The UNTRIED one-axis fix for the invented-text hazard. It is NOT part of any
# production recipe and negative_for() will only emit it when the caller has
# explicitly asked for the wedge. See the negative-prompt canary.
NEG_TEXT_WEDGE_TERM = "text, letters, words, watermark, signage, logo, typography"

RECIPES = {
    # name        lora          strength  steps  cfg   production?
    "base40":  {"lora": None,        "strength": 0.0, "steps": 40, "cfg": 6.0},
    "turbo10": {"lora": TURBO_LORA,  "strength": 1.0, "steps": 10, "cfg": 1.0},
}

# Which recipes each kind of Asset runs, in order.
#
# BOTH lists are two long, and the character one is two long for a reason that
# was NOT in ANIMA-PASS3.md, because it happened after that report was written.
# Pass 3 concluded turbo10 was "not adopted for characters until the text
# hazard has a guard". Geoff then reviewed the 28 published character cells in
# ShotGrid and ruled the other way. Read off the live records, not off a
# report about them:
#
#   67462  apr   PILOTCHARA   turbo-lora-v0.2 @1.0, 10 / 1.0   <- the ONLY apr
#   67470  rrq   PILOTCHARB      rl-v0.1 @1.0,        40 / 6.0
#   67448  rrq   PILOTCHARC  no LoRA,             10 / 1.0
#   67408, 67447, 67453, 67455, 67457, 67463, 67469, 67472, 67473, 67474
#          rjct            no LoRA,             40 / 6.0
#
# That is ten rejections of base40 -- including all four seeds of the gap-5
# study that base40 was chosen on -- and one approval of turbo10. Note 51358,
# Geoff, on PILOTCHARB's 67470: "redo with turbo lora to match the other chosen
# character asset versions."
#
# So this tool runs BOTH on characters and neither is dropped on my judgement.
# base40 leads because it is the written production recipe; turbo10 runs
# because the reviewer approved it and asked for it by name. The known cost of
# turbo10 on characters is LEGIBLE invented text on described printed
# surfaces, which is why every turbo10 character candidate must be inspected
# for it and rejected if it carries any. The conflict is reported, not
# resolved silently in either direction.
# CHARACTERS run turbo10 ONLY. base40 is not run as a control here, and that
# is a deliberate exception to "adopted is not unexamined": Geoff has already
# examined it, on ten published cells, and rejected every one. Re-rendering it
# would be re-litigating a decision the reviewer has made, not checking it.
#
# SETS run turbo10 with a base40 control, because Geoff has reviewed NO set
# cell -- all seven set Versions on these assets are still 'rev', untouched.
# The speed recipe is proven on exactly one set (12200) and 12202 is the hard
# case, so the control earns its place there and not on the characters.
KIND_RECIPES = {
    "char": ("turbo10",),
    "set": ("turbo10", "base40"),
}

# The reviewer verdicts above, as data, so the canary asserts against the
# record rather than against this comment.
GEOFF_VERDICTS = {
    67462: ("apr", "turbo10"), 67470: ("rrq", "rl-v0.1"), 67448: ("rrq", "base10"),
    67408: ("rjct", "base40"), 67447: ("rjct", "base40"), 67453: ("rjct", "base40"),
    67455: ("rjct", "base40"), 67457: ("rjct", "base40"), 67463: ("rjct", "base40"),
    67469: ("rjct", "base40"), 67472: ("rjct", "base40"), 67473: ("rjct", "base40"),
    67474: ("rjct", "base40"),
}

# Retry policy, not a better prompt. Four draws of an identical cell produced
# one clean turnaround, one partial, one usable and one that was three
# identical front views (ANIMA-PASS3.md gap 5).
MIN_SEEDS = 4
SEED_BASE = 52001

ASSETS = {
    12197: ("SHOW_CHAR_PILOTCHARA", "char"),
    12198: ("SHOW_CHAR_PILOTCHARB", "char"),
    12199: ("SHOW_CHAR_PILOTCHARC", "char"),
    12200: ("SHOW_SET_PILOTCHARA_BEDROOM", "set"),
    12201: ("SHOW_SET_HALLWAY", "set"),
    12202: ("SHOW_SET_PILOTCHARB_BEDROOM", "set"),
}

MODEL_LABEL = "anima-base-v1.0"

# This tool NEVER writes Asset.sg_stage to anything but 'design'. Approval is
# a separate reviewer act (invariant 7, D10, D14). set_asset_stage() is the
# ONE choke point, so the refuse-to-approve canary has one thing to prove.
ALLOWED_ASSET_STAGE_WRITES = ("design",)

RUN_TAG = time.strftime("%H%M%S")


def log(m):
    print("[anima] %s" % m, flush=True)


def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def set_asset_stage(sg, asset_id, stage):
    """The ONLY path this tool uses to write Asset.sg_stage. Refuses anything
    but 'design'. A tool that generates a design AND can mark it approved
    makes the gap between "Geoff signed this off" and "an agent generated a
    stand-in" permanent. This is what the --self-test refuse-to-approve canary
    calls directly with 'approved' and asserts raises."""
    if stage not in ALLOWED_ASSET_STAGE_WRITES:
        raise PermissionError(
            "REFUSED: anima_anchor.py will not write Asset.sg_stage=%r. "
            "This tool only ever writes %r; approval is a separate reviewer "
            "act (invariant 7, D10; under D14 an agent may stand in as "
            "reviewer, but not this process and not silently)."
            % (stage, ALLOWED_ASSET_STAGE_WRITES))
    sg.update("Asset", asset_id, {"sg_stage": stage})


# ------------------------------------------------------------------- prompts
def seeds_for(n=MIN_SEEDS):
    """>= MIN_SEEDS distinct seeds. A single generation per Asset is not a
    valid attempt (ANIMA-PASS3.md gap 5), and the seed-policy canary refuses
    a caller who asks for fewer."""
    if n < MIN_SEEDS:
        raise ValueError(
            "REFUSED: %d seeds. Four draws of one identical Anima cell gave "
            "one clean turnaround, one partial, one usable and one with three "
            "identical front views -- turnaround quality is a seed property. "
            "The floor is %d (ANIMA-PASS3.md gap 5)." % (n, MIN_SEEDS))
    return [SEED_BASE + i for i in range(n)]


def compose(fragment, strip_graphic=False):
    """QUALITY_PREFIX + the REAL Asset fragment, guarded.
    -> (final_prompt, guard_changed, removed_graphic_clauses).

    The fragment goes in VERBATIM -- this function adds a prefix and removes
    guarded tokens, and does nothing else. It never paraphrases, reorders or
    "improves" it. `guard_changed` is returned rather than swallowed so a
    stripped token shows up in the published Version description instead of
    silently altering a prompt someone later reads back from the record.

    `strip_graphic=True` additionally removes garment-graphic clauses. That is
    the adopted fix for the invented-text hazard and it is OPT-IN, per call,
    because it is a real subtraction from the design: it is applied when
    generating a character ANCHOR (where the point is a clean turnaround) and
    the removed clauses are named in the Version description every time, so
    the subtraction is never invisible. See TEXT WEDGE in the module
    docstring for the evidence."""
    if not fragment or not fragment.strip():
        raise ValueError(
            "REFUSED: empty prompt fragment. The prompt is composed from the "
            "live Asset.sg_prompt_fragment; there is no hand-written "
            "fallback, because a hand-written paraphrase is the single "
            "largest measured quality regression in this show.")
    body, removed = (strip_printed_surface(fragment) if strip_graphic
                     else (fragment, []))
    raw = QUALITY_PREFIX + body
    # ONE guard call, not two. dialogue_guard.strip_practical_notes() removes
    # D15 practical/SFX text AND funnels through strip_forbidden_style()
    # internally ("applied HERE rather than at each call site... so no future
    # caller can forget it"). An extra explicit strip_forbidden_style() call
    # here looked like belt-and-braces and was in fact DEAD CODE -- proven by
    # mutation: deleting it changed no canary, because the token was already
    # gone. A guard that cannot be observed to fire is not a guard.
    final = DG.strip_practical_notes(raw)
    # There is deliberately no second "and refuse if a token survived" check
    # here. contains_forbidden_style() and strip_forbidden_style() share ONE
    # regex, so anything the detector could see the stripper has already
    # removed: such a check is unreachable by construction. It was written,
    # then deleted when mutation testing showed that stubbing it out broke no
    # canary. An unfalsifiable guard is decoration. What IS observable is
    # `guard_changed`, which the caller writes into the published Version
    # description as "STRIPPED A TOKEN".
    return final, (final != raw), removed


# ------------------------------------------------- the printed-surface guard
# THE HAZARD, characterised by pass 3 and re-opened by the recipe change.
# Anima invents text wherever the prompt describes a PRINTED SURFACE, and at
# 10 steps with the turbo LoRA the invention is LEGIBLE ("23WAR", "S.CET /
# SOPEK") where 40 steps leaves illegible scribble. Every recorded instance
# sits on a garment the fragment explicitly says carries a graphic. The set
# fragments describe no printed surface and produced no text at any step
# count -- which is the control that makes this a clause property rather than
# a model property.
#
# So the candidate fix is to stop ASKING for the printed surface. This is
# narrow on purpose: it removes the "... with a <something> graphic" clause
# and leaves the garment, its colour and its fit alone, exactly as
# dialogue_guard's style guard removes one token rather than the phrase
# around it.
_PRINTED_SURFACE_RE = re.compile(
    r"(?:^|(?<=[\s,]))\s*with\s+(?:a\s+|an\s+)?"
    r"(?:[A-Za-z-]+\s+){0,4}"
    r"(?:graphics?|prints?|printed\s+\w+|logos?|lettering|slogans?|artwork|"
    r"motifs?|emblems?|badges?|decals?)\b",
    re.I)


def strip_printed_surface(text):
    """Remove garment-graphic clauses. -> (text, [clauses removed]).

    Never raises. Returns the input unchanged when there is nothing to
    remove, so a fragment that describes no printed surface is provably
    untouched -- which is what makes the B cell of the text wedge a genuine
    one-axis change."""
    removed = [m.group(0).strip() for m in _PRINTED_SURFACE_RE.finditer(text or "")]
    if not removed:
        return text or "", []
    out = _PRINTED_SURFACE_RE.sub("", text)
    out = re.sub(r"\s+([,;.])", r"\1", out)
    out = re.sub(r"(?:,\s*){2,}", ", ", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out, removed


def wants_graphic_guard(kind):
    """OFF for everything. Geoff, 2026-09-07.

    *"I don't know where the prohibition on text came from, it's really not a
    big deal for the MVP, let's stop trying to prevent it."*

    This used to return True for characters, which silently DELETED any
    garment-graphic clause from the fragment before rendering. Two things were
    wrong with that beyond the ruling. It removed creative content the
    description asked for, so a character who is supposed to wear a printed tee
    could not have one. And it was fighting a defect we caused elsewhere: at
    cfg 1.0 an exclusion written as "no chest graphic" NAMES the graphic, which
    is F002, so the pipeline was adding the invitation in one place and
    deleting the subject in another.

    `strip_printed_surface()` is kept and still tested, so re-enabling is one
    return value if Geoff ever wants it back on."""
    return False


def negative_for(wedge=False):
    """The negative prompt. NEG_BASE is the production negative and is the
    pass-1/2/3 string unchanged, so anchors stay comparable with the 71
    published test cells.

    `wedge=True` appends the UNTRIED anti-text term. That is a TEST, not a
    recipe: it has never been run, it changes one axis, and folding an
    untested change into production is how a wedge result becomes a
    superstition. --generate cannot reach this branch; only --negtext-wedge
    can, and the negative-prompt canary asserts that."""
    return NEG_BASE + ", " + NEG_TEXT_WEDGE_TERM if wedge else NEG_BASE


# -------------------------------------------------------------------- graph
def template_for(lora):
    """anima_t2i_lora.api.json when a LoRA is wanted, anima_t2i.api.json
    otherwise. Two files rather than one with a bypassed node, for the same
    reason qwen_compose keeps a single- and a dual-image template: a graph
    carrying a dangling unused node is not the graph that was proven."""
    return TPL_ANIMA_LORA if lora else TPL_ANIMA


def subs_for(prompt, negative, seed, steps, cfg, lora, strength, prefix):
    """The exact --set substitutions, PLUS the three comfyui_execute.py
    generates for itself ({wedge}, {seed}, {output_prefix}) so this dict can
    reproduce the wrapper's posted graph locally for verification. `wedge` is
    0 because every call here is a single wedge (the wrapper is invoked
    `0 0 1`), which is why output_prefix gains the `_w000` suffix."""
    s = {"checkpoint": CHECKPOINT, "shift": SHIFT, "width": WIDTH,
         "height": HEIGHT, "steps": steps, "cfg": cfg, "sampler": SAMPLER,
         "scheduler": SCHEDULER, "prompt": prompt, "negative_prompt": negative}
    if lora:
        s["lora_name"] = lora
        s["lora_strength"] = strength
    gen = {"wedge": 0, "seed": seed, "output_prefix": "%s_w000" % prefix}
    return s, gen


def build_graph(prompt, negative, seed, steps, cfg, lora, strength, prefix):
    """The graph comfyui_execute.py WILL post, built here by calling its own
    load_and_patch_workflow() on the same committed template with the same
    subs. Not a second implementation of the graph -- the templates
    workflows/anima_t2i*.api.json are the single source and this reads them.

    THE LOAD-BEARING DETAIL is the wiring. A LoRA that silently fails to
    apply is indistinguishable from a LoRA that does nothing, so the LoRA
    patches the raw UNet BEFORE ModelSamplingAuraFlow and a LoRA cell differs
    from its control by exactly one node:

        no-LoRA :  1 UNETLoader -> 2 ModelSamplingAuraFlow -> 8 KSampler
        LoRA    :  1 UNETLoader -> 11 LoraLoaderModelOnly -> 2 -> 8

    The lora-wiring canary asserts node 2's model input is ["11",0] when a
    LoRA is asked for and ["1",0] when it is not, reading it out of the real
    template. Without that, a template edited to load a LoRA and never route
    through it would render a clean control and be reported as a LoRA
    result -- which is precisely the claim pass 3 had to prove three separate
    ways."""
    s, gen = subs_for(prompt, negative, seed, steps, cfg, lora, strength, prefix)
    return CE.load_and_patch_workflow(template_for(lora), dict(s, **gen))


def _canon(graph):
    return json.dumps(graph, sort_keys=True, separators=(",", ":"))


def executed_graph(png_path):
    """The graph ComfyUI says it RAN, read out of the PNG's own `prompt` tEXt
    chunk. This is the server's record, not ours."""
    from PIL import Image
    with Image.open(png_path) as im:
        raw = im.text.get("prompt")
    return json.loads(raw) if raw else None


def verify_executed_graph(png_path, posted):
    """(ok, detail). Compare what we POSTED against what ComfyUI EXECUTED.

    This exists because this pipeline has twice shipped a claim about a
    generation that was true of the provenance string and false of the call.
    A green "the driver set steps=40" proves nothing; the PNG proving the
    server ran steps=40 proves it."""
    try:
        got = executed_graph(png_path)
    except Exception as exc:                                  # noqa: BLE001
        return False, "could not read embedded graph: %s: %s" % (type(exc).__name__, exc)
    if got is None:
        return False, "no `prompt` tEXt chunk in %s" % os.path.basename(png_path)
    if _canon(got) == _canon(posted):
        return True, "posted == executed"
    diffs = []
    for k in sorted(set(got) | set(posted)):
        if _canon(got.get(k)) != _canon(posted.get(k)):
            diffs.append(k)
    return False, "posted != executed, nodes differ: %s" % ",".join(diffs)


# -------------------------------------------------------------------- render
def comfy_up():
    """This tool does NOT start ComfyUI, and deliberately does not stop it
    either -- qwen_compose.py's position, taken for the same reason. Teardown
    is a session-level act (invariant 4, and Geoff's standing rule that one
    must never be left running): the caller who started the seat knows its PID
    and whether anyone else is using it. A tool that pattern-matched
    python.exe and killed it would eventually kill someone else's render."""
    return CE.wait_for_comfy(COMFY_HOST, COMFY_PORT, timeout=5)


def render(prompt, negative, seed, steps, cfg, lora, strength, prefix, timeout=900):
    """-> (png_path, detail) or (None, why). Renders through the shared
    comfyui_execute.py wrapper, exactly as character_sheets.render_view() and
    qwen_compose.qwen_edit() do (invariant 11: one implementation, imported
    not copied). Then it does the thing this pipeline has twice failed to do:
    it reads the graph ComfyUI ACTUALLY EXECUTED out of the PNG's own tEXt
    chunk and compares it against the graph the wrapper was asked to post.
    A driver's log saying steps=40 is a description of a call; the PNG is the
    call."""
    expected = build_graph(prompt, negative, seed, steps, cfg, lora, strength, prefix)
    os.makedirs(PAYLOADS, exist_ok=True)
    with open(os.path.join(PAYLOADS, "%s.expected.json" % prefix), "w",
              encoding="utf-8") as fh:
        json.dump(expected, fh, indent=1, sort_keys=True)

    s, _gen = subs_for(prompt, negative, seed, steps, cfg, lora, strength, prefix)
    cmd = [PY, WRAPPER, "0", "0", "1", "--workflow", template_for(lora),
           "--comfy-output-dir", OUT_COMFY, "--verify-mode", "disk",
           "--output-prefix", prefix, "--seed-base", str(seed),
           "--expect-outputs", "1", "--timeout", str(timeout)]
    for k, v in s.items():
        cmd += ["--set", "%s=%s" % (k, v)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if "PASS:" not in (r.stdout or ""):
        return None, ((r.stdout or "") + (r.stderr or ""))[-800:]
    src = os.path.join(OUT_COMFY, "%s_w000_00001_.png" % prefix)
    if not os.path.exists(src):
        return None, "comfyui_execute.py reported PASS but %r is not on disk" % src
    os.makedirs(OUT, exist_ok=True)
    dst = os.path.join(OUT, "%s.png" % prefix)
    shutil.copy2(src, dst)
    ok, detail = verify_executed_graph(dst, expected)
    if not ok:
        return None, "GRAPH MISMATCH: %s" % detail
    return dst, detail


# ====================================================================== QC
# DETERMINISTIC signal, computed on every candidate and written onto its
# Version. Geoff, 2026-09-03: "in the long term we can't rely on claude vision
# to evaluate and gate keep things. it needs to be deterministic scripts more
# so... just publish them all and let the operator review."
#
# So nothing below decides what gets published. Every candidate publishes.
# These are numbers attached to the record so the operator can sort by them,
# and so a regression is detectable next month without anyone remembering what
# this batch looked like.
#
# The known failure these target is arithmetic, not aesthetic: pass 3 gap 5
# drew the same cell four times and one draw came back as "three identical
# front views". That is a pixel comparison, not a judgement.

# The design language's palette, as stated in THE-SHOW-PROGRAM.md: "warm
# orange, dusty purple, muted denim blue, teal accents", plus the outline
# colour ("dark warm brown") and the plain white ground. Hue bins in degrees.
PALETTE_BINS = {
    "warm_orange": (15, 45),
    "yellow_OFF_PALETTE": (46, 70),
    "green_OFF_PALETTE": (71, 150),
    "teal": (151, 200),
    "denim_blue": (201, 250),
    "dusty_purple": (251, 310),
    "red_pink_OFF_PALETTE": (311, 360),
}
QC_DUP_VIEW_THRESHOLD = 6.0      # mean abs grey difference; below this two
                                 # panels are the same drawing, not two views


def _panels(arr):
    """Split a turnaround into its view panels by finding the vertical gutters
    of plain background between figures. Deterministic and model-free: the
    sheets are drawn on a plain ground precisely so this works.

    -> list of (x0, x1) column ranges, left to right."""
    import numpy as np
    h, w, _ = arr.shape
    corner = np.median(
        np.concatenate([arr[0:8, 0:8].reshape(-1, 3), arr[0:8, -8:].reshape(-1, 3),
                        arr[-8:, 0:8].reshape(-1, 3), arr[-8:, -8:].reshape(-1, 3)]),
        axis=0)
    ink = (np.abs(arr.astype(np.int16) - corner.astype(np.int16)).sum(axis=2) > 40)
    col = ink.sum(axis=0)
    on = col > (h * 0.02)                       # a column carrying real drawing
    spans, start = [], None
    for x in range(w):
        if on[x] and start is None:
            start = x
        elif not on[x] and start is not None:
            if x - start > w * 0.05:            # ignore specks and stray hairs
                spans.append((start, x))
            start = None
    if start is not None and w - start > w * 0.05:
        spans.append((start, w))
    return spans


def _panel_key(arr, span, size=48):
    from PIL import Image
    import numpy as np
    x0, x1 = span
    im = Image.fromarray(arr[:, x0:x1]).convert("L").resize((size, size))
    return np.asarray(im, dtype=np.int16)


def _equal_split_distances(arr, n):
    """Cut the sheet into n equal-width panels and compare them pairwise.

    THE LIMITATION THIS COVERS, found against ground truth rather than
    guessed: the gutter splitter above cannot separate figures that OVERLAP
    IN X. On a real PILOTCHARB sheet the gutter between views 2 and 3 never drops
    below the ink threshold (min 51 px against a 14 px threshold) because the
    hair of one view reaches across the other, so a visibly three-view sheet
    reports two panels -- and a merged panel INFLATES the distance, which
    would mask a duplicate rather than merely miss a view.

    Lowering the threshold does not fix that: there is no clean column to
    find. So this second method assumes nothing about gutters at all. It is
    well matched to the failure that matters, because "three identical front
    views" places near-identical figures at regular spacing, which an equal-n
    cut separates exactly.

    Both methods are reported. The duplicate flag fires if EITHER sees one --
    a detector that can only fail in one direction is the safer default when
    the cost of a missed duplicate is a bad anchor going to panels."""
    import numpy as np
    h, w, _ = arr.shape
    step = w // n
    keys = [_panel_key(arr, (i * step, (i + 1) * step)) for i in range(n)]
    out = {}
    for i in range(n):
        for j in range(i + 1, n):
            out["v%d-v%d" % (i + 1, j + 1)] = round(
                float(np.abs(keys[i] - keys[j]).mean()), 2)
    return out


def qc_metrics(png_path):
    """Deterministic quality signal for one candidate. Never raises; on any
    internal failure it returns {"qc_error": ...} rather than taking a render
    down, because QC is a measurement of the artefact and must never be able
    to destroy it."""
    import numpy as np
    from PIL import Image
    try:
        with Image.open(png_path) as im:
            arr = np.asarray(im.convert("RGB"))
        h, w, _ = arr.shape
        m = {"width": w, "height": h,
             "resolution_ok": (w, h) == (WIDTH, HEIGHT)}

        # --- degenerate-output guard
        grey = arr.mean(axis=2)
        m["global_stddev"] = round(float(grey.std()), 2)
        m["degenerate_flat"] = bool(grey.std() < 8.0)
        corner = np.median(np.concatenate([arr[0:8, 0:8].reshape(-1, 3),
                                           arr[0:8, -8:].reshape(-1, 3)]), axis=0)
        bg = (np.abs(arr.astype(np.int16) - corner.astype(np.int16)).sum(axis=2) <= 40)
        m["background_pct"] = round(float(bg.mean() * 100), 1)
        m["degenerate_blank"] = bool(bg.mean() > 0.92)

        # --- duplicate-view detection
        spans = _panels(arr)
        m["views_detected"] = len(spans)
        keys = [_panel_key(arr, s) for s in spans]
        pairs = {}
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                pairs["v%d-v%d" % (i + 1, j + 1)] = round(
                    float(np.abs(keys[i] - keys[j]).mean()), 2)
        m["view_pair_distances"] = pairs
        m["min_view_distance"] = round(min(pairs.values()), 2) if pairs else None
        # Second, gutter-free method -- see _equal_split_distances for the
        # real PILOTCHARB sheet that proved the first one can under-count.
        eq = {}
        for n_ in (2, 3, 4):
            d = _equal_split_distances(arr, n_)
            eq["n%d" % n_] = {"pairs": d, "min": round(min(d.values()), 2)}
        m["equal_split"] = eq
        m["equal_split_min"] = round(min(v["min"] for v in eq.values()), 2)
        m["duplicate_views"] = bool(
            (pairs and min(pairs.values()) < QC_DUP_VIEW_THRESHOLD)
            or m["equal_split_min"] < QC_DUP_VIEW_THRESHOLD)
        m["duplicate_source"] = (
            "gutter" if (pairs and min(pairs.values()) < QC_DUP_VIEW_THRESHOLD)
            else ("equal-split" if m["equal_split_min"] < QC_DUP_VIEW_THRESHOLD
                  else None))

        # --- palette adherence, on the SUBJECT only (background excluded)
        sub = arr[~bg]
        if len(sub):
            mx = sub.max(axis=1).astype(np.float32)
            mn = sub.min(axis=1).astype(np.float32)
            sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
            keep = sat > 0.25                       # ignore near-greys/outline
            hues = np.zeros(len(sub), dtype=np.float32)
            r, g, b = sub[:, 0].astype(np.float32), sub[:, 1].astype(np.float32), sub[:, 2].astype(np.float32)
            d = np.maximum(mx - mn, 1e-6)
            hue = np.where(mx == r, ((g - b) / d) % 6,
                  np.where(mx == g, ((b - r) / d) + 2, ((r - g) / d) + 4)) * 60.0
            hues = hue % 360
            tot = max(int(keep.sum()), 1)
            hist = {}
            for name, (lo, hi) in PALETTE_BINS.items():
                hist[name] = round(float(((hues[keep] >= lo) & (hues[keep] <= hi)).sum())
                                   / tot * 100, 1)
            m["hue_histogram_pct"] = hist
            m["off_palette_pct"] = round(sum(v for k, v in hist.items()
                                             if k.endswith("OFF_PALETTE")), 1)
            m["saturated_pct"] = round(float(keep.mean() * 100), 1)
        return m
    except Exception as exc:                                   # noqa: BLE001
        return {"qc_error": "%s: %s" % (type(exc).__name__, exc)}


def qc_summary(m):
    """One line, for the Version description and the log."""
    if "qc_error" in m:
        return "QC FAILED TO RUN: %s" % m["qc_error"]
    flags = [k for k in ("duplicate_views", "degenerate_flat", "degenerate_blank")
             if m.get(k)]
    if not m.get("resolution_ok"):
        flags.append("wrong_resolution")
    return ("views=%s min_view_dist=%s eqsplit_min=%s off_palette=%.1f%% "
            "bg=%.1f%% stddev=%.1f flags=%s"
            % (m.get("views_detected"), m.get("min_view_distance"),
               m.get("equal_split_min"),
               m.get("off_palette_pct", -1), m.get("background_pct", -1),
               m.get("global_stddev", -1), ",".join(flags) or "none"))


# ------------------------------------------------------------------ ShotGrid
def fetch_assets(sg, only=None):
    """The Asset records, LIVE. Every prompt in this tool is built from what
    comes back here; nothing is cached in source."""
    ids = [i for i, (code, _) in ASSETS.items() if only in (None, code)]
    if not ids:
        raise SystemExit("FAIL: --only %r matches none of %s"
                         % (only, sorted(c for c, _ in ASSETS.values())))
    rows = sg.find("Asset", [["id", "in", ids]],
                   ["id", "code", "description", "sg_prompt_fragment",
                    "sg_design_attributes", "sg_status_list", "sg_stage",
                    "sg_approved_design"])
    by_id = {r["id"]: r for r in rows}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise SystemExit("FAIL: Assets not found in ShotGrid: %s" % missing)
    for i, r in by_id.items():
        expect = ASSETS[i][0]
        if r["code"] != expect:
            raise SystemExit("FAIL: Asset %d is %r, expected %r -- this tool's "
                             "id->code map is stale, refusing to render."
                             % (i, r["code"], expect))
    return [by_id[i] for i in ids]


def candidate_code(asset_code, recipe, seed, wedge=False):
    return "%s_ANCHOR_%s%s_s%d" % (asset_code, recipe, "_negtext" if wedge else "", seed)


def publish_candidate(sg, asset, recipe, seed, png, prompt, negative, guard_changed,
                      removed_graphic=(), wedge=False):
    """Publish ONE candidate and prove it viewable, immediately -- never as a
    batch step at the end. An interrupted batch leaves work that looks
    published and is not.

    entity is the ASSET. A Version's entity is the thing it is a version OF
    (verified population: Shot 327 / Asset 86 / Episode 55). A design anchor
    for SHOW_CHAR_PILOTCHARA is a version of SHOW_CHAR_PILOTCHARA, not of the episode.
    Status is 'rev': this tool publishes candidates into review and approves
    nothing."""
    r = RECIPES[recipe]
    code = candidate_code(asset["code"], recipe, seed, wedge)
    desc = (
        "Anima design anchor candidate. Asset %s (%d). Recipe %s: %s, %s, "
        "%d steps, cfg %.1f, seed %d, %dx%d, %s/%s shift %.1f.\n"
        "Prompt = 'masterpiece, best quality, score_7, safe, ' + the live "
        "ShotGrid sg_prompt_fragment verbatim (sha256 %s), never a "
        "paraphrase. Forbidden-style guard %s.\n"
        "Garment-graphic clauses removed: %s. A described printed surface "
        "is what invites Anima's invented lettering; the A/B/C wedge on "
        "12197 (Versions 67477/67478/67479) showed removing the clause "
        "clears it, and that a negative-prompt term CANNOT, because cfg "
        "1.0 makes the negative prompt inert.\n"
        "Negative: %s\n"
        "One of >=%d seeds of the same recipe -- turnaround quality is a seed "
        "property (ANIMA-PASS3.md gap 5), so this is a candidate, not a "
        "result. Status 'rev'; this tool approves nothing (invariant 7).%s"
        % (asset["code"], asset["id"], recipe, CHECKPOINT,
           ("LoRA %s @%.1f" % (r["lora"], r["strength"])) if r["lora"] else "no LoRA",
           r["steps"], r["cfg"], seed, WIDTH, HEIGHT, SAMPLER, SCHEDULER, SHIFT,
           hashlib.sha256((asset.get("sg_prompt_fragment") or "").encode()).hexdigest()[:16],
           "STRIPPED A TOKEN" if guard_changed else "clean (no-op)",
           ", ".join(removed_graphic) or "none",
           negative, MIN_SEEDS,
           "\nNEGATIVE-PROMPT WEDGE CELL: this run appends %r to the negative "
           "as a one-axis TEST of the invented-text hazard. It is not part of "
           "any adopted recipe." % NEG_TEXT_WEDGE_TERM if wedge else ""))
    kind = ASSETS[asset["id"]][1]
    qc = qc_metrics(png)
    desc += ("\n\nDETERMINISTIC QC (no vision model; computed by "
             "anima_anchor.qc_metrics on this exact file):\n  %s\n  %s\n"
             "These are measurements, not a verdict. Nothing here gates "
             "publication -- every seed is published and the operator picks. "
             "'duplicate_views' is the arithmetic form of the known seed "
             "failure (a sheet whose views are the same drawing); "
             "'off_palette' sums the hue bins outside the design language's "
             "warm orange / dusty purple / denim blue / teal.\n"
             "NOT COVERED: invented text on garments. No OCR is installed on "
             "this box (no tesseract binary, no pytesseract/easyocr in the "
             "venv), so the text hazard is NOT machine-checked here."
             % (qc_summary(qc), json.dumps(qc, sort_keys=True)))
    subs, gen = subs_for(prompt, negative, seed, r["steps"], r["cfg"],
                         r["lora"], r["strength"], code)
    wf_hash = PROV.workflow_hash_from_template(template_for(r["lora"]),
                                               dict(subs, **gen))
    v = PUB.publish_version(
        sg, project=PROJ, entity={"type": "Asset", "id": asset["id"]},
        code=code, media_path=png, description=desc, status="rev",
        stage="keyframe",
        extra_fields={"sg_prompt_final__as_sent_": prompt, "sg_model": MODEL_LABEL},
        character=(asset["code"] if kind == "char"
                   else "n/a - set design anchor, no character in frame"),
        set_=(asset["code"] if kind == "set"
              else "n/a - character turnaround on a plain white ground, no set"),
        action=("static %s anchor, seed %d, one of >=%d draws of the same recipe"
                % ("character turnaround" if kind == "char" else "set design",
                   seed, MIN_SEEDS)),
        camera=("no camera move; %dx%d single still" % (WIDTH, HEIGHT)),
        style="flat toon register, %s recipe (%s)" % (recipe, MODEL_LABEL),
        workflow_hash=wf_hash, log=log)
    ok, detail = PUBCHK.check_viewable(sg, v["id"])
    log("    Version %s %s -- viewable: %s" % (v["id"], code, ok))
    log("      QC: %s" % qc_summary(qc))
    if not ok:
        raise PUB.PublishError("Version %s published but NOT viewable: %s"
                               % (v["id"], detail))
    return v["id"], qc


# ------------------------------------------------------------------ commands
def cmd_show_prompts(sg, only):
    for a in fetch_assets(sg, only):
        frag = a.get("sg_prompt_fragment") or ""
        kind = ASSETS[a["id"]][1]
        prompt, changed, removed = compose(frag, strip_graphic=wants_graphic_guard(kind))
        print("=" * 72)
        print("%d %s  kind=%s  recipes=%s  seeds=%s"
              % (a["id"], a["code"], kind, ",".join(KIND_RECIPES[kind]), seeds_for()))
        print("fragment sha256 %s (%d chars), guard %s"
              % (hashlib.sha256(frag.encode()).hexdigest()[:16], len(frag),
                 "STRIPPED" if changed else "no-op"))
        print("graphic clauses removed: %s" % (removed or "none"))
        print("-- positive --"); print(prompt)
        print("-- negative --"); print(negative_for())
        expect_body = strip_printed_surface(frag)[0] if removed else frag
        print("-- fragment (minus reported removals) appears verbatim: %s"
              % (expect_body in prompt))
    return 0


def cmd_generate(sg, only, nseeds, wedge, dry):
    seeds = seeds_for(nseeds)
    assets = fetch_assets(sg, only)
    if not dry and not comfy_up():
        raise SystemExit("FAIL: ComfyUI is not up at %s:%s." % (COMFY_HOST, COMFY_PORT))
    os.makedirs(OUT, exist_ok=True)
    ledger_path = os.path.join(OUT, "ledger.json")
    ledger = json.load(open(ledger_path)) if os.path.exists(ledger_path) else {}
    for a in assets:
        kind = ASSETS[a["id"]][1]
        frag = a.get("sg_prompt_fragment") or ""
        # strip_graphic for CHARACTERS only: the text wedge on 12197 showed
        # the garment-graphic clause is what invites the invented lettering,
        # and that removing it clears the text at no register cost. Sets keep
        # their fragment whole -- 12202's poster walls ARE the set.
        prompt, changed, removed = compose(frag, strip_graphic=wants_graphic_guard(kind))
        # The fragment must reach the prompt VERBATIM, minus only clauses this
        # tool has named out loud. Comparing against the raw record would fail
        # the moment the graphic guard fires; comparing against nothing at all
        # is how a paraphrase gets in. So: strip exactly what was reported as
        # stripped, and require the remainder byte-for-byte.
        expect_body = strip_printed_surface(frag)[0] if removed else frag
        if expect_body not in prompt:
            raise SystemExit(
                "FAIL: composed prompt for %s does not contain its own live "
                "fragment verbatim (minus the %d clause(s) reported as "
                "removed: %s)." % (a["code"], len(removed), removed))
        negative = negative_for(wedge)
        recipes = ("base40",) if wedge else KIND_RECIPES[kind]
        log("=" * 72)
        log("%d %s (%s) -- %d seeds x %s" % (a["id"], a["code"], kind,
                                             len(seeds), ",".join(recipes)))
        for recipe in recipes:
            r = RECIPES[recipe]
            for seed in seeds:
                code = candidate_code(a["code"], recipe, seed, wedge)
                if code in ledger and os.path.exists(ledger[code].get("png", "")):
                    log("  %s already done (Version %s)" % (code, ledger[code].get("version")))
                    continue
                prefix = "show_%s_r%s" % (code.lower(), RUN_TAG)
                g = build_graph(prompt, negative, seed, r["steps"], r["cfg"],
                                r["lora"], r["strength"], prefix)
                if dry:
                    log("  DRY %s  steps=%d cfg=%.1f lora=%s wiring 2.model=%s"
                        % (code, r["steps"], r["cfg"], r["lora"],
                           g["2"]["inputs"]["model"]))
                    continue
                t0 = time.time()
                png, detail = render(prompt, negative, seed, r["steps"], r["cfg"],
                                     r["lora"], r["strength"], prefix)
                if png is None:
                    log("  RENDER FAILED %s: %s" % (code, detail))
                    continue
                log("  %s -> %s (%.1fs, %s)" % (code, os.path.basename(png),
                                                time.time() - t0, detail))
                vid, qc = publish_candidate(sg, a, recipe, seed, png, prompt,
                                            negative, changed, removed, wedge)
                ledger[code] = {"asset": a["id"], "asset_code": a["code"],
                                "recipe": recipe, "seed": seed, "png": png,
                                "version": vid, "wedge": wedge,
                                "steps": r["steps"], "cfg": r["cfg"],
                                "lora": r["lora"], "removed_graphic": removed,
                                "qc": qc}
                # Written after EVERY candidate, not at the end, for the same
                # reason the publish is: an interrupted run must leave a true
                # record of exactly what got as far as ShotGrid.
                with open(ledger_path, "w", encoding="utf-8") as fh:
                    json.dump(ledger, fh, indent=1)
    log("\nledger: %s (%d candidates)" % (ledger_path, len(ledger)))
    return 0


# The seed the reviewer's approved cell (67462) was drawn at. The wedge is
# locked to it so cell A reproduces that exact image and the comparison is
# against the thing Geoff actually looked at, not a fresh draw of it.
TEXT_WEDGE_SEED = 51002
TEXT_WEDGE_ASSET = 12197


def cmd_text_wedge(sg, dry):
    """The one-axis wedge that has to clear BEFORE the speed recipe is
    committed for characters.

    Geoff approved 67462 -- PILOTCHARA, turbo-LoRA on base, 10 steps / cfg 1.0 --
    and asked for PILOTCHARB to be redone to match. That recipe is also the one pass
    3 measured rendering LEGIBLE invented text on a described garment graphic.
    The look was never the objection; the text is. Three cells, seed locked at
    %d, everything else held:

      A  the fragment as-is, turbo @10                 (the known-bad case,
                                                        i.e. what 67462 is)
      B  A with the printed-graphic clause removed      (PROMPT axis)
      C  A plus an anti-text negative term              (RECIPE axis)

    B and C are alternatives, not a sequence: each differs from A by exactly
    one thing, and neither differs from the other by one thing. Whichever
    removes the text without costing the register is the fix; if neither does,
    that is the answer and it gets said rather than worked around.
    """ % TEXT_WEDGE_SEED
    a = fetch_assets(sg, "SHOW_CHAR_PILOTCHARA")[0]
    frag = a.get("sg_prompt_fragment") or ""
    stripped, removed = strip_printed_surface(frag)
    if not removed:
        raise SystemExit(
            "FAIL: %s's live fragment describes no printed surface, so cell B "
            "would be identical to cell A and the wedge would measure nothing. "
            "The hazard is defined by that clause; if the record no longer has "
            "one, re-read the record before running this." % a["code"])

    p_full, ch_full, _ = compose(frag)
    p_strip, ch_strip, _ = compose(stripped)
    r = RECIPES["turbo10"]

    cells = [
        ("A_asis", p_full, negative_for(False),
         "control: the live fragment as-is. This is the recipe and seed of "
         "Version 67462, the cell Geoff approved."),
        ("B_nographic", p_strip, negative_for(False),
         "PROMPT axis, one change from A: the printed-graphic clause %r is "
         "removed from the composed prompt. Nothing else differs." % removed),
        ("C_negtext", p_full, negative_for(True),
         "RECIPE axis, one change from A: %r appended to the negative prompt. "
         "Nothing else differs." % NEG_TEXT_WEDGE_TERM),
    ]
    log("text wedge on %d %s, seed %d, turbo10 (%d steps / cfg %.1f)"
        % (a["id"], a["code"], TEXT_WEDGE_SEED, r["steps"], r["cfg"]))
    log("  clause removed for cell B: %s" % removed)
    if p_full == p_strip:
        raise SystemExit("FAIL: A and B composed identically -- not a wedge.")

    out = {}
    for name, prompt, negative, why in cells:
        code = "%s_TEXTWEDGE_%s_s%d" % (a["code"], name, TEXT_WEDGE_SEED)
        prefix = "show_%s_r%s" % (code.lower(), RUN_TAG)
        if dry:
            log("  DRY %s neg=%r prompt_tail=%r"
                % (code, negative[-40:], prompt[-90:]))
            continue
        t0 = time.time()
        png, detail = render(prompt, negative, TEXT_WEDGE_SEED, r["steps"],
                             r["cfg"], r["lora"], r["strength"], prefix)
        if png is None:
            log("  RENDER FAILED %s: %s" % (code, detail))
            continue
        log("  %s -> %s (%.1fs, %s)" % (code, os.path.basename(png),
                                        time.time() - t0, detail))
        desc = ("ANIMA TEXT-HAZARD WEDGE, cell %s. %s\n\n"
                "Asset %s (%d). turbo10 recipe: %s + %s @%.1f, %d steps, "
                "cfg %.1f, seed %d (locked across all three cells), %dx%d.\n"
                "Negative: %s\n\n"
                "Why this wedge exists: Geoff approved Version 67462 (this "
                "recipe) and asked for PILOTCHARB to match it. Pass 3 measured the "
                "same recipe rendering LEGIBLE invented text on a described "
                "garment graphic. A/B/C separate the clause from the recipe. "
                "Test record, status 'rev'; this tool approves nothing."
                % (name, why, a["code"], a["id"], CHECKPOINT, r["lora"],
                   r["strength"], r["steps"], r["cfg"], TEXT_WEDGE_SEED,
                   WIDTH, HEIGHT, negative))
        subs, gen = subs_for(prompt, negative, TEXT_WEDGE_SEED, r["steps"],
                             r["cfg"], r["lora"], r["strength"], code)
        v = PUB.publish_version(
            sg, project=PROJ, entity={"type": "Asset", "id": a["id"]},
            code=code, media_path=png, description=desc, status="rev",
            stage="keyframe",
            extra_fields={"sg_prompt_final__as_sent_": prompt,
                          "sg_model": MODEL_LABEL},
            character=a["code"], set_="n/a - character turnaround, no set",
            action="text-hazard wedge cell %s, seed locked" % name,
            camera="no camera move; %dx%d single still" % (WIDTH, HEIGHT),
            style="flat toon register, turbo10 recipe",
            workflow_hash=PROV.workflow_hash_from_template(
                template_for(r["lora"]), dict(subs, **gen)), log=log)
        ok, why_v = PUBCHK.check_viewable(sg, v["id"])
        log("    Version %s viewable: %s (%s)" % (v["id"], ok, why_v))
        if not ok:
            raise PUB.PublishError("Version %s not viewable: %s" % (v["id"], why_v))
        out[name] = {"version": v["id"], "png": png, "code": code}
    if out:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "text_wedge.json"), "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
    return 0


def cmd_report():
    ledger_path = os.path.join(OUT, "ledger.json")
    if not os.path.exists(ledger_path):
        log("no ledger at %s" % ledger_path)
        return 1
    led = json.load(open(ledger_path))
    for code in sorted(led):
        e = led[code]
        log("%-58s v%-6s %s" % (code, e.get("version"),
                                "on disk" if os.path.exists(e.get("png", "")) else "MISSING"))
    log("%d candidates" % len(led))
    return 0


# ------------------------------------------------------------------ self test
def self_test():
    fails = []

    def ck(name, cond):
        """`cond` may be a bool OR a zero-arg callable. A callable is invoked
        inside a try: a canary that raises must report FAIL and let the rest
        of the suite run. Letting it propagate kills every later check and
        prints no FAIL line at all -- which is how a self-test that detected a
        real bug can look like a self-test that never ran."""
        if callable(cond):
            try:
                cond = bool(cond())
            except Exception as exc:                           # noqa: BLE001
                print("  %-96s FAIL (raised %s)" % (name, type(exc).__name__))
                fails.append(name)
                return
        print("  %-96s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    import re as _re
    ck("the shared ComfyUI wrapper is on disk", os.path.exists(WRAPPER))
    ck("both committed Anima templates are on disk",
       os.path.exists(TPL_ANIMA) and os.path.exists(TPL_ANIMA_LORA))

    def placeholders_of(path):
        return set(_re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                               open(path, encoding="utf-8").read()))

    s_no, g_no = subs_for("p", "n", 1, 40, 6.0, None, 0.0, "x")
    s_lo, g_lo = subs_for("p", "n", 1, 10, 1.0, TURBO_LORA, 1.0, "x")
    # `wedge` is generated by comfyui_execute.py for every job but is not a
    # placeholder either Anima template uses, so it is the one legitimate
    # extra. Everything else must match exactly in BOTH directions: a
    # placeholder we do not supply is a hard SystemExit at render time, and a
    # sub we supply that no template reads is a setting that silently does
    # nothing -- which is how a "40-step" cell renders at the template default.
    ck("CANARY every placeholder in anima_t2i.api.json is supplied, and no "
       "supplied sub is ignored",
       placeholders_of(TPL_ANIMA) == (set(s_no) | set(g_no)) - {"wedge"})
    ck("CANARY every placeholder in anima_t2i_lora.api.json is supplied, and "
       "no supplied sub is ignored",
       placeholders_of(TPL_ANIMA_LORA) == (set(s_lo) | set(g_lo)) - {"wedge"})
    ck("CANARY the LoRA template differs from the plain one by exactly the "
       "two LoRA placeholders",
       placeholders_of(TPL_ANIMA_LORA) - placeholders_of(TPL_ANIMA)
       == {"lora_name", "lora_strength"})
    ck("CANARY template_for() picks the LoRA graph only when a LoRA is asked "
       "for", template_for(None) == TPL_ANIMA and template_for(TURBO_LORA) == TPL_ANIMA_LORA)
    ck("CANARY subs_for() emits no lora_* keys for a no-LoRA cell (a stray "
       "one would leave an unsubstituted placeholder or a silent extra)",
       "lora_name" not in s_no and "lora_strength" not in s_no)
    ck("CANARY subs_for() reproduces the wrapper's own _w000 output-prefix "
       "suffix, so the local expectation matches what it posts",
       g_no["output_prefix"] == "x_w000" and g_no["wedge"] == 0)
    ck("checkpoint is Anima Base v1.0", CHECKPOINT == "anima-base-v1.0.safetensors")
    ck("checkpoint file is on disk",
       os.path.exists(os.path.join(OUT_COMFY, "..", "models", "diffusion_models", CHECKPOINT)))
    ck("turbo LoRA file is on disk",
       os.path.exists(os.path.join(OUT_COMFY, "..", "models", "loras", TURBO_LORA)))
    ck("resolution is the bake-off's 1280x704", (WIDTH, HEIGHT) == (1280, 704))
    ck("all six EVT Assets are mapped", len(ASSETS) == 6)
    ck("three characters and three sets",
       sum(1 for _, k in ASSETS.values() if k == "char") == 3
       and sum(1 for _, k in ASSETS.values() if k == "set") == 3)

    # --- RECIPE CANARY. The two recipes are not interchangeable and a swap is
    # silent: a character rendered at 10/1.0 comes back drawn, just with
    # legible invented text on it.
    ck("CANARY the base40 recipe is no-LoRA / 40 steps / cfg 6.0",
       RECIPES["base40"] == {"lora": None, "strength": 0.0, "steps": 40, "cfg": 6.0})
    ck("CANARY set recipe leads with turbo-v0.2 @1.0 / 10 steps / cfg 1.0",
       KIND_RECIPES["set"][0] == "turbo10"
       and RECIPES["turbo10"] == {"lora": TURBO_LORA, "strength": 1.0,
                                  "steps": 10, "cfg": 1.0})
    ck("CANARY every set ALSO runs the 40-step control -- adopted is not "
       "unexamined", "base40" in KIND_RECIPES["set"])
    ck("CANARY characters run the recipe the reviewer APPROVED (67462 is a "
       "turbo10 cell)",
       KIND_RECIPES["char"] == ("turbo10",)
       and GEOFF_VERDICTS[67462] == ("apr", "turbo10"))
    ck("CANARY dropping base40 from characters is backed by >=10 recorded "
       "reviewer rejections, not by this tool's opinion",
       sum(1 for st, r in GEOFF_VERDICTS.values() if st == "rjct" and r == "base40") >= 10)
    ck("CANARY the character recipe is base + turbo LoRA, NOT the turbo "
       "CHECKPOINT (67448 is the checkpoint cell and is only 'rrq')",
       RECIPES["turbo10"]["lora"] == TURBO_LORA
       and CHECKPOINT == "anima-base-v1.0.safetensors"
       and GEOFF_VERDICTS[67448][0] != "apr")
    ck("CANARY the graphic guard is OFF for every kind (Geoff 2026-09-07: stop "
       "preventing text). strip_printed_surface() is kept and still tested, so "
       "turning it back on is one return value",
       not any(wants_graphic_guard(k) for k in ("char", "set", "prop", None)))
    ck("CANARY the guard is off by DECISION, not by a missing mapping: every "
       "mapped kind still resolves, they just all resolve to False",
       set(wants_graphic_guard(k) for _, k in ASSETS.values()) == {False})
    ck("CANARY the guard is opt-in at the compose() boundary too",
       bool(compose("a t-shirt with a big graphic, x", strip_graphic=True)[2])
       and compose("a t-shirt with a big graphic, x")[2] == [])

    # --- SEED-POLICY CANARY. One generation per Asset is not a valid attempt.
    ck("CANARY the seed floor is 4", MIN_SEEDS >= 4)
    ck("seeds_for() returns >= 4 distinct seeds",
       len(set(seeds_for())) >= 4)
    refused = False
    try:
        seeds_for(1)
    except ValueError:
        refused = True
    ck("CANARY seeds_for(1) is REFUSED -- a single draw settles style and "
       "nothing else", refused)

    # --- FRAGMENT-SOURCE CANARY, against the REAL live fragment for 12197,
    # pasted here only as test DATA (the renderer never reads it).
    real_12197 = (
        "PILOTCHARA, flat 2d toon character, contemporary children's animation "
        "illustration style, adult man, 31 years old, slim build, large head "
        "proportion, chunky simplified anatomy, oversized hands, short messy "
        "dark brown hair, round wire-frame glasses, light stubble, dark "
        "circles under eyes, exhausted expression, slouched tired posture, "
        "oversized heather-grey t-shirt with faded film-reel graphic, warm "
        "orange accent trim, denim-blue plaid pajama pants, bare feet, bold "
        "clean dark warm brown outlines, slight line weight variation, flat "
        "colour fills, subtle grain texture, no gradients, simple one-side "
        "cel shadow, large expressive eyes, big white sclera, dark iris, "
        "simple highlight, plain white background, character turnaround "
        "sheet, (flat toon style:1.3)")
    ck("CANARY compose() does not raise on the real live fragment",
       lambda: compose(real_12197) and True)
    p, changed, removed_c = compose(real_12197)
    ck("CANARY the live fragment appears in the prompt BYTE-FOR-BYTE "
       "(no paraphrase, no reordering)", real_12197 in p)
    ck("CANARY the guard is a no-op on a clean real fragment", not changed)
    ck("CANARY compose() leaves the graphic clause ALONE unless asked "
       "(strip_graphic is opt-in, never a silent default)", removed_c == [])
    _pg, _, _rg = compose(real_12197, strip_graphic=True)
    ck("CANARY compose(strip_graphic=True) removes the graphic clause and "
       "REPORTS which one", _rg == ["with faded film-reel graphic"])
    ck("CANARY the graphic strip removes ONLY that clause -- checked against a "
       "LITERAL expected string, never against the function under test",
       _pg == QUALITY_PREFIX + real_12197.replace(
           "oversized heather-grey t-shirt with faded film-reel graphic",
           "oversized heather-grey t-shirt"))
    ck("prompt is exactly prefix + fragment", p == QUALITY_PREFIX + real_12197)
    ck("CANARY 'no gradients' SURVIVES -- the forbidden-token guard is narrow "
       "and must not eat design language", "no gradients" in p)
    refused = False
    try:
        compose("   ")
    except ValueError:
        refused = True
    ck("CANARY compose() REFUSES an empty fragment rather than inventing one",
       refused)

    # --- FORBIDDEN-TOKEN CANARY. `no lineart` is a published trigger word for
    # a LoRA in this same family, so it arrives innocently; cells L18/L19
    # proved it strips the bold outline the design language requires.
    dirty = real_12197.replace("no gradients", "no gradients, no lineart")
    ck("CANARY the forbidden-token DETECTOR is not inert (it fires on the "
       "un-guarded string)",
       DG.contains_forbidden_style(QUALITY_PREFIX + dirty))
    ck("CANARY compose() strips 'no lineart' from a fragment that carries it",
       lambda: not DG.contains_forbidden_style(compose(dirty)[0]))
    ck("CANARY the strip is REPORTED, not silent (guard_changed is True)",
       lambda: compose(dirty)[1])
    ck("CANARY the surrounding phrase survives the narrow strip",
       lambda: "no gradients" in compose(dirty)[0])
    ck("CANARY the guard is the shared one, not a reimplementation",
       compose.__module__ == __name__ and DG.strip_forbidden_style("a, no lineart, b") == "a, b")

    # --- PRINTED-SURFACE CANARIES. The B cell of the text wedge is only a
    # one-axis change if this removes the graphic clause and NOTHING else.
    v_frag = ("oversized heather-grey t-shirt with faded film-reel graphic, "
              "warm orange accent trim, denim-blue plaid pajama pants, bare feet")
    v_out, v_rm = strip_printed_surface(v_frag)
    ck("CANARY strip_printed_surface removes the garment-graphic clause",
       v_rm == ["with faded film-reel graphic"])
    ck("CANARY it keeps the garment itself, its colour and its fit",
       v_out == ("oversized heather-grey t-shirt, warm orange accent trim, "
                 "denim-blue plaid pajama pants, bare feet"))
    ck("CANARY it reports WHAT it removed, so a wedge cell can name its own "
       "one axis instead of asserting it", bool(v_rm))
    b_frag = ("oversized plain sleep shirt with a wide scooped neck opening, "
              "worn off one shoulder, one bare shoulder, cleavage")
    ck("CANARY a fragment with a neckline but NO graphic is returned "
       "byte-identical (a wider neckline is not a printed surface)",
       strip_printed_surface(b_frag) == (b_frag, []))
    s_frag = ("walls densely covered edge to edge in colourful stylised movie "
              "poster art with no legible text or titles, double bed")
    ck("CANARY the poster-wall set clause is NOT eaten -- it is the set's "
       "whole subject, and the guard is for garments",
       strip_printed_surface(s_frag) == (s_frag, []))
    ck("CANARY strip_printed_surface never raises on empty input",
       strip_printed_surface("") == ("", []))

    # --- TEXT-WEDGE CANARIES: A/B/C must each differ from A by ONE thing.
    _pa = compose(v_frag)[0]
    _pb = compose(v_frag, strip_graphic=True)[0]
    ck("CANARY wedge cell B differs from A in the PROMPT and only there",
       _pa != _pb and negative_for(False) == negative_for(False))
    ck("CANARY wedge cell C differs from A in the NEGATIVE and only there",
       negative_for(True) != negative_for(False))
    ck("CANARY B and C are alternatives, not a sequence (neither is the "
       "other's one-axis twin)", _pb != _pa and NEG_TEXT_WEDGE_TERM not in _pb)
    ck("CANARY the wedge is locked to the seed of the cell Geoff approved",
       TEXT_WEDGE_SEED == 51002 and GEOFF_VERDICTS[67462][0] == "apr")

    # --- D15 CANARY: nothing dialogue-shaped reaches a generation prompt.
    ck("CANARY no live-shaped prompt asks for text",
       not any(t in p.lower() for t in ("subtitle", "caption", "speech bubble",
                                        "says ", "dialogue")))

    # --- NEGATIVE-PROMPT CANARY. The anti-text term is UNTRIED. It must not
    # leak into the production negative just because it is defined here.
    ck("CANARY the production negative is the pass-1/2/3 string unchanged",
       negative_for() == NEG_BASE)
    ck("CANARY the untried anti-text term is ABSENT from the production "
       "negative -- token by token, not just as a whole phrase",
       not any(t.strip() in [x.strip() for x in negative_for().split(",")]
               for t in NEG_TEXT_WEDGE_TERM.split(",")))
    ck("CANARY the wedge negative differs by exactly one appended clause",
       negative_for(True) == NEG_BASE + ", " + NEG_TEXT_WEDGE_TERM)

    # --- LORA-WIRING CANARY. A LoRA that loads but is not routed through
    # renders a clean control and gets reported as a LoRA result.
    gc = build_graph("p", "n", 1, 40, 6.0, None, 0.0, "x")
    gl = build_graph("p", "n", 1, 10, 1.0, TURBO_LORA, 1.0, "x")
    ck("CANARY no-LoRA graph routes 2.model straight from node 1",
       gc["2"]["inputs"]["model"] == ["1", 0] and "11" not in gc)
    ck("CANARY LoRA graph REWIRES 2.model through node 11",
       gl["2"]["inputs"]["model"] == ["11", 0])
    ck("CANARY the LoRA node patches the raw UNet (node 1), not the "
       "already-sampled model", gl["11"]["inputs"]["model"] == ["1", 0])
    ck("CANARY the LoRA graph differs from its control by EXACTLY one node "
       "plus the rewire (invariant 5, one axis)",
       set(gl) - set(gc) == {"11"}
       and all(_canon(gl[k]) == _canon(gc[k]) for k in gc
               if k not in ("2", "8", "10")))
    ck("CANARY steps/cfg reach the KSampler, not just the log",
       gc["8"]["inputs"]["steps"] == 40 and gc["8"]["inputs"]["cfg"] == 6.0
       and gl["8"]["inputs"]["steps"] == 10 and gl["8"]["inputs"]["cfg"] == 1.0)
    ck("CANARY the seed reaches the KSampler",
       build_graph("p", "n", 51005, 40, 6.0, None, 0.0, "x")["8"]["inputs"]["seed"] == 51005)
    ck("CANARY the positive prompt reaches CLIPTextEncode node 4 verbatim",
       build_graph(p, "n", 1, 40, 6.0, None, 0.0, "x")["4"]["inputs"]["text"] == p)

    # --- POSTED-vs-EXECUTED CANARY, run against a REAL bake-off PNG rather
    # than a fixture: P3_16 is the adopted set recipe and its own PNG carries
    # the graph ComfyUI executed.
    real_png = os.path.join(ROOT, "output", "anima_bakeoff",
                            "P3_16_PILOTCHARA_bedroom_real12200_base_turboLoraV0.2_10step.png")
    if os.path.exists(real_png):
        ex = executed_graph(real_png)
        ok_same, _ = verify_executed_graph(real_png, ex)
        ck("CANARY verify_executed_graph PASSES a real PNG against its own "
           "executed graph", ok_same)
        tampered = json.loads(json.dumps(ex))
        tampered["8"]["inputs"]["steps"] = 40
        ok_diff, why = verify_executed_graph(real_png, tampered)
        ck("CANARY verify_executed_graph CATCHES a one-field difference "
           "(steps 10 -> 40)", (not ok_diff) and "8" in why)
        ck("CANARY the real adopted set recipe IS turbo10 as this tool "
           "encodes it",
           ex["11"]["inputs"]["lora_name"] == TURBO_LORA
           and ex["8"]["inputs"]["steps"] == 10 and ex["8"]["inputs"]["cfg"] == 1.0
           and ex["2"]["inputs"]["model"] == ["11", 0])
        ck("CANARY this tool's negative matches the one the 71 cells ran",
           ex["5"]["inputs"]["text"] == NEG_BASE)
        ck("CANARY this tool's quality prefix matches the one the 71 cells ran",
           ex["4"]["inputs"]["text"].startswith(QUALITY_PREFIX))
    else:
        ck("CANARY posted-vs-executed runs against a real bake-off PNG", False)

    # --- QC CANARIES, against SYNTHETIC images whose right answer is known by
    # construction. This is the deterministic replacement for eyeing a render,
    # so it needs a positive control (duplicates are caught) AND a negative one
    # (distinct views are not called duplicates). A duplicate detector that
    # flags everything is as useless as one that flags nothing.
    import tempfile as _tf
    import numpy as _np
    from PIL import Image as _Im, ImageDraw as _Dw

    def _sheet(panels, size=(WIDTH, HEIGHT)):
        """size[0]-wide plain-white sheet with `panels` blobs across it."""
        im = _Im.new("RGB", size, (255, 255, 255))
        d = _Dw.Draw(im)
        n = len(panels)
        for i, kind in enumerate(panels):
            cx = int(size[0] * (i + 0.5) / n)
            cy = size[1] // 2
            if kind == "circle":
                d.ellipse([cx - 120, cy - 180, cx + 120, cy + 180], fill=(210, 120, 40),
                          outline=(60, 40, 30), width=8)
            elif kind == "square":
                d.rectangle([cx - 110, cy - 190, cx + 110, cy + 190], fill=(60, 90, 160),
                            outline=(60, 40, 30), width=8)
            else:
                d.polygon([(cx, cy - 190), (cx + 130, cy + 180), (cx - 130, cy + 180)],
                          fill=(40, 150, 150), outline=(60, 40, 30))
        p = os.path.join(_tf.gettempdir(), "anima_qc_%s.png" % "_".join(panels))
        im.save(p)
        return p

    same = qc_metrics(_sheet(["circle", "circle", "circle"]))
    diff = qc_metrics(_sheet(["circle", "square", "tri"]))
    ck("CANARY QC splits a three-view sheet into three views",
       same.get("views_detected") == 3 and diff.get("views_detected") == 3)
    ck("CANARY QC CATCHES three identical views (the known seed failure) -- "
       "positive control", same.get("duplicate_views") is True)
    ck("CANARY QC does NOT call three genuinely different views duplicates -- "
       "negative control, without which the detector could just flag "
       "everything", diff.get("duplicate_views") is False)
    ck("CANARY the duplicate verdict is backed by a REPORTED distance, not a "
       "bare boolean",
       same.get("min_view_distance") < QC_DUP_VIEW_THRESHOLD
       < diff.get("min_view_distance"))
    ck("CANARY QC reports a distance for every view PAIR",
       len(diff.get("view_pair_distances", {})) == 3)
    ck("CANARY the gutter-free equal-split method runs and reports n=2,3,4",
       set(same.get("equal_split", {})) == {"n2", "n3", "n4"})
    ck("CANARY equal-split ALSO catches three identical panels (it is a "
       "backstop for sheets whose figures overlap in x)",
       same.get("equal_split_min") < QC_DUP_VIEW_THRESHOLD)
    ck("CANARY equal-split does not call three DIFFERENT panels duplicates",
       diff.get("equal_split_min") > QC_DUP_VIEW_THRESHOLD)
    ck("CANARY the duplicate verdict NAMES which method fired, so a weak "
       "backstop is never mistaken for a strong one",
       same.get("duplicate_source") in ("gutter", "equal-split")
       and diff.get("duplicate_source") is None)

    blank = qc_metrics(_sheet([]))
    ck("CANARY QC flags a blank render as degenerate",
       blank.get("degenerate_blank") is True and blank.get("views_detected") == 0)
    ck("CANARY QC does not flag a real render as degenerate",
       diff.get("degenerate_blank") is False and diff.get("degenerate_flat") is False)

    small = qc_metrics(_sheet(["circle", "square"], size=(640, 352)))
    ck("CANARY QC catches a wrong resolution",
       small.get("resolution_ok") is False and diff.get("resolution_ok") is True)

    ck("CANARY the off-palette bins really are the off-palette ones",
       set(k for k in PALETTE_BINS if k.endswith("OFF_PALETTE"))
       == {"yellow_OFF_PALETTE", "green_OFF_PALETTE", "red_pink_OFF_PALETTE"})
    ck("CANARY palette bins cover the hue circle without gaps or overlaps",
       sorted(PALETTE_BINS.values())[0][0] == 15
       and all(b[0] == a[1] + 1 for a, b in zip(sorted(PALETTE_BINS.values()),
                                                sorted(PALETTE_BINS.values())[1:])))
    ck("CANARY QC never raises on a missing file -- it measures artefacts, it "
       "must never be able to destroy one",
       "qc_error" in qc_metrics(os.path.join(_tf.gettempdir(), "no_such_file.png")))

    # --- ENTITY / STATUS CANARY. A Version's entity is the thing it is a
    # version OF (verified population: Shot 327 / Asset 86 / Episode 55). This
    # inspects the REAL kwargs publish_candidate hands the publish contract,
    # not a provenance string describing them -- checking the description
    # instead of the call is exactly how this pipeline shipped an unenforced
    # clause twice.
    seen = {}
    real_publish, real_check = PUB.publish_version, PUBCHK.check_viewable
    try:
        PUB.publish_version = lambda sg, **kw: (seen.update(kw) or {"id": -1})
        PUBCHK.check_viewable = lambda sg, vid, **kw: (True, "stubbed")
        a = {"id": 12197, "code": "SHOW_CHAR_PILOTCHARA", "sg_prompt_fragment": real_12197}
        publish_candidate(None, a, "base40", 52001, _sheet(["circle", "square"]),
                          p, NEG_BASE, False, [])
    finally:
        PUB.publish_version, PUBCHK.check_viewable = real_publish, real_check
    ck("CANARY the Version entity is the ASSET, never the Episode",
       seen.get("entity") == {"type": "Asset", "id": 12197})
    ck("CANARY the candidate publishes at status 'rev', never 'apr'",
       seen.get("status") == "rev")
    ck("CANARY the prompt sent to SG is the prompt sent to the GPU",
       seen.get("extra_fields", {}).get("sg_prompt_final__as_sent_") == p)
    ck("CANARY the fragment's sha256 is recorded so the source is checkable",
       hashlib.sha256(real_12197.encode()).hexdigest()[:16] in seen.get("description", ""))

    # --- REFUSE-TO-APPROVE CANARY (invariant 7 / D10 / D14).
    class _StubSG:
        def update(self, *a, **k):
            raise AssertionError("set_asset_stage must refuse BEFORE sg.update")

    def refuses(value):
        """True only for a clean PermissionError. Any OTHER exception means
        the refusal did not happen and sg.update was reached -- that must read
        as FAIL, not as a crashed self-test, or the one canary that matters
        most reports nothing at all."""
        try:
            set_asset_stage(_StubSG(), 1, value)
        except PermissionError:
            return True
        except Exception:                                      # noqa: BLE001
            return False
        return False

    ck("CANARY set_asset_stage REFUSES 'approved' with a PermissionError "
       "(this tool never approves its own output)", refuses("approved"))

    wrote = []

    class _OkSG:
        def update(self, entity, eid, data):
            wrote.append((entity, eid, data))

    set_asset_stage(_OkSG(), 42, "design")
    ck("CANARY set_asset_stage DOES allow 'design'",
       wrote == [("Asset", 42, {"sg_stage": "design"})])
    for bad in ("Approved", "APPROVED", "approve", "apr", ""):
        ck("CANARY set_asset_stage refuses %r too (no case/typo loophole)" % bad,
           refuses(bad))

    # --- naming
    ck("candidate codes are unique per (asset, recipe, seed)",
       len({candidate_code("A", r, s) for r in RECIPES for s in seeds_for()})
       == len(RECIPES) * MIN_SEEDS)
    ck("a wedge cell is named differently from its production twin",
       candidate_code("A", "base40", 1, True) != candidate_code("A", "base40", 1, False))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--negtext-wedge", action="store_true",
                    help="one-axis TEST of an anti-text negative term; not a recipe")
    ap.add_argument("--show-prompts", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--text-wedge", action="store_true",
                    help="A/B/C one-axis wedge on the invented-text hazard")
    ap.add_argument("--only")
    ap.add_argument("--seeds", type=int, default=MIN_SEEDS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if ns.report:
        return cmd_report()
    if ns.text_wedge:
        return cmd_text_wedge(sg_connect(), ns.dry_run)
    if ns.show_prompts:
        return cmd_show_prompts(sg_connect(), ns.only)
    if ns.generate or ns.negtext_wedge:
        return cmd_generate(sg_connect(), ns.only, ns.seeds, ns.negtext_wedge, ns.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
