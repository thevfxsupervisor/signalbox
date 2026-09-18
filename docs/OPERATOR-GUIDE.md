---
type: method
title: Gen-video: how to drive it from ShotGrid
description: Everything happens in the GENVIDEO_TEST project (extend to another project later by changing one constant in the worker). You never touch a terminal.
tags: [operator, guide, genvideo]
timestamp: 2026-09-06
---
# Gen-video: how to drive it from ShotGrid

Everything happens in the **GENVIDEO_TEST** project (extend to another project later by changing one
constant in the worker). You never touch a terminal.

## Make a generation

1. Open (or create) a **Shot**.
2. Fill the generation fields (add the columns to your Shot page view once:
   right-click a column header, Insert Column):

   | field | what it does | default if blank |
   |---|---|---|
   | Gen Prompt | the positive prompt | required |
   | Gen Negative Prompt | what to steer away from | the wan-prompt skill's universal baseline (bright colors, overexposed, static, blurred details, ... - see below) |
   | Gen Kind | `still` or `video` | video |
   | Gen Size WxH | e.g. `512x288` | 512x288 |
   | Gen Frames | video length; (n-1) must divide by 4: 25, 49, 81... | 25 |
   | Gen Steps | sampler steps | 10 |
   | Gen CFG | guidance; 5.0 is right for this model | 5.0 |
   | Gen Seed | starting seed; each revision bumps it +100 | 1000 |

3. **Link references**: put Assets in the Shot's normal **Assets** field. The worker uses the FIRST
   linked Asset's newest image attachment as the start frame (image-to-video). No Assets linked =
   pure text-to-video.
4. **Flip `Gen Status` to `queued`. That is the button.**

## What happens then

The worker (runs on the workstation) picks it up on its next pass:

    queued -> generating -> review     (a Version appears on the Shot, status "Pending Wangle Review")
                                or -> error  (the reason is written into Gen Log - fix and re-queue)

`Gen Log` always holds the latest one-line state, including seed and timing.

## Review

Open the Version, watch it in the web player, then either:

- **Approve it** (any approved status: apr / ad / fin / paf / dlvr)
  -> the Shot flips to `done`. Nothing is delivered anywhere; approval closes the loop.
- **Ask for changes**: write a Note on the Version, and/or set it to `rrq` (Revision Requested;
  `rjct` and `tekfix` also requeue)
  -> the worker requeues the Shot, regenerates with a bumped seed as the NEXT version
     (v001 stays put, your Note stays attached to it), and publishes for review again.
  Your Note text is kept verbatim in the ledger on disk.

`appcbb` / `appgra` ("approved but...") deliberately do NOT close a shot: they are counted as
provisional, so nothing with an open caveat reads as finished.

## Adding references

Create an **Asset**, give it a thumbnail-able image: drag an image onto its thumbnail, or attach the
file to the Asset (the worker takes the newest image attachment). Link it from any Shot's Assets
field. Existing types `Prop` and `Environment` are used; nothing new was added to your Asset-type
taxonomy.

## Prompt rule the worker enforces

Do not negate inside the positive prompt ("no cars", "without text"). The model conditions on the
nouns it sees, so a negation summons the thing it names. The worker refuses such a prompt and tells
you in Gen Log which word to move to the Negative field.

## What the worker will not do

- Start generating while the GPU is busy (Fusion or another app under real load): it defers and
  retries next pass.
- Leave ComfyUI running: started per batch, torn down after, verified gone (Geoff's rule).
- Overwrite a version, reuse a version number, or ship anything anywhere. Approval ends at `done`.

## If something looks stuck

`Gen Status` meanings: `queued` waiting for the next worker pass; `generating` in progress right
now; `review` waiting on you (a revision request flips the Shot straight back to `queued`, with
your request quoted in Gen Log); `error` read Gen Log;
`done` closed. The worker passes run continuously while the workstation's agent session is up; if
nothing happens for a long time, that session may be down - the queue survives, nothing is lost, it
resumes on the next pass.

## Writing better prompts: the studio skills

`build/reference/skills/generative-ai/` in the project tree on the node (not in this repo) holds
the studio's prompt-expert skills (vendored from
Wangle-Media/claude-skills; canonical copy lives there). `wan-prompt.md` is the one for this
pipeline's model. Use it in any Claude session to turn a rough brief into a model-optimal prompt,
then paste the result into Gen Prompt.

Two of its rules are already built into the worker, so you get them without asking:

- a strong universal negative baseline is applied whenever Gen Negative Prompt is left blank
- in image-to-video (a reference is linked), "Preserve all appearance and environment from source
  image." is appended automatically unless you already wrote it

Its settings table agrees with what we measured here: cfg 5 to 7 is the sweet spot (worker default
5.0). It recommends 20 to 30 steps for final quality; the worker defaults to 10 for fast drafts, so
set Gen Steps to 25 on a shot when it is worth the extra time.