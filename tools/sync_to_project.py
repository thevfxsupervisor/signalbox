#!/usr/bin/env python3
r"""Mirror the repo working tree onto the E: project folder.

WHY THIS EXISTS
    C: is a SCRATCH DISK and is not backed up. Geoff, 2026-09-03:
    "remember that C: is not being backed up, it's a scratch disk only!
     Any real work must be returned to E: project folder. very important!"

    The repo lives on C: for speed (E: is a synced network share, a Dropbox
    mount on the local NAS). Git push to GitHub is one backup; this is the other, and
    it is the one Geoff actually asked for.

SAFETY: THIS NEVER DELETES ANYTHING ON E:.
    A drift audit on 2026-09-03 found E: held real work the repo did NOT have
    (PRODUCTION-PROCESS.md, SG-PAGE-LAYOUTS.md, the script sources). A
    delete-to-match mirror would have destroyed them. So this is strictly
    additive: it copies repo -> E:, and leaves anything E:-only untouched.
    Reconciling the other direction is a human decision, so it is REPORTED,
    never performed.

    Files are only overwritten when the repo copy differs AND is newer. A
    newer file on E: is reported as a conflict and left alone -- losing an
    edit someone made on E: is exactly the failure this must not cause.
"""
import argparse, filecmp, os, shutil, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Mirrored because they hold authored work. 'out'/'output' deliberately absent:
# generated media is large, lives on E: already, and is not the repo's to own.
DIRS = ["plan", "tools", "docs", "tests", "workflows", "creative",
        "research", "ops", "worker"]
SKIP = {".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules"}

# REPO-ROOT DOCUMENTS. DIRS mirrors DIRECTORIES, so until 2026-09-05 nothing at
# the repo root reached E: at all. STATE.md, the one file this project treats as
# guaranteed-current, existed ONLY on the unbacked-up scratch disk, and the share
# is where Geoff actually browses. Add a root doc here or it is invisible to him.
ROOT_FILES = ["STATE.md", "README.md"]

# Claude's own durable state also lives on the scratch disk, and it is exactly
# the thing a fresh session needs. Geoff, 2026-09-03: "context is temporary,
# only E disk survives... other sessions might start fresh and need to know
# everything you know in order to continue."
#
# NO CREDENTIALS PASS THROUGH HERE. These are notes and rules only. E: is a
# Dropbox share, so anything mirrored to it replicates to every synced machine
# and into Dropbox's cloud -- sg.ps1 and civitai.ps1 stay on C: deliberately
# and are not in this list. Anything added here must be checked against that.
_CLAUDE = os.path.join(os.path.expanduser("~"), ".claude")
_PROJ_KEY = ("projects", "C--example-genvideo-pipeline-users-claude", "memory")
AGENT_BACKUP = "agent-config-backup"

# NAMED "agent-config-backup", NOT "claude-state". Geoff, 2026-09-06, looking at
# the project root: *"isn't this confusing? STATE.md and
# users/claude/SESSION-STATE-2026-08-26.md and claude-state/"*. It was: this
# holds the AGENT's config and memory, nothing to do with the project's state,
# and it sat next to STATE.md reading like a rival for it.
STATE = [
    (os.path.join(_CLAUDE, "CLAUDE.md"), os.path.join(AGENT_BACKUP, "CLAUDE.md")),
    (os.path.join(_CLAUDE, *_PROJ_KEY), os.path.join(AGENT_BACKUP, "memory")),
]


def sync_state(dest, dry_run=False, log=print):
    """Mirror CLAUDE.md and the memory directory to a durable backup location.

    Returns (copied, skipped). Same additive discipline as the main sync:
    never deletes, and a newer file on E: is reported rather than clobbered."""
    copied = skipped = 0
    conflicts = []
    for src, rel in STATE:
        if not os.path.exists(src):
            log("  state: MISSING on C: %s" % src)
            continue
        pairs = ([(src, os.path.join(dest, rel))] if os.path.isfile(src)
                 else [(f, os.path.join(dest, rel, r)) for f, r in walk(src)])
        for sfile, dfile in pairs:
            if os.path.exists(dfile):
                if filecmp.cmp(sfile, dfile, shallow=False):
                    skipped += 1
                    continue
                if os.path.getmtime(dfile) > os.path.getmtime(sfile):
                    conflicts.append(dfile)
                    continue
            if not dry_run:
                os.makedirs(os.path.dirname(dfile), exist_ok=True)
                shutil.copy2(sfile, dfile)
            copied += 1
    log("session state: %s %d file(s); %d already identical"
        % ("would copy" if dry_run else "copied", copied, skipped))
    for c in conflicts:
        log("  ! newer on E:, left alone: %s" % c)
    return copied, skipped


def walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fn in filenames:
            if fn.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, root)


def _refuse_if_obsolete():
    """RETIRED 2026-09-06. The working tree IS the project root now, so there is
    nothing to mirror: source and destination are the same directory.

    This does not just no-op, it REFUSES, because a mirror that silently copies
    a tree onto itself is indistinguishable from a mirror that is working, and
    this tool's whole purpose was to stop two copies drifting. One copy is the
    fix; running this again would only ever re-create the problem."""
    if os.path.normcase(os.path.abspath(REPO)) == os.path.normcase(os.path.abspath(DEST)):
        print("REFUSING: the working tree IS the project root since 2026-09-06, so")
        print("there is nothing to mirror. See STATE.md, 'Where the code lives'.")
        print("  repo: %s" % REPO)
        print("  dest: %s" % DEST)
        return True
    return False


def main():
    if _refuse_if_obsolete():
        return 3

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", default=DEST)
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 if E: has diverged (a conflict or an E:-only file). "
                         "A divergence is invisible otherwise: this tool prints it and "
                         "then exits 0, so a scheduled run cannot alarm on it, and the "
                         "two copies stay forked forever while both look healthy.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, copy nothing")
    a = ap.parse_args()

    if not os.path.isdir(a.dest):
        print("FAIL: destination does not exist: %s" % a.dest)
        print("  E: is a mapped network share. If it is disconnected, this is")
        print("  NOT a reason to skip the backup -- reconnect it and re-run.")
        return 2

    copied = skipped = 0
    conflicts, created = [], []

    def _pairs():
        for name in ROOT_FILES:
            src = os.path.join(REPO, name)
            if os.path.isfile(src):
                yield src, os.path.join(a.dest, name), name
        for d in DIRS:
            src_root = os.path.join(REPO, d)
            if not os.path.isdir(src_root):
                continue
            for src, rel in walk(src_root):
                yield src, os.path.join(a.dest, d, rel), os.path.join(d, rel)

    for src, dst, label in _pairs():
        if os.path.exists(dst):
            if filecmp.cmp(src, dst, shallow=False):
                skipped += 1
                continue
            # Differs. Only overwrite if the repo copy is NEWER.
            if os.path.getmtime(dst) > os.path.getmtime(src):
                conflicts.append(label)
                continue
        else:
            created.append(label)
        if not a.dry_run:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(src, dst)
        copied += 1

    verb = "would copy" if a.dry_run else "copied"
    print("%s %d file(s); %d already identical" % (verb, copied, skipped))
    if created:
        print("\nnew on E: (%d):" % len(created))
        for f in created[:20]:
            print("  + %s" % f)
        if len(created) > 20:
            print("  ... and %d more" % (len(created) - 20))
    if conflicts:
        print("\nCONFLICT -- newer on E: than in the repo, LEFT UNTOUCHED (%d):"
              % len(conflicts))
        for f in conflicts:
            print("  ! %s" % f)
        print("  These were edited on E: after the repo copy. Reconcile by hand;")
        print("  this tool will not choose for you.")

    # Report E:-only files under mirrored dirs. Not deleted, not copied back --
    # just made visible, because silent divergence is how work gets lost.
    orphans = []
    for d in DIRS:
        dst_root = os.path.join(a.dest, d)
        if not os.path.isdir(dst_root):
            continue
        for _, rel in walk(dst_root):
            if not os.path.exists(os.path.join(REPO, d, rel)):
                orphans.append(os.path.join(d, rel))
    if orphans:
        print("\nON E: BUT NOT IN THE REPO (%d) -- not git-backed:" % len(orphans))
        for f in orphans[:20]:
            print("  ? %s" % f)
        if len(orphans) > 20:
            print("  ... and %d more" % (len(orphans) - 20))
        print("  If any is real work, import it into the repo so git protects it too.")

    print()
    sync_state(a.dest, dry_run=a.dry_run)

    # DIVERGENCE MUST BE ABLE TO FAIL SOMETHING. E: is a deploy destination with
    # no .git, and the mtime guard means a file edited there is left alone
    # FOREVER: the copies fork silently and both look healthy. Printing it is
    # not enough, because nobody reads a successful run's scrollback.
    if a.strict and (conflicts or orphans):
        print("STRICT: E: has diverged (%d conflict(s), %d file(s) only on E:)."
              % (len(conflicts), len(orphans)))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
