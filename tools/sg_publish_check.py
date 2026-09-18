#!/usr/bin/env python3
"""Real ShotGrid Version viewability check.

Geoff: "My own verification was the deeper failure. An earlier pass checked
'does this Version have media OR a thumbnail' and reported zero missing.
True, and useless -- a thumbnail alone is not reviewable. The check must be
'does this open and play', verified against the real media URL." This module
is that check, and ONLY this check (invariant 11: one place, imported by
tests/sg_preflight.py, never re-copied).

WHAT "VIEWABLE" MEANS HERE: Version.sg_uploaded_movie is populated AND its
signed media URL actually resolves -- HTTP 200/206, a plausible
video/*-or-image/* content-type, and a non-trivial body size. A populated
`image` (thumbnail) field is explicitly NOT sufficient: the entire audit this
module exists to prevent a repeat of was 205 Versions with a thumbnail and
`sg_uploaded_movie` == None, "reviewable" by a naive presence check and
unplayable in ShotGrid.

CANARY (ship this, per the brief): call check_viewable() on a Version whose
sg_uploaded_movie is empty and confirm it FAILS with "EMPTY" in the detail,
without making any HTTP request at all (there is nothing to fetch). See
self_test() below, and tests/sg_preflight.py's --check-viewable, which was
run for real against this project's own not-yet-backfilled board Versions
before the backfill (see docs/METHOD.md) as the live
proof this canary actually fires.
"""
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 20
MIN_BYTES = 256
PLAUSIBLE_PREFIXES = ("video/", "image/")


def _default_fetch(url, timeout):
    """GET the first 64KB (Range request -- S3/ShotGrid media honors it, and
    this avoids pulling a multi-hundred-MB movie over the wire just to prove
    it exists). Returns (status, headers, body_bytes)."""
    req = urllib.request.Request(url, headers={"Range": "bytes=0-65535"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, dict(resp.headers.items()), resp.read()


def check_viewable(sg, version_id, timeout=DEFAULT_TIMEOUT, min_bytes=MIN_BYTES,
                   fetch=None):
    """Fetch Version `version_id`'s sg_uploaded_movie and confirm it is
    actually viewable. Returns (ok: bool, detail: str) -- never raises for an
    ordinary "not viewable" outcome (that is the expected shape of a failing
    check, not an error), but a genuinely broken sg connection/lookup does
    propagate.

    fetch(url, timeout) -> (status, headers_dict, body_bytes) is injectable
    for tests; defaults to a real Range GET."""
    fetch = fetch or _default_fetch

    row = sg.find_one("Version", [["id", "is", version_id]],
                      ["code", "sg_uploaded_movie", "image"])
    if not row:
        return False, "no such Version %s" % version_id
    code = row.get("code") or ("Version %s" % version_id)

    media = row.get("sg_uploaded_movie")
    if not media or not media.get("url"):
        # THE canary case: exactly the 205-Version bug (thumbnail present,
        # sg_uploaded_movie empty). A thumbnail alone never makes this True.
        thumb_note = " (a thumbnail IS present, but that is not enough)" if row.get("image") \
                    else " (no thumbnail either)"
        return False, "%s: sg_uploaded_movie is EMPTY -- not viewable%s" % (code, thumb_note)

    url = media["url"]
    declared_ct = media.get("content_type") or ""
    try:
        status, headers, body = fetch(url, timeout)
    except Exception as exc:                              # noqa: BLE001
        return False, "%s: media URL fetch FAILED: %s: %s" % (code, type(exc).__name__, exc)

    if status not in (200, 206):
        return False, "%s: media URL returned HTTP %s" % (code, status)

    content_type = (headers.get("Content-Type") or headers.get("content-type")
                    or declared_ct or "")
    if not content_type.lower().startswith(PLAUSIBLE_PREFIXES):
        return False, "%s: implausible content-type %r" % (code, content_type)

    total_size = None
    content_range = headers.get("Content-Range") or headers.get("content-range")
    if content_range and "/" in content_range:
        try:
            total_size = int(content_range.rsplit("/", 1)[1])
        except ValueError:
            total_size = None
    if total_size is None:
        cl = headers.get("Content-Length") or headers.get("content-length")
        if cl:
            try:
                total_size = int(cl)
            except ValueError:
                total_size = None
    effective_size = total_size if total_size is not None else len(body)

    if effective_size < min_bytes or len(body) == 0:
        return False, ("%s: media body implausibly small (%d bytes total, %d fetched)"
                       % (code, effective_size, len(body)))

    return True, ("%s: VIEWABLE (HTTP %s, %s, %d byte(s) total)"
                  % (code, status, content_type, effective_size))


# ---------------------------------------------------------------------- self-test
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-62s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    class _StubSG:
        def __init__(self, rows):
            self.rows = rows

        def find_one(self, entity, filters, fields):
            vid = None
            for f in filters:
                if f[0] == "id":
                    vid = f[2]
            return self.rows.get(vid)

    # CANARY: thumbnail present, sg_uploaded_movie empty -> FAILS, no fetch attempted.
    fetch_calls = []

    def _should_not_be_called(url, timeout):
        fetch_calls.append(url)
        raise AssertionError("fetch must not be called when sg_uploaded_movie is empty")

    sg = _StubSG({1: {"code": "PILOT01_A_0010_BRD_board_v001", "sg_uploaded_movie": None,
                      "image": "https://example/thumb.jpg"}})
    ok, detail = check_viewable(sg, 1, fetch=_should_not_be_called)
    ck("CANARY: a thumbnail-only Version (sg_uploaded_movie empty) FAILS check_viewable",
       ok is False and "EMPTY" in detail)
    ck("CANARY: check_viewable does not attempt an HTTP fetch when there is nothing to fetch",
       fetch_calls == [])

    sg_none = _StubSG({2: {"code": "NOTHING_v001", "sg_uploaded_movie": None, "image": None}})
    ok, detail = check_viewable(sg_none, 2, fetch=_should_not_be_called)
    ck("a Version with neither media nor thumbnail also fails, and says so",
       ok is False and "no thumbnail either" in detail)

    # missing Version entirely
    ok, detail = check_viewable(_StubSG({}), 999, fetch=_should_not_be_called)
    ck("a nonexistent Version id fails cleanly instead of raising", ok is False)

    # real media, good fetch
    def _good_fetch(url, timeout):
        return 200, {"Content-Type": "video/mp4", "Content-Length": "500000"}, b"x" * 65536

    sg_ok = _StubSG({3: {"code": "GOOD_v001",
                        "sg_uploaded_movie": {"url": "https://x/good.mp4",
                                              "content_type": "video/mp4"},
                        "image": "https://x/thumb.jpg"}})
    ok, detail = check_viewable(sg_ok, 3, fetch=_good_fetch)
    ck("a Version with a real, fetchable video passes", ok is True and "VIEWABLE" in detail)

    # HTTP error
    def _404_fetch(url, timeout):
        return 404, {"Content-Type": "text/html"}, b"not found"

    ok, detail = check_viewable(sg_ok, 3, fetch=_404_fetch)
    ck("CANARY: an HTTP 404 on the media URL fails the check", ok is False and "404" in detail)

    # network exception
    def _boom_fetch(url, timeout):
        raise TimeoutError("boom")

    ok, detail = check_viewable(sg_ok, 3, fetch=_boom_fetch)
    ck("CANARY: a fetch exception (network failure) fails the check, not an unhandled raise",
       ok is False and "FAILED" in detail)

    # implausible content-type
    def _wrong_ct_fetch(url, timeout):
        return 200, {"Content-Type": "text/html", "Content-Length": "500000"}, b"<html>"

    ok, detail = check_viewable(sg_ok, 3, fetch=_wrong_ct_fetch)
    ck("CANARY: a 200 response with a non-media content-type still fails "
       "(a redirect-to-login page would look like '200 OK' otherwise)",
       ok is False and "content-type" in detail)

    # zero-byte body
    def _empty_fetch(url, timeout):
        return 200, {"Content-Type": "video/mp4", "Content-Length": "0"}, b""

    ok, detail = check_viewable(sg_ok, 3, fetch=_empty_fetch)
    ck("CANARY: a 200 response with a zero-length body fails the check",
       ok is False and "small" in detail)

    # partial content (Range honored) with Content-Range total size
    def _range_fetch(url, timeout):
        return 206, {"Content-Type": "image/png", "Content-Range": "bytes 0-65535/900000"}, \
              b"y" * 65536

    ok, detail = check_viewable(sg_ok, 3, fetch=_range_fetch)
    ck("a 206 partial-content response with Content-Range is accepted (still image case)",
       ok is True)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
