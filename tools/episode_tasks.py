#!/usr/bin/env python3
"""Create an episode's Shot Tasks, matching an existing episode's convention.

WHY THIS EXISTS. SHOW01's 55 shots had ZERO Tasks. PILOT01's 121 shots have 265
between them. The publish contract carries the Task: 324 of the 327
Shot-entity Versions on this project have sg_task set, and every Shot
publisher (animatic.board_task, panel_compose.panel_task,
genvideo_worker.py, video_from_panel.py) resolves-or-CREATES its own Task
before publishing. So SHOW01 was not broken -- it would have grown its Tasks
one at a time, invisibly, as each stage first ran. What it did not have was a
Shot page a producer can read: no Board/Comp/Panel row to set a status on,
nothing to schedule against, no task-based filter that returns anything.

Assets need none of this and this tool deliberately does not create any: all
268 Tasks on this project are on Shots, ZERO on Assets, and
sg_publish.publish_version() takes sg_task=None. The anchor/design stage
genuinely does not use Tasks. Counted, not assumed.

THE CONVENTION IS READ, NOT GUESSED. --reference (default PILOT01) is queried
live and its distinct (Task.content, Task.step) pairs become the convention
applied to the target episode. Today that reads back exactly:

    Board  -> Step 441 "Board" (short_name BRD)
    Comp   -> Step   8 "Comp"  (short_name CMP)
    Panel  -> Step 472 "Panel" (short_name PNL)
    task_template: none on any of the 268 existing Tasks
    sg_status_list: left to ShotGrid's own default, 'wtg', which is what
                    263 of the 268 existing Tasks sit at

Hardcoding those ids would have been a guess that happened to be right; this
way a change to the reference episode's own shape carries across, and
--self-test asserts the LIVE read still matches the shape recorded above, so
a silent drift is caught rather than copied.

IDEMPOTENCE, and why the guard key matters more than the guard. The defect
this tool was written to avoid is live in this repo: sg_create_event_assets.py
has no find_one guard and duplicates Assets 12197-12202 on re-run, and three
PILOT01 shots (12662/12663/12664) already carry TWO 'Board' Tasks each from
exactly that omission. So this tool guards -- but it guards on the SAME KEY
the publishers use:

    ["entity", "is", shot], ["step.Step.short_name", "is", <SHORT>]

not on Task.content. If it keyed on content and a publisher keyed on step,
the two would not see each other and would duplicate each other's work the
first time any stage ran. Same key, one Task. --audit reports any existing
duplicates without touching them.

    python episode_tasks.py --self-test
    python episode_tasks.py --audit
    python episode_tasks.py --episode SHOW01 --dry-run
    python episode_tasks.py --episode SHOW01
"""
import argparse
import os
import sys
from collections import Counter, OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episode_context as EPCTX                                # noqa: E402

PROJECT_ID = 9999
PROJ = {"type": "Project", "id": PROJECT_ID}
DEFAULT_REFERENCE = "PILOT01"

# What the live read off PILOT01 is EXPECTED to return. Not used to create
# anything -- read_convention() supplies the real values -- but asserted in
# --self-test so a drift in the reference episode is caught loudly instead of
# silently copied onto a new episode.
EXPECTED_CONVENTION = (("Board", "BRD"), ("Comp", "CMP"), ("Panel", "PNL"))


def log(m):
    print("[episode_tasks] %s" % m, flush=True)


def sg_connect():
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


# ------------------------------------------------------------------ scoping
def episode_shots(sg, ep):
    """-> ([shot rows], [orphan codes]) for this episode.

    Scoped through the episode's ACT SEQUENCES (episode_context's `acts`),
    which is the authoritative link -- Shot.sg_episode is set on PILOT01's 121
    shots and NULL on all 55 of SHOW01's, so it cannot be the selector.

    The code-prefix match every other tool in this repo uses
    (note_triage/conform/finishing all do `code starts_with EP.code`) is NOT
    a second selection mechanism here -- it is only cross-checked, and any
    shot it finds that the sequence link does not is reported as an ORPHAN and
    LEFT ALONE. A shot silently included by a looser rule, or silently
    skipped by a tighter one, is exactly the kind of thing that shows up
    three weeks later as a missing deliverable."""
    seqs = sg.find("Sequence", [["project", "is", PROJ], ["code", "in", list(ep.acts)]],
                   ["code"])
    if not seqs:
        sys.exit("FATAL: none of episode %s's act Sequences %r exist in project %d"
                 % (ep.code, ep.acts, PROJECT_ID))
    shots = sg.find("Shot",
                    [["project", "is", PROJ],
                     ["sg_sequence", "in", [{"type": "Sequence", "id": s["id"]} for s in seqs]]],
                    ["code", "sg_sequence"], order=[{"field_name": "code", "direction": "asc"}])
    by_id = set(s["id"] for s in shots)
    prefixed = sg.find("Shot", [["project", "is", PROJ], ["code", "starts_with", ep.code]],
                       ["code"])
    orphans = [s["code"] for s in prefixed if s["id"] not in by_id]
    return shots, orphans


# --------------------------------------------------------------- convention
def read_convention(sg, reference_code=DEFAULT_REFERENCE):
    """-> OrderedDict content -> {"step": {...}, "short_name": "BRD", "n": 124}

    Read LIVE off the reference episode's own Shot Tasks. Ordered by how many
    shots use each, which is also pipeline order here (Board 124, Comp 78,
    Panel 66). Refuses loudly rather than inventing a default if the
    reference has no Tasks at all -- a tool that silently falls back to a
    guess is the failure this docstring's own rule is about."""
    ref = EPCTX.resolve_episode(reference_code)
    seqs = sg.find("Sequence", [["project", "is", PROJ], ["code", "in", list(ref.acts)]], ["code"])
    shots = sg.find("Shot",
                    [["project", "is", PROJ],
                     ["sg_sequence", "in", [{"type": "Sequence", "id": s["id"]} for s in seqs]]],
                    ["code"])
    if not shots:
        sys.exit("FATAL: reference episode %s has no shots -- nothing to copy a "
                 "convention from" % reference_code)
    tasks = sg.find("Task",
                    [["entity", "in", [{"type": "Shot", "id": s["id"]} for s in shots]]],
                    ["content", "step", "task_template"])
    if not tasks:
        sys.exit("FATAL: reference episode %s has no Shot Tasks -- refusing to invent "
                 "a convention" % reference_code)

    templates = set(str((t.get("task_template") or {}).get("name")) for t in tasks)
    if templates != {"None"}:
        log("NOTE: the reference episode's Tasks use task_template(s) %r -- this tool "
            "does not set one; check that is still right" % sorted(templates))

    counts = Counter()
    steps = {}
    for t in tasks:
        c, st = t.get("content"), t.get("step")
        if not c or not st:
            continue
        counts[c] += 1
        steps[c] = st
    if not counts:
        sys.exit("FATAL: reference episode %s's Tasks have no content/step pair to copy"
                 % reference_code)

    step_rows = sg.find("Step", [["id", "in", [s["id"] for s in steps.values()]]],
                        ["short_name", "code", "entity_type"])
    short_by_id = {}
    for r in step_rows:
        if r.get("entity_type") != "Shot":
            sys.exit("FATAL: Step %s (%r) is for %r, not Shot -- refusing to attach a "
                     "Shot Task to it" % (r["id"], r.get("code"), r.get("entity_type")))
        short_by_id[r["id"]] = r.get("short_name")

    out = OrderedDict()
    for content, n in counts.most_common():
        st = steps[content]
        short = short_by_id.get(st["id"])
        if not short:
            sys.exit("FATAL: Step %s (for Task %r) has no short_name -- the idempotence "
                     "guard keys on it, so it cannot be skipped" % (st["id"], content))
        out[content] = {"step": {"type": "Step", "id": st["id"]}, "short_name": short, "n": n}
    return out


# --------------------------------------------------------------------- work
def ensure_task(sg, shot, content, spec, dry_run=False):
    """-> ("exists"|"created"|"would_create", task_or_None).

    THE GUARD. Keyed on step short_name, byte-for-byte the same filter
    animatic.board_task() / panel_compose.panel_task() /
    genvideo_worker.py / video_from_panel.py already use, so this tool and
    those publishers can never create a second Task for the same step on the
    same shot."""
    found = sg.find_one("Task",
                        [["entity", "is", {"type": "Shot", "id": shot["id"]}],
                         ["step.Step.short_name", "is", spec["short_name"]]],
                        ["content", "step"])
    if found:
        return "exists", found
    if dry_run:
        return "would_create", None
    t = sg.create("Task", {"project": PROJ,
                           "entity": {"type": "Shot", "id": shot["id"]},
                           "step": spec["step"],
                           "content": content})
    return "created", t


def cmd_create(sg, ep_code, reference_code, dry_run):
    ep = EPCTX.resolve_episode(ep_code)
    convention = read_convention(sg, reference_code)
    log("convention read live off %s: %s" % (reference_code, ", ".join(
        "%s(step %s/%s, %d shots)" % (c, s["step"]["id"], s["short_name"], s["n"])
        for c, s in convention.items())))
    shots, orphans = episode_shots(sg, ep)
    log("%s: %d shot(s) via act Sequences %r" % (ep.code, len(shots), ep.acts))
    if orphans:
        log("WARNING: %d shot(s) whose code starts with %s are NOT in any act Sequence "
            "and were LEFT ALONE: %s" % (len(orphans), ep.code, ", ".join(sorted(orphans))))
    if not shots:
        sys.exit("FATAL: no shots resolved for %s -- refusing to report success on "
                 "an empty run" % ep.code)

    tally = Counter()
    for shot in shots:
        made = []
        for content, spec in convention.items():
            status, _ = ensure_task(sg, shot, content, spec, dry_run=dry_run)
            tally[status] += 1
            if status in ("created", "would_create"):
                made.append(content)
        if made:
            log("  %-16s %s %s" % (shot["code"], "would create" if dry_run else "created",
                                   "+".join(made)))
    log("%s: %d created, %d would create, %d already existed"
        % ("DRY RUN" if dry_run else "done", tally["created"], tally["would_create"],
           tally["exists"]))
    return 0


def cmd_audit(sg):
    """Report duplicate Tasks per (shot, step). Reports only -- deleting a
    Task can take a Version's sg_task with it, and that is a human call."""
    tasks = sg.find("Task", [["project", "is", PROJ]], ["content", "entity", "step"])
    seen = {}
    for t in tasks:
        ent = t.get("entity") or {}
        st = t.get("step") or {}
        seen.setdefault((ent.get("type"), ent.get("id"), ent.get("name"), st.get("id")),
                        []).append(t)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    log("%d Task(s) on %d entity/step slot(s)" % (len(tasks), len(seen)))
    by_entity_type = Counter(k[0] for k in seen)
    log("by entity type: %s" % dict(by_entity_type))
    if not dupes:
        log("no duplicate Tasks")
        return 0
    for (etype, eid, ename, step_id), rows in sorted(dupes.items(), key=lambda x: str(x[0])):
        log("DUPLICATE: %s %s (%s) step %s -> Task ids %s (%r)"
            % (etype, eid, ename, step_id, [r["id"] for r in rows],
               [r.get("content") for r in rows]))
    log("%d duplicate slot(s). Not deleting: a Task may be referenced by a Version's "
        "sg_task, so removal is a human call." % len(dupes))
    return 1


# ---------------------------------------------------------------- self-test
class _StubSG(object):
    """Enough ShotGrid to exercise the real functions: Sequences, Shots,
    Tasks and Steps, with create() writing back into the Task table so a
    SECOND run genuinely sees the first run's Tasks -- which is the entire
    property the idempotence canary tests. A mock that forgets its own writes
    cannot catch a duplication bug."""

    def __init__(self):
        self.sequences = [{"id": 1, "code": "SHOW01_A"}, {"id": 2, "code": "PILOT01_A"},
                          {"id": 3, "code": "PILOT01_B"}, {"id": 4, "code": "PILOT01_C"}]
        self.shots = [{"id": 10, "code": "SHOW01_A_0010", "sg_sequence": {"type": "Sequence", "id": 1}},
                      {"id": 11, "code": "SHOW01_A_0020", "sg_sequence": {"type": "Sequence", "id": 1}},
                      {"id": 20, "code": "PILOT01_A_0010", "sg_sequence": {"type": "Sequence", "id": 2}}]
        self.steps = [{"id": 441, "code": "Board", "short_name": "BRD", "entity_type": "Shot"},
                      {"id": 8, "code": "Comp", "short_name": "CMP", "entity_type": "Shot"},
                      {"id": 472, "code": "Panel", "short_name": "PNL", "entity_type": "Shot"}]
        # The reference episode's existing Tasks -- Board twice, Comp twice,
        # Panel once, so read_convention()'s ordering has something to order.
        self.tasks = [
            {"id": 900, "content": "Board", "entity": {"type": "Shot", "id": 20},
             "step": {"type": "Step", "id": 441}, "task_template": None},
            {"id": 901, "content": "Comp", "entity": {"type": "Shot", "id": 20},
             "step": {"type": "Step", "id": 8}, "task_template": None},
            {"id": 902, "content": "Panel", "entity": {"type": "Shot", "id": 20},
             "step": {"type": "Step", "id": 472}, "task_template": None},
        ]
        self._next = 1000
        self.created = []

    # --- helpers
    def _step_short(self, step_id):
        for s in self.steps:
            if s["id"] == step_id:
                return s["short_name"]
        return None

    def find(self, entity_type, filters, fields=None, **kw):
        if entity_type == "Sequence":
            codes = None
            for f in filters:
                if f[0] == "code" and f[1] == "in":
                    codes = f[2]
            return [dict(s) for s in self.sequences if codes is None or s["code"] in codes]
        if entity_type == "Shot":
            seq_ids, prefix = None, None
            for f in filters:
                if f[0] == "sg_sequence" and f[1] == "in":
                    seq_ids = [q["id"] for q in f[2]]
                if f[0] == "code" and f[1] == "starts_with":
                    prefix = f[2]
            out = list(self.shots)
            if seq_ids is not None:
                out = [s for s in out if (s.get("sg_sequence") or {}).get("id") in seq_ids]
            if prefix is not None:
                out = [s for s in out if s["code"].startswith(prefix)]
            return [dict(s) for s in out]
        if entity_type == "Task":
            ent_ids = None
            for f in filters:
                if f[0] == "entity" and f[1] == "in":
                    ent_ids = [q["id"] for q in f[2]]
            return [dict(t) for t in self.tasks
                    if ent_ids is None or (t.get("entity") or {}).get("id") in ent_ids]
        if entity_type == "Step":
            ids = None
            for f in filters:
                if f[0] == "id" and f[1] == "in":
                    ids = f[2]
            return [dict(s) for s in self.steps if ids is None or s["id"] in ids]
        return []

    def find_one(self, entity_type, filters, fields=None, **kw):
        if entity_type != "Task":
            return None
        shot_id, short = None, None
        for f in filters:
            if f[0] == "entity" and f[1] == "is":
                shot_id = f[2]["id"]
            if f[0] == "step.Step.short_name" and f[1] == "is":
                short = f[2]
        for t in self.tasks:
            if (t.get("entity") or {}).get("id") != shot_id:
                continue
            if self._step_short((t.get("step") or {}).get("id")) == short:
                return dict(t)
        return None

    def create(self, entity_type, data):
        self._next += 1
        row = dict(data)
        row["id"] = self._next
        if entity_type == "Task":
            self.tasks.append(row)
        self.created.append((entity_type, row))
        return row


def self_test(live_sg=None):
    fails = []

    def ck(name, cond):
        print("  %-76s %s" % (name, "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    sg = _StubSG()
    conv = read_convention(sg, "PILOT01")
    ck("the convention is READ off the reference episode, not hardcoded",
       list(conv) == ["Board", "Comp", "Panel"])
    ck("each convention entry carries the real Step id and its short_name",
       conv["Board"]["step"]["id"] == 441 and conv["Board"]["short_name"] == "BRD"
       and conv["Comp"]["step"]["id"] == 8 and conv["Panel"]["short_name"] == "PNL")

    ep = EPCTX.resolve_episode("SHOW01")
    shots, orphans = episode_shots(sg, ep)
    ck("episode scoping selects only the target episode's shots",
       [s["code"] for s in shots] == ["SHOW01_A_0010", "SHOW01_A_0020"])
    ck("CANARY: a shot from another episode is never selected",
       all(not s["code"].startswith("PILOT01") for s in shots))

    # THE IDEMPOTENCE CANARY. Run the real create pass TWICE against a stub
    # that remembers its own writes. Six Tasks after the first run, six after
    # the second. sg_create_event_assets.py's missing guard duplicated Assets
    # 12197-12202 exactly this way, and three PILOT01 shots carry a second
    # 'Board' Task from the same omission -- this is that bug, reproduced as
    # a test.
    before = len(sg.tasks)
    cmd_create(sg, "SHOW01", "PILOT01", dry_run=False)
    after_one = len(sg.tasks)
    cmd_create(sg, "SHOW01", "PILOT01", dry_run=False)
    after_two = len(sg.tasks)
    ck("first run creates 3 Tasks per shot (2 shots -> 6)", after_one - before == 6)
    ck("CANARY (idempotence): the SECOND run creates nothing -- a re-run cannot "
       "duplicate", after_two == after_one)
    ck("CANARY: no shot ends up with two Tasks on the same step",
       max(Counter(((t.get("entity") or {}).get("id"), (t.get("step") or {}).get("id"))
                   for t in sg.tasks).values()) == 1)

    # The guard must recognise a Task a PUBLISHER made. animatic.board_task()
    # creates content="Board" on the BRD step; if this tool keyed on content
    # instead of the step it would still match here, so the sharper test is a
    # Task with a DIFFERENT content on the same step -- which a publisher can
    # legitimately produce, and which must still be recognised as "the Board
    # slot is taken".
    sg2 = _StubSG()
    sg2.tasks.append({"id": 950, "content": "Boards (legacy name)",
                      "entity": {"type": "Shot", "id": 10},
                      "step": {"type": "Step", "id": 441}, "task_template": None})
    n_before = len(sg2.tasks)
    cmd_create(sg2, "SHOW01", "PILOT01", dry_run=False)
    board_tasks_on_10 = [t for t in sg2.tasks
                         if (t.get("entity") or {}).get("id") == 10
                         and (t.get("step") or {}).get("id") == 441]
    ck("CANARY: the guard keys on STEP, not on Task.content -- an existing Board-step "
       "Task under a different name is not duplicated", len(board_tasks_on_10) == 1)
    ck("the other shots still got their full set", len(sg2.tasks) - n_before == 5)

    # dry run must write nothing at all
    sg3 = _StubSG()
    n3 = len(sg3.tasks)
    cmd_create(sg3, "SHOW01", "PILOT01", dry_run=True)
    ck("CANARY: --dry-run creates nothing", len(sg3.tasks) == n3 and sg3.created == [])

    # refusing beats inventing
    class _NoTasksSG(_StubSG):
        def __init__(self):
            _StubSG.__init__(self)
            self.tasks = []

    try:
        read_convention(_NoTasksSG(), "PILOT01")
        ck("CANARY: a reference episode with no Tasks REFUSES rather than inventing "
           "a convention", False)
    except SystemExit:
        ck("CANARY: a reference episode with no Tasks REFUSES rather than inventing "
           "a convention", True)

    class _AssetStepSG(_StubSG):
        def __init__(self):
            _StubSG.__init__(self)
            self.steps = [dict(s, entity_type="Asset") for s in self.steps]

    try:
        read_convention(_AssetStepSG(), "PILOT01")
        ck("CANARY: a Step that is not a Shot step REFUSES rather than attaching a "
           "Shot Task to it", False)
    except SystemExit:
        ck("CANARY: a Step that is not a Shot step REFUSES rather than attaching a "
           "Shot Task to it", True)

    # --- LIVE shape check: the real reference episode must still look like
    # EXPECTED_CONVENTION. This is what catches a drift in PILOT01 being
    # copied silently onto a new episode.
    if live_sg is not None:
        live = read_convention(live_sg, DEFAULT_REFERENCE)
        got = tuple((c, s["short_name"]) for c, s in live.items())
        ck("LIVE: the real %s convention still matches the recorded shape %r (got %r)"
           % (DEFAULT_REFERENCE, EXPECTED_CONVENTION, got), got == EXPECTED_CONVENTION)

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", metavar="CODE",
                    help="episode to create Tasks for (else GENVIDEO_EPISODE / registry "
                         "default -- see episode_context.py)")
    ap.add_argument("--reference", metavar="CODE", default=DEFAULT_REFERENCE,
                    help="episode whose Task convention is copied (default %s)"
                         % DEFAULT_REFERENCE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--audit", action="store_true",
                    help="report duplicate Tasks per (entity, step); writes nothing")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--live-shape", action="store_true",
                    help="with --self-test: also check the real reference episode's "
                         "convention against the recorded shape (needs ShotGrid)")
    ns = ap.parse_args()

    if ns.self_test:
        return self_test(live_sg=sg_connect() if ns.live_shape else None)
    sg = sg_connect()
    if ns.audit:
        return cmd_audit(sg)
    return cmd_create(sg, ns.episode, ns.reference, ns.dry_run)


if __name__ == "__main__":
    sys.exit(main())
