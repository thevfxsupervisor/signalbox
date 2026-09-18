#!/usr/bin/env python3
r"""The one place that composes a Version's description. Short, and deterministic.

GEOFF, 2026-09-04: "your version descriptions are too verbose and long. If the
information could be or already is in another field then that's enough. The
descriptions ought to be a quick reminder for the reviewer what they are
reviewing, what the context is. Importantly the descriptions need to be 100%
deterministic."

He is right on both counts. The median SHOW01 description was 2127 characters
and the longest 4743 -- multi-paragraph write-ups of method, rationale and
verdict, in a field a reviewer reads while deciding. Meanwhile the seed, the
model, the exact prompt as sent, and the component provenance all have their
own fields already, so most of that text was a second copy of data that was
one click away.

TWO RULES, AND THE SECOND IS THE STRICT ONE.

SHORT: one line of what this is, one line of context. If a fact has a field,
it is not repeated here.

DETERMINISTIC: the same inputs produce a byte-identical string, every time.
No model-written prose, no adjectives, no judgement, no timestamps, no
measured durations, no counts that drift. A description that varies run to run
cannot be diffed, cannot be trusted as a label, and quietly becomes the place
where an opinion hides. Verdicts appear only as the gate's own token
(PASS/FAIL/ERROR/SKIP) plus which component produced it -- both deterministic
outputs of a deterministic check.

WHERE THE LONG TEXT GOES INSTEAD. Findings, rationale and full QC output are
worth keeping and do not belong in a label: QC detail goes to the Version's own
detail field, and anything a human should read and reply to goes in a Note,
which is threaded and reviewable. Neither is lost; both stop shouting at the
reviewer.

    python version_description.py --self-test
"""
import argparse
import sys

MAX_CHARS = 400          # a label, not a page. Refused above this, not truncated.


def _clean(s):
    """Collapse whitespace. A description that differs only in line breaks is
    still a different string, and determinism is the whole point."""
    return " ".join(str(s or "").split())


def panel(shot_code, version_num, characters=(), set_code=None, sequence=None,
          qc_status=None, qc_failed=(), stale_of=None, seed=None,
          group_of=None):
    """-> the description for a panel candidate.

    characters/qc_failed are sorted before use so the caller's ordering cannot
    change the output. Nothing here is read from a model."""
    chars = ", ".join(sorted(c for c in characters if c)) or "none"
    bits = ["Panel candidate v%03d for %s." % (int(version_num), shot_code)]
    ctx = ["Characters: %s" % chars]
    if set_code:
        ctx.append("Set: %s" % set_code)
    if sequence:
        ctx.append("Sequence: %s" % sequence)
    bits.append(" ".join(x + "." for x in ctx))
    if qc_status:
        failed = ", ".join(sorted(c for c in qc_failed if c))
        bits.append("Attribute QC: %s%s."
                    % (qc_status, " (%s)" % failed if failed else ""))
    if seed is not None:
        bits.append("Seed %s." % seed)
    if group_of and group_of > 1:
        # A reviewer opening one cell of a wedge needs to know there are
        # siblings, or they will judge it as the only option.
        bits.append("One of %d candidates from the same run." % int(group_of))
    if stale_of:
        bits.append("Supersedes %s." % stale_of)
    return _clean(" ".join(bits))


def design(asset_code, kind, seed=None, source_version=None, instruction=None):
    """-> the description for an asset design candidate.

    `kind` is 'render' or 'edit'. An edit names what it was edited FROM,
    because that is the one thing a reviewer cannot see in the image."""
    bits = ["%s design candidate for %s."
            % ("Edited" if kind == "edit" else "Rendered", asset_code)]
    if source_version:
        bits.append("Edited from %s." % source_version)
    if instruction:
        # The instruction is operator-written and already deterministic; it is
        # the one thing that says what this candidate was FOR. Capped so a
        # long note cannot turn the label back into a page.
        bits.append("Change requested: %s." % _clean(instruction)[:160].rstrip("."))
    if seed is not None:
        bits.append("Seed %s." % seed)
    return _clean(" ".join(bits))


def wedge(shot_code, family, seed, characters=(), variable=None):
    """-> the description for an experiment cell.

    A wedge cell's job is to be comparable with its siblings, so the label
    names the family and the ONE variable that differs. The finding goes in a
    Note or a report, never here: a finding is a judgement and judgements are
    not deterministic."""
    chars = ", ".join(sorted(c for c in characters if c)) or "none"
    bits = ["Wedge cell: %s, seed %s, on %s." % (family, seed, shot_code),
            "Characters: %s." % chars]
    if variable:
        bits.append("Variable: %s." % _clean(variable)[:120].rstrip("."))
    bits.append("Experiment, not a panel candidate.")
    return _clean(" ".join(bits))


def check(desc):
    """-> (ok, reason). Callers use this before writing, so a description that
    drifted back into prose is refused at the seam rather than discovered
    later in a review page."""
    if not desc or not desc.strip():
        return False, "empty description"
    if len(desc) > MAX_CHARS:
        return False, ("%d chars, limit %d -- this is a label, not a report; "
                       "put the detail in its own field or a Note"
                       % (len(desc), MAX_CHARS))
    if "\n" in desc:
        return False, "contains a line break; a label is one paragraph"
    return True, ""


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-72s %s" % (name[:72], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    a = panel("SHOW01_A_0060", 4, ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"],
              set_code="SHOW_SET_PILOTCHARB_BEDROOM", sequence="SHOW01_C",
              qc_status="FAIL", qc_failed=["SHOW_CHAR_PILOTCHARB"])
    print("\n  example: %s\n" % a)
    ck("a panel label is short", len(a) < 220)
    ck("it says what is being reviewed", a.startswith("Panel candidate v004"))
    ck("it names the characters and the set",
       "SHOW_CHAR_PILOTCHARB" in a and "SHOW_SET_PILOTCHARB_BEDROOM" in a)
    ck("the QC verdict is a token plus who failed, never prose",
       "Attribute QC: FAIL (SHOW_CHAR_PILOTCHARB)." in a)

    # DETERMINISM, the strict rule. Same inputs, byte-identical output, and
    # caller ordering must not leak in.
    b = panel("SHOW01_A_0060", 4, ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"],
              set_code="SHOW_SET_PILOTCHARB_BEDROOM", sequence="SHOW01_C",
              qc_status="FAIL", qc_failed=["SHOW_CHAR_PILOTCHARB"])
    ck("the same inputs produce a byte-identical string", a == b)
    c = panel("SHOW01_A_0060", 4, ["SHOW_CHAR_PILOTCHARB", "SHOW_CHAR_PILOTCHARA"],
              set_code="SHOW_SET_PILOTCHARB_BEDROOM", sequence="SHOW01_C",
              qc_status="FAIL", qc_failed=["SHOW_CHAR_PILOTCHARB"])
    ck("character ORDER from the caller cannot change the output", a == c)
    ck("no timestamp, duration or elapsed time appears",
       not any(t in a.lower() for t in ("s)", "seconds", "took", "20260", "2026-")))

    d = design("SHOW_CHAR_PILOTCHARA", "edit",
               source_version="SHOW_CHAR_PILOTCHARA_ANCHOR_turbo10_s52003",
               instruction="Remove all lettering from the t-shirt", seed=53001)
    ck("an edit names what it was edited FROM", "Edited from" in d)
    ck("a design label stays short", len(d) < 250)
    ck("design is deterministic",
       d == design("SHOW_CHAR_PILOTCHARA", "edit",
                   source_version="SHOW_CHAR_PILOTCHARA_ANCHOR_turbo10_s52003",
                   instruction="Remove all lettering from the t-shirt", seed=53001))

    w = wedge("SHOW01_A_0060", "REGIONALA", 9001,
              ["SHOW_CHAR_PILOTCHARA", "SHOW_CHAR_PILOTCHARB"],
              variable="one masked region per character")
    ck("a wedge names its family, seed and variable",
       "REGIONALA" in w and "9001" in w and "Variable:" in w)
    ck("a wedge says it is not a panel candidate",
       "not a panel candidate" in w)
    ck("a wedge label stays short", len(w) < 250)

    ck("check() passes a real label", check(a)[0])
    ck("check() refuses an essay", not check("x" * (MAX_CHARS + 1))[0])
    ck("check() refuses a multi-paragraph description",
       not check("line one\nline two")[0])
    ck("check() refuses an empty description", not check("")[0])
    ck("the limit is stated in the refusal, not just the fact of it",
       "limit %d" % MAX_CHARS in check("x" * (MAX_CHARS + 1))[1])

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
