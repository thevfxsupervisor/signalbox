#!/usr/bin/env python3
"""Single place a tool resolves which EPISODE (show) it is operating on.

SCOPE CORRECTION, Geoff, 2026-09-03: PILOT01/project 9999 is one ShotGrid
Project holding multiple Episodes -- the live show is a second EPISODE inside
the SAME project, not a second project. PROJECT_ID = 9999 stays a hardcoded
constant everywhere it already is; nothing in this module touches it, and
nothing should. What actually varies between shows is the episode: its
ShotGrid Episode code ("PILOT01"), the act/Sequence codes built from it
("PILOT01_A" / "PILOT01_B" / "PILOT01_C"), and the handful of derived paths
(subtitle SRT, the script markdown) that get built from that code. Those
were hardcoded as literal "PILOT01" strings and starts_with filters in at
least five tools (annotations.py, captions_sg.py, conform.py, finishing.py,
note_triage.py) plus animatic.py's episode stitch, character_sheets.py's
style anchor, episode_setup.py's episode bootstrap, and script_to_beats.py's
act->sequence map and beat-id prefix. Those are the real seams a second show
needs -- this module is the one place they all now resolve from.

RESOLUTION ORDER (first hit wins):
  1. an explicit code passed by the caller (a tool's own --sequence/--shot
     CLI value can imply one; most tools do not thread a separate --episode
     flag through argparse -- see WHY NO CLI FLAG below)
  2. the GENVIDEO_EPISODE environment variable
  3. build/config/episodes.json -- the registry a new show gets added to
  4. the built-in default: "PILOT01"

WHY NO CLI FLAG. captions_sg.py and conform.py already use `--episode` as a
boolean ("scope this run to the WHOLE episode" vs. one --sequence/--shot);
reusing that name for "WHICH episode" would collide, and inventing a second
flag name per tool is exactly the kind of per-file surface change this pass
is supposed to avoid ("do not change behaviour"). The env var does the same
job with less risk: it mirrors the SHOTGRID_* convention already used by
sg.ps1 (set once per session/process), and it is what a long-running or
subprocess-spawning tool (genvideo_service.py's children, for instance)
would inherit for free without any argparse changes at all. A CLI override
can be added per-tool later without touching this module.

WHY A REGISTRY FILE, NOT MORE ENV VARS. Onboarding a new show should be a
JSON edit -- add an entry to build/config/episodes.json -- not a grep-and-
replace across nine files. The registry is read fresh on every resolve()
call (it is tiny; no caching complexity) so an edit takes effect on the next
process without restarting anything already running.

WHY THE DEFAULT IS PILOT01. Every tool invoked exactly as before -- no env
var set, registry present or not -- must resolve to exactly the same
Episode it resolves to today. This refactor must not disturb the running
production: PILOT01 has 300+ Versions and a live standing service.

    python episode_context.py --self-test
"""
import io
import json
import os
import sys

ROOT = os.environ.get("GENVIDEO_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REGISTRY_PATH = os.path.join(ROOT, "build", "config", "episodes.json")
CAPTIONS_DIR = os.path.join(ROOT, "build", "out", "captions")

DEFAULT_CODE = "PILOT01"

# One-element list rather than a bool so resolve_episode() can append to it
# without a global statement, and so a test can clear it in place.
_WARNED_DEFAULT = []

# Built-in fallback. Used only if build/config/episodes.json is missing,
# unreadable, or lacks an entry for the resolved code -- so PILOT01 keeps
# working even if the registry file is deleted or corrupted. This is NOT a
# second way to resolve an episode (invariant 11): resolve_episode() is the
# only entry point, and every tool calls it, whichever source ultimately
# supplies the data.
_BUILTIN = {
    "PILOT01": {
        "acts": ["PILOT01_A", "PILOT01_B", "PILOT01_C"],
        "srt": "PILOT01.srt",
        # Resolved from THIS file, not a named clone: see the retired-clone note
        # in figure_count_qc.py. A fallback pointing at a dead tree is worse than
        # no fallback, because it looks like it works.
        "script": os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "creative", "PILOT-SCRIPT.md"),
    },
}


class Episode(object):
    """code: the ShotGrid Episode code, e.g. "PILOT01".
    acts: ordered list of Sequence codes, e.g. ["PILOT01_A", "PILOT01_B", "PILOT01_C"].
    srt_path: full path to this episode's subtitle sidecar.
    script: full path to this episode's script markdown, or None."""

    __slots__ = ("code", "acts", "srt", "script")

    def __init__(self, code, acts, srt, script):
        self.code = code
        self.acts = acts
        self.srt = srt
        self.script = script

    @property
    def srt_path(self):
        return os.path.join(CAPTIONS_DIR, self.srt)

    def __repr__(self):
        return "Episode(code=%r, acts=%r)" % (self.code, self.acts)


def beats_path(episode=None):
    """-> the beats.json path FOR THIS EPISODE.

    THE BUG THIS FIXES. `build/out/beats.json` was a single un-scoped path
    shared by every episode, hardcoded separately in `script_to_beats.py`
    and `genvideo_service.py`. Running the beat linker for a second episode
    would have OVERWRITTEN the first episode's beats -- PILOT01's 121-shot
    file, destroyed by a tool that looked like it was doing its job. Caught
    by inspection during SHOW01 setup, before it ran.

    Scoped name is `beats-<CODE>.json`, matching `beats-SHOW01.json` which
    was already written by hand during that setup.

    LEGACY. PILOT01's beats predate this and live in the un-scoped
    `beats.json`. `migrate_legacy_beats()` COPIES rather than moves, so a
    standing service still reading the old path cannot be broken mid-run.
    The old file is then inert, not authoritative."""
    ep = episode or resolve_episode()
    code = getattr(ep, "code", ep)
    return os.path.join(ROOT, "build", "out", "beats-%s.json" % code)


LEGACY_BEATS = os.path.join(ROOT, "build", "out", "beats.json")


def migrate_legacy_beats(episode=None, log=print):
    """Copy the un-scoped beats.json to this episode's scoped path, once.

    Returns the scoped path. Never overwrites an existing scoped file --
    the scoped file is authoritative the moment it exists."""
    dest = beats_path(episode)
    if os.path.exists(dest):
        return dest
    if not os.path.exists(LEGACY_BEATS):
        return dest
    import shutil
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(LEGACY_BEATS, dest)
    log("[episode] migrated legacy beats.json -> %s (original left in place, "
        "now inert)" % os.path.basename(dest))
    return dest


def _load_registry():
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("episodes") or {}
    except Exception:
        return {}


def known_episode_codes():
    """-> every episode code this project knows about, registry + built-ins.

    Exists so a STANDING SERVICE can sweep all episodes rather than inheriting
    resolve_episode()'s single default. That default (PILOT01 unless
    GENVIDEO_EPISODE is set) is right for a one-shot CLI tool, where the
    operator picks the episode, and wrong for a daemon, where nobody is there
    to pick and the wrong choice is silent: on 2026-09-04 note_triage's sweep
    was found ignoring all 55 SHOW01 shots for exactly this reason."""
    codes = set(_BUILTIN)
    try:
        codes.update(_load_registry())
    except Exception:                                          # noqa: BLE001
        # A missing or malformed registry must not stop a caller from
        # reaching the built-ins; _load_registry's own callers already treat
        # absence as empty.
        pass
    return sorted(codes)


def resolve_episode(explicit=None):
    """-> Episode, per the resolution order in the module docstring.

    Raises SystemExit (loud failure, matching this codebase's convention --
    see sg_publish.py's rationale) if the resolved code has no registry
    entry AND no built-in fallback: a silently-invented episode is worse
    than refusing."""
    code = explicit or os.environ.get("GENVIDEO_EPISODE") or DEFAULT_CODE
    # SAY SO WHEN NOBODY CHOSE. The built-in default is PILOT01 because this
    # module's job was to change no behaviour (see the docstring), and that was
    # right at the time. It has since aged badly: PILOT01 is the RETIRED episode
    # and SHOW01 is the live one, so a human running any episode-aware tool by
    # hand, without the env var, silently operates on the dead show. Measured
    # twice: note_triage's sweep ignored all 55 SHOW01 shots for a day, and on
    # 2026-09-07 a hand-run sweep reported "139 notes, 0 actionable" while the
    # service was reporting 2 actionable on SHOW01 in the same minute.
    #
    # The default is NOT changed here, deliberately: it is inherited by
    # everything and the day before a demo is the wrong day to move it. What is
    # cheap and sufficient is to stop it being SILENT. One line on stderr, only
    # on the fall-through, never when a caller or the env var chose.
    # ONCE PER PROCESS. The first version of this warning fired on every call,
    # and within minutes it had written itself twice into the standing service's
    # stderr from a MODULE-LEVEL `EP = resolve_episode()` that the service never
    # uses for its sweeps. That is a false alarm, and a check that cries wolf is
    # ignored inside a day, which is worse than no check: it manufactures the
    # feeling of coverage. Bounded to one line per process, it stays a signal.
    if (not explicit and not os.environ.get("GENVIDEO_EPISODE")
            and not _WARNED_DEFAULT):
        _WARNED_DEFAULT.append(code)
        sys.stderr.write(
            "[episode] no episode chosen, defaulting to %s. Set GENVIDEO_EPISODE "
            "to pick another (the live episode is SHOW01).%s" % (code, chr(10)))
    reg = _load_registry()
    entry = reg.get(code) or _BUILTIN.get(code)
    if entry is None:
        sys.exit("FATAL: unknown episode %r -- add it to %s" % (code, REGISTRY_PATH))
    acts = list(entry.get("acts") or [])
    if not acts:
        sys.exit("FATAL: episode %r has no acts in %s" % (code, REGISTRY_PATH))
    return Episode(code=code, acts=acts,
                   srt=entry.get("srt") or (code + ".srt"),
                   script=entry.get("script"))


# ------------------------------------------------------------------ self-test
def self_test():
    fails = []

    def ck(name, cond):
        print("  %-56s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ep = resolve_episode()
    ck("default resolves to PILOT01 with no env/CLI override", ep.code == "PILOT01")

    # THE WARNING IS ONLY WORTH ANYTHING IF IT FIRES, AND ONLY WHERE IT SHOULD.
    # Both directions are checked: a silent fall-through would put a human on
    # the dead episode without saying so, and a warning on an explicit choice
    # would train everyone to ignore it.
    import contextlib
    import io as _io

    def _warn_text(explicit=None, env=None):
        keep = os.environ.get("GENVIDEO_EPISODE")
        if env is None:
            os.environ.pop("GENVIDEO_EPISODE", None)
        else:
            os.environ["GENVIDEO_EPISODE"] = env
        buf = _io.StringIO()
        del _WARNED_DEFAULT[:]          # the warning is once per PROCESS, and
        try:                            # the self-test runs several in one
            with contextlib.redirect_stderr(buf):
                resolve_episode(explicit)
        finally:
            if keep is None:
                os.environ.pop("GENVIDEO_EPISODE", None)
            else:
                os.environ["GENVIDEO_EPISODE"] = keep
        return buf.getvalue()

    ck("CANARY: falling through to the built-in default WARNS on stderr",
       "[episode]" in _warn_text())
    ck("...and the warning NAMES the live episode, so the reader knows what "
       "to set rather than only that they did not set it",
       "SHOW01" in _warn_text())
    ck("an EXPLICIT code is a choice and warns about nothing",
       _warn_text(explicit="PILOT01") == "")
    ck("GENVIDEO_EPISODE is a choice too and warns about nothing",
       _warn_text(env="SHOW01") == "")

    # ONCE PER PROCESS, canaried, because the first version of this warning
    # wrote itself twice into the running service's stderr from an import that
    # never used the answer.
    del _WARNED_DEFAULT[:]
    buf2 = _io.StringIO()
    keep2 = os.environ.pop("GENVIDEO_EPISODE", None)
    try:
        with contextlib.redirect_stderr(buf2):
            resolve_episode()
            resolve_episode()
            resolve_episode()
    finally:
        if keep2 is not None:
            os.environ["GENVIDEO_EPISODE"] = keep2
    ck("CANARY: three fall-throughs in one process warn ONCE, not three times. "
       "A check that cries wolf is ignored inside a day",
       buf2.getvalue().count("[episode]") == 1)

    codes = known_episode_codes()
    ck("known_episode_codes includes the built-in default", "PILOT01" in codes)
    ck("known_episode_codes returns a sorted list, not a set",
       isinstance(codes, list) and codes == sorted(codes))
    # The point of the function: a standing service must see MORE than the
    # one episode resolve_episode() would pick for it.
    ck("known_episode_codes is a superset of the single resolved default",
       resolve_episode().code in codes)
    ck("default acts are the three real sequence codes",
       ep.acts == ["PILOT01_A", "PILOT01_B", "PILOT01_C"])
    ck("srt_path points at the existing PILOT01 subtitle sidecar",
       os.path.basename(ep.srt_path) == "PILOT01.srt")
    ck("explicit code wins over everything else",
       resolve_episode("PILOT01").code == "PILOT01")

    prior = os.environ.get("GENVIDEO_EPISODE")
    try:
        os.environ["GENVIDEO_EPISODE"] = "PILOT01"
        ck("env var is honoured", resolve_episode().code == "PILOT01")
    finally:
        if prior is None:
            os.environ.pop("GENVIDEO_EPISODE", None)
        else:
            os.environ["GENVIDEO_EPISODE"] = prior

    ck("an unknown episode refuses loudly rather than inventing one",
       _unknown_raises())
    ck("registry file, if present, is valid JSON with an 'episodes' object",
       _registry_is_sane())

    # --- beats_path: the canaries that would have caught the overwrite bug ---
    # An un-scoped beats.json meant running the beat linker for a second
    # episode would silently destroy the first episode's beats. These three
    # assert the property that was missing, not merely that a path is returned.
    pig = beats_path(resolve_episode("PILOT01"))
    evt = beats_path(resolve_episode("SHOW01")) if _has("SHOW01") else None
    ck("beats_path is episode-scoped, not a shared file",
       os.path.basename(pig) == "beats-PILOT01.json")
    ck("two episodes never share a beats path (the actual defect)",
       evt is None or pig != evt)
    ck("beats_path is not the legacy un-scoped file",
       os.path.abspath(pig) != os.path.abspath(LEGACY_BEATS))
    ck("migrate_legacy_beats never clobbers an existing scoped file",
       _migrate_is_non_destructive())

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0



def _has(code):
    """True if `code` resolves without exiting (registry or built-in)."""
    reg = _load_registry()
    return bool(reg.get(code) or _BUILTIN.get(code))


def _migrate_is_non_destructive():
    """A scoped file that already exists must be returned UNTOUCHED.

    Written as a real filesystem exercise rather than a shape check: the
    failure being guarded against is data loss, and a mock cannot lose data."""
    import tempfile, shutil as _sh
    global ROOT
    keep = ROOT
    tmp = tempfile.mkdtemp(prefix="epctx_canary_")
    try:
        ROOT = tmp
        out = os.path.join(tmp, "build", "out")
        os.makedirs(out)
        legacy = os.path.join(out, "beats.json")
        io.open(legacy, "w", encoding="utf-8").write('{"beats": "LEGACY"}')
        scoped = os.path.join(out, "beats-PILOT01.json")
        io.open(scoped, "w", encoding="utf-8").write('{"beats": "SCOPED"}')
        globals()["LEGACY_BEATS"] = legacy
        migrate_legacy_beats(resolve_episode("PILOT01"), log=lambda *a, **k: None)
        return io.open(scoped, encoding="utf-8").read() == '{"beats": "SCOPED"}'
    finally:
        ROOT = keep
        globals()["LEGACY_BEATS"] = os.path.join(ROOT, "build", "out", "beats.json")
        _sh.rmtree(tmp, ignore_errors=True)


def _unknown_raises():
    try:
        resolve_episode("NO_SUCH_SHOW_XYZ")
    except SystemExit:
        return True
    return False


def _registry_is_sane():
    if not os.path.exists(REGISTRY_PATH):
        return True   # absence is fine -- the built-in fallback covers it
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return isinstance(data.get("episodes"), dict)
    except Exception:
        return False


def main():
    if "--self-test" in sys.argv:
        return self_test()
    ep = resolve_episode()
    print(ep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
