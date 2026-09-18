#!/usr/bin/env python3
"""Download one file from a public HuggingFace repo, and PROVE it arrived whole.

The failure this exists for: a large-model download that stops early. A
truncated .gguf on disk looks exactly like a good one to every check anybody
usually runs (the file is there, it is big, the shell said nothing), and it
does not announce itself until ComfyUI tries to load it and produces a
confusing tensor error somewhere far away from the real cause. Two of these
files are 8.7 GB; over a long transfer a silent truncation is not exotic.

So the expected size is fetched from the HF API FIRST, from the repo tree,
before a byte is transferred, and the file on disk is measured against that
number afterwards. A mismatch is a hard, loud failure and the partial file is
left as `<name>.part` rather than being renamed into place, so a half file can
never be mistaken for a finished one.

`--sha256` additionally hashes the finished file against the LFS oid the API
reports. That is the strongest check available, but it costs a full re-read of
8.7 GB, so it is opt-in rather than the default; size alone catches truncation,
which is the actual observed failure mode.

Resume is by HTTP Range against the existing `.part`, because on a 40 GB batch
restarting from zero after one dropped connection is its own kind of failure.

A check that has never failed is decoration, so `--self-test` writes a
deliberately short file and asserts this script REFUSES it.

Usage:
    python hf_fetch.py --repo QuantStack/Wan2.2-I2V-A14B-GGUF \\
        --path HighNoise/Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf \\
        --dest C:/ComfyUI_windows_portable/ComfyUI/models/diffusion_models \\
        [--as some_other_name.gguf] [--sha256]
    python hf_fetch.py --self-test

Exit: 0 file present and verified, 1 anything else.
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request

API = "https://huggingface.co/api/models/%s/tree/main/%s"
RESOLVE = "https://huggingface.co/%s/resolve/main/%s"
CHUNK = 1024 * 1024


def log(msg):
    print(msg, flush=True)


def remote_meta(repo, path):
    """Expected size (and LFS sha256 if the file is LFS) straight from the repo
    tree. This is the number everything else is judged against, so it is read
    from the API rather than guessed from a plan document: the plan's size
    column is an estimate written by a human, the API is the artifact."""
    parent = os.path.dirname(path)
    url = API % (repo, parent)
    try:
        with urllib.request.urlopen(url, timeout=90) as resp:
            entries = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise SystemExit("FAIL: cannot read the HF tree for %s/%s (%s). "
                         "Without the expected size there is nothing to verify "
                         "against, so this refuses to download blind."
                         % (repo, parent, exc))
    for e in entries:
        if e.get("path") == path and e.get("type") == "file":
            return e.get("size"), ((e.get("lfs") or {}).get("oid") or "")
    raise SystemExit("FAIL: %s is not in %s at main. Check the exact path and "
                     "capitalisation against the repo tree." % (path, repo))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK * 8), b""):
            h.update(block)
    return h.hexdigest()


def download(repo, path, out, expect_size):
    part = out + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    if have > expect_size:
        # A .part bigger than the target is not resumable, it is wrong.
        log("  existing .part is LARGER than expected (%d > %d) - discarding it"
            % (have, expect_size))
        os.remove(part)
        have = 0

    while have < expect_size:
        req = urllib.request.Request(RESOLVE % (repo, path))
        if have:
            req.add_header("Range", "bytes=%d-" % have)
            log("  resuming at %.2f GB" % (have / 1e9))
        started, last = time.time(), time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, \
                    open(part, "ab" if have else "wb") as fh:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    fh.write(block)
                    have += len(block)
                    if time.time() - last > 30:
                        rate = (have) / max(1e-9, time.time() - started) / 1e6
                        log("    %.2f / %.2f GB  (%.0f MB/s avg)"
                            % (have / 1e9, expect_size / 1e9, rate))
                        last = time.time()
        except Exception as exc:
            # Retry rather than abort: on a 40 GB batch a dropped connection is
            # expected at least once, and starting over is a worse answer.
            log("  transfer interrupted at %.2f GB (%s) - retrying in 10s"
                % (have / 1e9, exc))
            time.sleep(10)
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            continue
        have = os.path.getsize(part)
    return part


def fetch(repo, path, dest_dir, as_name=None, check_sha=False):
    name = as_name or os.path.basename(path)
    out = os.path.join(dest_dir, name)
    expect_size, oid = remote_meta(repo, path)
    log("%s/%s -> %s" % (repo, path, out))
    log("  expected %d bytes (%.2f GB)%s"
        % (expect_size, expect_size / 1e9, (" sha256=%s" % oid[:16]) if oid else ""))

    if os.path.isfile(out):
        actual = os.path.getsize(out)
        if actual == expect_size:
            log("  already present and size-verified (%d bytes) - skipping" % actual)
            return verify(out, expect_size, oid if check_sha else "")
        log("  present but WRONG SIZE (%d != %d) - re-downloading"
            % (actual, expect_size))
        os.remove(out)

    os.makedirs(dest_dir, exist_ok=True)
    part = download(repo, path, out, expect_size)

    actual = os.path.getsize(part)
    if actual != expect_size:
        log("FAIL: downloaded %d bytes but the repo says %d. The partial file "
            "is left at %s and deliberately NOT renamed into place, because a "
            "truncated model that looks finished is the failure this check "
            "exists to prevent." % (actual, expect_size, part))
        return 1
    os.replace(part, out)
    return verify(out, expect_size, oid if check_sha else "")


def verify(out, expect_size, oid):
    actual = os.path.getsize(out)
    if actual != expect_size:
        log("FAIL: %s is %d bytes, expected %d" % (out, actual, expect_size))
        return 1
    if oid:
        log("  hashing (full re-read)...")
        got = sha256_of(out)
        if got != oid:
            log("FAIL: sha256 %s != repo oid %s. Bytes arrived but they are "
                "not the right bytes." % (got, oid))
            return 1
        log("  sha256 OK %s" % got[:16])
    log("VERIFIED: %s (%d bytes)" % (out, actual))
    return 0


def self_test():
    """Prove the size check can say no. A verifier that has only ever passed
    is decoration."""
    import tempfile
    d = tempfile.mkdtemp(prefix="hf_fetch_canary_")
    good = os.path.join(d, "good.bin")
    with open(good, "wb") as fh:
        fh.write(b"x" * 1000)

    if verify(good, 1000, "") != 0:
        log("SELF-TEST FAILED: a correct file was rejected.")
        return 1
    log("canary 1: correct size accepted")

    # The real failure mode: the file exists, it is large, it is simply short.
    if verify(good, 5000, "") == 0:
        log("SELF-TEST FAILED: a TRUNCATED file passed verification. This "
            "script cannot detect the exact failure it was written for.")
        return 1
    log("canary 2: truncated file correctly REFUSED")

    real = sha256_of(good)
    if verify(good, 1000, "0" * 64) == 0:
        log("SELF-TEST FAILED: a wrong sha256 passed verification.")
        return 1
    log("canary 3: wrong sha256 correctly REFUSED (real=%s)" % real[:16])
    log("self-test: this check can fail, so a VERIFIED from it means something")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--path")
    ap.add_argument("--dest")
    ap.add_argument("--as", dest="as_name", default=None,
                    help="save under this filename instead of the basename "
                         "(the Lightning LoRAs are all called "
                         "high_noise_model.safetensors and would collide)")
    ap.add_argument("--sha256", action="store_true",
                    help="also hash the finished file against the LFS oid")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test()
    if not (ns.repo and ns.path and ns.dest):
        log("FAIL: --repo, --path and --dest are all required.")
        return 1
    return fetch(ns.repo, ns.path, ns.dest, ns.as_name, ns.sha256)


if __name__ == "__main__":
    sys.exit(main())
