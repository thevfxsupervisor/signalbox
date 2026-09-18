#!/usr/bin/env python3
"""Is what EXECUTES the same as what is COMMITTED? Answer it every cycle.

WHY THIS EXISTS. Geoff, 2026-09-06: *"How are you going to make a system to
ensure commits and deployment doesn't fall out of sync unintentionally."*

Because it already did, for five hours (F098). Every fix made that day sat
committed and inert while the standing service ran bytes from 12:29, so the trap
Geoff had asked to have fixed was still armed in the live process while the
record said it was fixed. It was found by accident: housekeeping's dry-run said
"6 to move" and the running service said "0", the same tool disagreeing with
itself about the same data.

**Nothing was watching the gap, because the gap is invisible from both ends.**
`git status` is clean when everything is committed. The service is healthy when
it is running. Only the COMPARISON is informative, and nobody was making it.

THE MEASUREMENT. A release directory is named
`<timestamp>_<short-sha>[_label]`, and `build/CURRENT_RELEASE.txt` names the one
that executes. So the deployed commit is recoverable from the pointer alone, and
comparing it to `git rev-parse HEAD` is the whole check.

WHAT IT REPORTS, in the order that matters:

  1. DRIFT: how many commits the running release is behind HEAD.
  2. WHICH TOOLS changed in between, because "12 commits behind" is a number
     and "sg_review_housekeeping.py changed" is a decision.
  3. Whether the working tree is dirty, which is a different problem with the
     same symptom: code that exists nowhere but this disk.

IT DOES NOT DEPLOY. Deciding to restart a standing service is not a check's
business, and a checker that fixes things is one nobody can run safely.

    python deploy_drift.py              exit 1 if the running release is behind
    python deploy_drift.py --quiet      one line, for a service cycle
    python deploy_drift.py --self-test
"""
import argparse
import os
import re
import subprocess
import sys

_PROJECT_ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = (_REPO_ROOT if os.path.exists(os.path.join(_REPO_ROOT, "docs", "ARCHITECTURE.md"))
        else _PROJECT_ROOT)
POINTER = os.path.join(ROOT, "build", "CURRENT_RELEASE.txt")
# <timestamp>_<sha>[_label]; the sha is the second underscore-separated field
# and is hex. Anchored so a label containing an underscore cannot be read as one.
RELEASE = re.compile(r"^\d{8}-\d{6}-\d+_([0-9a-f]{7,40})(?:_.*)?$")


def deployed_sha(pointer=None):
    """-> the short sha the running release was built from, or None."""
    pointer = pointer or POINTER
    if not os.path.exists(pointer):
        return None
    with open(pointer, encoding="utf-8") as fh:
        name = fh.read().strip()
    m = RELEASE.match(name)
    return m.group(1) if m else None


def _git(args, repo=None):
    r = subprocess.run(["git", "-C", repo or ROOT] + args,
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def drift(pointer=None, repo=None):
    """-> dict describing the gap. Pure enough to test; the git calls are the
    only side of it that needs a real repo."""
    dep = deployed_sha(pointer)
    head = _git(["rev-parse", "--short", "HEAD"], repo)
    if dep is None:
        return {"state": "no-pointer", "deployed": None, "head": head}
    if head is None:
        return {"state": "no-git", "deployed": dep, "head": None}
    if head.startswith(dep) or dep.startswith(head):
        dirty = _git(["status", "--porcelain"], repo)
        return {"state": "current", "deployed": dep, "head": head,
                "behind": 0, "changed": [], "dirty": len(dirty.splitlines()) if dirty else 0}
    behind = _git(["rev-list", "--count", "%s..HEAD" % dep], repo)
    # ops/ IS EXECUTABLE and was missing from this list. `ops/run_service.ps1` is
    # the launcher the service actually starts from and `ops/deploy_config.json`
    # is the one named place that says where the release lives; a change to
    # either would have been reported as "docs only", which is the opposite of
    # true. Found 2026-09-07 while fixing the quiet-mode line below.
    changed = _git(["diff", "--name-only", "%s..HEAD" % dep, "--",
                    "tools/", "workflows/", "ops/"], repo)
    dirty = _git(["status", "--porcelain"], repo)
    return {"state": "behind", "deployed": dep, "head": head,
            "behind": int(behind) if behind and behind.isdigit() else None,
            "changed": [c for c in (changed or "").splitlines() if c],
            "dirty": len(dirty.splitlines()) if dirty else 0}


def render(d, quiet=False):
    """-> (lines, exit_code). One line when quiet, because this runs per cycle."""
    st = d["state"]
    if st == "current":
        line = "deploy: running %s, matches HEAD" % d["deployed"]
        if d.get("dirty"):
            return ([line + "; %d UNCOMMITTED file(s), which exist nowhere else"
                     % d["dirty"]], 1)
        return ([line], 0)
    if st == "no-pointer":
        return (["deploy: NO release pointer at %s -- nothing is deployed" % POINTER], 1)
    if st == "no-git":
        return (["deploy: running %s but git cannot say what HEAD is, so drift is "
                 "UNKNOWN rather than zero" % d["deployed"]], 1)
    head = "%d commit(s)" % d["behind"] if d.get("behind") is not None else "an unknown number"
    lines = ["deploy: DRIFTED. Running %s, HEAD is %s, %s behind."
             % (d["deployed"], d["head"], head)]
    if d["changed"]:
        lines.append("  %d executable file(s) changed since the running release:"
                     % len(d["changed"]))
        for c in d["changed"][:8]:
            lines.append("    %s" % c)
        if len(d["changed"]) > 8:
            lines.append("    ... and %d more" % (len(d["changed"]) - 8))
        lines.append("  Those fixes are committed and NOT running. Deploy or say why not.")
    else:
        lines.append("  No tools/ or workflows/ changes, so the gap is docs only.")
    if d.get("dirty"):
        lines.append("  Also %d uncommitted file(s)." % d["dirty"])
    if quiet:
        # THE QUIET LINE MUST CARRY THE QUALIFIER, NOT JUST THE ALARM.
        #
        # This used to return lines[:1], which is the word DRIFTED and a commit
        # count, and it dropped the one clause that decides whether anyone
        # should care: whether any EXECUTABLE file actually changed. The service
        # prints this every cycle, so on a day of heavy documentation and
        # findings commits the operator sees "DRIFTED, 3 commits behind" on
        # repeat while the running code is byte-identical to the working tree.
        #
        # Measured 2026-09-07: exactly that, three commits behind, zero
        # executable changes, and `cmp` on the deployed panel_compose.py against
        # the working copy returned identical. **A warning that fires when
        # nothing is wrong is how a real warning gets ignored**, and this one
        # would have been on screen during tomorrow's rehearsal.
        if not d["changed"]:
            return (["deploy: running %s, %s behind HEAD (%s) but NO executable "
                     "change: the gap is docs and record only"
                     % (d["deployed"], head, d["head"])], 0)
        return (lines[:1], 1)
    return (lines, 1)


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "CURRENT_RELEASE.txt")
    with open(p, "w") as fh:
        fh.write("20260906-185236-505746_61c7cf0\n")
    ck("the sha is recovered from a release pointer", deployed_sha(p) == "61c7cf0")
    with open(p, "w") as fh:
        fh.write("20260906-122905-701377_0fe8681_ground-contact-clause\n")
    ck("CANARY: a LABEL after the sha does not confuse it",
       deployed_sha(p) == "0fe8681")
    with open(p, "w") as fh:
        fh.write("garbage\n")
    ck("CANARY: an unparseable pointer is None, never a guess", deployed_sha(p) is None)
    ck("a missing pointer is None", deployed_sha(os.path.join(d, "nope.txt")) is None)

    lines, code = render({"state": "current", "deployed": "abc1234",
                          "head": "abc1234", "behind": 0, "changed": [], "dirty": 0})
    ck("a matching release exits 0", code == 0 and "matches HEAD" in lines[0])
    lines, code = render({"state": "current", "deployed": "abc1234", "head": "abc1234",
                          "behind": 0, "changed": [], "dirty": 3})
    ck("CANARY: uncommitted files FAIL even when the release matches",
       code == 1 and "UNCOMMITTED" in lines[0])
    lines, code = render({"state": "behind", "deployed": "aaa", "head": "bbb", "behind": 12,
                          "changed": ["tools/sg_review_housekeeping.py"], "dirty": 0})
    ck("CANARY: a drifted release FAILS and names the changed tool",
       code == 1 and any("sg_review_housekeeping" in x for x in lines))
    lines, code = render({"state": "no-git", "deployed": "aaa", "head": None})
    ck("CANARY: 'git cannot answer' is UNKNOWN drift, not zero drift",
       code == 1 and "UNKNOWN" in lines[0])
    lines, code = render({"state": "no-pointer", "deployed": None, "head": "abc"})
    ck("nothing deployed at all is a failure, not a pass", code == 1)

    live = drift()
    print("     LIVE: %s" % render(live)[0][0])
    ck("the real project answers the question at all", live["state"] != "no-pointer")

    # --- THE QUIET LINE IS THE ONE THE SERVICE PRINTS EVERY CYCLE, so it is
    # --- the one that must not cry wolf. A warning that fires when nothing is
    # --- wrong is how a real warning gets ignored.
    _docs = {"state": "behind", "deployed": "aaaaaaa", "head": "bbbbbbb",
             "behind": 3, "changed": [], "dirty": 0}
    _q, _rc = render(_docs, quiet=True)
    ck("CANARY a docs-only gap does NOT say DRIFTED in the quiet line",
       "DRIFTED" not in _q[0])
    ck("...and it SAYS there is no executable change, so the reader can tell "
       "it apart from a real one",
       "NO executable change" in _q[0])
    ck("...and it exits 0, because nothing needs doing",  _rc == 0)
    _real = {"state": "behind", "deployed": "aaaaaaa", "head": "bbbbbbb",
             "behind": 1, "changed": ["tools/panel_compose.py"], "dirty": 0}
    _q2, _rc2 = render(_real, quiet=True)
    ck("CANARY a REAL executable gap still says DRIFTED and still exits 1, or "
       "this fix has disarmed the check it was meant to sharpen",
       "DRIFTED" in _q2[0] and _rc2 == 1)
    ck("CANARY ops/ counts as EXECUTABLE: run_service.ps1 is the launcher and "
       "deploy_config.json is the one named place",
       "ops/" in __import__("inspect").getsource(drift))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quiet", action="store_true", help="one line, for a service cycle")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    lines, code = render(drift(), quiet=a.quiet)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
