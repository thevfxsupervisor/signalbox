---
type: reference
title: Model licence record
description: > SUPERSEDED WHERE IT CONFLICTS, 2026-09-05. Written BEFORE a peer engineer ran the hash-based sweep.
tags: [model, licence, record, genvideo]
timestamp: 2026-09-06
---
# Model licence record

> **SUPERSEDED WHERE IT CONFLICTS, 2026-09-05.** Written BEFORE a peer engineer ran the hash-based sweep.
> Every row below marked *not recorded* was a genuine gap at the time; many are now settled by
> **plan/LICENCE-SWEEP-VERDICT-2026-09-05.md**, which matched 17 of 17 files to their upstream
> publisher by sha256 and found 16 Apache 2.0 CLEAR and 1 R&D ONLY. **Read that first.** The
> demo-facing summary, including the twelve files the sweep did NOT cover, is in
> **docs/DEMO-LICENCE-CAVEAT.md**. What stays valid here: the three-questions framing, the
> no-artist-name-LoRA standing rule, and the record-at-generation-time recommendation.

Started 2026-09-05 after `a peer engineer` raised the legal gate for the client add-on. Geoff has not been
strict about auditing output rights for internal R&D, which is fine for R&D and **not fine for a
client deliverable or for anything promoted from R&D into internal production.**

**This file is a RECORD, not an audit.** I have read CivitAI's declared permission flags. I have
not read a single licence file, and an uploader's flag is a declaration, not a legal opinion.
Anywhere below that says "not recorded" means exactly that: no claim either way.

Three separate questions, per component, routinely collapsed into one:

1. **Weights licence.** What we may do with the model. Apache/MIT clean; RAIL variants carry use
   restrictions that survive into outputs; research-only or non-commercial is disqualifying.
2. **Output rights.** Separate from the weights, and some licences are silent.
3. **Training-data provenance and character resemblance.** The one that bites on a character show,
   and the one no permission flag addresses.

## The production stack

| Component | Role | Commercial use | Source of that answer |
|---|---|---|---|
| `anima-base-v1.0` | base image model | **not recorded** | no sidecar, no licence file on disk |
| `anima-turbo-lora-v0.2` | **speed LoRA**, 10-step cfg 1.0 | `Image, RentCivit, Rent` | CivitAI API, model 2560840, fetched 2026-09-05 |
| `anima-base-1-flat-color-v3` | style | `Image, RentCivit, Rent` | CivitAI API, model 1132089 |
| `anima-highres-aesthetic-boost` | style | `Image, RentCivit` | CivitAI API, model 2540444 |
| `Qwen-Image-Edit-2509` (`Q3_K_M` GGUF) | panel compositor | **not recorded** | |
| `qwen_image_edit_2509_lightning_4steps_v1` | **speed LoRA** | **not recorded** | **no sidecar file at all** |
| `qwen_2.5_vl_7b_fp8_scaled` | text encoder | **not recorded** | |
| `Wan 2.2 I2V A14B` (`Q4_K_S` GGUF, high/low) | shot video | **not recorded** | |
| `wan22_lightning_i2v_a14b_high` / `_low` | **speed LoRAs** | **not recorded** | **no sidecar file at all** |
| `wan2.2_vae`, `wan_2.1_vae`, `qwen_image_vae` | VAEs | **not recorded** | |

**`allowCommercialUse: Image` is the permission that matters to us**: it covers selling the images
produced. It is not `Sell`, which would cover selling the model itself, and we do not need that.

## What this record already shows

**Not one licence field existed anywhere before today.** The CivitAI sidecars record source, URL,
SHA256 and a verification timestamp, which is good provenance and says nothing about rights.

**The three most load-bearing speed LoRAs have no metadata file at all.** Qwen Lightning and both
Wan Lightning halves. `a peer engineer` predicted exactly this: a speed LoRA is a model with a licence and it
is the component everyone forgets. It is the component we forgot.

**Nothing here is yet known to be disqualifying, and nothing here is yet known to be clean.** The
three fetched flags are permissive for our use. Everything else is unrecorded.

## STANDING RULE: no artist-name LoRAs (Geoff, 2026-09-05)

**Artist-related LoRAs are excluded going forward, for legal reasons.** This is a rule at model
SELECTION, not something to catch at audit: a style LoRA trained on a named living artist creates a
downstream claim regardless of how clean the weights licence is, and the claim attaches to output
we ship rather than to the file on disk.

`anima-greg-rutkowski-style.safetensors` was **deleted 2026-09-05** on Geoff's instruction. SHA256
recorded before deletion: `3251ae0bf2771d7957b3aad251827dc5de716fd3c5911444d981045af5c82e11`.

**Correction to the first version of this file.** It said the LoRA was "not referenced by any
tool", which was wrong: that grep was truncated by `head` and a partial result was reported as
complete. It is referenced in two places, both harmless, and neither loads it:
`tools/build_trials_page.py` records it in a historical bakeoff table where it scored
"destroys register", and `tools/publish_test_versions.py:248` strips its trigger word from prompts.
The conclusion (nothing loads it, deleting breaks nothing) held. The evidence given for it did not.

**Still on disk, same class, not artist-named:** `AfroBullStyle_ANIMAv1` and `possummachine-A1_v1`,
both bakeoff leftovers that no tool loads. Not covered by the rule above, so left in place.

## What to do from here

- **Record the licence per artifact at GENERATION time, not retroactively.** A reconstruction is not
  an audit, which is the same principle as the prompt-provenance gate: working backwards from an
  output to what produced it is the failure the gate exists to prevent, one scope wider.
- **Close the unrecorded rows above**, starting with the Qwen and Wan speed LoRAs, since they are in
  every single render and have no metadata at all. They are not on CivitAI, so this means reading
  the model card and licence file at the source repo.
- **Read the licence file, not the summary.** Vendor copy is not a licence, the same scepticism that
  caught Qwen-Image-Edit-2511's "fusion of two person images" being marketing for a side-by-side
  stitch.
- **A permission flag says nothing about training data.** Question 3 stays open for every row in
  this table, including the three marked permissive.
