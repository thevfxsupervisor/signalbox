"""The ONLY way a wedge should render. Rendering and publishing are one operation here.

WHY THIS EXISTS, and it is not a tidy-up.

Geoff, 2026-09-07: *"are your R&D renders no longer publishing to shotgrid, I thought we already
agreed they would all auto publish? make sure they do! you must remember this. don't do it by hand,
make sure the code ensures it happens everytime so when you forget again it doesn't stop
happening."*

He is right and it was worse than one runner. Checked across every wedge script in this folder:
**not one of them called the publish path.** Publishing was a manual `rnd_publish.py` pass I ran
afterwards from memory, so "auto-publish" was never automatic, it was me remembering. It held while
I remembered and stopped the moment I wrote a new runner, which is exactly the failure F050 names:
a rendered-but-unpublished file is indistinguishable from a published one by looking at the disk.

THE DESIGN RULE: a wedge cannot render without publishing, because there is no render function here
that does not publish. Not a reminder, not a convention, not a checklist item. If publishing fails
the cell is marked FAILED and the run stops, because a silently unpublished cell is the thing we are
trying to make impossible.

The backstop for the case where somebody writes a new runner with raw urllib anyway is
`check_unpublished.py`, which compares rendered output against ShotGrid and exits non-zero. Two
layers, because a rule that only works when followed is the thing that just broke.
"""
import json
import os
import sys
import re
import time
import urllib.request
import urllib.error
import urllib.parse

REPO_TOOLS = r"C:/example/genvideo-pipeline/tools"
SERVER = "127.0.0.1:8188"
COMFY_OUT = os.environ.get("COMFYUI_OUTPUT_DIR", r"C:/example/ComfyUI/output")
SCRATCH_ROOT = os.environ.get("GENVIDEO_SCRATCH", r"C:/example/scratch")


def _sg_ready():
    os.environ.setdefault("SHOTGRID_SITE_URL", "https://your-tracker.example.com/")
    os.environ.setdefault("SHOTGRID_SCRIPT_NAME", "mcp-server")
    return bool(os.environ.get("SHOTGRID_SCRIPT_KEY"))


class Lab(object):
    """Holds the ShotGrid connection and publishes each cell as it finishes.

    Constructed ONCE per wedge. Refuses to exist if ShotGrid is unreachable, so a wedge cannot start
    rendering and discover at the end that nothing can be published. Fail before the GPU time, not
    after it.
    """

    def __init__(self, sequence, shot, question, seat="a peer engineer", allow_unpublished=False,
                 server=None, stage_dir=None, wedge=None):
        self.sequence, self.shot_code, self.question, self.seat = sequence, shot, question, seat
        # findings_check.py accepts an EVT/PIG Version code or a WEDGE_ playlist, and R&D cells are
        # neither EVT nor PIG, so the playlist is the ONLY citation a wedge can offer. Derived from
        # the shot code by default so it cannot be forgotten, overridable when a name reads better.
        self.wedge = wedge or ("WEDGE_" + re.sub(r"[^A-Za-z0-9]+", "_", shot).strip("_").upper())
        self.rows, self.published, self.failed = [], [], []
        self.offline = False
        # A remote ComfyUI is the SAME primitive pointed at another host, never a second runner.
        self.server = server or SERVER
        self.remote = self.server != SERVER
        self.stage = stage_dir or os.path.join(SCRATCH_ROOT, "remote_%s" % self.server.replace(":", "_"))
        self._last_prompt_id = None
        if self.remote:
            os.makedirs(self.stage, exist_ok=True)
            print("lab targeting REMOTE ComfyUI at %s; renders staged to %s"
                  % (self.server, self.stage), flush=True)

        if allow_unpublished:
            # Deliberately loud and deliberately awkward to type. The only legitimate use is a
            # local smoke test that produces nothing anyone will cite.
            print("!! PUBLISHING DISABLED BY CALLER. These renders will NOT be citable evidence.",
                  flush=True)
            self.offline = True
            return

        if not _sg_ready():
            raise SystemExit(
                "SHOTGRID_SCRIPT_KEY is not set, so these renders could not be published.\n"
                "Refusing to spend GPU time on cells that would not be citable. Set the key, or\n"
                "pass allow_unpublished=True and accept that the output is not evidence.")
        sys.path.insert(0, REPO_TOOLS)
        import rnd_lab as R
        self.R = R
        self.sg = R._sg(R.sg_connect())
        ep, seqs = R.ensure_lab(self.sg)
        self.shot, _ = R.ensure_experiment(
            self.sg, sequence, shot, question=question,
            seq_row=seqs[sequence], episode_id=ep["id"])
        print("lab ready: episode %s, experiment %s/%s -> shot %s"
              % (ep.get("id"), sequence, shot, self.shot.get("id")), flush=True)

    # ---------------------------------------------------------------- render

    def _post(self, path, payload):
        r = urllib.request.Request("http://%s%s" % (self.server, path),
                                   data=json.dumps(payload).encode(),
                                   headers={"Content-Type": "application/json"})
        body = urllib.request.urlopen(r, timeout=120).read()
        try:
            self._last_prompt_id = json.loads(body).get("prompt_id")
        except Exception:
            self._last_prompt_id = None
        return body

    # 60 seconds was too short and it cost a 24-cell run. A 35 GB model offloading on a 24 GB card
    # leaves the ComfyUI server too busy to answer a queue poll, so the CLIENT times out while the
    # GPU is working perfectly, and the exception is indistinguishable from a dead server. P031
    # registered a timeout as a plausible failure mode; this is that failure mode, and it belonged
    # to the harness rather than to FLUX.2.
    POLL_TIMEOUT = 300

    def _get(self, path):
        last = None
        for _attempt in range(3):
            try:
                return json.loads(urllib.request.urlopen(
                    "http://%s%s" % (self.server, path), timeout=self.POLL_TIMEOUT).read())
            except Exception as e:
                last = e
                time.sleep(5)
        raise last

    def _wait(self, cap=9000, poll=6.0):
        t0 = time.time()
        while time.time() - t0 < cap:
            q = self._get("/queue")
            if not q.get("queue_running") and not q.get("queue_pending"):
                return True
            time.sleep(poll)
        return False

    def _fetch_remote(self, prefix):
        """Pull this cell's render off a remote ComfyUI using its own history and view endpoints.

        Keyed on the prompt_id the server returned for THIS submission, not on a filename or an
        mtime. A remote box may be running other work, and a name-and-time scan on somebody else's
        machine is the kind of check that cannot fail in the direction that matters.
        """
        pid = self._last_prompt_id
        if not pid:
            return None
        try:
            hist = self._get("/history/%s" % pid)
        except Exception:
            return None
        entry = hist.get(pid) or {}
        for _node, out in sorted((entry.get("outputs") or {}).items()):
            for img in (out.get("images") or []) + (out.get("gifs") or []):
                fn = img.get("filename") or ""
                if not fn.startswith(prefix):
                    continue
                q = urllib.parse.urlencode({"filename": fn,
                                            "subfolder": img.get("subfolder") or "",
                                            "type": img.get("type") or "output"})
                url = "http://%s/view?%s" % (self.server, q)
                dest = os.path.join(self.stage, fn)
                data = urllib.request.urlopen(url, timeout=300).read()
                with open(dest, "wb") as fh:
                    fh.write(data)
                return dest
        return None

    def _newest(self, prefix):
        """The file this cell just wrote. Searched by prefix and mtime, newest wins."""
        if self.remote:
            return self._fetch_remote(prefix)
        best, best_t = None, -1
        for n in os.listdir(COMFY_OUT):
            if n.startswith(prefix) and n.lower().endswith((".png", ".mp4", ".webm")):
                p = os.path.join(COMFY_OUT, n)
                t = os.path.getmtime(p)
                if t > best_t:
                    best, best_t = p, t
        return best

    #  works / inert / mixed / refuted / pending. A LIST field in ShotGrid, not prose.
    #  Learned the expensive way: 18 cells rendered, then every publish rejected on a prose
    #  verdict. Validated here so the failure lands BEFORE the GPU time, not after it.
    #  Mirrored from rnd_lab so a wedge fails on the FIRST cell, not after eighteen. I discovered
    #  these one ValueError at a time, which is the expensive way to read a schema: verdict, then
    #  role, then hypothesis, then limits. Read the contract once, assert it up front.
    VERDICTS = ("pending", "works", "inert", "mixed", "refuted", "abandoned")
    ROLES = ("control", "test", "probe")

    def render_cell(self, graph, prefix, code, arm, role, axis,
                    hypothesis, result, verdict, limits, provenance=None, state=None):
        """Render ONE cell and publish it. There is no way to do the first without the second.

        `state` is an optional dict used for resume: if `code` is already in it, this is a no-op.
        """
        bad = None
        if verdict not in self.VERDICTS:
            bad = "verdict %r is not one of %s" % (verdict, list(self.VERDICTS))
        elif role not in self.ROLES:
            bad = "role %r is not one of %s" % (role, list(self.ROLES))
        elif not (hypothesis or "").strip():
            bad = ("no hypothesis. The prediction is registered BEFORE the result, or the "
                   "result can be rationalised into agreeing with whatever we now believe")
        elif not (limits or "").strip():
            bad = "no limits line. That is what stops a wedge being over-quoted later"
        if bad:
            raise SystemExit(
                "cell %s: %s. Refusing to render a cell that cannot be published."
                % (code, bad))

        if state is not None and state.get(code, {}).get("published"):
            # A RESUMED CELL STILL BELONGS IN THE PLAYLIST. `playlist()` REPLACES the version list
            # with `self.published`, so returning here without recording the cell silently drops
            # every resumed cell from the playlist the finding cites. Measured 2026-09-08: P074b
            # part B resumed past its twelve part A cells and left the playlist holding TWO
            # versions, having held fourteen a moment earlier, with both runs reporting success.
            # The resume fix and the replace-not-append policy are each correct alone and wrong
            # together, which is why this is recorded in the code and not only in a finding.
            vid = state[code].get("version_id") or state[code].get("version")
            if vid and not any(c == code for c, _v in self.published):
                self.published.append((code, vid))
            print("SKIP %-40s already rendered and published (kept in the playlist)" % code,
                  flush=True)
            return state[code]

        t0 = time.time()
        try:
            self._post("/prompt", {"prompt": graph})
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:400]
            rec = {"ok": False, "published": False, "error": body}
            print("FAIL %-40s %s" % (code, body), flush=True)
            if state is not None:
                state[code] = rec
            return rec

        ok = self._wait()
        secs = round(time.time() - t0, 1)
        media = self._newest(prefix)
        rec = {"ok": bool(ok), "seconds": secs, "media": media, "published": False}

        if not ok or not media:
            print("FAIL %-40s %s" % (code, "timeout" if not ok else "no output file found"),
                  flush=True)
            if state is not None:
                state[code] = rec
            return rec

        row = {"sequence": self.sequence, "shot": self.shot_code, "question": self.question,
               "code": code, "arm": arm, "role": role, "axis": axis,
               "hypothesis": hypothesis, "result": result, "verdict": verdict,
               "limits": limits, "seat": self.seat, "media_path": media,
               "provenance": provenance or {}}
        self.rows.append(row)

        if self.offline:
            print("%-40s OK %6.1fs  NOT PUBLISHED (disabled)" % (code, secs), flush=True)
            if state is not None:
                state[code] = rec
            return rec

        try:
            v = self.R.publish_cell(
                self.sg, self.shot, code=code, arm=arm, role=role, axis=axis,
                hypothesis=hypothesis, result=result, verdict=verdict, limits=limits,
                seat=self.seat, media_path=media, provenance=row["provenance"])
            if isinstance(v, tuple):          # publish_cell returns (version, created)
                v = v[0]
            vid = v.get("id") if isinstance(v, dict) else v
            rec["published"], rec["version_id"] = True, vid
            self.published.append((code, vid))
            print("%-40s OK %6.1fs  Version %s" % (code, secs, vid), flush=True)
        except Exception as e:
            rec["publish_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
            self.failed.append((code, rec["publish_error"]))
            print("PUBLISH FAILED %-30s %s" % (code, rec["publish_error"]), flush=True)
            print("STOPPING. A cell that rendered but did not publish is exactly the state this "
                  "module exists to prevent, so it is an error and not a warning.", flush=True)
            if state is not None:
                state[code] = rec
            raise SystemExit(2)

        if state is not None:
            state[code] = rec
        return rec

    def playlist(self):
        """Create or update the wedge's playlist and return its code, or None if offline.

        Idempotent on the code, and it REPLACES the version list rather than appending, so a rerun
        of the same wedge does not leave a playlist half from this run and half from the last one.
        """
        if self.offline or not self.published:
            return None
        try:
            proj = self.shot.get("project") or {"type": "Project", "id": 9999}
            pl = self.sg.find_one("Playlist", [["code", "is", self.wedge],
                                               ["project", "is", proj]], ["id"])
            data = {"versions": [{"type": "Version", "id": v} for _c, v in self.published if v],
                    "description": self.question}
            if pl:
                self.sg.update("Playlist", pl["id"], data)
            else:
                data.update({"code": self.wedge, "project": proj})
                pl = self.sg.create("Playlist", data)
            print("playlist %s (id %s) holds %d version(s). CITE THIS in the finding's evidence "
                  "column; a numeric Version id does not satisfy findings_check."
                  % (self.wedge, pl.get("id"), len(data["versions"])), flush=True)
            return self.wedge
        except Exception as e:
            # Not fatal: the cells are already published and citable through the shot. But say so
            # loudly, because a missing playlist means the finding cannot pass the gate.
            print("PLAYLIST FAILED for %s: %s: %s. The cells ARE published; the finding will not "
                  "pass findings_check until a playlist exists."
                  % (self.wedge, type(e).__name__, str(e)[:200]), flush=True)
            return None

    def summary(self):
        print("", flush=True)
        print("published %d cell(s), %d publish failure(s)" % (len(self.published), len(self.failed)),
              flush=True)
        self.playlist()
        if self.failed:
            for c, why in self.failed:
                print("   FAILED %-40s %s" % (c, why), flush=True)
        return not self.failed
