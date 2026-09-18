#!/usr/bin/env python3
r"""Give existing Versions playable media: upload the still to sg_uploaded_movie.

WHY. panel_compose published every panel with a thumbnail and nothing in
sg_uploaded_movie, so ShotGrid shows a picture in a list and says "no playable
media" the moment anyone opens the Version to review it -- which is what the
operator actually does. Measured 2026-09-04: 12 of 12 recent panels, thumbnail
yes, media no. sg_publish.py's docstring documents this exact defect and this
exact fix; that publish path never adopted it.

panel_compose is fixed for new panels. This repairs the ones already published.

A still going into sg_uploaded_movie IS how ShotGrid plays a still. That is the
documented convention, not a workaround.

VERIFIES BY FETCH, NOT BY FIELD. After each upload it re-reads the Version and
confirms sg_uploaded_movie is populated. "Created is not viewable" applies here
exactly as it does to a render.

    python backfill_playable_media.py --self-test
    python backfill_playable_media.py --dry-run
    python backfill_playable_media.py --episode SHOW01
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJ = {"type": "Project", "id": 9999}
FIELDS = ["code", "sg_uploaded_movie", "sg_path_to_movie", "image", "sg_stage"]


def log(m):
    print("[playable] %s" % m, flush=True)


def needs_media(v):
    """-> True if this Version has a file on disk and nothing in
    sg_uploaded_movie.

    Deliberately NOT 'has media OR thumbnail'. That is the check that reported
    zero missing while every panel was unplayable: a thumbnail satisfies it and
    a thumbnail is not playback."""
    if v.get("sg_uploaded_movie"):
        return False
    p = v.get("sg_path_to_movie")
    return bool(p) and os.path.isfile(p)


def backfill(sg, episode=None, dry=True, limit=None):
    filters = [["project", "is", PROJ]]
    if episode:
        filters.append(["code", "starts_with", episode])
    vs = sg.find("Version", filters, FIELDS,
                 order=[{"field_name": "code", "direction": "asc"}])
    todo = [v for v in vs if needs_media(v)]
    missing_file = [v for v in vs
                    if not v.get("sg_uploaded_movie") and not needs_media(v)]
    log("%d Version(s); %d need media; %d have no media AND no file on disk"
        % (len(vs), len(todo), len(missing_file)))
    for v in missing_file[:10]:
        log("  NO FILE: %-44s path=%s" % (v["code"][:44], v.get("sg_path_to_movie")))
    if limit:
        todo = todo[:limit]
    done = failed = 0
    for v in todo:
        if dry:
            log("  would upload %s" % v["code"])
            continue
        try:
            sg.upload("Version", v["id"], v["sg_path_to_movie"],
                      field_name="sg_uploaded_movie")
        except Exception as exc:                                  # noqa: BLE001
            log("  FAILED %s: %s: %s" % (v["code"], type(exc).__name__, str(exc)[:120]))
            failed += 1
            continue
        # Verify by re-reading, not by the absence of an exception.
        after = sg.find_one("Version", [["id", "is", v["id"]]], ["sg_uploaded_movie"])
        if after and after.get("sg_uploaded_movie"):
            done += 1
        else:
            log("  UPLOAD REPORTED OK BUT FIELD IS STILL EMPTY: %s" % v["code"])
            failed += 1
    log("uploaded %d, failed %d%s" % (done, failed, " (dry run)" if dry else ""))
    return failed


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("a Version that already has media is skipped",
       not needs_media({"sg_uploaded_movie": {"id": 1}, "sg_path_to_movie": __file__}))
    ck("a Version with a real file and no media NEEDS it",
       needs_media({"sg_uploaded_movie": None, "sg_path_to_movie": __file__}))
    ck("a Version whose file is gone is not attempted",
       not needs_media({"sg_uploaded_movie": None,
                        "sg_path_to_movie": r"C:\nope\gone.png"}))
    ck("a Version with no path at all is not attempted",
       not needs_media({"sg_uploaded_movie": None, "sg_path_to_movie": None}))
    # THE CANARY. A thumbnail is not playback, and the check that treated it as
    # playback is why this went unnoticed across a whole episode of panels.
    ck("a THUMBNAIL does not satisfy the check",
       needs_media({"sg_uploaded_movie": None, "image": "http://x/thumb.jpg",
                    "sg_path_to_movie": __file__}))

    class _Stub(object):
        def __init__(self, verify=True):
            self.uploads = []
            self.verify = verify

        def find(self, *a, **k):
            return [{"id": 1, "code": "A", "sg_uploaded_movie": None,
                     "sg_path_to_movie": __file__},
                    {"id": 2, "code": "B", "sg_uploaded_movie": {"id": 9},
                     "sg_path_to_movie": __file__}]

        def find_one(self, *a, **k):
            return {"sg_uploaded_movie": {"id": 5} if self.verify else None}

        def upload(self, *a, **k):
            self.uploads.append(a)

    st = _Stub()
    backfill(st, dry=True)
    ck("a dry run uploads nothing", st.uploads == [])
    st2 = _Stub()
    ck("a real run uploads only the Version that needs it",
       backfill(st2, dry=False) == 0 and len(st2.uploads) == 1)
    st3 = _Stub(verify=False)
    ck("an upload that leaves the field empty is counted as FAILED",
       backfill(st3, dry=False) == 1)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default="SHOW01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    return backfill(PMB.get_backend(), episode=a.episode, dry=a.dry_run,
                    limit=a.limit)


if __name__ == "__main__":
    sys.exit(main())
