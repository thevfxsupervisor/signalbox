#!/usr/bin/env python3
"""Give the show a home in ShotGrid: the Episode entity, and the documents.

Two standing gaps, both versions of the same complaint - the site is not
self-contained:

    Stage 0: "the bible lives in the repo, not in ShotGrid. A human on the site
              cannot see it."
    Stage 1: "no Script entity, no versioning of the script inside ShotGrid."

And a structural one nobody had flagged: the project had Sequences and Shots but
no **Episode**, so the standard ShotGrid hierarchy was missing its top level.
Episode -> Sequence -> Shot is the convention every episodic pipeline uses, and
without the Episode there is no page that means "the whole show" - which is
exactly the page a producer opens first.

This creates it, links the acts to it, records the format facts from the bible
as real fields (so they are filterable, not prose), and attaches the bible, the
script and the subtitle track so they can be read from the site.

The runtime figure is MEASURED from the animatic Version, not copied from the
bible's target. A page that shows the intended runtime next to the actual one is
useful; a page that shows the intention twice is decoration.

    python episode_setup.py --build
    python episode_setup.py --self-test
"""
import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_context as EPCTX                                # noqa: E402

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
def _content_root():
    """-> the tree that holds `creative/`, which is NOT always this file's repo.

    THE TRAP, found by a review agent on 2026-09-07 before it could fire.
    `dirname(dirname(__file__))` looks like "the repo" and is, in the working
    tree. But `tools/deploy.py` stages ONLY `tools/` into
    `build/releases/<id>/tools/`, so from a deployed release that expression
    resolves to the RELEASE directory, which has no `creative/` in it. Worse
    than a wrong path: deploy's preflight runs every staged module's own
    --self-test and refuses to activate a release whose tests fail, so this
    would have failed its own gate and blocked EVERY future deploy.

    Prefer the tree this file sits in (so a future move of the repo needs no
    edit here), fall back to the project root (so a deployed release still
    finds the content that was never staged with it)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (here, ROOT):
        if os.path.isdir(os.path.join(cand, "creative")):
            return cand
    return ROOT

REPO = _content_root()
BIBLE = os.path.join(REPO, "creative", "SHOW-BIBLE.md")
PROJ = {"type": "Project", "id": 9999}

EP = EPCTX.resolve_episode()
EP_CODE = EP.code
ACTS = EP.acts
# SCRIPT/SRT come from the episode registry so a new show's own script and
# subtitle sidecar get attached, not PILOT01's. LOGLINE/FORMAT/TARGET below
# remain PILOT01's own creative content (see the module-level note further
# down): this tool bootstraps ONE episode's ShotGrid home per run and that
# content is authored prose, not an identifier this refactor's config layer
# is meant to abstract.
SCRIPT = EP.script or os.path.join(REPO, "creative", "PILOT-SCRIPT.md")
SRT = EP.srt_path

# Facts from the bible that belong in fields rather than prose.
FIELDS = {
    "sg_logline": ("text", None),
    "sg_format": ("text", None),
    "sg_target_seconds": ("number", None),
    "sg_runtime_seconds": ("number", None),
    "sg_runtime_note": ("text", None),
}

# PER-EPISODE CONTENT. These were bare module constants, so `--build` stamped
# PILOT01's marionette logline, format and target onto ANY episode it was run
# for -- SHOW01 included. Keyed by episode code now, and an unregistered code
# REFUSES rather than falling back (invariant 3: loud failure). A wrong logline
# on the wrong show is the kind of defect nobody notices until a delivery.
#
# SHOW01 is deliberately absent: its logline and format were written straight
# onto Episode 1687 during setup, and inventing a second copy here would create
# two sources of truth for one fact. `content_for()` reads the live record for
# any episode not listed, and only falls back to these constants for PILOT01,
# whose values predate that convention.
_PILOT01_LOGLINE = ("[REDACTED FOR THIS PUBLIC SNAPSHOT: the original constant held a real "
           "pilot's logline, a couple of sentences of prose. Replaced with this placeholder "
           "so the fallback mechanism below still has a value to demonstrate against.]")
_PILOT01_FORMAT = ("[REDACTED FOR THIS PUBLIC SNAPSHOT: the original constant held the real "
          "pilot's format description, naming its medium (a puppet-animation format) and its "
          "tone (a sitcom) -- both replaced with this placeholder, kept generic on purpose.]")
_PILOT01_TARGET = 585  # 9:45, the bible's target

CONTENT = {
    "PILOT01": {"logline": _PILOT01_LOGLINE, "format": _PILOT01_FORMAT,
               "target": _PILOT01_TARGET},
}


def content_for(sg, ep):
    """-> (logline, format, target) for this episode.

    A registered episode uses its constants. Any other reads the LIVE ShotGrid
    record, because that is where the fact already lives. If neither has it,
    REFUSE -- never inherit another show's content."""
    ent = CONTENT.get(ep.code)
    if ent:
        return ent["logline"], ent["format"], ent["target"]
    row = sg.find_one("Episode", [["project", "is", PROJ], ["code", "is", ep.code]],
                      ["sg_logline", "sg_format", "sg_target_seconds"])
    if not row:
        sys.exit("FATAL: episode %r has no entry in CONTENT and no Episode record "
                 "in ShotGrid. Refusing to build rather than stamp another "
                 "show's logline onto it." % ep.code)
    lg, fm, tg = (row.get("sg_logline"), row.get("sg_format"),
                  row.get("sg_target_seconds"))
    missing = [n for n, v in (("sg_logline", lg), ("sg_format", fm),
                              ("sg_target_seconds", tg)) if not v]
    if missing:
        sys.exit("FATAL: episode %r is missing %s on its ShotGrid record, and "
                 "this tool will not invent them. Populate the Episode, or add "
                 "%r to CONTENT." % (ep.code, ", ".join(missing), ep.code))
    return lg, fm, tg


def _src_of(fn):
    """Source of `fn`, for canaries that must assert on the QUERY rather than
    on a return value -- an unscoped filter returns a plausible number, so the
    number cannot be the evidence."""
    import inspect
    return inspect.getsource(fn)


def log(m):
    print("[episode] %s" % m, flush=True)


def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


def ensure_fields(sg):
    """Create the Episode fields we need. ShotGrid RENAMES field codes on
    creation (sg_prompt_final became sg_prompt_final__as_sent_ earlier in this
    project), so the real name is read back and used, never assumed."""
    have = sg.schema_field_read("Episode")
    real = {}
    for name, (dtype, _) in FIELDS.items():
        if name in have:
            real[name] = name
            continue
        disp = name[3:].replace("_", " ").title()
        try:
            got = sg.schema_field_create("Episode", dtype, disp)
            real[name] = got
            log("  created Episode.%s (asked for %s)" % (got, name))
        except Exception as exc:
            log("  could not create %s: %s" % (name, str(exc)[:90]))
    return real


# F462, fixed a second time (Geoff, 2026-09-08): "Assets have an Episodes
# field, use that for style link, taking the earliest episode." An Asset has
# no Sequence and no Shot, so an Asset-level style read needs a field ON
# EPISODE to resolve to. Sequence already carries sg_style_prefix/suffix/
# negative (read by the SHOT path: animatic.py, genvideo_worker.py,
# prompt_revision.py - untouched by this), but Episode had NONE of the three
# -- schema_field_read('Episode') checked live 2026-09-08 (this project's
# rule 1: query the schema, never assume it), no sg_style_* key present at
# all. This mirrors them onto Episode, same type and display name as the
# Sequence originals, which were also read back live rather than assumed:
# schema_field_read('Sequence', ...) confirmed all three are plain 'text'
# fields, display names "Style Prefix" / "Style Suffix" / "Style Negative".
# See tools/character_sheets.py's style_prefix() for the reader this feeds.
EPISODE_STYLE_FIELDS = [
    ("sg_style_prefix", "Style Prefix", "text", None),
    ("sg_style_suffix", "Style Suffix", "text", None),
    ("sg_style_negative", "Style Negative", "text", None),
]


def setup_style_schema(sg):
    """Idempotent: create Episode.sg_style_prefix/suffix/negative if absent,
    mirroring Sequence's fields of the same name exactly (pattern: tools/
    prompt_revision.py's setup_asset_schema() -- schema_field_read, create
    only what is missing, read the real code back rather than trust the
    requested one). An existing field is reported and left untouched, never
    recreated, so a second run is a no-op. ShotGrid may rename a field on
    create (it has before in this project -- sg_auto_apply became
    sg_auto_apply_proposals); if that happens here the real code is logged
    LOUDLY, because character_sheets.py's style_prefix() reads these three
    codes by the constant name, not by a schema lookup."""
    sch = sg.schema_field_read("Episode")
    for code, display, dtype, props in EPISODE_STYLE_FIELDS:
        if code in sch:
            log("Episode.%s already exists (%s) -- no-op"
                % (code, sch[code]["data_type"]["value"]))
            continue
        real = sg.schema_field_create("Episode", dtype, display, properties=props)
        log("Episode field created: requested display %r -> REAL CODE %r" % (display, real))
        if real != code:
            log("  WARNING: ShotGrid renamed it. character_sheets.py's "
                "style_prefix() expects %r; update it before relying on "
                "this field." % code)
    return sg.schema_field_read("Episode")


def measured_runtime(sg, ep=None):
    """-> (seconds, version_code) from the newest episode-stage Version OF THIS
    EPISODE, or (None, None). Measured, never assumed.

    THE DEFECT THIS FIXES: the query filtered on project + stage only, with no
    episode scope. On a project holding two shows it returned whichever episode
    cut was newest -- it handed back PILOT01's animatic runtime as SHOW01's on
    the first run for the second show. A number that is real, and about the
    wrong thing, is worse than no number."""
    ep = ep or EPCTX.resolve_episode()
    row = sg.find_one("Episode", [["project", "is", PROJ], ["code", "is", ep.code]], ["id"])
    if not row:
        return None, None
    vs = sg.find("Version", [["project", "is", PROJ], ["sg_stage", "is", "episode"],
                             ["entity", "is", {"type": "Episode", "id": row["id"]}]],
                 ["code", "sg_first_frame", "sg_last_frame", "created_at"])
    if not vs:
        return None, None
    vs.sort(key=lambda v: v.get("created_at") or 0)
    v = vs[-1]
    first, last = v.get("sg_first_frame"), v.get("sg_last_frame")
    if first is None or last is None:
        return None, v["code"]
    return round((last - first) / 24.0, 2), v["code"]


def cmd_build(sg, dry):
    real = ensure_fields(sg)

    ep = sg.find_one("Episode", [["project", "is", PROJ], ["code", "is", EP_CODE]], ["code"])
    if ep is None:
        if dry:
            log("DRY RUN: would create Episode %s" % EP_CODE)
            return 0
        ep = sg.create("Episode", {"project": PROJ, "code": EP_CODE,
                                   "description": "The pilot."})
        log("created Episode %s (id %d)" % (EP_CODE, ep["id"]))
    else:
        log("Episode %s exists (id %d)" % (EP_CODE, ep["id"]))

    ep = EPCTX.resolve_episode()
    logline, fmt, target = content_for(sg, ep)
    secs, vcode = measured_runtime(sg, ep)
    data = {"description": "%s\n\n%s" % (logline, fmt)}
    if real.get("sg_logline"):
        data[real["sg_logline"]] = logline
    if real.get("sg_format"):
        data[real["sg_format"]] = fmt
    if real.get("sg_target_seconds"):
        data[real["sg_target_seconds"]] = target
    if secs is not None and real.get("sg_runtime_seconds"):
        # ShotGrid's "number" is an INTEGER field; handing it 590.5 is a hard
        # API error, not a rounding. The exact value lives in the note below.
        data[real["sg_runtime_seconds"]] = int(round(secs))
    if real.get("sg_runtime_note"):
        if secs is None:
            data[real["sg_runtime_note"]] = "No episode cut yet; runtime is unmeasured."
        else:
            data[real["sg_runtime_note"]] = (
                "Measured from %s: %.2fs (%d:%02d) against a %ds target, %+.1fs. "
                "This is the animatic, not finished picture."
                % (vcode, secs, int(secs // 60), int(secs % 60), target, secs - target))
    if not dry:
        sg.update("Episode", ep["id"], data)
    log("  runtime: %s" % (data.get(real.get("sg_runtime_note", ""), "(not recorded)")))

    # link the acts, so the hierarchy is real and the Episode page lists them
    linked = 0
    for code in ACTS:
        s = sg.find_one("Sequence", [["project", "is", PROJ], ["code", "is", code]],
                        ["code", "episode"])
        if not s:
            log("  MISSING sequence %s" % code)
            continue
        if (s.get("episode") or {}).get("id") == ep["id"]:
            linked += 1
            continue
        if not dry:
            sg.update("Sequence", s["id"], {"episode": {"type": "Episode", "id": ep["id"]}})
        linked += 1
    log("  %d/%d act(s) linked to the Episode" % (linked, len(ACTS)))

    # the documents, so the site is self-contained
    for path, label in ((BIBLE, "Show bible"), (SCRIPT, "Script"), (SRT, "Subtitles")):
        if not os.path.exists(path):
            log("  MISSING %s: %s" % (label, path))
            continue
        if dry:
            log("  would attach %s (%s)" % (label, os.path.basename(path)))
            continue
        existing = sg.find("Attachment",
                           [["attachment_links", "in", [{"type": "Episode", "id": ep["id"]}]]],
                           ["this_file"])
        names = [(a.get("this_file") or {}).get("name") or "" for a in existing]
        if os.path.basename(path) in names:
            log("  %s already attached" % label)
            continue
        sg.upload("Episode", ep["id"], path, display_name="%s (%s)"
                  % (label, os.path.basename(path)))
        log("  attached %s" % label)
    return 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-56s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("bible exists to attach", os.path.exists(BIBLE))
    ck("script exists to attach", os.path.exists(SCRIPT))
    ck("subtitle track exists to attach", os.path.exists(SRT))
    ck("target is inside the stated format window", 570 <= _PILOT01_TARGET <= 600)
    ck("logline is a logline, not a synopsis", 80 <= len(_PILOT01_LOGLINE) <= 400)
    # The defect these guard: PILOT01's content reaching another show, and an
    # unscoped runtime query. Both wrote one episode's facts onto another.
    ck("content is keyed by episode, not a bare module constant",
       "PILOT01" in CONTENT and isinstance(CONTENT["PILOT01"], dict))
    ck("SHOW01 is NOT silently inheriting PILOT01's content",
       "SHOW01" not in CONTENT)
    ck("measured_runtime takes an episode argument (it was project-wide)",
       "ep" in measured_runtime.__code__.co_varnames[:2])
    ck("measured_runtime filters on the Episode entity, not just the project",
       '"entity"' in _src_of(measured_runtime) or "'entity'" in _src_of(measured_runtime))
    ck("content_for REFUSES rather than falling back to another show",
       "Refusing to build" in _src_of(content_for))
    ck("format text names the medium and the tone",
       "puppet" in _PILOT01_FORMAT.lower() and "sitcom" in _PILOT01_FORMAT.lower())
    ck("every field has a declared type",
       all(v[0] in ("text", "number") for v in FIELDS.values()))
    ck("acts are the three real sequence codes",
       ACTS == ["PILOT01_A", "PILOT01_B", "PILOT01_C"])

    # F462 (Geoff, 2026-09-08): the three Episode style fields mirror
    # Sequence's fields of the same name -- text, matching display names.
    ck("EPISODE_STYLE_FIELDS names the same three codes Sequence carries",
       {f[0] for f in EPISODE_STYLE_FIELDS}
       == {"sg_style_prefix", "sg_style_suffix", "sg_style_negative"})
    ck("EPISODE_STYLE_FIELDS is all 'text' type, mirroring Sequence's",
       all(f[2] == "text" for f in EPISODE_STYLE_FIELDS))
    ck("setup_style_schema is idempotent: an already-present field is "
       "reported and never recreated",
       "already exists" in _src_of(setup_style_schema)
       and "schema_field_create" in _src_of(setup_style_schema))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--setup-episode-style-schema", action="store_true",
                    help="idempotent: create Episode.sg_style_prefix/suffix/"
                         "negative, mirroring Sequence's -- see "
                         "setup_style_schema()")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test()
    if ns.setup_episode_style_schema:
        setup_style_schema(sg_connect())
        return 0
    if ns.build:
        return cmd_build(sg_connect(), ns.dry_run)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
