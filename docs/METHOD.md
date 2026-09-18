---
type: reference
title: Method, and what it found
description: How a change gets trusted before it ships, three findings that shaped the current design, and why each check was built the way it was.
tags: [genvideo, method, verification, wedge, qc]
timestamp: 2026-09-11
---

# Method, and what it found

This is the general method behind the architecture in `ARCHITECTURE.md`, and three concrete
findings that shaped it. A lot of code comments in this repo point at internal reports that are
not part of this public snapshot; this page is what they were pointing at, written for a reader
who was not in the room.

---

## The wedge: a controlled comparison, not an opinion

Most of the open research questions in a generative pipeline (does this recipe hold identity
better than that one, does this ControlNet setting fix a defect or just move it) do not have a
correct answer available up front. A **wedge** here is the standard fix for that: change exactly
one variable, hold everything else constant, and score the result against an objective measure
instead of a glance.

The discipline that makes a wedge worth trusting:

- **One variable changes.** If two things change at once, a result cannot be attributed to
  either.
- **The measure is a computation, not a vote.** Where a computed check exists (a perceptual hash,
  a figure count, a pixel comparison), it is used instead of eyeballing the grid, for the same
  reason the QC gate below moved away from a vision model.
- **A baseline is in every comparison.** A wedge without its control cell is a demo, not evidence.
- **A negative result is a result.** Several wedges concluded "this does not fix it" or "this
  fixes it but costs VRAM we do not have," and that conclusion is what let later work stop
  re-testing the same idea.

## Finding: a vision model is not a QC gate

A QC gate needs to answer one question cheaply and repeatably: does this rendered panel actually
match the approved design. The first version asked a vision-capable model to look at the image and
the attribute list and answer. It measured **38% pass on panels a human had already approved as
correct.**

The failure was not random noise, it had a shape: attribute checklists conflate identity with
pose (a profile view "fails" a frontal description that was never meant to be literal), and a
legitimate close-up crops out an attribute the checklist expects to see. A model asked to grade
against a text checklist will fail images for reasons that have nothing to do with whether the
character is right.

The fix was to stop asking a model to judge, and check something a computer can actually verify:
a perceptual hash against the approved reference, a deterministic figure count from a skin-tone
mask and connected-component labelling, a direct comparison of what was recorded against what was
sent. None of these need an opinion. A perceptual-hash comparison scored 7 of 7 correct on the
same set the vision gate scored badly on; a deterministic figure counter, calibrated once against
three known-ground-truth panels, replaced a check that would have needed re-tuning by feel every
time the art style shifted.

**The generalizable rule:** where a computation can answer the question, use it instead of a
judgment call, even a good model's judgment call. Reserve the human (or the model) for the
question that is genuinely subjective.

## Finding: a defect that reproduces "sometimes" is still a defect

A repeated defect class in this project was the same fact computed in two places that quietly
drifted apart: a status tuple copied into several modules, a prompt clause assembled by two code
paths where only one of them talked to the model. The defence that stuck was mechanical rather
than procedural: a canary that reads the source of both copies and fails the moment they disagree,
rather than a comment asking someone to remember to keep them in sync. Comments do not run;
canaries do.

The same discipline applies to a check itself. **A canary that has never been seen to fail is not
evidence that it works.** Three canaries were written in this project that could not fail by
construction: one built its fixture from the same constant it was testing, one shared an object
between the fixture and the assertion, one compared two literals. All three were green from day
one, and all three were guarding nothing. The fix, applied everywhere self-tests exist in this
repo, is to break the thing on purpose, watch the check go red, then restore it, before trusting a
clean result from it again.

## Finding: a real system defect surfaces as three stale copies, not as an error

Early in this project the tools directory existed in three places at once: the working repo, a
mirror written by a sync step, and the tree an already-running service had loaded into memory.
Nothing errored. Editing the repo did not touch what was running, and a sync script wrote to a
directory a different hardcoded path never read from, so "keeping things in sync" was quietly
writing to a location that mattered to nobody.

It was found the same way the rest of this project finds things: not by reading code, but by
comparing artifacts against each other. File creation timestamps across the three locations
disagreed by hours to days, and a parent/child process check showed which directory the actual
running service had opened its files from. The fix (`deploy.py`, described in `ARCHITECTURE.md`)
removes the ambiguity structurally: one release directory per deploy, a pointer file naming the
current one, and self-tests run against the staged release before it is allowed to become that
pointer. The question "what does this machine actually execute" now has exactly one place to look,
instead of three candidates and a guess.

## What this adds up to

None of these three findings needed a smarter model. They needed the willingness to distrust a
result until it had been checked from a second angle: a computed measure instead of a glance, a
canary proven to fail instead of one assumed to work, a file timestamp instead of a belief about
what should be running. That habit, more than any specific recipe, is what the architecture in
this repo is built to enforce automatically wherever it can.
