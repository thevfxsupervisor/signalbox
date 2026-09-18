#!/usr/bin/env python3
"""Codepoint scan for em-dash (and friends) in authored text files.

Why a codepoint scan and not `grep`: an em-dash is U+2014, a multi-byte
sequence in UTF-8. A bash grep for it depends on the shell's encoding, the
locale, and how the pattern itself survived being typed into a command line
on a box whose console is cp1252. Any of those can make a real em-dash go
unnoticed, and a check that quietly cannot see the thing it looks for is
worse than no check. Decoding the file and comparing integer codepoints has
none of those failure modes.

The check is only trustworthy if it has been SEEN to fail, so --canary plants
a known-bad string and asserts the detector fires on it. A clean report from a
detector that has never fired is not evidence of anything.

Usage:
    python no_emdash.py <path> [<path> ...]     scan files and/or directories
    python no_emdash.py --canary                prove the detector can fail
    python no_emdash.py --canary <path> ...     canary first, then scan

Exit codes: 0 clean, 1 found something, 2 canary failed (detector broken).
"""
import sys
import os

# U+2014 EM DASH is the banned one. The others are reported because they are
# the characters a model reaches for next when told not to use an em-dash, and
# finding them is useful even though the hard rule names only U+2014.
BANNED = {
    0x2014: "EM DASH",
}
SUSPECT = {
    0x2013: "EN DASH",
    0x2012: "FIGURE DASH",
    0x2015: "HORIZONTAL BAR",
    0x2212: "MINUS SIGN",
}

TEXT_EXT = {".md", ".txt", ".py", ".json", ".ps1", ".bat", ".yml", ".yaml", ".cfg", ".ini"}


def scan_text(text):
    """Return (banned_hits, suspect_hits) as lists of (line_no, col, cp, name)."""
    banned, suspect = [], []
    for lineno, line in enumerate(text.splitlines(), 1):
        for col, ch in enumerate(line, 1):
            cp = ord(ch)
            if cp in BANNED:
                banned.append((lineno, col, cp, BANNED[cp]))
            elif cp in SUSPECT:
                suspect.append((lineno, col, cp, SUSPECT[cp]))
    return banned, suspect


def iter_files(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in names:
                    if os.path.splitext(n)[1].lower() in TEXT_EXT:
                        yield os.path.join(root, n)
        elif os.path.isfile(p):
            yield p


def canary():
    """Prove the detector fires. Exit 2 if it does not."""
    # Build the character by CODEPOINT. If this file contained a literal U+2014
    # it would flag itself on every run, and a check that always fails is as
    # useless as one that never does.
    planted = "a line with an em dash " + chr(0x2014) + " right here"
    banned, _ = scan_text(planted)
    if not banned:
        print("CANARY FAILED: detector did NOT fire on a planted U+2014.")
        print("               A clean scan from this build means NOTHING. Fix the detector.")
        return False
    print("canary: detector fired on planted U+2014 at col %d - a clean scan is now meaningful"
          % banned[0][1])
    clean = "a line with no dash at all"
    b2, _ = scan_text(clean)
    if b2:
        print("CANARY FAILED: detector fired on CLEAN text (false positive).")
        return False
    print("canary: detector silent on clean text - no false positive")
    return True


def main(argv):
    args = list(argv[1:])
    run_canary = "--canary" in args
    if run_canary:
        args.remove("--canary")
        if not canary():
            return 2
        if not args:
            return 0

    if not args:
        sys.stderr.write(__doc__)
        return 2

    missing = [a for a in args if not os.path.exists(a)]
    if missing:
        print("REFUSED: path(s) do not exist: %s" % ", ".join(missing))
        print("(a scan that cannot see its target must not report clean)")
        return 2

    files = sorted(set(iter_files(args)))
    if not files:
        print("no text files found in: %s" % ", ".join(args))
        return 0

    total_banned = 0
    total_suspect = 0
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print("SKIP %s (%s)" % (path, exc))
            continue
        banned, suspect = scan_text(text)
        for lineno, col, cp, name in banned:
            print("BANNED  %s:%d:%d  U+%04X %s" % (path, lineno, col, cp, name))
        for lineno, col, cp, name in suspect:
            print("suspect %s:%d:%d  U+%04X %s" % (path, lineno, col, cp, name))
        total_banned += len(banned)
        total_suspect += len(suspect)

    print("")
    print("scanned %d file(s): %d banned, %d suspect" % (len(files), total_banned, total_suspect))
    return 1 if total_banned else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
