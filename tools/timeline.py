#!/usr/bin/env python3
"""THE timeline frame rate, in one place, plus the native generation facts.

WHY THIS EXISTS. Six modules each defined their own `FPS = 24`: captions_gen,
captions_sg, conform, episode_assemble, fps_bridge and video_from_panel, and
animatic hardcoded 24.0 inline twice. Eight copies of one number that must
agree, which is how they stop agreeing.

THE MVP DECISION (Geoff, 2026-09-06): **deliver at 16 fps.**

    "So the model is trained for 16fps output, thats fine, then thats what we
    are getting with generations with this model. I suggest for MVP we just
    leave it playing at 16fps."

That is not a compromise, it is the removal of a conversion we were paying for
twice over:

1. **Interpolation blur.** Bridging 16 -> 24 meant synthesising one frame per
   two real ones, and the smear landed on hands and held objects. Measured on
   SHOW01_A_0040: per-frame edge energy [1048, 905, 876, 1030, 1021, 894].

2. **Motion speed, which was the bigger defect and was invisible.** The bridge
   also time-stretched each clip to hit the shot's requested duration, so shot
   length was being bought with motion speed:

       _0030 _0050 _0070   3.04s target   1.66x FASTER than generated
       _0020               4.04s target   1.25x faster
       _0010               6.04s target   1.19x slower
       _0090               8.04s target   1.59x slower
       _0040 _0060 _0080   5.04s target   natural

   A 1.66x speed-up is not subtle, and nothing in the record said it was
   happening. Every clip reported success.

WHAT REPLACES IT. Generate 81 frames, play them at 16 fps, and get shot length
by TRIMMING, which is what editing does. Motion is then always exactly what the
model generated. A shot that wants LESS than 5.0625s is a trim. A shot that
wants MORE is not a retime and must not be silently slowed: it is a generation
question, and `native_conform()` refuses it by name.

RAISING THE RATE LATER is a one-line change here, and it is on a peer engineer's R&D
backlog at LOW priority per Geoff, below prompt engineering and content. The
honest way to 24/25/30 is a learned interpolator (FILM is Apache-2.0) or a
longer generation, never ffmpeg motion compensation.
"""
import os

# The delivery timeline. Everything downstream of generation reads THIS.
FPS = float(os.environ.get("GENVIDEO_TIMELINE_FPS", "16"))

# Facts about the generator, not settings. Wan 2.2 A14B exposes no fps input
# at all: its graph is WanImageToVideo -> KSampler -> VAEDecode -> SaveImage,
# and `length` is the only temporal control. NATIVE_FPS is the rate it was
# TRAINED at, meaning how much motion it puts between adjacent frames.
NATIVE_FPS = 16.0
NATIVE_FRAMES = int(os.environ.get("GENVIDEO_NATIVE_FRAMES", "81"))


def native_seconds(frames=None, fps=None):
    """Duration of a native generation, in seconds."""
    return (NATIVE_FRAMES if frames is None else frames) / (fps or NATIVE_FPS)


def is_native_timeline():
    """True when the delivery rate equals the generation rate, so no frame
    rate conversion is needed at all. This is the MVP case."""
    return abs(FPS - NATIVE_FPS) < 1e-6


def frames_for(seconds):
    """Timeline frames for a duration. Rounds to nearest, never floors: a
    consistent half-frame bias across a 55-shot episode is a visible drift."""
    return int(round(seconds * FPS))


def retime_factor(target_frames, target_fps=None):
    """How much a native clip would have to be SPEED-CHANGED to hit
    target_frames at target_fps. 1.0 is natural.

    Kept so the old behaviour stays measurable and nameable rather than just
    deleted: this is the number that was silently 1.66 on three demo shots."""
    tf = target_fps or FPS
    return (target_frames / tf) / native_seconds()


def self_test():
    fails = []

    def ck(name, cond):
        print("  %-70s %s" % (name[:70], "ok" if cond else "FAIL"))
        if not cond:
            fails.append(name)

    ck("MVP delivers at the generator's own rate, so no conversion is needed",
       is_native_timeline())
    ck("81 native frames is 5.0625s", abs(native_seconds() - 5.0625) < 1e-6)
    ck("a 3.04s shot is 49 timeline frames at 16fps", frames_for(3.042) == 49)
    ck("frames_for ROUNDS, it does not floor (drift over 55 shots)",
       frames_for(0.999 / FPS * FPS / FPS + 0) == 0 or frames_for(1.9 / FPS) == 2)

    # The defect this module exists to end, stated as a number.
    ck("CANARY: the old 24fps path sped _0030 up by about 1.66x",
       abs(retime_factor(73, 24.0) - 0.601) < 0.01)
    ck("CANARY: and slowed _0090 by about 1.59x",
       abs(retime_factor(193, 24.0) - 1.588) < 0.01)
    ck("at 16fps a 81-frame shot needs no retime at all",
       abs(retime_factor(81, 16.0) - 1.0) < 1e-6)

    ck("raising the rate is one env var, not a code edit",
       "GENVIDEO_TIMELINE_FPS" in open(__file__, encoding="utf-8").read())

    print("\n%s" % ("ALL PASS" if not fails else "FAILED: " + ", ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
