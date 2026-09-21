#!/usr/bin/env python3
"""
sync_master_tutor.py

Refreshes Desktop/Master_Tutor.csv -- the roster file
kpi_tracker_phase1_download_update.py, kpi_tracker_phase2_copy_from_staging.py,
and kpi_tracker_correct_parent_videos.py all read Faculty Leader / Tier /
Professional-Adjunct assignments from -- straight from Redshift, using
the EXACT same "master_tutor" query sync_redshift_cache.py already runs
for the dashboard's cache (data/cache/master_tutor.csv). That's the
"tutor/team query we already use in the dashboard" this was built to
reuse, rather than inventing a second, possibly-inconsistent one.

Why this exists: Master_Tutor.csv was being hand-maintained and drifted
out of date -- e.g. it had Eleanor Mancilla under Katherine Marino when
she'd actually moved to Annelies de Groot's team, which silently made
kpi_tracker_phase1's RETRY_ONLY filtering skip her with no error. Run
this before kpi_tracker_phase1_download_update.py each cycle (it's also
wired in as step 0 of run_full_metrics_pipeline.py) so the roster is
never more than a day stale.

Overwrites the file completely -- Preferred Pronouns and Notes columns
are preserved as blank (Redshift has no equivalent data, and every row
in the current file already has them blank, so there's nothing to lose
by not merging).

Usage:
    python3 sync_master_tutor.py
"""

import os

import pandas as pd
import psycopg2
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

REDSHIFT_HOST = os.environ["REDSHIFT_HOST"]
REDSHIFT_PORT = int(os.environ.get("REDSHIFT_PORT", 5439))
REDSHIFT_DB = os.environ["REDSHIFT_DB"]
REDSHIFT_USER = os.environ["REDSHIFT_USER"]
REDSHIFT_PASS = os.environ["REDSHIFT_PASSWORD"]

# Same path kpi_tracker_phase1_download_update.py reads (desktop_path
# there is computed from the current user's home dir the same way).
DESKTOP_PATH = os.path.expanduser("~/Desktop")
MASTER_TUTOR_PATH = os.path.join(DESKTOP_PATH, "Master_Tutor.csv")

# Identical to the "master_tutor" query in sync_redshift_cache.py --
# deliberately not modified, so this stays in lockstep with whatever the
# dashboard's own cache considers the source of truth for roster data.
QUERY = """
SELECT DISTINCT
    e.id AS user_id,
    DATE(e.hire_date) AS hire_date,
    tu.first_name||' '||tu.last_name AS tutor_name,
    flu.first_name||' '||flu.last_name AS faculty_leader,
    CASE WHEN e.delivery_target < 30 THEN 'Adjunct' ELSE 'Professional' END AS tutor_type,
    t.name AS tier,
    e.delivery_target,
    e.accept_new_students,
    e.featured
FROM dw.employees e
JOIN dw.team_members tm ON tm.member_id = e.id
JOIN dw.teams ON dw.teams.id = tm.team_id
JOIN dw.users tu ON e.user_id = tu.id
JOIN dw.employees mgr ON mgr.id = dw.teams.manager_id
JOIN dw.users flu ON mgr.user_id = flu.id
JOIN dw.tiers t ON e.tier_id = t.id
WHERE e.type = 'Tutor'
  AND e.end_date IS NULL
  AND e.tier_id IS NOT NULL
  AND tu.title = 'Tutor'
ORDER BY tutor_name
"""


def log(msg):
    print(msg, flush=True)


def get_conn():
    return psycopg2.connect(
        host=REDSHIFT_HOST, port=REDSHIFT_PORT,
        dbname=REDSHIFT_DB, user=REDSHIFT_USER,
        password=REDSHIFT_PASS, connect_timeout=30,
    )


def main():
    log("Connecting to Redshift...")
    conn = get_conn()
    try:
        log("Running master_tutor query...")
        df = pd.read_sql(QUERY, conn)
    finally:
        conn.close()
    log(f"Got {len(df)} tutors.")

    if os.path.exists(MASTER_TUTOR_PATH):
        old_df = pd.read_csv(MASTER_TUTOR_PATH)
        # A handful of rows in the hand-maintained file have a blank/NaN
        # Full Name -- drop those before building the lookup, otherwise
        # comparing/sorting a mix of str and float (NaN) blows up.
        old_df = old_df.dropna(subset=["Full Name"])
        old_assignments = dict(zip(old_df["Full Name"], old_df["Faculty Leader"]))
        current_names = set(df["tutor_name"].dropna())
        changed = []
        for _, row in df.iterrows():
            name = row["tutor_name"]
            new_fl = row["faculty_leader"]
            old_fl = old_assignments.get(name)
            if old_fl is not None and pd.notna(old_fl) and old_fl != new_fl:
                changed.append((name, old_fl, new_fl))
        if changed:
            log(f"{len(changed)} tutor(s) changed Faculty Leader since the last sync:")
            for name, old_fl, new_fl in changed:
                log(f"  {name}: {old_fl} -> {new_fl}")
        new_names = current_names - set(old_assignments.keys())
        dropped_names = set(old_assignments.keys()) - current_names
        if new_names:
            log(f"{len(new_names)} new tutor(s) not in the old file: {sorted(new_names)}")
        if dropped_names:
            log(f"{len(dropped_names)} tutor(s) in the old file no longer in Redshift's active roster "
                f"(likely departed): {sorted(dropped_names)}")
    else:
        log("No existing Master_Tutor.csv found -- writing fresh.")

    out = pd.DataFrame({
        "First Name": "",
        "Last Name": "",
        "Full Name": df["tutor_name"],
        "Faculty Leader": df["faculty_leader"],
        "Professional/Adjunct": df["tutor_type"],
        "Tier": df["tier"],
        "Preferred Pronouns": "",
        "Notes": "",
    })
    out.to_csv(MASTER_TUTOR_PATH, index=False)
    log(f"Saved -> {MASTER_TUTOR_PATH} ({len(out)} rows)")


if __name__ == "__main__":
    main()
