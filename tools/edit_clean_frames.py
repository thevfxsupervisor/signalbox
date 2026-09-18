"""Ask Qwen-Image-Edit to remove a stray that no geometric rule can reach.

GEOFF'S SUGGESTION, 2026-09-08: *"If you want help cleaning up the training images I bet qwen image
edit could help you remove them. Just ask it to."* He is right about the shape of the problem. The
despeckler is a DISTANCE rule and F428 already established the failure it cannot cover: some specks
are ATTACHED to the figure's own ink and survive any connectivity or distance test. A geometric rule
cannot tell "blob" from "hair", because to geometry they are one component. A model that has seen a
million pictures can.

RUNS ON THIS WORKSTATION'S OWN GPU, NOT THE SHARED TRAINING CARD. That card is busy training, and a
second job on it does not OOM, it silently slows the run by roughly 20x. This workstation has its own
ComfyUI on 8188 and that card was released for parallel work, so this contends with nothing.

THE GEOMETRY RULE THIS GRAPH EXISTS TO RESPECT (F476, verified in our own ComfyUI source tonight).
`comfy_extras.nodes_qwen` rescales every reference to exactly 1,048,576 px AT THE SOURCE'S OWN
ASPECT, and the model lays reference tokens and generated tokens on ONE SHARED CENTRED GRID. If the
sampler's latent is a different size or shape from that reference, position (i, j) means two
different places, the model cannot copy across the grids, and it RE-SYNTHESISES the subject instead
of editing it. Nothing errors; you just get a different character back. So the source goes through
`FluxKontextImageScale` ONCE and BOTH branches read that same scaled image: the encoder gets it, and
`VAEEncode` of the very same pixels seeds the sampler. That is the coincidence-free version.

DENOISE IS THE OTHER HALF. Sub-1.0 denoise keeps part of the incoming latent, which is only
meaningful because the latent IS the source. Low denoise preserves the frame and may not remove the
blob; high denoise removes it and may redraw the character. That is the axis this compares, and it
is compared on a KNOWN-BAD frame and a KNOWN-CLEAN frame together, because a cleaner that damages
clean frames is worse than the debris it removes.
"""
import argparse
import io
import json
import os
import sys
import urllib.request

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)

SERVER = "127.0.0.1:8188"                       # this workstation's own ComfyUI, not the shared card
UNET = "qwen_image_edit_2509_fp8_e4m3fn.safetensors"
CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"
SHIFT, STEPS, CFG = 3.1, 20, 4.0

# Say what to REMOVE and, just as importantly, what to KEEP. F476's delta-only rule: describe the
# change, never redescribe the whole scene, or the model treats it as a fresh generation.
PROMPT = ("Remove the small floating brown blob in the empty white space near the head. Keep the "
          "character, his hair, his glasses, his clothing and his pose exactly as they are. Keep "
          "the plain white background.")


def upload(local, name):
    body = io.BytesIO()
    b = "----editcleanboundary"
    for k, v in (("subfolder", ""), ("type", "input"), ("overwrite", "true")):
        body.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (b, k, v)).encode())
    body.write(("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
                "Content-Type: image/png\r\n\r\n" % (b, name)).encode())
    body.write(open(local, "rb").read())
    body.write(("\r\n--%s--\r\n" % b).encode())
    r = urllib.request.Request("http://%s/upload/image" % SERVER, data=body.getvalue(),
                               headers={"Content-Type": "multipart/form-data; boundary=%s" % b})
    return json.loads(urllib.request.urlopen(r, timeout=180).read())


def graph(image_name, denoise, seed, prefix):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET,
                                                     "weight_dtype": "fp8_e4m3fn"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "qwen_image",
                                                     "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": SHIFT}},
        "5": {"class_type": "CFGNorm", "inputs": {"model": ["4", 0], "strength": 1.0}},
        "10": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        # ONE scale, read by BOTH branches. This is the whole point, see the docstring.
        "11": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["10", 0]}},
        "20": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["2", 0], "prompt": PROMPT, "vae": ["3", 0], "image1": ["11", 0]}},
        "21": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["20", 0]}},
        "30": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["3", 0]}},
        "40": {"class_type": "KSampler", "inputs": {
            "model": ["5", 0], "positive": ["20", 0], "negative": ["21", 0],
            "latent_image": ["30", 0], "seed": seed, "steps": STEPS, "cfg": CFG,
            "sampler_name": "euler", "scheduler": "simple", "denoise": denoise}},
        "50": {"class_type": "VAEDecode", "inputs": {"samples": ["40", 0], "vae": ["3", 0]}},
        "51": {"class_type": "SaveImage", "inputs": {"images": ["50", 0],
                                                     "filename_prefix": prefix}},
    }


def run(graph_dict):
    r = urllib.request.Request("http://%s/prompt" % SERVER,
                               data=json.dumps({"prompt": graph_dict}).encode(),
                               headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=300).read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", nargs="+", required=True, help="full paths to source frames")
    ap.add_argument("--denoise", type=float, nargs="+", default=[0.5, 0.8])
    ap.add_argument("--seed", type=int, default=1602)
    a = ap.parse_args()

    # A graph that asserts its own shape before spending anything: the two branches MUST read the
    # same scaled image, or this is the silent re-synthesis failure rather than an edit.
    g = graph("x.png", 0.5, 1, "t")
    assert g["20"]["inputs"]["image1"] == g["30"]["inputs"]["pixels"] == ["11", 0], (
        "the encoder and the sampler's latent are not reading the same scaled image, which is the "
        "exact mismatch that makes the edit model re-synthesise instead of edit (F476)")

    for p in a.frames:
        assert os.path.isfile(p), "missing frame: %s" % p
        name = os.path.basename(p)
        upload(p, name)
        for dn in a.denoise:
            pref = "edclean_%s_d%02d" % (os.path.splitext(name)[0], int(dn * 100))
            run(graph(name, dn, a.seed, pref))
            print("queued %s at denoise %.2f" % (name, dn), flush=True)


if __name__ == "__main__":
    main()
