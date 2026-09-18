#!/usr/bin/env python3
r"""Every pending panel candidate belongs to a REVIEW playlist for its shot.

WHY. The operator reviews by opening a Playlist in ShotGrid's player and
flipping through its cells: that is how a seed wedge becomes a decision instead
of a list. panel_compose's publish_alternates() creates
REVIEW_<shot>_v<NNN> for every run it publishes, so anything composed since
that landed is already grouped. Anything composed BEFORE it is not, and those
candidates are reachable only by hunting the Versions table shot by shot.

Measured 2026-09-05: 84 pending candidates across 22 shots, of which 4 shots
(SHOW01_A_0010, _0020, _0060, _0320 -- the oldest, from before wedge grouping)
had every candidate ungrouped. Small, and exactly the shots an operator would
hit first if they worked in cut order.

IT ONLY GROUPS WHAT IS STILL PENDING. A rejected or approved candidate is a
settled decision and putting it in a review playlist would ask for that decision
again. This is the same rule the housekeeping watcher applies from the other
side, and the two must agree or a Version can be swept out of review and then
pulled back in by this tool (invariant 11 in spirit: one definition of "needs a
decision").

Idempotent: an existing playlist is reused and only missing members are added,
so it is safe to run on every deploy or from a watcher.

    python sg_review_playlists.py --dry-run
    python sg_review_playlists.py --episode SHOW01
    python sg_review_playlists.py --self-test
"""
import argparse
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJ = {"type": "Project", "id": 9999}
PREFIX = "REVIEW"

# The one status that means "this is waiting for the operator". Kept as a tuple
# so it reads the same as sg_review_housekeeping's SWEEPABLE, which is the other
# half of the same rule.
PENDING = ("rev",)


def log(m):
    print("[review-pl] %s" % m, flush=True)


def playlist_name(entity_name, wedge_group=None):
    """-> the playlist these pending candidates belong in.

    A WEDGE GROUP WINS OVER THE ENTITY, added 2026-09-06. Geoff, looking at 12
    new anchor candidates: *"are they no longer grouped by a playlist? what are
    they grouped by? how do I filter to keep the same-batch items together?"*

    They were grouped by nothing. Panels get REVIEW_<shot> because
    publish_alternates names one per compose; asset edits got no playlist at
    all, and the only thing separating one batch from another was the seed
    number inside the Version CODE. That is a name, not an identity, and two
    batches of the same room in one playlist asks the operator to compare
    things that are not comparable.

    `sg_wedge_group` already existed on the Version schema, was referenced by
    zero lines of code, and was empty on all 994 Versions. It is the right
    field and it is now populated at publish time, so a batch is a fact on the
    record rather than a substring."""
    if wedge_group:
        return "%s_%s" % (PREFIX, wedge_group)
    return "%s_%s" % (PREFIX, entity_name)


def _legacy_playlist_name(shot_code):
    """-> the playlist a shot's pending candidates belong in.

    ONE PLAYLIST PER SHOT, not per run. publish_alternates names its own
    REVIEW_<shot>_v<NNN> per compose, which is right for "these four came out of
    one run"; this is the backstop that guarantees a shot is reachable at all,
    and a shot with candidates from three different runs should still be ONE
    thing to open."""
    return "%s_%s" % (PREFIX, shot_code)


def plan(versions):
    """-> {playlist_name: [version]} for pending candidates only.

    A Version with no Shot entity is skipped rather than filed under a guessed
    name: an unfiled candidate is visible as a gap, a misfiled one is not."""
    out, skipped = defaultdict(list), []
    for v in versions:
        if v.get("sg_status_list") not in PENDING:
            continue
        ent = v.get("entity") or {}
        # Shots AND Assets. An asset anchor is reviewed exactly like a panel:
        # several candidates, one gets picked. Anything else is skipped rather
        # than filed under a guessed name, because an unfiled candidate is
        # visible as a gap and a misfiled one is not.
        if ent.get("type") not in ("Shot", "Asset") or not ent.get("name"):
            skipped.append(v.get("code"))
            continue
        out[playlist_name(ent["name"], v.get("sg_wedge_group"))].append(v)
    return out, skipped


def run(sg, episode="SHOW01", dry=True):
    vs = sg.find("Version", [["project", "is", PROJ],
                             ["code", "starts_with", episode],
                             ["sg_stage", "is", "panel"]],
                 ["code", "sg_status_list", "entity", "sg_wedge_group"],
                 order=[{"field_name": "code", "direction": "asc"}])
    # Asset anchor candidates are reviewed the same way and were reachable
    # through no playlist at all. They are not episode-prefixed, so they need
    # their own query rather than a wider one.
    vs += sg.find("Version", [["project", "is", PROJ],
                              ["sg_stage", "is", "keyframe"]],
                  ["code", "sg_status_list", "entity", "sg_wedge_group"],
                  order=[{"field_name": "code", "direction": "asc"}])
    # VIDEOS, and they need DIFFERENT GRANULARITY. A panel batch is several
    # candidates for ONE shot, so the playlist is per shot: that is where the
    # choice is. A shot has exactly one video candidate, so a per-shot video
    # playlist holds one clip and is useless to flip through.
    #
    # PLAYLIST GRANULARITY FOLLOWS WHERE THE CHOICE IS. For video the choice is
    # per shot but the SITTING is the episode, so one playlist holds every
    # pending video and the operator watches them in cut order. Measured
    # 2026-09-06: 16 videos awaiting review, 0 in any playlist, reachable only
    # by hunting the Versions table shot by shot.
    vids = sg.find("Version", [["project", "is", PROJ],
                               ["code", "starts_with", episode],
                               ["sg_stage", "is", "video"]],
                   ["code", "sg_status_list", "entity", "sg_wedge_group"],
                   order=[{"field_name": "code", "direction": "asc"}])
    for v in vids:
        # One group for the whole episode's pending videos, unless the publisher
        # already declared a batch.
        v.setdefault("sg_wedge_group", None)
        if not v.get("sg_wedge_group"):
            v["sg_wedge_group"] = "%s_video_pending" % episode
    vs += vids
    # Two queries can return the same Version, so dedupe on ID before planning.
    # Not a test accommodation: a Version whose stage changes between the two
    # calls would otherwise be filed twice and counted twice.
    seen, unique = set(), []
    for v in vs:
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        unique.append(v)
    vs = unique
    groups, skipped = plan(vs)
    log("%d panel Version(s) in %s -> %d shot(s) with pending candidates"
        % (len(vs), episode, len(groups)))
    for c in skipped[:5]:
        log("  SKIPPED (no Shot entity, not guessing a home): %s" % c)

    made = added = 0
    for name in sorted(groups):
        members = groups[name]
        existing = sg.find_one("Playlist", [["project", "is", PROJ],
                                            ["code", "is", name]],
                               ["code", "versions"])
        have = set(x["id"] for x in ((existing or {}).get("versions") or []))
        want = [{"type": "Version", "id": v["id"]} for v in members
                if v["id"] not in have]
        if existing and not want:
            continue
        log("  %-34s %d pending%s" % (name, len(members),
                                      "" if existing else "  [new]"))
        if dry:
            continue
        if not existing:
            sg.create("Playlist", {
                "project": PROJ, "code": name,
                "description": ("Every panel candidate on this shot that is still "
                                "waiting for a decision. Approve one, reject the "
                                "rest, or add a Note; the alternates are tidied "
                                "automatically once something is approved."),
                "versions": [{"type": "Version", "id": v["id"]} for v in members]})
            made += 1
        else:
            sg.update("Playlist", existing["id"],
                      {"versions": [{"type": "Version", "id": x["id"]}
                                    for x in (existing.get("versions") or [])] + want})
            added += len(want)
    if dry:
        log("DRY RUN, nothing written")
    else:
        log("created %d playlist(s), added %d member(s)" % (made, added))
    return 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("a shot's playlist is named after the shot",
       playlist_name("SHOW01_A_0010") == "REVIEW_SHOW01_A_0010")

    ent = {"type": "Shot", "id": 1, "name": "SHOW01_A_0010"}
    vs = [{"id": 1, "code": "A_v001", "sg_status_list": "rev", "entity": ent},
          {"id": 2, "code": "A_v002", "sg_status_list": "rev", "entity": ent},
          {"id": 3, "code": "A_v003", "sg_status_list": "apr", "entity": ent},
          {"id": 4, "code": "A_v004", "sg_status_list": "rjct", "entity": ent},
          {"id": 5, "code": "A_v005", "sg_status_list": "omt", "entity": ent},
          {"id": 6, "code": "B_v001", "sg_status_list": "rev", "entity": None}]
    g, skipped = plan(vs)
    ck("only PENDING candidates are grouped",
       [v["id"] for v in g["REVIEW_SHOW01_A_0010"]] == [1, 2])
    # The two that would undo the housekeeping watcher's work.
    ck("CANARY an APPROVED candidate is never pulled back into review",
       all(v["id"] != 3 for v in g["REVIEW_SHOW01_A_0010"]))
    ck("CANARY a REJECTED or OMITTED one is not either",
       all(v["id"] not in (4, 5) for v in g["REVIEW_SHOW01_A_0010"]))
    ck("a Version with no Shot is reported, not filed under a guess",
       skipped == ["B_v001"] and len(g) == 1)

    class _Stub(object):
        def __init__(self, existing=None):
            self.existing, self.created, self.updated = existing, [], []

        def find(self, *a, **k):
            return vs

        def find_one(self, *a, **k):
            return self.existing

        def create(self, et, data):
            self.created.append(data)
            return {"id": 9}

        def update(self, et, i, data):
            self.updated.append(data)

    st = _Stub()
    run(st, dry=True)
    ck("a dry run writes nothing", not st.created and not st.updated)
    st2 = _Stub()
    run(st2, dry=False)
    ck("a new shot becomes one playlist holding both pending cells",
       len(st2.created) == 1 and len(st2.created[0]["versions"]) == 2)
    st3 = _Stub(existing={"id": 7, "code": "REVIEW_SHOW01_A_0010",
                          "versions": [{"id": 1}, {"id": 2}]})
    run(st3, dry=False)
    ck("a complete playlist is left alone on a second run (idempotent)",
       not st3.created and not st3.updated)
    st4 = _Stub(existing={"id": 7, "code": "REVIEW_SHOW01_A_0010",
                          "versions": [{"id": 1}]})
    run(st4, dry=False)
    ck("a partial playlist gains only what is missing",
       len(st4.updated) == 1 and len(st4.updated[0]["versions"]) == 2)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default="SHOW01")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), episode=a.episode, dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
