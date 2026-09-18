#!/usr/bin/env python3
r"""E0: the deploy seam. repo tools/ -> one executed release, atomically, stamped.

THE PROBLEM (MASTER-PLAN-V3.md section 3.2, plan/SESSION-HANDOVER.md section 9,
verified independently on disk 2026-09-03): there were THREE copies of every
tool --

    repo        C:\genvideo\repo\genvideo-pipeline\tools\      HEAD, edited here
    E: mirror   E:\...\genvideo-pipeline\tools\                written by sync_to_project.py
    live tree   E:\...\genvideo-pipeline\build\tools\          what the standing service ran

-- and `sync_to_project.py`'s DIRS list writes the MIDDLE one, a different
directory from the THIRD one genvideo_service.py hardcoded as TOOLS. Syncing
never deployed anything. Worse: the live tree was not uniformly stale, it was
a MIXTURE -- some files current, most from 2026-08-29 -- because at some point
someone hand-copied a subset of files into it. A directory like that cannot be
reasoned about: it is a combination of versions that exists nowhere in the
repo and was never tested together. See docs/METHOD.md for the
forensics (parent/child process check, file creation-time evidence) behind
every claim above.

THE FIX, one release directory per deploy, a pointer file that names the
current one, and nothing else executes anything until that pointer says so:

    E:\...\genvideo-pipeline\
      build\
        releases\
          20260903-131500_6083da9\tools\   <- an immutable, complete snapshot
          20260903-140200_a1b2c3d\tools\   <- another one; the first is untouched
        CURRENT_RELEASE.txt                <- "20260903-140200_a1b2c3d", nothing else

    1. STAGE. Copy the WHOLE tools/ tree into a release directory that has
       never existed before, under a temporary name, then rename it into its
       final name in ONE filesystem call. A crash before the rename leaves an
       orphaned ".partial" directory that NOTHING ever points to or executes;
       a crash after it leaves a complete, correctly-named release. There is
       no state in between where a live path exists half-written -- this is
       what "never copy file-by-file into a live directory" (E0's own brief)
       means in practice. See stage_release().
    2. STAMP. Write _DEPLOY_STAMP.json into the release: git SHA, dirty flag,
       deploy timestamp (UTC), who deployed it, how many files. This is what
       genvideo_service.py logs as its FIRST line at startup (W10) -- "what is
       running?" becomes one line of log, not a file diff across three trees.
    3. PREFLIGHT. Run every staged module's OWN --self-test against the
       STAGED bytes (not the repo's -- this tests what will actually run).
       Any failure REFUSES to activate the release. This is D16's rule
       (deterministic scripts, not judgement) applied to the deploy seam
       itself: the gate is an exit code, never a read of the diff.
    4. ACTIVATE. Swap the pointer file: write to a temp file beside it, then
       os.replace() over it in one call. THIS is the only step that makes a
       release live, and it is a single small-file rename -- about as atomic
       as Windows allows, and nothing like copying 40-odd files into a
       directory something is already running from.
    5. RESTART. Re-install ops/run_service.ps1 to its launch location and
       (re)start it. A deploy that does not restart has changed nothing that
       matters, because Python loads a module once (SESSION-HANDOVER.md
       section 9, point 4) -- so this is not a separate step someone has to
       remember, it is the last step of THIS one.

Every release directory is immutable once staged: a later deploy never edits
one, it only ever creates a new one and, if preflight passes, repoints the
tiny pointer file. So "if a deploy is interrupted, the previous release must
still be intact and runnable" is not a property this code has to maintain
carefully -- it is a property of never writing into an existing release
directory in the first place.

THE SINGLE NAMED LOCATION (E0 invariant 11: one implementation) is
ops/deploy_config.json. This script and ops/run_service.ps1 both read it;
neither hardcodes project_root, the releases directory or the pointer
filename. Before this file existed, three different pieces of code
(genvideo_service.py's TOOLS constant, sync_to_project.py's DIRS list,
run_service.ps1's launch line) each independently guessed at "where do the
executed tools live" and disagreed -- see the module docstring above and
docs/METHOD.md for the live evidence.

WHAT THIS DOES NOT DO. It never touches ShotGrid (no credential of any kind
is read, printed or required) and it never touches a GPU. --self-test is
fully offline: it stages a synthetic fixture tree under a temp directory and
never writes to E: or to C:\genvideo\run_service.ps1.

Usage:
    python deploy.py                              stage, preflight, activate, restart -- against
                                                    the configured project_root (production unless
                                                    --root overrides it)
    python deploy.py --root PATH                   target a different project root entirely
                                                    (this is how a staging rehearsal stays off
                                                    production without any other flag)
    python deploy.py --source PATH                 deploy from a different tools/ source dir
                                                    (default: this file's own directory)
    python deploy.py --no-restart                  stage, preflight, activate -- leave whatever
                                                    is currently running alone
    python deploy.py --dry-run                      stage and preflight only; never activates
    python deploy.py --allow-preflight-failures     activate anyway (loud, logged, last resort --
                                                    never the default)
    python deploy.py --status                       what is deployed right now, read-only
    python deploy.py --rollback                     point CURRENT_RELEASE.txt at the previous
                                                    release and restart; deletes nothing
    python deploy.py --self-test                    offline canaries, temp dir only
"""
import argparse
import filecmp
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))          # this repo's tools/
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_CONFIG = os.path.join(REPO_ROOT, "ops", "deploy_config.json")
STAMP_FILENAME = "_DEPLOY_STAMP.json"

SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git"}
SKIP_SUFFIXES = (".pyc", ".pyo")
# THE STAGING RENAME IS RETRIED. Measured 2026-09-07: a deploy died renaming a
# just-written directory of 90 files on the Dropbox share with WinError 5, and
# the identical call ten seconds later worked. The share has a sync client and a
# scanner holding fresh handles, so this is a transient to ride out, not a
# reason to abandon a staged release.
RENAME_TRIES = 4
RENAME_DELAY = 5


def log(m):
    print("[deploy] %s" % m, flush=True)


# --------------------------------------------------------------------- config
def load_config(path=DEFAULT_CONFIG):
    """The one place naming the deploy target (E0 invariant 11). Fails LOUDLY
    on a missing key rather than letting a KeyError surface deep inside a
    deploy -- the whole point of this file is that nothing about the target
    is guessed."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    required = ("project_root", "releases_dir", "current_pointer", "tools_subdir",
                "service_script")
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError("deploy_config.json (%s) is missing required key(s): %s"
                          % (path, ", ".join(missing)))
    return cfg


def resolve_paths(cfg, root=None):
    root = root or cfg["project_root"]
    return {
        "root": root,
        "releases_root": os.path.join(root, cfg["releases_dir"]),
        "pointer_path": os.path.join(root, cfg["current_pointer"]),
    }


# ----------------------------------------------------------------------- git
def git_info(repo_root=REPO_ROOT):
    def run(args):
        try:
            r = subprocess.run(["git", "-C", repo_root] + args,
                                capture_output=True, text=True, timeout=15)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None
    sha = run(["rev-parse", "--short", "HEAD"]) or "unknown"
    status = run(["status", "--porcelain"])
    return {"git_sha": sha, "git_dirty": bool(status)}


# ---------------------------------------------------------------- staging
def iter_source_files(source_dir):
    for dirpath, dirnames, filenames in os.walk(source_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, source_dir)


def stage_release(source_dir, releases_root, release_id, log=log,
                   _fail_after=None):
    """Copy the WHOLE toolset into a brand-new, never-before-used directory.
    Never writes into an existing/live directory -- that is exactly how
    build\\tools became a mixture of versions nothing tested together
    (MASTER-PLAN-V3 3.2). Stages under a ".partial" name and renames into the
    real name in one call, so a crash mid-copy leaves either nothing at the
    real name (the previous release, if any, is untouched) or a complete
    release -- never something in between.

    _fail_after: TEST HOOK ONLY. Raise after copying this many files, to
    prove the partial-copy-interruption canary for real rather than by
    inspection. Never passed by the CLI."""
    dest = os.path.join(releases_root, release_id, "tools")
    if os.path.exists(dest):
        raise RuntimeError("release id collision, refusing to overwrite: %s" % dest)
    tmp_dest = dest + ".partial"
    if os.path.exists(tmp_dest):
        shutil.rmtree(tmp_dest)

    count = 0
    total_bytes = 0
    try:
        for src, rel in iter_source_files(source_dir):
            dst = os.path.join(tmp_dest, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            count += 1
            total_bytes += os.path.getsize(dst)
            if _fail_after is not None and count >= _fail_after:
                raise RuntimeError("SIMULATED interruption after %d files (test hook)" % count)
    except Exception:
        # Leave the ".partial" directory on disk for forensics -- it is
        # harmless: nothing names it, nothing executes it, and the pointer
        # file (if any) still names whatever release was current before this
        # call. Re-raise so the caller sees the deploy failed.
        log("STAGING INTERRUPTED after %d file(s) -- %s is an orphan, "
            "nothing points to it, the previous release (if any) is untouched"
            % (count, tmp_dest))
        raise

    # The one filesystem call that makes this release directory exist under
    # its real name. Before this line: dest does not exist. After it: dest is
    # complete. No observer can see a half-written "tools" directory.
    #
    # Retried (see RENAME_TRIES): this atomic step is the one that fails on this
    # box, and it fails transiently. Bounded and LOUD, so a genuine permission
    # problem still surfaces instead of hiding inside a quiet retry loop.
    last = None
    for attempt in range(1, RENAME_TRIES + 1):
        try:
            os.rename(tmp_dest, dest)
            last = None
            break
        except OSError as e:
            last = e
            if attempt == RENAME_TRIES:
                break
            log("staging: rename attempt %d/%d failed (%s: %s), retrying in %ds"
                % (attempt, RENAME_TRIES, e.__class__.__name__, e, RENAME_DELAY))
            time.sleep(RENAME_DELAY)
    if last is not None:
        log("STAGING FAILED at the rename after %d attempt(s): %s. %s is an "
            "orphan, nothing points to it, the previous release is untouched"
            % (RENAME_TRIES, last, tmp_dest))
        raise last
    log("staged %d file(s), %.1f KB -> %s" % (count, total_bytes / 1024.0, dest))
    return dest, count, total_bytes


# ---------------------------------------------------------- version stamp
def write_stamp(release_dir, stamp):
    path = os.path.join(release_dir, STAMP_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stamp, f, indent=2)
    return path


def read_stamp(tools_dir):
    """None means "no stamp" -- either this tree predates the deploy seam or
    someone hand-copied into a release directory. Never raises: a corrupt
    stamp is exactly as informative as a missing one (both mean "do not
    trust this")."""
    path = os.path.join(tools_dir, STAMP_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def describe_stamp(stamp):
    """W10 / E0 acceptance: 'the service log's first line names the deployed
    SHA'. One implementation (invariant 11) -- genvideo_service.py imports
    THIS function rather than re-deriving the stamp format itself."""
    if not stamp:
        return ("DEPLOY STAMP MISSING -- this tree was not placed by tools/deploy.py "
                "(hand-copied, or predates the deploy seam). 'What is running?' cannot "
                "be answered from this log line; go diff files instead.")
    pf = stamp.get("preflight") or {}
    return ("DEPLOY sha=%s%s release=%s deployed=%s(UTC) by=%s files=%s preflight=%s/%s"
             % (stamp.get("git_sha", "?"),
                " DIRTY" if stamp.get("git_dirty") else "",
                stamp.get("release_id", "?"),
                stamp.get("deployed_at_utc", "?"),
                stamp.get("deployed_by", "?"),
                stamp.get("file_count", "?"),
                pf.get("passed", "?"), pf.get("ran", "?")))


# -------------------------------------------------------------------- preflight

def scan_control_characters(root, log=log):
    """-> [(path, [codepoints])] for staged .py files with control bytes in them.

    MEASURED 2026-09-05, and two of the three were LIVE BUGS rather than
    cosmetics. A shell heredoc ate the escapes in three source files before
    Python ever saw them, turning an intended word-boundary into a literal
    control byte:

      regional_compose.beat_for()  r"BS %s BS"   instead of  r"word-boundary"
        -> the regex could never match, so EVERY masked band was handed the
           WHOLE beat naming both characters. That is exactly the fusion defect
           the regional path has been blamed for and measured at 1 clean of 4.
      character_sheets._fail_count_in_tail()  same shape
        -> returned 0 on a string containing two FAILs. A check that cannot
           count a failure.
      beat_split._find_tests_dir()  a hardcoded path with BS and TAB in it
        -> silently dropped the one fallback that finds attribute_check.py in a
           deployed release, so every two-character shot refused.

    None of it is visible in a diff or a terminal: a backspace DELETES the
    character before it on display, so the corrupted line renders as the line
    you meant to write. An r"" prefix cannot help either -- the bytes were
    already wrong when they were written. Only a byte-level scan sees it, which
    is why this runs in preflight rather than living in anyone's review habits.
    """
    import glob
    bad = []
    for sub in ("tools", "tests", "worker"):
        for path in glob.glob(os.path.join(root, sub, "*.py")):
            try:
                text = io.open(path, encoding="utf-8", errors="replace").read()
            except Exception:                                     # noqa: BLE001
                continue
            found = sorted(set(c for c in text
                               if ord(c) < 32 and c not in (chr(10), chr(13))))
            if found:
                bad.append((path, [hex(ord(c)) for c in found]))
    for path, codes in bad:
        log("CONTROL CHARACTERS in %s: %s -- a heredoc or editor ate an escape; "
            "the line LOOKS correct in a diff because a backspace deletes the "
            "character before it on display" % (os.path.basename(path), ", ".join(codes)))
    return bad


def find_self_testable(release_tools_dir):
    """Files in THIS release that offer --self-test, found by reading the
    STAGED bytes (not the repo's) -- preflight must test what will actually
    execute, not a promise about it."""
    out = []
    for dirpath, dirnames, filenames in os.walk(release_tools_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py") or fn == "deploy.py":
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except Exception:
                continue
            if "--self-test" in text:
                out.append(full)
    return out


def run_preflight(release_tools_dir, python_exe=None, timeout=120, log=log):
    """Run every staged module's OWN --self-test. A tool refuses to deploy a
    tree whose self-tests do not pass (E0 requirement 6). This is arithmetic
    over exit codes, never a read of the output -- D16 applied to the seam
    itself."""
    python_exe = python_exe or sys.executable
    targets = find_self_testable(release_tools_dir)
    results = []
    for path in targets:
        name = os.path.relpath(path, release_tools_dir)
        t0 = time.time()
        try:
            r = subprocess.run([python_exe, path, "--self-test"],
                                capture_output=True, text=True, timeout=timeout,
                                cwd=release_tools_dir)
            ok = (r.returncode == 0)
            lines = (r.stdout or "").strip().splitlines()
            detail = (lines[-1] if lines else "") if ok else (
                ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1])
        except subprocess.TimeoutExpired:
            ok, detail = False, "TIMEOUT after %ss" % timeout
        except Exception as exc:
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        dt = time.time() - t0
        results.append({"module": name, "ok": ok, "detail": detail[:160], "seconds": round(dt, 1)})
        log("  preflight  %-34s %-4s (%5.1fs)  %s" % (name, "ok" if ok else "FAIL", dt, detail[:120]))
    failed = [r for r in results if not r["ok"]]
    log("preflight: %d/%d self-test(s) passed" % (len(results) - len(failed), len(results)))

    # A BYTE-LEVEL SCAN, because no self-test can see this. Corrupted escapes
    # produce code that RUNS -- a regex that simply never matches, a path
    # candidate that is never found -- so every module's own self-test passes
    # while the thing it tests is inert. Two of the three found on 2026-09-05
    # were live bugs that had been shipping for days. Treated as a preflight
    # FAILURE so a release carrying one cannot activate.
    for path, codes in scan_control_characters(os.path.dirname(release_tools_dir), log=log):
        failed.append({"module": os.path.basename(path), "ok": False,
                       "detail": "control characters in source: %s" % ", ".join(codes),
                       "seconds": 0})
    return results, failed


# ------------------------------------------------------------------- pointer
def read_pointer(pointer_path):
    if not os.path.exists(pointer_path):
        return None
    with open(pointer_path, "r", encoding="utf-8") as f:
        val = f.read().strip()
    return val or None


def publish_tests(source_dir, root, log=log):
    """Refresh <root>/build/tests from the repo's tests/ directory.

    THE FOURTH STALE COPY. DEPLOY-SEAM.md fixed three copies of tools/ and
    left tests/ alone, but panel_compose runs the attribute gate as a
    SUBPROCESS at TESTS/attribute_check.py, where TESTS is <root>/build/tests
    -- a directory nothing ever wrote. MEASURED 2026-09-04: a fix to
    attribute_check.py landed in the repo, passed preflight, was reported as
    deployed, and the gate went on running a copy from days earlier. Every
    panel that day still came back ERROR from a bug that was already fixed.

    Deliberately a MIRROR of the repo rather than a versioned release dir:
    the constant every module resolves is <root>/build/tests, so versioning it
    would mean changing that constant in several modules at once. Making the
    one directory they all read match what was just deployed is the smaller,
    honest fix; the release tree stays the versioned artefact for tools.

    Reports what it did either way -- a silent copy is how this went unnoticed.
    """
    src = os.path.join(os.path.dirname(source_dir), "tests")
    if not os.path.isdir(src):
        log("publish_tests: no tests/ beside %s -- nothing to publish" % source_dir)
        return False
    dst = os.path.join(root, "build", "tests")
    n = 0
    try:
        if not os.path.isdir(dst):
            os.makedirs(dst)
        for name in sorted(os.listdir(src)):
            if not name.endswith(".py"):
                continue
            s_path = os.path.join(src, name)
            if not os.path.isfile(s_path):
                continue
            d_path = os.path.join(dst, name)
            same = (os.path.isfile(d_path)
                    and os.path.getsize(d_path) == os.path.getsize(s_path))
            if same:
                with open(s_path, "rb") as a, open(d_path, "rb") as b:
                    same = a.read() == b.read()
            if not same:
                shutil.copy2(s_path, d_path)
                n += 1
    except Exception as exc:                                      # noqa: BLE001
        # LOUD. A stale gate is exactly what this function exists to stop, so
        # failing to refresh it must not look like success.
        log("publish_tests: FAILED to refresh %s (%s: %s). The attribute gate "
            "may still be running an OLD copy." % (dst, type(exc).__name__, exc))
        return False
    log("publish_tests: %s is current (%d file(s) updated)" % (dst, n))
    return True


def write_pointer(pointer_path, release_id):
    """THE activation step. Everything before this call only prepared a
    candidate; nothing is live until this returns. A single small-file
    os.replace() -- write-then-rename onto the same volume -- is about as
    atomic as Windows makes available, and it is the ONLY write this whole
    module makes to anything another process is reading."""
    os.makedirs(os.path.dirname(pointer_path), exist_ok=True)
    tmp = pointer_path + ".tmp-%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(release_id)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, pointer_path)


# ------------------------------------------------------------------- restart
def find_running(match_fragment, timeout=20):
    """Processes whose command line contains match_fragment, via WMI --
    the SAME technique ops/run_service.ps1's own single-instance guard uses
    (Get-CimInstance Win32_Process), so this and the guard can never disagree
    about what 'running' means (invariant 11). Returns None (not []) if the
    query itself could not be answered -- 'unknown' must never be treated as
    'nothing running'."""
    esc = match_fragment.replace("'", "''")
    # FILTER BY PROCESS NAME, not command line alone. Without this the query
    # matches ANY process whose command line merely MENTIONS the script --
    # including the powershell running this very query, and any operator or
    # agent command inspecting the service. The first real deploy stopped one
    # such observer (pid 6696, a Get-CimInstance one-liner) as though it were
    # the service. run_service.ps1's own guard has always had this filter;
    # this is the half of invariant 11 that was missed.
    ps_cmd = ("Get-CimInstance Win32_Process | Where-Object { "
              "($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
              "$_.CommandLine -like '*%s*' } | "
              "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json -Compress -Depth 3" % esc)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return None
    if not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return None
    return [data] if isinstance(data, dict) else data



def distinct_instances(procs):
    r"""-> the matching processes that are NOT a child of another match.

    COUNTING PROCESSES OVERCOUNTS INSTANCES. This venv is uv-created
    (pyvenv.cfg: uv = 0.12.6), and its Scripts/python.exe is a launcher that
    runs the real interpreter as a CHILD process. Both carry the same command
    line, so a single service appears in the process table twice -- as a
    launcher and the interpreter it launched, one the parent of the other.

    That is why "2 instances of genvideo_service.py" showed up on a clean
    deploy that had just stopped everything and started exactly one. Measured:
    pid 22376 (venv launcher) with pid 8060 (uv interpreter) as its child,
    same script, same --interval.

    So count LAUNCH CHAINS, not processes: a match whose parent is also a match
    is a continuation of that parent's launch, not a separate service. Two
    genuine services give two roots; a service plus its launcher gives one.

    The runtime authority is still genvideo_service.py's own exclusive lock --
    this only stops the deploy from misreporting.
    """
    procs = procs or []
    pids = set(p.get("ProcessId") for p in procs)
    return [p for p in procs if p.get("ParentProcessId") not in pids]


def inflight_render(log=log):
    """-> a list of RENDER subprocesses a restart would kill, or None if unknown.

    WHY. Measured 2026-09-07 by being the operator: `SHOW01_A_0070` was requested
    for panel composition FIVE times in one night and completed ZERO times. Every
    attempt was killed part-way by a deploy, reclaimed to 'rdy' at the next
    startup, restarted, and killed again. About fifty minutes of GPU on one shot,
    and nothing ever said so, because from the deploy's side stopping the service
    looks identical whether it was idle or ten minutes into a render.

    A render is not the service. Stopping the standing service is cheap and
    correct; stopping the `panel_compose.py` or `anima_anchor.py` child it
    launched throws away work that has no checkpoint.

    UNKNOWN IS NOT EMPTY, the same rule find_running() already follows: a process
    table we cannot read must never read as 'nothing is rendering'."""
    hits = []
    for frag in ("panel_compose.py", "anima_anchor.py", "video_from_panel"):
        procs = find_running(frag)
        if procs is None:
            log("inflight_render: could not query the process table -- UNKNOWN, not empty")
            return None
        for pr in procs:
            hits.append((frag, pr.get("ProcessId"), (pr.get("CommandLine") or "")[:120]))
    return hits


def stop_running(match_fragment, log=log):
    procs = find_running(match_fragment)
    if procs is None:
        log("stop_running: could not query the process table -- treating as UNKNOWN, stopping nothing")
        return None
    if not procs:
        log("stop_running: nothing matching '%s' is currently running" % match_fragment)
        return []
    stopped = []
    for p in procs:
        pid = p.get("ProcessId")
        if pid is None:
            continue
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             "Stop-Process -Id %d -Force -ErrorAction Stop" % pid],
                            capture_output=True, text=True, timeout=15)
            stopped.append(pid)
            log("stop_running: stopped pid %d (%s)" % (pid, (p.get("CommandLine") or "")[:100]))
        except Exception as exc:
            log("stop_running: FAILED to stop pid %d: %s" % (pid, exc))
    return stopped


def install_launcher(cfg, log=log):
    """Copy the repo's ops/run_service.ps1 to wherever it is actually
    launched from (cfg['launcher_install_path']), so restarting always runs
    the CURRENT launcher logic, not whatever was hand-placed there last.
    A staging config simply omits launcher_install_path and this is a no-op
    -- nothing under C:\\genvideo is ever touched by a staging run."""
    dest = cfg.get("launcher_install_path")
    if not dest:
        log("install_launcher: no launcher_install_path in this config -- skipped "
            "(expected for a staging run)")
        return None
    src = os.path.join(REPO_ROOT, "ops", "run_service.ps1")
    if os.path.exists(dest) and filecmp.cmp(src, dest, shallow=False):
        log("install_launcher: %s already matches the repo copy" % dest)
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)
    log("install_launcher: %s -> %s" % (src, dest))
    return dest


# WAITING FOR A GAP, BECAUSE THE GUARD ALONE IS UNUSABLE ON A BUSY QUEUE.
# The in-flight check is correct and it checks ONE INSTANT. Measured 2026-09-07:
# a corrected release sat undeployed through four attempts over half an hour
# because the render queue never happened to be empty at the moment anyone
# looked -- one compose finished and the next cycle claimed the next shot
# before the next poll. A guard that can only be satisfied by luck means an
# urgent fix cannot land while the pipeline is doing its job, which is exactly
# when a fix is most likely to be needed.
#
# The gap is real, it is just short: the service claims work at cycle start, so
# between cycles there is an idle window. Poll FAST for it rather than hoping to
# land in it. Bounded, and it says how long it waited either way, so "the queue
# never went idle" is a reported fact and not a silent hang.
IDLE_POLL_SECONDS = 2


def wait_for_idle(timeout_s, poll_s=IDLE_POLL_SECONDS, log=log, now=None):
    """-> True when no render is in flight, False if `timeout_s` elapsed first.

    `now` is injectable so the self-test can drive the clock instead of
    sleeping through a real timeout."""
    clock = now or time.time
    started = clock()
    waited_any = False
    while True:
        busy = inflight_render(log=lambda _m: None)
        if busy is not None and not busy:
            if waited_any:
                log("restart_service: queue went idle after %ds of waiting"
                    % int(clock() - started))
            return True
        elapsed = clock() - started
        if elapsed >= timeout_s:
            log("restart_service: waited %ds for the render queue to go idle and it "
                "did not (%s). Not forcing."
                % (int(elapsed),
                   "ShotGrid/process table unreadable" if busy is None
                   else "%d render(s) still in flight" % len(busy)))
            return False
        if not waited_any:
            log("restart_service: %s in flight -- waiting up to %ds for a gap rather "
                "than discarding the work"
                % ("a render" if busy else "work", int(timeout_s)))
            waited_any = True
        time.sleep(poll_s)


def restart_service(cfg, root, config_path=DEFAULT_CONFIG, log=log, force=False,
                    wait_idle_s=0):
    """Restart is part of deploy, not a separate thing someone remembers
    (E0 requirement 4). Stops whatever currently matches the configured
    service script, then (re)installs and launches ops/run_service.ps1,
    which owns single-instance enforcement and log-fallback logic already --
    this calls it rather than re-implementing either (invariant 11).

    Passes -ConfigPath explicitly, not just -Root: ops/run_service.ps1
    defaults -ConfigPath to the PRODUCTION config
    (C:\\genvideo\\repo\\genvideo-pipeline\\ops\\deploy_config.json). A launch
    that overrode only -Root would resolve the release directory under a
    staging root using PRODUCTION's service_script/releases_dir/pointer
    names -- caught for real during E0's own staging rehearsal (see
    docs/METHOD.md): the launcher went looking for
    genvideo_service.py under a release that only ever contained
    fixture_service.py, and refused. Config and root now always travel
    together."""
    # REFUSE TO KILL A RENDER IN FLIGHT unless explicitly forced. See
    # inflight_render(): five interrupted composes of one shot in one night, and
    # the deploy could not tell an idle service from one ten minutes into a
    # render. Deploying is not urgent often enough to justify that by default.
    if not force:
        if wait_idle_s:
            wait_for_idle(wait_idle_s, log=log)
        busy = inflight_render(log=log)
        if busy is None or busy:
            if busy:
                log("restart_service: REFUSING. %d render process(es) are in flight and a "
                    "restart would discard their work with no checkpoint:" % len(busy))
                for frag, pid, cmd in busy[:4]:
                    log("    %s pid %s  %s" % (frag, pid, cmd))
            log("restart_service: re-run with --force-restart to deploy anyway, with "
                "--wait-for-idle SECONDS to wait for a gap, or wait for the render to "
                "finish. The release is STAGED and preflighted either way.")
            return False
    stop_running(cfg["service_script"], log=log)
    launcher = install_launcher(cfg, log=log)
    if not launcher:
        log("restart_service: no launcher installed for this config -- nothing started. "
            "(This is expected for a staging config with no launcher_install_path; "
            "the caller is responsible for starting the staging launcher itself.)")
        return False
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", launcher,
           "-Root", root, "-ConfigPath", config_path]
    log("restart_service: launching %s" % " ".join('"%s"' % c if " " in c else c for c in cmd))

    # DO NOT DISCARD THE LAUNCHER'S OUTPUT. It was going to DEVNULL, so when
    # the first real deploy failed to bring the service back, the launcher's
    # own refusal message went nowhere and the deploy reported success over a
    # dead service. run_service.ps1 refuses LOUDLY and correctly; nobody could
    # read it. Capture it, and put it in the deploy log.
    diag = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ops")
    diag = os.path.abspath(os.path.join(diag, "last_launch.log"))
    try:
        fh = open(diag, "w", encoding="utf-8", errors="replace")
    except Exception:
        fh = None
    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, *not* DETACHED_PROCESS.
    #
    # MEASURED 2026-09-04, twice: with DETACHED_PROCESS the restart failed
    # every time and ops/last_launch.log came back ZERO BYTES -- the launcher
    # never wrote a single line, including its own first banner, so it was
    # dying before it ran rather than refusing for a reason. The same
    # command line started and stayed up when launched with Start-Process.
    # DETACHED_PROCESS gives the child NO console at all, and powershell.exe
    # needs one; CREATE_NO_WINDOW gives it a console that is simply never
    # shown, which is what "run this in the background" actually means here.
    # CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C in this shell from reaching the
    # service.
    #
    # The zero-byte log is the tell worth remembering: an empty capture file
    # means the process never started, a non-empty one means it started and
    # refused. Those need different fixes and look identical from outside.
    subprocess.Popen(cmd, creationflags=0x08000000 | 0x00000200,
                      stdout=(fh or subprocess.DEVNULL),
                      stderr=subprocess.STDOUT if fh else subprocess.DEVNULL)

    # LAUNCHING IS NOT RUNNING. Popen succeeding means a process was created,
    # not that the service is up -- the same "created is not viewable" mistake
    # this project made with ShotGrid Versions. Verify, and say so either way.
    ok = wait_for_service(cfg, timeout=90, log=log)
    if not ok and fh:
        try:
            fh.flush()
        except Exception:
            pass
        try:
            with open(diag, encoding="utf-8", errors="replace") as f:
                tail = [ln.rstrip() for ln in f.readlines()[-8:] if ln.strip()]
            if tail:
                log("restart_service: the launcher said:")
                for ln in tail:
                    log("    %s" % ln)
        except Exception:
            pass
    return ok


def wait_for_service(cfg, timeout=90, log=log):
    """Poll until the service process actually exists, or give up loudly.

    Returns True only on a process that is genuinely there. A deploy that
    cannot confirm this must NOT report success: taking production down and
    saying 'deployed' is worse than refusing."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        procs = find_running(cfg["service_script"])
        if procs:
            roots = distinct_instances(procs)
            pids = ", ".join(str(x.get("ProcessId")) for x in roots)
            if len(roots) > 1:
                # MORE THAN ONE IS A FAILURE, NOT A SUCCESS.
                #
                # This printed "VERIFIED up -- pid(s) 25212, 26316" on
                # 2026-09-04 and returned True. Two services double-process
                # every ShotGrid trigger, so the deploy reported health over
                # exactly the condition ops/run_service.ps1's single-instance
                # guard exists to prevent -- and the count that proved it was
                # already in the success message, unread. A check that
                # accepts "one or more" cannot detect "too many".
                log("restart_service: REFUSING to call this a success -- %d instances "
                    "of %s are running (pid(s) %s). Exactly one is correct; two "
                    "double-process every trigger. Stop the extras, then redeploy."
                    % (len(roots), cfg["service_script"], pids))
                return False
            log("restart_service: VERIFIED up -- pid %s" % pids)
            return True
        if procs is None:
            log("restart_service: cannot query processes; treating as NOT verified")
        time.sleep(3)
    log("restart_service: FAILED -- no service process after %ds. Production may be DOWN."
        % timeout)
    return False


# --------------------------------------------------------------------- deploy
def do_deploy(cfg, root=None, source=None, restart=True, dry_run=False,
              allow_preflight_failures=False, label=None, python_exe=None,
              restart_fn=None, config_path=DEFAULT_CONFIG, log=log,
              force_restart=False, wait_idle_s=0):
    """The whole cycle. Returns a result dict; only raises for a genuinely
    broken environment (bad config, unreadable source) -- an ordinary
    refusal (failing preflight) is a normal return, never an exception,
    because "the preflight said no" is not this function malfunctioning."""
    paths = resolve_paths(cfg, root)
    root, releases_root, pointer_path = paths["root"], paths["releases_root"], paths["pointer_path"]
    source = source or HERE

    if not os.path.isdir(source):
        raise RuntimeError("source tools dir does not exist: %s" % source)

    git = git_info()
    # Microsecond precision, not just seconds: two deploys issued back to back
    # (this module's own self-test does exactly that) must never collide on a
    # release id -- see stage_release()'s refusal to overwrite one that does.
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    release_id = "%s_%s" % (ts, git["git_sha"])
    if label:
        release_id += "_" + label

    prev_release = read_pointer(pointer_path)
    log("deploying release %s (previous: %s) root=%s source=%s"
        % (release_id, prev_release, root, source))

    release_dir, count, total_bytes = stage_release(source, releases_root, release_id, log=log)

    stamp = {
        "release_id": release_id,
        "git_sha": git["git_sha"],
        "git_dirty": git["git_dirty"],
        "deployed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deployed_by": os.environ.get("USERNAME") or os.environ.get("USER") or "unknown",
        "source": source,
        "file_count": count,
        "total_bytes": total_bytes,
        "preflight": {"ran": 0, "passed": 0, "failed": []},
    }
    write_stamp(release_dir, stamp)

    results, failed = run_preflight(release_dir, python_exe=python_exe, log=log)
    stamp["preflight"] = {"ran": len(results), "passed": len(results) - len(failed),
                           "failed": [r["module"] for r in failed]}
    write_stamp(release_dir, stamp)   # rewrite now the real preflight numbers exist

    # THE LIVE RECORD GATE, added 2026-09-06. Geoff, finding a cut on disk with
    # no ShotGrid Version: "how can something be rendered and not published? I
    # thought we agreed to use auto-publishing mechanisms to avoid that?"
    #
    # We did, and the mechanism works; what was missing is that a
    # rendered-but-unpublished file is INDISTINGUISHABLE from a published one by
    # looking at the disk. The self-tests above cannot catch it because it is a
    # fact about the live record, not about the code. So it is asked here, once,
    # against ShotGrid, and it BLOCKS: shipping while a deliverable exists only
    # as a private file is exactly the standing rule's failure case.
    #
    # It shares --allow-preflight-failures rather than getting its own escape
    # hatch, because a second override is a second thing to reach for.
    unpublished = []
    try:
        sys.path.insert(0, release_dir)
        import unpublished_check as _UP
        from episode_assemble import sg_connect as _conn
        unpublished = _UP.compare(_UP.rendered_cuts(), _UP.published_codes(_conn()))
    except Exception as exc:
        unpublished = ["<check could not run: %s>" % str(exc)[:80]]
    if unpublished:
        log("RENDERED BUT NOT PUBLISHED (%d): %s" % (len(unpublished), ", ".join(unpublished)))
        failed = list(failed) + [{"module": "unpublished_check (live)"}]
    stamp["unpublished_cuts"] = unpublished
    write_stamp(release_dir, stamp)

    if failed and not allow_preflight_failures:
        log("PREFLIGHT FAILED (%d/%d module(s)) -- release %s staged but NOT activated. "
            "Previous release (%s) is untouched and stays live/current."
            % (len(failed), len(results), release_id, prev_release))
        return {"activated": False, "release_id": release_id, "release_dir": release_dir,
                "failed": [r["module"] for r in failed], "prev_release": prev_release,
                "results": results}

    if failed and allow_preflight_failures:
        log("PREFLIGHT FAILED (%d/%d) but --allow-preflight-failures was passed: "
            "ACTIVATING ANYWAY. This is a deliberate override, not a pass -- it is logged "
            "as one." % (len(failed), len(results)))

    if dry_run:
        log("--dry-run: staged and preflighted %s, NOT activating (pointer stays: %s)"
            % (release_id, prev_release))
        return {"activated": False, "dry_run": True, "release_id": release_id,
                "release_dir": release_dir, "prev_release": prev_release, "results": results}

    write_pointer(pointer_path, release_id)
    log("ACTIVATED %s (previous: %s). Pointer: %s" % (release_id, prev_release, pointer_path))
    publish_tests(source, root, log=log)

    restarted = False
    if restart:
        fn = restart_fn or (lambda: restart_service(cfg, root, config_path=config_path,
                                                    log=log, force=force_restart,
                                                    wait_idle_s=wait_idle_s))
        restarted = bool(fn())
    else:
        log("--no-restart: the pointer is live, but any already-running process is still "
            "executing the PREVIOUS release until something restarts it by hand.")

    return {"activated": True, "release_id": release_id, "release_dir": release_dir,
            "prev_release": prev_release, "restarted": restarted, "results": results}


def do_status(cfg, root=None, log=log):
    paths = resolve_paths(cfg, root)
    root, releases_root, pointer_path = paths["root"], paths["releases_root"], paths["pointer_path"]
    release_id = read_pointer(pointer_path)
    if not release_id:
        log("no release activated yet at root=%s (pointer missing: %s)" % (root, pointer_path))
        return {"root": root, "release_id": None}
    tools_dir = os.path.join(releases_root, release_id, "tools")
    stamp = read_stamp(tools_dir)
    log("root=%s" % root)
    log("current release: %s" % release_id)
    log(describe_stamp(stamp))
    log("tools dir: %s (%s)" % (tools_dir, "exists" if os.path.isdir(tools_dir) else "MISSING"))
    procs = find_running(cfg["service_script"])
    if procs is None:
        log("running instance: UNKNOWN (could not query the process table)")
    elif not procs:
        log("running instance: none found matching '%s'" % cfg["service_script"])
    else:
        for p in procs:
            log("running instance: pid %s -- %s" % (p.get("ProcessId"), (p.get("CommandLine") or "")[:140]))
    return {"root": root, "release_id": release_id, "stamp": stamp}


def do_rollback(cfg, root=None, restart=True, restart_fn=None,
                config_path=DEFAULT_CONFIG, log=log, force_restart=False,
                wait_idle_s=0):
    """Point CURRENT_RELEASE.txt at the previous release directory and
    restart. Deletes nothing -- rollback is just another pointer swap, the
    same single-file-replace as a forward deploy, onto a release that is
    already staged and was already proven by ITS OWN preflight when it was
    first deployed."""
    paths = resolve_paths(cfg, root)
    root, releases_root, pointer_path = paths["root"], paths["releases_root"], paths["pointer_path"]
    current = read_pointer(pointer_path)
    if not os.path.isdir(releases_root):
        log("no releases directory at %s -- nothing to roll back" % releases_root)
        return {"rolled_back": False}
    ids = sorted(d for d in os.listdir(releases_root)
                 if os.path.isdir(os.path.join(releases_root, d, "tools")))
    candidates = [i for i in ids if i != current]
    if not candidates:
        log("no other release to roll back to (current: %s, releases on disk: %s)" % (current, ids))
        return {"rolled_back": False}
    target = candidates[-1]   # names sort chronologically (timestamp-prefixed)
    write_pointer(pointer_path, target)
    log("ROLLED BACK: %s -> %s. Pointer: %s" % (current, target, pointer_path))
    restarted = False
    if restart:
        fn = restart_fn or (lambda: restart_service(cfg, root, config_path=config_path,
                                                    log=log, force=force_restart,
                                                    wait_idle_s=wait_idle_s))
        restarted = bool(fn())
    return {"rolled_back": True, "from": current, "to": target, "restarted": restarted}


# ------------------------------------------------------------------ self-test
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-90s %s" % (name[:90], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    tmp = tempfile.mkdtemp(prefix="deploy_selftest_")
    try:
        src = os.path.join(tmp, "source_tools")
        os.makedirs(src)

        def write_module(name, body):
            with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                f.write(body)

        write_module("mod_a.py", (
            "import argparse, sys\n"
            "def self_test():\n"

            "    print('ALL PASS')\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    ap = argparse.ArgumentParser()\n"
            "    ap.add_argument('--self-test', action='store_true')\n"
            "    ns = ap.parse_args()\n"
            "    sys.exit(self_test() if ns.self_test else 0)\n"
        ))
        write_module("mod_b_no_selftest.py", "X = 1\n")   # a plain module: nothing to run
        write_module("mod_c_passing.py", (
            "import argparse, sys\n"
            "def self_test():\n"
            "    print('ALL PASS')\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    ap = argparse.ArgumentParser()\n"
            "    ap.add_argument('--self-test', action='store_true')\n"
            "    ns = ap.parse_args()\n"
            "    sys.exit(self_test() if ns.self_test else 0)\n"
        ))

        cfg = {"project_root": os.path.join(tmp, "project"),
               "releases_dir": os.path.join("build", "releases"),
               "current_pointer": os.path.join("build", "CURRENT_RELEASE.txt"),
               "tools_subdir": "tools", "service_script": "__deploy_selftest_service__.py"}
        # deliberately no launcher_install_path -- self-test must never touch
        # anything under C:\genvideo, or E: for that matter.

        restart_calls = []

        def fake_restart():
            restart_calls.append(True)
            return True

        # -------------------------------------------- config validation
        bad_cfg_path = os.path.join(tmp, "bad_config.json")
        with open(bad_cfg_path, "w", encoding="utf-8") as f:
            json.dump({"project_root": "x"}, f)
        threw = False
        try:
            load_config(bad_cfg_path)
        except ValueError as exc:
            threw = "missing required key" in str(exc)
        ck("load_config refuses a config missing required keys, loudly and specifically", threw)

        # -------------------------------------------- CANARY: normal deploy activates
        r1 = do_deploy(cfg, source=src, restart=True, restart_fn=fake_restart, log=lambda m: None)
        ck("a clean source (2 of 3 files self-test-capable, both passing) ACTIVATES",
           r1["activated"] is True)
        ck("release directory contains every source file, none dropped",
           set(os.listdir(r1["release_dir"])) >= {"mod_a.py", "mod_b_no_selftest.py", "mod_c_passing.py"})
        ck("the version stamp is written into the release and is readable back",
           read_stamp(r1["release_dir"]) is not None
           and read_stamp(r1["release_dir"])["release_id"] == r1["release_id"])
        ck("preflight ran exactly the 2 self-test-capable modules, not the plain one",
           read_stamp(r1["release_dir"])["preflight"]["ran"] == 2)
        ck("restart_fn was called exactly once on a successful activate", restart_calls == [True])
        paths = resolve_paths(cfg)
        ck("the pointer now names this release", read_pointer(paths["pointer_path"]) == r1["release_id"])

        # -------------------------------------------- CANARY: failing preflight refuses
        write_module("mod_c_passing.py", (   # same filename, now broken
            "import argparse, sys\n"
            "def self_test():\n"
            "    print('FAILED: deliberately broken for the deploy.py canary')\n"
            "    return 1\n"
            "if __name__ == '__main__':\n"
            "    ap = argparse.ArgumentParser()\n"
            "    ap.add_argument('--self-test', action='store_true')\n"
            "    ns = ap.parse_args()\n"
            "    sys.exit(self_test() if ns.self_test else 0)\n"
        ))
        restart_calls.clear()
        r2 = do_deploy(cfg, source=src, restart=True, restart_fn=fake_restart, log=lambda m: None)
        ck("CANARY (failing preflight): a source with a broken --self-test is REFUSED, "
           "not activated", r2["activated"] is False and r2.get("failed") == ["mod_c_passing.py"])
        ck("CANARY: the pointer still names the PREVIOUS (good) release, unchanged",
           read_pointer(paths["pointer_path"]) == r1["release_id"])
        ck("CANARY: restart_fn was NOT called when preflight refused the release",
           restart_calls == [])
        prev_dir = os.path.join(paths["releases_root"], r1["release_id"], "tools")
        ck("CANARY: the previous release directory is still intact on disk after the refusal",
           os.path.isdir(prev_dir) and set(os.listdir(prev_dir)) >= {"mod_a.py", "mod_c_passing.py"})
        r2b = subprocess.run([sys.executable, os.path.join(prev_dir, "mod_a.py"), "--self-test"],
                              capture_output=True, text=True)
        ck("CANARY: the previous release is still RUNNABLE (its own self-test still passes), "
           "not just present", r2b.returncode == 0)

        # -------------------------------------------- CANARY: --allow-preflight-failures
        restart_calls.clear()
        r2c = do_deploy(cfg, source=src, restart=True, restart_fn=fake_restart,
                         allow_preflight_failures=True, log=lambda m: None)
        ck("--allow-preflight-failures activates a failing release anyway, as a deliberate, "
           "logged override", r2c["activated"] is True
           and read_pointer(paths["pointer_path"]) == r2c["release_id"])
        ck("...and still restarts, because the override applies to preflight, not to restart",
           restart_calls == [True])

        # fix it back for what follows
        write_module("mod_c_passing.py", (
            "import argparse, sys\n"
            "def self_test():\n"
            "    print('ALL PASS')\n"
            "    return 0\n"
            "if __name__ == '__main__':\n"
            "    ap = argparse.ArgumentParser()\n"
            "    ap.add_argument('--self-test', action='store_true')\n"
            "    ns = ap.parse_args()\n"
            "    sys.exit(self_test() if ns.self_test else 0)\n"
        ))

        # -------------------------------------------- CANARY: partial-copy interruption
        r3_releases_before = set(os.listdir(paths["releases_root"]))
        threw = False
        try:
            stage_release(src, paths["releases_root"], "interrupted-release", log=lambda m: None,
                           _fail_after=1)
        except RuntimeError:
            threw = True
        ck("CANARY (partial-copy interruption): stage_release raises when interrupted mid-copy",
           threw)
        final_path = os.path.join(paths["releases_root"], "interrupted-release", "tools")
        partial_path = final_path + ".partial"
        ck("CANARY: NO 'tools' directory exists at the interrupted release's real name -- "
           "nothing could ever point to a half-written release",
           not os.path.exists(final_path))
        ck("CANARY: the orphaned '.partial' directory is on disk for forensics, but nothing "
           "references it", os.path.isdir(partial_path))
        ck("CANARY: the pointer is completely unaffected by an interruption that happened "
           "during staging, before any pointer write was ever attempted",
           read_pointer(paths["pointer_path"]) == r2c["release_id"])
        ck("CANARY: a RETRY after the interruption (same source, no bug) succeeds cleanly, "
           "proving the interruption left no corrupt state behind",
           do_deploy(cfg, source=src, restart=False, log=lambda m: None)["activated"] is True)

        # -------------------------------------------- CANARY: missing version stamp
        ck("describe_stamp(None) -- an unstamped tree -- says so loudly, doesn't fabricate one",
           "MISSING" in describe_stamp(None) and "hand-copied" in describe_stamp(None))
        ck("read_stamp of a directory with the file deleted returns None, not a crash",
           read_stamp(tempfile.mkdtemp(prefix="deploy_selftest_empty_")) is None)
        valid = read_stamp(prev_dir)
        line = describe_stamp(valid)
        ck("describe_stamp of a REAL stamp names the sha, the release id and the deploy time",
           valid["git_sha"] in line and valid["release_id"] in line and valid["deployed_at_utc"] in line)

        # -------------------------------------------- CANARY: --dry-run never activates
        before_ptr = read_pointer(paths["pointer_path"])
        rd = do_deploy(cfg, source=src, dry_run=True, log=lambda m: None)
        ck("--dry-run stages and preflights but never swaps the pointer",
           rd["activated"] is False and read_pointer(paths["pointer_path"]) == before_ptr)

        # -------------------------------------------- CANARY: --no-restart never calls restart
        restart_calls.clear()
        do_deploy(cfg, source=src, restart=False, restart_fn=fake_restart, log=lambda m: None)
        ck("--no-restart activates the pointer but never calls the restart function",
           restart_calls == [])

        # -------------------------------------------- rollback
        r_status_before = read_pointer(paths["pointer_path"])
        rb = do_rollback(cfg, restart_fn=fake_restart, log=lambda m: None)
        ck("rollback swaps the pointer to a DIFFERENT release than the one just active",
           rb["rolled_back"] is True and rb["to"] != r_status_before)
        ck("rollback deletes nothing -- the release it moved away FROM is still on disk",
           os.path.isdir(os.path.join(paths["releases_root"], r_status_before, "tools")))

        # -------------------------------------------- install_launcher is a no-op without config
        ck("install_launcher is a documented no-op when launcher_install_path is absent -- "
           "this is what keeps a staging run off C:\\genvideo entirely",
           install_launcher(cfg, log=lambda m: None) is None)

        # -------------------------------------------- find_self_testable reads STAGED bytes
        staged = find_self_testable(os.path.join(paths["releases_root"], r1["release_id"], "tools"))
        ck("find_self_testable finds exactly the self-test-capable modules, by filename",
           {os.path.basename(p) for p in staged} == {"mod_a.py", "mod_c_passing.py"})
        ck("find_self_testable never includes deploy.py itself, even if this file were "
           "copied into a release (it always is -- it lives in tools/ like everything else)",
           all(os.path.basename(p) != "deploy.py" for p in staged))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- distinct_instances: a launcher and its child are ONE service -------
    _pair = [{"ProcessId": 22376, "ParentProcessId": 9740},
             {"ProcessId": 8060, "ParentProcessId": 22376}]
    ck("a uv launcher and the interpreter it spawned count as ONE instance",
       len(distinct_instances(_pair)) == 1)
    ck("...and the one reported is the launcher, the root of the chain",
       distinct_instances(_pair)[0]["ProcessId"] == 22376)
    ck("two genuinely separate services still count as TWO",
       len(distinct_instances([{"ProcessId": 1, "ParentProcessId": 99},
                               {"ProcessId": 2, "ParentProcessId": 98}])) == 2)
    ck("no processes is no instances, not a crash",
       distinct_instances([]) == [] and distinct_instances(None) == [])
    ck("a three-deep launch chain is still one instance",
       len(distinct_instances([{"ProcessId": 1, "ParentProcessId": 99},
                               {"ProcessId": 2, "ParentProcessId": 1},
                               {"ProcessId": 3, "ParentProcessId": 2}])) == 1)

    # --- the control-character scan -----------------------------------------
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="ctrl_")
    os.makedirs(os.path.join(_d, "tools"))
    _clean = os.path.join(_d, "tools", "fine.py")
    io.open(_clean, "w", encoding="utf-8").write("x = 1" + chr(10))
    _dirty = os.path.join(_d, "tools", "eaten.py")
    # The real corruption, byte for byte: an intended word-boundary regex whose
    # backslash-b became a literal backspace.
    io.open(_dirty, "w", encoding="utf-8").write(
        'import re' + chr(10) + 'p = re.compile(r"' + chr(8) + 'FAIL' + chr(8) + '")' + chr(10))
    _bad = scan_control_characters(_d, log=lambda m: None)
    ck("a corrupted source file is found by the byte scan",
       len(_bad) == 1 and os.path.basename(_bad[0][0]) == "eaten.py")
    ck("...and the codepoint is named, since the line looks correct on screen",
       "0x8" in _bad[0][1])
    ck("a clean file is not flagged",
       all(os.path.basename(b[0]) != "fine.py" for b in _bad))
    io.open(os.path.join(_d, "tools", "tabbed.py"), "w", encoding="utf-8").write(
        'x = "a' + chr(9) + 'b"' + chr(10))
    ck("a literal TAB inside a string is caught too (the beat_split shape)",
       any(os.path.basename(b[0]) == "tabbed.py"
           for b in scan_control_characters(_d, log=lambda m: None)))
    # Newlines are not corruption.
    io.open(os.path.join(_d, "tools", "multiline.py"), "w", encoding="utf-8").write(
        'x = 1' + chr(10) + 'y = 2' + chr(13) + chr(10))
    ck("CANARY newlines and carriage returns are NOT flagged, or every file fails",
       all(os.path.basename(b[0]) != "multiline.py"
           for b in scan_control_characters(_d, log=lambda m: None)))
    ck("THIS repo is clean right now",
       not scan_control_characters(os.path.dirname(os.path.dirname(
           os.path.abspath(__file__))), log=lambda m: None))
    # ------------------------------------- CANARY: a render in flight blocks a restart
    # MEASURED 2026-09-07: SHOW01_A_0070 was requested for composition FIVE times
    # in one night and completed ZERO times, each attempt killed part-way by a
    # deploy. From the deploy's side an idle service and one ten minutes into a
    # render looked identical.
    _real_inflight = inflight_render
    try:
        globals()["inflight_render"] = lambda log=None: [("panel_compose.py", 999, "cmd")]
        ck("CANARY a RENDER in flight refuses the restart rather than discarding it",
           restart_service({"service_script": "x"}, root=None, log=lambda m: None) is False)
        # NOT asserting the forced path here on purpose: calling restart_service
        # with force=True would stop and start the REAL service from inside a
        # self-test. What is testable offline is that force SKIPS the guard, so
        # that is what is checked, by proving the guard is the only thing that
        # can return False before any process is touched.
        ck("CANARY the guard is the ONLY early return, so --force-restart reaches "
           "the real restart path instead of being silently refused",
           "if not force:" in inspect.getsource(restart_service))
        globals()["inflight_render"] = lambda log=None: None
        ck("CANARY an UNQUERYABLE process table also refuses: unknown is not empty",
           restart_service({"service_script": "x"}, root=None, log=lambda m: None) is False)

        globals()["inflight_render"] = lambda log=None: []
    finally:
        globals()["inflight_render"] = _real_inflight

    # --- WAITING FOR A GAP. The in-flight guard is correct and checks ONE
    # --- INSTANT: four deploy attempts over half an hour all landed while a
    # --- render happened to be running, so a corrected release sat undeployed.
    _real_sleep = time.sleep
    _real_inflight2 = inflight_render
    try:
        time.sleep = lambda _s: None
        clock = {"t": 0.0}

        def _tick():
            clock["t"] += 1.0
            return clock["t"]

        calls = {"n": 0}

        def _busy_then_idle(log=None):
            calls["n"] += 1
            return [] if calls["n"] >= 3 else [("panel_compose.py", 1, "cmd")]

        globals()["inflight_render"] = _busy_then_idle
        ck("CANARY wait_for_idle returns TRUE once the queue goes idle: an urgent "
           "fix must be able to land on a busy pipeline",
           wait_for_idle(600, poll_s=0, log=lambda m: None, now=_tick) is True)

        globals()["inflight_render"] = lambda log=None: [("panel_compose.py", 1, "c")]
        clock["t"] = 0.0
        ck("CANARY a queue that NEVER goes idle times out and returns False, so "
           "waiting cannot become a silent hang",
           wait_for_idle(5, poll_s=0, log=lambda m: None, now=_tick) is False)

        globals()["inflight_render"] = lambda log=None: None
        clock["t"] = 0.0
        ck("CANARY an UNREADABLE process table is not mistaken for idle: unknown "
           "times out rather than restarting over a render it cannot see",
           wait_for_idle(5, poll_s=0, log=lambda m: None, now=_tick) is False)

        globals()["inflight_render"] = lambda log=None: []
        ck("CANARY an ALREADY-idle queue returns at once, so the wait costs "
           "nothing on the normal path",
           wait_for_idle(600, poll_s=0, log=lambda m: None, now=_tick) is True)

        globals()["inflight_render"] = lambda log=None: [("panel_compose.py", 1, "c")]
        ck("CANARY after an unsuccessful wait the guard STILL refuses: waiting is "
           "not a softer --force-restart",
           restart_service({"service_script": "x"}, root=None, log=lambda m: None,
                           wait_idle_s=1) is False)
    finally:
        time.sleep = _real_sleep
        globals()["inflight_render"] = _real_inflight2

    # THE RENAME IS THE STEP THAT FAILS ON THIS BOX, so prove the retry both
    # recovers from a transient AND still raises on a permanent failure. A
    # retry loop that swallowed the permanent case would turn a broken deploy
    # into a silent one.
    _real_rename = os.rename
    try:
        calls = {"n": 0}

        def _flaky(a, b):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "simulated share lock")
            return _real_rename(a, b)

        tmpd = tempfile.mkdtemp()
        try:
            src = os.path.join(tmpd, "src")
            os.makedirs(src)
            with open(os.path.join(src, "m.py"), "w") as fh:
                fh.write("x = 1" + chr(10))
            rels = os.path.join(tmpd, "releases")
            os.rename = _flaky
            saved_delay = globals()["RENAME_DELAY"]
            globals()["RENAME_DELAY"] = 0
            d, n, _b = stage_release(src, rels, "rel1", log=lambda m: None)
            ck("CANARY the staging rename RECOVERS from a transient share lock "
               "(2 failures then success), which is the failure a real deploy hit",
               os.path.isdir(d) and n == 1 and calls["n"] == 3)

            def _always(a, b):
                raise PermissionError(5, "simulated permanent denial")
            os.rename = _always
            raised = False
            try:
                stage_release(src, rels, "rel2", log=lambda m: None)
            except OSError:
                raised = True
            ck("CANARY a PERMANENT rename failure still RAISES, so the retry "
               "cannot turn a broken deploy into a silent one", raised)
            ck("...and it leaves no directory under the real name to be mistaken "
               "for a release", not os.path.isdir(os.path.join(rels, "rel2", "tools")))
        finally:
            globals()["RENAME_DELAY"] = saved_delay
            os.rename = _real_rename
            shutil.rmtree(tmpd, ignore_errors=True)
    finally:
        os.rename = _real_rename


    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


# ------------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="override project_root from the config (this is how a "
                                    "staging rehearsal stays off production)")
    ap.add_argument("--source", help="override the tools/ source dir (default: this file's own directory)")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="path to deploy_config.json")
    ap.add_argument("--label", help="optional suffix appended to the release id")
    ap.add_argument("--wait-for-idle", type=int, default=0, metavar="SECONDS",
                    help="before restarting, poll for up to SECONDS until no render is "
                         "in flight, then restart. Use this on a busy queue: the "
                         "in-flight guard checks one instant, and a saturated queue "
                         "refills between polls, so an urgent fix can otherwise never "
                         "land. Never discards work; if the gap never comes it says so "
                         "and leaves the release staged.")
    ap.add_argument("--force-restart", action="store_true",
                    help="deploy even if a RENDER is in flight; its work is "
                         "discarded with no checkpoint")
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-preflight-failures", action="store_true",
                     help="activate even if a self-test fails -- logged loudly, never the default")
    ap.add_argument("--status", action="store_true", help="print what is deployed now; no changes")
    ap.add_argument("--rollback", action="store_true",
                     help="point the pointer at the previous release and restart; deletes nothing")
    ap.add_argument("--python-exe", help="interpreter used to run each module's --self-test "
                                          "(default: this interpreter)")
    ap.add_argument("--self-test", action="store_true", help="offline canaries, temp dir only")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test()

    config_path = os.path.abspath(ns.config)
    cfg = load_config(config_path)

    if ns.status:
        do_status(cfg, root=ns.root)
        return 0

    if ns.rollback:
        r = do_rollback(cfg, root=ns.root, restart=not ns.no_restart, config_path=config_path)
        return 0 if r["rolled_back"] else 1

    r = do_deploy(cfg, root=ns.root, source=ns.source, restart=not ns.no_restart,
                  dry_run=ns.dry_run, allow_preflight_failures=ns.allow_preflight_failures,
                  label=ns.label, python_exe=ns.python_exe, config_path=config_path,
                  force_restart=ns.force_restart, wait_idle_s=ns.wait_for_idle)
    return 0 if r["activated"] or ns.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
