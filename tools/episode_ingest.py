#!/usr/bin/env python3
"""Episode ingest: a timing CSV in, an episode skeleton of Sequences+Shots out.

This is the front door of the pipeline the worker (genvideo_worker.py) drives:
an editor writes one CSV for the episode, this script materialises it in
ShotGrid, and the worker takes over the moment a human flips shots to queued.

CSV CONTRACT (header row required, UTF-8, extra columns are ignored):

  column           required  meaning
  ---------------  --------  ----------------------------------------------
  sequence         yes       Sequence code, e.g. EP01_SQ010
  shot             yes       Shot code, e.g. EP01_SQ010_SH0010. Must be
                             unique within the file.
  cut_order        yes       positive integer, position in the cut
  duration_frames  yes       positive integer. kind=video: (frames-1) must
                             divide by 4 (Wan constraint). kind=still: 1.
  prompt           yes       positive prompt, non-empty
  kind             no        still | video (default video)
  size             no        WxH like 832x480 (worker default if blank)
  steps            no        positive integer
  cfg              no        positive number (worker defaults to 5.0)
  seed             no        integer
  refs             no        semicolon-separated Asset codes, in order; the
                             FIRST becomes the i2v start frame (worker rule)
  negative_prompt  no        overrides the worker's universal negative

Behaviour rules (each is a promise, not a preference):
  - EVERY row is validated before ShotGrid is touched. All errors are
    collected and the WHOLE file is refused on any error, so a bad CSV can
    never half-ingest an episode. Validation does read-only Asset lookups
    (refs must resolve); nothing is created or updated until all rows pass.
  - Idempotent: an existing Sequence/Shot code is updated in place, never
    duplicated. Re-running the same CSV is safe.
  - sg_gen_status is set to "queued" ONLY with --queue. Default leaves it
    unset so a human reviews the skeleton before the worker picks it up.
    On update without --queue the field is not touched at all, so a re-run
    cannot silently requeue a shot that is mid-review.
  - sg_cut_order is used when the Shot schema has it; otherwise the order
    is recorded in the Shot description so it is never simply dropped.
  - --dry-run prints the full plan (creates, updates, links) and exits
    without writing anything.

Usage:
    python episode_ingest.py episode.csv --dry-run     validate + print plan
    python episode_ingest.py episode.csv               ingest, status unset
    python episode_ingest.py episode.csv --queue       ingest and queue
    python episode_ingest.py --self-test               offline fixtures, no network
"""
import csv
import os
import re
import sys

PROJECT_ID = 9999

REQUIRED_COLUMNS = ("sequence", "shot", "cut_order", "duration_frames", "prompt")
OPTIONAL_COLUMNS = ("kind", "size", "steps", "cfg", "seed", "refs", "negative_prompt")
KINDS = ("still", "video")


def log(msg):
    print("[ingest] %s" % msg, flush=True)


def sg_connect():
    # FAIL LOUDLY if the env vars are absent. A default here would mean
    # silently talking to the wrong site or authenticating as nobody.
    # (pm_backend_shotgrid.get_backend() performs the same check itself,
    # in one place instead of this and sixteen other copies -- kept here
    # too only so the message stays identical for anyone grepping it.)
    missing = [k for k in ("SHOTGRID_SITE_URL", "SHOTGRID_SCRIPT_NAME",
                           "SHOTGRID_SCRIPT_KEY") if not os.environ.get(k)]
    if missing:
        sys.exit("FATAL: missing environment variable(s): %s" % ", ".join(missing))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import pm_backend_shotgrid as PMB
    return PMB.get_backend()


# ---------------------------------------------------------------- parsing
def read_rows(path):
    """Read the CSV into a list of dicts. Returns (rows, errors). Structural
    problems (missing file, missing required columns, empty file) are errors
    of the whole file, reported once, not per row."""
    errors = []
    if not os.path.isfile(path):
        return [], ["file not found: %s" % path]
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip().lower() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            return [], ["missing required column(s): %s (header was: %s)"
                        % (", ".join(missing), ", ".join(header) or "<empty>")]
        rows = []
        for i, raw in enumerate(reader, start=2):     # start=2: line 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in raw.items() if k is not None}
            row["_line"] = i
            rows.append(row)
    if not rows:
        errors.append("no data rows in %s" % path)
    return rows, errors


# ---------------------------------------------------------------- validation
def validate_rows(rows, resolvable_asset_codes):
    """Pure validation, no ShotGrid access. `resolvable_asset_codes` is the
    set of Asset codes that exist in the project (looked up read-only by the
    caller). Returns a list of error strings; empty means the file is good.
    ALL errors are collected so the editor fixes the file once, not N times."""
    errors = []
    seen_shots = {}
    for row in rows:
        line = row["_line"]

        def err(msg):
            errors.append("line %d (shot %r): %s" % (line, row.get("shot") or "?", msg))

        for col in ("sequence", "shot", "prompt"):
            if not row.get(col):
                err("column %r is empty" % col)

        shot = row.get("shot")
        if shot:
            if shot in seen_shots:
                err("duplicate shot code (already on line %d); which row wins "
                    "would be ambiguous, so both are refused" % seen_shots[shot])
            else:
                seen_shots[shot] = line

        cut = row.get("cut_order", "")
        if not re.fullmatch(r"[1-9]\d*", cut):
            err("cut_order %r is not a positive integer" % cut)

        kind = row.get("kind") or "video"
        if kind not in KINDS:
            err("kind %r is not one of %s" % (kind, "|".join(KINDS)))

        frames_s = row.get("duration_frames", "")
        if not re.fullmatch(r"[1-9]\d*", frames_s):
            err("duration_frames %r is not a positive integer" % frames_s)
        else:
            frames = int(frames_s)
            if kind == "still" and frames != 1:
                err("duration_frames=%d but kind=still requires exactly 1" % frames)
            # Wan constraint: the model only accepts lengths where
            # (frames-1) divides by 4. Catch it here, not at render time.
            if kind == "video" and (frames - 1) % 4 != 0:
                err("duration_frames=%d invalid: (frames-1) must divide by 4 "
                    "(Wan constraint); nearest valid are %d and %d"
                    % (frames, frames - ((frames - 1) % 4), frames + 4 - ((frames - 1) % 4)))

        size = row.get("size", "")
        if size and not re.fullmatch(r"\d+x\d+", size.lower().replace(" ", "")):
            err("size %r is not WxH like 832x480" % size)

        steps = row.get("steps", "")
        if steps and not re.fullmatch(r"[1-9]\d*", steps):
            err("steps %r is not a positive integer" % steps)

        cfg = row.get("cfg", "")
        if cfg:
            try:
                v = float(cfg)
                # 0 < v < inf refuses nan too (nan fails every comparison);
                # a plain "v <= 0" check would let cfg=nan reach ShotGrid.
                if not (0 < v < float("inf")):
                    err("cfg %r must be a positive finite number" % cfg)
            except ValueError:
                err("cfg %r is not a number" % cfg)

        seed = row.get("seed", "")
        if seed and not re.fullmatch(r"-?\d+", seed):
            err("seed %r is not an integer" % seed)

        for code in ref_codes(row):
            if code not in resolvable_asset_codes:
                err("unresolved asset ref %r: no Asset with that code in the "
                    "project" % code)
    return errors


def ref_codes(row):
    return [c.strip() for c in (row.get("refs") or "").split(";") if c.strip()]


def resolve_assets(sg, rows):
    """Read-only: map every referenced Asset code to its id in one query.
    Codes that do not come back simply stay unmapped and validation names
    them as errors."""
    codes = sorted({c for row in rows for c in ref_codes(row)})
    if not codes:
        return {}
    found = sg.find("Asset", [["project", "is", {"type": "Project", "id": PROJECT_ID}],
                              ["code", "in", codes]], ["code"])
    return {a["code"]: a["id"] for a in found}


# ---------------------------------------------------------------- planning
def shot_has_cut_order_field(sg):
    """True when the Shot schema carries sg_cut_order. Checked once per run;
    when absent we refuse to invent the field and record order in the
    description instead, so the information is never dropped."""
    try:
        return "sg_cut_order" in sg.schema_field_read("Shot", "sg_cut_order")
    except Exception as e:
        # Say WHY out loud: a transient schema-read failure must not silently
        # masquerade as "field does not exist" - the fallback overwrites shot
        # descriptions, and the operator deserves to see what triggered it.
        log("note: Shot.sg_cut_order not readable (%s: %s); recording cut "
            "order in the description instead" % (type(e).__name__, e))
        return False


def build_plan(sg, rows, queue):
    """Turn validated rows into an explicit list of actions. The plan is
    computed in full (including existing-entity lookups) BEFORE anything is
    written, so --dry-run shows exactly what a real run would do."""
    project = {"type": "Project", "id": PROJECT_ID}
    asset_ids = resolve_assets(sg, rows)
    has_cut_field = shot_has_cut_order_field(sg)

    seq_codes = []
    for row in rows:                       # preserve file order, first mention wins
        if row["sequence"] not in seq_codes:
            seq_codes.append(row["sequence"])
    existing_seqs = {s["code"]: s["id"] for s in sg.find(
        "Sequence", [["project", "is", project], ["code", "in", seq_codes]], ["code"])}
    existing_shots = {s["code"]: s["id"] for s in sg.find(
        "Shot", [["project", "is", project],
                 ["code", "in", [r["shot"] for r in rows]]], ["code"])}

    plan = []
    for code in seq_codes:
        if code in existing_seqs:
            plan.append({"action": "keep_sequence", "code": code,
                         "id": existing_seqs[code]})
        else:
            plan.append({"action": "create_sequence", "code": code})

    for row in rows:
        fields = {
            "sg_sequence": {"code": row["sequence"]},   # id filled at apply time
            "sg_gen_prompt": row["prompt"],
            "sg_gen_kind": row.get("kind") or "video",
            "sg_gen_frames": int(row["duration_frames"]),
        }
        if row.get("negative_prompt"):
            fields["sg_gen_negative_prompt"] = row["negative_prompt"]
        if row.get("size"):
            fields["sg_gen_size_wxh"] = row["size"].lower().replace(" ", "")
        if row.get("steps"):
            fields["sg_gen_steps"] = int(row["steps"])
        if row.get("cfg"):
            fields["sg_gen_cfg"] = float(row["cfg"])
        if row.get("seed"):
            fields["sg_gen_seed"] = int(row["seed"])
        refs = ref_codes(row)
        if refs:
            # CSV order preserved: the worker treats the FIRST linked Asset
            # as the i2v start frame, so order is meaning, not cosmetics.
            fields["assets"] = [{"type": "Asset", "id": asset_ids[c]} for c in refs]
        cut = int(row["cut_order"])
        if has_cut_field:
            fields["sg_cut_order"] = cut
        else:
            fields["description"] = ("cut_order=%d. Ingested by episode_ingest "
                                     "(no sg_cut_order field on this site)." % cut)
        if queue:
            fields["sg_gen_status"] = "queued"
        action = "update_shot" if row["shot"] in existing_shots else "create_shot"
        plan.append({"action": action, "code": row["shot"], "fields": fields,
                     "id": existing_shots.get(row["shot"]), "refs": refs})
    return plan


def print_plan(plan, queue):
    log("PLAN (%d action(s), sg_gen_status %s):"
        % (len(plan), "-> queued" if queue else "left unset for human review"))
    for p in plan:
        if p["action"] == "keep_sequence":
            log("  keep   Sequence %s (id %d)" % (p["code"], p["id"]))
        elif p["action"] == "create_sequence":
            log("  create Sequence %s" % p["code"])
        else:
            verb = "update" if p["action"] == "update_shot" else "create"
            refs = (", refs: " + ";".join(p["refs"])) if p["refs"] else ", no refs (t2v)"
            log("  %s Shot %s (%s, %d frames%s)"
                % (verb, p["code"], p["fields"]["sg_gen_kind"],
                   p["fields"]["sg_gen_frames"], refs))


def apply_plan(sg, plan):
    """Execute the plan. Sequences first so shots can link to them."""
    project = {"type": "Project", "id": PROJECT_ID}
    seq_ids = {}
    for p in plan:
        if p["action"] == "keep_sequence":
            seq_ids[p["code"]] = p["id"]
        elif p["action"] == "create_sequence":
            created = sg.create("Sequence", {"project": project, "code": p["code"]})
            seq_ids[p["code"]] = created["id"]
            log("  created Sequence %s (id %d)" % (p["code"], created["id"]))
    for p in plan:
        if p["action"] not in ("create_shot", "update_shot"):
            continue
        fields = dict(p["fields"])
        fields["sg_sequence"] = {"type": "Sequence",
                                 "id": seq_ids[fields["sg_sequence"]["code"]]}
        if p["action"] == "create_shot":
            fields["project"] = project
            fields["code"] = p["code"]
            # A PROPOSED PROMPT IS AUTO-APPLIED. Geoff, 2026-09-07: *"I have
            # already said the operator should not have to accept the proposed
            # prompt at this stage, it should just be rerendered. At a later
            # stage if GPU becomes more controlled or scarce we may introduce
            # that gate, for now proposed prompts are auto approved."*
            #
            # SET AT CREATION, not swept per episode. The ShotGrid checkbox
            # defaults to OFF, so every new episode would otherwise silently
            # reintroduce the accept gate and someone would have to notice and
            # flip 55 shots by hand. That is the episode-agnostic rule Geoff
            # asked for as a design paradigm: a new show inherits the
            # behaviour, it does not need a migration.
            fields.setdefault("sg_auto_apply_proposals", True)
            created = sg.create("Shot", fields)
            log("  created Shot %s (id %d)" % (p["code"], created["id"]))
        else:
            sg.update("Shot", p["id"], fields)
            log("  updated Shot %s (id %d)" % (p["code"], p["id"]))


# ---------------------------------------------------------------- main
def ingest(sg, csv_path, dry_run, queue):
    """Returns 0 on success, 1 on refusal. Split from main() so the self-test
    can drive it with a mock sg."""
    rows, errors = read_rows(csv_path)
    if not errors:
        # Read-only lookup for validation; sg is not MUTATED before all
        # rows pass, which is the never-half-ingest promise.
        errors = validate_rows(rows, set(resolve_assets(sg, rows)))
    if errors:
        log("REFUSED: %s has %d error(s); nothing was ingested:"
            % (csv_path, len(errors)))
        for e in errors:
            log("  - %s" % e)
        return 1
    plan = build_plan(sg, rows, queue)
    print_plan(plan, queue)
    if dry_run:
        log("dry-run: nothing written")
        return 0
    apply_plan(sg, plan)
    log("ingest complete: %d row(s) from %s" % (len(rows), csv_path))
    return 0


def main():
    args = sys.argv[1:]
    if "--self-test" in args:
        return self_test()
    dry_run = "--dry-run" in args
    queue = "--queue" in args
    paths = [a for a in args if not a.startswith("--")]
    if len(paths) != 1:
        sys.exit("usage: episode_ingest.py <episode.csv> [--dry-run] [--queue]\n"
                 "       episode_ingest.py --self-test")
    return ingest(sg_connect(), paths[0], dry_run, queue)


# ---------------------------------------------------------------- self-test
class MockSG:
    """Offline stand-in for shotgun_api3.Shotgun. Knows two Assets, one
    pre-existing Sequence and one pre-existing Shot so both the create and
    the update (idempotent) paths get exercised. Records every mutating call
    so the test can assert exactly what would have hit the site."""

    def __init__(self, has_cut_order_field=True):
        self.has_cut_order_field = has_cut_order_field
        self.assets = {"A_CAR": 501, "A_CITY": 502}
        self.sequences = {"EP01_SQ010": 71}
        self.shots = {"EP01_SQ010_SH0010": 901}
        self.mutations = []
        self.next_id = 1000

    def find(self, entity_type, filters, fields, **kw):
        table = {"Asset": self.assets, "Sequence": self.sequences,
                 "Shot": self.shots}[entity_type]
        wanted = next(f[2] for f in filters if f[1] == "in")
        return [{"type": entity_type, "id": table[c], "code": c}
                for c in wanted if c in table]

    def create(self, entity_type, data):
        self.next_id += 1
        self.mutations.append(("create", entity_type, data))
        if entity_type == "Sequence":
            self.sequences[data["code"]] = self.next_id
        if entity_type == "Shot":
            self.shots[data["code"]] = self.next_id
        return {"type": entity_type, "id": self.next_id, "code": data.get("code")}

    def update(self, entity_type, entity_id, data):
        self.mutations.append(("update", entity_type, entity_id, data))
        return {"type": entity_type, "id": entity_id}

    def schema_field_read(self, entity_type, field_name):
        if entity_type == "Shot" and field_name == "sg_cut_order" \
                and self.has_cut_order_field:
            return {"sg_cut_order": {"data_type": {"value": "number"}}}
        raise RuntimeError("field %s not in schema" % field_name)


VALID_CSV = """sequence,shot,cut_order,duration_frames,prompt,kind,size,steps,cfg,seed,refs
EP01_SQ010,EP01_SQ010_SH0010,1,25,A red car drives through rain,video,832x480,10,5.0,1000,A_CAR;A_CITY
EP01_SQ010,EP01_SQ010_SH0020,2,1,Establishing skyline at dusk,still,832x480,10,5.0,2000,A_CITY
EP01_SQ020,EP01_SQ020_SH0010,3,49,Slow push in on the driver,video,,,,,
"""

# Three DISTINCT deliberate errors, one per row: bad frame count (Wan rule),
# bad size format, unresolvable asset ref. This is the canary: if the
# validator ever stops catching any of these, the self-test fails.
INVALID_CSV = """sequence,shot,cut_order,duration_frames,prompt,kind,size,steps,cfg,seed,refs
EP01_SQ010,EP01_SQ010_SH0030,4,26,Car skids to a halt,video,832x480,10,5.0,1000,A_CAR
EP01_SQ010,EP01_SQ010_SH0040,5,25,Neon sign flickers,video,832by480,10,5.0,1000,
EP01_SQ010,EP01_SQ010_SH0050,6,25,Hero looks up,video,832x480,10,5.0,1000,A_GHOST
"""


def self_test():
    import tempfile
    tmp = tempfile.mkdtemp(prefix="episode_ingest_selftest_")
    valid_path = os.path.join(tmp, "valid.csv")
    invalid_path = os.path.join(tmp, "invalid.csv")
    with open(valid_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(VALID_CSV)
    with open(invalid_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(INVALID_CSV)

    # --- canary: the invalid file MUST be refused and every error NAMED.
    sg = MockSG()
    rows, errs = read_rows(invalid_path)
    assert not errs, "structural errors unexpected: %r" % errs
    errors = validate_rows(rows, set(resolve_assets(sg, rows)))
    text = "\n".join(errors)
    assert len(errors) == 3, "expected exactly 3 errors, got %d:\n%s" % (len(errors), text)
    assert "duration_frames=26" in text and "divide by 4" in text, \
        "frames error not named:\n%s" % text
    assert "'832by480'" in text and "WxH" in text, "size error not named:\n%s" % text
    assert "'A_GHOST'" in text and "unresolved asset" in text, \
        "ref error not named:\n%s" % text
    rc = ingest(sg, invalid_path, dry_run=False, queue=True)
    assert rc == 1, "invalid file must be refused with rc 1, got %r" % rc
    assert sg.mutations == [], \
        "REFUSAL MUST NOT HALF-INGEST; mutating calls: %r" % sg.mutations
    print("self-test: invalid file refused, all 3 errors named, zero writes")

    # --- prove the canary is a real canary: hand the validator a lie (every
    # code resolvable) and the ref error disappears while the other two stay.
    lied = validate_rows(rows, {"A_CAR", "A_CITY", "A_GHOST"})
    assert len(lied) == 2 and "A_GHOST" not in "\n".join(lied), \
        "canary broken: ref check did not depend on resolution"
    print("self-test: ref check verified to depend on actual asset resolution")

    # --- cfg must be a positive FINITE number: nan compares false to
    # everything, so a naive "<= 0" check would let it through to ShotGrid.
    for bad in ("nan", "inf", "-1", "0"):
        bad_row = dict(rows[1], cfg=bad)          # row 1 is otherwise clean of cfg errors
        got = validate_rows([bad_row], {"A_CAR", "A_CITY"})
        assert any("cfg" in e for e in got), \
            "cfg=%r must be refused, errors were: %r" % (bad, got)
    ok_row = dict(rows[1], cfg="5.0", size="832x480")
    assert not any("cfg" in e for e in validate_rows([ok_row], {"A_CAR", "A_CITY"})), \
        "cfg=5.0 must pass"
    print("self-test: cfg refuses nan/inf/non-positive, accepts 5.0")

    # --- the valid file produces exactly the expected calls.
    sg = MockSG()
    rc = ingest(sg, valid_path, dry_run=False, queue=False)
    assert rc == 0, "valid file must ingest, got rc %r" % rc
    kinds = [(m[0], m[1]) for m in sg.mutations]
    # EP01_SQ010 pre-exists (kept), EP01_SQ020 is new. SH0010 pre-exists
    # (updated, idempotent), SH0020 and SH0030-equivalent rows are created.
    assert kinds == [("create", "Sequence"), ("update", "Shot"),
                     ("create", "Shot"), ("create", "Shot")], \
        "unexpected call sequence: %r" % kinds
    upd = next(m for m in sg.mutations if m[0] == "update")
    assert upd[2] == 901, "update must target the existing Shot id, got %r" % upd[2]
    assert "sg_gen_status" not in upd[3], \
        "without --queue sg_gen_status must not be touched"
    assert upd[3]["assets"] == [{"type": "Asset", "id": 501},
                                {"type": "Asset", "id": 502}], \
        "refs must link in CSV order (first = i2v start frame): %r" % upd[3]["assets"]
    assert upd[3]["sg_cut_order"] == 1, "cut order lost: %r" % upd[3]
    created_seq = next(m for m in sg.mutations if m[:2] == ("create", "Sequence"))
    assert created_seq[2]["code"] == "EP01_SQ020", \
        "only the missing Sequence may be created: %r" % created_seq[2]
    t2v_shot = next(m[2] for m in sg.mutations
                    if m[0] == "create" and m[1] == "Shot"
                    and m[2]["code"] == "EP01_SQ020_SH0010")
    assert "assets" not in t2v_shot, "empty refs must not write an assets link"
    assert t2v_shot["sg_sequence"]["id"] == sg.sequences["EP01_SQ020"], \
        "new shot must link the newly created Sequence"
    print("self-test: valid file -> 1 sequence created, 1 kept, 1 shot updated, "
          "2 created, refs in order, status untouched")

    # --- --queue flips status to queued; default (above) proved it does not.
    sg = MockSG()
    rc = ingest(sg, valid_path, dry_run=False, queue=True)
    assert rc == 0, "--queue run must ingest cleanly, got rc %r" % rc
    shot_mutations = [m for m in sg.mutations if m[1] == "Shot"]
    # Guard against a vacuous pass: an empty mutation list would sail through
    # the per-shot loop below without ever exercising the assertion.
    assert len(shot_mutations) == 3, \
        "--queue run must touch all 3 shots, saw %d: %r" \
        % (len(shot_mutations), shot_mutations)
    for m in shot_mutations:
        data = m[3] if m[0] == "update" else m[2]
        assert data.get("sg_gen_status") == "queued", \
            "--queue must set queued on every shot: %r" % data
    print("self-test: --queue sets sg_gen_status=queued on every shot")

    # --- no sg_cut_order field on the site: order lands in the description.
    sg = MockSG(has_cut_order_field=False)
    rc = ingest(sg, valid_path, dry_run=False, queue=False)
    assert rc == 0, "fallback run must ingest cleanly, got rc %r" % rc
    upd = next(m for m in sg.mutations if m[0] == "update")
    assert "sg_cut_order" not in upd[3] and "cut_order=1" in upd[3]["description"], \
        "cut order must fall back to description: %r" % upd[3]
    print("self-test: missing sg_cut_order field falls back to description")

    # --- dry-run writes nothing even on a valid file.
    sg = MockSG()
    rc = ingest(sg, valid_path, dry_run=True, queue=True)
    assert rc == 0 and sg.mutations == [], \
        "dry-run must not mutate: %r" % sg.mutations
    print("self-test: dry-run mutates nothing")

    print("self-test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
