---
type: reference
title: ShotGrid page layouts: the spec, and why you have to apply it
description: A script key cannot create ShotGrid pages. Verified against the live site:
tags: [page, layouts, genvideo]
timestamp: 2026-09-06
---
# ShotGrid page layouts: the spec, and why you have to apply it

## The constraint, established by testing rather than assumed

**A script key cannot create ShotGrid pages.** Verified against the live site:

    sg.create("Page", {...page_type: "canvas"})
      -> API create() Page.page_type is read only

    sg.create("Page", {...})                       # without page_type
      -> CRUD ERROR #6: HumanUser(#1111) expected, got ApiUser(#2222)

Page creation is gated on a **HumanUser**. The `genvideo` script identity is an ApiUser, so no
amount of API work will produce a page. `PageSetting.settings_json` is readable but belongs to a
page that must already exist, and is per-user.

This is why the interface work stalled, and it is the honest answer to "most pages need a first
version built": the pages need your account, not more code. Everything the pages *display* is
already in place - all the fields below exist and are populated. Adding a column is a few clicks
once; this document is so it is the right column.

Geoff, you said "I can help with the interface layout later". This is that, specified.

---

## What is already done for you

- **Episode `PILOT01` exists** with the acts, all 121 shots and both episode cuts linked, so the
  Episode -> Sequence -> Shot hierarchy is complete and navigable.
- The bible, the script and the subtitle track are **attached to the Episode**, so the site is
  self-contained.
- Every field named below exists and holds real data. No column here will be empty.

---

## Page 1: EPISODES  (entity: Episode, type: list/canvas)

The page a producer opens first. It does not exist yet because Episode is new.

| Column | Field | Why |
|---|---|---|
| Episode | `code` | |
| Logline | `sg_logline` | the show in one line |
| Format | `sg_format` | medium and tone, so nobody re-litigates it |
| Target | `sg_target_seconds` | intended runtime |
| Runtime | `sg_runtime_seconds` | **measured** from the cut |
| Runtime note | `sg_runtime_note` | the delta, in words |
| Status | `sg_status_list` | |

**Group by:** none. **Sort:** `code`.

## Page 2: SHOT BOARD  (entity: Shot, type: thumbnail grid)

This is the Leonardo/Krea-style page. A wall of images with the prompt under each, which is how
every popular generative interface presents work, and it is the correct default for a show whose
unit of work IS an image.

**Set the page to thumbnail/card view, largest thumbnail size.**

| Field | Role on the card |
|---|---|
| `image` (thumbnail) | the board or latest take |
| `code` | shot name |
| `sg_gen_prompt` | the prompt, under the image |
| `sg_gen_status` | queued / generating / review / revise / done |
| `sg_stage` | board / keyframe / video |
| `sg_cut_order` | |

**Sort:** `sg_cut_order` ascending. **Group by:** `sg_sequence`.
**Filter preset "Needs my eyes":** `sg_gen_status is review`.

## Page 3: REVIEW QUEUE  (entity: Shot, type: list)

The triage page. The point of this one is that it separates work that needs the GPU from work that
does not, which is the whole economic argument of the pipeline.

| Column | Field |
|---|---|
| Thumbnail | `image` |
| Shot | `code` |
| Verdict | `sg_review_verdict` |
| Note class | `sg_note_class` |
| Caption | `sg_caption_text` |
| Script beat | `sg_script_beat` |
| Gen log | `sg_gen_log` |

**Group by:** `sg_stage`. **Sort:** `sg_cut_order`.

**DO NOT build a `sg_note_class` view.** That field is no longer stamped by anything (the note
classifier was deleted on 2026-09-08, see PRODUCTION-PROCESS.md), so any preset filtering on it
returns an empty list forever, which reads as "no work" rather than as a broken filter.

**The preset worth making instead:** Versions at `rev`, grouped by `sg_stage`. That is the real
review queue, and it is what an operator actually works through.

## Page 4: SHOT DETAIL  (entity: Shot, type: detail)

Field groups, in this order. The order matters: what the shot IS, then how it was MADE, then what
was SAID about it.

**Story** - `sg_script_beat`, `sg_caption_text`, `sg_background_description`
**Grammar** - `sg_camera`, `sg_shot_size`, `sg_time_of_day`, `sg_cut_order`, `sg_gen_frames`
**Prompt** - `sg_gen_prompt`, `sg_gen_negative_prompt`, `sg_prompt_final__composed_`
**Generation** - `sg_gen_status`, `sg_stage`, `sg_gen_seed`, `sg_gen_steps`, `sg_gen_cfg`,
`sg_gen_size_wxh`, `sg_gen_worker`
**Review** - `sg_review_verdict`, `sg_note_class`, `sg_gen_log`
**Links** - `assets`, `sg_sequence`, `sg_episode`

## Page 5: VERSIONS / TAKES  (entity: Version, type: list)

The provenance page. Its job is to make "why does this take look like that" answerable without
archaeology.

| Column | Field |
|---|---|
| Thumbnail | `image` |
| Version | `code` |
| Shot | `entity` |
| Stage | `sg_stage` |
| Status | `sg_status_list` |
| Prompt as sent | `sg_prompt_final__as_sent_` |
| Seed | `sg_gen_seed` |
| Steps | `sg_gen_steps` |
| CFG | `sg_gen_cfg` |
| Size | `sg_gen_size_wxh` |
| Model | `sg_model` |
| Seconds | `sg_gen_seconds` |
| Refs | `sg_reference_assets` |

**Group by:** `sg_stage`. **Sort:** `created_at` descending.

## Page 6: SEQUENCE / ACT  (entity: Sequence, type: list)

The page where the look of a whole act is one edit. Changing `sg_style_prefix` here restyles every
subsequent generation in that act - that is a real, tested behaviour, not a plan.

| Column | Field |
|---|---|
| Act | `code`, `sg_act` |
| Stage | `sg_stage` |
| Gen status | `sg_gen_status` |
| Style prefix | `sg_style_prefix` |
| Style suffix | `sg_style_suffix` |
| Style negative | `sg_style_negative` |
| Target | `sg_target_seconds` |
| Log | `sg_gen_log` |

## Page 7: ASSET LIBRARY  (entity: Asset, type: thumbnail grid)

| Field | Role |
|---|---|
| `image` | the design |
| `code` | |
| `sg_ref_role` | character / style / prop |
| `sg_prompt_fragment` | the locked text this asset contributes to every prompt |
| `sg_fragment_order` | where it lands in the composed prompt |
| `sg_status_list` | approved design vs agent stand-in |

**Group by:** `sg_ref_role`.

---

## Two filter presets worth making before any others

1. **"Waiting on me"** on the Review Queue: `sg_status_list is rev`, grouped by `sg_stage`. The
   real queue. (The old recommendation here filtered on `sg_note_class`, a field nothing stamps any
   more; it would return nothing.)
2. **"Boards awaiting approval"** on the Shot Board: `sg_stage is board` and
   `sg_gen_status is review`. Approving a board here is what lets the video start from it -
   the worker prefers an approved board as its i2v start frame, so this page gates identity
   consistency for the whole show.

## What is still genuinely missing from the interface

- **ActionMenuItems** ("Queue this shot", "Apply post fix") need a configured action URL and a
  reachable endpoint. This box has no public HTTPS, so the buttons would point nowhere.
  Until then the interface verb is editing a field, which does work: flipping `sg_gen_status`
  to `queued` generates the shot within one service cycle.
- **Annotation ingestion.** ShotGrid stores frame-accurate annotation images on Notes; nothing
  reads them yet.
