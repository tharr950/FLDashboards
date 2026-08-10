"""
sync_family_availability.py
----------------------------
Pulls student availability JSON from orbit_production MySQL,
expands into day/hour counts (ET), and pushes to GitHub.
Runs daily via cron.
"""

import os, sys, json, time
import mysql.connector
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]

DAY_ORDER = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
ET = ZoneInfo("America/New_York")

def fetch_family_availability():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Connecting to MySQL...")
    conn = mysql.connector.connect(
        host=os.environ["RP_HOST"],
        port=int(os.environ.get("RP_PORT", 3306)),
        user=os.environ["RP_USER"],
        password=os.environ["RP_PASSWORD"],
        database="orbit_production",
        connection_timeout=30,
        charset="utf8mb4",
        auth_plugin="mysql_native_password"
    )
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT
            s.id AS student_id,
            s.graduation_year,
            u.time_zone,
            s.availabilities_json,
            e.course_id
        FROM students s
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN enrollments e ON (e.enrollee_id = s.id AND e.enrollee_type = 'Student')
        WHERE s.availabilities_json IS NOT NULL
          AND s.availabilities_json != 'null'
          AND s.availabilities_json != '[]'
          AND s.availabilities_json != '{}'
    """)
    rows = cur.fetchall()
    conn.close()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Fetched {len(rows):,} rows.")
    return rows

def expand_availability(rows):
    """Expand JSON availability into one row per student per 30-min slot in ET."""
    records = []
    for row in rows:
        try:
            avail = json.loads(row['availabilities_json'])
        except:
            continue
        if not isinstance(avail, list):
            continue

        student_tz_str = row.get('time_zone') or 'America/New_York'
        try:
            student_tz = ZoneInfo(student_tz_str)
        except:
            student_tz = ET

        for block in avail:
            try:
                day   = block.get('day')
                time_str = block.get('time','00:00')
                duration = int(block.get('duration', 60))
                tz_str = block.get('timeZone') or student_tz_str
                try:
                    block_tz = ZoneInfo(tz_str)
                except:
                    block_tz = student_tz

                if day not in DAY_ORDER:
                    continue

                # Parse start time
                h, m = map(int, time_str.split(':'))

                # Use a reference date for each day (week of 2024-01-07 = Sunday)
                dow_idx = DAY_ORDER.index(day)
                ref_date = datetime(2024, 1, 7 + dow_idx, h, m, tzinfo=block_tz)

                # Generate 30-min slots
                slots = duration // 30
                for i in range(slots):
                    slot_time = ref_date + timedelta(minutes=30*i)
                    slot_et   = slot_time.astimezone(ET)
                    slot_dow  = DAY_ORDER[slot_et.weekday() + 1 if slot_et.weekday() < 6 else 0]
                    # weekday(): Mon=0...Sun=6 → we want Sun=0
                    # ET DOW
                    et_dow = slot_et.strftime('%A')  # just use name
                    et_hour = slot_et.hour

                    records.append({
                        'student_id':     row['student_id'],
                        'graduation_year': row.get('graduation_year'),
                        'course_id':      row.get('course_id'),
                        'day_et':         et_dow,
                        'hour_et':        et_hour,
                        'slot_30min':     slot_et.strftime('%H:%M'),
                    })
            except Exception:
                continue

    df = pd.DataFrame(records)
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Expanded to {len(df):,} slot rows.")
    return df

def push_to_github(df):
    import requests, base64
    # Aggregate to day/hour counts before pushing — raw slot data is too large
    agg = df.groupby(['day_et','hour_et']).agg(
        student_count=('student_id','nunique'),
        slot_count=('student_id','count')
    ).reset_index()
    agg['fetched_at'] = datetime.now().strftime('%Y-%m-%d %H:%M UTC')
    csv_content = agg.to_csv(index=False)
    encoded = base64.b64encode(csv_content.encode()).decode()
    api_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    path = "data/cache/family_availability.csv"
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    r = requests.get(url, headers=api_headers, timeout=15)
    payload = {
        "message": f"Auto-update family availability — {datetime.now():%Y-%m-%d %H:%M}",
        "content": encoded,
    }
    if r.status_code == 200:
        payload["sha"] = r.json().get("sha")
    requests.put(url, headers=api_headers, json=payload, timeout=30).raise_for_status()
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Pushed to GitHub: {GITHUB_REPO}/{path}")

if __name__ == "__main__":
    def _run():
        rows = fetch_family_availability()
        df   = expand_availability(rows)
        push_to_github(df)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ Done.")
    run_with_retry(_run)
