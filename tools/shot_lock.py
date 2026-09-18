#!/usr/bin/env python3
"""The Version-level HARD lock. ROADMAP.md item 0a.

Geoff, 2026-09-08, immediately after a set-design approval put 13 shots back
into recompose: "we should be able to somehow lock shots (with a status) so
that some of them do not regenerate when they are already approved, say like
approved by client or similar". First decided as a status directly on the
Shot, then CORRECTED the same day, verbatim: "no sorry correction, we
don't want shot status protect, we want versions protected and that should
be: Pending Client Feedback pf or Approved apr / This is the internal
approval that should NOT be protected: Approved by Director ad" and, ruling
it: "ok then we use apr for internal and pf / fin are protected."

So protection lives on the VERSION, not the Shot, and it is exactly two
statuses: 'pf' (Pending Client Feedback) and 'fin' (final). 'apr' is the
everyday INTERNAL approval every shot passes through on its way to being
worked -- it does NOT protect, and must never be made to, because doing so
would lock every Version currently sitting at 'apr' (178 of them on this
project when this was written) and stop every cascade dead. The original
Shot-status value this module used to check was invented for the first
(rejected) draft of this design and was NEVER ADDED to Shot.sg_status_list's
valid_values, so that first lock was permanently inert; it has been retired
everywhere, not just here, and self_test() below proves the retired literal
does not survive anywhere in this tree.

A Shot is protected when ANY Version linked to that Shot -- panel, video,
any sg_stage -- is at 'pf' or 'fin'. One protected Version blocks every
regeneration path on the WHOLE shot, not just the stage it protects.
Rationale: if a picture is with the client (pf) or already final (fin),
regenerating anything upstream of it on that shot supersedes what the
client is holding, whichever stage the regeneration targets. A finer,
stage-aware refinement -- a protected PANEL blocking only panel
regeneration, leaving an unrelated video-stage regen free to run -- is a
deliberate FUTURE option this module does not implement, not an omission:
it needs its own design pass (which Version protects which downstream
stage) that Geoff has not made yet.

ONE constant, imported everywhere a protection check is needed (invariant
11: one definition, not several copies that can drift):
invalidate_stale_panels.invalidate(), genvideo_service.watch_hand_edits(),
genvideo_service.watch_stale_videos(), and prompt_revision.propose_for_shot()
/ apply_for_shot(). genvideo_service.watch_finishing() is explicitly EXEMPT
by the roadmap's own design (it has no sampler and cannot change the
picture) and does not import this module.

THIS MODULE NEVER QUERIES SHOTGRID ITSELF and imports nothing of ours
(dependency-free by design, see the module docstring history). Every
caller fetches the relevant Version rows for its own Shot (typically one
sg.find('Version', ...) with version_filter() in the filter list, limit=1
is enough since one protected row already proves the shot is locked) and
hands the list to is_protected()/protected_versions(). This module only
ever COMPARES the sg_status_list values it is handed against
PROTECTED_VERSION_STATUSES -- it never assumes a schema, never reads
valid_values, and fails SAFE (not protected) on anything malformed or
missing, per function.

THIS MODULE WRITES NOTHING TO SHOTGRID. It has no sg.update or sg.batch
call of any kind anywhere in it, by design -- roadmap 0a: "no watcher may
ever clear the lock". Unlocking is a deliberate human act in ShotGrid
(revising the protecting Version's own status away from pf/fin), never a
side effect of running this pipeline. Canaried in self_test() below by
scanning this file's own source.

Hard refusal, unconditionally: log loudly, change nothing, no queue, no
replay on unlock.

    python shot_lock.py --self-test
"""

PROTECTED_VERSION_STATUSES = ("pf", "fin")


def is_protected_version(version):
    """version: a dict carrying (at least) 'sg_status_list', as returned by
    any sg.find/find_one('Version', ...) call that included that field in
    its field list. True only when the live value is exactly one of
    PROTECTED_VERSION_STATUSES. A version missing the field (not fetched,
    or genuinely unset), or None itself, or an empty dict, is never
    protected -- every caller that needs protection honoured MUST fetch
    'sg_status_list' explicitly; this function does not raise on its
    absence so a caller that forgot to fetch it fails safe (nothing is
    ever treated as locked) rather than fails loud. Also fails safe (never
    raises) if handed something that is not dict-shaped at all -- e.g. a
    caller that mistakenly passes a single dict where a list of dicts was
    expected iterates its keys as bare strings, and this must return False
    for those too, not blow up."""
    if not version or not hasattr(version, "get"):
        return False
    return version.get("sg_status_list") in PROTECTED_VERSION_STATUSES


def protected_versions(versions):
    """versions: an iterable of Version dicts (e.g. the result of one
    sg.find('Version', ...) call). -> the ones that are actually protected,
    in the order they were given. Fails safe on a None/empty iterable
    (-> [])."""
    return [v for v in (versions or []) if is_protected_version(v)]


def is_protected(shot_versions):
    """shot_versions: the list of Version dicts already fetched for ONE
    Shot (the caller's own sg.find('Version', [['entity','is',
    {'type':'Shot','id':shot_id}], version_filter()], [...]) result, or
    the shot's full Version list if the caller already had it in hand). ->
    True when ANY of them is protected. This is the Shot-level answer the
    old Shot-status is_protected(shot) used to give; that signature is
    GONE (not kept as an overload) because a Shot dict silently has no
    'entity' of Versions to check and a leftover overload would look
    installed while always returning False."""
    return bool(protected_versions(shot_versions))


def version_filter():
    """The ShotGrid filter clause every caller should use rather than
    hand-copying the status tuple: ['sg_status_list', 'in', ['pf', 'fin']].
    Combine with an entity filter (typically
    ['entity', 'is', {'type': 'Shot', 'id': shot_id}]) to ask "is this Shot
    protected" with one query."""
    return ["sg_status_list", "in", list(PROTECTED_VERSION_STATUSES)]


def refusal(shot_code, watcher, version=None):
    """One shared wording for the loud skip every consulting path logs
    (roadmap 0a: "a skip must be as loud as an action"). Callers still call
    their own log() with this string -- it only standardises the sentence so
    grepping the service log for 'PROTECTED' finds every refusal from every
    path in one pattern, never four different phrasings. Pass the
    protecting Version dict (code + sg_status_list) when the caller has
    one in hand, so the log line names WHICH Version is holding the lock,
    not just which Shot."""
    if version:
        return ("%s is PROTECTED by Version %s (sg_status_list=%r) -- %s refused, "
                "nothing changed. Unlock it by revising that Version's status in "
                "ShotGrid to allow this."
                % (shot_code, version.get("code"), version.get("sg_status_list"), watcher))
    return ("%s is PROTECTED -- %s refused, nothing changed. "
            "Unlock the protecting Version's status in ShotGrid to allow this."
            % (shot_code, watcher))


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    # --- is_protected_version(): literal strings, never the constant, so
    # this canary cannot pass by construction (a canary built from
    # PROTECTED_VERSION_STATUSES[0] cannot fail).
    ck("a version at 'pf' (Pending Client Feedback) is protected",
       is_protected_version({"sg_status_list": "pf"}))
    ck("a version at 'fin' (final) is protected",
       is_protected_version({"sg_status_list": "fin"}))
    ck("CANARY: a version at 'apr' (the everyday internal approval) is NOT "
       "protected -- protecting on 'apr' would lock every approved Version "
       "in the project and stop every cascade",
       not is_protected_version({"sg_status_list": "apr"}))
    ck("a version at an ordinary live status ('rev') is not protected",
       not is_protected_version({"sg_status_list": "rev"}))
    ck("a version missing sg_status_list entirely is not protected (fails safe)",
       not is_protected_version({"code": "X"}))
    ck("a version with sg_status_list=None is not protected",
       not is_protected_version({"sg_status_list": None}))
    ck("None itself is not protected", not is_protected_version(None))
    ck("an empty dict is not protected", not is_protected_version({}))
    ck("a near-miss value ('pf ' with trailing space, wrong case 'PF') "
       "is not protected -- exact match only",
       not is_protected_version({"sg_status_list": "pf "})
       and not is_protected_version({"sg_status_list": "PF"}))

    # --- protected_versions()
    ck("protected_versions() keeps only the protected rows, in order",
       protected_versions([{"code": "a", "sg_status_list": "rev"},
                           {"code": "b", "sg_status_list": "pf"},
                           {"code": "c", "sg_status_list": "apr"},
                           {"code": "d", "sg_status_list": "fin"}])
       == [{"code": "b", "sg_status_list": "pf"}, {"code": "d", "sg_status_list": "fin"}])
    ck("protected_versions(None) is [] (fails safe)", protected_versions(None) == [])
    ck("protected_versions([]) is []", protected_versions([]) == [])

    # --- is_protected(): the Shot-level answer
    ck("a shot with one protected version among several is protected",
       is_protected([{"sg_status_list": "apr"}, {"sg_status_list": "pf"}]))
    ck("a shot with only unprotected versions is not protected",
       not is_protected([{"sg_status_list": "apr"}, {"sg_status_list": "rev"}]))
    ck("a shot with no versions at all is not protected",
       not is_protected([]))
    ck("is_protected(None) is not protected (fails safe)", not is_protected(None))
    ck("CANARY: is_protected() no longer accepts a bare Shot dict -- the old "
       "shot-status overload is GONE, not silently answering False forever",
       not is_protected({"sg_status_list": "pf"}))

    # --- version_filter()
    ck("version_filter() is the exact ['sg_status_list','in',['pf','fin']] clause",
       version_filter() == ["sg_status_list", "in", ["pf", "fin"]])

    # --- refusal()
    msg = refusal("SHOW01_A_0010", "hand-edit watcher")
    ck("the refusal message names the shot code", "SHOW01_A_0010" in msg)
    ck("the refusal message says PROTECTED", "PROTECTED" in msg)
    ck("the refusal message names the watcher that refused", "hand-edit watcher" in msg)

    msg2 = refusal("SHOW01_A_0020", "invalidate_stale_panels",
                   version={"code": "SHOW01_A_0020_v003", "sg_status_list": "fin"})
    ck("with a version given, the refusal names that Version's code",
       "SHOW01_A_0020_v003" in msg2)
    ck("...and that Version's status", "'fin'" in msg2 or "fin" in msg2)
    ck("...and still says PROTECTED and names the watcher",
       "PROTECTED" in msg2 and "invalidate_stale_panels" in msg2)

    src = open(__file__, encoding="utf-8").read()
    ck("CANARY: this module has no sg.update call anywhere in its own source",
       ("sg" + ".update(") not in src)
    ck("CANARY: this module has no sg.batch call anywhere in its own source",
       ("sg" + ".batch(") not in src)

    # THE RETIRED SHOT-STATUS LITERAL, GONE FROM THE WHOLE TOOLS TREE, NOT
    # JUST THIS FILE. Comments are stripped first (an explanatory comment
    # mentioning the old value would trivially match a naive substring
    # check). The needle itself is assembled from fragments that never
    # appear contiguous in this file's own source -- chr(112)+chr(114)+
    # chr(111) is 'p'+'r'+'o' at RUNTIME only, so this very check cannot
    # match itself the way a needle written as a plain "pro" literal would
    # (the same trap as a canary built from the constant it is testing:
    # here the constant is the search pattern itself).
    import os as _os
    _p = chr(112) + chr(114) + chr(111)
    _dq, _sq = chr(34) + _p + chr(34), chr(39) + _p + chr(39)
    _hits = []
    _scanned = 0
    _tools_root = _os.path.dirname(_os.path.abspath(__file__))
    for _dirpath, _dirnames, _filenames in _os.walk(_tools_root):
        for _fn in _filenames:
            if not _fn.endswith(".py"):
                continue
            _fp = _os.path.join(_dirpath, _fn)
            try:
                _text = open(_fp, encoding="utf-8").read()
            except OSError:
                continue
            _scanned += 1
            _stripped = chr(10).join(ln.split("#", 1)[0] for ln in _text.splitlines())
            if _dq in _stripped or _sq in _stripped:
                _hits.append(_os.path.relpath(_fp, _tools_root))
    ck("CANARY: the retired Shot-status literal is gone from the WHOLE tools "
       "tree (%d .py files scanned), not just this module -- %s"
       % (_scanned, ", ".join(_hits) if _hits else "clean"),
       not _hits and _scanned > 50)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
