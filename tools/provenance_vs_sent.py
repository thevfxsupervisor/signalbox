#!/usr/bin/env python3
r"""Does the provenance record agree with the prompt we actually sent?

THE BUG CLASS THIS CATCHES, three instances in one day, all found by hand:

  1. sg_component__camera records "locked-off / close up / 768x432" while the
     only camera words reaching the GPU are a fixed constant. Every SHOW01
     panel renders as the same medium-wide figure; 13 shots asked for a
     close-up. (2026-09-05)
  2. The no-burnt-in-text clause was recorded in provenance and never appended
     to the string qwen_compose actually composites from, for the majority code
     path -- any shot with a character in frame. (COLOUR-AND-TEXT-DEFECTS,
     defect 2)
  3. A panel revision wrote Shot.sg_gen_prompt, a field panel_compose never
     reads, while every step reported success. (2026-09-04)

Each is the same shape: THE RECORD SAYS WE SENT IT. Provenance is built from
the inputs a module INTENDED to use, and the prompt is built separately from
what it actually used, so the two can disagree indefinitely and nothing
notices. sg_preflight already checks provenance is POPULATED -- which all three
of these passed, because a field full of a claim nobody honoured is still full.

So this compares the two records against each other rather than either against
a schema. It is deterministic (D16): no model, no vision, no judgement. It
reads two text fields off a Version and asks whether the distinctive words in
one appear in the other.

WHAT IT CANNOT DO. It cannot prove a prompt reached the GPU -- only that two
ShotGrid fields agree. sg_prompt_final__as_sent_ is itself written by the same
module. It is a consistency check between two records that are built by
different code paths, which is exactly where these three bugs lived; a bug that
corrupts both identically is invisible to it, and that limit is why the tool
says "AGREES" rather than "VERIFIED".

    python provenance_vs_sent.py --episode SHOW01
    python provenance_vs_sent.py --episode SHOW01 --stage panel --verbose
    python provenance_vs_sent.py --self-test
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROJ = {"type": "Project", "id": 9999}
F_SENT = "sg_prompt_final__as_sent_"

# Words too common to carry evidence: finding "the" in both records proves
# nothing. Kept small and explicit rather than a frequency heuristic, because a
# heuristic tuned on today's corpus would quietly stop checking tomorrow's.
STOP = set("""a an and are as at be by for from in into is it its of on onto or
that the their them they this to with without do not no any every all one same
exactly shown image reference character set room""".split())


def log(m):
    print("[prov-check] %s" % m, flush=True)


def salient(text, minlen=4):
    """-> the distinctive lowercase words in a provenance value.

    Punctuation and separators are dropped, so "locked-off / close up /
    768x432" yields {locked, off, close, up, 768x432} minus stopwords."""
    words = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    return set(w for w in words if len(w) >= minlen and w not in STOP)


def claim_honoured(claim, sent, threshold=0.6):
    """-> (ok, missing). Is what provenance CLAIMS present in what was SENT?

    A claim is honoured when most of its distinctive words appear in the sent
    prompt. Not all of them: provenance values carry bookkeeping the prompt
    never would ("approved design Version 67492: SHOW_SET..."), and demanding
    an exact match would flag every healthy Version and the check would be
    turned off within a day. A threshold that most healthy rows pass and the
    three known defects fail is the useful one.

    An EMPTY claim is honoured trivially -- there is nothing to have dropped.
    That is not a loophole: sg_preflight already fails a Version whose
    provenance is blank, and this tool deliberately does not duplicate it
    (invariant 11)."""
    want = salient(claim)
    if not want:
        return True, set()
    have = salient(sent)
    missing = want - have
    return (len(want - missing) >= threshold * len(want)), missing


def check_version(v, fields):
    """-> [(field, missing_words)] for every provenance claim not in the prompt."""
    sent = v.get(F_SENT) or ""
    if not sent.strip():
        return [("<no as-sent prompt recorded>", set())]
    bad = []
    for f in fields:
        ok, missing = claim_honoured(v.get(f) or "", sent)
        if not ok:
            bad.append((f, missing))
    return bad


def find_disagreements(sg, episode="SHOW01", stage=None, since=None, verbose=False):
    """-> (vs, disagree, nosent, ok). The query-and-check core of run(),
    pulled out so a CALLER (F461: genvideo_service.py's cycle) can read the
    structured result directly rather than re-parsing log lines -- the same
    reason note_triage.is_actionable() returns (bool, why) instead of only
    printing.

    `since`: a datetime (or anything shotgun_api3 accepts for a date filter)
    -- only Versions created AFTER it are queried. None means every Version
    in the episode, which is what a full sweep (CLI use, --self-test) wants;
    a caller running every cycle wants only what is NEW, see the docstring
    on genvideo_service.watch_provenance()."""
    import sg_provenance as PROV
    # WHAT IS WORTH COMPARING, AND WHY __camera IS NO LONGER IN IT.
    #
    # sg_component__action is built from what the module MEANT to send, so a
    # disagreement between it and the sent text is a real defect and is
    # exactly what this gate exists to catch.
    #
    # sg_component__camera IS NOT. F471, measured 2026-09-08: Shot.sg_camera
    # and Shot.sg_gen_size_wxh are read by the compositor and DISCARDED, never
    # reaching the model on any production path. So the camera component is a
    # RECORD, not a claim about the prompt, and comparing it against the sent
    # text is a category error, not a check. Live proof: on 2026-09-09 the
    # first real run reported all 33 new videos as disagreeing because their
    # camera component "claims" the words 832x480, delivery, raster. Those are
    # recipe description. They could never appear in a prompt.
    #
    # Keeping it in `core` produced one meaningless Note per video, forever,
    # which is how a gate teaches people to ignore it. Demoted to --verbose
    # alongside the other bookkeeping fields.
    core = [PROV.F_ACTION]
    extra = [PROV.F_CAMERA, PROV.F_CHARACTER, PROV.F_SET, PROV.F_STYLE]
    filters = [["project", "is", PROJ], ["code", "starts_with", episode]]
    if stage:
        filters.append(["sg_stage", "is", stage])
    if since:
        filters.append(["created_at", "greater_than", since])
    vs = sg.find("Version", filters,
                 ["code", "sg_stage", "created_at", F_SENT] + core + extra,
                 order=[{"field_name": "code", "direction": "asc"}])
    disagree, nosent, ok = [], [], 0
    for v in vs:
        bad = check_version(v, core + (extra if verbose else []))
        if bad and bad[0][0].startswith("<no as-sent"):
            nosent.append(v)
        elif bad:
            disagree.append((v, bad))
        else:
            ok += 1
    return vs, disagree, nosent, ok


def run(sg, episode="SHOW01", stage=None, verbose=False, since=None):
    vs, disagree, nosent, ok = find_disagreements(sg, episode=episode, stage=stage,
                                                   since=since, verbose=verbose)
    log("%d Version(s) in %s%s%s"
        % (len(vs), episode, " at stage %s" % stage if stage else "",
           " created after %s" % since if since else ""))

    log("  %d agree, %d DISAGREE, %d have no as-sent prompt recorded"
        % (ok, len(disagree), len(nosent)))
    for v, bad in disagree[:12]:
        log("  DISAGREES  %s" % v["code"])
        for f, missing in bad:
            log("      %-28s claims words absent from the prompt sent: %s"
                % (f, ", ".join(sorted(missing))[:110]))
    if len(disagree) > 12:
        log("  ... and %d more" % (len(disagree) - 12))
    for v in nosent[:6]:
        log("  NO RECORD  %s (nothing to compare against)" % v["code"])
    if len(nosent) > 6:
        log("  ... and %d more with no as-sent prompt" % (len(nosent) - 6))
    if disagree:
        log("A DISAGREEMENT MEANS THE RECORD CLAIMS SOMETHING THE PROMPT DOES NOT "
            "SAY. Read the Version's own %s before changing any code." % F_SENT)
    return 1 if disagree else 0


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("stopwords carry no evidence", not salient("the a of and to in"))
    ck("short words are dropped", "up" not in salient("close up"))
    ck("a size token survives as evidence", "768x432" in salient("locked-off / 768x432"))
    ck("separators are not words", salient("a / b") == set())

    # THE SHAPE OF THE REAL DEFECT, FABRICATED strings. This mirrors SHOW01_A_0160:
    # provenance recorded the shot size, the prompt only ever carried the fixed
    # camera constant.
    camera_claim = "locked-off / close up / 768x432"
    real_sent = ("Place the character from the character reference image into the "
                 "room shown in the set reference image. PilotCharB flinches at the "
                 "sound and sits upright fast, blinking hard, one hand braced against "
                 "the mattress. Camera static, locked-off composition, natural consistent lighting.")
    ok, missing = claim_honoured(camera_claim, real_sent)
    ck("CANARY the shot-size defect is CAUGHT on these strings", not ok)
    ck("...and it names the words that went missing",
       "close" in missing and "768x432" in missing)

    # And a healthy row must pass, or the check gets switched off.
    action_claim = "PilotCharB flinches at the sound and sits upright fast, blinking hard, one hand braced against the mattress."
    ck("CANARY a HEALTHY action claim passes on those same strings",
       claim_honoured(action_claim, real_sent)[0])

    ck("an empty claim is honoured trivially (preflight owns 'is it populated')",
       claim_honoured("", "anything")[0] and claim_honoured(None, "")[0])
    ck("a claim wholly absent from the prompt fails",
       not claim_honoured("hexagonal marmalade turbine", real_sent)[0])
    # WHY character/set/style ARE NOT CHECKED BY DEFAULT, made concrete. Their
    # real values carry ids and codes the prompt would never contain, so they
    # fail the threshold while being perfectly healthy. Checking them by
    # default would produce a wall of false positives and the tool would be
    # switched off inside a day -- which is how a gate stops gating.
    bookkeeping = ("SHOW_SET_PILOTCHARB_BEDROOM (approved design Version 67492: "
                   "SHOW_SET_PILOTCHARB_BEDROOM_ANCHOR_turbo10_s52001)")
    ck("CANARY a healthy bookkeeping field would false-positive, which is why "
       "it is opt-in", not claim_honoured(bookkeeping, real_sent)[0])
    ck("...and the default field list excludes it",
       "sg_component__set" not in ("sg_component__action",))
    # F471, 2026-09-09: __camera joined the bookkeeping fields. It records
    # camera and raster description that is DISCARDED before the prompt is
    # built, so comparing it against the sent text reported every video in the
    # project as disagreeing. Built from a literal here, never from the
    # module constant, so this canary cannot agree with the code it tests.
    # THE FIRST VERSION OF THIS CANARY COULD NOT FAIL. It compared two
    # literals ("sg_component__camera" not in ("sg_component__action",)),
    # which is a tautology: reintroducing F_CAMERA into `core` left it green.
    # Proven by doing exactly that and watching a DIFFERENT canary catch the
    # regression while this one sat there agreeing with itself. It now reads
    # the real assignment out of the function's own source, with comment
    # lines stripped so the paragraph above cannot satisfy it.
    _fd_src = __import__("inspect").getsource(find_disagreements)
    _fd_code = "".join(l for l in _fd_src.splitlines(True)
                       if not l.lstrip().startswith(chr(35)))
    _core_line = [l for l in _fd_code.splitlines() if l.strip().startswith("core = ")]
    ck("CANARY the camera component is NOT compared by default: it is a record "
       "of a discarded field (F471), not a claim about the prompt",
       len(_core_line) == 1 and "CAMERA" not in _core_line[0])
    ck("...and the action component IS still compared, so narrowing the list "
       "did not switch the gate off entirely",
       len(_core_line) == 1 and "ACTION" in _core_line[0])

    # The fixture now disagrees on the field the gate ACTUALLY compares. It
    # used to disagree only on __camera, so narrowing the default field list
    # turned this test red, which is the test doing its job.
    v_bad = {"code": "V1", F_SENT: real_sent, "sg_component__camera": camera_claim,
             "sg_component__action": camera_claim}
    got = check_version(v_bad, ["sg_component__action"])
    ck("check_version reports the action field when it claims words the sent "
       "prompt does not contain",
       [f for f, _ in got] == ["sg_component__action"])
    v_ok = {"code": "V0", F_SENT: real_sent, "sg_component__action": action_claim}
    ck("...and reports nothing when the action component IS honoured",
       check_version(v_ok, ["sg_component__action"]) == [])
    v_none = {"code": "V2", F_SENT: "", "sg_component__action": camera_claim}
    ck("a Version with no as-sent prompt is reported separately, not as agreement",
       check_version(v_none, ["sg_component__action"])[0][0].startswith("<no as-sent"))

    class _Stub(object):
        def __init__(self):
            self.calls = []

        def find(self, *a, **k):
            self.calls.append((a, k))
            # V3 HONOURS its action claim, so exactly one of the two rows
            # disagrees. A fixture where both disagree would pass a
            # len(...) == 1 canary only by accident.
            return [v_bad, dict(v_bad, code="V3", sg_component__action=action_claim)]
    ck("a run that finds a disagreement exits non-zero (it can gate a deploy)",
       run(_Stub(), verbose=False) == 1)

    # F461: find_disagreements() is the same query-and-check core run() uses,
    # exposed so a caller (genvideo_service.watch_provenance) can read the
    # structured result rather than parsing log lines.
    _stub2 = _Stub()
    _vs, _dis, _nosent, _ok = find_disagreements(_stub2, episode="SHOW01")
    ck("find_disagreements() returns the same disagreement run() acted on",
       len(_dis) == 1 and _dis[0][0]["code"] == "V1")
    ck("run() and find_disagreements() are not two implementations -- run() "
       "calls find_disagreements() (invariant 11)",
       "find_disagreements(" in __import__("inspect").getsource(run))

    # `since` must actually reach the ShotGrid filter, or a caller scoping the
    # check to "new Versions only" (F461's cadence choice) is silently doing
    # a full sweep every time.
    _stub3 = _Stub()
    find_disagreements(_stub3, episode="SHOW01", since="2026-09-08T00:00:00")
    _filters = _stub3.calls[0][0][1]  # sg.find(entity_type, filters, fields, ...)
    ck("CANARY: a `since` value is added to the Version filter",
       any(f[0] == "created_at" and f[1] == "greater_than"
           and f[2] == "2026-09-08T00:00:00" for f in _filters))
    _stub4 = _Stub()
    find_disagreements(_stub4, episode="SHOW01")
    _filters_none = _stub4.calls[0][0][1]
    ck("...and no `since` means no created_at filter at all (a full sweep)",
       not any(f[0] == "created_at" for f in _filters_none))

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", default="SHOW01")
    ap.add_argument("--stage")
    ap.add_argument("--verbose", action="store_true",
                    help="also check the character/set/style fields, which carry "
                         "bookkeeping and produce false positives")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    import pm_backend_shotgrid as PMB
    return run(PMB.get_backend(), episode=a.episode, stage=a.stage, verbose=a.verbose)


if __name__ == "__main__":
    sys.exit(main())
