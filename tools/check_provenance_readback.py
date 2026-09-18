"""Read provenance fields BACK off live Versions, rather than reading the code that claims to write
them. Written to check a claim that three publishers 'skip write_provenance() entirely'.
"""
import os, sys
TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import rnd_render as RR                      # sets REPO_TOOLS on sys.path the way the wedges do
sys.path.insert(0, RR.REPO_TOOLS)
assert RR._sg_ready(), "SHOTGRID_SCRIPT_KEY is not set in this shell"
import rnd_lab as R

sg = R._sg(R.sg_connect())
GEN = ["sg_model", "sg_gen_size_wxh", "sg_gen_seed", "sg_gen_steps", "sg_gen_cfg",
       "sg_workflow_template"]
D6 = ["sg_component__character", "sg_component__set", "sg_component__action",
      "sg_component__camera", "sg_component__style", "sg_anchor_version"]
for vid in [int(x) for x in sys.argv[1:]] or [69655, 69664]:
    v = sg.find_one("Version", [["id", "is", vid]], ["code"] + GEN + D6)
    if not v:
        print(vid, "NOT FOUND"); continue
    print("\nVersion %s  %s" % (vid, v.get("code")))
    print("  GEN populated:", {k: v.get(k) for k in GEN if v.get(k) not in (None, "")})
    print("  GEN missing  :", [k for k in GEN if v.get(k) in (None, "")])
    print("  D6  populated:", {k: v.get(k) for k in D6 if v.get(k) not in (None, "")})
    print("  D6  missing  :", [k for k in D6 if v.get(k) in (None, "")])
