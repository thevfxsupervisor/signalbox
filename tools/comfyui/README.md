# ComfyUI as a Royal Render Execute job

ComfyUI has no native Royal Render render-app config, so this runs it through
RR's generic **Execute** app type: the "scene file" is a script, not a scene.

## What is here

- `comfyui_execute.py` - the actual job. Talks to an already-running local
  ComfyUI server, submits one workflow per "wedge" (RR's frame range maps onto
  independent variations, not literal video frames), and verifies every
  output file it expects is genuinely on disk before calling the job a
  success. See the module docstring for the reasoning behind each design
  choice; several of them exist because a simpler version would fail silently.
- `comfyui_execute.bat` - the RR-facing entry point. This IS the "scene file"
  you submit; it just forwards RR's positional args into the Python script.

Use the `comfyui` preset (`list_presets` / `build_submission` in the MCP
server) to build a submission spec with the right app/renderer names already
set.

## Prerequisites this integration depends on, and does not manage itself

**1. ComfyUI must already be running as a persistent service on the render
node**, before any job reaches it. This script never starts or stops it: a
generative model takes real time to load into VRAM, so starting one per job
would be slow and would collide with itself the moment two jobs land on the
same node. Run it the same way you would run any other always-on local
service (a scheduled task at machine startup, for example), independent of
and outliving any individual RR job.

**2. A client group restricting these jobs to ComfyUI-capable nodes.** Client
assignment at the per-job level does not work through Royal Render's XML
submission path (see the note in `rr_mcp/submit.py`), so the correct lever is
a **named client group**, matched automatically by Royal Render's own
`<RenderApp>_<Renderer>` convention. This preset sets `renderer="ComfyUI"`, so
a group literally named `Execute_ComfyUI` (containing only the node(s) that
actually have ComfyUI installed) will auto-restrict jobs built from this
preset to those nodes. Royal Render's own SDK ships a reference example for
creating client groups (`ClientGroups_addClient.py` in the RR Python SDK).
Until that group exists, jobs from this preset run on ANY Execute-capable
node and will fail loudly (the wrapper's own startup check) rather than
silently on a node without ComfyUI.

**3. A freeze-timeout that fits generative workloads.** Royal Render's
Execute app has its own freeze detector (`Frozen_Minutes` /
`Frozen_MinCoreUsage`), keyed on the CPU usage of the launched process - this
script, not the separate ComfyUI server it talks to over localhost. This
script's own CPU usage while polling is negligible, so a generation that runs
longer than the farm's configured `Frozen_Minutes` window can look frozen to
Royal Render and be aborted mid-generation even though ComfyUI is actively
working. If generations regularly run long, this needs a render-app config
variant with a longer `Frozen_Minutes` for this render app/renderer
combination; this repo does not ship farm configuration, so that change
belongs in your farm's own config tree.

## Running ComfyUI on a different machine, and how outputs get verified

By default this script assumes ComfyUI is on the same machine and verifies
outputs by looking at the filesystem: it stats every file ComfyUI claims it
wrote and confirms each one exists and is non-empty. A "completed" status is a
claim, not evidence.

Pointing `--comfy-host` at another machine quietly breaks that, because the
output directory is then on the OTHER machine's disk. Left alone the disk check
reports "these output files do not exist" for a generation that actually
succeeded, which reads as a broken workflow rather than a misdirected check.

So there are two verification modes:

- `--verify-mode disk` (default) stats the files. Correct when ComfyUI is
  local, and also correct when its output directory is on a share this machine
  mounts too. Requires `--comfy-output-dir`.
- `--verify-mode api` fetches every output back from ComfyUI's `/view` endpoint
  and checks real bytes came out. This is the only mode that works when
  ComfyUI is on another machine and its disk is not visible here. It needs no
  `--comfy-output-dir`.

`api` mode deliberately downloads the bytes rather than checking a status code:
`/view` answering 200 with a zero-length body is exactly the silent
empty-output failure the disk check exists to catch, so a status-code check
would reintroduce it.

If you ask for a remote host with `disk` verification, the script decides on
evidence rather than on the hostname: if the output directory is readable here
it says so and proceeds (the shared-path case), and if it is not it refuses at
startup and names the real reason instead of failing later as a phantom
missing file.

## The workflow file

This script fills in `{wedge}`, `{seed}` and `{output_prefix}` itself, once per
wedge. **Everything else in the template is passed with `--set NAME=VALUE`**,
repeatable, so the script needs no opinion about what a prompt, a resolution or
a step count means:

    --set prompt="a slow aerial push over a forest" --set width=480 --set steps=10

The value is inserted verbatim, so a placeholder used where JSON expects a
string needs its quotes in the template (`"text": "{prompt}"`), and one used as
a number must be bare (`"width": {width}`).

`--set` refuses the three generated names. Overriding `{seed}` or
`{output_prefix}` would give every wedge in a range the same seed and the same
output filename, so a multi-wedge job would silently overwrite its own results
and still report success.

A placeholder you forget is named at startup rather than surfacing as a JSON
parse error pointing at a character offset.

Export from ComfyUI's UI using **Save (API format)**, not the plain Save (a
different schema the /prompt endpoint does not accept). The exported JSON is
treated as a **text template**: `{seed}`, `{wedge}`, and `{output_prefix}`
are substituted before the file is parsed as JSON, so this script has no
opinion about your node graph and works with any workflow. Whatever field you
want to vary per wedge (a seed input, a filename prefix, a text prompt),
write the matching `{placeholder}` directly into that field's value in the
exported JSON.

## Testing without a farm

`comfyui_execute.py` can be run directly against any local ComfyUI instance
with no Royal Render involvement at all - that is the cheapest way to prove
the workflow-and-verification logic works before it ever goes near a farm
job:

    python comfyui_execute.py 0 2 1 --workflow my_workflow_api.json \
        --comfy-output-dir /path/to/ComfyUI/output \
        --output-prefix test --seed-base 1000
