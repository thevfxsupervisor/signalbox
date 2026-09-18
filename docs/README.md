---
type: reference
title: What is in docs, for this snapshot
description: An index of the documentation kept in this public, trimmed snapshot.
tags: [genvideo, documentation, index]
timestamp: 2026-09-11
---

# The documentation set

This is a trimmed public snapshot (see the root `README.md`, "Notes & scope"), so this index only
lists what actually ships here, not the full internal documentation set it was drawn from.

## Start here

| Document | What it answers |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Why the system is built this way, and what each choice costs |
| [`HOW-A-SHOT-IS-MADE.md`](HOW-A-SHOT-IS-MADE.md) | The steps from an empty shot to a finished clip, and the two places a human decides |
| [`PROMPT-COMPOSITION-BY-STAGE.md`](PROMPT-COMPOSITION-BY-STAGE.md) | Every field that reaches a model at each stage, what happens when it is empty, and which fields are read and discarded |
| [`METHOD.md`](METHOD.md) | The wedge method, and three findings that shaped the design |

If you read three, read those first three in that order: why, then what, then which field.

## Operating the pipeline

| Document | What it is for |
|---|---|
| [`OPERATOR-GUIDE.md`](OPERATOR-GUIDE.md) | Working the review queue: approving, requesting revisions, what each status means |
| [`WATCHERS.md`](WATCHERS.md) | Every watcher, its trigger, and how to tell whether it fired |
| [`SG-PAGE-LAYOUTS.md`](SG-PAGE-LAYOUTS.md) | Which fields to put in front of an operator, and which to keep off the page |
| [`SG-UX-SETUP.md`](SG-UX-SETUP.md) | Setting the tracker up so the pipeline's triggers are reachable |
| [`MODELS-AND-PROMPTING.md`](MODELS-AND-PROMPTING.md) | What each model is for and how it is actually prompted here |

## Reference

| Document | What it is |
|---|---|
| [`TWO-CHARACTER-PROBLEM.md`](TWO-CHARACTER-PROBLEM.md) | One named open problem, its tested hypotheses and their negative results |
| [`MODEL-LICENCE-RECORD.md`](MODEL-LICENCE-RECORD.md) | Every model in use, its licence, and what that permits |
| [`BUILD-LOG.md`](BUILD-LOG.md) | One dated block per iteration: measurements, corrections, refuted hypotheses |
