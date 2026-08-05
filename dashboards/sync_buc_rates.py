"""
sync_buc_rates.py
--------------------------
Queries the RP MySQL replica (orbit_production) for tutor BUC/instruction pay rates,
brand acceptance toggles, and tier info. Saves as CSV, pushes to GitHub so
Streamlit Cloud can read it. Runs daily via cron alongside sync_brand_permissions.py
and sync_exam_data.py.
"""

import os
import sys
import mysql.connector
import pandas as pd
from datetime import datetime
import time

def run_with_retry(func, max_attempts=5, delay=120):
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as e:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] All {max_attempts} attempts failed.")
                raise

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RP_HOST       = os.environ["RP_HOST"]
RP_PORT       = int(os.environ.get("RP_PORT", 3306))
RP_USER       = os.environ["RP_USER"]
RP_PASSWORD   = os.environ["RP_PASSWORD"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ["GITHUB_REPO"]
GITHUB_PATH   = "data/buc_rates.csv"

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "sync_log.txt")),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

QUERY = """
SELECT
    e1.id AS tutor_id,
    t_users.first_name,
    t_users.last_name,
    CONCAT(t_users.first_name,' ',t_users.last_name) AS tutor,
    t_users.email,
    CONCAT(m_users.first_name,' ',m_users.last_name) AS manager,
    DATE(e1.hire_date) AS hire_date,
    e1.rippling_handle AS rippling_id,
    tiers.name AS tier,
    e1.delivery_target,
    e1.hourly_rate AS instruction_rate,
    e1.accept_new_students,
    pay_rates.rate AS buc_rate,
    brand_preferences.accept_new_students AS buc_accepting,
    brand_preferences.lock_accept_new_students AS buc_accepting_lock,
    MAX(CASE WHEN tags.name LIKE 'buc%%'
        THEN 1
        ELSE 0
        END) AS buc_tag,
    MAX(CASE WHEN tags.name = 'departing'
        THEN 1
        ELSE 0
        END) AS departing
FROM orbit_production.employees e1
    LEFT JOIN orbit_production.pay_rates
        ON (pay_rates.employee_id = e1.id
        AND pay_rates.brand_id = 42)
    LEFT JOIN orbit_production.brand_preferences
        ON (brand_preferences.employee_id = e1.id
        AND brand_preferences.brand_id = 42)
    JOIN orbit_production.team_members
        ON team_members.member_id = e1.id
    JOIN orbit_production.teams
        ON teams.id = team_members.team_id
    JOIN orbit_production.users t_users
        ON e1.user_id = t_users.id
    JOIN orbit_production.employees e2
        ON e2.id = teams.manager_id
    JOIN orbit_production.users m_users
        ON e2.user_id = m_users.id
    JOIN orbit_production.tiers
        ON e1.tier_id = tiers.id
    LEFT JOIN orbit_production.taggings
        ON (taggings.taggable_id = e1.id
        AND taggings.taggable_type = 'Employee')
    LEFT JOIN orbit_production.tags
        ON taggings.tag_id = tags.id
WHERE 1=1
    AND e1.end_date IS NULL
    AND e1.type = 'Tutor'
    AND e1.tier_id IS NOT NULL
    AND t_users.title = 'Tutor'
GROUP BY e1.id
"""

def fetch_data():
    log.info("Connecting to MySQL...")
    conn = mysql.connector.connect(
        host=RP_HOST, port=RP_PORT,
        user=RP_USER, password=RP_PASSWORD,
        connection_timeout=30,
        charset="utf8mb4",
        auth_plugin="mysql_native_password",
    )
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SET SESSION MAX_EXECUTION_TIME=120000")
        cursor.execute(QUERY)
        rows = cursor.fetchall()
        cursor.close()
    finally:
        conn.close()
    df = pd.DataFrame(rows)
    log.info(f"Fetched {len(df):,} rows.")
    df["fetched_at"] = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    return df

def push_to_github(csv_content):
    import requests, base64
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    existing_sha = None
    r = requests.get(api_url, headers=headers, timeout=15)
    if r.status_code == 200:
        existing_sha = r.json().get("sha")
    encoded = base64.b64encode(csv_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": f"Auto-update BUC rates — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
        "content": encoded,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    log.info(f"Pushed to GitHub: {GITHUB_REPO}/{GITHUB_PATH}")

if __name__ == "__main__":
    def _run():
        df = fetch_data()
        csv_content = df.to_csv(index=False)
        push_to_github(csv_content)
        log.info("Done ✅")
    run_with_retry(_run)
