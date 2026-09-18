#!/usr/bin/env python3
r"""Record and verify the ComfyUI environment this pipeline needs.

WHY. Everything this pipeline BUILDS is in this repo -- 60 tools, 15 workflow
graphs -- but what those graphs RUN ON was written down nowhere: which ComfyUI,
which custom node packs at which commit, which model weights. That lived only
in one operator's head, a memory file and a few docstrings. A second GPU seat,
a rebuilt box or a client handover would each have had to rediscover it.

Same class as the stale deploy copies found the same day: knowing that what is
installed is what you think is installed.

THE MANIFEST IS RECORDED FROM A WORKING BOX, NOT WRITTEN BY HAND. `--record`
reads the live install: node pack remotes and commits, model filenames and
sizes, the ComfyUI commit. `--check` compares a box against it and names every
difference. Neither ever installs anything -- a tool that silently fixed drift
would hide the drift.

WHAT IS DELIBERATELY NOT HERE. Model weights themselves (tens of GB, and some
are licence-restricted), and nothing client-specific: this file records tool
versions and filenames, not what is being made with them.

    python comfyui_env.py --record        write comfyui_env.json from this box
    python comfyui_env.py --check         compare this box against the manifest
    python comfyui_env.py --self-test
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST = os.path.join(REPO, "ops", "comfyui_env.json")

COMFY_ROOT = r"C:\ComfyUI_windows_portable\ComfyUI"
MODEL_DIRS = ("unet", "diffusion_models", "loras", "vae", "clip", "text_encoders")


def log(m):
    print("[comfyenv] %s" % m, flush=True)


def git_head(path):
    """-> (remote, short sha) for a checkout, or (None, None). Never raises:
    a pack installed by copy rather than clone is a fact to record, not a
    crash."""
    if not os.path.isdir(os.path.join(path, ".git")):
        return None, None
    try:
        remote = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=30)
        sha = subprocess.run(["git", "-C", path, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
    except Exception:                                             # noqa: BLE001
        return None, None
    r = (remote.stdout or "").strip() or None
    h = (sha.stdout or "").strip() or None
    return r, h


def scan(comfy_root=COMFY_ROOT):
    """-> the manifest dict for THIS box."""
    env = {"comfyui_root": comfy_root, "custom_nodes": {}, "models": {}}
    env["comfyui"] = dict(zip(("remote", "commit"), git_head(comfy_root)))

    cn = os.path.join(comfy_root, "custom_nodes")
    if os.path.isdir(cn):
        for name in sorted(os.listdir(cn)):
            d = os.path.join(cn, name)
            if not os.path.isdir(d) or name == "__pycache__":
                continue
            remote, sha = git_head(d)
            env["custom_nodes"][name] = {"remote": remote, "commit": sha}

    models_root = os.path.join(comfy_root, "models")
    for sub in MODEL_DIRS:
        d = os.path.join(models_root, sub)
        if not os.path.isdir(d):
            continue
        found = {}
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and not name.startswith("put_"):
                # Size, not a hash: hashing 9 GB per file on every check would
                # make the check something nobody runs.
                found[name] = os.path.getsize(p)
        if found:
            env["models"][sub] = found
    return env


def diff(expected, actual):
    """-> list of human-readable differences, most important first."""
    out = []
    for name, want in sorted((expected.get("custom_nodes") or {}).items()):
        have = (actual.get("custom_nodes") or {}).get(name)
        if have is None:
            out.append("MISSING node pack %s (%s)" % (name, want.get("remote")))
        elif want.get("commit") and have.get("commit") != want.get("commit"):
            out.append("node pack %s at %s, manifest says %s"
                       % (name, have.get("commit"), want.get("commit")))
    for name in sorted((actual.get("custom_nodes") or {})):
        if name not in (expected.get("custom_nodes") or {}):
            out.append("EXTRA node pack %s (not in the manifest)" % name)

    for sub, want in sorted((expected.get("models") or {}).items()):
        have = (actual.get("models") or {}).get(sub) or {}
        for fn, size in sorted(want.items()):
            if fn not in have:
                out.append("MISSING model %s/%s (%d bytes)" % (sub, fn, size))
            elif have[fn] != size:
                out.append("model %s/%s is %d bytes, manifest says %d"
                           % (sub, fn, have[fn], size))
    return out


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    base = {"custom_nodes": {"A": {"remote": "r", "commit": "aaa"}},
            "models": {"unet": {"m.gguf": 100}}}
    ck("an identical box reports no differences", diff(base, base) == [])
    ck("a missing node pack is reported",
       "MISSING node pack A" in " ".join(
           diff(base, {"custom_nodes": {}, "models": base["models"]})))
    ck("a node pack at the wrong commit is reported",
       "manifest says aaa" in " ".join(diff(base, {
           "custom_nodes": {"A": {"remote": "r", "commit": "bbb"}},
           "models": base["models"]})))
    ck("an EXTRA node pack is reported (it can change node behaviour too)",
       "EXTRA node pack B" in " ".join(diff(base, {
           "custom_nodes": {"A": {"remote": "r", "commit": "aaa"},
                            "B": {"remote": "r2", "commit": "ccc"}},
           "models": base["models"]})))
    ck("a missing model file is reported",
       "MISSING model unet/m.gguf" in " ".join(diff(base, {
           "custom_nodes": base["custom_nodes"], "models": {"unet": {}}})))
    ck("a model of the wrong SIZE is reported (a truncated download)",
       "manifest says 100" in " ".join(diff(base, {
           "custom_nodes": base["custom_nodes"],
           "models": {"unet": {"m.gguf": 55}}})))
    ck("a pack with no git checkout records None rather than raising",
       git_head(HERE if not os.path.isdir(os.path.join(HERE, ".git"))
                else r"C:\definitely\not\here") == (None, None))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--comfy-root", default=COMFY_ROOT)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()

    if a.record:
        env = scan(a.comfy_root)
        os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
        with open(MANIFEST, "w", encoding="utf-8") as fh:
            json.dump(env, fh, indent=2, sort_keys=True)
        log("recorded %d node pack(s), %d model dir(s) -> %s"
            % (len(env["custom_nodes"]), len(env["models"]), MANIFEST))
        return 0

    if not os.path.isfile(MANIFEST):
        raise SystemExit("no manifest at %s -- run --record on a working box "
                         "first" % MANIFEST)
    with open(MANIFEST, "r", encoding="utf-8") as fh:
        expected = json.load(fh)
    diffs = diff(expected, scan(a.comfy_root))
    if not diffs:
        log("this box matches the manifest")
        return 0
    log("%d difference(s) from the manifest:" % len(diffs))
    for d in diffs:
        log("  %s" % d)
    return 1


if __name__ == "__main__":
    sys.exit(main())
