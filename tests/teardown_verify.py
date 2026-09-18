#!/usr/bin/env python3
"""Verify ComfyUI is REALLY gone after a batch. Three independent checks.

The failure this exists for: an orphaned ComfyUI holding a GPU. One was found
on a box in this fleet after 8 days, and one was found idle on a second workstation after
weeks. Both looked fine from every angle nobody was checking.

The checks are deliberately independent, because each can pass while the box is
still occupied:

  1. PORT   nothing listening on 8188. Passes if the process wedged without
            its socket, so it is necessary and nowhere near sufficient.
  2. PROC   no python process whose image path is under the ComfyUI install.
            Passes if a different interpreter holds the GPU.
  3. GPU    nothing ComfyUI-shaped in nvidia-smi --query-compute-apps, AND
            GPU memory back near baseline. This is the one that actually
            catches an orphan, because a process can lose its port and its
            recognisable name and still hold VRAM.

Run with --expect-running to assert the OPPOSITE, which is how you prove the
checks can fail before you trust a clean report.

Usage:
    python teardown_verify.py [--port 8188] [--baseline-mb 1500]
    python teardown_verify.py --expect-running     (canary: assert NOT torn down)

Exit: 0 assertion held, 1 assertion failed.
"""
import argparse
import shutil
import subprocess
import sys


def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def port_listeners(port):
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return None
    out = run([ps, "-NoProfile", "-Command",
               "@(Get-NetTCPConnection -State Listen -LocalPort %d -EA SilentlyContinue).Count" % port])
    try:
        return int(out.strip())
    except ValueError:
        return None


def comfy_processes():
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return None
    out = run([ps, "-NoProfile", "-Command",
               "@(Get-Process python -EA SilentlyContinue | "
               "Where-Object { $_.Path -like '*ComfyUI*' }).Count"])
    try:
        return int(out.strip())
    except ValueError:
        return None


def gpu_state():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, None
    apps = run([exe, "--query-compute-apps=process_name", "--format=csv,noheader"])
    comfy = [a for a in apps.splitlines() if "comfyui" in a.lower()]
    mem = run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"]).strip()
    try:
        used = int(mem)
    except ValueError:
        used = None
    return comfy, used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--baseline-mb", type=int, default=1500,
                    help="idle desktop draw; measured ~860 MiB on the workstation")
    ap.add_argument("--expect-running", action="store_true")
    ns = ap.parse_args()

    listeners = port_listeners(ns.port)
    procs = comfy_processes()
    comfy_apps, used = gpu_state()

    print("  1 PORT  listeners on %d      : %s" % (ns.port, listeners))
    print("  2 PROC  ComfyUI python procs : %s" % procs)
    print("  3 GPU   ComfyUI compute apps : %s" % (len(comfy_apps) if comfy_apps is not None else "?"))
    print("          GPU memory used      : %s MiB (baseline %d)" % (used, ns.baseline_mb))

    clean = (listeners == 0 and procs == 0
             and (comfy_apps is not None and len(comfy_apps) == 0)
             and (used is not None and used <= ns.baseline_mb))

    if ns.expect_running:
        if clean:
            print("CANARY FAILED: reported CLEAN while ComfyUI is supposed to be running.")
            print("               These checks cannot detect an orphan. Do not trust them.")
            return 1
        print("canary: correctly reports NOT torn down while ComfyUI is up")
        print("        -> a clean report from these checks is meaningful")
        return 0

    if clean:
        print("TORN DOWN: port free, no process, nothing on the GPU, memory at baseline")
        return 0
    print("NOT CLEAN: something is still holding this box - investigate before dispatching")
    return 1


if __name__ == "__main__":
    sys.exit(main())
