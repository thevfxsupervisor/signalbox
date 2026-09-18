---
type: log
title: LOOP-LOG, gen-video build on the workstation
description: One dated block per iteration. What shipped, what was verified and HOW (independent evidence, including a healthy control), what is blocked.
tags: [build, log, genvideo]
timestamp: 2026-09-06
---
# LOOP-LOG, gen-video build on the workstation

One dated block per iteration. What shipped, what was verified and HOW (independent evidence,
including a healthy control), what is blocked.

---

## 2026-08-26, iteration 1: Phase 0 (environment + self-sufficiency)

### Shipped

- Controlled interpreter: **CPython 3.12.14**, uv-managed, venv at `C:\genvideo\venv`. Named by full
  path in every command. The PATH `python` on this box is a Microsoft Store stub and is never used.
- **uv 0.12.6**, **ffmpeg + ffprobe 9.0.1-full_build**, both user-scope via winget, no elevation.
- `build/STATE.md` written: interpreter path, every install with version, machine state, and the
  open items.
- `build/tests/no_emdash.py`: codepoint scanner with a self-proving canary.

### Verified, and HOW

**Bundle integrity: 7/7 md5 match.** Verified twice: once at 14:30, and again after rr-mcp reported
the brief had changed.

> *Control that makes the result mean something:* the md5 comparison was shown to FAIL first, on a
> planted file hashed against a bogus digest. A comparison that has never returned "mismatch" is not
> evidence of a match.

**rr-mcp's "GEN-VIDEO-BUILD.md CHANGED, re-read it" was wrong, and the manifest is why we know.**
The file's md5 (`971a76cd...`) and mtime (14:22:54) were identical to the earlier verification, and
it was already read at 14:30. Only `MANIFEST.md` was rewritten, adding prose claiming two files had
been revised, while both files' hashes stayed the same. **The hash is the authority; the prose beside
it is not.** No stale content was acted on.

**Wrapper runs: `comfyui_execute.py --help` exits 0** under `C:\genvideo\venv\Scripts\python.exe`.
Confirmed stdlib-only (argparse, json, os, re, sys, time, uuid, urllib), so it needs no packages and
cannot break on a dependency resolve.

**Em-dash gate: 25 files scanned, 0 banned, 0 suspect.**

> *Control:* the canary ran FIRST and fired on a planted U+2014, then stayed silent on clean text
> (so it is not a detector that simply always fires). Only then was the clean scan trusted.

The gate found real violations rather than rubber-stamping: **6 on the first run** (5 in
`workflows/README.md`, 1 in the scanner itself) and **48 more** across earlier documents, all
authored before this rule was known. All corrected. The scanner's own hit was a genuine design flaw:
it contained a literal U+2014 in its canary string and so flagged itself every run. The canary is now
built with `chr(0x2014)`, so the file that bans the character does not contain it.

### Recorded, not silently resolved

`comfyui_execute.py`'s docstring instructs keeping ComfyUI running as a persistent service via a
Scheduled Task. **That is the opposite of the rule for this box** (Geoff, 2026-08-26, after finding
an idle ComfyUI running for weeks on a second workstation) and of GEN-VIDEO-BUILD.md sections 1 and 5. The brief
wins; the wrapper text is vendored from a dedicated-render-node context. Noted in STATE.md so nobody
later "fixes" this box to match the tool.

### Blocked

Nothing in Phase 0.

**Not blocking, and explicitly not on this build's path:** SSH key auth to this box still fails
(`ssh_dispatch_run_fatal ... Unknown error [preauth]`). KB5121003 was the leading hypothesis and is
now refuted with stronger evidence than rr-mcp stated: `sshd.exe` **did** change (1,333,248 to
1,335,296 bytes, SHA256 now `98BBBDD8...`), matching the working ws15 box, and the fault is
byte-identical. The build dispatches over local HTTP and verifies on local disk, so it needs no SSH.

### Next

Phase 1, one shot end to end. Its criteria, and the one that is a genuine gap rather than a check:
**no wall-clock timing has ever been recorded under controlled conditions.** The 9.25 s figure from
earlier today was warm-cache (weights still in the OS page cache from the download) and must not be
quoted as a benchmark. Phase 1 owes a cold and a warm number, measured, with the cache state stated.

---

## 2026-08-26, iteration 2: Phase 1 (one shot, one machine)

### Shipped

- One shot generated end to end through `comfyui_execute.py`, disk-mode verification, 25 PNG frames.
- Encoded to `output/phase1/p1_cold_v001.mp4`, versioned, non-destructive (the encoder step refuses
  to overwrite an existing version rather than silently replacing it).
- Contact sheet `output/phase1/p1_cold_contactsheet_v001.png`, inspected BEFORE the video render.
- `build/tests/prompt_negation_audit.py`, with a self-proving canary.

### THE TIMING, which had never been measured

    cold  (post-reboot, models unloaded, seed 1000)   15.52 s    execution_cached: none
    warm  (models resident, NEW seed 2000)            10.49 s    real execution
    cache hit (byte-identical inputs, seed 1000)       0.72 s    execution_cached: ALL 9 nodes

    => model load ~5.03 s; sampling ~10.49 s for 25 frames at 512x288, 10 steps

Cold was genuinely cold: measured 35 minutes after a reboot with `torch_vram_total: 0` confirmed
before the run, so the weights had never been read. The 9.25 s figure quoted earlier today was
warm-cache and should not be used.

### Verified, and HOW

**Cache-hit guard: PROVEN, and it caught the exact false positive it exists for.** A byte-identical
re-run returned `PASS: 25 output file(s) verified` while generating **nothing at all**:

    wall clock        0.72 s vs 15.52 s cold      21.6x faster
    execution_cached  nodes 1..9 (every node)     vs [] on the cold run
    output files      still 25, timestamps UNCHANGED, no new files written

So the discriminators are wall-clock and the cached-node list. **The PASS on its own is worthless**,
which is precisely the trap the brief names.

**Playability:** ffprobe reports `h264 / High / yuv420p / 512x288 / 25 frames / 24 fps`, browser-safe.

**Prompt-negation audit: CLEAN** on the prompt used. The auditor was proven able to fail first (it
caught 2 planted negations, then stayed silent on a clean prompt), and separately demonstrated on a
bad prompt where it correctly named `cars`, `watermark` and `text` as subjects that would end up in
the conditioning.

**Teardown:** verified three independent ways, not one. Port gone, process gone, and absent from
`nvidia-smi --query-compute-apps`; GPU back to 869 MiB desktop-only. Note that Fusion Render Node
appears in that same GPU list: this box IS shared, which is why the contention rule exists.

### The finding that matters, and only the contact sheet caught it

**The output is technically valid and visually poor.** Every per-frame check passed. The contact
sheet shows the 25 frames are **near-identical**: there is almost no motion, and the reds are
clipping with visible chromatic fringing.

Two causes, one mine and one worth carrying into Phase 2:

1. **Self-inflicted:** the prompt said "static locked-off camera". That suppresses camera motion,
   though it does not explain why the car does not move either.
2. **Off-recipe cfg.** This ran at **cfg 5.0**. GEN-VIDEO-BUILD.md section 3 states the validated
   recipe runs at **cfg 1.0**, and that raising cfg above 1 "pulls the image toward the text and off
   model". Oversaturation and clipped primaries are the classic high-cfg artifact, so the brief
   predicted this result before it was produced.

**A single frame looked fine.** The earlier smoke test at these settings produced one convincing
image, which is exactly how this would have been signed off. The sequence is what exposed it.

### Blocked

Nothing. Phase 1 criteria met.

### Next

Phase 2 (wedges + controls) is now pointed at a real question rather than a synthetic one: **wedge
cfg**, with the label burnt in, a control run for the noise floor, and mean/spread/n on any
magnitude claim. One prerequisite found: ffmpeg `drawtext` fails here with
`Fontconfig error: Cannot load default config file`, so the burnt-in label must pass an explicit
`fontfile=` (`C:\Windows\Fonts\consola.ttf` is present and confirmed).
---

## 2026-08-26, iteration 3: Phase 2 (wedges, controls, QC)

### CORRECTION, against my own Phase 1 conclusion

In iteration 2 I wrote that the poor output was caused by running at **cfg 5.0**, "off-recipe",
citing GEN-VIDEO-BUILD.md section 3 which specifies cfg 1.0. **I ran the wedge and it is the
opposite.** Quality improves monotonically as cfg RISES:

    cfg 1.0   chromatic breakup, no coherent subject
    cfg 2.0   still prismatic, subject emerging
    cfg 3.5   coherent car, road, horizon
    cfg 5.0   cleanest of the four

The brief is not wrong; **I applied its constant outside the configuration it belongs to.** Section 3
ties cfg 1.0 to the **4-step lightx2v distill LoRA on Wan2.2 A14B**, and says so plainly: the LoRA is
a "FIDELITY ANCHOR" and dropping it means "cfg must rise above 1". This node runs **TI2V-5B with no
distill LoRA**, which is the "dropped it" case. cfg 1.0 there gives the sampler almost no guidance.

I asserted a cause from a document instead of from a measurement, in the same write-up where I
recorded the rule "run the control before attributing any difference to your change". The wedge is
that control, and it took about 30 seconds of GPU time.

**I also over-attributed the Phase 1 colour clipping to cfg.** At cfg 5.0 here the colour is clean.
The Phase 1 sequence differed in prompt and seed as well, so cfg was never isolated. The near-zero
motion is still fairly attributed to my prompt saying "static locked-off camera"; the colour claim
is withdrawn as unsupported.

**Fleet consequence:** any job template that hardcodes cfg 1.0 from the canonical plan will produce
garbage on a TI2V-5B node. cfg belongs to the model-plus-LoRA combination, not to the pipeline.
rr-mcp has been told, since the canonical plan lives on a second workstation.

### The noise floor is EXACTLY ZERO, and that is a real result

Two independent executions, identical inputs, **separate ComfyUI processes with the execution cache
cleared by a restart** (not a re-submit, which would only have measured the cache):

    NOISE FLOOR (ctrlA vs ctrlB)   n=25  mean=0.00000  sd=0.00000  min=0.00000  max=0.00000

This pipeline is bit-deterministic on this box. Consequence: **any nonzero difference is signal**,
and the usual "is this above the noise" question has a trivial answer here. Determinism is per
machine; the brief's warning that the same recipe diverges across GPUs still stands and is untested.

### Parameter-reaches-model, with mean, spread and n

Seed held at 3000, measured against the cfg 1.0 control:

    cfg 1.0 (control B)   n=25  mean= 0.00000  sd=0.00000     <- the floor
    cfg 2.0               n=25  mean=36.05527  sd=2.12371
    cfg 3.5               n=25  mean=55.67814  sd=2.89297
    cfg 5.0               n=25  mean=66.05664  sd=1.84838

Monotonic and far above the floor: cfg reaches the model. Seed wedge at cfg 5.0, same treatment:

    seed 4200 vs 4100     n=25  mean=56.87205  sd=1.48221
    seed 4300 vs 4100     n=25  mean=63.41163  sd=0.93281

### The grid refuses a mis-sized wedge, PROVEN before it was trusted

`build/tests/wedge_grid.py` was shown to refuse first, on a planted 9-frame wedge beside a 25-frame
one. It named both counts, explained that padding would make an incomparable grid look comparable,
exited 1, and **wrote no file**. Only then was the real grid built.

Labels are burnt into each tile, so a pick is stated as a VALUE ("cfg 5.0") and never as a position.
ffmpeg `drawtext` could not be used: no fontconfig on this box. PIL with an explicit
`C:\Windows\Fonts\consola.ttf` has no such dependency.

### Timing observation, deliberately NOT explained

First run after a ComfyUI restart takes ~10.5 s; subsequent runs in the same process ~5.5 s. The
obvious guess is cached text-encode, but I did not isolate it and am not asserting it. Recorded as an
observation for whoever needs throughput numbers.

### Blocked

Nothing. Phase 2 criteria met.

### Next

Phase 3: a pre-dispatch GPU-contention check that can say **no** (and is proven to say no), plus
teardown verification, which is already running after every batch here. Fusion Render Node is live
on this GPU, so the contention check has a real thing to detect rather than a synthetic one.
---

## 2026-08-26, iteration 4: Phase 3 (dispatch that yields) + Phase 4 preflight

### Shipped

- `build/tests/gpu_guard.py` - pre-dispatch contention check returning PROCEED / DEFER / SKIP.
- `build/tests/teardown_verify.py` - three independent teardown checks.
- `build/tests/sg_preflight.py` - Phase 4 readiness, fails loudly with no credential.
- `shotgun_api3 3.10.3` pinned into the venv.

### The guard was WRONG on its first real run, and the fix matters

First version deferred whenever a foreign app appeared in `nvidia-smi --query-compute-apps`. Run
against reality it returned:

    DEFER: another application holds this GPU: FusionRenderNode.exe

**That is a false positive, and a permanent one.** Fusion Render Node sits resident on this box at
**1% utilisation and 860 MiB total GPU memory**, i.e. doing nothing. A guard keyed on presence would
have deferred every job forever, and nobody could tell "correctly yielding" from "broken and always
refusing". A guard that cannot say yes is exactly as useless as one that cannot say no.

Fixed to decide on **measured load**, and to report a resident neighbour without obeying it.
(nvidia-smi reports per-process memory as N/A for these processes, so per-process attribution is not
available on this box; whole-GPU numbers are what there is. Stated rather than papered over.)

### Proven it can say NO, against REAL contention rather than a rigged threshold

    idle              PROCEED  11426 MiB free of 12282, util 0%
                               (resident but idle: FusionRenderNode.exe)
    models resident   DEFER    only 3005 MiB free, job needs 11000 MiB
    nvidia-smi gone   SKIP     cannot read - refusing to guess

The DEFER was produced by actually loading the models (9277 MiB occupied), not by lowering a
threshold until it tripped. `--self-test` additionally drives an impossible VRAM demand and asserts
DEFER, so the guard's yes is worth something.

**SKIP is deliberately distinct from DEFER**: "I could not check" and "it is free" are the same
silence, and the safe reading of silence is not-free.

### Teardown, with the checks proven able to fail FIRST

Run against a live ComfyUI with `--expect-running`, all three checks correctly reported NOT torn
down (port 1, proc 1, GPU app 1, 9248 MiB). Only then was the real teardown trusted:

    1 PORT  listeners on 8188      : 0
    2 PROC  ComfyUI python procs   : 0
    3 GPU   ComfyUI compute apps   : 0
            GPU memory used        : 876 MiB (baseline 1500)

They are independent on purpose: a wedged process can lose its socket and keep its VRAM, which is
how an orphan held a GPU in this fleet for 8 days.

### BLOCKED: Phases 4 and 5, on a Geoff-only task

`sg_preflight.py` reports all three ShotGrid variables absent on this box. Phase 4 cannot start.
This is the stop condition GEN-VIDEO-BUILD.md section 7 names, and it is not workaroundable without
breaking the one secret rule that still binds.

Needed once, by Geoff:

    1. a dedicated ShotGrid script user (suggested: genvideo)
    2. SG_SITE_URL, SG_SCRIPT_NAME, SG_API_KEY set as User or Machine env vars ON THIS BOX
    3. a NEW dedicated ShotGrid project for the test, never a live client show

Everything else on that path is done: `shotgun_api3` is pinned, and the preflight's BOTH branches are
tested. The failure path was verified with no key, and the success path with a dummy value that was
never a real credential and was never written to disk. **When the key exists, the only new variable
is the key.**

### Phase status

    Phase 0  COMPLETE
    Phase 1  COMPLETE
    Phase 2  COMPLETE
    Phase 3  COMPLETE for the parts this seat owns. The RR-side items (client group,
             Frozen_Minutes render-app config) are explicitly rr-mcp's and Geoff's per the
             brief, and direct HTTP dispatch needs neither.
    Phase 4  BLOCKED on the ShotGrid setup above.
    Phase 5  BLOCKED behind Phase 4.
---

## 2026-08-26, iteration 5: Phase 4 (ShotGrid Versions) + Phase 5 (review loop)

### Unblocked by Geoff

Geoff instructed this seat directly to reuse the existing `mcp-server` key, overriding the earlier
hold. Copied from the `.mcp.json` into this box's User environment WITHOUT the value passing through
a terminal, a log, or any file: verified by length and by comparing a truncated hash, read back from
the store rather than from the in-memory copy. Source file deliberately left untouched, because the MCP
server on the other box still reads it and breaking someone's tooling to tidy a secret is not an
improvement.

**Still owed, deferred not forgotten:** rotate that key, which is overdue, and split the shared
identity into a dedicated one so the audit log can attribute actions.

A dedicated test project was created via the API with Geoff's explicit approval, chosen over reusing
someone else's sandbox. The site carries live client shows alongside it, which is why the target was
not picked unilaterally.

### CORRECTION to the reference doc: the status vocabulary was a guess, and wrong

`shotgrid-review-version-fieldset.md` says the defaults are "typically `rev`, `apr`, `vwd`, `na`" and
tells you to verify. Verified with `schema_field_read`, this site has **14** values and **no `vwd` at
all**:

    rev     Pending Wangle Review        ad      Approved by Director
    pf      Pending Client Feedback      rrq     Revision Requested
    tekfix  Pending Tech Fix             appcbb  Approved CBB
    appgra  Approved with Grading Note   apr     Approved
    rjct    Rejected                     dlvr    Delivered
    fin     Final                        omt     Omit
    paf     Presented as Final           na      N/A

The doc was right to say verify. Anything that assumed the defaults would have mapped `rrq`
(Revision Requested) to nothing at all and missed every revision request.

**The mapping, recorded once as the doc asks:**

    approved      apr, ad, fin, paf, dlvr
    provisional   appcbb, appgra   <- "approved" WITH outstanding work. Never counts as done.
    needs_revision rrq, rjct, tekfix
    pending       rev, pf
    inactive      na, omt
    unknown       -> provisional, so a new studio status cannot silently ship a shot

`appcbb` and `appgra` are the interesting ones: both read as approval and both carry unfinished work.
Folding them into approved is exactly how a shot ships with a note still open.

### Phase 4, verified by outcome

    version   GENVID_010_CMP_wan22ti2v_v001 created, status rev, frames 1001..1025
    sg_task   resolved to 'Comp' on step CMP  <- NOT an orphan
    upload    accepted, 140,123 bytes
    transcode ran: ShotGrid derived a 4,307-byte thumbnail FROM the movie
    round-trip HTTP 200, 140,123 bytes back, mp4 signature present

The round-trip downloads real bytes rather than checking a status code, because a 200 with an empty
body is the silent failure a status check would call success. **Stated honestly: this verifies the
upload, the transcode and a byte-exact round-trip. It does not verify a human pressing play.**

The Task guard is real, not decorative: the Task is re-resolved after creation and the script exits
rather than creating a Version with a dangling `sg_task`, which is the studio hook's known orphan bug.

Delete tested as the brief requires: `delete` returned True, the read-back returned NOT FOUND, and a
control query confirmed the finder can still see live Versions, so NOT FOUND means gone rather than
the query never matching.

### Phase 5, the full loop, live

    1. publish v001               -> done? False   (pending is not done)
    2. reviewer note + rrq        -> ingested 1 note, VERBATIM MATCH TRUE
                                     status rrq -> needs_revision, done? False
    3. next version               -> v002, and v001 STILL EXISTS at status rrq
                                     NOT overwritten, so the note still refers to something
    4. approve v002               -> done? True, and SHIPPED NOTHING

Ledger at `output/ledger/GENVID_010.json`, append-only, written atomically via a temp file and
`os.replace` so a crash mid-write cannot truncate it. Notes deduplicated on ShotGrid note id so a
re-run cannot double-count.

`next_version_number` counts from the highest version ever SEEN, not the highest still alive: a
deleted v002 must not let a later render reuse v002, because the reviewer's note about the old one
still exists.

### The ledger rules, each asserted rather than described

    appcbb (Approved CBB)      -> provisional, NOT done
    an unknown future status   -> provisional, NOT done
    apr                        -> approved, done
    a change request AFTER an approval -> reopens the shot
    next version after v001    -> v002

### Phase status: the plan is complete

    Phase 0  COMPLETE    Phase 1  COMPLETE    Phase 2  COMPLETE
    Phase 3  COMPLETE (the parts this seat owns; RR client group and Frozen_Minutes
                       are rr-mcp's and Geoff's by the brief, and HTTP dispatch needs neither)
    Phase 4  COMPLETE    Phase 5  COMPLETE

Nothing client-facing was shipped. Nothing touched a live show. ComfyUI is down.
---

## 2026-08-26, iteration 6: the production workflow, live end to end

### What now exists

ShotGrid IS the interface. Fields on Shot (sg_gen_prompt/negative/kind/size/frames/steps/cfg/seed,
sg_gen_status, sg_gen_log - created via schema_field_create as genvideo), references are Assets
linked through Shot.assets, and flipping sg_gen_status to `queued` is the button. The worker
(`build/tools/genvideo_worker.py`) polls, generates (i2v from the first linked reference via the new
`wan22_ti2v_5b_i2v.api.json` template, t2v otherwise), auto-publishes Versions, ingests feedback,
regenerates revisions, closes on approval. Operator doc: `build/OPERATOR-GUIDE.md`.

### Proven, in four worker passes against the live site

    pass 1  GENVID_020 queued -> v001 published (Version 66877), i2v from REF_car, teardown VERIFIED
    pass 2  reviewer note + rrq -> requeued -> v002 published, seed auto-bumped 8000->8100,
            v001 left intact with the note attached
    pass 3  v002 in review, no feedback -> worker does NOTHING (idempotent; ComfyUI never started)
    pass 4  v002 approved -> shot closed: "v002 approved (apr). Shot closed. Nothing shipped."

Also proven on the way: the worker's own prompt audit refused MY reference prompt ("no vehicles"),
which was regenerated with the subject moved to the negative. The gate caught its author again.

### References

REF_car (Prop) and REF_coast (Environment) created as Assets with thumbnails + full-res attachments.
`sg_asset_type` is an existing studio taxonomy; nothing was added to it. Asset has no
sg_uploaded_movie field - full-res reference lives as a plain Attachment, worker takes the newest
image attachment.

### The vendored prompt skills (rr-mcp snapshot, md5-verified, 12-file manifest clean)

`build/reference/skills/generative-ai/`: ltx, seedance, seedance25, wan. wan-prompt.md
INDEPENDENTLY agrees with our measured cfg wedge: its goldilocks is 5-7, our wedge picked 5.0.
Adopted into the worker and validated live (GENVID_030, still, defaults only, Version 66879):
the WAN universal negative baseline as the blank-field default, and the i2v preservation suffix
auto-appended when a reference is linked. Note: the vendored files carry em-dashes; they are
verbatim canonical content and exempt from the local gate, which applies to text authored here.

### Still open

- Worker cadence is this session's loop (a pass per wakeup). If Geoff wants it to survive the
  session, the natural next step is a Scheduled Task running --once every N minutes; needs nothing
  new, the worker is already single-pass idempotent.
- SG key rotation + Test Project API write-fence question, both unchanged.