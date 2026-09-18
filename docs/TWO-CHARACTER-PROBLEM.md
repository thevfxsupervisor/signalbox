---
type: reference
title: The two-character problem
description: 32 of 55 SHOW01 shots put two characters in frame, and none of them composes correctly.
tags: [two, character, problem, genvideo]
timestamp: 2026-09-06
---
# The two-character problem

**32 of 55 SHOW01 shots put two characters in frame, and none of them composes
correctly.** This file records what the problem is, what was measured here, and
what the field knows, so the next person does not repeat five experiments to
arrive where we are.

Researched 2026-09-05 by four parallel agents. Claims below are marked
**verified** (read from a source), **anecdote** (a practitioner report), or
**inference**. Where the evidence is thin it says so.

---

## It is not a Qwen problem

**Verified.** The failure has names in the literature, and they distinguish
sub-cases:

| Term | Meaning | Source |
|---|---|---|
| semantic leakage | the umbrella term | Bounded Attention, ECCV 2024 |
| catastrophic neglect | a subject missing entirely | same |
| incorrect attribute binding | the right features on the wrong subject | same |
| **subject fusion** | **two subjects merged into one** | same |
| identity mixing / collapse | the same, in the personalisation literature | MuDI, NeurIPS 2024 |

**It is worst for same-class subjects** - two humans, two dogs. That is exactly
our case, and it is the case the field finds hardest.

**It is open, not solved.** A 2026 benchmark scores face-identity similarity at
two subjects: best baseline about 42 of 100, most models 3 to 23. Everything
collapses by four or five subjects.

**Mechanism, partial consensus.** Semantically similar subjects produce similar
query vectors, so cross-attention responses are similar and token semantics leak
between them; self-attention then copies features directly between
semantically similar regions. A second body of work locates the root cause
upstream in the text encoder instead. Both are probably contributing
(**inference**); neither is disputed as a factor.

**The pattern across every method that works: spatial separation.** Masks,
layouts, regions. **Nothing solves it by prompt alone.** That single sentence is
the most useful thing in this file.

## What Qwen-Image-Edit-2509 claims

**Verified.** The 2509 model card says it was trained by image concatenation to
enable multi-image editing, explicitly lists **person + person**, and recommends
1 to 3 input images. Its own example composes two subjects.

**And 2511 advertises "high-fidelity fusion of two separate person images into a
coherent group shot" as a headline improvement**, two months later. Shipping
that as news is an implicit admission 2509 did not do it.

**Anecdote,** and it matches our renders exactly: facial averaging ("individuals
who look like strange composites of each other"), identity swapping (distinct
faces assigned to the wrong person), and partial blending.

**Inference:** 2509 concatenates references into one conditioning sequence with
nothing binding reference A to figure A. Assignment is left to textual ordering
cues, which is the weak channel.

## Why OUR regional masking produces one hybrid

**Verified, by reading this install's own `comfy/samplers.py`.** This is the most
important entry in the file:

> `calc_cond_batch` runs **a separate full model forward pass per conditioning**,
> then accumulates `out_conds[i] += output[o] * mult[o]` and finally
> `out_conds[i] /= out_counts[i]`, where `mult = mask * strength`.

So with `set_cond_area="default"`, **the mask never reaches attention.** It is a
per-pixel weighted average of two complete noise predictions. Each prompt sees
the entire latent and paints its character wherever it likes; the mask only
decides which prediction wins each pixel.

**Two full-frame characters averaged together is exactly the fused figure we
render.** The community docs state this obliquely: "strength is normalized
before mixing multiple noise predictions".

`set_cond_area="mask bounds"` narrows the sampled region to the mask bounding
box, which is a real receptive-field restriction. **Tried here 2026-09-05: it
errors on our graph** (rc=1), probably the same 5D-latent incompatibility that
crashes `ConditioningSetAreaPercentage` on Qwen - **not yet confirmed.**

## What was measured here

| Approach | Result |
|---|---|
| Global conditioning, one prompt | a duplicated third figure |
| Regional masks (`set_cond_area="default"`) | one blended hybrid |
| Text fixes to the regional path | real bugs found, outcome unchanged |
| Sequential compositing (pass 2 adds to pass 1) | **pass 2 replaces the character** |
| **Inpainting with `SetLatentNoiseMask`** | **character A preserved 3/3**, B arrives as parts |

The mask geometry itself is sound: a single band over the left half renders the
left half and leaves the right a dead rectangle. But two bands holding the SAME
character produce ONE centred figure spanning the seam, which is the averaging
above, seen directly.

**Inpainting is the only approach that has ever preserved the first character**,
because it restricts which pixels may change rather than asking the model to
preserve them.

## What teams actually ship

**Verified.** Nobody ships single-pass two-character frames. Three patterns:

1. **Sequential inpaint.** Regional prompting is "mandatory at 2+ characters"
   and breaks past 3 regions; beyond that, generate everyone as the same
   character and fix each face by inpaint.
2. **Separate renders, hand-composited.** And the harmonising step is
   **hand-painted** white and shadow at the boundary, not a low-denoise img2img
   pass. If we assumed automation there, no shipped account supports it.
3. **Per-character shots stitched into environments** by an edit model.

The one industrial-scale case avoided generation entirely: 3D rigs animated in
Maya/Unreal with diffusion used only as a style-transfer pass.

**Verified cost:** a two-character shot runs **6x to 18x** a single close-up in
the one detailed public time log (10 minutes versus 1 to 3 hours), with 30 to 50
generations per panel considered normal.

**Anecdote:** designing around it is real practice - "build dialogue scenes
reaction-first, don't cut to the person speaking, cut to the person listening",
one production reporting two-thirds of the final cut was reaction. It has no
community name.

## Options, none free

1. **Fix the masking.** Get `mask bounds` working, or use Attention Couple,
   which patches cross-attention rather than averaging predictions. Note its
   documented limit: "because the generation still sees and diffuses the full
   latent, attention coupling is not guaranteed to perfectly limit the effect of
   your prompt to the masked area". Also: the main ComfyUI implementation
   supports SD1, SDXL and Anima, **not** Flux, and nothing states it supports a
   Qwen edit model.
2. **Composite properly.** Render each character alone, cut out, place, and
   accept a hand-painted harmonise. This is what shipping teams do.
3. **Design around it.** Reframe two-handers as singles, over-shoulders and
   reaction shots. Free in GPU terms, costs story coverage.

## Gap in the research

**Reddit was hard-blocked to every agent**, at both search-policy and network
level, and mirrors 403'd. So none of this carries r/StableDiffusion or
r/comfyui primary accounts, which is likely the densest source for this exact
question. Closing it needs a Reddit API token or an allowlisted mirror.

**And nobody has published an attempts-per-usable-panel rate** for on-model
two-character panels against approved designs. Our own numbers are more precise
than anything found in public.
