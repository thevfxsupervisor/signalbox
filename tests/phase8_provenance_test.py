#!/usr/bin/env python3
"""Phase 8 (Provenance extension) acceptance tests.

Two parts, per the phase's acceptance criteria in MASTER-PLAN-V2.md:

  1. OFFLINE (--self-test, no ShotGrid needed): re-runs sg_provenance.py's own
     canary suite, which is the pure-logic proof that a blanked field IS
     detected as missing and that write_provenance() surfaces a write failure
     loudly instead of swallowing it.

  2. LIVE (--live, needs SHOTGRID_SCRIPT_KEY/SITE_URL/SCRIPT_NAME in env - run
     this through C:\\genvideo\\sg.ps1): does the real thing end to end against
     ShotGrid -

       a. creates one synthetic PHASE8_CANARY_v001 Version on a real Shot
       b. writes full D6 provenance onto it via sg_provenance.write_provenance
       c. runs sg_preflight.py --check-version against it: MUST PASS
       d. blanks ONE field directly via the ShotGrid API (simulating a
          publisher that forgot it)
       e. runs sg_preflight.py --check-version again: MUST FAIL, and the
          failure output is printed here so it is not just asserted, it is
          SHOWN (invariant 2: a canary must be provably able to fail)
       f. retires the synthetic Version, leaving no residue in ShotGrid

Usage:
    python phase8_provenance_test.py --self-test    (default if no flag given)
    python phase8_provenance_test.py --live
"""
import os
import subprocess
import sys

ROOT = r"C:\example\genvideo-pipeline"
TOOLS = os.path.join(ROOT, "build", "tools")
TESTS = os.path.join(ROOT, "build", "tests")
PY = sys.executable

sys.path.insert(0, TOOLS)
import sg_provenance as PROV                                   # noqa: E402

PROJ = {"type": "Project", "id": 9999}
CANARY_CODE = "PHASE8_CANARY_v001"


def run_preflight_check(version_id, require_anchor=False):
    """Shell out to sg_preflight.py exactly the way an operator would, so this
    test proves the SHIPPED CLI behaves correctly, not just the library
    function underneath it."""
    cmd = [PY, os.path.join(TESTS, "sg_preflight.py"), "--connect",
          "--check-version", str(version_id)]
    if require_anchor:
        cmd.append("--require-anchor")
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def live():
    for n in ("SHOTGRID_SITE_URL", "SHOTGRID_SCRIPT_NAME", "SHOTGRID_SCRIPT_KEY"):
        if not os.environ.get(n):
            sys.exit("FATAL: %s not set (run this via C:\\genvideo\\sg.ps1)" % n)
    import shotgun_api3
    sg = shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                              script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                              api_key=os.environ["SHOTGRID_SCRIPT_KEY"])

    fails = []

    def ck(name, cond):
        print("  %-58s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    shot = sg.find_one("Shot", [["project", "is", PROJ]], ["code"])
    if not shot:
        sys.exit("no Shot in project 9999 to attach the canary Version to")

    existing = sg.find_one("Version", [["project", "is", PROJ],
                                       ["code", "is", CANARY_CODE]], ["id"])
    if existing:
        v = existing
        print("reusing existing canary Version id=%d (a previous run did not "
              "clean up)" % v["id"])
    else:
        v = sg.create("Version", {
            "project": PROJ, "entity": {"type": "Shot", "id": shot["id"]},
            "code": CANARY_CODE,
            "description": "Phase 8 acceptance test canary. Synthetic, created and "
                           "retired by phase8_provenance_test.py --live. If you are "
                           "reading this in ShotGrid, the test that made it did not "
                           "finish cleaning up.",
            "sg_status_list": "rev"})
        print("created canary Version id=%d on shot %s" % (v["id"], shot["code"]))

    try:
        ok = PROV.write_provenance(
            sg, v["id"], character="CHAR_TEST", set_="SET_TEST",
            action="test action: walks across frame", camera="wide static",
            style="1980s skate-deck matte test style",
            workflow_hash=PROV.workflow_hash_from_text("phase8-live-canary"),
            anchor_version_id=v["id"])
        ck("write_provenance() succeeded against real ShotGrid", ok)

        rc, out = run_preflight_check(v["id"], require_anchor=True)
        print("\n--- sg_preflight.py --check-version %d --require-anchor (COMPLETE) ---"
             % v["id"])
        print(out)
        ck("preflight PASSES on a fully-populated Version (rc=0)", rc == 0)

        # THE CANARY: blank one field and prove the check can fail.
        sg.update("Version", v["id"], {PROV.F_STYLE: ""})
        rc2, out2 = run_preflight_check(v["id"], require_anchor=True)
        print("\n--- sg_preflight.py --check-version %d --require-anchor "
             "(sg_component__style BLANKED) ---" % v["id"])
        print(out2)
        ck("CANARY: preflight FAILS when a field is blanked (rc=1)", rc2 == 1)
        ck("CANARY: the failure output NAMES the blanked field",
           PROV.F_STYLE in out2 and "FAILED" in out2)
    finally:
        sg.delete("Version", v["id"])
        print("\nretired canary Version id=%d - no residue left in ShotGrid" % v["id"])

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    if "--live" in sys.argv[1:]:
        return live()
    print("Phase 8 offline canary suite (sg_provenance.py's own self-test):\n")
    return PROV.self_test()


if __name__ == "__main__":
    sys.exit(main())
