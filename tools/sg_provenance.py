#!/usr/bin/env python3
"""THE one place every publisher writes D6 provenance onto a Version.

D6 (MASTER-PLAN-V2.md decision ledger): "Every publish carries/links: prompt
components (character/set/action/camera/style), anchor Version link,
seed/steps/cfg/model, workflow template + hash. Fields, not new component
entities." Invariant 9: "Provenance on every Version. A publish without
provenance is a bug." Invariant 11 ("one classifier") is the model here too:
this module is the ONLY place that knows the real ShotGrid field codes for the
D6 fields, and the only place that decides what counts as "missing." Every
publisher imports this; none should carry its own copy of the field codes.

Real field codes (read back after schema_field_create per invariant 8 -
ShotGrid renamed every one of the text fields on creation):

    requested                  -> real code
    sg_anchor_version           -> sg_anchor_version            (entity->Version)
    sg_component_character      -> sg_component__character      (text)
    sg_component_set            -> sg_component__set            (text)
    sg_component_action         -> sg_component__action         (text)
    sg_component_camera         -> sg_component__camera         (text)
    sg_component_style          -> sg_component__style           (text)
    sg_workflow_hash             -> sg_workflow_hash__sha256_    (text)

seed/steps/cfg/model already have their own established fields from an earlier
provenance pass (sg_gen_seed, sg_gen_steps, sg_gen_cfg, sg_model on Version) -
those are NOT duplicated here; this module owns exactly the six D6 fields that
did not exist before Phase 8.

Every text field must carry a REAL, non-empty value on every publish. When a
component genuinely does not apply to a given publisher (e.g. a character
sheet has no "set", a post-color-grade pass has no "camera" move), the caller
must say so explicitly ("n/a - <reason>") rather than pass "". An empty string
and "n/a - first-generation design, no set composited" both read as present to
a naive truthiness check, but only the second one tells a human reading
ShotGrid that the blank was a decision, not an oversight. missing_fields()
below treats "" and None as MISSING either way, precisely so that lazily
passing "" gets caught by the preflight canary instead of shipping quietly.
"""
import hashlib
import io

# ---------------------------------------------------------------- real field codes
F_ANCHOR = "sg_anchor_version"
F_CHARACTER = "sg_component__character"
F_SET = "sg_component__set"
F_ACTION = "sg_component__action"
F_CAMERA = "sg_component__camera"
F_STYLE = "sg_component__style"
F_WORKFLOW_HASH = "sg_workflow_hash__sha256_"

TEXT_FIELDS = [F_CHARACTER, F_SET, F_ACTION, F_CAMERA, F_STYLE, F_WORKFLOW_HASH]
ALL_FIELDS = TEXT_FIELDS + [F_ANCHOR]


# ---------------------------------------------------------------- workflow hash
def render_template_text(template_path, subs):
    """Read the raw ComfyUI workflow template and apply the SAME {name}
    substitution comfyui_execute.py's render_template() does (text.replace,
    one pass, whatever the caller's own --set flags actually were), so the
    hash reflects what was actually sent for this specific publish. Any
    {placeholder} the caller did not supply is left as literal text: this is a
    reproducibility fingerprint, not a submission, so an unresolved token is
    not an error here (comfyui_execute.py already refused the real submission
    if one was left over)."""
    text = io.open(template_path, encoding="utf-8").read()
    for k, v in sorted(subs.items()):
        text = text.replace("{%s}" % k, str(v))
    return text


def workflow_hash_from_template(template_path, subs):
    """sha256 of the filled workflow template, per D6."""
    text = render_template_text(template_path, subs)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def workflow_hash_from_text(text):
    """For publishers with no JSON ComfyUI template at all (pure-ffmpeg passes:
    grading, upres chaining, concatenation). Hashes a deterministic description
    of the recipe instead, so two runs with identical inputs/parameters produce
    the identical hash, and a changed filter/command produces a different one."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- classify
def classify_assets(sg, shot, log=print):
    """Split the Shot's linked Assets into character / set codes via
    sg_ref_role, for the sg_component_character / sg_component_set provenance
    fields. This tool predates the Phase 5 panel stage (which will carry
    explicit per-component fields on the Shot/Beat); until then, the linked
    Asset library is the best-available source of "what character/set fed this
    generation." An asset with no role, or no role match, is not silently
    dropped: it lands in `other` so nothing that was actually linked goes
    unrecorded.

    Used to live as two near-identical copies (genvideo_worker.py,
    animatic.py) that had already drifted: one caught an Asset-lookup
    exception, the other did not. This is the one shared copy, per invariant
    11's spirit ("one place... import it, never copy") and D6/invariant 9.

    The Asset-lookup itself IS wrapped in try/except, because a transient SG
    hiccup here must not crash generation over provenance metadata (the same
    "don't lose the shot's work over a metadata failure" reasoning as
    write_provenance() below). But per invariant 3 ("loud failures"), that
    catch must not swallow silently the way one of the two original copies
    did: a lookup failure is logged loudly before degrading to "nothing
    classified", so it shows up in the service log instead of silently
    mis-attributing a Version's provenance as empty."""
    links = shot.get("assets") or []
    if not links:
        return [], [], []
    try:
        rows = sg.find("Asset", [["id", "in", [a["id"] for a in links]]],
                       ["code", "sg_ref_role"])
    except Exception as exc:                      # noqa: BLE001
        log("  ASSET CLASSIFY LOOKUP FAILED on Shot %s: %s"
            % (shot.get("id"), type(exc).__name__))
        return [], [], []
    char = [r["code"] for r in rows if (r.get("sg_ref_role") or "").lower() == "character"]
    setl = [r["code"] for r in rows if (r.get("sg_ref_role") or "").lower() in ("set", "style")]
    known = set(char) | set(setl)
    other = [r["code"] for r in rows if r["code"] not in known]
    return char, setl, other


# ---------------------------------------------------------------- write
def build_fields(character="", set_="", action="", camera="", style="",
                  workflow_hash="", anchor_version_id=None):
    """The ShotGrid update dict for one Version's D6 fields."""
    data = {
        F_CHARACTER: character or "",
        F_SET: set_ or "",
        F_ACTION: action or "",
        F_CAMERA: camera or "",
        F_STYLE: style or "",
        F_WORKFLOW_HASH: workflow_hash or "",
    }
    if anchor_version_id:
        data[F_ANCHOR] = {"type": "Version", "id": anchor_version_id}
    return data


def write_provenance(sg, version_id, character="", set_="", action="", camera="",
                     style="", workflow_hash="", anchor_version_id=None, log=print):
    """Write the D6 fields onto an already-created Version. Best effort: a
    write failure must be LOUD (mirrors invariant 3's "loud failure" spirit)
    but must not retroactively unpublish the artifact - the Version already
    exists either way, and a silent provenance gap is a defect to go fix, not
    a reason to lose the shot's work. Returns True/False so callers can log."""
    data = build_fields(character, set_, action, camera, style,
                        workflow_hash, anchor_version_id)
    try:
        sg.update("Version", version_id, data)
        return True
    except Exception as exc:                      # noqa: BLE001
        log("  PROVENANCE WRITE FAILED on Version %s: %s" % (version_id, type(exc).__name__))
        return False


# ---------------------------------------------------------------- check (preflight/canary)
def missing_fields(version_row, require_anchor=False):
    """version_row: a dict as returned by sg.find/find_one, requested with (at
    least) ALL_FIELDS. Returns the list of REAL field codes that are blank or
    absent. This is the single function both sg_preflight.py and the Phase 8
    canary tests call, so "what counts as missing" cannot drift between them."""
    missing = []
    for f in TEXT_FIELDS:
        v = version_row.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(f)
    if require_anchor and not version_row.get(F_ANCHOR):
        missing.append(F_ANCHOR)
    return missing


# ---------------------------------------------------------------------- self-test
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-62s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("field codes are the REAL ones (double underscore renames present)",
       F_CHARACTER == "sg_component__character"
       and F_WORKFLOW_HASH == "sg_workflow_hash__sha256_"
       and F_ANCHOR == "sg_anchor_version")
    ck("TEXT_FIELDS has exactly the 6 text fields, no anchor",
       len(TEXT_FIELDS) == 6 and F_ANCHOR not in TEXT_FIELDS)
    ck("ALL_FIELDS adds the anchor back", F_ANCHOR in ALL_FIELDS and len(ALL_FIELDS) == 7)

    h1 = workflow_hash_from_text("recipe A")
    h2 = workflow_hash_from_text("recipe A")
    h3 = workflow_hash_from_text("recipe B")
    ck("workflow_hash_from_text is deterministic", h1 == h2)
    ck("workflow_hash_from_text is sensitive to input", h1 != h3)
    ck("workflow_hash_from_text produces hex sha256 (64 chars)", len(h1) == 64)

    fields = build_fields(character="CHARB", set_="CHARB_FLAT", action="walks in",
                          camera="wide static", style="1980s skate-deck matte",
                          workflow_hash=h1, anchor_version_id=4242)
    ck("build_fields sets all 6 text fields", all(fields.get(f) for f in TEXT_FIELDS))
    ck("build_fields links the anchor as an entity dict",
       fields.get(F_ANCHOR) == {"type": "Version", "id": 4242})

    no_anchor = build_fields(character="CHARB", set_="CHARB_FLAT", action="a",
                             camera="c", style="s", workflow_hash=h1)
    ck("build_fields omits the anchor key entirely when none given",
       F_ANCHOR not in no_anchor)

    complete = {f: "x" for f in TEXT_FIELDS}
    complete[F_ANCHOR] = {"type": "Version", "id": 1}
    ck("missing_fields reports nothing missing on a complete row",
       missing_fields(complete) == [])
    ck("missing_fields reports nothing missing when anchor not required and absent",
       missing_fields({f: "x" for f in TEXT_FIELDS}, require_anchor=False) == [])

    # CANARY: a blanked field must be caught. This is the fixture that proves
    # the check can actually FAIL (invariant 2) - not decoration.
    blanked = dict(complete)
    blanked[F_STYLE] = ""
    ck("CANARY: a blanked text field is reported missing",
       missing_fields(blanked) == [F_STYLE])

    blanked_ws = dict(complete)
    blanked_ws[F_CAMERA] = "   "
    ck("CANARY: a whitespace-only field counts as missing, not present",
       missing_fields(blanked_ws) == [F_CAMERA])

    no_anchor_row = dict(complete)
    del no_anchor_row[F_ANCHOR]
    ck("CANARY: missing anchor is caught ONLY when required",
       missing_fields(no_anchor_row, require_anchor=True) == [F_ANCHOR]
       and missing_fields(no_anchor_row, require_anchor=False) == [])

    class _FailSG:
        def update(self, *a, **k):
            raise RuntimeError("boom")

    logged = []
    ok = write_provenance(_FailSG(), 999, character="x", set_="x", action="x",
                          camera="x", style="x", workflow_hash="x",
                          log=lambda m: logged.append(m))
    ck("write_provenance returns False on an SG error instead of raising", ok is False)
    ck("write_provenance logs the failure LOUDLY instead of swallowing it",
       len(logged) == 1 and "PROVENANCE WRITE FAILED" in logged[0])

    class _StubSG:
        def find(self, entity, filters, fields):
            return [{"code": "CHAR_A", "sg_ref_role": "character"},
                    {"code": "STYLE_A", "sg_ref_role": "style"},
                    {"code": "SET_A", "sg_ref_role": "set"},
                    {"code": "REF_UNROLED", "sg_ref_role": None}]

    ck("classify_assets returns ([], [], []) when nothing is linked",
       classify_assets(_StubSG(), {"assets": []}) == ([], [], []))
    char, setl, other = classify_assets(_StubSG(), {"id": 1, "assets": [{"id": 9}]})
    ck("classify_assets splits character / (set+style) / unrecorded-other",
       char == ["CHAR_A"] and setl == ["STYLE_A", "SET_A"] and other == ["REF_UNROLED"])

    class _FailFindSG:
        def find(self, *a, **k):
            raise RuntimeError("boom")

    logged2 = []
    result = classify_assets(_FailFindSG(), {"id": 7, "assets": [{"id": 9}]},
                             log=lambda m: logged2.append(m))
    ck("CANARY: classify_assets degrades to ([], [], []) on an Asset-lookup error",
       result == ([], [], []))
    ck("CANARY: classify_assets logs the lookup failure LOUDLY instead of "
       "swallowing it (invariant 3) - this is the drift fix: one of the two "
       "original copies swallowed this silently",
       len(logged2) == 1 and "ASSET CLASSIFY LOOKUP FAILED" in logged2[0])

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
