#!/usr/bin/env python3
"""
diagnose_rest_folders.py -- READ-ONLY. Uses the logged-in browser session to ask
SharePoint's REST API for the full list of folders in each Faculty Leader's
team folder (no scrolling), then compares to Master_Tutor.csv and reports which
tutors have NO folder in their leader's team folder.

Run:  python3 diagnose_rest_folders.py
"""
import json
import os

import pandas as pd

import kpi_tracker_phase1_download_update as p

BASE = "/sites/FacultyLeadership/Shared Documents/General/IL Team Folders"
LEADERS = ["Annelies de Groot", "Ela Cross", "Geoff St. Marie", "Ian Plamondon", "Kristin Haase-Alvey"]

JS = """
var done = arguments[arguments.length - 1];
var path = arguments[0];
var url = "/sites/FacultyLeadership/_api/web/GetFolderByServerRelativeUrl('" +
          path.replace(/'/g, "''") + "')/Folders?$select=Name&$top=2000";
fetch(url, {headers: {"Accept": "application/json;odata=nometadata"}, credentials: "same-origin"})
  .then(function(r){ return r.text().then(function(t){ done({status: r.status, body: t}); }); })
  .catch(function(e){ done({status: -1, body: String(e)}); });
"""


def main():
    master = pd.read_csv(os.path.expanduser("~/Desktop/Master_Tutor.csv"))
    driver = p.make_driver()
    try:
        p.login_and_reach_il_team_folders(driver)
        for leader in LEADERS:
            team = f"{leader.split()[0]}'s Team"
            res = driver.execute_async_script(JS, f"{BASE}/{team}")
            print(f"\n=== {leader} ({team}) -- REST status {res['status']}")
            if res["status"] != 200:
                print("  body:", res["body"][:300])
                continue
            names = [f["Name"] for f in json.loads(res["body"]).get("value", [])]
            print(f"  folders in team folder: {len(names)}")
            have = set(names)
            tutors = master[master["Faculty Leader"] == leader]["Full Name"].dropna().tolist()
            missing = [t for t in tutors if t not in have]
            print(f"  roster tutors: {len(tutors)}   with NO matching folder here: {len(missing)}")
            for t in missing:
                print("   -", t)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
