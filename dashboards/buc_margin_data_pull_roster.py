"""
buc_margin_data_pull_roster.py
--------------------------------
Re-run of just the roster/rates pull — the first attempt hit Redshift for this
(orbit_stitch.brand_preferences, which doesn't exist), but rates actually live in
the MySQL orbit_production replica, same as sync_buc_rates.py already queries.
Sessions, weekly-hours, and tier-history pulls from the first run were fine — no
need to redo those.

Run from a normal terminal (network access required):

    cd "/Users/tylerharrington/Desktop/Revolution Prep/FL_Dashboards/dashboards"
    python3 buc_margin_data_pull_roster.py

Writes buc_margin_model_data/buc_roster.csv (overwriting the empty/missing one).
"""

import os
from datetime import datetime

import pandas as pd
import mysql.connector
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

OUT_DIR = os.path.join(HERE, "buc_margin_model_data")
os.makedirs(OUT_DIR, exist_ok=True)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


QUERY = """
SELECT
    e1.id AS tutor_id,
    tiers.name AS tier,
    e1.delivery_target,
    e1.hourly_rate AS instruction_rate,
    e1.accept_new_students,
    pay_rates.rate AS buc_pay_rate,
    brand_preferences.accept_new_students AS buc_accepting,
    brand_preferences.lock_accept_new_students AS buc_accepting_lock,
    DATE(e1.hire_date) AS hire_date
FROM orbit_production.employees e1
    JOIN orbit_production.users t_users
        ON e1.user_id = t_users.id
    LEFT JOIN orbit_production.pay_rates
        ON (pay_rates.employee_id = e1.id AND pay_rates.brand_id = 42)
    LEFT JOIN orbit_production.brand_preferences
        ON (brand_preferences.employee_id = e1.id AND brand_preferences.brand_id = 42)
    JOIN orbit_production.tiers
        ON e1.tier_id = tiers.id
WHERE e1.end_date IS NULL
    AND e1.type = 'Tutor'
    AND e1.tier_id IS NOT NULL
    AND t_users.title = 'Tutor'
"""


def main():
    log("Connecting to MySQL (orbit_production)...")
    conn = mysql.connector.connect(
        host=os.environ["RP_HOST"],
        port=int(os.environ.get("RP_PORT", 3306)),
        user=os.environ["RP_USER"],
        password=os.environ["RP_PASSWORD"],
        database="orbit_production",
        connection_timeout=30,
        charset="utf8mb4",
        auth_plugin="mysql_native_password",
    )
    log("Connected.")

    df = pd.read_sql(QUERY, conn)
    conn.close()

    dupes = df["tutor_id"].duplicated().sum()
    if dupes:
        log(f"WARNING: {dupes} duplicate tutor_id rows — pay_rates or brand_preferences "
            f"may have more than one row per (employee, brand=42). Inspect before trusting counts.")

    out_path = os.path.join(OUT_DIR, "buc_roster.csv")
    df.to_csv(out_path, index=False)
    log(f"-> {len(df):,} rows written to {out_path}")


if __name__ == "__main__":
    main()
