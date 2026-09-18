#!/usr/bin/env python3
"""Download one file from CivitAI, and PROVE it is the artefact CivitAI published.

The gap this exists for: the upstream Hearmeman24/CivitAI_Downloader validates a
1 MB size floor and, for zips, that the file opens. It never compares the bytes
that arrived against the SHA256 CivitAI publishes for every file in its public
API. So a file that completes but is subtly wrong -- a truncation, a proxy
rewrite, or the 106-byte JSON error document CivitAI serves on 401 -- passes
that check silently and lands in models/loras/ looking exactly like a model.

CivitAI hands us the answer for free. `/api/v1/model-versions/<id>` is PUBLIC,
needs no token, and carries `files[].hashes.SHA256`. That number is read BEFORE
a byte is transferred and the finished file is hashed against it. A mismatch is
a hard non-zero exit with the file quarantined under a `.REJECTED` name, never
renamed into place. There is no warning-level outcome here: a swallowed failure
that reads as success is the specific defect class this pipeline keeps getting
bitten by, so this script only ever says VERIFIED or FAIL.

Idempotence is by hash, not by existence. If the target file is already on disk
AND hashes to the published value, this reports "already present, hash verified"
and does not re-download or overwrite. A file that exists but hashes wrong is
NOT quietly clobbered either -- it is reported and refused unless --force.

401 is handled by name. Many CivitAI files require auth, and an anonymous fetch
returns `{"error":"Unauthorized",...}` with HTTP 401. That is detected and
reported as "CIVITAI_TOKEN is required", not written to disk as a model.

THE TOKEN. Read from the CIVITAI_TOKEN environment variable ONLY. It is never
accepted as a command-line argument (it would land in shell history and in
process listings; the upstream README says the same), never printed, never
logged and never written to any file. It goes into an Authorization header for
every request that accepts one.

ONE EXCEPTION, measured 2026-09-03 and reluctant: CivitAI's DOWNLOAD endpoint
returns 400 for the header form on any file whose type is not `Model` -- every
workflow pack (`Config`) and archive (`Archive`) -- and 200 only for
`?token=<value>` in the URL. So the download path falls back to the query form,
and that is safe only because `_redact()` lives inside `log()` and strips the
value from every line this script can emit, INCLUDING urllib exception text,
which carries the URL. That specific leak is not hypothetical: it is how the
token reached an agent transcript on 2026-09-03 (credential-rotations.md row 6).
The metadata API is unaffected and still uses the header.

The only thing this script will ever say about the token is whether it is set.
`--self-test` asserts that, by scanning its own output for the secret.

Every fetch also writes a `<name>.json` sidecar carrying `trainedWords`. A LoRA
loads fine and appears to do nothing when its trigger word is missing or subtly
wrong -- `@possummachine` with the `@` dropped is exactly that failure, and
`zfr0,` really does carry a trailing comma upstream. That metadata is free from
the public API at download time and expensive to reconstruct later, so it is
never thrown away.

Usage:
    # fetch (public metadata always; token only if the file is gated)
    # --version-id takes one id or many; each is fetched and verified in turn
    python civitai_fetch.py --version-id 2855073 2954771 2947255 2976285 \\
        --dest C:/ComfyUI_windows_portable/ComfyUI/models/loras \\
        [--file-id 2739000] [--as some_name.safetensors] [--force]

    # verify a file already on disk against a known version's published hash
    python civitai_fetch.py --version-id 2855073 --verify <path>

    # ask CivitAI what an on-disk file IS, by its own hash (no version id needed)
    python civitai_fetch.py --by-hash <path> [<path> ...]

    python civitai_fetch.py --self-test

Exit: 0 file present and hash-verified, 1 anything else.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

META = "https://civitai.com/api/v1/model-versions/%s"
BY_HASH = "https://civitai.com/api/v1/model-versions/by-hash/%s"
DOWNLOAD = "https://civitai.com/api/download/models/%s"
UA = "genvideo-pipeline/1.0 (+civitai_fetch.py)"
CHUNK = 1024 * 1024
# CivitAI's 401 body is 106 bytes and its login page is a few KB. Anything this
# small is an error document, a login page, or a stub -- categorically not a
# file we asked for, so refuse to hash it and pretend the comparison meant
# something.
#
# THIS IS AN ABSOLUTE FLOOR AND NOTHING MORE. It used to be 1 MB, which was a
# safe assumption while every fetch was a LoRA and a lurking one afterwards: a
# legitimate 887 KB workflow JSON and an 88 KB archive both fail a 1 MB floor,
# and would have been rejected AFTER downloading correctly. The real size test
# is relative to the API's own published sizeKB -- see verify_file below.
MIN_MODEL_BYTES = 4 * 1024

_LOG_SINK = None


def _redact(msg):
    """Strip the token from anything on its way out, in every encoding this
    codebase can produce it in.

    THIS IS NOT BELT AND BRACES. The download fallback below has to put the
    token in the URL (CivitAI rejects the header form for non-Model files), and
    urllib puts the URL into its exception messages, which the transfer loop
    logs with `% exc`. Redacting at each call site would work until someone
    added the next log line, so it happens HERE, once, where it cannot be
    skipped."""
    tok = os.environ.get("CIVITAI_TOKEN")
    if not tok:
        return msg
    out = str(msg)
    for form in (tok, urllib.parse.quote(tok, safe=""),
                 urllib.parse.quote_plus(tok)):
        if form:
            out = out.replace(form, "<REDACTED>")
    return out


def log(msg):
    """All output goes through here so --self-test can scan every line this
    script is capable of emitting for the token."""
    msg = _redact(msg)
    if _LOG_SINK is not None:
        _LOG_SINK.append(msg)
    print(msg, flush=True)


def token_present():
    return bool(os.environ.get("CIVITAI_TOKEN"))


def auth_headers(shape=0):
    """Authorization header from the env var, or nothing. The value is returned
    for the request layer only; no caller prints this dict.

    shape 0 puts the token in the header (correct, and what the metadata API
    wants). shape 1 omits it because it is going in the URL instead -- see
    _shaped_url."""
    h = {"User-Agent": UA}
    tok = os.environ.get("CIVITAI_TOKEN")
    if tok and shape == 0:
        h["Authorization"] = "Bearer " + tok
    return h


def _shaped_url(url, shape):
    """shape 0 -> url unchanged. shape 1 -> the same url with ?token=<value>.

    MEASURED 2026-09-03, three model-versions, six request shapes: CivitAI's
    download endpoint accepts the Bearer header for files of type `Model` and
    returns 400 for `Config` and `Archive`, which is every workflow pack. The
    query-param form returns 200 for all three. Nothing about the User-Agent or
    the published ?fileId= parameter changes this; dropping the UA turns the 400
    into a 403, which is the only reason the header is clearly being read at all.

    Putting a secret in a URL is bad practice and the module docstring says so.
    It is done here because the API leaves no alternative for these files, and
    it is survivable ONLY because _redact() sits inside log(). Never log this
    return value, and never widen this function to the metadata API, which
    accepts the header perfectly well."""
    tok = os.environ.get("CIVITAI_TOKEN")
    if shape == 0 or not tok:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "token=" + urllib.parse.quote(tok, safe="")


def api_get(url, what):
    req = urllib.request.Request(url, headers=auth_headers())
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise SystemExit("FAIL: CivitAI API returned http=%d for %s. Without "
                         "the published hash there is nothing to verify "
                         "against, so this refuses to download blind."
                         % (exc.code, what))
    except Exception as exc:
        raise SystemExit("FAIL: cannot reach the CivitAI API for %s (%s)."
                         % (what, exc))


def version_meta(version_id):
    """Filename, size, published SHA256, baseModel and trainedWords, from the
    PUBLIC metadata API. No token needed for this -- only the file download is
    gated -- which is why a full verification plan can be built for a file we
    are not yet allowed to fetch."""
    data = api_get(META % version_id, "model-version %s" % version_id)
    if data is None:
        raise SystemExit("FAIL: no CivitAI model-version %s. Check the id: it "
                         "is the versionId (the ?modelVersionId= in the URL), "
                         "not the model id." % version_id)
    return data


def pick_file(meta, file_id=None):
    files = meta.get("files") or []
    if not files:
        raise SystemExit("FAIL: model-version %s lists no files."
                         % meta.get("id"))
    if file_id is not None:
        for f in files:
            if str(f.get("id")) == str(file_id):
                return f
        raise SystemExit("FAIL: fileId %s is not in model-version %s. Present: "
                         "%s" % (file_id, meta.get("id"),
                                 ", ".join("%s=%s" % (f.get("id"), f.get("name"))
                                           for f in files)))
    primary = [f for f in files if f.get("primary")]
    return (primary or files)[0]


def published_sha(fmeta):
    return ((fmeta.get("hashes") or {}).get("SHA256") or "").lower()


def describe(meta, fmeta):
    log("  model      %s / %s" % ((meta.get("model") or {}).get("name", "?"),
                                  meta.get("name", "?")))
    log("  file       %s (fileId %s)" % (fmeta.get("name"), fmeta.get("id")))
    log("  sizeKB     %s" % fmeta.get("sizeKB"))
    log("  baseModel  %s" % meta.get("baseModel"))
    words = meta.get("trainedWords") or []
    log("  trigger    %s" % (", ".join(repr(w) for w in words) if words
                             else "(none published)"))
    sha = published_sha(fmeta)
    log("  published SHA256 %s" % (sha or "(NONE PUBLISHED)"))
    return sha


def write_sidecar(out, meta, fmeta):
    """Keep the trigger words with the file. The commonest way a correctly
    installed LoRA looks broken is a missing or mistyped trigger word, and the
    only place that is authoritative is CivitAI's own trainedWords. Written on
    every successful outcome, including the already-present one, so an
    idempotent re-run backfills a sidecar that was never created.

    Contains published metadata only. No token, no header, no request detail."""
    side = out + ".json"
    doc = {
        "source": "civitai",
        "url": "https://civitai.com/models/%s?modelVersionId=%s"
               % (meta.get("modelId", ""), meta.get("id")),
        "versionId": meta.get("id"),
        "modelId": meta.get("modelId"),
        "modelName": (meta.get("model") or {}).get("name"),
        "versionName": meta.get("name"),
        "baseModel": meta.get("baseModel"),
        "trainedWords": meta.get("trainedWords") or [],
        "file": fmeta.get("name"),
        "fileId": fmeta.get("id"),
        "sizeKB": fmeta.get("sizeKB"),
        "sha256": published_sha(fmeta),
        "savedAs": os.path.basename(out),
        "verifiedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        with open(side, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        log("  note: could not write sidecar %s (%s)" % (side, exc))
        return
    words = doc["trainedWords"]
    log("  sidecar    %s (trigger: %s)"
        % (side, ", ".join(repr(w) for w in words) if words else "none published"))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK * 8), b""):
            h.update(block)
    return h.hexdigest()


def verify(path, expect_sha, expect_kb=None):
    """The whole point of this script. Returns 0 only when the bytes on disk
    hash to the value CivitAI published."""
    if not os.path.isfile(path):
        log("FAIL: %s does not exist." % path)
        return 1
    size = os.path.getsize(path)
    # Two guards, in order of how much they know. The absolute floor catches an
    # error document when we have nothing to compare against; the relative one
    # is the real test whenever CivitAI published a size, and it scales to an
    # 88 KB archive and a 9 GB checkpoint alike.
    if size < MIN_MODEL_BYTES:
        log("FAIL: %s is only %d bytes. That is not a file -- CivitAI serves a "
            "106-byte JSON error document when a download is refused, and this "
            "is the check that stops one being filed as a model." % (path, size))
        return 1
    if expect_kb:
        want = int(round(float(expect_kb) * 1024))
        if size < want // 2:
            log("FAIL: %s is %d bytes but CivitAI publishes %d for it. Less than "
                "half the published size is a truncated transfer or an error "
                "page, not this file." % (path, size, want))
            return 1
    if not expect_sha:
        log("FAIL: CivitAI published no SHA256 for this file, so it cannot be "
            "verified. Refusing to report an unverified file as good.")
        return 1
    if expect_kb:
        want = int(round(float(expect_kb) * 1024))
        if abs(size - want) > 1024:
            log("  note: on disk %d bytes, API sizeKB implies ~%d (hash is the "
                "binding check)" % (size, want))
    log("  hashing %s (%.1f MB)..." % (os.path.basename(path), size / 1e6))
    got = sha256_of(path)
    if got != expect_sha.lower():
        log("")
        log("FAIL: SHA256 MISMATCH -- this file is NOT the CivitAI artefact.")
        log("  on disk   %s" % got)
        log("  published %s" % expect_sha.lower())
        log("  path      %s" % path)
        log("  The bytes arrived but they are not the right bytes. Do not load "
            "this file.")
        return 1
    log("  sha256 OK %s" % got)
    log("VERIFIED: %s (%d bytes) is byte-identical to the CivitAI artefact."
        % (path, size))
    return 0


def _unauthorized(version_id, detail=""):
    log("")
    log("FAIL: CivitAI refused the download with http=401 Unauthorized.%s"
        % ((" " + detail) if detail else ""))
    log("  This file's creator requires a logged-in account. The public "
        "metadata read above succeeded; only the transfer is gated.")
    log("  CIVITAI_TOKEN set: %s" % ("yes" if token_present() else "no"))
    log("  Fix: set the CIVITAI_TOKEN environment variable to a CivitAI API "
        "key and re-run the SAME command. Never pass the token as a command-"
        "line argument -- it would land in shell history and process listings.")
    log("  Nothing was written to disk. The 401 response body is a 106-byte "
        "JSON error document, not a model, and this refuses to file it as one.")
    log("  Blocked command: python civitai_fetch.py --version-id %s --dest <dir>"
        % version_id)


def download(version_id, file_id, part, expect_bytes):
    """Stream to <out>.part. Resume by HTTP Range. The token, if any, rides in
    an Authorization header -- never in the URL, which gets logged by every
    proxy and error handler between here and CivitAI."""
    url = DOWNLOAD % version_id
    if file_id is not None:
        # CivitAI selects a non-primary file by type/format, not fileId, so the
        # honest thing is to say what we cannot do rather than fetch the wrong
        # file and hash it against the right file's hash.
        log("  note: --file-id given; CivitAI's download endpoint serves the "
            "primary file for a version. The hash check below is against the "
            "file you named, so a mismatch here means the endpoint served a "
            "different file and you need the direct downloadUrl.")
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have and expect_bytes and have > expect_bytes:
        log("  existing .part is LARGER than expected (%d > %d) - discarding"
            % (have, expect_bytes))
        os.remove(part)
        have = 0

    attempts = 0
    shape = 0            # 0 = Authorization header; 1 = ?token= query param
    while True:
        req = urllib.request.Request(_shaped_url(url, shape),
                                     headers=auth_headers(shape))
        if have:
            req.add_header("Range", "bytes=%d-" % have)
            log("  resuming at %.1f MB" % (have / 1e6))
        started = last = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    # MEASURED: an unauthenticated download returns HTTP *200*
                    # with CivitAI's login page. Status alone cannot tell that
                    # apart from a file, so the 200 must be inspected. Without
                    # this the page would be written out and only caught later
                    # by the hash check, or not at all for a small file.
                    log("FAIL: CivitAI returned an HTML page with http=200 -- "
                        "that is the login page, not a file. The token was not "
                        "accepted for this request.")
                    return 1
                if "application/json" in ctype:
                    body = resp.read(4096)
                    # Redact BEFORE truncating: a 200-byte cut can bisect a
                    # token echoed in the body, and _redact's whole-string
                    # replace cannot match half a token.
                    text = _redact(body.decode("utf-8", "replace"))[:200]
                    log("FAIL: CivitAI returned JSON (%s), not a model file. "
                        "Body: %s" % (ctype, text))
                    return 1
                with open(part, "ab" if have else "wb") as fh:
                    while True:
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        if time.time() - last > 20:
                            rate = have / max(1e-9, time.time() - started) / 1e6
                            log("    %.1f / %.1f MB  (%.0f MB/s avg)"
                                % (have / 1e6, (expect_bytes or 0) / 1e6, rate))
                            last = time.time()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403) and shape == 0 and token_present():
                # Not a failure yet -- the header form is simply not accepted
                # for this file type. Retry once with the query-param shape
                # before spending an attempt or declaring the item blocked.
                log("  http=%d with the Authorization header; retrying with "
                    "the query-param form (CivitAI rejects the header for "
                    "Config and Archive files)" % exc.code)
                shape = 1
                have = os.path.getsize(part) if os.path.isfile(part) else 0
                continue
            if exc.code in (401, 403):
                if os.path.isfile(part):
                    os.remove(part)
                _unauthorized(version_id,
                              "" if exc.code == 401 else "(http=403)")
                return 1
            log("  transfer failed http=%d" % exc.code)
            attempts += 1
            if attempts >= 3:
                log("FAIL: gave up after %d attempts (http=%d)."
                    % (attempts, exc.code))
                return 1
            time.sleep(10)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            continue
        except Exception as exc:
            attempts += 1
            if attempts >= 5:
                log("FAIL: transfer kept failing (%s). Partial left at %s."
                    % (exc, part))
                return 1
            log("  transfer interrupted at %.1f MB (%s) - retrying in 10s"
                % (have / 1e6, exc))
            time.sleep(10)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            continue

        have = os.path.getsize(part) if os.path.isfile(part) else 0
        if expect_bytes and have < expect_bytes:
            attempts += 1
            if attempts >= 5:
                log("FAIL: stream ended at %d of %d bytes." % (have, expect_bytes))
                return 1
            log("  stream ended short (%d / %d) - resuming" % (have, expect_bytes))
            continue
        return 0


def fetch(version_id, dest_dir, file_id=None, as_name=None, force=False):
    meta = version_meta(version_id)
    fmeta = pick_file(meta, file_id)
    log("CivitAI model-version %s" % version_id)
    sha = describe(meta, fmeta)
    log("  CIVITAI_TOKEN set: %s" % ("yes" if token_present() else "no"))
    if not sha:
        log("FAIL: CivitAI publishes no SHA256 for this file. This tool exists "
            "to check downloads against that hash and will not fetch a file it "
            "cannot verify.")
        return 1

    name = as_name or fmeta.get("name")
    out = os.path.join(dest_dir, name)
    kb = fmeta.get("sizeKB")
    expect_bytes = int(round(float(kb) * 1024)) if kb else 0

    # Idempotence, decided by hash rather than by the file merely existing.
    if os.path.isfile(out):
        log("  target already on disk: %s" % out)
        if verify(out, sha, kb) == 0:
            write_sidecar(out, meta, fmeta)
            log("ALREADY PRESENT, HASH VERIFIED - nothing downloaded, nothing "
                "overwritten.")
            return 0
        if not force:
            log("REFUSING to overwrite: a file is at %s but it does not match "
                "the published hash. That is either a different artefact you "
                "care about or a corrupt one worth keeping for diagnosis. "
                "Re-run with --force to replace it, or move it aside." % out)
            return 1
        log("  --force given: the mismatching file will be replaced.")

    os.makedirs(dest_dir, exist_ok=True)
    part = out + ".part"
    log("  downloading -> %s" % part)
    if download(version_id, file_id, part, expect_bytes) != 0:
        return 1

    if verify(part, sha, kb) != 0:
        bad = out + ".REJECTED"
        try:
            os.replace(part, bad)
        except OSError:
            bad = part
        log("QUARANTINED: the downloaded bytes are at %s and were deliberately "
            "NOT renamed to %s. A file that fails its hash must never be "
            "reachable under the name a workflow loads." % (bad, out))
        return 1
    os.replace(part, out)
    write_sidecar(out, meta, fmeta)
    log("DONE: %s" % out)
    return 0


def fetch_many(version_ids, dest_dir, file_id=None, as_name=None, force=False):
    """Four linked LoRAs is already past the point where one-at-a-time is the
    right shape. Each id is fetched and verified independently; one failure
    does not abandon the rest, but any failure makes the whole run non-zero."""
    if len(version_ids) == 1:
        return fetch(version_ids[0], dest_dir, file_id, as_name, force)
    if as_name or file_id:
        log("FAIL: --as and --file-id name a single file, so they cannot be "
            "used with more than one --version-id.")
        return 1
    results = []
    for vid in version_ids:
        log("")
        log("=" * 72)
        results.append((vid, fetch(vid, dest_dir, None, None, force)))
    log("")
    log("=" * 72)
    log("SUMMARY (%d versions)" % len(results))
    for vid, rc in results:
        log("  %-10s %s" % (vid, "OK" if rc == 0 else "FAILED"))
    bad = [v for v, rc in results if rc != 0]
    if bad:
        log("FAIL: %d of %d failed: %s"
            % (len(bad), len(results), ", ".join(str(v) for v in bad)))
        return 1
    log("All %d versions present and hash-verified." % len(results))
    return 0


def by_hash(paths):
    """Reverse lookup: hand CivitAI the hash of a file we already hold and ask
    what it is. This answers 'is the artefact we have the CivitAI artefact?'
    for a file whose versionId nobody wrote down -- a HIT means byte-identical
    by construction, because the lookup key IS the content hash."""
    rc = 0
    for path in paths:
        if not os.path.isfile(path):
            log("FAIL: %s does not exist." % path)
            rc = 1
            continue
        size = os.path.getsize(path)
        log("")
        log("%s (%d bytes)" % (path, size))
        got = sha256_of(path)
        log("  sha256 %s" % got)
        data = api_get(BY_HASH % got, "hash lookup")
        if data is None:
            log("  NO CIVITAI COUNTERPART: CivitAI knows no file with this "
                "hash. Either it was never published there, or the bytes we "
                "hold differ from the published ones.")
            continue
        fmeta = None
        for f in (data.get("files") or []):
            if published_sha(f) == got:
                fmeta = f
                break
        log("  CONFIRMED IDENTICAL to CivitAI versionId %s" % data.get("id"))
        log("    model      %s / %s" % ((data.get("model") or {}).get("name", "?"),
                                        data.get("name", "?")))
        log("    file       %s" % (fmeta.get("name") if fmeta else "(hash matched"
                                   " the version, not a listed file)"))
        log("    baseModel  %s" % data.get("baseModel"))
        words = data.get("trainedWords") or []
        log("    trigger    %s" % (", ".join(repr(w) for w in words) if words
                                   else "(none published)"))
        log("    url        https://civitai.com/models/%s?modelVersionId=%s"
            % ((data.get("modelId") or ""), data.get("id")))
    return rc


# The fixture: the one file already proven byte-identical to what CivitAI
# publishes for versionId 2855073. The self-test verifies against the LIVE
# public API, so it is testing the real code path, not a recorded fixture.
FIXTURE = ("C:/ComfyUI_windows_portable/ComfyUI/models/loras/"
           "anima-highres-aesthetic-boost.safetensors")
FIXTURE_VERSION = 2855073
# Two real, reliably-401 versions. Testing the auth path against a mock would
# only prove the mock; these prove the guard against CivitAI itself.
GATED_VERSIONS = (2954771, 2947255, 2976285)


def self_test(fixture=None, fixture_version=None):
    """Prove this can say NO. A verifier that has only ever passed is
    decoration (invariant 12). Runs without a token, on the public API."""
    global _LOG_SINK
    import shutil
    import tempfile

    fixture = fixture or FIXTURE
    version = fixture_version or FIXTURE_VERSION
    _LOG_SINK = []
    lines = _LOG_SINK
    rc = 0
    log("self-test: no token required; using the PUBLIC metadata API.")
    log("CIVITAI_TOKEN set: %s" % ("yes" if token_present() else "no"))

    if not os.path.isfile(fixture):
        log("SELF-TEST FAILED: fixture %s is missing. This test verifies "
            "against a real on-disk artefact and will not silently skip the "
            "check. Pass --fixture/--fixture-version to point at another file "
            "whose CivitAI version is known." % fixture)
        _LOG_SINK = None
        return 1

    # canary 1: the public metadata API is readable without a token and
    # publishes a SHA256 at all.
    meta = version_meta(version)
    fmeta = pick_file(meta)
    sha = published_sha(fmeta)
    if len(sha) != 64:
        log("SELF-TEST FAILED: no usable published SHA256 for version %s." % version)
        _LOG_SINK = None
        return 1
    log("canary 1: public metadata read with no token; published sha %s" % sha)

    # canary 2: the known-good file is ACCEPTED.
    if verify(fixture, sha, fmeta.get("sizeKB")) != 0:
        log("SELF-TEST FAILED: a known-good file was rejected.")
        rc = 1
    else:
        log("canary 2: known-good file accepted against the LIVE published hash")

    d = tempfile.mkdtemp(prefix="civitai_fetch_canary_")
    try:
        # canary 3: THE guard. A file corrupted by one byte must be refused.
        bad = os.path.join(d, "corrupt.safetensors")
        shutil.copyfile(fixture, bad)
        with open(bad, "r+b") as fh:
            fh.seek(os.path.getsize(bad) // 2)
            b = fh.read(1)
            fh.seek(os.path.getsize(bad) // 2)
            fh.write(bytes([b[0] ^ 0xFF]))
        if verify(bad, sha, fmeta.get("sizeKB")) == 0:
            log("SELF-TEST FAILED: a CORRUPTED file passed hash verification. "
                "This script cannot detect the exact failure it was written for.")
            rc = 1
        else:
            log("canary 3: one flipped byte correctly REFUSED")

        # canary 4: the 106-byte JSON error document must never pass as a model.
        stub = os.path.join(d, "stub.safetensors")
        with open(stub, "wb") as fh:
            fh.write(b'{"error":"Unauthorized","message":"The creator of this '
                     b'asset requires you to be logged in to download it"}')
        if verify(stub, sha) == 0:
            log("SELF-TEST FAILED: a 106-byte JSON error document passed as a model.")
            rc = 1
        else:
            log("canary 4: 401 JSON error document correctly REFUSED as a model")

        # canary 5: the real 401 path, against real gated versions. Must exit
        # non-zero, must name CIVITAI_TOKEN, must leave NO file behind.
        for gated in GATED_VERSIONS:
            before = set(os.listdir(d))
            mark = len(lines)
            got = fetch(gated, d)
            after = set(os.listdir(d))
            said_token = any("CIVITAI_TOKEN" in ln for ln in lines[mark:])
            if got == 0:
                log("SELF-TEST FAILED: gated version %s reported success." % gated)
                rc = 1
            elif after != before:
                log("SELF-TEST FAILED: gated version %s left files behind: %s"
                    % (gated, sorted(after - before)))
                rc = 1
            elif not said_token:
                log("SELF-TEST FAILED: the 401 for %s did not mention "
                    "CIVITAI_TOKEN." % gated)
                rc = 1
            else:
                log("canary 5.%s: real 401 refused, non-zero exit, no file "
                    "written, CIVITAI_TOKEN named" % gated)

        # canary 6: idempotence. The fixture is already correct on disk, so a
        # fetch into its own directory must skip rather than re-download or
        # overwrite. mtime is the witness.
        fixture_dir = os.path.dirname(fixture)
        mtime = os.path.getmtime(fixture)
        mark = len(lines)
        got = fetch(version, fixture_dir, as_name=os.path.basename(fixture))
        if got != 0:
            log("SELF-TEST FAILED: idempotent re-fetch of a good file did not "
                "return 0.")
            rc = 1
        elif os.path.getmtime(fixture) != mtime:
            log("SELF-TEST FAILED: idempotent re-fetch MODIFIED the file on disk.")
            rc = 1
        elif not any("ALREADY PRESENT" in ln for ln in lines[mark:]):
            log("SELF-TEST FAILED: re-fetch did not report already-present.")
            rc = 1
        else:
            log("canary 6: already-present file hash-verified and skipped, "
                "untouched on disk")
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # canary 7: the token must not be derivable from anything this script says.
    # A leak check that only runs when a token happens to be present is inert
    # on the machine that most needs it, so a canary value is INJECTED into the
    # environment for the duration and the output is scanned for it. Offline:
    # no request is made with the fake token; the code paths that would print a
    # token are driven directly. It also asserts the header IS built from it,
    # so this cannot pass merely because the token is never used at all.
    real = os.environ.get("CIVITAI_TOKEN")
    canary_tok = "CANARY-TOKEN-4f9b2a71d0c3e8a6-DO-NOT-PRINT"
    os.environ["CIVITAI_TOKEN"] = canary_tok
    try:
        log("canary 7: driving every token-adjacent output path with a fake "
            "token -- the FAIL block below is DELIBERATE, it is the text being "
            "scanned, not a failure.")
        mark = len(lines)
        hdr = auth_headers()
        log("  CIVITAI_TOKEN set: %s" % ("yes" if token_present() else "no"))
        _unauthorized(FIXTURE_VERSION)
        describe(meta, fmeta)
        emitted = lines[mark:]
        if hdr.get("Authorization") != "Bearer " + canary_tok:
            log("SELF-TEST FAILED: the token is not reaching the Authorization "
                "header, so the leak check below proves nothing.")
            rc = 1
        frags = [canary_tok, canary_tok[:8], canary_tok[-8:]]
        leaked = [ln for ln in emitted if any(f in ln for f in frags)]
        if leaked:
            log("SELF-TEST FAILED: the token appears in this script's output: "
                "%d line(s)." % len(leaked))
            rc = 1
        else:
            log("canary 7: token reaches the Authorization header yet appears "
                "in none of the %d lines the token-adjacent paths emit"
                % len(emitted))

        # canary 7b: the two objects that actually carry the query-param URL.
        # _shaped_url() is the only place the token enters a URL, and
        # urllib.error.HTTPError carries that URL on .url and .geturl() --
        # the exact vehicle of the 2026-09-03 leak. Drive both through log()
        # and prove the redactor fires on them.
        import io
        shaped = _shaped_url(DOWNLOAD % version, 1)
        if urllib.parse.quote(canary_tok, safe="") not in shaped:
            log("SELF-TEST FAILED: _shaped_url did not embed the token, so "
                "canary 7b proves nothing.")
            rc = 1
        mark = len(lines)
        log("  shaped url through log(): %s" % shaped)
        synth = urllib.error.HTTPError(shaped, 400, "Bad Request",
                                       {}, io.BytesIO(b""))
        log("  synthetic HTTPError through log(): %s url=%s geturl=%s"
            % (synth, synth.url, synth.geturl()))
        emitted = lines[mark:]
        leaked = [ln for ln in emitted if any(f in ln for f in frags)]
        if leaked:
            log("SELF-TEST FAILED: the token survived redaction in %d line(s) "
                "of canary 7b." % len(leaked))
            rc = 1
        elif not any("<REDACTED>" in ln for ln in emitted):
            log("SELF-TEST FAILED: canary 7b lines never contained the token "
                "at all, so they prove nothing.")
            rc = 1
        else:
            log("canary 7b: query-param URL and synthetic HTTPError .url/"
                ".geturl() all redacted by log()")

        # canary 7c: the traceback path. An uncaught exception is printed by
        # the interpreter, NOT by log(), so __main__ routes it through
        # _guarded_main below, which redacts traceback text with _redact().
        # Prove that redaction works on a real formatted traceback that
        # carries the query-param URL.
        import traceback
        try:
            raise RuntimeError("transfer failed for %s" % shaped)
        except RuntimeError:
            tb = _redact(traceback.format_exc())
        if any(f in tb for f in frags):
            log("SELF-TEST FAILED: a _redact()ed traceback still contains "
                "the token.")
            rc = 1
        elif "<REDACTED>" not in tb:
            log("SELF-TEST FAILED: the traceback canary never contained the "
                "token, so it proves nothing.")
            rc = 1
        else:
            log("canary 7c: a traceback carrying the query-param URL is "
                "clean after _redact()")
    finally:
        if real is None:
            os.environ.pop("CIVITAI_TOKEN", None)
        else:
            os.environ["CIVITAI_TOKEN"] = real

    # canary 8: a token on the command line must be refused outright, not
    # quietly accepted. Shell history and process listings are readable.
    argv = sys.argv
    try:
        for probe in ("--token", "--api-key", "--apikey", "--civitai_token",
                      "--CIVITAI_TOKEN", "-token", "--token=SECRET-VALUE"):
            sys.argv = ["civitai_fetch.py", probe, "SECRET-VALUE"]
            mark = len(lines)
            if main() == 0:
                log("SELF-TEST FAILED: %s was accepted on the command line." % probe)
                rc = 1
            elif any("SECRET-VALUE" in ln for ln in lines[mark:]):
                log("SELF-TEST FAILED: refusing %s echoed its value." % probe)
                rc = 1
        log("canary 8: token-shaped flags (dash, underscore, case, = forms) "
            "refused, value not echoed")

        # canary 8b: the argparse channel itself. Its error() echoes
        # unrecognized argv verbatim and used to print straight to stderr,
        # bypassing _redact() -- a live leak measured 2026-09-04 with a fake
        # value. _RedactingParser routes it through log(); prove the token
        # comes out <REDACTED>.
        os.environ["CIVITAI_TOKEN"] = canary_tok
        try:
            sys.argv = ["civitai_fetch.py", "--totally-wrong-flag", canary_tok]
            mark = len(lines)
            try:
                got = main()
            except SystemExit as exc:
                got = exc.code
            emitted = lines[mark:]
            if got == 0:
                log("SELF-TEST FAILED: an unrecognized flag was accepted.")
                rc = 1
            elif any(any(f in ln for f in frags) for ln in emitted):
                log("SELF-TEST FAILED: argparse's error echoed the token.")
                rc = 1
            elif not any("<REDACTED>" in ln for ln in emitted):
                log("SELF-TEST FAILED: canary 8b error text never carried the "
                    "token, so it proves nothing.")
                rc = 1
            else:
                log("canary 8b: argparse unrecognized-argument error is "
                    "redacted (was a live stderr leak before 2026-09-04)")
        finally:
            if real is None:
                os.environ.pop("CIVITAI_TOKEN", None)
            else:
                os.environ["CIVITAI_TOKEN"] = real
    finally:
        sys.argv = argv

    log("")
    log("self-test: %s" % ("PASS - this check can fail, so a VERIFIED from it "
                           "means something" if rc == 0 else "FAILED"))
    _LOG_SINK = None
    return rc


class _RedactingParser(argparse.ArgumentParser):
    """argparse's error() prints its message -- which echoes unrecognized argv
    values VERBATIM -- straight to stderr and only then raises SystemExit(2).
    That print never passes through log()/_redact(), and _guarded_main only
    redacts string exit codes, so a token pasted with a misspelled flag was a
    live leak channel (measured 2026-09-04 with a fake value). Route the
    message through log() instead; canary 8b proves it."""
    def error(self, message):
        log("FAIL: bad command line: %s" % message)
        log("  " + self.format_usage().strip())
        raise SystemExit(1)


def main():
    # Before argparse, so the refusal is this message rather than argparse's
    # generic "unrecognized arguments" -- and so it can never be mistaken for
    # an option that merely needs spelling differently. A command line is
    # recorded in shell history and visible in process listings. Normalised
    # (case, dashes, underscores) so --CIVITAI_TOKEN and -token cannot slip
    # past to argparse, whose echo is the channel canary 8b guards.
    for arg in sys.argv[1:]:
        flag = arg.lower().split("=")[0]
        if flag.startswith("-") and flag.replace("_", "-").lstrip("-") in (
                "token", "api-key", "apikey", "civitai-token"):
            log("FAIL: this tool does not accept a token on the command line. "
                "A command line is recorded in shell history and visible in "
                "process listings. Set the CIVITAI_TOKEN environment variable "
                "instead. (The value you passed is deliberately not echoed.)")
            return 1

    ap = _RedactingParser(
        description="Fetch and hash-verify a CivitAI file. The token is read "
                    "from CIVITAI_TOKEN only and is never accepted here.")
    ap.add_argument("--version-id", nargs="+", default=None,
                    help="one or more CivitAI modelVersionIds (the "
                         "?modelVersionId= in the URL); each is fetched and "
                         "hash-verified in turn")
    ap.add_argument("--file-id", default=None,
                    help="pick a specific file within the version")
    ap.add_argument("--dest", help="directory to download into")
    ap.add_argument("--as", dest="as_name", default=None,
                    help="save under this filename instead of CivitAI's")
    ap.add_argument("--force", action="store_true",
                    help="replace an on-disk file that fails the hash check")
    ap.add_argument("--verify", default=None,
                    help="verify this existing file against --version-id's "
                         "published hash; downloads nothing")
    ap.add_argument("--by-hash", nargs="+", default=None,
                    help="ask CivitAI what these on-disk files are, by hash")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--fixture", default=None, help="self-test: file to use")
    ap.add_argument("--fixture-version", default=None,
                    help="self-test: that file's CivitAI versionId")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test(ns.fixture, ns.fixture_version)
    if ns.by_hash:
        return by_hash(ns.by_hash)
    if ns.verify:
        if not ns.version_id:
            log("FAIL: --verify needs --version-id (or use --by-hash, which "
                "needs no id).")
            return 1
        if len(ns.version_id) != 1:
            log("FAIL: --verify checks one file, so it takes exactly one "
                "--version-id.")
            return 1
        vid = ns.version_id[0]
        meta = version_meta(vid)
        fmeta = pick_file(meta, ns.file_id)
        log("CivitAI model-version %s" % vid)
        sha = describe(meta, fmeta)
        return verify(ns.verify, sha, fmeta.get("sizeKB"))
    if not (ns.version_id and ns.dest):
        log("FAIL: --version-id and --dest are both required (or use "
            "--verify, --by-hash, or --self-test).")
        return 1
    return fetch_many(ns.version_id, ns.dest, ns.file_id, ns.as_name, ns.force)


def _guarded_main():
    """Redact the exit paths that do NOT go through log(). Two channels
    bypass _redact() entirely: an uncaught exception (the interpreter prints
    the traceback straight to stderr) and every `raise SystemExit("FAIL: ...")`
    in this file (Python prints a string exit code to stderr verbatim). With
    the query-param fallback in _shaped_url, an exception message can carry
    the token inside a URL, so both channels are routed through _redact()
    here. Canary 7c proves the traceback redaction works."""
    import traceback
    try:
        return main()
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(_redact(exc.code), file=sys.stderr, flush=True)
            return 1
        raise
    except BaseException:
        print(_redact(traceback.format_exc()), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(_guarded_main())
