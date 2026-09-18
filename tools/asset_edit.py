#!/usr/bin/env python3
r"""
POLICY, Geoff 2026-09-07, binding: **THIS IS NOT A REPAIR TOOL FOR A BAD RENDER.**
"Until editing is a real part of the pipeline we should not be editing to fix
renders. We need to use the pipeline or improve the pipeline."

A render that comes out wrong is fixed by changing what we SEND; if that cannot
fix it, the pipeline is what needs changing. An edit must not stand in for
either, because an edit lands only in the pixels: the Asset description then
stops describing what is approved, and the next regeneration silently reverts
it. Two Assets are already in that state and are being retired, see
docs/ROADMAP.md item 1 and P017.

Legitimate use is a deliberate creative change to an image that is already
right, and even then the instruction belongs back in sg_prompt_fragment.
Edit an Asset's APPROVED design instead of regenerating it.

WHY THIS EXISTS, measured 2026-09-04.

PilotCharA's approved anchor had garbled pseudo-lettering on the t-shirt, so every
panel composed from it inherited that text and no panel-prompt wording could
remove it. The design-revision loop fixed the cause correctly -- it changed one
clause of the design fragment and left everything else alone -- and then
re-rendered from scratch on fresh seeds. All eight candidates came back with
the shirt text gone AND the character off-model: orange skin instead of the
pale register, wrong hair colour, heavier outlines, one with misplaced glasses
and a malformed eye. The prompt was right; the seed re-roll destroyed a look
that had already been approved.

That is a bad trade for a small note, and small notes are most notes. Removing
lettering from an approved image is an EDIT: one thing changes and everything
else is preserved because the pixels are the input, not a description of them.

THE ASYMMETRY THIS CLOSES. Panels have edited an approved image with
Qwen-Image-Edit-2509 since Phase 5. Assets never did, because Anima is
text-to-image only and so asset work defaulted to regeneration. But nothing
required the EDIT to use Anima -- the compositor was already installed, proven
and pinned. It was simply never pointed at an Asset.

WHAT IT DOES NOT DO. It does not approve anything and it does not touch
Asset.sg_approved_design: the new image is published as one more candidate at
'rev' alongside the re-rolled ones, and the operator picks. Nothing
self-approves (invariant 7).

WHAT IT CANNOT FIX. An edit can only change what is visible. If a note asks for
something the anchor does not show at all -- a new costume, a different age --
regeneration is the right tool and this one refuses rather than pretending.
That judgement is the operator's; this tool does not classify.

    python asset_edit.py --self-test
    python asset_edit.py --asset SHOW_CHAR_PILOTCHARA --instruction "..." --dry-run
    python asset_edit.py --asset SHOW_CHAR_PILOTCHARA --instruction "..." --seeds 3
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

EXEC = os.path.join(HERE, "comfyui", "comfyui_execute.py")
COMFY_IN = r"C:\ComfyUI_windows_portable\ComfyUI\input"
COMFY_OUT = r"C:\ComfyUI_windows_portable\ComfyUI\output"

# Same resolution order as the other tools that live outside the tools dir:
# deploy.py stages tools/ into a versioned release tree and runs every
# module's --self-test there, where a repo-relative workflows path does not
# exist.
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


TPL = find_workflow("qwen_edit_single.api.json")
import sg_publish as PUB                    # invariant 11: one publish contract
PROJ = {"type": "Project", "id": 9999}

# a peer engineer-proven Qwen-Image-Edit recipe, copied from qwen_compose.py and NOT
# re-derived. cfg 1.0 is load-bearing: above it the result is pulled toward the
# TEXT, which has no idea what the character looks like, and away from the
# reference image -- which is the exact opposite of what an edit wants.
RECIPE = {
    "unet_name": "Qwen-Image-Edit-2509-Q3_K_M.gguf",
    "lora_name": "qwen_image_edit_2509_lightning_4steps_v1.safetensors",
    "lora_strength": "1.0",
    "clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "vae_name": "qwen_image_vae.safetensors",
    "shift": "3.0",
    "steps": "4",
    "cfg": "1.0",
}

# Composed and sent for shape parity with the other templates, and INERT at
# cfg 1.0 (proven pixel-identical, delta 0.000). Nothing should ever be
# expressed here that the edit actually depends on.
NEGATIVE = "blurry, distorted, watermark, low quality"

# The preservation clause. This is the whole reason an edit beats a re-render,
# so it is not left to the caller's instruction to remember.
# TWO PRESERVE CLAUSES, BECAUSE THERE ARE TWO KINDS OF ANCHOR. Written
# 2026-09-06 after six set edits came back with the room replaced by white.
#
# The original clause ended "and the same plain white background", which is
# exactly right for a CHARACTER turnaround, the case this tool was built for:
# those really do sit on a white ground. Applied to a SET anchor it is an
# instruction to whiten the room, and that is what the model did. It also
# explains the dose response recorded in F059: the harder the edit works, the
# more of the room gets redrawn, and the more of it that clause reaches.
#
# Nothing about the model was at fault. Read the composed prompt first.
PRESERVE_CHAR = ("Keep everything else in the image exactly as it is: the same "
                 "character, the same face, the same hair colour and shape, the "
                 "same skin tone, the same palette, the same line weight and "
                 "shading style, the same poses, the same framing and the same "
                 "plain white background. Change nothing except what is asked "
                 "for above")

# For a set, the envelope is NAMED rather than described as absent, per F058:
# an instruction to leave a region empty gives the model nothing to draw, and
# what is not positively asserted is what goes missing.
PRESERVE_SET = ("Keep the rest of the room exactly as it is: the same walls "
                "with their colour and their posters, the same window with the "
                "same night sky beyond it, the same ceiling and light fitting, "
                "the same floorboards running to the skirting, the same palette, "
                "the same line weight and shading style, and the same framing. "
                "It is an interior; every edge of the frame stays inside the "
                "room. Change nothing except what is asked for above")


def preserve_for(asset):
    """-> the right clause for this KIND of anchor.

    Keyed on the Asset, not on the caller remembering, because the failure it
    prevents is silent: a set edit with the character clause returns a white
    void and looks like a model limitation."""
    code = (asset or {}).get("code") or ""
    kind = (asset or {}).get("sg_asset_type") or ""
    is_set = "_SET_" in code.upper() or kind.lower() == "environment"
    return PRESERVE_SET if is_set else PRESERVE_CHAR


PRESERVE = PRESERVE_CHAR   # back-compat for anything importing the old name


def log(m):
    print("[asset_edit] %s" % m, flush=True)


def build_prompt(instruction, asset=None):
    """The edit instruction plus the preservation clause, in that order.

    Instruction first because the model weights the opening of the prompt more
    heavily, and preservation is the default we are protecting rather than the
    change we are asking for."""
    ins = (instruction or "").strip().rstrip(".")
    if not ins:
        raise SystemExit("an empty instruction would re-render the image for "
                         "no reason -- refusing")
    return "%s. %s." % (ins, preserve_for(asset) if asset else PRESERVE_CHAR)


def approved_anchor(sg, asset_code):
    """-> (asset, version, media_path). Refuses rather than guessing."""
    a = sg.find_one("Asset", [["project", "is", PROJ],
                              ["code", "is", asset_code]],
                    ["code", "sg_approved_design", "sg_stage"])
    if not a:
        raise SystemExit("no Asset %s in project 9999" % asset_code)
    ap = a.get("sg_approved_design")
    if not isinstance(ap, dict):
        raise SystemExit("%s has no sg_approved_design. There is nothing to "
                         "edit -- this tool edits an APPROVED design, and a "
                         "design that has never been approved should be "
                         "regenerated, not patched." % asset_code)
    v = sg.find_one("Version", [["id", "is", ap["id"]]],
                    ["code", "sg_path_to_movie", "sg_status_list"])
    p = (v or {}).get("sg_path_to_movie")
    if not p or not os.path.isfile(p):
        raise SystemExit("%s -> %s has no media on disk (%s). Refusing to "
                         "edit an image that is not there."
                         % (asset_code, (v or {}).get("code"), p))
    return a, v, p


def edited_outputs(prefix, out_dir=None, listing=None):
    """-> [(wedge_index, path)] for the files this run produced, sorted.

    Separated from run() so the mapping is testable without a GPU: it is the
    step that decides WHAT gets published, and getting it wrong publishes the
    wrong image or nothing at all."""
    out_dir = out_dir or COMFY_OUT
    names = listing if listing is not None else (
        os.listdir(out_dir) if os.path.isdir(out_dir) else [])
    found = []
    for n in names:
        if not n.startswith(prefix + "_w") or not n.endswith(".png"):
            continue
        tail = n[len(prefix) + 2:]
        idx = tail.split("_", 1)[0]
        if idx.isdigit():
            found.append((int(idx), os.path.join(out_dir, n)))
    # ONE FILE PER WEDGE. ComfyUI appends its own counter per prefix, so a
    # second run at the same seeds writes _00002_ beside _00001_. Measured on
    # this project: the two are byte-identical, because the same seed and the
    # same prompt are deterministic. But publishing both mapped them onto ONE
    # seed-derived Version code, so the second silently overwrote the first,
    # and it was only harmless because they matched. Keeping the newest makes
    # that a decision instead of a coincidence.
    newest = {}
    for idx, path in found:
        if idx not in newest or _mtime(path) >= _mtime(newest[idx]):
            newest[idx] = path
    return sorted(newest.items())


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def publish_edits(sg, asset, anchor, prompt, produced, seed_base, log=log):
    """Publish each edited image as a candidate Version at 'rev'. -> [Version].

    WHY THIS FUNCTION HAD TO BE WRITTEN, 2026-09-06. This module's docstring
    has said since it was created that the edited image "is published as one
    more candidate at 'rev'". It was not: the word `publish` appeared in that
    docstring and nowhere else in the file. Six edited anchors were rendered
    before anyone checked, and zero Versions existed.

    That is worse than an obviously missing feature, because the documentation
    reads as evidence the step happens, and Geoff's whole upstream-fix plan
    depends on it: an edit that never reaches ShotGrid cannot invalidate
    anything downstream, so nothing regenerates.

    Nothing is approved here (invariant 7). Candidates land at 'rev' beside the
    re-rolled ones and the operator picks. Stage is 'keyframe', matching
    anima_anchor.py, so an approved sibling still sweeps the losers.
    """
    published = []
    for idx, path in produced:
        code = "%s_EDIT_s%d" % (asset["code"], seed_base + idx - 1)
        v = PUB.publish_version(
            sg, project=PROJ, entity={"type": "Asset", "id": asset["id"]},
            code=code, media_path=path, status="rev", stage="keyframe",
            # THE BATCH, declared at publish time. One seed base is one run, so
            # two batches of the same room stay separable in review instead of
            # being told apart by a substring of the Version code.
            wedge_group="%s_EDIT_s%d" % (asset["code"], seed_base),
            description=("Edit of approved anchor %s. Instruction as sent is in "
                         "sg_prompt_final__as_sent_. Nothing is approved by this "
                         "tool: this is one candidate among the seeds."
                         % anchor.get("code")),
            extra_fields={"sg_prompt_final__as_sent_": prompt},
            character=("n/a - set design edit, no character in frame"),
            set_=asset["code"],
            action="single-image edit of an already-approved anchor, seed %d" % (seed_base + idx - 1),
            camera="no camera move; edit preserves the anchor's framing",
            style="inherits the approved anchor's register; the edit is not a restyle",
            log=log)
        published.append(v)
        log("  published %s as Version %s (rev)" % (code, v["id"]))
    return published


def run(sg, asset_code, instruction, seeds=3, seed_base=53000, dry=False):
    a, v, src = approved_anchor(sg, asset_code)
    prompt = build_prompt(instruction, asset=a)
    log("%s: editing approved anchor %s" % (asset_code, v["code"]))
    log("  instruction: %s" % instruction.strip()[:140])
    dst_name = "assetedit_" + os.path.basename(src)
    if not dry:
        shutil.copyfile(src, os.path.join(COMFY_IN, dst_name))
    prefix = "%s_EDIT" % v["code"]
    subs = dict(RECIPE)
    subs.update({"image1": dst_name, "prompt": prompt,
                 "negative_prompt": NEGATIVE})
    cmd = [sys.executable, EXEC, "1", str(seeds), "1",
           "--workflow", TPL, "--output-prefix", prefix,
           "--seed-base", str(seed_base),
           "--comfy-output-dir", COMFY_OUT, "--expect-outputs", "1"]
    for k, val in sorted(subs.items()):
        cmd += ["--set", "%s=%s" % (k, val)]
    if dry:
        log("  would run %d seed(s); prompt: %s" % (seeds, prompt[:180]))
        return 0
    rc = subprocess.call(cmd)
    if rc != 0:
        log("  render returned %d: nothing published" % rc)
        return rc
    produced = edited_outputs(prefix)
    if not produced:
        log("  RENDER SUCCEEDED BUT NO OUTPUT FILES MATCHED %s_w*.png -- nothing "
            "published. That combination is a bug, not an empty result." % prefix)
        return 3
    publish_edits(sg, a, v, prompt, produced, seed_base)
    return 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("the single-image edit template exists", os.path.isfile(TPL))
    ck("the executor exists", os.path.isfile(EXEC))

    import json
    import re
    txt = open(TPL, encoding="utf-8").read()
    doc = json.loads(re.sub(r"\{[a-z_0-9]+\}", "0", txt))
    ck("every top-level key in the template is a node with a class_type",
       all(isinstance(x, dict) and "class_type" in x for x in doc.values()))
    need = set(re.findall(r"\{([a-z_0-9]+)\}", txt))
    have = set(RECIPE) | {"image1", "prompt", "negative_prompt",
                          "seed", "output_prefix"}
    ck("every template placeholder is substituted (missing: %s)"
       % sorted(need - have), not (need - have))
    ck("the template edits ONE image (this is not the compose graph)",
       doc["8"]["inputs"].get("image1") == ["7", 0]
       and "image2" not in doc["8"]["inputs"])
    ck("the latent comes from the anchor's own pixels, so it is an edit",
       doc["10"]["inputs"]["pixels"] == ["7", 0])

    p = build_prompt("remove the lettering from the t-shirt")
    ck("the instruction leads the prompt", p.startswith("remove the lettering"))
    ck("the preservation clause is always appended, never left to the caller",
       "Change nothing except what is asked for above" in p)
    ck("a trailing full stop in the instruction is not doubled",
       ".." not in build_prompt("remove the lettering."))

    try:
        build_prompt("   ")
        ck("an empty instruction is refused, not sent", False)
    except SystemExit:
        ck("an empty instruction is refused, not sent", True)

    ck("cfg is pinned at 1.0 (above it the edit drifts toward the text)",
       RECIPE["cfg"] == "1.0")
    ck("the recipe still matches qwen_compose.py's pinned values",
       RECIPE["steps"] == "4" and RECIPE["shift"] == "3.0")

    class _Stub(object):
        def __init__(self, asset):
            self.asset = asset

        def find_one(self, et, *a, **k):
            if et == "Asset":
                return self.asset
            return {"id": 9, "code": "ANCHOR",
                    "sg_path_to_movie": os.path.abspath(__file__),
                    "sg_status_list": "apr"}

    def refuses(asset, fragment):
        try:
            approved_anchor(_Stub(asset), "X")
        except SystemExit as exc:
            return fragment in str(exc)
        return False

    ck("an Asset with no approved design is REFUSED, not regenerated blind",
       refuses({"code": "X", "sg_approved_design": None},
               "no sg_approved_design"))
    ck("a missing Asset is refused by name",
       refuses(None, "no Asset X"))

    class _NoFile(_Stub):
        def find_one(self, et, *a, **k):
            if et == "Asset":
                return self.asset
            return {"id": 9, "code": "ANCHOR",
                    "sg_path_to_movie": r"C:\nope\missing.png",
                    "sg_status_list": "apr"}
    try:
        approved_anchor(_NoFile({"code": "X",
                                 "sg_approved_design": {"id": 1}}), "X")
        ck("an approved design whose media is gone is refused", False)
    except SystemExit as exc:
        ck("an approved design whose media is gone is refused",
           "not there" in str(exc))

    # THE CLAUSE THAT WHITENED SIX ROOMS. A set anchor must never be told it
    # has a plain white background; that is a character-turnaround clause and
    # it read as an instruction.
    _set = {"code": "SHOW_SET_PILOTCHARA_BEDROOM", "sg_asset_type": "Environment"}
    _char = {"code": "SHOW_CHAR_PILOTCHARA", "sg_asset_type": "Prop"}
    ck("CANARY: a SET edit is never told the background is plain white",
       "plain white background" not in build_prompt("x", _set))
    ck("a CHARACTER edit still gets the white-ground clause it needs",
       "plain white background" in build_prompt("x", _char))
    ck("a set edit NAMES the room's envelope rather than describing it as empty",
       all(w in build_prompt("x", _set) for w in ("walls", "window", "ceiling", "floorboards")))
    ck("CANARY: kind is decided by the ASSET, not by the caller remembering",
       preserve_for({"code": "SHOW_SET_X"}) is PRESERVE_SET
       and preserve_for({"code": "X", "sg_asset_type": "Environment"}) is PRESERVE_SET
       and preserve_for({"code": "SHOW_CHAR_X", "sg_asset_type": "Prop"}) is PRESERVE_CHAR)

    # THE STEP THIS MODULE CLAIMED TO HAVE AND DID NOT. Its docstring promised
    # a published candidate; six anchors rendered with zero Versions created.
    # These canaries are on the mapping, because choosing the wrong file is how
    # a publish step silently publishes the wrong image.
    listing = ["P_w001_00001_.png", "P_w002_00001_.png", "P_w003_00002_.png",
               "P_w002_00002_.png", "OTHER_w001_00001_.png", "P_w001_00001_.txt",
               "P_notawedge.png"]
    got = edited_outputs("P", out_dir="X", listing=listing)
    ck("CANARY: ONE file per wedge, not one per ComfyUI counter",
       [i for i, _ in got] == [1, 2, 3])
    ck("CANARY: another asset's outputs are NOT picked up",
       not any("OTHER" in pth for _, pth in got))
    ck("CANARY: a non-png beside them is ignored",
       not any(pth.endswith(".txt") for _, pth in got))
    ck("CANARY: a file without a wedge index is ignored",
       not any("notawedge" in pth for _, pth in got))
    ck("an empty output dir yields nothing rather than raising",
       edited_outputs("P", out_dir="X", listing=[]) == [])

    # And the code a published candidate gets must be derived from the SEED, so
    # two runs at different seed bases cannot collide on one Version code.
    codes = ["%s_EDIT_s%d" % ("A", 77000 + i - 1) for i, _ in got]
    ck("candidate codes are seed-derived and unique per wedge",
       len(set(codes)) == len(set(i for i, _ in got)))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset")
    ap.add_argument("--instruction", default="")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed-base", type=int, default=53000)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if not a.asset:
        raise SystemExit("--asset is required")
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), a.asset, a.instruction, seeds=a.seeds,
               seed_base=a.seed_base, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
