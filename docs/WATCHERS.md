---
type: reference
title: The watchers: what the pipeline does on its own
description: Audience: an operator driving this pipeline from ShotGrid, and anyone redeploying it on another machine.
tags: [watchers, genvideo]
timestamp: 2026-09-06
---
# The watchers: what the pipeline does on its own

**Audience:** an operator driving this pipeline from ShotGrid, and anyone
redeploying it on another machine.

There is exactly **one** background process, `tools/genvideo_service.py`. It
polls ShotGrid every 45 seconds and runs twelve watchers in a fixed order. Each
watcher reads ShotGrid state, decides whether its trigger condition is true, and
if so acts and writes back.

**ShotGrid state is the only trigger** (invariant 1). There is no queue, no
message bus, and no hidden job file. If you cannot cause something by changing a
field in ShotGrid, the pipeline cannot do it, and if a step only happens when a
human runs a script, it is *not* part of the pipeline, however well documented it
is. That distinction is not academic: `sg_review_housekeeping.py` existed and was
self-tested for a day while every description of the review loop claimed
"approving rejects the alternates". Nobody had wired it in. An operator watched a
panel approval for thirty minutes and correctly concluded it was broken.

---

## The twelve watchers, in cycle order

| # | Watcher | Trigger (a ShotGrid state) | What it does |
|---|---|---|---|
| 1 | `watch_panel_video_queue` | `Shot.sg_gen_status = queued` **and** `sg_approved_panel` set | Wan 2.2 A14B i2v from the approved panel. Queued with **no** approved panel is refused, loudly |
| 2 | `watch_beats` | `Shot.sg_script_beat` edited | Re-derives downstream beat data; marks affected panels stale |
| 3 | `watch_note_triage` | any Note on a Shot or its Versions | Classifies the shot's **actionable** notes into one verdict → `Shot.sg_note_class` |
| 4 | `watch_prompt_proposals` | `sg_note_class = prompt-addressable` | `claude -p` drafts a prompt revision → `Shot.sg_prompt_proposal` |
| 5 | `watch_animatic_requests` | `Sequence.sg_gen_status = animatic_requested` | Cuts an animatic (on request only, D8) |
| 6 | `watch_panel_approvals` | Version `sg_stage=panel` at an approved status | Writes `Shot.sg_approved_panel`. **The only place that field is ever written** |
| 7 | `watch_panel_designs` | an Asset's `sg_approved_design` changed under a composed panel | Flags the panel stale and posts a Note saying why |
| 8 | `watch_design_approvals` | approved Version whose entity is an **Asset** | Sets `Asset.sg_approved_design` + `sg_stage=approved` |
| 9 | `watch_design_revisions` | Asset design Version at `rrq` + a Note | Drafts, and for SHOW01 applies, a design revision → `Asset.sg_gen_status=queued` |
| 10 | `watch_design_renders` | `Asset.sg_gen_status = queued` | Renders new design candidates at `rev` |
| 11 | `watch_panel_composition` | a Shot's **Panel Task** flipped to `rdy` | Runs `panel_compose.py`: renders a seed wedge, publishes every candidate at `rev`, groups them in a `REVIEW_<shot>_v<NNN>` Playlist, sets the Task to `rev` (or `hld` if refused) |
| 12 | `watch_review_housekeeping` | an approved Version exists on an entity+stage | Moves the losing siblings out of the review queue |

Watcher 12 runs **last** on purpose: an approval processed by watcher 6 earlier in
the same cycle is tidied in that same pass, not 45 seconds later.

---

## The status vocabulary

These are this project's own display names, read from the schema, not assumed.
`rev` is **not** "revision requested"; `rrq` is.

| Code | Display name | Means |
|---|---|---|
| `rev` | Pending Wangle Review | **Waiting for you.** This is the review queue |
| `rrq` | Revision Requested | A note here needs acting on |
| `apr` | Approved | Chosen |
| `rjct` | Rejected | Considered, and another was chosen |
| `omt` | Omit | Never in the running (a wedge cell) |
| `pf` | Pending Client Feedback | Past the operator, awaiting the client |

**`rjct` and `omt` are deliberately not the same thing.** A page filtered "show me
what was rejected" should show work someone looked at and turned down, not sixty
wedge cells from a technical experiment nobody judged. The distinction costs
nothing to keep and cannot be recovered once collapsed.

---

## Notes: requests versus records

> "notes on rejected don't require acting, they are optional records of
> something.", Geoff, 2026-09-04

A Shot accumulates notes forever: real review feedback, but also the pipeline's
own bookkeeping ("PROVISIONAL D14 approval", "Approved design changed for…").
Triage acts on a note **only while a Version it hangs off is still awaiting a
decision** (`rev` or `rrq`). Once that Version is approved, rejected or omitted,
its notes are the record of *why*, worth reading, never worth acting on.

A note linked to the Shot and to no Version at all stays actionable: nothing has
closed it.

Measured effect on PILOT01: 139 notes, **15 actionable, 124 record-only**.

**Where notes live, and why both sources are read.** `Shot.open_notes` is
ShotGrid's own rollup and is the cheap one, but it holds only notes linked to the
*Shot*. A note typed against a Version in the review player can link to the
Version alone, measured, note 51148 on `PILOT01_A_0090` does exactly that and is
absent from `open_notes`. So triage reads both and dedupes by Note id.

**One verdict per Shot, not one per note.** A shot whose notes classify
differently gets a single write, and the **most expensive** class wins, the same
safety rule `classify()` applies within one note. Under-treating silently leaves
the note unaddressed and the reviewer must catch it a second time; over-treating
only costs GPU time, which is free locally.

---

## Single instance

Two services double-process every trigger: two renders for one queued shot, two
Versions racing for the same version number, and a triage that can never
converge. From ShotGrid it is invisible, it just looks like the pipeline
behaving erratically.

The guard is an **exclusive OS lock** at a fixed absolute path
(`C:\genvideo\ops\service.lock`), taken before the service touches ShotGrid and
released by the kernel when the process dies. A second instance refuses loudly
and exits.

Two things it must not be, both learned the hard way:

- **Not a check-then-act.** `ops/run_service.ps1` looks for a running service and
  then starts one. Two launches inside that window both look, both see nothing,
  and both start, measured, two pids created in the same second. Only an atomic
  operation closes that.
- **Not a path derived from `__file__`.** That put the lock inside the versioned
  release directory, so an old instance and a new one held two *different* locks
  and never contended. A mutual-exclusion primitive whose identity moves with the
  code it guards excludes nothing.

**Counting instances:** this venv is uv-created, and its `Scripts/python.exe`
runs the real interpreter as a **child process with the same command line**. One
service therefore appears in the process table twice. `deploy.py` counts launch
*chains*, a match whose parent is also a match is a continuation, not a second
service.

---

## Driving it as the operator

| To do this | Change this in ShotGrid |
|---|---|
| Compose a panel | Shot's **Panel Task** → `rdy` |
| Approve a panel | Version → `apr`. Watcher 6 links it; watcher 12 rejects the alternates |
| Ask for a revision | Add a **Note** on the Version (or set it to `rrq`) |
| Accept a prompt proposal | `Shot.sg_proposal_status` → accepted |
| Generate video | `Shot.sg_gen_status` → `queued`, with an approved panel |
| Approve a design | Approve a Version whose entity is an **Asset** |
| Assemble a sequence | `Sequence.sg_gen_status` → `queued` |

**Approving a second panel on the same shot silently replaces the first** as
`sg_approved_panel`, watcher 6 takes the newest by `created_at`.

**Nothing self-approves** (invariant 7). `panel_compose.py` publishes at `rev`
and never writes `sg_approved_panel`; the QC gate grades and refuses, it does not
decide. Every approval in this pipeline is a person's.

---

## Where to look when it seems stuck

1. **`C:\genvideo\logs\service-<date>.log`**, every cycle, every watcher.
2. **Is exactly one service running?** `python tools/deploy.py` reports it, and
   refuses to call a deploy successful with more than one.
3. **Did the trigger field actually change?** Watchers act on state, not
   intention. A Task left at `wtg` composes nothing.
4. **Is the work *published*?** "Created" is not "viewable", check the Version
   has `sg_uploaded_movie`, not just a path.
