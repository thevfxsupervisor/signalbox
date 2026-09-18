---
type: reference
title: Architecture, and the decisions behind it
description: Why the production tracker is the state machine, how the watcher loop is built, the invariants it holds, and the failure postures that follow from them.
tags: [genvideo, architecture, design, shotgrid, watchers, invariants]
timestamp: 2026-09-10
---

# Architecture, and the decisions behind it

This describes how the system is built and, more usefully, **why it is built this way and what each
choice costs.** Most of the decisions below were made twice: once on a whiteboard and once after
something failed in a way the whiteboard version could not survive.

Companion documents: `HOW-A-SHOT-IS-MADE.md` traces the executing path step by step,
`PROMPT-COMPOSITION-BY-STAGE.md` maps every field that reaches a model, and `METHOD.md` is the
research record this document argues from: the wedge method and three findings that shaped it.

---

## The central decision: the tracker is the state machine

There is no job queue, no scheduler, and no in-memory state that matters. **Every trigger is a piece
of state in the production tracker.** A watcher notices that a Task moved to ready, or that a Shot
carries a queued flag, or that a Version reached an approved status, and acts on it.

The alternative, the obvious one, is a queue: an operator clicks a button, a job is enqueued, a
worker picks it up. That design is easier to write and it is worse here, for three reasons that only
show up in production.

**It survives being killed.** The service can be stopped at any instant, mid-render, and restarted
with no reconciliation step, because it holds nothing that a restart would lose. What it was doing is
still written down in the tracker. This is not a theoretical benefit: this pipeline is deployed by
stopping the running process and starting a new one, several times a day.

**The operator is already there.** A production coordinator lives in the tracker. They are not going
to open a second interface to press a button, and a system that requires them to is a system that
gets driven by an engineer instead, which defeats the point. Approving a Version is a thing they were
going to do anyway; making that the trigger costs them nothing.

**Every trigger is auditable by construction.** "Why did this render?" is answerable by reading the
state that caused it, along with who set that state and when, from the tracker's own event log. No
separate audit trail can drift from the thing it audits, because there is no separate audit trail.

### What it costs, stated plainly

**A state nobody can see is a state nobody can act on.** This is the failure mode of the whole
design, and it has bitten more than once. When a stage was driven by a custom field with an informal
vocabulary, "this shot has an approved picture and has never been asked for a video" became a state
that existed, mattered, and was invisible: **30 of 55 shots sat in it**, and it was found only when a
human opened one shot and asked why. A queue would have made that visible as an empty queue. Here it
was visible as nothing at all.

The lesson taken from it is not "add more fields". It is that **state which drives work belongs in
the vocabulary the tracker displays**, so that a list view shows it, rather than in a private field
only the code reads.

**Polling has a floor.** The loop runs about once a minute. Nothing is instant, and a change made
while a subprocess is running is not noticed until it finishes. In exchange there is nothing to keep
in sync.

---

## The watcher loop

One process, one cycle, a sequence of independent watchers. Each watcher:

- **is triggered by tracker state**, never by another watcher calling it;
- **is idempotent**, because it will see the same state again next cycle if it did not change it;
- **logs a skip as loudly as an action**, because a silent skip is indistinguishable from a step that
  ran and did nothing;
- **catches its own exceptions**, so that one failing watcher does not take the cycle with it.

That last point is a rule with scar tissue behind it. A protection check added inside an
invalidation loop had no exception boundary of its own, and the caller wrapped every episode in a
single try, so **one transient API fault would have abandoned invalidation for every remaining shot
and every later episode** rather than skipping the one shot that faulted. It was found by an
adversarial review, reproduced by injecting a fault, and fixed with a canary that fails if the
boundary is removed.

### Ordering is deliberate, and stated where it matters

Watchers are ordered so that a step which records a decision runs before a step which acts on it, and
a sweep which tidies runs last. Where the order is load-bearing, it is asserted in the self-test by
reading the cycle's own source, rather than left as a comment that a later tidy-up can quietly
violate.

---

## Invariants

These are numbered in the code and cited at the point where they are relied on. The two that come up
most:

**Invariant 1: tracker state is the only trigger.** If a consequence cannot be reached from tracker
state alone, it will not happen reliably. The corollary is that anything a person does by hand is a
step the system does not have, and a document describing the pipeline as automatic is then wrong.
A tool that exists, passes its tests, and is only ever run by a human typing its name is not part of
the pipeline. That has happened here five times, and each one passed its own self-test every day
while being reachable from nothing.

**Invariant 11: one implementation, not several that drift.** The most repeated defect class in this
project is the same decision made in two places. Three functions each decided what a note meant, so a
rule added to one was silently absent from the others. A status tuple is copied into nine modules. A
prompt clause was assembled by two different code paths, and the one that wrote the record was not
the one that talked to the model, so the record described a prompt that was never sent.

The defence is mechanical rather than cultural: **a canary that reads the source and fails when two
copies stop agreeing.** Where a constant is genuinely duplicated for deployment reasons, an equality
check ties the copies together, and where a rule is genuinely shared, every caller reaches one
definition and a grep proves it.

---

## Provenance: a record must be produced by the code that acts

Every published artefact records what produced it: the prompt as actually sent, the components it was
built from, the model, and a hash of the workflow.

The rule that matters is narrower than "record things", and it was learned the expensive way. **A
record assembled beside the code that acts will diverge from it.** In this pipeline the divergence
was three clauses wide and shipped for weeks: one function built the text for the record, another
built the text for the model, they agreed when written, and then one of them changed.

So the function that SENDS now REPORTS. The call that submits work fills a record dictionary from the
exact substitutions it submits, and the publisher writes that. The reconstructing function was
deleted so that a second assembler cannot exist. A gate compares the record against the sent text and
reports disagreements without blocking a publish, because a provenance mismatch is a reporting defect
and not a reason to throw away a rendered frame.

**A field is an INPUT, a RECORD, or a LIE**, and a tracker does not distinguish them by sight. Three
fields on the composition path are read by the code, used only for the record, and never reach the
model. That is fine as long as it is documented, and actively misleading if a page presents them as
controls.

---

## Failure posture

**Fail safe, and be explicit about which direction is safe.** The direction is not always obvious and
it is not always the same:

- A protection check that cannot complete treats nothing as locked, because a lock that silently
  appears is worse than one that silently does not.
- A lookup that decides whether work is needed treats a failure as "work exists", because reading a
  failed lookup as "nothing exists" would have started thirty renders.
- A retiring sweep that cannot read its input does nothing at all, because the cost of wrongly
  retiring somebody's work is higher than the cost of tidying late.

**A skip must be as loud as an action.** The pipeline has been observed silently doing nothing for
hours while looking healthy, and the cause each time was a branch that returned early without saying
so.

**Liveness is the artefact, not the process.** A running process proves nothing; a growing log proves
more; a published artefact proves the most. The current monitor watches all three and calls a stall
only when every one of them is quiet, because a healthy pass can legitimately produce a silent log
for over an hour.

---

## The deploy seam

The repository is not what executes. `deploy.py` stages the tools into a timestamped release
directory, runs **every module's self-test as preflight**, and refuses to activate a release whose
tests fail. A pointer file names the active release; the service runs from there.

This exists because at one point there were three copies of every tool and the running service used a
fourth, a hand-assembled mixture that existed nowhere in version control and had never been tested
together. The seam makes one question answerable: **what does this machine actually execute?**

Deployment waits for the GPU to go idle rather than discarding an in-flight render, then stops the
old process and verifies the new one is up.

---

## Testing: a canary that has never failed is not evidence

Self-tests here are called canaries, and the discipline around them is the part worth copying.

**Every canary must be proven to fail.** Break the thing it guards, watch it go red, restore it. A
canary written from the constant it is testing cannot fail; a canary whose fixture shares an object
with its assertion cannot fail; a canary that compares two literals is a tautology. All three have
been written in this repository, all three were green, and all three guarded nothing.

**Canary the call site, not the tool.** Preflight proves a module works. It says nothing about
whether anything calls it. Where a capability must be reachable, the service's own self-test asserts
that the cycle's source contains the call, which is honest about what it checks and catches the real
risk, someone moving a block while tidying.

**A fixture that disagrees on everything passes a single-disagreement check by accident.** Fixtures
are built so that exactly the intended thing is wrong.

---

## What this system does not do

Stated because a pipeline document that lists only capabilities is a brochure.

- **Nothing detects a Version made by a superseded recipe.** Every invalidation is a timestamp
  comparison, so changing resolution, model or sampler leaves every existing artefact looking
  current.
- **Two characters in one frame is a general limit**, not a bug with a fix. Composition of two
  different characters produces a duplicated third figure often enough to matter, and four separate
  mechanisms have been tested against it.
- **The workflow that produced an artefact is not always recoverable.** Images carry their own graph;
  video does not, and a video whose template was not recorded cannot be reproduced. This is tracked
  as the highest-value gap, because the most valuable output a pipeline produces is the one that was
  nearly right, and that is exactly the case where the graph is needed.
- **There is no automated quality judgement.** Deterministic checks gate what can be checked
  deterministically, such as whether a file exists, whether a figure is present, whether a record
  matches what was sent. Whether a frame is any good is a person's call, and the system is built to
  put that call in front of them quickly rather than to make it for them.
