#!/usr/bin/env python3
"""THE one place every publisher creates/updates a viewable ShotGrid Version.

Geoff, 2026-08-28/29: "A foundational aspect of all this is publishing
results to shotgrid. Get that right first." An audit of project 9999 found
205 of 306 Versions (124 board, 81 keyframe) had a thumbnail but NO uploaded
media in sg_uploaded_movie -- they cannot be opened or played in ShotGrid.
Root cause, read straight out of the code: publish_board() (animatic.py),
cmd_generate/cmd_generate_sets/_publish_view_version (character_sheets.py)
called sg.upload_thumbnail() and NEVER sg.upload(field_name="sg_uploaded_movie")
at all. A thumbnail alone is not reviewable; the earlier verification pass
that reported "zero missing" checked "has media OR thumbnail" instead of
"does this actually play," which is exactly backwards -- see
missing_fields()'s sibling check in sg_publish_check.py for the fix.

PRIOR ART, PORTED NOT COPIED (Geoff: "Royal rendering has post process
scripts that do publishing if you need examples. ... Follow their
conventions where they are sound."). Read from (never imported, never
modified -- that tree is live production infra for other projects):

    C:\\example\\pipeline\\RR\\plugins\\project_manager\\shotgun\\
        shotgun_publish_quicktime_commandline.py   (movie case)
        shotgun_publish_sequence_commandline.py    (STILLS case -- this is
                                                     the one that matters:
                                                     it is exactly our
                                                     206/205 unviewable
                                                     boards/keyframes/panels)

Conventions taken from there, with the file:line each came from:

  1. shotgun_publish_quicktime_commandline.py:173 and
     shotgun_publish_sequence_commandline.py:156 -- both call
         sg.upload("Version", id, path, field_name="sg_uploaded_movie", ...)
     for a STILL IMAGE exactly as for a movie. This is the fix: a still
     belongs in sg_uploaded_movie too, ShotGrid displays it fine (confirmed
     against this project's own already-working "video"/"episode" Versions,
     which never call upload_thumbnail explicitly and get a thumbnail anyway
     -- SG auto-generates the review thumbnail from whatever lands in
     sg_uploaded_movie, image or movie). publish_version() below therefore
     does not call sg.upload_thumbnail() at all when a still is going into
     sg_uploaded_movie: it would be a redundant second upload of the same
     bytes. It IS still called for movies with a distinct thumbnail_path.
  2. shotgun_publish_quicktime_commandline.py:146-149 and
     shotgun_publish_sequence_commandline.py:114-122 -- "copy to publish
     folder" happens BEFORE any ShotGrid write, and the Version/upload use
     the COPIED path, never the original work/scratch path. Ours: durable_copy()
     below, target folder is the durable one Geoff named --
     C:\\example\\...\\genvideo-pipeline\\output\\published\\ -- never
     C:\\ComfyUI_windows_portable\\ComfyUI\\output (the 46 panels' bug: their
     sg_path_to_movie pointed straight at ComfyUI scratch, which gets
     overwritten and which ShotGrid's own service account cannot reach).

WHERE THIS DELIBERATELY DEVIATES FROM RR, AND WHY:
  - RR's publish_video()/publishSequence() wrap the whole publish in
    try/except and only print the traceback -- a publish failure there is
    invisible except in the per-job RR log (this is exactly what
    WANGLE_FIX_NOTE_publish_sequence.md is about: "a publish failure that
    only shows up as a job-level error is nearly invisible", and it took a
    real Fusion job silently disabling itself after 3 strikes before anyone
    noticed). Invariant 3 here is "loud failures" and Geoff's brief is
    explicit: "fail loudly if any of that cannot be done, rather than
    publishing something unviewable." So publish_version() raises
    PublishError instead of swallowing -- every failure mode below is a
    raise, not a print+continue.
  - RR's sequence publisher creates a PublishedFile record first, then a
    Version pointing at it. This project's other 8 publish sites never use
    PublishedFile at all (Version-only, sg_path_to_movie + sg_uploaded_movie
    directly) -- matching them, not RR, is "one contract used everywhere"
    for THIS codebase; introducing PublishedFile for only the ex-thumbnail-only
    stages would make the contract inconsistent with itself.

THE "ORPHAN VERSION" WART (flagged by Geoff -- read, not copied):
shotgun_publish_quicktime_commandline.py resolves its Task by shot code /
step short_name (its findStep()/sg.find("Task", ...)), and if that search
comes back empty it just leaves `task = None` and PUBLISHES ANYWAY -- nothing
in publishSequence() checks the result before building version_data with
"sg_task": task. shotgun_rStats_addPreview_cmdline.py's sibling
addPreviewCmd() shows the studio already knows this is wrong: its own code
comments "DO NOT silently continue. The studio's hook resolves Task from a
filename token and creates an orphan Version when it misses. Report it
loudly and let a human decide" and then does exactly that -- prints FAIL and
returns instead of creating the Version. THIS is the fix the quicktime hook
itself never got. publish_version() below does not inherit the wart: it
takes sg_task as a caller-supplied optional field rather than resolving Task
itself (each of this project's publishers already has its own board_task()/
panel_task()-style lookup-or-create helper upstream), so there is no
"missed the lookup, published anyway" path inside this contract. If a
publisher's own task lookup can silently miss, that is that publisher's
concern, not fixed by this module -- flagging it here rather than claiming
more than was verified.

RETRY. character_sheets.py:657-674's _sg_retry() documents a REAL failure
observed live in this project: a plain ConnectionAbortedError mid-HTTP-response
during a D13 run killed a publish call after the GPU render had already
passed QC -- the render survived, the unretried network call did not, and
took real work down with it. sg_retry() below is that same shape (tries=4,
delay=3s, last exception re-raised loudly after the last attempt -- never
swallowed), promoted here as the ONE copy so every publish site gets it
without re-copying the loop; character_sheets.py's own _sg_retry is untouched
for now (out of scope: it wraps calls beyond publish_version, e.g. the Asset
sg_stage update) but should eventually call this one instead of keeping a
second copy.

Every publisher (worker, character_sheets, finishing, animatic, note_triage,
episode_assemble; panel_compose.py and video_from_panel.py deferred -- see
docs/METHOD.md for why) must create/update its Version
through publish_version() below. None should carry its own sg.create("Version"
+ sg.upload(...) pair (invariant 11: one implementation, imported not copied).
"""
import os
import shutil
import time

import sg_provenance as PROV

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PUBLISHED_DIR = os.path.join(ROOT, "output", "published")

# THE ONE DEFINITION of "statuses a publisher must supersede before it lands a
# replacement" (invariant 11: one implementation, not three).
#
# sg_review_housekeeping rejects any 'rev' Version whose (entity, stage)
# already has an APPROVED sibling, and it runs live in the service. So every
# publisher that can produce a second Version for the same entity and stage has
# to un-approve what it replaces, or its own output is auto-rejected before a
# human sees it. Nothing else un-approves; that is the recorded gap.
#
# Found three times on three surfaces before it was centralised here: panels
# (prompt_revision.apply_for_shot), episode cuts (episode_assemble) and shot
# videos (video_from_panel). Each had, or was about to get, its own copy of
# this tuple. Three copies of a list that MUST agree with housekeeping is how
# they silently stop agreeing, so it lives here and is imported.
#
# It deliberately MIRRORS sg_review_housekeeping.APPROVED rather than importing
# it: that module is the sweeper and this is the publish helper, and a shared
# mutable tuple across that boundary is how one end gets quietly widened. The
# self-test below asserts the two still match, which is the real protection.
SUPERSEDABLE = ("apr", "ad", "fin", "paf", "dlvr", "appcbb", "appgra")

STILL_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff")


class PublishError(Exception):
    """Raised the moment a publish cannot be made viewable/complete, instead
    of quietly leaving a Version with a thumbnail and nothing else (invariant
    3; see the module docstring's "where this deviates from RR")."""


def sg_retry(fn, *args, tries=4, delay=3, log=print, **kwargs):
    """Retry a ShotGrid call a few times on a transient connection error.
    Same shape as character_sheets.py:657 _sg_retry() (tries=4, delay=3),
    promoted here per the module docstring's RETRY section: a real
    ConnectionAbortedError took down a publish for work that had already
    passed QC. Only retries; the last exception is re-raised loudly after
    the last attempt (invariant 3) -- never swallowed."""
    last = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                          # noqa: BLE001
            last = exc
            log("  SG call failed (attempt %d/%d): %s: %s - retrying"
               % (i + 1, tries, type(exc).__name__, exc))
            if i + 1 < tries:
                time.sleep(delay)
    raise last


def durable_copy(src_path, code, log=print):
    """Copy src_path into PUBLISHED_DIR under a name derived from the

    THIS IS THE ONLY RECORD OF WHAT WAS APPROVED, and that became literally
    true on 2026-09-07. a peer engineer's F323 diffed two byte-identical graphs node by
    node and got different images at 1 of 4 seeds (max channel difference 246,
    suspected fp8 kernel nondeterminism plus a model reload), so **an approved
    image cannot be reliably re-derived from its seed and provenance**. The
    provenance says what we asked for; only this copy says what we got.
    F320 found three approved SHOW01 panels whose only copy was on the scratch
    disk ComfyUI clears, which under a deterministic sampler would have been
    untidy and under this one is unrecoverable.
    Version code, and return that durable path. Never returns a path under
    ComfyUI scratch or any other non-durable location -- that is the whole
    point (Geoff: "sg_path_to_movie ... never ComfyUI scratch").

    If src_path is already inside PUBLISHED_DIR this is a no-op (idempotent,
    and avoids genvideo_worker.py/finishing.py/etc double-copying files they
    already wrote straight into PUBLISHED_DIR)."""
    if not src_path:
        raise PublishError("%s: no source media path given" % code)
    if not os.path.isfile(src_path):
        raise PublishError("%s: source media file does not exist: %r" % (code, src_path))
    if os.path.getsize(src_path) == 0:
        raise PublishError("%s: source media file is zero bytes: %r" % (code, src_path))

    src_abs = os.path.abspath(src_path)
    published_abs = os.path.abspath(PUBLISHED_DIR)
    if os.path.dirname(src_abs).lower() == published_abs.lower():
        return src_abs

    ext = os.path.splitext(src_abs)[1] or ".bin"
    dst = os.path.join(PUBLISHED_DIR, code + ext)
    try:
        os.makedirs(PUBLISHED_DIR, exist_ok=True)
        shutil.copyfile(src_abs, dst)
    except Exception as exc:                              # noqa: BLE001
        raise PublishError("%s: could not copy media into durable published dir "
                           "(%r -> %r): %s" % (code, src_abs, dst, type(exc).__name__)) from exc
    if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src_abs):
        raise PublishError("%s: durable copy verification failed (%r -> %r)"
                           % (code, src_abs, dst))
    log("  durable copy: %s -> %s" % (src_abs, dst))
    return dst


def publish_version(sg, *, project, entity, code, media_path, sg_task=None,
                     description="", status="rev", stage=None,
                     first_frame=1001, last_frame=1001,
                     thumbnail_path=None, extra_fields=None,
                     character="", set_="", action="", camera="", style="",
                     wedge_group=None, batch_id=None,
                     workflow_hash="", anchor_version_id=None,
                     find_existing=True, log=print):
    """THE contract. Create (or update, if find_existing and a Version with
    this code already exists on this project) one Version, and guarantee:

      1. media_path exists and is non-empty (else PublishError, before any
         ShotGrid write -- never create a Version with nothing to back it).
      2. the artefact is copied into the durable PUBLISHED_DIR (RR's
         "copy to publish folder before touching ShotGrid" convention --
         see module docstring point 2) and sg_path_to_movie points there.
      3. real media is uploaded to sg_uploaded_movie -- stills included, per
         RR's own sequence publisher (module docstring point 1). A PNG/JPG
         going into sg_uploaded_movie IS how ShotGrid displays a still; this
         is not a workaround.
      4. a thumbnail exists: for a movie with a distinct thumbnail_path,
         uploaded explicitly; otherwise ShotGrid's own transcoder generates
         one from whatever was just uploaded to sg_uploaded_movie (confirmed
         against this project's already-working publishers, none of which
         call upload_thumbnail and all of which have thumbnails).
      5. D6 provenance (sg_provenance.write_provenance) is written.
      6. sg_stage is set, if given.
      7. sg_batch_id is stamped, if `batch_id` is given -- at CREATE time,
         alongside every other field, never as a later update. Geoff,
         2026-09-08: "stamp a batch id on the Version when the batch is
         published. time gap is flaky." This is what replaced
         sg_review_housekeeping.py's created_at-clustering heuristic: the
         batch's own publisher (panel_compose.py) computes the id once (the
         REVIEW playlist name this run will use) and passes the SAME string
         to every Version in the batch, so the record can never disagree
         with the playlist. Distinct from `wedge_group` above: that field is
         for WEDGE experiments (sg_stage='wedge', never reviewed) and drives
         its own REVIEW_<wedge_group> playlist as a side effect; `batch_id`
         is for review batches at alternate-bearing stages and creates no
         playlist itself -- the caller already owns that.

    Any failure at any of these steps is LOUD: raises PublishError (or lets
    the underlying shotgun_api3 exception propagate) rather than leaving a
    half-published, unviewable Version sitting in review. Returns the
    Version dict (with at least "id" and "code") on success.
    """
    if not code:
        raise PublishError("publish_version: no code given")

    durable = durable_copy(media_path, code, log=log)

    fields = {
        "project": project, "entity": entity, "code": code,
        "description": description, "sg_status_list": status,
        "sg_first_frame": first_frame, "sg_last_frame": last_frame,
        "sg_path_to_movie": durable,
    }
    if sg_task:
        fields["sg_task"] = sg_task
    if batch_id:
        fields["sg_batch_id"] = batch_id
    if extra_fields:
        fields.update(extra_fields)

    v = None
    if find_existing:
        try:
            v = sg_retry(sg.find_one, "Version", [["project", "is", project],
                                                  ["code", "is", code]], ["code"], log=log)
        except Exception as exc:                          # noqa: BLE001
            raise PublishError("%s: lookup-before-create failed: %s"
                               % (code, type(exc).__name__)) from exc
    try:
        if v is None:
            v = sg_retry(sg.create, "Version", fields, log=log)
        else:
            sg_retry(sg.update, "Version", v["id"], fields, log=log)
    except Exception as exc:                              # noqa: BLE001
        raise PublishError("%s: Version create/update failed: %s"
                           % (code, type(exc).__name__)) from exc

    try:
        sg_retry(sg.upload, "Version", v["id"], durable, field_name="sg_uploaded_movie",
                display_name=code, log=log)
    except Exception as exc:                              # noqa: BLE001
        raise PublishError("%s: media upload FAILED on Version %s -- it exists in "
                           "ShotGrid but is UNVIEWABLE: %s"
                           % (code, v["id"], type(exc).__name__)) from exc

    is_still = os.path.splitext(durable)[1].lower() in STILL_EXTS
    if thumbnail_path and not (is_still and thumbnail_path == durable):
        try:
            sg_retry(sg.upload_thumbnail, "Version", v["id"], thumbnail_path, log=log)
        except Exception as exc:                          # noqa: BLE001
            raise PublishError("%s: explicit thumbnail upload FAILED on Version %s: %s"
                               % (code, v["id"], type(exc).__name__)) from exc

    if stage:
        try:
            sg_retry(sg.update, "Version", v["id"], {"sg_stage": stage}, log=log)
        except Exception as exc:                          # noqa: BLE001
            raise PublishError("%s: sg_stage update FAILED on Version %s: %s"
                               % (code, v["id"], type(exc).__name__)) from exc

    ok = PROV.write_provenance(
        sg, v["id"], character=character, set_=set_, action=action, camera=camera,
        style=style, workflow_hash=workflow_hash, anchor_version_id=anchor_version_id,
        log=log)
    if not ok:
        raise PublishError("%s: D6 provenance write FAILED on Version %s (invariant 9: "
                           "'a publish without provenance is a bug')" % (code, v["id"]))

    # GROUPING HAPPENS HERE, NOT IN A TOOL SOMEBODY REMEMBERS TO RUN.
    # Geoff, 2026-09-06, on 12 anchor candidates that landed in no playlist:
    # "that type of thing should be done by the pipeline, not as housekeeping
    # manually by you."
    #
    # He is right, and the backfill tool is a backstop for old data, not the
    # mechanism. A batch that is only grouped when someone runs a sweep is a
    # batch that is ungrouped for however long the sweep is forgotten, and the
    # operator meets it in that state.
    #
    # sg_wedge_group already existed on the schema, was referenced by no code,
    # and was empty on all 994 Versions. It is the batch's IDENTITY; the
    # playlist is the thing the operator opens. Both are written here so every
    # publisher gets them without opting in.
    if wedge_group:
        try:
            sg_retry(sg.update, "Version", v["id"],
                     {"sg_wedge_group": wedge_group}, log=log)
            _file_in_review_playlist(sg, project, v, wedge_group, log=log)
        except Exception as exc:                                  # noqa: BLE001
            # Loud, never fatal. Losing the grouping costs a hunt through the
            # Versions table; losing the publish costs the render.
            log("WARNING: published %s but could not group it under %r (%s). "
                "sg_review_playlists.py will pick it up." % (code, wedge_group, exc))
    return v


def _file_in_review_playlist(sg, project, version, wedge_group, log=print):
    """Add this Version to REVIEW_<wedge_group>, creating it if needed.

    Idempotent by membership, so a re-publish of the same code does not
    duplicate the member, and the same naming as sg_review_playlists.py so the
    backstop and the live path cannot disagree about where a batch lives."""
    name = "REVIEW_%s" % wedge_group
    pl = sg_retry(sg.find_one, "Playlist",
                  [["project", "is", project], ["code", "is", name]],
                  ["code", "versions"], log=log)
    if pl is None:
        sg_retry(sg.create, "Playlist",
                 {"project": project, "code": name,
                  "description": "Review batch %s, filed at publish time." % wedge_group,
                  "versions": [{"type": "Version", "id": version["id"]}]}, log=log)
        log("  filed %s in NEW playlist %s" % (version.get("code"), name))
        return
    have = [x for x in (pl.get("versions") or [])]
    if any(x.get("id") == version["id"] for x in have):
        return
    sg_retry(sg.update, "Playlist", pl["id"],
             {"versions": have + [{"type": "Version", "id": version["id"]}]}, log=log)
    log("  filed %s in playlist %s" % (version.get("code"), name))


# ---------------------------------------------------------------------- self-test
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-62s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    import tempfile

    tmp = tempfile.mkdtemp(prefix="sg_publish_selftest_")
    global PUBLISHED_DIR
    saved_dir = PUBLISHED_DIR
    PUBLISHED_DIR = os.path.join(tmp, "published")

    try:
        src = os.path.join(tmp, "src.png")
        with open(src, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfakebutnonzero")

        # ---- durable_copy
        try:
            durable_copy(None, "X", log=lambda m: None)
            ck("durable_copy raises PublishError on no path", False)
        except PublishError:
            ck("durable_copy raises PublishError on no path", True)

        try:
            durable_copy(os.path.join(tmp, "does_not_exist.png"), "X", log=lambda m: None)
            ck("durable_copy raises PublishError on missing file", False)
        except PublishError:
            ck("durable_copy raises PublishError on missing file", True)

        zero = os.path.join(tmp, "zero.png")
        open(zero, "wb").close()
        try:
            durable_copy(zero, "X", log=lambda m: None)
            ck("CANARY: durable_copy raises PublishError on a zero-byte file", False)
        except PublishError:
            ck("CANARY: durable_copy raises PublishError on a zero-byte file", True)

        dst = durable_copy(src, "MYCODE_v001", log=lambda m: None)
        ck("durable_copy copies into PUBLISHED_DIR", os.path.dirname(dst) == PUBLISHED_DIR)
        ck("durable_copy names the file after the Version code",
           os.path.basename(dst) == "MYCODE_v001.png")
        ck("durable_copy preserves the bytes", open(dst, "rb").read() == open(src, "rb").read())

        dst2 = durable_copy(dst, "MYCODE_v001", log=lambda m: None)
        ck("durable_copy is a no-op when already inside PUBLISHED_DIR", dst2 == dst)

        # ---- publish_version, stub sg
        class _StubSG:
            def __init__(self):
                self.versions = {}
                self.uploads = []
                self.thumbs = []
                self.next_id = 1
                self.fail_upload = False
                self.fail_create = False
                self.create_calls = []

            def find_one(self, entity, filters, fields):
                for v in self.versions.values():
                    if any(f[0] == "code" and f[2] == v["code"] for f in filters):
                        return {"code": v["code"], "id": v["id"]}
                return None

            def create(self, entity, data):
                if self.fail_create:
                    raise RuntimeError("boom-create")
                vid = self.next_id
                self.next_id += 1
                row = dict(data)
                row["id"] = vid
                self.versions[vid] = row
                self.create_calls.append(dict(data))
                return row

            def update(self, entity, vid, data):
                self.versions[vid].update(data)
                return self.versions[vid]

            def upload(self, entity, vid, path, field_name=None, display_name=None):
                if self.fail_upload:
                    raise RuntimeError("boom-upload")
                self.uploads.append((vid, path, field_name))

            def upload_thumbnail(self, entity, vid, path):
                self.thumbs.append((vid, path))

        sg = _StubSG()
        v = publish_version(
            sg, project={"type": "Project", "id": 9999}, entity={"type": "Shot", "id": 1},
            code="TEST_BRD_v001", media_path=src, description="d", stage="board",
            character="CHARB", set_="n/a - test", action="a", camera="c", style="s",
            workflow_hash="h" * 64, log=lambda m: None)
        ck("publish_version returns a Version dict with an id", "id" in v)
        ck("publish_version uploaded real media to sg_uploaded_movie",
           len(sg.uploads) == 1 and sg.uploads[0][2] == "sg_uploaded_movie")
        ck("publish_version's uploaded path is the durable copy, not the scratch source",
           sg.uploads[0][1] != src and os.path.dirname(sg.uploads[0][1]) == PUBLISHED_DIR)
        ck("publish_version does NOT call upload_thumbnail for a still with no explicit "
           "thumbnail_path (avoids a redundant second upload -- module docstring point 1)",
           len(sg.thumbs) == 0)
        ck("publish_version set sg_path_to_movie to the durable copy",
           sg.versions[v["id"]]["sg_path_to_movie"] == sg.uploads[0][1])
        ck("publish_version wrote D6 provenance",
           PROV.missing_fields(sg.versions[v["id"]]) == [])
        ck("publish_version set sg_stage", sg.versions[v["id"]].get("sg_stage") == "board")
        ck("publish_version with no batch_id given writes no sg_batch_id key at all "
           "(never a guessed or empty value)",
           "sg_batch_id" not in sg.versions[v["id"]])

        # CANARY: batch_id is written AT CREATE TIME, in the same fields dict
        # as everything else -- not a later sg.update() a second assembler
        # could disagree with (invariant: "the id must be written by the code
        # that ACTUALLY publishes the batch").
        sg_b = _StubSG()
        vb = publish_version(
            sg_b, project={"type": "Project", "id": 9999}, entity={"type": "Shot", "id": 9},
            code="TEST_PNL_v009", media_path=src, description="d", stage="panel",
            character="CHARB", set_="n/a", action="a", camera="c", style="s",
            workflow_hash="h" * 64, batch_id="REVIEW_TEST_SHOT_v009", log=lambda m: None)
        ck("publish_version stamps sg_batch_id when given",
           sg_b.versions[vb["id"]].get("sg_batch_id") == "REVIEW_TEST_SHOT_v009")
        ck("CANARY: sg_batch_id was IN THE create() call's own data, not added by a "
           "later sg.update() -- the invariant is 'written by the code that actually "
           "publishes the batch, not reconstructed afterwards'",
           len(sg_b.create_calls) == 1
           and sg_b.create_calls[0].get("sg_batch_id") == "REVIEW_TEST_SHOT_v009")

        # CANARY: re-publish with the same code updates in place, does not duplicate
        v2 = publish_version(
            sg, project={"type": "Project", "id": 9999}, entity={"type": "Shot", "id": 1},
            code="TEST_BRD_v001", media_path=src, description="d2", stage="board",
            character="CHARB", set_="n/a - test", action="a", camera="c", style="s",
            workflow_hash="h" * 64, log=lambda m: None)
        ck("CANARY: republishing the same code updates the existing Version, no duplicate",
           v2["id"] == v["id"] and len(sg.versions) == 1)

        # CANARY: upload failure must raise PublishError, not silently leave a
        # thumbnail-only Version sitting in ShotGrid (the exact bug this module fixes)
        sg2 = _StubSG()
        sg2.fail_upload = True
        try:
            publish_version(
                sg2, project={"type": "Project", "id": 9999}, entity={"type": "Shot", "id": 2},
                code="TEST_BRD_v002", media_path=src, description="d", stage="board",
                character="CHARB", set_="n/a", action="a", camera="c", style="s",
                workflow_hash="h" * 64, log=lambda m: None)
            ck("CANARY: an upload failure raises PublishError instead of publishing "
               "an unviewable Version", False)
        except PublishError as exc:
            ck("CANARY: an upload failure raises PublishError instead of publishing "
               "an unviewable Version", "UNVIEWABLE" in str(exc))

        # CANARY: missing media file must raise BEFORE any ShotGrid write happens at all
        sg3 = _StubSG()
        try:
            publish_version(
                sg3, project={"type": "Project", "id": 9999}, entity={"type": "Shot", "id": 3},
                code="TEST_BRD_v003", media_path=os.path.join(tmp, "nope.png"),
                description="d", stage="board", log=lambda m: None)
            ck("CANARY: a missing source file raises before touching ShotGrid at all", False)
        except PublishError:
            ck("CANARY: a missing source file raises before touching ShotGrid at all",
               len(sg3.versions) == 0)

    finally:
        PUBLISHED_DIR = saved_dir
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    # SUPERSEDABLE MUST TRACK THE SWEEPER, or a publisher un-approves the
    # wrong set and its output is auto-rejected anyway. The tuple is
    # deliberately a mirror rather than an import (see its comment), so THIS
    # is the thing that makes the mirror safe. If housekeeping ever adds an
    # approved status, this fails and names the drift instead of letting a
    # publisher silently stop superseding it.
    try:
        import sg_review_housekeeping as _HK
        ck("SUPERSEDABLE still matches sg_review_housekeeping.APPROVED exactly",
           tuple(SUPERSEDABLE) == tuple(_HK.APPROVED))
    except ImportError:
        ck("SUPERSEDABLE drift check could import the sweeper", False)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
