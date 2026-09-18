#!/usr/bin/env python3
"""Phase 4: create a review Version in GENVIDEO_TEST, upload the movie, verify.

Follows build/reference/shotgrid-review-version-fieldset.md. Refuses to create an
orphan: if the Task cannot be resolved, it says so LOUDLY and stops rather than
creating a Version with a dangling sg_task, which is the studio hook's known
failure mode.
"""
import os
import sys

import shotgun_api3

PROJECT_ID = 9999               # GENVIDEO_TEST
SHOT_CODE = "GENVID_010"
STEP = "CMP"
DESC = "wan22ti2v"
VERSION_NUMBER = 1
FIRST_FRAME = 1001
FRAMES = 25
MP4 = (r"C:\example\genvideo-pipeline\output\phase4"
       r"\GENVID_010_CMP_wan22ti2v_v001.mp4")

sg = shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                          script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                          api_key=os.environ["SHOTGRID_SCRIPT_KEY"])

project = sg.find_one("Project", [["id", "is", PROJECT_ID]], ["name"])
if project is None:
    sys.exit("FAIL: project id %d not found" % PROJECT_ID)
print("project: %s (id %d)" % (project["name"], project["id"]))

# --- Shot ----------------------------------------------------------------
shot = sg.find_one("Shot", [["project", "is", project], ["code", "is", SHOT_CODE]], ["code"])
if shot is None:
    shot = sg.create("Shot", {"project": project, "code": SHOT_CODE,
                              "description": "Synthetic gen-video test shot. No client content."})
    print("shot:    created %s (id %d)" % (SHOT_CODE, shot["id"]))
else:
    print("shot:    reusing %s (id %d)" % (SHOT_CODE, shot["id"]))

# --- Task. THIS is the orphan guard. -------------------------------------
task = sg.find_one("Task", [["entity", "is", shot],
                            ["step.Step.short_name", "is", STEP]], ["content"])
if task is None:
    step = sg.find_one("Step", [["short_name", "is", STEP], ["entity_type", "is", "Shot"]], ["code"])
    if step is None:
        sys.exit("FAIL: no pipeline Step with short_name %r for Shot. Not creating an orphan." % STEP)
    task = sg.create("Task", {"project": project, "entity": shot,
                              "step": step, "content": "Comp"})
    print("task:    created %r on step %s (id %d)" % ("Comp", STEP, task["id"]))
else:
    print("task:    reusing %r (id %d)" % (task.get("content"), task["id"]))

# Re-resolve to PROVE the link is real, rather than trusting the create call.
task_check = sg.find_one("Task", [["entity", "is", shot],
                                  ["step.Step.short_name", "is", STEP]], ["content"])
if task_check is None:
    sys.exit("FAIL: Task did not resolve after creation. Not creating an orphan Version.")

# --- User. A script user has no HumanUser; that is fine, leave it unset. --
login = os.environ.get("SHOTGRID_REVIEW_LOGIN")
user = sg.find_one("HumanUser", [["login", "is", login]], ["name"]) if login else None
print("user:    %s" % (user["name"] if user else "(none - running as a script user)"))

code = "%s_%s_%s_v%03d" % (SHOT_CODE, STEP, DESC, VERSION_NUMBER)
data = {
    "project": project,
    "entity": shot,
    "sg_task": task,
    "code": code,
    "description": ("Wan2.2 TI2V-5B, 512x288, 25f, 10 steps, cfg 5.0, seed 3000. "
                    "Generated on the workstation. Synthetic test content, no client material."),
    "sg_status_list": "rev",          # verified against the site: 'Pending Wangle Review'
    "sg_first_frame": FIRST_FRAME,
    "sg_last_frame": FIRST_FRAME + FRAMES - 1,
    "sg_path_to_movie": MP4,
}
if user:
    data["user"] = user

version = sg.create("Version", data)
print("version: created %s (id %d)" % (code, version["id"]))

print("upload:  sending %.1f KB ..." % (os.path.getsize(MP4) / 1024.0))
sg.upload("Version", version["id"], MP4, field_name="sg_uploaded_movie", display_name=code)
print("upload:  accepted")
print("VERSION_ID=%d" % version["id"])
print("SHOT_ID=%d" % shot["id"])
print("TASK_ID=%d" % task["id"])
