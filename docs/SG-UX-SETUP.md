---
type: method
title: ShotGrid UX setup: one-time clicks that make the pipeline pleasant
description: Audience: Geoff. Do this once in the GENVIDEO_TEST project (id 9999) and the daily loop becomes: open a tab, watch, flip a status. Everything here is layout and saved pages - no schema surgery, no code.
tags: [setup, genvideo]
timestamp: 2026-09-06
---
# ShotGrid UX setup: one-time clicks that make the pipeline pleasant

Audience: Geoff. Do this once in the **GENVIDEO_TEST** project (id 9999) and the daily loop
becomes: open a tab, watch, flip a status. Everything here is layout and saved pages - no
schema surgery, no code. Companion docs: `OPERATOR-GUIDE.md` (how to drive a generation),
`research/01-dcc-interface-conventions...md` (why these conventions).

Autodesk now calls the product **Flow Production Tracking (FPT)**; same site, same clicks.
"ShotGrid" and "FPT" are used interchangeably below.

One truth to hold onto before any of the clicks: **FPT has no real custom buttons.** A true
"Generate" button would need an Action Menu Item wired to a webhook server that we deliberately
do not run (nothing on the workstation accepts inbound calls). So in this pipeline **flipping `Gen Status`
to `queued` IS the button**, and everything below is arranged to make that flip, and the review
that follows it, one or two clicks.

---

## 1. The Shot page

Open **Shots** in the project (left nav, or the global "+" > Pages if you want it pinned).

### 1a. Columns, in this order

Right-click any column header > **Insert Column**, then drag headers left/right to order them.
Remove noise columns the same way (right-click > Remove Column). Target layout, left to right:

| # | column | field code | why here |
|---|---|---|---|
| 1 | Thumbnail | `image` | latest Version's frame shows up automatically |
| 2 | Shot Name | `code` | fixed identity, keep pinned left |
| 3 | Sequence | `sg_sequence` | grouping key |
| 4 | Cut Order | `sg_cut_order` | position in the cut (see section 2) |
| 5 | Gen Status | `sg_gen_status` | THE control: flip here |
| 6 | Gen Prompt | `sg_gen_prompt` | the work itself; widen it, enable wrap |
| 7 | Gen Kind | `sg_gen_kind` | still / video |
| 8 | Gen Size WxH | `sg_gen_size_wxh` | |
| 9 | Gen Frames | `sg_gen_frames` | |
| 10 | Gen Steps | `sg_gen_steps` | bump to 25 for finals |
| 11 | Gen CFG | `sg_gen_cfg` | |
| 12 | Gen Seed | `sg_gen_seed` | base seed; revisions bump +100 |
| 13 | Gen Negative Prompt | `sg_gen_negative_prompt` | |
| 14 | Assets | `assets` | linked references; FIRST one is the i2v start frame |
| 15 | Gen Log | `sg_gen_log` | worker's one-line state; widen it, enable wrap |

Prompt and Log want room: right-click their headers > adjust width, and turn on text
wrapping (right-click header > Wrap Text where offered). Everything the worker reads or
writes is on this one row; you never need a second page to queue work.

Sort: click the **Sequence** header, then shift-click **Cut Order** for a secondary sort.
That makes the page read in story order.

### 1b. Saved filter tabs, one per state

Along the top of the Shots page is a tab strip with a "+" at the end. Click "+" > new tab,
rename it (double-click the tab name), then open the filter panel (funnel icon) and set the
tab's filter. Make these tabs, in this order:

| tab name | filter | sort |
|---|---|---|
| `1 - Write` | Gen Status is none/blank | Sequence, then Cut Order |
| `2 - Queued` | Gen Status is `queued` or `generating` | Date Updated, newest first |
| `3 - Review` | Gen Status is `review` | Date Updated, newest first |
| `4 - Error` | Gen Status is `error` | Date Updated, newest first |
| `5 - Done` | Gen Status is `done` | Sequence, then Cut Order |
| `All` | no filter, grouped by Sequence | Cut Order |

The numbers keep the tabs in workflow order. `3 - Review` is your inbox: anything on it is
waiting on you and nobody else. `4 - Error` plus the Gen Log column is the whole debugging
UI - the worker writes the refusal or failure reason there, you fix the field it names and
flip back to `queued`.

On the `All` tab, group by Sequence: right-click the Sequence header > **Group by this field**
(or use the grouping control in the toolbar). Collapsed groups give you an episode overview.

### 1c. Save it

FPT does not auto-save layout for everyone. After arranging: **Page menu (top right of the
page) > Save Page**. If you skip this, the layout reverts next session.

---

## 2. The Sequences page (the cut, readable)

`episode_ingest.py` creates a Sequence per scene block (e.g. `PILOT01_A`) and stamps each
Shot's position into **`sg_cut_order`** - IF the field exists. Check first:

1. Open the Shots page, right-click a header > Insert Column, search "Cut Order".
2. If it exists, insert it and you are done.
3. If it does not: Insert Column > **+ Create New Field** > name it exactly `Cut Order`,
   type **Number**. ShotGrid derives the code `sg_cut_order` from that display name, which
   is the code the ingest tool looks for. Then re-run the episode ingest once so existing
   shots get backfilled (until then the value lives as text in each Shot's Description,
   the tool's documented fallback).

Now build the page:

1. Left nav > **Sequences** (create the page via "+" > Pages > Sequences if absent).
2. Columns: Thumbnail, Sequence Name (`code`), Shots (linked entity list), Description.
3. Click a Sequence to open its detail page > **Shots** tab > sort by Cut Order ascending >
   Page > Save Page. That sorted list IS the cut; reading it top to bottom is reading the
   scene.

There is no timeline view in FPT and no drag-to-reorder cut tool - do not look for one.
Cut order is a number column, changed by editing the number. (If the cut ever changes in
editorial for real, the research doc's OTIO note is the grown-up path.)

---

## 3. Versions page: surface the seed and settings

The worker writes every generation parameter into the Version's **Description**:
kind, size, frames, steps, cfg, **the actual seed used**, and whether a reference was
attached (i2v) or not (t2v). Example:

    Auto-published by genvideo_worker. video 768x432, 121 frame(s), 25 steps, cfg 5.0,
    seed 1200. Reference: CHAR_CHARE (i2v).

There is no per-field breakout - Description is the settings readout, so give it room.
On the **Versions** page (project left nav):

| # | column | field code | note |
|---|---|---|---|
| 1 | Thumbnail | `image` | |
| 2 | Version Name | `code` | `{shot}_CMP_gen_v###` - shot and take number in one string |
| 3 | Link | `entity` | the Shot |
| 4 | Status | `sg_status_list` | this flip is the review verdict (see below) |
| 5 | Description | `description` | seed + settings; widen, wrap |
| 6 | Date Created | `created_at` | |
| 7 | First/Last Frame | `sg_first_frame` / `sg_last_frame` | frames start at 1001 |
| 8 | Notes | `notes` | your revision requests live here |

Save the page. On a Version's **detail** page, the layout is also editable: gear /
"Design Page" mode lets you drag the Description field up next to the player so the seed
is visible while you watch. Save the design when done.

Status meanings the worker acts on (verbatim from the worker):

- approve: any of `apr`, `ad`, `fin`, `paf`, `dlvr` -> Shot flips to `done`, loop closed.
- revise: `rrq`, `rjct`, or `tekfix` -> Shot requeues, next version generates with seed +100.
- `appcbb` / `appgra` ("approved but...") are counted as provisional on purpose: they close
  nothing, so no shot with an open caveat ever reads as finished.

---

## 4. Daily review playlists

Convention: **one playlist per review day**, named

    PIG_review_YYYY-MM-DD

(zero exceptions, so playlists sort chronologically and a date is never ambiguous).

The worker does not create playlists today, but FPT makes the daily one a 30-second job:

1. Versions page > `3 - Review`-equivalent filter: Status is `rev` (Pending Wangle Review),
   Date Created is "in the last 1 day". Save this as a tab named `Today` (same tab mechanics
   as section 1b) - build it once, reuse forever.
2. Select all rows (click first, shift-click last, or ctrl+A).
3. Right-click > **Add to Playlist** > New Playlist > name it per the convention.
4. Open the playlist and press play: FPT plays it as a continuous session, in order.

Sort the playlist by Version Name before reviewing so shots run in shot order, or drag
rows within the playlist for a custom running order. If we later want the worker to
auto-create these, the naming convention above is the contract it will follow; adopting
the convention now costs nothing.

---

## 5. Where Hold, Ghost, and Compare live

The June 2026 FPT review update rebuilt the web player with proper comparison tools.
They live **inside the player overlay**, not on any list page:

1. Open a Playlist (or any Version) and launch the web player.
2. The player toolbar carries the **Compare** control. Load two Versions of the same shot
   (in a playlist, select both versions before opening the player, or add the second from
   the player's version browser) and Compare puts them against each other - the wipe/A-B
   grammar every review tool since RV has used.
3. **Hold** and **Ghost** are modes in the same toolbar: Hold pins a reference frame while
   you scrub, Ghost overlays it translucently - both are for eyeballing what changed
   between takes, which is exactly the "same shot, seed +100" question this pipeline asks
   every revision.
4. Annotations (pencil icon) are frame-accurate and become Notes linked to the Version.
   Write revision requests as a Note there and/or flip the Version to `rrq` - the worker
   ingests the newest Version's status flip and keeps your Note text verbatim in the
   on-disk ledger.

Because the worker never overwrites a version (v001 stays put forever), every regeneration
is automatically comparable against its predecessor. Compare against the previous version
is the payoff of that rule.

The update also added synchronized review sessions (multiple people, one transport). With a
review crew of one this is irrelevant today; it exists when a client call needs it.

---

## 6. Mobile review, honestly

What works well on a phone (FPT mobile app or mobile browser):

- watching Versions: the worker uploads to `sg_uploaded_movie`, so ShotGrid's transcoded
  proxies stream fine on mobile.
- flipping statuses: Version to `apr` or `rrq`, Shot's Gen Status to `queued`. Since the
  status flip is the button, the ENTIRE control surface of this pipeline is phone-operable.
- typing Notes on a Version.
- playing through a playlist.

What does not work well on a phone, so do not fight it:

- the 15-column Shot page. Prompt writing is a desktop job.
- drawn annotations. Fingers on a 6-inch player produce noise, not notes; type instead.
- Hold/Ghost/Compare - treat the comparison tooling as desktop-only.
- page/layout design of any kind.

Practical split: **phone = watch, verdict, note. Desktop = write prompts, compare takes,
triage errors.** A morning coffee review of yesterday's playlist with approve/rrq flips
from the couch is a fully legitimate operation of this pipeline.

---

## 7. What FPT cannot do (so you stop looking for it)

- **No true custom buttons.** Action Menu Items exist, but an AMI is just a URL: doing
  real work requires a webhook endpoint we would have to host and expose. The workstation accepts no
  inbound connections, on purpose. The status flip IS the button; the worker polls.
- **No push automation.** Nothing happens the instant you flip a status; the worker picks
  it up on its next pass. Seconds to a couple of minutes of lag is normal, not broken.
- **No timeline/NLE view.** Cut order is a number column (section 2). Conform happens in
  an NLE, fed by naming discipline, not inside FPT.
- **No custom field rendering.** Seed and settings stay inside the Description text blob;
  FPT will not parse it into columns. Wide + wrapped is as good as it gets without adding
  per-parameter fields to the Version schema, which is not worth the drift risk while the
  worker's Description line already says everything.
- **Layout is not magically shared or saved.** Every page arranged above needs an explicit
  Page > Save Page, and detail-page layouts an explicit save from design mode.

Total one-time cost of this document: roughly 30 minutes of clicking. After that the loop
is the one on the tin: write a prompt, flip to `queued`, watch it in tab `3 - Review`,
approve or `rrq` from wherever you are.
