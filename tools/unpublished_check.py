#!/usr/bin/env python3
"""Find deliverables that were RENDERED and never PUBLISHED.

WHY THIS EXISTS. Geoff, 2026-09-06, on finding a cut sitting on disk with no
ShotGrid Version: *"how can something be rendered and not published? I thought
we agreed to use auto-publishing mechanisms to avoid that?"*

We did agree, and the mechanism is real: `episode_assemble.py` publishes by
default and `--no-publish` is an explicit opt-out. That is not enough, and the
reason is the shape this project keeps hitting:

  **A rendered-but-unpublished file is INDISTINGUISHABLE from a published one
  by looking at the disk.** Same directory, same name, same bytes. One flag, or
  one lower-level code path that skips the publisher, and a deliverable becomes
  a private file with nothing to notice it. `SHOW01_SUBS_DEMO_cut_v003.mp4` sat
  unpublished for four hours and was found by a human, not by us.

So this asks the question from the OTHER SIDE, which is the standing rule for
every check here: not "did the publish step run" (that is a fact about our own
action) but "does every rendered cut have a Version" (a fact about the record).

WHAT IT CHECKS. Every cut in output/episodes/ must have a ShotGrid Version
whose code matches the file stem. A cut without one is reported and FAILS.

WHAT IT DELIBERATELY DOES NOT DO. It does not publish anything. A missing
Version may mean the render was abandoned, superseded, or a test; publishing it
automatically would put junk in the record, which is the opposite of the point.
It makes the gap visible and a human decides.

    python unpublished_check.py              exit 1 if any cut is unpublished
    python unpublished_check.py --self-test
"""
import argparse
import os
import re
import sys

_PROJECT_ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = (_REPO_ROOT if os.path.exists(os.path.join(_REPO_ROOT, "docs", "ARCHITECTURE.md"))
        else _PROJECT_ROOT)
EPISODES = os.path.join(ROOT, "output", "episodes")
# A cut, as episode_assemble names them: <LABEL>_cut_v###.mp4
CUT = re.compile(r"^(.+_cut_v\d{3})\.mp4$", re.I)


def rendered_cuts(episodes_dir=None):
    """-> [code] for every cut file on disk, newest first."""
    d = episodes_dir or EPISODES
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        m = CUT.match(fn)
        if m:
            out.append((os.path.getmtime(os.path.join(d, fn)), m.group(1)))
    return [c for _, c in sorted(out, reverse=True)]


def published_codes(sg):
    """-> set of Version codes that exist in ShotGrid."""
    raw = getattr(sg, "_sg", sg)
    return set(v["code"] for v in raw.find("Version", [["code", "contains", "_cut_v"]], ["code"])
               if v.get("code"))


def compare(on_disk, in_shotgrid):
    """-> [code] rendered but not published. Pure, so it is testable."""
    return [c for c in on_disk if c not in in_shotgrid]


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("a cut present in both is not reported",
       compare(["A_cut_v001"], {"A_cut_v001"}) == [])
    ck("CANARY: a cut on disk with no Version IS reported",
       compare(["A_cut_v001", "B_cut_v002"], {"A_cut_v001"}) == ["B_cut_v002"])
    ck("CANARY: an empty ShotGrid reports everything, not nothing",
       compare(["A_cut_v001"], set()) == ["A_cut_v001"])
    ck("a Version with no file is NOT reported (that is not this check's job)",
       compare([], {"A_cut_v001"}) == [])

    # The filename pattern must match what episode_assemble actually writes.
    ck("the cut pattern matches a real assembler filename",
       CUT.match("SHOW01_SUBS_DEMO_cut_v003.mp4").group(1) == "SHOW01_SUBS_DEMO_cut_v003")
    ck("the pattern does not match a stray mp4", CUT.match("shot_0010_a14b_v001.mp4") is None)

    found = rendered_cuts()
    print("     %d cut(s) on disk in %s" % (len(found), EPISODES))
    ck("the episodes directory is readable and holds cuts", len(found) > 0)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from episode_assemble import sg_connect

    on_disk = rendered_cuts()
    missing = compare(on_disk, published_codes(sg_connect()))
    if missing:
        print("RENDERED BUT NOT PUBLISHED (%d):" % len(missing))
        for c in missing:
            print("  - %s" % c)
        print("A file on disk is not a deliverable. Publish it or delete it.")
        return 1
    print("every one of %d rendered cut(s) has a ShotGrid Version" % len(on_disk))
    return 0


if __name__ == "__main__":
    sys.exit(main())
