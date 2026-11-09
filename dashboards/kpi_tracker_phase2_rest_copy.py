#!/usr/bin/env python3
"""
kpi_tracker_phase2_rest_copy.py -- Phase 2 via SharePoint's REST API.

Replaces the click-through "Copy to" flow (kpi_tracker_phase2_copy_from_staging.py),
which depends on scrolling SharePoint's file lists -- that broke on 2026-11-08
(lists stop at the first 30-row batch) and which could pick the wrong row.

For each Faculty Leader's team folder, using your logged-in browser session:
  1. Lists the files in <Team>/aa_Metrics_Upload (staging) and the folders in
     <Team> -- by REST, no scrolling.
  2. For every tutor on that leader's roster (Master_Tutor.csv), finds their
     staged "<name> KPI Tracker.xlsx" and their own folder (trying the same
     aliases / spelling variants phase 1 uses).
  3. Copies the staged file into <Team>/<Tutor>/KPI Tracker/, overwriting.
     If that folder already holds exactly one KPI Tracker .xlsx under a
     different name (e.g. "Eve Shame 2025 KPI Tracker.xlsx"), the copy
     replaces THAT file instead of creating a second one.
  4. Verifies the destination size matches the source.

Safety: skips staged files not modified in the last STALE_DAYS days (old
leftovers from earlier cycles), and never guesses -- anything it can't match
exactly is reported, not copied.

Usage (default is a DRY RUN that copies nothing):
  python3 kpi_tracker_phase2_rest_copy.py                  # dry run, all leaders
  python3 kpi_tracker_phase2_rest_copy.py --copy --limit 1 # real copy, first tutor only (TEST)
  python3 kpi_tracker_phase2_rest_copy.py --copy           # real copy, everyone
  python3 kpi_tracker_phase2_rest_copy.py --copy --leader "Ian Plamondon"
"""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd

import kpi_tracker_phase1_download_update as p

BASE = "/sites/FacultyLeadership/Shared Documents/General/IL Team Folders"
STAGING = "aa_Metrics_Upload"
STALE_DAYS = 3
RESULTS_PATH = os.path.expanduser("~/Desktop/phase2_rest_results.csv")

# Tutors to skip entirely (leader -> [names]).
EXCLUDED_TUTORS = {}

JS = """
var cb = arguments[arguments.length - 1];
var method = arguments[0], url = arguments[1], digest = arguments[2];
var headers = {"Accept": "application/json;odata=nometadata"};
if (digest) { headers["X-RequestDigest"] = digest; }
fetch(url, {method: method, headers: headers, credentials: "same-origin"})
  .then(function(r){ return r.text().then(function(t){ cb({status: r.status, body: t}); }); })
  .catch(function(e){ cb({status: -1, body: String(e)}); });
"""


def enc(path):
    return quote(path.replace("'", "''"), safe="/'")


def rest(driver, method, url, digest=None):
    return driver.execute_async_script(JS, method, url, digest)


def api(path):
    return "/sites/FacultyLeadership/_api/web/" + path


def get_digest(driver):
    r = rest(driver, "POST", "/sites/FacultyLeadership/_api/contextinfo")
    if r["status"] != 200:
        raise RuntimeError(f"contextinfo failed: {r['status']} {r['body'][:200]}")
    return json.loads(r["body"])["FormDigestValue"]


def list_names(driver, folder_path, kind):
    """kind = 'Files' or 'Folders'. Returns list of dicts."""
    sel = "Name,Length,TimeLastModified" if kind == "Files" else "Name"
    r = rest(driver, "GET", api(f"GetFolderByServerRelativeUrl('{enc(folder_path)}')/{kind}?$select={sel}&$top=5000"))
    if r["status"] != 200:
        return None
    return json.loads(r["body"]).get("value", [])


def name_candidates(tutor):
    return list(dict.fromkeys(
        v for base in [tutor, p.NAME_ALIASES.get(tutor, tutor),
                       tutor.replace("'", "").replace("’", "").replace("-", " ")]
        for v in p.unicode_variants(base)
    ))


def log_result(leader, tutor, status, detail=""):
    new = not os.path.exists(RESULTS_PATH)
    with open(RESULTS_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "faculty_leader", "tutor", "status", "detail"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), leader, tutor, status, detail])


def main():
    argv = sys.argv[1:]
    do_copy = "--copy" in argv
    limit = int(argv[argv.index("--limit") + 1]) if "--limit" in argv else None
    only_leader = argv[argv.index("--leader") + 1] if "--leader" in argv else None

    master = pd.read_csv(os.path.expanduser("~/Desktop/Master_Tutor.csv"))
    leaders = sorted(master["Faculty Leader"].dropna().unique())
    leaders = [l for l in leaders if l not in p.EXCLUDED_FACULTY_LEADERS]
    if only_leader:
        leaders = [l for l in leaders if l == only_leader]

    print("MODE:", "REAL COPY" if do_copy else "DRY RUN (nothing will be copied)")
    driver = p.make_driver()
    copied = 0
    problems = []
    try:
        p.login_and_reach_il_team_folders(driver)
        for leader in leaders:
            team = f"{leader.split()[0]}'s Team"
            team_path = f"{BASE}/{team}"
            staged = list_names(driver, f"{team_path}/{STAGING}", "Files")
            folders = list_names(driver, team_path, "Folders")
            if staged is None or folders is None:
                print(f"[{leader}] could not list staging/team folder -- skipping leader")
                problems.append((leader, "(whole team)", "could not list folders via REST"))
                continue
            staged_by_name = {f["Name"]: f for f in staged}
            folder_names = {f["Name"] for f in folders}
            digest = get_digest(driver) if do_copy else None
            print(f"\n[{leader}] staged files: {len(staged)}, team folders: {len(folders)}")

            tutors = master[master["Faculty Leader"] == leader]["Full Name"].dropna().tolist()
            tutors = [t for t in tutors if t not in EXCLUDED_TUTORS.get(leader, [])]
            for tutor in tutors:
                if limit is not None and copied >= limit:
                    break
                cands = name_candidates(tutor)
                src_name = next((f"{c} KPI Tracker.xlsx" for c in cands if f"{c} KPI Tracker.xlsx" in staged_by_name), None)
                if not src_name:
                    problems.append((leader, tutor, "no staged file"))
                    log_result(leader, tutor, "SKIPPED", "no staged file")
                    continue
                sf = staged_by_name[src_name]
                modified = datetime.fromisoformat(sf["TimeLastModified"].replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - modified).total_seconds() / 86400
                if age_days > STALE_DAYS:
                    problems.append((leader, tutor, f"staged file is stale ({age_days:.1f} days old)"))
                    log_result(leader, tutor, "SKIPPED", f"stale staged file {age_days:.1f}d")
                    continue
                folder = next((c for c in cands if c in folder_names), None)
                if not folder:
                    problems.append((leader, tutor, "no tutor folder in team folder"))
                    log_result(leader, tutor, "SKIPPED", "no tutor folder")
                    continue
                dest_dir = f"{team_path}/{folder}/KPI Tracker"
                existing = list_names(driver, dest_dir, "Files")
                if existing is None:
                    problems.append((leader, tutor, "no 'KPI Tracker' subfolder"))
                    log_result(leader, tutor, "SKIPPED", "no KPI Tracker subfolder")
                    continue
                trackers = [f["Name"] for f in existing if f["Name"].lower().endswith(".xlsx") and "kpi tracker" in f["Name"].lower()]
                dest_name = src_name if src_name in trackers or len(trackers) != 1 else trackers[0]
                src = f"{team_path}/{STAGING}/{src_name}"
                dest = f"{dest_dir}/{dest_name}"
                note = "" if dest_name == src_name else f" (replacing existing '{dest_name}')"
                if not do_copy:
                    print(f"  would copy: {src_name} -> {folder}/KPI Tracker/{dest_name}{note}")
                    copied += 1
                    continue
                url = api(f"GetFileByServerRelativeUrl('{enc(src)}')/copyTo(strNewUrl='{enc(dest)}',bOverWrite=true)")
                r = rest(driver, "POST", url, digest)
                if r["status"] not in (200, 204):
                    problems.append((leader, tutor, f"copy failed: {r['status']} {r['body'][:120]}"))
                    log_result(leader, tutor, "FAILED", f"{r['status']} {r['body'][:200]}")
                    print(f"  FAILED {tutor}: {r['status']} {r['body'][:120]}")
                    continue
                after = list_names(driver, dest_dir, "Files") or []
                got = next((f for f in after if f["Name"] == dest_name), None)
                if got is None or int(got["Length"]) != int(sf["Length"]):
                    problems.append((leader, tutor, "copied but size check failed"))
                    log_result(leader, tutor, "FAILED", "size mismatch after copy")
                    print(f"  FAILED size check for {tutor}")
                    continue
                copied += 1
                log_result(leader, tutor, "SUCCESS", dest_name)
                print(f"  copied+verified: {src_name} -> {folder}/KPI Tracker/{dest_name}{note}")
            if limit is not None and copied >= limit:
                break
    finally:
        driver.quit()

    print("\n" + "=" * 60)
    print(f"{'Would copy' if not do_copy else 'Copied and verified'}: {copied}")
    if problems:
        print(f"{len(problems)} tutor(s) NOT copied:")
        cur = None
        for leader, tutor, why in problems:
            if leader != cur:
                print(f"{leader}:")
                cur = leader
            print(f"  - {tutor}  ({why})")
    print("=" * 60)


if __name__ == "__main__":
    main()
