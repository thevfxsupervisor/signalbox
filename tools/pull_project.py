#!/usr/bin/env python3
"""Keep the E: working tree current with what the other seat pushed.

WHY. Geoff, 2026-09-06: *"Coordinate with a peer engineer how you guys are going to keep
our important work current on E. It is writing to C. Is that the smart plan?"*

a peer engineer writing to its own local disk IS the smart plan for a working clone: that
is the fleet's disk rule, work on fast local disk and land the shared artefact
on the share. What was missing is the second half. a peer engineer pushes to GitHub, and
**nothing brought those commits into the E: working tree** except me happening
to run `git pull`. Measured when Geoff asked: E: was one commit behind origin,
and no scheduled task existed to fix that.

So the share went stale whenever this seat was not actively working, which is
precisely when nobody would notice.

THE GUARD, and it is the reason this is a tool and not a cron one-liner. This
pulls into a LIVE WORKING TREE that a person and a service are both using:

  * If the tree is DIRTY it does nothing and says so. Never stash, never
    discard: uncommitted work on this box exists nowhere else.
  * `--ff-only`, so a divergence is reported rather than merged into a shape
    nobody chose.
  * It never pushes. Publishing is a decision.

It also does NOT deploy. A pull changes what is COMMITTED here; what EXECUTES
is `build/CURRENT_RELEASE.txt`, and `tools/deploy_drift.py` reports that gap
separately, every service cycle.

    python pull_project.py            pull if clean, else report and stop
    python pull_project.py --check    say what it would do, change nothing
    python pull_project.py --self-test
"""
import argparse
import os
import subprocess
import sys

_PROJECT_ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = (_REPO_ROOT if os.path.exists(os.path.join(_REPO_ROOT, "docs", "ARCHITECTURE.md"))
        else _PROJECT_ROOT)


def _git(args, repo=None):
    r = subprocess.run(["git", "-C", repo or ROOT] + args,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def survey(repo=None):
    """-> dict of the facts a decision needs, with no decision taken."""
    rc, dirty, _ = _git(["status", "--porcelain"], repo)
    if rc != 0:
        return {"state": "no-git"}
    _git(["fetch", "--quiet", "origin", "main"], repo)
    _, behind, _ = _git(["rev-list", "--count", "HEAD..origin/main"], repo)
    _, ahead, _ = _git(["rev-list", "--count", "origin/main..HEAD"], repo)
    return {"state": "ok",
            "dirty": [x for x in dirty.splitlines() if x],
            "behind": int(behind) if behind.isdigit() else None,
            "ahead": int(ahead) if ahead.isdigit() else None}


def decide(s):
    """-> (action, reason). Pure, so every branch is testable without a repo."""
    if s.get("state") != "ok":
        return "abort", "not a git repository"
    if s.get("behind") is None:
        return "abort", "cannot tell how far behind origin is; refusing to guess"
    if s["dirty"]:
        return "skip", ("%d uncommitted file(s) on this box, which exist nowhere else. "
                        "Refusing to pull over them; commit or stash by hand."
                        % len(s["dirty"]))
    if s["behind"] == 0:
        return "current", "already current with origin/main"
    if s.get("ahead"):
        return "diverged", ("%d behind AND %d ahead: a fast-forward is not possible, "
                            "so this needs a human" % (s["behind"], s["ahead"]))
    return "pull", "%d commit(s) behind origin/main, fast-forward is safe" % s["behind"]


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("clean and behind -> pull",
       decide({"state": "ok", "dirty": [], "behind": 3, "ahead": 0})[0] == "pull")
    ck("clean and current -> nothing to do",
       decide({"state": "ok", "dirty": [], "behind": 0, "ahead": 0})[0] == "current")
    ck("CANARY: a DIRTY tree is never pulled over, however far behind",
       decide({"state": "ok", "dirty": ["M x.py"], "behind": 9, "ahead": 0})[0] == "skip")
    ck("CANARY: diverged needs a human, it is not a fast-forward",
       decide({"state": "ok", "dirty": [], "behind": 2, "ahead": 1})[0] == "diverged")
    ck("CANARY: an unknown distance aborts rather than guessing",
       decide({"state": "ok", "dirty": [], "behind": None, "ahead": 0})[0] == "abort")
    ck("a non-repo aborts", decide({"state": "no-git"})[0] == "abort")

    live = survey()
    act, why = decide(live)
    print("     LIVE: %s -- %s" % (act, why))
    ck("the real tree answers the question", live.get("state") == "ok")

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    s = survey()
    act, why = decide(s)
    if act != "pull" or a.check:
        print("pull_project: %s -- %s" % (act, why))
        return 0 if act in ("current", "pull") else 1
    rc, out, err = _git(["pull", "--ff-only", "origin", "main"])
    if rc != 0:
        print("pull_project: FAILED -- %s" % (err or out)[:300])
        return 1
    print("pull_project: pulled %d commit(s); now at %s"
          % (s["behind"], _git(["rev-parse", "--short", "HEAD"])[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
