#!/usr/bin/env python3
"""ShotGrid preflight: fail LOUDLY when the credential is absent.

Names match `build/reference/shotgrid-review-version-fieldset.md`, which is the
canonical source. An earlier version of this file invented its own names
(SG_API_KEY etc). That is worse than useless: Geoff sets the real variable and
the checker reports MISSING, so the setup looks broken when it is correct.
Canonical name first, historical aliases accepted so an early setup still works.

Only ONE of these is a secret:

  SHOTGRID_SCRIPT_KEY   the script user's API key. SECRET. Environment only:
                        never a file, never STATE.md, never the channel, never
                        E: (which is a synced share and would copy it to every
                        machine in the building).
  SHOTGRID_SITE_URL     the site address. Not secret, but read from env so no
                        environment is hardcoded into the tooling.
  SHOTGRID_SCRIPT_NAME  optional; defaults to "genvideo" per the canonical doc.

This never prints a value, only whether one is set and how long it is. A key
echoed to a terminal lands in a log, and a log lands on a share.

PHASE 8 EXTENSION (provenance). D6/invariant 9: every publish must carry the
provenance fields defined in build/tools/sg_provenance.py. A missing field is
a bug, so this preflight now has two more checks, both of which FAIL loudly:

  --check-schema             the Version entity actually has all 7 D6 fields
                              (by their REAL, post-rename codes - see
                              sg_provenance.py's module docstring). Catches a
                              deleted/renamed field before any publisher does.
  --check-version VERSION_ID  that ONE Version has every D6 text field
                              populated (non-empty, non-whitespace). Add
                              --require-anchor to also demand sg_anchor_version
                              be linked (only meaningful for publishers that
                              have an upstream anchor - see sg_provenance.py's
                              per-publisher comments for which do and do not).
                              This is what the Phase 8 blanked-field canary
                              exercises: publish, blank one field, run this,
                              watch it fail.

FOUNDATIONAL-PUBLISH EXTENSION (Geoff, 2026-08-28/29: "publishing results to
shotgrid ... get that right first"). A field being non-empty is not the same
as a Version being reviewable: an audit of project 9999 found 205 Versions
with a thumbnail and NO uploaded media - not openable, not playable. The
earlier check that reported "zero missing" tested "has media OR thumbnail"
instead of "does this actually resolve," which is exactly backwards.

  --check-viewable VERSION_ID  fetches that ONE Version's real media URL
                              (tools/sg_publish_check.py, the only place that
                              decides what "viewable" means) and confirms it
                              actually resolves: HTTP 200/206, a plausible
                              video/image content-type, non-trivial body
                              size. A populated thumbnail with an EMPTY
                              sg_uploaded_movie FAILS this check - that is
                              the canary: a Version with a thumbnail but no
                              uploaded media must fail, and does.

Usage:
    python sg_preflight.py                          check readiness, do not connect
    python sg_preflight.py --connect                 also attempt a real login
    python sg_preflight.py --connect --check-schema   also verify the D6 Version fields exist
    python sg_preflight.py --connect --check-version 12345 [--require-anchor]
    python sg_preflight.py --connect --check-viewable 12345   real media-URL fetch

Exit: 0 ready/pass, 1 not ready or a provenance/viewability check failed, 2 connect failed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "tools"))
import sg_provenance as PROV                                   # noqa: E402
import sg_publish_check as VCHECK                              # noqa: E402

KEY_NAMES = ["SHOTGRID_SCRIPT_KEY", "SG_API_KEY", "SG_SCRIPT_KEY"]
URL_NAMES = ["SHOTGRID_SITE_URL", "SG_SITE_URL", "SG_URL"]
NAME_NAMES = ["SHOTGRID_SCRIPT_NAME", "SG_SCRIPT_NAME"]
DEFAULT_SCRIPT_NAME = "genvideo"
PROJECT_ID = 9999


def first_set(names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return n, v
    return None, None


def check_schema(sg):
    """FAIL loudly if the Version entity is missing any D6 provenance field
    (by its real, post-rename code). Returns True/False; prints exactly which
    field(s) are gone."""
    project = {"type": "Project", "id": PROJECT_ID}
    schema = sg.schema_field_read("Version", project_entity=project)
    missing = [f for f in PROV.ALL_FIELDS if f not in schema]
    print("")
    print("Provenance schema check (Version entity, project %d):" % PROJECT_ID)
    for f in PROV.ALL_FIELDS:
        print("  %-32s %s" % (f, "present" if f in schema else "MISSING"))
    if missing:
        print("SCHEMA CHECK FAILED: %s" % ", ".join(missing))
        return False
    print("SCHEMA CHECK PASSED: all %d D6 fields present" % len(PROV.ALL_FIELDS))
    return True


def check_version(sg, version_id, require_anchor):
    """FAIL loudly if the named Version is missing any D6 provenance field.
    This is exactly what the blanked-field canary exercises."""
    row = sg.find_one("Version", [["id", "is", version_id]],
                      ["code"] + PROV.ALL_FIELDS)
    print("")
    if not row:
        print("VERSION CHECK FAILED: no Version %s" % version_id)
        return False
    print("Provenance check on Version %s (%s):" % (version_id, row.get("code")))
    for f in PROV.ALL_FIELDS:
        v = row.get(f)
        shown = v if f != PROV.F_ANCHOR else (v.get("name") or v.get("id") if v else None)
        print("  %-32s %s" % (f, repr(shown)[:70] if shown not in (None, "") else "BLANK"))
    missing = PROV.missing_fields(row, require_anchor=require_anchor)
    if missing:
        print("VERSION CHECK FAILED: missing/blank field(s): %s" % ", ".join(missing))
        return False
    print("VERSION CHECK PASSED: every D6 field is populated"
         + (" (anchor required and present)" if require_anchor else ""))
    return True


def check_viewable(sg, version_id):
    """FAIL loudly if the named Version's media does not actually resolve.
    This is the canary the brief asked for: a Version with a thumbnail but
    no uploaded media must fail, and this is what proves it."""
    print("")
    print("Viewability check (real media-URL fetch) on Version %s:" % version_id)
    ok, detail = VCHECK.check_viewable(sg, version_id)
    print("  %s" % detail)
    if not ok:
        print("VIEWABILITY CHECK FAILED")
        return False
    print("VIEWABILITY CHECK PASSED")
    return True


def main():
    argv = sys.argv[1:]
    want_connect = "--connect" in argv
    want_schema = "--check-schema" in argv
    require_anchor = "--require-anchor" in argv
    check_vid = None
    if "--check-version" in argv:
        i = argv.index("--check-version")
        if i + 1 >= len(argv):
            sys.exit("--check-version needs a Version id")
        try:
            check_vid = int(argv[i + 1])
        except ValueError:
            sys.exit("--check-version expects an integer id, got %r" % argv[i + 1])
    check_viewable_vid = None
    if "--check-viewable" in argv:
        i = argv.index("--check-viewable")
        if i + 1 >= len(argv):
            sys.exit("--check-viewable needs a Version id")
        try:
            check_viewable_vid = int(argv[i + 1])
        except ValueError:
            sys.exit("--check-viewable expects an integer id, got %r" % argv[i + 1])
    # A provenance/viewability check needs a live connection to read the
    # schema/Version; asking for one implies --connect rather than silently
    # doing nothing.
    want_connect = (want_connect or want_schema or (check_vid is not None)
                    or (check_viewable_vid is not None))
    print("ShotGrid preflight (values are never printed)")

    key_var, key = first_set(KEY_NAMES)
    url_var, url = first_set(URL_NAMES)
    name_var, script_name = first_set(NAME_NAMES)
    if not script_name:
        script_name = DEFAULT_SCRIPT_NAME
        name_var = "(default)"

    print("  %-8s %-22s %s" % ("KEY", key_var or KEY_NAMES[0],
                               ("SET (%d chars)" % len(key)) if key else "MISSING  <- the only secret"))
    print("  %-8s %-22s %s" % ("URL", url_var or URL_NAMES[0],
                               ("SET (%d chars)" % len(url)) if url else "MISSING"))
    print("  %-8s %-22s %s" % ("NAME", name_var, script_name))

    missing = []
    if not key:
        missing.append(KEY_NAMES[0])
    if not url:
        missing.append(URL_NAMES[0])

    if missing:
        print("")
        print("NOT READY. Phase 4 is blocked. This is a Geoff-only task by design.")
        print("")
        print("Set them so they survive a reboot (User scope, this box only):")
        for n in missing:
            print("  [Environment]::SetEnvironmentVariable('%s','<value>','User')" % n)
        print("")
        print("Then open a NEW terminal (env vars are read at process start) and re-run this.")
        print("Do NOT paste the key into a chat session, a file, STATE.md, or the channel.")
        return 1

    print("")
    print("READY.")
    if not want_connect:
        print("(no connection attempted; re-run with --connect to verify the login)")
        return 0

    try:
        import shotgun_api3
    except ImportError:
        print("shotgun_api3 is not installed in this interpreter")
        return 2

    try:
        sg = shotgun_api3.Shotgun(url, script_name=script_name, api_key=key)
        info = sg.info()
        print("connected: ShotGrid %s as script user %r" % (info.get("version"), script_name))
        projects = sg.find("Project", [], ["name"], limit=5)
        print("visible projects (first %d): %s"
              % (len(projects), ", ".join(p.get("name", "?") for p in projects) or "none"))

        ok = True
        if want_schema:
            ok = check_schema(sg) and ok
        if check_vid is not None:
            ok = check_version(sg, check_vid, require_anchor) and ok
        if check_viewable_vid is not None:
            ok = check_viewable(sg, check_viewable_vid) and ok
        if not ok:
            print("")
            print("PREFLIGHT FAILED: a provenance check above did not pass.")
            return 1
        return 0
    except Exception as exc:                      # noqa: BLE001
        # Deliberately not printing the exception repr: some client libraries
        # include the request payload, and the payload contains the key.
        print("CONNECT FAILED: %s" % type(exc).__name__)
        print("  check the site URL, that the script user %r exists, and that the key is valid"
              % script_name)
        return 2


if __name__ == "__main__":
    sys.exit(main())
