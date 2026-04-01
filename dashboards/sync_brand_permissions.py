"""
sync_brand_permissions.py
--------------------------
Queries the RP MySQL replica for tutor brand permissions,
saves as CSV, and pushes to GitHub so Streamlit Cloud can read it.
Runs daily via cron alongside sync_exam_data.py.
"""

import os
import io
import sys
import mysql.connector
import pandas as pd
from datetime import datetime
import time

def run_with_retry(func, max_attempts=5, delay=120):
    """Retry func up to max_attempts times with delay seconds between attempts."""
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
GITHUB_PATH   = "data/brand_permissions.csv"

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
    CONCAT(u.first_name, ' ', u.last_name) AS tutor_name,
    e.tier_id,
    b.department_id,
    bt.name AS brand_name
FROM orbit_production.brand_permissions bp
JOIN orbit_production.users u ON bp.user_id = u.id
JOIN orbit_production.brands b ON bp.brand_id = b.id
JOIN orbit_production.brand_translations bt ON (bp.brand_id = bt.brand_id AND bt.locale = 'en')
JOIN orbit_production.employees e ON u.id = e.user_id
WHERE e.type = 'Tutor'
AND e.end_date IS NULL
ORDER BY tutor_name, brand_name
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
        "message": f"Auto-update brand permissions — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
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
