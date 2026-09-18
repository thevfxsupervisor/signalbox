#!/usr/bin/env python3
"""Pre-dispatch GPU contention check. Its job is to be ABLE TO SAY NO.

This workstation is a shared box, not a dedicated render node. Fusion Render Node runs on
this same GPU, and an artist may be sitting at it. A generative job that starts
regardless will either OOM against someone else's allocation or, worse, succeed
while making their work crawl. The farm is at its licence ceiling precisely so
this box can be a spare-capacity worker, and spare capacity means yielding.

Three outcomes, deliberately distinct:

  PROCEED (0)  the GPU is idle enough; dispatch.
  DEFER  (10)  someone else is using it. Come back later. NOT an error: a
               scheduler should retry, not alarm.
  SKIP   (20)  we cannot tell (no nvidia-smi, unreadable output). Refusing to
               guess is the safe answer, because "I could not check" and "it is
               free" are the same silence.

A guard nobody has watched say no is not a guard, so --self-test drives it
against a threshold the current machine must violate and asserts DEFER.

Usage:
    python gpu_guard.py [--need-vram-mb N] [--max-util PCT] [--max-used-mb N]
    python gpu_guard.py --self-test
"""
import argparse
import shutil
import subprocess
import sys

PROCEED, DEFER, SKIP = 0, 10, 20

# Processes that mean somebody else's work is on this GPU. ComfyUI is NOT here:
# our own worker is what we are deciding whether to start.
FOREIGN = ("fusionrendernode", "octane", "resolve", "blender", "maya", "houdini",
           "nuke", "unrealeditor", "cinema 4d", "c4d")


def smi(query, extra=None):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    cmd = [exe, "--query-%s" % query[0], query[1], "--format=csv,noheader,nounits"]
    if extra:
        cmd += extra
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def read_state():
    gpu = smi(("gpu", "memory.total,memory.used,utilization.gpu"))
    if not gpu:
        return None
    try:
        total, used, util = [int(x.strip()) for x in gpu[0].split(",")]
    except (ValueError, IndexError):
        return None
    apps = smi(("compute-apps", "process_name")) or []
    return {"total": total, "used": used, "free": total - used, "util": util, "apps": apps}


def decide(st, need_vram, max_util, max_used):
    if st is None:
        return SKIP, "cannot read nvidia-smi - refusing to guess (unknown is not idle)"

    foreign = [a for a in st["apps"] if any(f in a.lower() for f in FOREIGN)]
    names = ", ".join(sorted({a.split("\\")[-1] for a in foreign})) if foreign else ""

    # PRESENCE IS NOT CONTENTION. Fusion Render Node sits resident on this box
    # permanently, listed in compute-apps at 1% utilisation and no measurable
    # allocation. Deferring on presence alone made this guard return DEFER
    # forever, which is the same as having no guard: nobody can distinguish
    # "correctly yielding" from "broken and always refusing". The decision is
    # made on MEASURED load below; a resident neighbour is reported, not obeyed.
    # (nvidia-smi reports per-process memory as N/A for these, so per-process
    # attribution is not available here and whole-GPU numbers are what we have.)

    if st["free"] < need_vram:
        return DEFER, ("only %d MiB free, job needs %d MiB" % (st["free"], need_vram))

    if st["util"] > max_util:
        return DEFER, "GPU utilisation %d%% exceeds %d%%" % (st["util"], max_util)

    if st["used"] > max_used:
        return DEFER, ("%d MiB already in use, threshold %d MiB" % (st["used"], max_used))

    note = ("%d MiB free of %d, util %d%%" % (st["free"], st["total"], st["util"]))
    if names:
        note += " (resident but idle: %s - will DEFER the moment it actually loads)" % names
    return PROCEED, note


def main():
    ap = argparse.ArgumentParser()
    # 2026-08-29: was 11000, tuned for Wan2.2 TI2V-5B (peak 10.93 GB). V2 does not
    # run the 5B model - it runs A14B Q4_K_S, and the old default had become
    # STRUCTURALLY UNREACHABLE on this box: 12282 MiB total against a ~1320 MiB
    # idle desktop baseline leaves ~10962 MiB free, so 11000 could never pass and
    # deferred a real batch on a 38 MiB margin.
    #
    # 9500 is justified by measurement, not roundness: A14B T2V stills peaked at
    # 7868 MiB, and the act B run of 40 i2v shots completed cleanly under it.
    # Note this is a PRE-FLIGHT free-VRAM check, not a cap - the Phase 1 wedge
    # peaked at 10967 MiB DURING a run, which is the card being used as intended.
    # Raising this above what the machine can ever offer does not buy safety, it
    # just guarantees a defer.
    ap.add_argument("--need-vram-mb", type=int, default=9500,
                    help="A14B Q4_K_S pre-flight free-VRAM floor; see comment above for why not 11000")
    ap.add_argument("--max-util", type=int, default=25)
    ap.add_argument("--max-used-mb", type=int, default=2000)
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args()

    st = read_state()

    if ns.self_test:
        print("machine now: %s" % ("unreadable" if st is None else
              "%d MiB used of %d, util %d%%, apps=%d"
              % (st["used"], st["total"], st["util"], len(st["apps"]))))
        # Drive it against a threshold this machine MUST violate. If a guard
        # cannot be made to say no on demand, its yes is worthless.
        code, why = decide(st, need_vram=10 ** 9, max_util=100, max_used=10 ** 9)  # impossible VRAM demand
        if code != DEFER:
            print("SELF-TEST FAILED: guard returned %d for an impossible VRAM demand." % code)
            return 1
        print("self-test: impossible VRAM demand -> DEFER (%s)" % why)
        code2, why2 = decide(None, 1, 100, 10 ** 9)
        if code2 != SKIP:
            print("SELF-TEST FAILED: unreadable GPU did not return SKIP.")
            return 1
        print("self-test: unreadable nvidia-smi -> SKIP (%s)" % why2)
        print("self-test: guard can say NO, so a PROCEED from it means something")
        return 0

    code, why = decide(st, ns.need_vram_mb, ns.max_util, ns.max_used_mb)
    print({PROCEED: "PROCEED", DEFER: "DEFER", SKIP: "SKIP"}[code] + ": " + why)
    return code


if __name__ == "__main__":
    sys.exit(main())
