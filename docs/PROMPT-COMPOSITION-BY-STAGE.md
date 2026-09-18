---
type: reference
title: How a prompt is composed, stage by stage, and which fields an operator can actually change
description: Every field that reaches (or fails to reach) the model at each generation stage, with its fallback, plus the ShotGrid page layouts that would expose them.
tags: [genvideo, prompts, shotgrid, fields, operator, layouts]
timestamp: 2026-09-08
---

# How a prompt is composed, stage by stage

**Written 2026-09-08 for one purpose: so the ShotGrid page layouts can show an operator the fields
that actually change the picture, and stop showing the ones that do not.**

Scope: the three PRODUCTION stages. R&D publishers, wedge tools and the dead PILOT01 episode are
excluded deliberately, so absence from this document is not evidence that something does not exist.

**Read this first, because it is the finding that shapes every layout below (F471).**

> **A field is an INPUT, a RECORD, or a LIE, and ShotGrid does not distinguish them by sight.**

An input reaches the model. A record is written afterwards as provenance. A lie is a field that looks
like an input, is read by the code, and is then discarded. All three render identically on a page.
**Showing a record or a lie as editable teaches an operator that the pipeline ignores them.**

Every table below marks each field **INPUT**, **RECORD** or **DISCARDED**.

---

## Stage 1: ASSET DESIGN (the character and set anchors)

What runs: `tools/anima_anchor.py`, invoked by the service as `anima_anchor.py --only CODE`
(`tools/genvideo_service.py:1863`). `tools/character_sheets.py` builds multi-view sheets and is
**not wired into the service**; it is run by hand.

### The anchor itself

| Entity | Field | Kind | Contributes | Order | If empty |
|---|---|---|---|---|---|
| Asset | `sg_prompt_fragment` | **INPUT** | the entire subject description, verbatim | 2 | **REFUSES.** `compose()` raises; there is deliberately no fallback |
| (code) | `QUALITY_PREFIX` | hardcoded | `masterpiece, best quality, score_7, safe,` | 1 | n/a, `anima_anchor.py:143` |
| (code) | `NEG_BASE` | hardcoded | the whole negative | negative | n/a, and **inert** at cfg 1.0 |

**Shape:** `QUALITY_PREFIX + Asset.sg_prompt_fragment`

**Verified** byte for byte against Version 69647, `SHOW_CHAR_PILOTCHARB_ANCHOR_turbo10_s52008`.

### The multi-view sheet (hand-run, not in the service)

| Entity | Field | Kind | Contributes | If empty |
|---|---|---|---|---|
| Asset | `sg_design_attributes` | **INPUT** | appends `name: value; ...` plus a string-colour sentence | silently skipped |
| Asset | `sg_negative_fragment` | **INPUT** (inert) | joined into the negative | omitted, and inert regardless |
| Episode via `Asset.episodes`, EARLIEST by code | `sg_style_prefix`, `sg_style_suffix`, `sg_style_negative` | **INPUT** | show style, set language stripped | no link or blank gives no style at all, never blocks |
| (code) | `VIEWS`, `SHEET_STAGING`, `PORTRAIT_STAGING`, `SHEET_NEG` | hardcoded | pose and isolation staging | always present |

**An Asset has no Sequence and no Shot**, which is why style resolves through `Asset.episodes` to the
earliest Episode. That was Geoff's ruling on 2026-09-08, replacing a version that read whichever
Sequence happened to sort first.

---

## Stage 2: SHOT PANEL (the still, and the busiest path in the project)

What runs: `tools/panel_compose.py` into `tools/qwen_compose.py`.

| Entity | Field | Kind | Contributes | Order | If empty |
|---|---|---|---|---|---|
| Shot | `sg_action_beat` | **INPUT** (preferred) | the action text | 2 | falls back to `sg_script_beat` |
| Shot | `sg_script_beat` | **INPUT** (fallback) | the action text | 2 | **REFUSES** if both are blank |
| Asset (char) | `sg_approved_design` to its Version `sg_path_to_movie` | **INPUT** (a link, not text) | the character reference image | image | **REFUSES**: no approved design, or file missing on disk |
| Asset (set) | same, the STYLE-role asset | **INPUT** | the set reference image | image | **REFUSES** |
| Shot | `sg_shot_size` | **INPUT** (indirect) | selects a hardcoded framing sentence from `FRAMING` | 4 | **empty string**, never a guessed default |
| Shot | `sg_shot_size` = `wide` or `full` only | **INPUT** (indirect) | adds a ground-contact sentence | 5 | omitted for every other size |
| Asset | `sg_instance_count` | pipeline-set | `There are exactly N copies...` | 3 | omitted when unset or 1 |
| Sequence | `sg_style_prefix` | **DISCARDED** | read into `inputs`, used only for provenance | never sent | n/a |
| Shot | `sg_camera` | **DISCARDED** | provenance only | never sent | n/a |
| Shot | `sg_gen_size_wxh` | **DISCARDED** | provenance only | never sent | n/a |
| (code) | the `Place the character...` opener | hardcoded | clause 1 | 1 | always |
| (code) | `NO_BURNT_IN_TEXT_CLAUSE` | hardcoded | do not render text | 6 | always |
| (code) | `SLOT_SIDE_CLAUSE` | hardcoded | screen direction, and it says `the man in Picture 1` | 3b | two-character branch only |
| (code) | `PRESERVE_SUFFIX` family | hardcoded | the preserve veto, appended by `qwen_edit()` | last | always |
| (code) | `NEGATIVE` | hardcoded | the whole negative | negative | always sent, **inert** at cfg 1.0 |

**Shape, single character:**

    "Place the character..." + action_text + NO_BURNT_IN_TEXT_CLAUSE
      + [framing] + [contact] + PRESERVE_SUFFIX

**Verified** against Version 69765, `SHOW01_A_0550_PNL_panel_v016`: the sent text matches exactly,
and `style_text` and `camera_text` are absent from it.

**The two-assembler hazard, recorded because this shape has bitten three times:**
`_build_instruction()` at `panel_compose.py:1120` builds a near-identical string used ONLY for the
description and provenance, while the text actually sent is assembled separately in
`compose_with_retry()`. They agree today. They are two independently maintained copies.

---

## Stage 3: SHOT VIDEO (motion, i2v from the approved panel)

What runs: `tools/video_from_panel.py`.

| Entity | Field | Kind | Contributes | Order | If empty |
|---|---|---|---|---|---|
| Shot | `sg_approved_panel` | **INPUT** (the gate) | selects the anchor Version and source image | first | **REFUSED**, `sg_gen_status` set to `refused` |
| Shot | `sg_gen_prompt` | **INPUT** (preferred) | the motion text | 1 | falls back to `sg_script_beat` |
| Shot | `sg_script_beat` | **INPUT** (fallback) | the motion text | 1 | **REFUSES** if both blank |
| Shot | `sg_gen_negative_prompt` | **INPUT** (inert) | the negative | negative | hardcoded `blurry, distorted, watermark, text, extra limbs, static`, and inert either way |
| Shot | `sg_gen_seed` | **INPUT** | locks the seed across regenerations | n/a | hardcoded `1000` |
| Shot | `sg_camera`, `sg_shot_size` | **DISCARDED** | reach only the provenance description, as the literal `unspecified` when unset | never sent | n/a |
| (code) | `. Preserve all appearance and environment from source image.` | hardcoded | appended unconditionally | last | **always**, `video_from_panel.py:359` |

**Shape:**

    strip_dialogue(sg_gen_prompt or sg_script_beat)
      + ". Preserve all appearance and environment from source image."

**That appended sentence is the most consequential clause in the motion stage, and there is no field
for it.**

---

## Every hardcoded string that reaches a model and is not a ShotGrid field

This list IS the 0d work order. Nothing here can be exposed on a page today.

- `tools/anima_anchor.py:143` `QUALITY_PREFIX`, `:148` `NEG_BASE`
- `tools/character_sheets.py:129` `VIEWS`, `:166` `SHEET_STAGING`, `:207` `PORTRAIT_STAGING`, `:235` `SHEET_NEG`, `:255` `SET_STAGING`, `:259` `SET_NEG`
- `tools/panel_compose.py:938` `FRAMING`, `:973` `FRAMING_PLURAL`, `:1055` `CONTACT_CLAUSE`, `:1090` `CONTACT_CLAUSE_PLURAL`, `:1218` `SLOT_SIDE_CLAUSE`
- `tools/dialogue_guard.py:281` `NO_BURNT_IN_TEXT_CLAUSE`
- `tools/qwen_compose.py:110` `PRESERVE_SUFFIX`, `:133` `_MULTI`, `:143` `NEGATIVE`, `:156` `_TWO_CHAR_NO_SET`, `:170` `_WITH_SET`, `:340` and `:383` the two openers actually sent
- `tools/video_from_panel.py:359` the preserve sentence, `:360` the negative default, `:97` to `:136` the recipe constants (models, LoRA, steps, resolution)

**The negative prompt is inert at every stage.** All three run cfg 1.0, so it is composed, sent,
recorded, and changes nothing.

---

## THE PAGE LAYOUTS

What to put in front of an operator, and what to keep off the page.

### Shot detail page

**Editable, these change the picture:**

1. `sg_action_beat`, the panel action. Falls back to `sg_script_beat`.
2. `sg_gen_prompt`, the video motion. Falls back to `sg_script_beat`.
3. `sg_script_beat`, the shared fallback for both.
4. `sg_shot_size`. It selects a framing sentence from a fixed table, so **make it a LIST field**: a
   value outside the known set silently produces NO framing clause at all.
5. `sg_gen_seed`.
6. `assets`, the native link deciding which characters and which set are composed.

**Read-only, a record of what happened:** `sg_approved_panel`, `sg_gen_status`, `sg_gen_log`,
`sg_proposal_status`, `sg_prompt_proposal`.

**Keep OFF the page, or label them plainly as not used:** `sg_camera` and `sg_gen_size_wxh` are
discarded on every path. `sg_gen_negative_prompt` is inert.

### Version detail page

**All read-only. Every field here is provenance:** `sg_prompt_final__as_sent_`,
`sg_negative_final__as_sent_`, `sg_component__character`, `sg_component__set`,
`sg_component__action`, `sg_component__camera`, `sg_component__style`, `sg_workflow_hash__sha256_`,
`sg_model`, `sg_batch_id`, `sg_stage`.

The one editable thing on a Version is its **status**, and that is the entire operator vocabulary:
approve it, or set `rrq` to request a revision.

### Asset detail page

**Editable:** `sg_prompt_fragment` (required, refuses if blank), `sg_design_attributes`,
`sg_negative_fragment` (inert), `episodes` (the link that resolves style).

**Read-only:** `sg_approved_design`, `sg_stage`, `sg_gen_status`.

### Episode detail page

**Editable:** `sg_style_prefix`, `sg_style_suffix`, `sg_style_negative`. Created 2026-09-08, empty on
SHOW01. **These reach asset sheets only.**

### Sequence detail page

**Editable:** `sg_style_prefix`, `sg_style_suffix`, `sg_style_negative`.

**Worth putting in the field description itself:** these reach the PROMPT PROPOSER and are
DISCARDED by the panel compositor. Editing them changes how a note is turned into a new beat. It
does not directly change a render.

---

## What this document does not do, said plainly

It is **hand-written from a code trace on 2026-09-08 and it will go stale.** The right end state is a
generator that reads the real constants and templates and emits these tables, run in preflight so it
cannot drift. That is not built. Until it is, treat the line numbers as of that date and re-verify
anything surprising before acting on it.
