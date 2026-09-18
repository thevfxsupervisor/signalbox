---
type: reference
title: How a shot is made, and how a change cascades through everything downstream
description: The executing path from an empty shot to a finished clip, what triggers each step, where a human decides, how an upstream change invalidates downstream work, and the four places the cascade does not reach.
tags: [genvideo, pipeline, cascade, shotgrid, watchers, operator]
timestamp: 2026-09-08
---

# How a shot is made, and how the cascade works

**Traced 2026-09-08 from the code that actually executes**, starting at `genvideo_service.py`
`cycle()`, which runs about once a minute. Where a docstring and the code disagreed, the code won.

Companion document: `PROMPT-COMPOSITION-BY-STAGE.md` covers which fields build each prompt.

**The one rule that explains the whole design:** every trigger is a piece of ShotGrid state. Nothing
is queued in memory, nothing is passed between steps in a variable. An operator changes a status or a
field, and on the next cycle a watcher notices. That is why the pipeline can be restarted at any
moment without losing its place, and it is also why a state nobody can see is a state nobody can act
on.

---

## Part A: the happy path

| # | Trigger | Watcher | Produces | Leaves behind |
|---|---|---|---|---|
| 1 | `Shot.sg_stage == board` | `cycle()` step 1b, `animatic.py --boards` | board stills | a board Version |
| 2 | **Panel Task status flips to `rdy`** | `watch_panel_composition()` | one or more panel Versions at `rev` | Task to `rev`, or `hld` if refused |
| 3 | **HUMAN.** Operator reviews the candidates | none | a decision | one Version set to an approved status |
| 4 | a panel Version reaches an approved status | `watch_panel_approvals()` | `Shot.sg_approved_panel` points at it | Panel Task closed to `apr` |
| 5 | approved panel AND **no live video** | `watch_stale_videos()` | `Shot.sg_gen_status = queued` | the only thing that ever auto-queues a first video |
| 6 | `Shot.sg_gen_status == queued` | `watch_panel_video_queue()`, running `video_from_panel.py` | a video Version at `rev`, full provenance | `Shot.sg_gen_status = review` |
| 7 | **HUMAN.** Operator reviews the video | none | a decision | an approved video Version |
| 8 | an approved video with no newer `_FIN_` sibling | `watch_finishing()` | the upres, the finished clip | a `_FIN_` Version |
| 9 | every cycle, last | `watch_review_housekeeping()` | losing alternates retired to `rjct` | a clean review queue |

**There are exactly two human decision points, steps 3 and 7.** Everything else is machinery. If a
shot is stuck, it is nearly always waiting at one of those two, or on a refusal at step 2 or 6.

---

## Part B: the cascade, what a change invalidates

### An asset design is approved or changes

Two mechanisms, and they do different things.

- `watch_panel_designs()` retires now-obsolete panel candidates at `rev` to `rjct`
  (`retire_superseded_panels()`). They are obsolete, not judged, and the log says so.
- `invalidate_stale_panels.invalidate()` does the un-approving: Version `apr` to `rev`,
  `Shot.sg_approved_panel` cleared, Panel Task back to `rdy`, which re-enters the happy path at
  step 2 and recomposes.

**Both read `build/out/panel_designs.json`**, a local JSON sidecar, cross-checked against live
`Asset.sg_approved_design`. See Part C.

### A panel is approved

Step 4 above. The Shot gets a pointer, and step 5 then queues the video.

### A panel is superseded

`retire_superseded_panels()` moves the stale `rev` candidates to `rjct`. **As of 2026-09-08 there is
one retired status.** `omt` was collapsed into `rjct`; the reason survives in the log and the
description rather than in a status nobody acted on.

### A video is stale against its panel

`watch_stale_videos()` requeues the shot. **Extended twice on 2026-09-08 and this is its current
rule:** a shot needs a live video that is **at least as new as its approved panel**. So

- no video at all, queue one (this was missing entirely, F468, and stranded 30 of 55 shots)
- a video awaiting review that is OLDER than the approved panel, replace it, because approving it
  would bless a frame built from a design nobody approves any more
- a video awaiting review that is NEWER than the panel, leave it alone, a human is deciding

A Version at `pf` or `fin` is protected and refuses regeneration on every path.

### A note is written and the Version set to `rrq`

1. `note_triage.sweep()` marks the note actionable. **Only notes at `opn` count**, and only when the
   Version is at `rrq`. That is the operator saying the review session is finished.
2. `prompt_revision.service_cycle()` sends the note plus the current prompt components to
   `claude -p`, which **rewrites the prompt and never declines**. The gate keeping was deleted on
   2026-09-08 at Geoff's instruction: *"we just want claude -p to help improve notes into prompts,
   not be a gate keeper."*
3. `apply_for_shot()` writes either `sg_action_beat` (panel work, and requeues the Panel Task to
   `rdy`) or `sg_gen_prompt` plus `sg_gen_status = queued` (video work).
4. That re-enters the happy path at step 2 or step 6.

The identical loop exists for Asset designs, which then feeds the design cascade above.

---

## Part C: where the cascade does NOT reach

Named because a gap nobody has written down is indistinguishable from one nobody has noticed.

### 1. The design cascade reads a local JSON file, not ShotGrid

`watch_panel_designs()` and `invalidate_stale_panels.invalidate()` both read
`build/out/panel_designs.json`. A shot missing from it is **invisible to the design cascade, with no
error and no log line.**

**Currently not tripped:** measured 2026-09-08, all 120 shots with a published panel are present.
The mechanism is unchanged, so it remains a single point of failure, and the sidecar records **one
character per shot**, which is why a blast radius counted from it read 51 shots when the raw
Shot-to-Asset links say 52.

### 2. Nothing detects a Version made by a superseded RECIPE

Every invalidation in the pipeline is a **timestamp comparison**. Nothing reads `sg_model`,
`sg_workflow_template` or a workflow hash and compares it to the current recipe. **Change the
resolution, the model or the LoRA, and every existing Version still looks current.** Geoff ranked
this class of problem highest. It is not built.

### 3. Video approval writes nothing

Panel approval writes `Shot.sg_approved_panel`. **There is no `watch_video_approvals()` and no
`Shot.sg_approved_video`.** Everything downstream re-queries Version status instead. It works, and it
means nothing cheaply answers "does this shot have an approved video", which is exactly the question
whose absence let 30 shots sit with a picture and no motion until someone opened one and asked (F472,
F468).

### 4. A healthy video pass is indistinguishable from a hang

`watch_panel_video_queue()` **captures** the subprocess and replays its output only after it exits,
and `--once` means one PASS, not one shot. With 33 shots queued the service log is silent for over an
hour while work proceeds perfectly. Measured: the log untouched for 1,976 seconds while twelve videos
published, one every 2 minutes 11 seconds (F477).

**During a video pass, liveness is frames landing in the ComfyUI output directory and GPU
utilisation, not the log growing.** The fix, streaming rather than capturing, is not yet made.

---

## The control surface, in one place

**To change what a shot LOOKS like:** edit `sg_action_beat`, or write a note and set the panel
Version to `rrq`.

**To change how it MOVES:** edit `sg_gen_prompt`, or write a note and set the video Version to `rrq`.

**To change the whole show's look:** edit the design Asset's `sg_prompt_fragment` and re-approve.
Expect it to invalidate every shot built on that asset.

**To stop a shot changing:** set its Version to `pf` or `fin`. Every regeneration path refuses; only
the upres, which cannot alter the picture, still runs.

**To make something happen now:** flip the Panel Task to `rdy` for a panel, or `Shot.sg_gen_status`
to `queued` for a video. These are the two manual triggers, and roadmap 0i proposes replacing them
with Task statuses so they are visible on a list view instead of hidden in a custom field.
