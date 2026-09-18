#!/usr/bin/env python3
"""Phase 5 live: version -> review -> note -> next version -> approval -> closed.

Drives the real site. The "reviewer" is simulated by creating a Note and flipping
the status through the API, because what is being tested is the POLLING and
LEDGER machinery, not whether a human can type. The ledger code under test is
build/tests/review_ledger.py and is imported, not reimplemented here.
"""
import os
import sys
import time

sys.path.insert(0, r"C:\example\genvideo-pipeline\build\tests")
import review_ledger as RL                                    # noqa: E402
import shotgun_api3                                           # noqa: E402

PROJECT_ID = 9999
SHOT_CODE = "GENVID_010"
STEP = "CMP"
DESC = "wan22ti2v"
OUT = r"C:\example\genvideo-pipeline\output\phase4"
LEDGER = r"C:\example\genvideo-pipeline\output\ledger\GENVID_010.json"

sg = shotgun_api3.Shotgun(os.environ["SHOTGRID_SITE_URL"],
                          script_name=os.environ["SHOTGRID_SCRIPT_NAME"],
                          api_key=os.environ["SHOTGRID_SCRIPT_KEY"])
project = sg.find_one("Project", [["id", "is", PROJECT_ID]], ["name"])
shot = sg.find_one("Shot", [["project", "is", project], ["code", "is", SHOT_CODE]], ["code"])
task = sg.find_one("Task", [["entity", "is", shot], ["step.Step.short_name", "is", STEP]], ["content"])
if task is None:
    sys.exit("FAIL: no Task. Refusing to create an orphan Version.")

led = RL.load(LEDGER)
led["shot"] = SHOT_CODE


def publish(n, mp4):
    code = "%s_%s_%s_v%03d" % (SHOT_CODE, STEP, DESC, n)
    v = sg.create("Version", {
        "project": project, "entity": shot, "sg_task": task, "code": code,
        "description": "Synthetic gen-video test render. No client content.",
        "sg_status_list": "rev", "sg_first_frame": 1001, "sg_last_frame": 1025,
        "sg_path_to_movie": mp4,
    })
    sg.upload("Version", v["id"], mp4, field_name="sg_uploaded_movie", display_name=code)
    RL.append(led, "version", version_number=n, code=code, sg_id=v["id"], movie=os.path.basename(mp4))
    RL.append(led, "status", status="rev", sg_id=v["id"])
    print("  published %s (id %d)" % (code, v["id"]))
    return v


def poll(v):
    """Poll BOTH: a reviewer may write a note, flip the status, or do one only."""
    new = 0
    notes = sg.find("Note", [["note_links", "in", [{"type": "Version", "id": v["id"]}]]],
                    ["subject", "content", "user", "created_at"])
    for nt in notes:
        if nt["id"] in led["seen_note_ids"]:
            continue                      # dedupe on id so a re-run cannot double-count
        led["seen_note_ids"].append(nt["id"])
        body = nt.get("content") or ""
        RL.append(led, "note", sg_id=v["id"], note_id=nt["id"],
                  author=(nt.get("user") or {}).get("name"),
                  content_verbatim=body,               # VERBATIM. never summarised.
                  requests_change=RL.note_requests_change(body))
        new += 1
    cur = sg.find_one("Version", [["id", "is", v["id"]]], ["sg_status_list"])["sg_status_list"]
    last = [e for e in led["entries"] if e["kind"] == "status" and e.get("sg_id") == v["id"]]
    if not last or last[-1]["status"] != cur:
        RL.append(led, "status", status=cur, sg_id=v["id"])
        print("  status changed -> %s (%s)" % (cur, RL.classify(cur)))
    return new


print("=== 1. publish v001 for review ===")
v1 = publish(1, os.path.join(OUT, "GENVID_010_CMP_wan22ti2v_v001.mp4"))
poll(v1)
print("  done? %s   (correct: pending review is not done)" % RL.is_done(led))

print("")
print("=== 2. simulate the reviewer: a note asking for a change + status flip ===")
review_text = ("The car reads too saturated against the sky, and the horizon wobbles "
               "in the last few frames. Please pull the reds back and stabilise the "
               "background before we show this to the client.")
sg.create("Note", {"project": project, "subject": "GENVID_010 comp review",
                   "content": review_text,
                   "note_links": [{"type": "Version", "id": v1["id"]}]})
sg.update("Version", v1["id"], {"sg_status_list": "rrq"})
time.sleep(3)

print("")
print("=== 3. poll: the ledger must record it VERBATIM and reopen ===")
n = poll(v1)
print("  new notes ingested: %d" % n)
stored = [e for e in led["entries"] if e["kind"] == "note"][-1]["content_verbatim"]
print("  verbatim match: %s" % (stored == review_text))
print("  done? %s   (correct: a change request is not done)" % RL.is_done(led))

print("")
print("=== 4. the change request must make the NEXT version, never an overwrite ===")
nxt = RL.next_version_number(led)
print("  next version number: v%03d" % nxt)
alive = sg.find("Version", [["project", "is", project], ["code", "is", v1["code"] if "code" in v1 else ""]], ["code"])
v2 = publish(nxt, os.path.join(OUT, "GENVID_010_CMP_wan22ti2v_v002.mp4"))
still = sg.find_one("Version", [["id", "is", v1["id"]]], ["code", "sg_status_list"])
print("  v001 still exists: %s (status %s) <- NOT overwritten"
      % (still["code"] if still else "GONE", still["sg_status_list"] if still else "-"))

print("")
print("=== 5. approve v002 ===")
sg.update("Version", v2["id"], {"sg_status_list": "apr"})
time.sleep(2)
poll(v2)
print("  done? %s" % RL.is_done(led))
print("  SHIPPED: nothing. Approval closes the ledger entry and delivers no artefact.")

RL.save(LEDGER, led)
print("")
print("=== ledger written to %s ===" % LEDGER)
RL.summarise(led)
