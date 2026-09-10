"""
buc_margin_data_pull.py
--------------------------------
One-off pull for the BUC Premium Promotion margin model. Read-only — no writes.

Pulls three things from Redshift:
  1. buc_sessions.csv   — every BUC (brand_id=42) session booked in the last 12 months,
                          with booked_at (created_at) and delivered_at (starts_at), so we
                          can compute the real booking-to-delivery lead-time distribution.
  2. buc_roster.csv     — current active tutors: tier, instruction rate, BUC pay rate.
  3. buc_weekly_hours.csv — avg weekly BUC hours delivered per tutor, last 8 weeks.
  4. buc_tier_history_sample.csv — a small sample (SELECT *, LIMIT 20) of dw.histories
                          for tier changes, just to see what columns actually exist before
                          we pull the full history. Uses SELECT * on purpose since the exact
                          column names for old/new tier value weren't confirmed.

Run this from a normal terminal (NOT the Cowork sandbox) since it needs real network
access to Redshift:

    cd "/Users/tylerharrington/Desktop/Revolution Prep/FL_Dashboards/dashboards"
    python3 buc_margin_data_pull.py

Output lands in a new subfolder: buc_margin_model_data/
"""

import os
import sys
from datetime import datetime

import pandas as pd
import psycopg2
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

OUT_DIR = os.path.join(HERE, "buc_margin_model_data")
os.makedirs(OUT_DIR, exist_ok=True)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get_conn():
    return psycopg2.connect(
        host=os.environ["REDSHIFT_HOST"],
        port=int(os.environ.get("REDSHIFT_PORT", 5439)),
        dbname=os.environ["REDSHIFT_DB"],
        user=os.environ["REDSHIFT_USER"],
        password=os.environ["REDSHIFT_PASSWORD"],
        connect_timeout=30,
    )


# ── Queries ──────────────────────────────────────────────────────────────────
Q_SESSIONS = """
SELECT
    s.id AS session_id,
    s.supervisor_id AS tutor_id,
    t.name AS tutor_tier,
    s.created_at AS booked_at,
    s.starts_at AS delivered_at,
    s.duration AS duration_minutes,
    s.attendances_attended_count
FROM dw.sessions s
JOIN dw.courses c ON s.course_id = c.id
JOIN dw.employees e ON s.supervisor_id = e.id
JOIN dw.tiers t ON e.tier_id = t.id
WHERE c.brand_id = 42
  AND s.created_at >= GETDATE() - 365
"""

Q_ROSTER = """
SELECT
    e.id AS tutor_id,
    t.name AS tier,
    bp.instruction_rate,
    bp.pay_rate,
    e.delivery_target,
    e.accept_new_students,
    DATE(e.hire_date) AS hire_date
FROM dw.employees e
JOIN dw.tiers t ON e.tier_id = t.id
LEFT JOIN orbit_stitch.brand_preferences bp
    ON bp.employee_id = e.id AND bp.brand_id = 42
WHERE e.end_date IS NULL
  AND e.type = 'Tutor'
  AND e.tier_id IS NOT NULL
"""

Q_WEEKLY_HOURS = """
SELECT
    s.supervisor_id AS tutor_id,
    SUM(s.duration) / 60.0 / 8.0 AS avg_weekly_buc_hours
FROM dw.sessions s
JOIN dw.courses c ON s.course_id = c.id
WHERE c.brand_id = 42
  AND s.starts_at >= GETDATE() - 56
  AND s.starts_at < GETDATE()
  AND s.attendances_attended_count > 0
GROUP BY s.supervisor_id
"""

Q_HISTORY_SAMPLE = """
SELECT *
FROM dw.histories
WHERE item_type = 'Employee' AND attr = 'tutor_type'
ORDER BY created_at DESC
LIMIT 20
"""

JOBS = [
    ("buc_sessions.csv", Q_SESSIONS, "BUC sessions (last 12mo, by booked date)"),
    ("buc_roster.csv", Q_ROSTER, "Current active tutor roster + rates"),
    ("buc_weekly_hours.csv", Q_WEEKLY_HOURS, "Avg weekly BUC hours/tutor, last 8wks"),
    ("buc_tier_history_sample.csv", Q_HISTORY_SAMPLE, "Sample of tier-change history (schema check)"),
]


def main():
    log("Connecting to Redshift...")
    conn = get_conn()
    log("Connected.")

    failed = []
    for filename, query, label in JOBS:
        try:
            log(f"Running: {label}")
            df = pd.read_sql(query, conn)
            out_path = os.path.join(OUT_DIR, filename)
            df.to_csv(out_path, index=False)
            log(f"  -> {len(df):,} rows written to {out_path}")
        except Exception as e:
            log(f"  FAILED: {label}: {e}")
            failed.append(label)

    conn.close()

    if failed:
        log(f"Done with errors. Failed: {', '.join(failed)}")
        sys.exit(1)
    else:
        log("All pulls complete.")


if __name__ == "__main__":
    main()
