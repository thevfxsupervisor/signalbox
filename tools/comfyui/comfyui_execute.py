"""Run one ComfyUI generation as a Royal Render Execute job.

Talks to an ALREADY-RUNNING local ComfyUI server (http://127.0.0.1:8188 by
default). This script never starts or stops ComfyUI: a generative-AI server
takes real time to load its models into VRAM, so starting one per job would
make every job pay that cost and would fight itself if two jobs landed on the
same node at once. Keep ComfyUI running as its own persistent service on the
render node (a Scheduled Task at startup is the simplest form of that) and
let RR jobs be short-lived clients against it, the same relationship an
Execute job normally has with any always-on local service.

## The workflow file is a TEXT TEMPLATE, not a graph this script understands

This script does not know ComfyUI's node graph and does not try to. It takes
a workflow exported in API format (ComfyUI's UI: Save (API format), NOT the
plain "Save" which is a different schema) and treats it as TEXT: placeholders
like {seed}, {wedge}, {output_prefix} are substituted before the file is even
parsed as JSON. This works with any workflow, because the substitution has no
opinion about which nodes exist; it only has to produce valid JSON afterward,
which is checked before anything is sent to ComfyUI.

## Why judgment is by OUTPUT, never by the HTTP response or a bare timeout

POSTing to /prompt only proves ComfyUI accepted the job into ITS OWN queue,
not that it ran or finished; a 200 here is not a result. This script polls
/history/<prompt_id> until ComfyUI reports the prompt done, reads the actual
error out of a failed prompt's status messages, and then checks that every
output file the workflow was supposed to produce actually exists on disk and
is non-empty. A farm client that trusts the accept-response is trusting the
wrong signal at exactly the layer where it matters.

## A real freeze-detector hazard, not yet resolved

Royal Render's Execute app has its own freeze detector (Frozen_Minutes /
Frozen_MinCoreUsage in the client's render-app config), keyed on the CPU usage
of the LAUNCHED process, i.e. this script, not the separate long-running
ComfyUI server it talks to over localhost. This script's own CPU usage while
polling is negligible by design, so a generation that runs longer than the
farm's configured Frozen_Minutes window can look frozen to RR and be aborted
mid-generation even though ComfyUI is actively working. There is no code fix
for this on the script side; the farm-side fix is a render-app config with a
longer Frozen_Minutes tuned for generative jobs (see the studio-specific
deployment notes, not shipped here since farm config is not this repo's
business).

Usage (matches RR's Execute app convention: the "scene" IS this script,
called as `python comfyui_execute.py <seq_start> <seq_end> <seq_step> ...`):

    python comfyui_execute.py \
        --workflow path/to/workflow_api.json \
        --output-dir //server/share/project/shot/publish \
        --output-stem shot010_v001 \
        --wedge 0 \
        --seed-base 1000 \
        --comfy-host 127.0.0.1 --comfy-port 8188 \
        --expect-outputs 1 \
        --timeout 3600
"""

import argparse
import json
import os
import re
import sys
import time
import uuid

try:
    import urllib.request
    import urllib.error
    import urllib.parse
except ImportError:  # pragma: no cover - py2 fallback, unused on this farm
    urllib = None

# Hosts for which "the output directory" and "the machine running this script"
# are the same filesystem. Anything else is a REMOTE server, and a path-based
# check would be inspecting the wrong disk. See verify_outputs().
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "")


def log(msg):
    print(msg, flush=True)


def _http_json(url, data=None, timeout=15):
    body = None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_bytes(url, timeout=120):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def wait_for_comfy(host, port, timeout=10):
    """Fail fast and loud if ComfyUI is not already up. Starting it is not
    this script's job (see the module docstring); a missing server here means
    the persistent service is down and someone needs to know that directly,
    not have this job silently hang waiting for something that will never
    start itself."""
    url = "http://%s:%s/system_stats" % (host, port)
    try:
        _http_json(url, timeout=timeout)
        return True
    except Exception as exc:
        log("FAIL: ComfyUI is not reachable at %s (%s). It must already be "
            "running as a persistent service; this script does not start it."
            % (url, exc))
        return False


def render_template(text, subs):
    """Fill {NAME} placeholders in a workflow TEXT template and return TEXT
    that is still valid JSON afterward.

    THE BUG THIS CLOSES (found live, 2026-08-29, composing real panels: two
    of the first three shots died here before ever reaching the GPU). The
    old implementation did a bare str.replace of every placeholder with
    str(value), regardless of whether the placeholder sat inside an existing
    JSON string ("text": "{prompt}") or stood bare as a JSON number
    ("steps": {steps}). That is fine for a value with no JSON-special
    characters (a filename, an int) and silently broken for anything else -
    and the substituted values here are largely prose pulled from
    Shot.sg_script_beat, i.e. real screenplay text: quotes, apostrophes,
    newlines and backslashes are the NORM, not an edge case. A value
    containing a double quote breaks the surrounding string; a literal
    newline is an invalid control character inside a JSON string; a
    backslash starts an invalid escape. Hand-escaping one character class
    (e.g. quotes only) just moves the failure to the next one seen in
    production - the fix has to be general.

    THE FIX: this still can't parse-then-substitute the whole template as
    JSON up front, because a bare placeholder like {steps} is not valid JSON
    on its own (a `{...}` with no quoted key is not a legal object member),
    so the template genuinely is not valid JSON until every placeholder is
    filled. Instead, each placeholder is classified by its OWN immediate
    context in the template text, not by guessing the value's type:

      - QUOTED, i.e. the template has "{name}" (the placeholder is the
        entire content of a JSON string) -> replace the quoted token,
        quotes included, with json.dumps(str(value)). json.dumps() is a
        real JSON string encoder, so it escapes every character JSON cares
        about (quote, backslash, control chars incl. newline) correctly,
        not just the ones a hand-written .replace() chain happened to
        anticipate.
      - BARE, i.e. the template has {name} with no surrounding quotes (a
        JSON number position, e.g. "steps": {steps}) -> substitute
        str(value) verbatim, unquoted, exactly as before. This path is
        unchanged because these values are already-safe numeric/boolean
        literals produced by this codebase, never free text.

    Every {NAME} placeholder across workflows/*.api.json is one or the
    other in full - never a placeholder embedded inside a larger string
    alongside literal text - so this two-case rule covers every real
    template in this repo (verified by inspection, not assumed)."""
    for key, value in subs.items():
        token = "{%s}" % key
        quoted_token = '"%s"' % token
        if quoted_token in text:
            text = text.replace(quoted_token, json.dumps(str(value)))
        if token in text:
            text = text.replace(token, str(value))
    return text


def load_and_patch_workflow(path, subs):
    # An unreadable workflow is the single most likely operator error here (a
    # mistyped path, or a path that resolves on the submitting machine but not
    # on the render node). Left unguarded it surfaces as a raw traceback, which
    # in a farm log is a much worse signature than a one-line reason: it reads
    # as "the integration crashed" rather than "you gave me a bad path".
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise SystemExit(
            "FAIL: cannot read the workflow file %r (%s). Check the path is "
            "correct AND that it resolves on the machine running this job, "
            "not just on the one that submitted it." % (path, exc))
    patched = render_template(raw, subs)

    # Name the unsubstituted placeholders BEFORE trying to parse. Left alone,
    # a leftover {width} makes the JSON invalid and the operator gets a parse
    # error pointing at a character offset, which says nothing about the real
    # cause: a --set they forgot to pass. The pattern deliberately matches only
    # bare {identifier}, so real JSON braces are not flagged.
    leftover = sorted(set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", patched)))
    if leftover:
        raise SystemExit(
            "FAIL: the workflow still contains unsubstituted placeholder(s): "
            "%s. Pass each one as --set NAME=VALUE. (This script only fills in "
            "{wedge}, {seed} and {output_prefix} by itself.)"
            % ", ".join("{%s}" % name for name in leftover))

    try:
        return json.loads(patched)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "FAIL: workflow is not valid JSON after substitution (%s). "
            "Check that every {placeholder} substitutes into a JSON-safe "
            "value (e.g. a string placeholder used as a bare number needs "
            "its own quotes in the template)." % exc)


def submit_prompt(host, port, workflow, client_id):
    """POST the patched workflow to ComfyUI's /prompt.

    Also asks ComfyUI to embed a 'workflow' PNG text chunk on every image
    this prompt saves, via extra_data.extra_pnginfo -- ComfyUI's SaveImage
    node writes a 'prompt' chunk unconditionally from `prompt` itself, but
    only writes a 'workflow' chunk if the caller supplies one this way; the
    web UI always does (that's how dragging a PNG back into ComfyUI loads
    it), a bare script POST does not get it for free. Measured 2026-09-08:
    0 of 1474 published panel PNGs carried a 'workflow' chunk before this,
    all 1474 traced to this one submission call. This is ComfyUI's own
    extension point, not a private format; the value handed to it is the
    same API-format graph already sent as `prompt`, since that is the only
    workflow document this script has."""
    url = "http://%s:%s/prompt" % (host, port)
    payload = {
        "prompt": workflow,
        "client_id": client_id,
        "extra_data": {"extra_pnginfo": {"workflow": workflow}},
    }
    try:
        resp = _http_json(url, data=payload)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit("FAIL: ComfyUI rejected the workflow (HTTP %s): %s"
                         % (exc.code, detail[:2000]))
    if "prompt_id" not in resp:
        raise SystemExit("FAIL: /prompt response had no prompt_id: %r" % resp)
    return resp["prompt_id"]


def wait_for_history(host, port, prompt_id, timeout, poll_every=5):
    """Poll until ComfyUI's history shows this prompt as finished, one way or
    the other. Returns the history entry. Raises SystemExit with the real
    ComfyUI-reported error on failure, never a bare 'timed out'."""
    url = "http://%s:%s/history/%s" % (host, port, prompt_id)
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            hist = _http_json(url, timeout=15)
        except Exception as exc:
            log("  (history poll failed, retrying: %s)" % exc)
            time.sleep(poll_every)
            continue
        entry = hist.get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status != last_status:
                log("  status: %s" % json.dumps(status)[:300])
                last_status = status
            if status.get("completed"):
                return entry
            if status.get("status_str") == "error":
                messages = status.get("messages", [])
                raise SystemExit("FAIL: ComfyUI reported an error: %s"
                                 % json.dumps(messages)[:2000])
        time.sleep(poll_every)
    raise SystemExit(
        "FAIL: prompt %s did not complete within %ss. This is either a "
        "genuinely slow generation (raise --timeout) or a hung ComfyUI "
        "process (check its own log on the node)." % (prompt_id, timeout))


def cache_report(history_entry):
    """-> (cached_node_ids, elapsed_seconds_or_None, ran_anything).

    WHY THIS EXISTS. a peer engineer, 2026-09-06, caught twice three weeks apart:

      "A cached re-run reads as a PERFECT result. A determinism test returned
       0.000 mean difference and max-pixel 0, apparently solved. It was a
       ComfyUI cache hit; only the output node's filename had changed, so every
       upstream node was served from cache and the second render took 4.29s
       against the first's 123.51s. The tell both times was an implausible
       ELAPSED TIME, never the analysis."

    A before/after comparison on a cached pipeline is not a comparison. The
    numbers are internally consistent and confidently wrong, and no amount of
    staring at them helps.

    But we do not have to infer it from timing: **ComfyUI states it outright**.
    Its history carries an `execution_cached` message naming every node it
    served from cache, and `execution_start`/`execution_success` timestamps.
    So this reads the fact rather than estimating it, and the timing is kept
    only as the human-legible corroboration a peer engineer used.
    """
    status = (history_entry or {}).get("status", {}) or {}
    cached, t0, t1 = [], None, None
    for msg in status.get("messages", []) or []:
        if not isinstance(msg, (list, tuple)) or len(msg) < 2:
            continue
        kind, body = msg[0], msg[1] if isinstance(msg[1], dict) else {}
        if kind == "execution_cached":
            cached.extend(body.get("nodes") or [])
        elif kind == "execution_start":
            t0 = body.get("timestamp")
        elif kind in ("execution_success", "execution_error"):
            t1 = body.get("timestamp")
    elapsed = None
    if t0 is not None and t1 is not None:
        try:
            elapsed = (float(t1) - float(t0)) / 1000.0
        except (TypeError, ValueError):
            elapsed = None
    return cached, elapsed, bool(elapsed and elapsed > 0)


def warn_if_cached(history_entry, sampler_hint=("KSampler", "Sampler"), log=log):
    """Say loudly when a 'render' was served from cache. -> True if suspicious.

    Deliberately a WARNING and not a refusal: a cache hit is legitimate when you
    are iterating on an output filename, and only misleading when the numbers
    are then compared. The failure is silent, so the fix is to make it loud."""
    cached, elapsed, _ = cache_report(history_entry)
    if not cached:
        if elapsed is not None:
            log("  executed in %.1fs, no nodes served from cache" % elapsed)
        return False
    log("  CACHE: ComfyUI served %d node(s) from cache%s"
        % (len(cached), (" and finished in %.1fs" % elapsed) if elapsed else ""))
    log("  If you are COMPARING this render against another, that comparison may")
    log("  be meaningless: a cache hit is indistinguishable from a perfect result")
    log("  by the numbers alone. Restart ComfyUI or change an upstream input.")
    return True


def collect_output_items(history_entry):
    """Pull the output files ComfyUI itself claims it wrote out of the history
    entry. This is a CLAIM, not evidence; the verify_* functions below are what
    turn it into evidence."""
    items = []
    outputs = history_entry.get("outputs", {})
    for node_id, node_out in outputs.items():
        for key in ("images", "gifs", "videos", "audio"):
            for item in node_out.get(key, []) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                items.append({
                    "filename": filename,
                    "subfolder": item.get("subfolder", "") or "",
                    "type": item.get("type", "output") or "output",
                })
    return items


def verify_outputs_disk(items, comfy_output_dir):
    """Confirm each claimed output is really on disk and non-empty. ComfyUI
    reporting 'completed' is not proof a file exists; only the filesystem is.

    Only valid when ComfyUI's output directory is visible to THIS machine.
    main() enforces that; see the guard there for why it is not optional."""
    missing, empty, ok = [], [], []
    for item in items:
        path = os.path.join(comfy_output_dir, item["subfolder"], item["filename"])
        if not os.path.isfile(path):
            missing.append(path)
        elif os.path.getsize(path) == 0:
            empty.append(path)
        else:
            ok.append(path)

    if missing:
        raise SystemExit("FAIL: ComfyUI reported completion but these output "
                         "files do not exist: %s" % missing)
    if empty:
        raise SystemExit("FAIL: these output files exist but are empty: %s" % empty)
    return ok


def verify_outputs_api(items, host, port, timeout=120):
    """Confirm each claimed output by FETCHING IT BACK from ComfyUI's /view
    endpoint and checking real bytes came out.

    This exists for the remote-host case, where the generating machine's disk
    is not visible here. It is deliberately a byte-level fetch rather than a
    HEAD or an existence probe: /view returning 200 with a zero-length body is
    exactly the silent-empty-output failure the disk check was written to
    catch, so checking the status code alone would reintroduce it."""
    missing, empty, ok = [], [], []
    for item in items:
        query = urllib.parse.urlencode({
            "filename": item["filename"],
            "subfolder": item["subfolder"],
            "type": item["type"],
        })
        url = "http://%s:%s/view?%s" % (host, port, query)
        label = "%s/%s" % (item["subfolder"], item["filename"]) if item["subfolder"] \
            else item["filename"]
        try:
            blob = _http_bytes(url, timeout=timeout)
        except Exception as exc:
            missing.append("%s (%s)" % (label, exc))
            continue
        if not blob:
            empty.append(label)
        else:
            ok.append("%s [%d bytes]" % (label, len(blob)))

    if missing:
        raise SystemExit("FAIL: ComfyUI reported completion but these outputs "
                         "could not be fetched back from /view: %s" % missing)
    if empty:
        raise SystemExit("FAIL: these outputs fetched back as zero bytes: %s" % empty)
    return ok


def verify_outputs(history_entry, comfy_output_dir, expect_outputs,
                   mode="disk", host="127.0.0.1", port="8188"):
    """Turn ComfyUI's completion claim into verified evidence, by whichever
    route can actually see the files."""
    items = collect_output_items(history_entry)

    if mode == "api":
        ok = verify_outputs_api(items, host, port)
    else:
        ok = verify_outputs_disk(items, comfy_output_dir)

    if expect_outputs is not None and len(ok) != expect_outputs:
        raise SystemExit(
            "FAIL: expected %d output file(s), history reports %d: %s"
            % (expect_outputs, len(ok), ok))
    if not ok:
        raise SystemExit("FAIL: ComfyUI reported completion with zero output "
                         "files found in the history entry. Check the "
                         "workflow has a Save node the history API reports.")
    return ok


# ---------------------------------------------------------------- self-test
def _cache_canaries():
    """The cache trap, asserted. a peer engineer was caught by it twice, three weeks apart,
    noticing only because the elapsed time looked wrong. This reads ComfyUI's own
    execution_cached message rather than guessing from timing."""
    fresh = {"status": {"messages": [
        ["execution_start", {"timestamp": 1000}],
        ["execution_success", {"timestamp": 124000}]]}}
    nodes, elapsed, ran = cache_report(fresh)
    assert nodes == [] and abs(elapsed - 123.0) < 0.01 and ran,         "a fresh render should report no cached nodes: %r %r" % (nodes, elapsed)

    hit = {"status": {"messages": [
        ["execution_start", {"timestamp": 1000}],
        ["execution_cached", {"nodes": ["1", "2", "3", "10"]}],
        ["execution_success", {"timestamp": 5290}]]}}
    nodes, elapsed, _ = cache_report(hit)
    assert nodes == ["1", "2", "3", "10"] and abs(elapsed - 4.29) < 0.01,         "a cache hit must be DETECTED from ComfyUI's own message: %r" % (nodes,)

    seen = []
    assert warn_if_cached(hit, log=seen.append) is True and any("CACHE" in x for x in seen),         "warn_if_cached must be loud on a cache hit: %r" % (seen,)
    assert warn_if_cached(fresh, log=[].append) is False,         "warn_if_cached must not fire on a real render, or it is decoration"
    assert cache_report({"status": {"messages": [["execution_start"], "junk", None]}})[0] == [],         "a malformed history must not crash the detector"
    print("SELF-TEST: cache canaries PASS -- a 4.29s cache hit is detected from "
          "ComfyUI's own execution_cached message, and a 123s real render is not "
          "flagged.")


def _workflow_metadata_canaries():
    """Proves two things about the 2026-09-08 missing-'workflow'-chunk fix,
    offline (no ComfyUI, no GPU, no network):

    1. submit_prompt() actually sends extra_data.extra_pnginfo.workflow --
       captured by faking _http_json, never by inspecting the module's own
       constants (a literal 'workflow' string throughout, so a silent
       rename or removal of the key cannot pass this).
    2. The presence check this proves matters CAN go red: a scratch PNG
       with a real 'workflow' chunk is re-saved through PIL with no
       pnginfo at all (the exact shape of the original defect) and the
       check on that stripped copy must report the chunk absent. If it
       doesn't, the canary is decoration, not a test."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    import tempfile

    # 1. submit_prompt() uses ComfyUI's real extension point.
    captured = {}

    def _fake_http_json(url, data=None, timeout=15):
        captured["url"] = url
        captured["data"] = data
        return {"prompt_id": "canary-id"}

    module = sys.modules[__name__]
    real_http_json = module._http_json
    module._http_json = _fake_http_json
    try:
        submit_prompt("127.0.0.1", 8188, {"1": {"class_type": "CanaryNode"}},
                      "cid-canary")
    finally:
        module._http_json = real_http_json

    sent = captured.get("data") or {}
    extra = sent.get("extra_data", {}).get("extra_pnginfo", {})
    if "workflow" not in extra:
        print("SELF-TEST FAIL: submit_prompt() no longer sends "
              "extra_data.extra_pnginfo.workflow -- the fix for the "
              "0-of-1474-images-missing-metadata defect has regressed.")
        return False
    if extra["workflow"] != {"1": {"class_type": "CanaryNode"}}:
        print("SELF-TEST FAIL: submit_prompt() sent a 'workflow' payload "
              "that does not match the graph it actually submitted: %r"
              % (extra["workflow"],))
        return False

    # 2. The check that would catch a regression must genuinely distinguish
    #    present from absent, against real PNG files on disk.
    with tempfile.TemporaryDirectory(prefix="wf_metadata_canary_") as tmpdir:
        with_meta = os.path.join(tmpdir, "with_workflow.png")
        stripped = os.path.join(tmpdir, "stripped.png")

        info = PngInfo()
        info.add_text("prompt", json.dumps({"1": {"class_type": "CanaryNode"}}))
        info.add_text("workflow", json.dumps({"1": {"class_type": "CanaryNode"}}))
        Image.new("RGB", (4, 4)).save(with_meta, pnginfo=info)

        # Strip: re-save a scratch COPY with no pnginfo at all -- this is
        # the literal shape of the original defect, not a mock of it.
        Image.open(with_meta).convert("RGB").save(stripped)

        has_workflow_with = "workflow" in Image.open(with_meta).info
        has_workflow_stripped = "workflow" in Image.open(stripped).info

        if not has_workflow_with:
            print("SELF-TEST FAIL: canary fixture is broken -- the PNG built "
                  "WITH a 'workflow' chunk does not have one.")
            return False
        if has_workflow_stripped:
            print("SELF-TEST FAIL: canary is vacuous -- the stripped scratch "
                  "copy still reports a 'workflow' chunk, so this check "
                  "cannot go red on the real defect and proves nothing.")
            return False

    print("SELF-TEST: workflow-metadata canaries PASS -- submit_prompt() "
          "sends extra_data.extra_pnginfo.workflow (proven offline, no "
          "network), and the presence check genuinely goes RED on a scratch "
          "PNG with the chunk stripped (proven by re-saving through PIL with "
          "no pnginfo).")
    return True


def self_test():
    """Offline canary for render_template()/load_and_patch_workflow() -- no
    ComfyUI needed. Proves two things, in order: (1) the failure mode is
    real (the OLD naive str.replace-into-JSON approach really does break on
    ordinary screenplay prose), so this canary is not vacuous decoration
    (invariant 2); (2) the FIXED render_template() composes the exact same
    nasty text correctly, end to end through json.loads(), with the value
    preserved byte-for-byte and the bare-numeric-placeholder path untouched."""
    import tempfile

    nasty = ("She said \"hello there\" to the room. It's cold. "
            "Line two follows a real newline.\n"
            "A backslash: C:\\path\\file. Done.")

    template = ("{\n"
               '  "8": {"inputs": {"prompt": "{prompt}", "steps": {steps}, '
               '"strength": {lora_strength}}},\n'
               '  "13": {"inputs": {"filename_prefix": "{output_prefix}"}}\n'
               "}\n")

    # 1. The bug this closes, reproduced: naive substitution on nasty text
    #    must NOT be valid JSON. If this assertion fails, the canary itself
    #    is meaningless (nothing here would ever have been broken).
    naive = template
    for key, value in {"prompt": nasty, "steps": 4, "lora_strength": "1.0",
                       "output_prefix": "canary"}.items():
        naive = naive.replace("{%s}" % key, str(value))
    naive_is_valid = True
    try:
        json.loads(naive)
    except json.JSONDecodeError:
        naive_is_valid = False
    if naive_is_valid:
        print("SELF-TEST FAIL: canary is vacuous -- naive str.replace substitution "
              "did NOT break on nasty text (quote/apostrophe/newline/backslash). "
              "This fixture no longer proves anything.")
        return 1

    # 2. The fix: render_template() on the SAME nasty text must produce valid
    #    JSON with the string preserved exactly and the bare numeric/prefix
    #    placeholders substituted as before.
    patched = render_template(template, {"prompt": nasty, "steps": 4,
                                          "lora_strength": "1.0",
                                          "output_prefix": "canary"})
    try:
        doc = json.loads(patched)
    except json.JSONDecodeError as exc:
        print("SELF-TEST FAIL: render_template() still produces invalid JSON "
              "on nasty text: %s" % exc)
        return 1
    checks = [
        (doc["8"]["inputs"]["prompt"] == nasty,
         "quoted placeholder: string not preserved exactly"),
        (doc["8"]["inputs"]["steps"] == 4,
         "bare numeric placeholder broken by the fix"),
        (doc["8"]["inputs"]["strength"] == 1.0,
         "bare numeric-string placeholder ({lora_strength}) broken by the fix"),
        (doc["13"]["inputs"]["filename_prefix"] == "canary",
         "quoted placeholder: plain string case broken"),
    ]
    for ok, msg in checks:
        if not ok:
            print("SELF-TEST FAIL: %s" % msg)
            return 1

    # 3. End to end through load_and_patch_workflow() (the real entry point,
    #    reading a template FILE, not just the in-memory function).
    fd, tmp_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(template)
        doc2 = load_and_patch_workflow(tmp_path, {"prompt": nasty, "steps": 4,
                                                   "lora_strength": "1.0",
                                                   "output_prefix": "canary"})
    finally:
        os.remove(tmp_path)
    if doc2 != doc:
        print("SELF-TEST FAIL: load_and_patch_workflow() (file path) disagrees "
              "with render_template() (in-memory) on identical input.")
        return 1

    _cache_canaries()

    if not _workflow_metadata_canaries():
        return 1

    print("SELF-TEST: naive substitution correctly failed on nasty text "
         "(quote + apostrophe + newline + backslash), and the fixed "
         "render_template()/load_and_patch_workflow() composed the same text "
         "correctly, byte-for-byte, both in-memory and via a real template file.")
    print("ALL PASS")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # RR's Execute app calls the scene with three positional args:
    # <SeqStart> <SeqEnd> <SeqStep>, in that order, ahead of anything else.
    ap.add_argument("seq_start", type=int, nargs="?", default=None)
    ap.add_argument("seq_end", type=int, nargs="?", default=None)
    ap.add_argument("seq_step", type=int, default=1, nargs="?")
    ap.add_argument("--self-test", action="store_true",
                    help="Offline canary for the JSON-template substitution "
                         "fix (render_template()) -- no ComfyUI needed, no "
                         "other args required.")
    ap.add_argument("--workflow", default=None,
                    help="Path to a ComfyUI workflow exported in API format. "
                         "Required unless --self-test.")
    ap.add_argument("--comfy-host", default="127.0.0.1")
    ap.add_argument("--comfy-port", default="8188")
    ap.add_argument("--comfy-output-dir", default=None,
                    help="ComfyUI's own output/ folder as seen from THIS "
                         "machine, used to verify files it reports actually "
                         "landed. Required for --verify-mode disk; ignored by "
                         "--verify-mode api.")
    ap.add_argument("--verify-mode", choices=("disk", "api"), default="disk",
                    help="How to prove the outputs are real. 'disk' stats the "
                         "files (correct when ComfyUI is local, or when its "
                         "output dir is on a share this machine also mounts). "
                         "'api' fetches each output back from ComfyUI's /view "
                         "endpoint, which is the only route that works when "
                         "ComfyUI runs on another machine.")
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--output-prefix", default="rr_comfyui")
    ap.add_argument("--set", action="append", default=[], dest="extra_subs",
                    metavar="NAME=VALUE",
                    help="Substitute {NAME} in the workflow with VALUE. "
                         "Repeatable. This is how a workflow gets its prompt, "
                         "resolution, step count and anything else that varies "
                         "per job, without this script knowing what any of "
                         "them mean. VALUE is inserted verbatim, so a value "
                         "used where JSON expects a string needs quotes IN "
                         "the template, not here.")
    ap.add_argument("--expect-outputs", type=int, default=None,
                    help="Fail if the workflow does not produce exactly this "
                         "many output files. Omit to skip that check.")
    ap.add_argument("--timeout", type=int, default=3600,
                    help="Max seconds to wait for ONE generation. See the "
                         "module docstring: this is independent of, and can "
                         "be shorter than, the farm's own freeze timeout.")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    if args.seq_start is None or args.seq_end is None:
        log("FAIL: seq_start and seq_end are required (or --self-test).")
        return 1

    if not args.workflow:
        log("FAIL: --workflow is required (or --self-test).")
        return 1

    if urllib is None:
        log("FAIL: urllib is unavailable in this Python.")
        return 1

    # Parse --set NAME=VALUE pairs. The per-wedge substitutions below are
    # generated by this script and MUST win: letting --set override {seed} or
    # {output_prefix} would give every wedge in a range the same seed and the
    # same output name, so a multi-wedge job would silently overwrite its own
    # results and look like it succeeded. Refuse rather than quietly ignore.
    RESERVED = ("wedge", "seed", "output_prefix")
    extra_subs = {}
    for pair in args.extra_subs:
        if "=" not in pair:
            log("FAIL: --set expects NAME=VALUE, got %r" % pair)
            return 1
        name, value = pair.split("=", 1)
        name = name.strip()
        if name in RESERVED:
            log("FAIL: --set %s is not allowed. This script generates {%s} "
                "per wedge; overriding it would make every wedge in a range "
                "produce identical output." % (name, name))
            return 1
        extra_subs[name] = value

    if args.verify_mode == "disk" and not args.comfy_output_dir:
        log("FAIL: --verify-mode disk needs --comfy-output-dir. Pass it, or "
            "use --verify-mode api to verify over HTTP instead.")
        return 1

    # A remote ComfyUI with a disk check is the dangerous combination: the
    # script would stat a path on the WRONG MACHINE. That fails closed rather
    # than passing a broken job, but it fails with "these output files do not
    # exist", which reads as "generation is broken" when the truth is
    # "verification is looking at the wrong disk". Someone would then debug the
    # workflow for an hour. Name it at startup instead.
    #
    # It is a WARNING and not a hard error because one legitimate case exists:
    # ComfyUI's output dir pointed at a share that both machines mount, where
    # the path really is visible from here. Only the operator knows that, so
    # this checks whether the directory is actually readable and decides on the
    # evidence rather than on the hostname alone.
    if args.verify_mode == "disk" and args.comfy_host not in LOOPBACK_HOSTS:
        if os.path.isdir(args.comfy_output_dir):
            log("NOTE: ComfyUI is remote (%s) but --comfy-output-dir exists "
                "here, so it is presumably a shared path. Disk verification "
                "will check THAT copy." % args.comfy_host)
        else:
            log("FAIL: ComfyUI is remote (%s) and --comfy-output-dir (%s) is "
                "not readable from this machine, so a disk check would be "
                "inspecting the wrong filesystem and would report missing "
                "files for a generation that actually succeeded. Use "
                "--verify-mode api, or point --comfy-output-dir at a share "
                "both machines mount."
                % (args.comfy_host, args.comfy_output_dir))
            return 1

    if not wait_for_comfy(args.comfy_host, args.comfy_port):
        return 1

    # RR frames map onto WEDGES (independent variations), not literal video
    # frames: seq_start..seq_end with seq_step defines which wedge indices
    # this job instance covers. Each wedge gets its own seed and output name
    # so a multi-wedge job never overwrites its own outputs.
    exit_code = 0
    for wedge in range(args.seq_start, args.seq_end + 1, max(1, args.seq_step)):
        log("=== wedge %d ===" % wedge)
        subs = dict(extra_subs)
        subs.update({
            "wedge": wedge,
            "seed": args.seed_base + wedge,
            "output_prefix": "%s_w%03d" % (args.output_prefix, wedge),
        })
        workflow = load_and_patch_workflow(args.workflow, subs)
        client_id = str(uuid.uuid4())
        prompt_id = submit_prompt(args.comfy_host, args.comfy_port, workflow, client_id)
        log("  submitted prompt_id=%s" % prompt_id)
        try:
            entry = wait_for_history(args.comfy_host, args.comfy_port,
                                     prompt_id, args.timeout)
            # Say whether this was actually RENDERED, on every run, so a cache
            # hit cannot masquerade as a result in somebody's comparison later.
            warn_if_cached(entry)
            ok = verify_outputs(entry, args.comfy_output_dir,
                                args.expect_outputs, mode=args.verify_mode,
                                host=args.comfy_host, port=args.comfy_port)
            log("  PASS: %d output file(s) verified: %s" % (len(ok), ok))
        except SystemExit as exc:
            log(str(exc))
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
