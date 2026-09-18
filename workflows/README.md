# ComfyUI workflow templates for this workstation

For the `rr-mcp` wrapper on a second workstation. Produced and **validated on this workstation**, not written from memory.

## `wan22_ti2v_5b_t2v.api.json`

Wan 2.2 **TI2V-5B** text-to-video, ComfyUI API format (`POST /prompt` takes this object as
`{"prompt": <this>}`).

### Placeholders

You asked for `{seed}` and `{output_prefix}`. I added four more, because a template with a hardcoded
prompt is not a template - **`{prompt}` in particular is not optional for a T2V job.** If your
substitution only handles two keys, it will leave the rest in place and ComfyUI will reject the
graph, so wire all six.

| placeholder | type | quoting in the file | proven-safe value |
|---|---|---|---|
| `{prompt}` | string | **inside** quotes | any |
| `{negative_prompt}` | string | **inside** quotes | `blurry, distorted, watermark, text` |
| `{width}` | int | **unquoted** | `512` |
| `{height}` | int | **unquoted** | `288` |
| `{length}` | int | **unquoted** | `25` |
| `{steps}` | int | **unquoted** | `10` |
| `{seed}` | int | **unquoted** | any |
| `{output_prefix}` | string | **inside** quotes | e.g. `rrmcp_shot010` |

**The numeric placeholders are deliberately unquoted, so this file does NOT parse as JSON until you
substitute it.** That is intentional: a naive replace of a *quoted* `"{seed}"` yields the string
`"12345"`, and ComfyUI's `KSampler.seed` wants an INT and rejects it. Substitute first, parse second.

`length` must satisfy `(length - 1) % 4 == 0` - Wan's temporal compression is 4. 25 works; 24 does not.

### Verified, not assumed

Submitted through the live API on this workstation with `seed=987654`, `output_prefix=rrmcp_templatetest`:

    status: success    frames: 25    first: rrmcp_templatetest_00001_.png

### The VRAM ceiling - read before you raise any of these numbers

The proven-safe values above peaked at **10.93 GB of 11.99 GB**. That is 91% of the card on
what is close to the *smallest* useful Wan job (512x288x25 is 32x18x7 in latent space).

**Do not scale these up without measuring.** Raising resolution or length on this node will OOM
before it gets slow. If you need bigger, the graph needs `wanBlockSwap` or offload added - which is
a different template, not a bigger number in this one.

Also: **this box cannot run the A14B model** the pipeline research doc targets (26.6 GB per expert
at fp16, and there are two). Anything in rr-mcp that hardcodes A14B will fail on this workstation.

## Where output lands

`C:\ComfyUI_windows_portable\ComfyUI\output\<output_prefix>_#####_.png` on this workstation, retrievable over
the API with `GET /view?filename=...&subfolder=&type=output`.

Frames are PNG sequences, not an encoded movie. If rr-mcp wants a movie file, either add a
`SaveWEBM`/`SaveAnimatedWEBP` node to the graph or encode downstream - say which and I will add a
second template.

---

## `wan22_a14b_i2v.api.json` and `wan22_a14b_t2v.api.json`

Wan 2.2 **A14B** (14B x2 experts), Q4_K_S GGUF, added in Phase 1 of MASTER-PLAN-V2. These are
**new files; the 5B templates above are untouched** and remain what rr-mcp uses.

### The graph is not the 5B graph

A14B ships as two experts, a high-noise one and a low-noise one, and they are used in sequence
within a single generation rather than blended. So:

    UnetLoaderGGUF(high) -> LoraLoaderModelOnly(Lightning high) -> ModelSamplingSD3(shift 8)
        -> KSamplerAdvanced  add_noise=enable,  steps 0..switch,  leftover noise KEPT
    UnetLoaderGGUF(low)  -> LoraLoaderModelOnly(Lightning low)  -> ModelSamplingSD3(shift 8)
        -> KSamplerAdvanced  add_noise=DISABLE, steps switch..end, leftover noise dropped

`add_noise` must be `disable` on the second stage. The latent handed over already carries the
noise from stage one; adding more would re-noise a half-denoised latent and the second expert
would be solving a different problem than the one it was given.

i2v conditioning is **`WanImageToVideo`**, not `Wan22ImageToVideoLatent`. The latter is 5B-only.
`WanImageToVideo` returns three things - positive, negative AND the latent - so the sampler's
conditioning comes from the i2v node's outputs `[12,0]` / `[12,1]`, not straight from the text
encoders. Wiring the raw CLIPTextEncode outputs into the sampler instead silently drops the
image conditioning and you get a t2v render that ignores your anchor.

### THE VAE TRAP - read this one

A14B uses **`wan_2.1_vae.safetensors`** (0.25 GB). The `wan2.2_vae.safetensors` sitting next to it
in `models/vae` is the **5B's** VAE and is a different latent space. Both files are on this box,
their names differ by one character in the middle, and picking the wrong one does not raise a
tidy error. `{vae_name}` is a placeholder precisely so the choice is explicit at every call site
rather than defaulted; see the canary result in `docs/METHOD.md` for what the
wrong one actually does.

### Frame rate

A14B is **16 fps native**; 81 frames = 5.06 s. The 5B templates and the whole conform/caption
timeline are **24 fps**. Anything joining these two worlds needs an explicit retime - that is
Phase 6's problem, not a rounding error to ignore.

### Placeholders

| placeholder | quoting | notes |
|---|---|---|
| `{unet_high}` / `{unet_low}` | in quotes | GGUF filenames, e.g. `Wan2.2-I2V-A14B-HighNoise-Q4_K_S.gguf` |
| `{lora_high}` / `{lora_low}` | in quotes | must match the expert - i2v LoRAs on an i2v graph |
| `{lora_strength}` | **unquoted** | `1.0` |
| `{shift}` | **unquoted** | `8.0` |
| `{vae_name}` | in quotes | `wan_2.1_vae.safetensors`. See above. |
| `{start_image}` | in quotes | i2v only; a filename in ComfyUI's `input/` |
| `{prompt}` / `{negative_prompt}` | in quotes | at cfg 1.0 the negative is **inert** - steer in the positive |
| `{width}` `{height}` `{length}` `{steps}` `{switch_step}` | **unquoted** | `length` must satisfy `(length-1) % 4 == 0` |
| `{cfg}` | **unquoted** | `1.0` with Lightning; Lightning at any other cfg is not 4-step distilled behaviour |
| `{seed}` `{output_prefix}` | see 5B section | filled by `comfyui_execute.py`, do not `--set` them |

As with the 5B templates the numeric placeholders are deliberately unquoted, so **this file does
not parse as JSON until it is substituted.** That is the same intentional choice documented above.

### Turning Lightning OFF

There is no `{use_lora}` switch. `strength_model: 0.0` is arithmetically identity but leaves a
LoRA in the graph, so a "no-LoRA" cell built that way is really a zeroed-LoRA cell. To get a
genuine no-LoRA graph, delete nodes `3` and `4` and repoint `5`/`6` at `1`/`2` - which is exactly
what `strip_lora()` in `tests/a14b_wedge.py` does.
