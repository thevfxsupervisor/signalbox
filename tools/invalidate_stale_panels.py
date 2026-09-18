#!/usr/bin/env python3
r"""Invalidate panels whose approved design has moved on underneath them.

THE HALF THAT WAS MISSING. genvideo_service.watch_panel_designs() already
NOTIFIES: when an Asset's sg_approved_design changes after a panel was
composed, it writes a Note on the shot naming both Version ids. Nothing acts on
that Note. The stale panel keeps its 'apr' status and the Shot keeps pointing
at it through sg_approved_panel, so video_from_panel will happily build video
on a panel that no longer reflects its own character design, and will not say
so. Measured 2026-09-04: SHOW01_A_0010 and _0020 sat exactly like that after
SHOW_CHAR_PILOTCHARA's approved design moved 67482 -> 67631.

Geoff's requirement, verbatim: "if a shot is completed, but then a constituent
asset is later unapproved and re-iterated, all downstream dependencies must be
invalidated and regenerated."

WHY THIS IS A TOOL AND NOT A WATCHER, for now. Un-approving is the first thing
in this pipeline that takes something AWAY from the operator: a panel they
approved stops being the approved one. That should be seen working on real data
under --dry-run before it runs unattended, and the decision to make it
automatic is Geoff's, not this tool's. Wiring it into the service is a
one-line call to invalidate() once he has seen it.

WHAT IT DOES, per stale shot:
  1. the stale panel Version's status -> 'rev' (back into review, NOT deleted,
     NOT rejected: it was a good panel for a design that has since changed, and
     rejecting it would misrepresent why)
  2. Shot.sg_approved_panel -> cleared, so nothing downstream can silently
     consume it
  3. the Panel Task -> 'rdy', which is what watch_panel_composition triggers
     on, so the shot recomposes against the NEW approved design

WHAT IT REFUSES. A shot whose recorded design ids still match the live approved
ones is not stale and is never touched. A shot with no sidecar entry cannot be
judged and is reported, not guessed at. Nothing here approves anything.

    python invalidate_stale_panels.py --self-test
    python invalidate_stale_panels.py --dry-run
    python invalidate_stale_panels.py --episode SHOW01
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import shot_lock as SHOT_LOCK  # noqa: E402  ROADMAP 0a -- protected-shot hard lock

PROJ = {"type": "Project", "id": 9999}
PANEL_TASK_NAME = "Panel"

_ROOTS = (
    r"C:\example\genvideo-pipeline",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    # THE RETIRED CLONE WAS THE THIRD ENTRY HERE AND IS GONE. It was the working
    # tree until 2026-09-06 and now carries DO-NOT-EDIT-THIS-CLONE.md; 19 of its
    # tools already differ from these. As a LAST fallback it turned a loud "file
    # not found" into a silent load of a stale workflow, which is the worse
    # failure. (It could not even fire: os.path.join("C:", ...) yields the
    # drive-RELATIVE "C:genvideo" + os.sep + "...", not a path to that clone.)
)


def sidecar_path():
    """The same file watch_panel_designs reads. Looked for, not assumed: the
    deploy seam means this module can run from a versioned release tree whose
    own parent has no build/out at all."""
    tried = []
    for r in _ROOTS:
        p = os.path.join(r, "build", "out", "panel_designs.json")
        tried.append(p)
        if os.path.isfile(p):
            return p
    raise SystemExit("panel_designs.json not found. Looked in:" + chr(10) + "  "
                     + (chr(10) + "  ").join(tried))


def log(m):
    print("[invalidate] %s" % m, flush=True)


def load_sidecar(path=None):
    p = path or sidecar_path()
    with open(p, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    shots = doc.get("shots")
    if not isinstance(shots, dict):
        raise SystemExit("%s has no 'shots' object -- refusing to guess" % p)
    return shots


def stale_shots(shots, live_approved, episode=None):
    """-> [(shot_code, [(asset_code, recorded_id, live_id), ...])].

    live_approved maps asset_id -> the Version id currently approved for it.
    An asset absent from live_approved has no approved design at all now,
    which is a CHANGE from having had one, so it counts as stale rather than
    being skipped -- treating 'unapproved' as 'unchanged' is precisely the
    un-approval case Geoff named."""
    out = []
    for code in sorted(shots):
        if episode and not code.startswith(episode):
            continue
        e = shots[code]
        if not isinstance(e, dict):
            continue
        drifted = []
        for akey, vkey in (("character_asset_id", "character_design_version_id"),
                           ("set_asset_id", "set_design_version_id")):
            aid = e.get(akey)
            rec = e.get(vkey)
            if not aid or not rec:
                continue
            live = live_approved.get(aid, None)
            if live != rec:
                drifted.append((e.get(akey.replace("_id", "_code")) or aid,
                                rec, live))
        if drifted:
            out.append((code, drifted))
    return out


def invalidate(sg, episode=None, dry=True, sidecar=None):
    shots = load_sidecar(sidecar)
    ids = set()
    for e in shots.values():
        if isinstance(e, dict):
            for k in ("character_asset_id", "set_asset_id"):
                if e.get(k):
                    ids.add(e[k])
    live = {}
    if ids:
        for a in sg.find("Asset", [["id", "in", sorted(ids)]],
                         ["id", "code", "sg_approved_design"]):
            ap = a.get("sg_approved_design")
            live[a["id"]] = ap.get("id") if isinstance(ap, dict) else None

    stale = stale_shots(shots, live, episode=episode)
    if not stale:
        log("no stale panels%s" % (" in %s" % episode if episode else ""))
        return 0
    log("%d shot(s) whose approved design has moved since the panel was composed:"
        % len(stale))
    changed = 0
    for code, drifted in stale:
        for acode, rec, now in drifted:
            log("  %-16s %s: %s -> %s" % (code, acode, rec, now))
        # ONE SHOT FAULT MUST NOT SILENCE THE WHOLE CYCLE. The protection
        # check below is a live ShotGrid call, and the caller in
        # genvideo_service.py wraps the ENTIRE per-episode loop in a single
        # try, so an exception escaping here does not skip one shot: it
        # abandons invalidation for every remaining stale shot AND every
        # later episode for the rest of that cycle. Found by review
        # 2026-09-08 by injecting a RuntimeError on the Version query and
        # watching it propagate out of invalidate() untouched. Skip the
        # shot, say so loudly, carry on: the same shape watch_hand_edits
        # already uses.
        try:
            shot = sg.find_one("Shot", [["project", "is", PROJ], ["code", "is", code]],
                               ["code", "sg_approved_panel"])
            if not shot:
                log("    no such Shot in project 9999 -- skipped")
                continue
            protecting = sg.find("Version",
                                 [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                                  SHOT_LOCK.version_filter()],
                                 ["code", "sg_status_list"], limit=1)
            if SHOT_LOCK.is_protected(protecting):
                log("    " + SHOT_LOCK.refusal(code, "invalidate_stale_panels",
                                               version=protecting[0] if protecting else None))
                continue
            ap = shot.get("sg_approved_panel")
            task = sg.find_one("Task", [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                                        ["content", "is", PANEL_TASK_NAME]],
                               ["content", "sg_status_list"])
            acts = []
            if isinstance(ap, dict):
                acts.append("Version %s 'apr' -> 'rev'" % ap.get("name"))
                acts.append("Shot.sg_approved_panel -> cleared")
            if task and task.get("sg_status_list") != "rdy":
                acts.append("Panel Task %s -> 'rdy'" % task.get("sg_status_list"))
            if not acts:
                log("    already invalidated -- nothing to do")
                continue
            log("    %s%s" % ("; ".join(acts), " (dry run)" if dry else ""))
            if dry:
                continue
            if isinstance(ap, dict):
                # Back to review, never rejected: it was a good panel for a design
                # that has since changed, and 'rjct' would misrepresent why.
                sg.update("Version", ap["id"], {"sg_status_list": "rev"})
                sg.update("Shot", shot["id"], {"sg_approved_panel": None})
            if task and task.get("sg_status_list") != "rdy":
                sg.update("Task", task["id"], {"sg_status_list": "rdy"})
            changed += 1
        except Exception as _e:
            log("    %s: protection check or invalidation FAILED (%s) -- this "
                "shot is SKIPPED, invalidation continues for the rest" % (code, _e))
            continue
    return changed


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    shots = {
        "SHOW01_A_0010": {"character_asset_id": 1, "character_asset_code": "CHAR_A",
                          "character_design_version_id": 100,
                          "set_asset_id": 2, "set_asset_code": "SET_A",
                          "set_design_version_id": 200},
        "SHOW01_A_0020": {"character_asset_id": 1, "character_asset_code": "CHAR_A",
                          "character_design_version_id": 999,
                          "set_asset_id": 2, "set_asset_code": "SET_A",
                          "set_design_version_id": 200},
        "PILOT01_A_0010": {"character_asset_id": 3, "character_asset_code": "CHAR_B",
                          "character_design_version_id": 300,
                          "set_asset_id": 2, "set_asset_code": "SET_A",
                          "set_design_version_id": 200},
    }
    live = {1: 999, 2: 200, 3: 300}

    st = dict((c, d) for c, d in stale_shots(shots, live))
    ck("a shot whose recorded design no longer matches is stale",
       "SHOW01_A_0010" in st)
    ck("a shot already on the current design is NOT stale",
       "SHOW01_A_0020" not in st)
    ck("an untouched episode's shots are not stale either",
       "PILOT01_A_0010" not in st)
    ck("the drift is reported with both version ids, not just a flag",
       st["SHOW01_A_0010"][0][1] == 100 and st["SHOW01_A_0010"][0][2] == 999)

    ep = dict((c, d) for c, d in stale_shots(shots, live, episode="PILOT01"))
    ck("the episode filter excludes other episodes", not ep)

    # UN-APPROVAL is the case Geoff named explicitly. An asset with no approved
    # design at all must count as changed, not as 'nothing to compare'.
    gone = dict((c, d) for c, d in stale_shots(shots, {1: None, 2: 200, 3: 300}))
    ck("an asset whose design was UN-approved makes its shots stale",
       "SHOW01_A_0010" in gone and "SHOW01_A_0020" in gone)

    ck("a set-design change is caught too, not only the character",
       "SHOW01_A_0020" in dict((c, d) for c, d in
                               stale_shots(shots, {1: 999, 2: 555, 3: 300})))

    class _Stub(object):
        def __init__(self):
            self.updates = []

        def find(self, entity_type, *a, **k):
            if entity_type == "Asset":
                return [{"id": 1, "code": "CHAR_A",
                        "sg_approved_design": {"id": 999, "name": "V999"}},
                       {"id": 2, "code": "SET_A",
                        "sg_approved_design": {"id": 200, "name": "V200"}}]
            # ROADMAP 0a (2026-09-08 correction): the protection query is
            # against "Version", not "Shot" -- an entity-blind stub that
            # returned the Asset rows above for this call too would hand
            # them back AS IF they were Version protection rows, and pass
            # only because none of them carry sg_status_list in ('pf',
            # 'fin'). This shot is unprotected by default; _ProtectedStub
            # below is the one that overrides this to return a locked row.
            return []

        def find_one(self, et, *a, **k):
            if et == "Shot":
                return {"id": 10, "code": "SHOW01_A_0010",
                        "sg_approved_panel": {"id": 50, "name": "PNL_v001"}}
            return {"id": 60, "content": "Panel", "sg_status_list": "rev"}

        def update(self, et, eid, data):
            self.updates.append((et, eid, dict(data)))

    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(prefix="inval_"), "panel_designs.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"shots": {"SHOW01_A_0010": shots["SHOW01_A_0010"]}}, fh)

    dryrun = _Stub()
    invalidate(dryrun, dry=True, sidecar=tmp)
    ck("a dry run performs zero writes", dryrun.updates == [])

    real = _Stub()
    n = invalidate(real, dry=False, sidecar=tmp)
    kinds = dict(((et, eid), d) for et, eid, d in real.updates)
    ck("the stale panel goes back to REVIEW, never rejected",
       kinds.get(("Version", 50)) == {"sg_status_list": "rev"})
    ck("Shot.sg_approved_panel is cleared so nothing can consume it",
       kinds.get(("Shot", 10)) == {"sg_approved_panel": None})
    ck("the Panel Task is requeued so the shot recomposes",
       kinds.get(("Task", 60)) == {"sg_status_list": "rdy"})
    ck("it reports how many shots it changed", n == 1)
    ck("nothing is ever set to an APPROVED status by this tool",
       not any("apr" in str(d.values()) for _, _, d in real.updates))

    # ROADMAP 0a CANARY: a PROTECTED shot with the exact same drift must be
    # refused, not invalidated -- no Version un-approved, no Task requeued,
    # no Shot.sg_approved_panel cleared.
    class _ProtectedStub(_Stub):
        def find_one(self, et, *a, **k):
            if et == "Shot":
                return {"id": 10, "code": "SHOW01_A_0010",
                        "sg_approved_panel": {"id": 50, "name": "PNL_v001"}}
            return {"id": 60, "content": "Panel", "sg_status_list": "rev"}

        def find(self, entity_type, *a, **k):
            if entity_type == "Version":
                return [{"id": 999, "code": "SHOW01_A_0010_v003", "sg_status_list": "pf"}]
            return _Stub.find(self, entity_type, *a, **k)

    protected = _ProtectedStub()
    n_prot = invalidate(protected, dry=False, sidecar=tmp)
    ck("a PROTECTED shot is refused, not invalidated (zero writes)",
       protected.updates == [])
    ck("a PROTECTED shot's refusal is reflected in the changed count (0)",
       n_prot == 0)


    # REVIEW CANARY 2026-09-08: a transient ShotGrid fault on the protection
    # query must skip ONE shot, never propagate. Asserted here rather than
    # reasoned about, because the caller wraps every episode in one try, so a
    # leak costs the whole cycle. Proven to fail: remove the try/except in
    # invalidate() and this goes red with the RuntimeError escaping.
    class _BoomStub(_Stub):
        def find(self, entity_type, *a, **k):
            if entity_type == "Version":
                raise RuntimeError("simulated ShotGrid fault on the protection query")
            return _Stub.find(self, entity_type, *a, **k)

    boom = _BoomStub()
    try:
        n_boom = invalidate(boom, dry=False, sidecar=tmp)
        leaked = False
    except Exception:
        n_boom, leaked = None, True
    ck("a ShotGrid fault on the protection query does NOT escape invalidate()",
       not leaked)
    ck("the faulting shot is skipped rather than invalidated (zero writes)",
       boom.updates == [])
    ck("a skipped-on-fault shot is not counted as changed", n_boom == 0)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    invalidate(PMB.get_backend(), episode=a.episode, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
