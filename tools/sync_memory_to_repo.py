#!/usr/bin/env python3
r"""Mirror the agent memory directory into the repo, with a secret scan first.

WHY. The memory files are the densest record of what this pipeline has taught
us -- fifty of them, one fact each, most written the day the fact was measured.
They live under %USERPROFILE%\.claude\projects\...\memory, which is on the
local disk, in no repository, backed up by nothing. Geoff's standing rule is
that work returns to the project tree and git is the second backup; that rule
had never been applied to the memories themselves.

They are also the material for teaching a new operator what this system does
wrong, which is the reason he asked for them to be durable.

THE SCAN IS NOT OPTIONAL. This copies files into a git repository, and a
credential that reaches a repo is rotated, not deleted (two tokens already
reached agent transcripts on this box in one day). So every file is scanned
for anything that looks like a secret BEFORE anything is written, and a single
hit refuses the whole sync rather than copying the clean ones and leaving a
partial mirror that looks complete.

The scan is shape-based and deliberately noisy in the safe direction: it would
rather refuse a memory that merely quotes a variable NAME than let a value
through. A refusal prints the file and the line number, never the match.

    python sync_memory_to_repo.py --self-test
    python sync_memory_to_repo.py --dry-run
    python sync_memory_to_repo.py
"""
import argparse
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEST = os.path.join(REPO, "knowledge", "agent-memory")

SRC = os.path.join(
    os.path.expanduser("~"), ".claude", "projects",
    "C--example-genvideo-pipeline-users-claude",
    "memory")

# Shapes that mean "this is a secret VALUE", not "this names a secret".
# Each is a long opaque run or a known token prefix; none of them matches an
# ordinary English sentence, a path, or a variable name on its own.
SECRET_PATTERNS = [
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "an sk- API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "a GitHub token"),
    (r"\bxox[abps]-[A-Za-z0-9\-]{10,}", "a Slack token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "an AWS access key id"),
    (r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}", "a JWT"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "a private key block"),
    # A KEY=VALUE where the value is a long opaque run. Quoted or bare.
    (r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*"
     r"[\"']?[A-Za-z0-9_\-/+]{24,}", "a key/secret assignment"),
]


def log(m):
    print("[memsync] %s" % m, flush=True)


def scan_text(text):
    """-> [(line_no, description)]. Never returns the matched text itself:
    printing the hit to prove the scan works would print the secret."""
    hits = []
    for i, line in enumerate(text.splitlines(), 1):
        for CHARG, desc in SECRET_PATTERNS:
            if re.search(CHARG, line):
                hits.append((i, desc))
                break
    return hits


def scan_dir(src):
    """-> (files, problems). Reads every .md; a file that cannot be read is a
    problem, not a skip."""
    files, problems = [], []
    for name in sorted(os.listdir(src)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except Exception as exc:                                  # noqa: BLE001
            problems.append((name, 0, "unreadable: %s" % exc))
            continue
        for line_no, desc in scan_text(text):
            problems.append((name, line_no, desc))
        files.append((name, path, text))
    return files, problems


def sync(src=SRC, dest=DEST, dry=False):
    if not os.path.isdir(src):
        raise SystemExit("no memory directory at %s" % src)
    files, problems = scan_dir(src)
    if problems:
        log("REFUSING to sync: %d possible secret(s) found." % len(problems))
        for name, line_no, desc in problems:
            log("  %s:%s  %s" % (name, line_no or "?", desc))
        log("Nothing was copied. Remove or redact the value, then re-run.")
        return 1
    log("scanned %d memory file(s), no secret shapes found" % len(files))

    if not dry:
        if not os.path.isdir(dest):
            os.makedirs(dest)
    written = 0
    for name, path, text in files:
        target = os.path.join(dest, name)
        same = False
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as fh:
                same = fh.read() == text
        if same:
            continue
        written += 1
        if not dry:
            shutil.copy2(path, target)
    # A memory deleted upstream is a decision (a fact turned out to be wrong),
    # so the mirror follows it rather than keeping a copy nobody maintains.
    removed = 0
    if os.path.isdir(dest):
        keep = set(n for n, _p, _t in files)
        for name in sorted(os.listdir(dest)):
            if name.endswith(".md") and name not in keep:
                removed += 1
                if not dry:
                    os.remove(os.path.join(dest, name))
    log("%d file(s) to write, %d to remove%s"
        % (written, removed, " (dry run)" if dry else ""))
    return 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("an ordinary memory line is not a secret",
       not scan_text("The ShotGrid script key lives in SHOTGRID_SCRIPT_KEY."))
    ck("naming an env var is not a secret",
       not scan_text("check presence with [bool]$env:CIVITAI_TOKEN"))
    ck("a file path is not a secret",
       not scan_text(r"C:\genvideo\repo\genvideo-pipeline\tools\deploy.py"))
    ck("a git sha is not a secret", not scan_text("commit 07af36f, pushed"))

    # Canaries: the scan must actually fire. A scanner that never fires is
    # indistinguishable from a clean directory -- the exact failure this
    # project has hit before with QC gates.
    ck("an sk- key IS caught", bool(scan_text("key = sk-" + "A" * 24)))
    ck("a GitHub token IS caught", bool(scan_text("ghp_" + "b" * 30)))
    ck("an AWS key id IS caught", bool(scan_text("AKIA" + "C" * 16)))
    ck("a private key header IS caught",
       bool(scan_text("-----BEGIN RSA PRIVATE KEY-----")))
    ck("token = <long opaque value> IS caught",
       bool(scan_text("token = " + "d3f" * 12)))
    ck("a JWT IS caught",
       bool(scan_text("eyJ" + "e" * 24 + "." + "f" * 16)))
    # The property is that the VALUE never reaches the output. A description
    # naming the token FAMILY ("an sk- API key") is the point of it, so the
    # first version of this check -- which forbade the substring "sk-" --
    # asserted the wrong thing and failed against correct code.
    _secret = "A" * 24
    _hit = scan_text("x\nkey = sk-" + _secret)[0]
    ck("the scan reports the line number, not the value",
       _hit[0] == 2 and _secret not in _hit[1])

    import tempfile
    src = tempfile.mkdtemp(prefix="memsrc_")
    dst = tempfile.mkdtemp(prefix="memdst_")
    with open(os.path.join(src, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("a clean memory\n")
    with open(os.path.join(dst, "gone.md"), "w", encoding="utf-8") as fh:
        fh.write("deleted upstream\n")
    ck("a dry run writes nothing", sync(src, dst, dry=True) == 0
       and not os.path.isfile(os.path.join(dst, "a.md")))
    sync(src, dst, dry=False)
    ck("a real sync copies the clean file",
       os.path.isfile(os.path.join(dst, "a.md")))
    ck("a memory deleted upstream is removed from the mirror",
       not os.path.isfile(os.path.join(dst, "gone.md")))

    with open(os.path.join(src, "bad.md"), "w", encoding="utf-8") as fh:
        fh.write("token = " + "9a7" * 12 + "\n")
    before = sorted(os.listdir(dst))
    rc = sync(src, dst, dry=False)
    ck("ONE bad file refuses the WHOLE sync, leaving no partial mirror",
       rc == 1 and sorted(os.listdir(dst)) == before)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    return sync(dry=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
