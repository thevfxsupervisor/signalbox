---
type: reference
title: Models and prompting, stage by stage
description: Every value here is read from the code, not from memory. The authority is the module named in each row; if this file and the module disagree, the module wins and this file is stale.
tags: [models, prompting, genvideo]
timestamp: 2026-09-06
---
# Models and prompting, stage by stage

Every value here is read from the code, not from memory. The authority is the
module named in each row; if this file and the module disagree, the module wins
and this file is stale.

## The stages

| # | Stage | Model | Kind | Module |
|---|---|---|---|---|
| 1 | Script to beats | `claude -p` | text | `script_to_beats.py` |
| 2 | Asset references (characters, sets) | **Anima** | text to image | `anima_anchor.py` |
| 3 | Panels | **Qwen-Image-Edit-2509** | image edit, 2 to 3 refs | `qwen_compose.py`, `regional_compose.py` |
| 4 | Beat split (two-handers) | `claude -p` | text | `beat_split.py` |
| 5 | Panel revision from a note | `claude -p` | text | `prompt_revision.py` |
| 6 | Note triage | none | deterministic phrases | `note_triage.py` |
| 7 | Shot video | **Wan 2.2 I2V A14B** | image to video | `video_from_panel.py` |
| 8 | Two-shot video (first/last frame) | **Wan 2.2 FMLF** | image to video | `video_from_panel_pair.py` |
| 9 | Animatic | none | ffmpeg only | `animatic.py` |
| 10 | Attribute QC | `claude -p` vision | **OFF as of 2026-09-05** | `tests/attribute_check.py` |

Built but not yet used on SHOW01: audio bed, captions, episode assembly,
finishing/upres, delivery (`audio_bed.py`, `captions_gen.py`,
`episode_assemble.py`, `finishing.py`, `delivery.py`).

---

## 2. Asset references, Anima

```
checkpoint  anima-base-v1.0.safetensors
1280 x 704, sampler er_sde / simple, shift 3.0
```

Two recipes, and which one is used is a real decision:

| recipe | LoRA | steps | cfg |
|---|---|---|---|
| `base40` | none | 40 | 6.0 |
| `turbo10` | `anima-turbo-lora-v0.2.safetensors` @ 1.0 | 10 | **1.0** |

**`turbo10` runs at cfg 1.0, which makes the negative prompt inert** - proven
pixel-identical, delta 0.000. Exclusions have to be written into the positive
prompt as what IS in frame. Most approved SHOW01 anchors are `turbo10`.

Prompting is plain text to image: a description of the character or set, with
no reference image. The approved output becomes the anchor every later stage
composites from, so an error here propagates to every panel using that asset.

## 3. Panels, Qwen-Image-Edit-2509

```
unet   Qwen-Image-Edit-2509-Q3_K_M.gguf
lora   qwen_image_edit_2509_lightning_4steps_v1.safetensors @ 1.0
clip   qwen_2.5_vl_7b_fp8_scaled.safetensors
vae    qwen_image_vae.safetensors
steps 4, cfg 1.0, shift 3.0 (AuraFlow's, NOT Wan's 8.0)
```

**cfg 1.0 again, so negatives are inert here too.** Both renderers in this
pipeline are cfg 1.0 on their production recipe.

It is an EDIT model, not text-to-image. It receives reference IMAGES and a
sentence:

- `image1` the character's approved design
- `image2` the set's approved design

**The SLOT NUMBER carries no meaning to the model** (a peer engineer, F154, confirmed from live
ComfyUI source): image1/image2/image3 are functionally symmetric, each independently
resized, so a wide plate and a tight crop carry comparable reference weight. There is
no subject/scene/style role built into the slots.

**So the PROMPT is what disambiguates them, and that is not decoration.** "the character
from the character reference image into the room shown in the set reference image" is
doing the work the slot cannot. Two consequences for anyone changing this: swapping the
order is harmless, and adding a third image WITHOUT naming it in the prompt gives the
model an unlabelled reference rather than a new role.
- the prompt: *"Place the character from the character reference image into the
  room shown in the set reference image. <action>. <no-text clause>. <framing
  clause>."*

The action text comes from `Shot.sg_action_beat` (falling back to
`sg_script_beat`), and the framing clause is derived from `Shot.sg_shot_size` -
plain English about what the frame contains ("head and shoulders fill most of
the frame"), not film shorthand.

### Two characters: regional prompting

`regional_compose.py`. The frame is split into **two vertical bands at exactly
50%**:

```
bands(2) = [(0.0, 0.5), (0.5, 0.5)]
```

Each band gets its own text encoder node, masked to its half, then combined:

| node | character ref | set ref |
|---|---|---|
| `te0` | PilotCharA | the set |
| `te1` | PilotCharB | the set |

**This is why halves of different characters merge down the centre line**, and
**why two beds appear**: the set reference goes to BOTH bands, so the room is
composed twice, once per half. Neither is in the script - checked, zero shots
mention two beds and the set design says `key_props: double bed`, singular.

Status 2026-09-05: **not production-usable.** Measured across shots, not one:
`_0080` about 2 of 8 clean, `_0390` 0 of 8, `_0400` 0 of 8. Text-level fixes
(band regex, beat split, shared staging) each addressed a real defect and none
fixed it, which points at the band geometry rather than the prompting.

## 7. Shot video, Wan 2.2 I2V A14B

```
high  Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf
low   Wan2.2-I2V-A14B-LowNoise-Q4_K_S.gguf
lora  wan22_lightning_i2v_a14b_high/low.safetensors @ 1.0
steps 4, cfg 1.0, shift 8.0, switch_step 2, 81 frames
```

Two-model schedule: steps 0-2 on the high-noise model, 2-4 on the low. Input is
the **approved panel** plus the shot's action text. Output is bridged from 16fps
to 24fps by ffmpeg retime (RIFE is not installed here), so a 81-frame render
becomes 73 to 145 frames depending on the shot's `sg_gen_frames`.

The watcher **refuses** to render without `Shot.sg_approved_panel` pointing at a
genuinely approved Version.

## 10. Attribute QC, currently OFF

A `claude -p` VISION call per character per seed, comparing the rendered panel
against the character Asset's `sg_design_attributes` text.

Switched off 2026-09-05 (`panel_compose.ATTRIBUTE_QC_ENABLED = False`) on the
operator's decision. Measured pass rates: `distinguishing_features` 51%,
`wardrobe` 56%, `build` 75%, `hair` 77%; all four must pass, so a
single-character panel passed 38% and a two-character one 0.1%. Most failures
were not errors - an attribute list encoding a POSTURE the beat legitimately
overrides, or an attribute cropped out of frame, which scores FAIL because the
checker has only pass/true and pass/false.

Staged for re-inclusion, not deleted: the tool, its self-tests and its
repeat-and-vote logic are untouched and still run in preflight.

---

## The one rule that keeps being relearned

**Read what was actually sent before blaming the model.** Five of five "model
limitations" on the first show turned out to be our own prompt. Two more this
week: a shot rendered two copies of one character because its beat said "the two
of them", and every regional band was told the frame held "two standing figures
facing each other" because that phrase was in the shared staging.

`Version.sg_prompt_final__as_sent_` records the prompt for single-character
panels. **It is wrong for regional panels** - it records the single-character
wrapper, not the band prompts. That is a known gap.
