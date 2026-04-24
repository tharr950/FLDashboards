"""
sync_parent_update_videos.py
────────────────────────────
1. Pulls weekly parent update compliance data from Redshift
2. For every row where parent update sent = True, opens the course
   contact page in headless Chrome, finds the correct tutor message
   within the week date range, checks for a video, and captures duration.
3. Runs scraping in parallel (3 workers by default) for speed.
4. Saves results to CSV and pushes to GitHub.

Run weekly via cron or GitHub Actions.

Dependencies:
    pip install psycopg2-binary selenium webdriver-manager requests pandas

Environment variables:
    REDSHIFT_HOST, REDSHIFT_PORT, REDSHIFT_DB, REDSHIFT_USER, REDSHIFT_PASSWORD
    RP_ADMIN_EMAIL, RP_ADMIN_PASSWORD
    GITHUB_TOKEN, GITHUB_REPO        (e.g. "tylerharrington/fl-dashboards-data")
    GITHUB_DATA_PATH                 (optional, default "data/parent_update_videos.csv")
    SCRAPE_WORKERS                   (optional, default 3)
"""

import os
import re
import time
import base64
import logging
import requests
import psycopg2
import pandas as pd
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

REDSHIFT_CREDS = {
    "host":     os.environ.get("REDSHIFT_HOST",     ""),
    "port":     int(os.environ.get("REDSHIFT_PORT", 5439)),
    "dbname":   os.environ.get("REDSHIFT_DB",       ""),
    "user":     os.environ.get("REDSHIFT_USER",     ""),
    "password": os.environ.get("REDSHIFT_PASSWORD", ""),
}

RP_BASE_URL  = "https://admin.revolutionprep.com"
RP_EMAIL     = os.environ.get("RP_ADMIN_EMAIL",    "")
RP_PASSWORD  = os.environ.get("RP_ADMIN_PASSWORD", "")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO",  "")
GITHUB_PATH  = os.environ.get("GITHUB_DATA_PATH", "data/parent_update_videos.csv")
OUTPUT_FILE  = "parent_update_videos.csv"

WORKERS    = int(os.environ.get("SCRAPE_WORKERS", 3))
CLICK_WAIT = 2
PAGE_WAIT  = 4

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Week range
# ─────────────────────────────────────────────────────────────────────────────

def get_previous_week():
    import zoneinfo
    pacific       = zoneinfo.ZoneInfo("US/Pacific")
    today_pacific = datetime.now(pacific).date()
    # Find the most recently completed Saturday
    # weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # Days since last Saturday:
    days_since_saturday = (today_pacific.weekday() + 2) % 7
    if days_since_saturday == 0:
        days_since_saturday = 7  # if today IS Saturday, go back to previous Saturday
    last_saturday = today_pacific - timedelta(days=days_since_saturday)
    week_start    = last_saturday - timedelta(days=6)  # Previous Sunday
    week_end      = last_saturday + timedelta(days=1)  # +1 day buffer for video scraping only (Eastern timezone edge cases)
    log.info(f"Previous week: {week_start} -> {week_end}")
    return (
        datetime(week_start.year, week_start.month, week_start.day),
        datetime(week_end.year,   week_end.month,   week_end.day),
    )

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Redshift
# ─────────────────────────────────────────────────────────────────────────────

SQL = """
WITH previous_week AS (
  SELECT
    (CURRENT_TIMESTAMP AT TIME ZONE 'US/Pacific')::date
      - EXTRACT(DOW FROM (CURRENT_TIMESTAMP AT TIME ZONE 'US/Pacific'))::int
      - 7 AS week_start,
    (CURRENT_TIMESTAMP AT TIME ZONE 'US/Pacific')::date
      - EXTRACT(DOW FROM (CURRENT_TIMESTAMP AT TIME ZONE 'US/Pacific'))::int
      - 1 AS week_end  -- strict Saturday, no buffer
),
weekly_sessions AS (
  SELECT
    s.id AS session_id,
    s.starts_at AS session_start,
    s.attendances_attended_count,
    c.id AS course_id,
    b.name AS brand_name,
    st.id AS student_id,
    u_st.first_name || ' ' || u_st.last_name AS student_name,
    s.supervisor_id,
    u_tutor.first_name || ' ' || u_tutor.last_name AS tutor_name,
    t.name AS faculty_leader_name,
    CASE
      WHEN b.name = 'Trial' THEN s.id
      WHEN b.name IN ('Group Course','Small Group Course','Boot Camp','SGC','Boot Camps') THEN c.id
      ELSE st.id
    END AS update_unit_id,
    CASE WHEN s.attendances_attended_count > 0 THEN 1 ELSE 0 END AS update_required_flag
  FROM dw.sessions s
  JOIN dw.courses c         ON s.course_id = c.id
  JOIN dw.brands b          ON b.id = c.brand_id
  JOIN dw.enrollments en    ON en.course_id = c.id
  JOIN dw.students st       ON st.id = en.enrollee_id
  JOIN dw.users u_st        ON u_st.id = st.user_id
  JOIN dw.employees e_tutor ON s.supervisor_id = e_tutor.id
  JOIN dw.users u_tutor     ON u_tutor.id = e_tutor.user_id
  LEFT JOIN dw.team_members tm ON e_tutor.id = tm.member_id
  LEFT JOIN dw.teams t      ON tm.team_id = t.id
  WHERE s.starts_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
    AND b.name NOT IN (
      'Special Events','Seminar','Professional Development',
      '1-on-1 Meetings','Group Meetings','Special Event',
      'Self Study','Parent Event'
    )
),
updates_sent AS (
  -- Both types count for compliance rate
  SELECT DISTINCT ca.employee_id, s.course_id
  FROM dw.contact_activities ca
  JOIN dw.sessions s ON ca.regarding_id = s.id
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Session'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
  UNION
  -- Catch updates stored at course level (e.g. Progress Updates)
  SELECT DISTINCT ca.employee_id, ca.regarding_id AS course_id
  FROM dw.contact_activities ca
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Course'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
),
parent_updates_only AS (
  -- Only Parent Updates count for video scraping
  SELECT DISTINCT ca.employee_id, s.course_id
  FROM dw.contact_activities ca
  JOIN dw.sessions s ON ca.regarding_id = s.id
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type = 'Parent Update'
    AND ca.regarding_type = 'Session'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
  UNION
  -- Catch Parent Updates stored at course level
  SELECT DISTINCT ca.employee_id, ca.regarding_id AS course_id
  FROM dw.contact_activities ca
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type = 'Parent Update'
    AND ca.regarding_type = 'Course'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
),
-- One row per update unit — exactly like the reference query
-- For 1-on-1: one row per student (update_unit_id = student_id)
-- For group: one row per course (update_unit_id = course_id)
-- For trial: one row per session (update_unit_id = session_id)
homework_mentioned AS (
  -- Check if update body mentions homework-related keywords
  SELECT DISTINCT ca.employee_id, s.course_id
  FROM dw.contact_activities ca
  JOIN dw.sessions s ON ca.regarding_id = s.id
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Session'
    AND ca.body IS NOT NULL
    AND LOWER(ca.body) SIMILAR TO '%(homework|home work|assignment|assignments|practice problems|practice work|workbook|independent work|study guide|worksheet|exercises)%'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
  UNION
  SELECT DISTINCT ca.employee_id, ca.regarding_id AS course_id
  FROM dw.contact_activities ca
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Course'
    AND ca.body IS NOT NULL
    AND LOWER(ca.body) SIMILAR TO '%(homework|home work|assignment|assignments|practice problems|practice work|workbook|independent work|study guide|worksheet|exercises)%'
    AND ca.created_at::date BETWEEN
        (SELECT week_start FROM previous_week)
    AND (SELECT week_end   FROM previous_week)
),
unit_summary AS (
  SELECT
    ws.tutor_name,
    ws.faculty_leader_name,
    ws.update_unit_id,
    ws.brand_name,
    ws.supervisor_id,
    -- Pick one representative course_id for scraping
    MIN(ws.course_id) AS course_id,
    -- Display name
    MAX(CASE
      WHEN ws.brand_name IN ('Group Course','Small Group Course','Boot Camp','SGC','Boot Camps')
      THEN ws.brand_name || ' (Course ' || ws.course_id::varchar || ')'
      WHEN ws.brand_name = 'Trial'
      THEN 'Trial (Session ' || ws.update_unit_id::varchar || ')'
      ELSE ws.student_name
    END) AS display_name,
    MAX(ws.update_required_flag) AS update_required_flag,
    -- Update was sent if ANY course this student attended had an update sent
    MAX(CASE
      WHEN ws.update_required_flag = 1 AND us.course_id IS NOT NULL THEN 1
      ELSE 0
    END) AS update_sent_flag,
    MAX(CASE
      WHEN ws.update_required_flag = 1 AND pu.course_id IS NOT NULL THEN 1
      ELSE 0
    END) AS parent_update_sent_flag,
    MAX(CASE
      WHEN ws.update_required_flag = 1 AND hw.course_id IS NOT NULL THEN 1
      ELSE 0
    END) AS homework_mentioned_flag
  FROM weekly_sessions ws
  LEFT JOIN updates_sent us
    ON ws.course_id = us.course_id
    AND ws.supervisor_id = us.employee_id
  LEFT JOIN parent_updates_only pu
    ON ws.course_id = pu.course_id
    AND ws.supervisor_id = pu.employee_id
  LEFT JOIN homework_mentioned hw
    ON ws.course_id = hw.course_id
    AND ws.supervisor_id = hw.employee_id
  GROUP BY
    ws.tutor_name, ws.faculty_leader_name, ws.update_unit_id,
    ws.brand_name, ws.supervisor_id
)
SELECT
  (SELECT week_start FROM previous_week) AS "week of",
  tutor_name          AS "tutor",
  faculty_leader_name AS "faculty leader",
  display_name        AS "student",
  course_id           AS "course id",
  brand_name          AS "brand",
  update_required_flag AS "sessions attended",
  0                    AS "sessions unattended",
  CASE
    WHEN update_required_flag = 0 THEN 'N/A'
    WHEN update_sent_flag     = 1 THEN 'True'
    ELSE                               'False'
  END AS "parent update sent",
  CASE
    WHEN update_required_flag    = 0 THEN 'N/A'
    WHEN parent_update_sent_flag = 1 THEN 'True'
    ELSE                               'False'
  END AS "parent update only sent",
  update_required_flag AS "update required",
  CASE
    WHEN update_sent_flag        = 0 THEN 'False'
    WHEN homework_mentioned_flag = 1 THEN 'True'
    ELSE                               'False'
  END AS "homework mentioned"
FROM unit_summary
ORDER BY tutor_name, display_name
"""

def fetch_sql_data() -> pd.DataFrame:
    log.info("Connecting to Redshift ...")
    conn = psycopg2.connect(**REDSHIFT_CREDS, connect_timeout=30)
    try:
        df = pd.read_sql(SQL, conn)
    finally:
        conn.close()
    log.info(f"  -> {len(df)} rows | {(df['parent update sent'] == 'True').sum()} updates sent")
    return df

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Chrome driver (one per worker thread)
# ─────────────────────────────────────────────────────────────────────────────

def build_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    )
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
    )

def login(driver: webdriver.Chrome) -> None:
    driver.get(f"{RP_BASE_URL}/users/sign_in")
    time.sleep(4)
    driver.find_element(By.NAME, "login").send_keys(RP_EMAIL)
    driver.find_element(By.NAME, "password").send_keys(RP_PASSWORD)
    driver.find_element(By.NAME, "password").send_keys("\n")
    time.sleep(5)
    if "login" in driver.current_url:
        raise RuntimeError("Login failed — check RP_ADMIN_EMAIL / RP_ADMIN_PASSWORD.")

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Scrape helpers
# ─────────────────────────────────────────────────────────────────────────────

def date_in_range(date_str: str, week_start: datetime, week_end: datetime) -> bool:
    try:
        dt = datetime.strptime(date_str.strip(), "%m/%d/%y")
        return week_start <= dt <= week_end
    except Exception:
        return False

def get_from_name(driver: webdriver.Chrome):
    html = driver.page_source
    match = re.search(r'From:.*?"([^"]+)"', html)
    if match:
        return match.group(1).strip()
    match = re.search(r'From:\s*([A-Z][a-z]+ [A-Z][a-z]+)\s*<', html)
    return match.group(1).strip() if match else None

def get_video_info(driver: webdriver.Chrome) -> dict:
    result = driver.execute_script("""
        var v = document.querySelector('video');
        if (!v) return {found: false, duration: null};
        return {found: true, duration: v.duration};
    """)
    if result["found"] and result["duration"]:
        secs = int(result["duration"])
        result["duration_fmt"] = f"{secs // 60}:{secs % 60:02d}"
    else:
        result["duration_fmt"] = None
    return result

def is_logged_out(driver: webdriver.Chrome) -> bool:
    """Check if we've been redirected to the login page."""
    try:
        return "sign_in" in driver.current_url or "login" in driver.current_url
    except Exception:
        return False

def scrape_course(
    driver: webdriver.Chrome,
    course_id: int,
    tutor_name: str,
    week_start: datetime,
    week_end: datetime,
) -> dict:
    base = {"video found": False, "video duration": "N/A", "scrape error": None}

    try:
        driver.get(f"{RP_BASE_URL}/courses/{course_id}/contact")
    except Exception as e:
        base["scrape error"] = f"Page load failed: {e}"
        return base

    # Re-login if session expired
    if is_logged_out(driver):
        log.warning(f"    Session expired — re-logging in before course {course_id}")
        try:
            login(driver)
            driver.get(f"{RP_BASE_URL}/courses/{course_id}/contact")
        except Exception as e:
            base["scrape error"] = f"Re-login failed: {e}"
            return base

    # Smart wait — poll for sidebar up to 12 seconds instead of fixed sleep
    try:
        items = WebDriverWait(driver, 12).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "li.list-group-item"))
        )
    except Exception:
        # Sidebar never appeared — fall back to checking what's on the page
        items = driver.find_elements(By.CSS_SELECTOR, "li.list-group-item")

    if not items:
        base["scrape error"] = "No sidebar messages found"
        return base

    matched = False
    parent_update_result = None
    any_result = None

    for item in items:
        try:
            text = item.text.strip()
            if not text:
                continue
            date_str = text.split("\n")[0].strip()
            if not date_in_range(date_str, week_start, week_end):
                continue
            driver.execute_script("arguments[0].click();", item)
            time.sleep(CLICK_WAIT)
            from_name = get_from_name(driver)
            if not from_name or tutor_name.lower() not in from_name.lower():
                continue
            matched = True
            time.sleep(CLICK_WAIT)  # Wait for message content to load
            # Check h1 title to determine message type (case-insensitive)
            try:
                h1_els = driver.find_elements(By.CSS_SELECTOR, "h1")
                h1_text = h1_els[0].text.lower() if h1_els else ""
                item_text_lower = item.text.lower()
                is_parent_update = "parent update" in h1_text or                     ("parent update" in item_text_lower and "progress update" not in item_text_lower)
            except Exception:
                item_text_lower = item.text.lower()
                is_parent_update = "parent update" in item_text_lower and "progress update" not in item_text_lower
            video = get_video_info(driver)
            result = {
                "video found":    video["found"],
                "video duration": video["duration_fmt"] if video["found"] else "N/A",
                "scrape error":   None,
            }
            if is_parent_update:
                parent_update_result = result
                break  # Found a Parent Update — stop looking
            elif any_result is None:
                any_result = result  # Keep Progress Update as fallback
        except Exception as e:
            log.warning(f"    Sidebar item error (course {course_id}): {e}")
            continue

    if parent_update_result:
        return parent_update_result
    if any_result:
        return any_result
    if not matched:
        base["scrape error"] = f"No message from '{tutor_name}' in date range"
    return base

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Worker: each thread gets its own driver + batch of courses
# ─────────────────────────────────────────────────────────────────────────────

def worker_scrape_batch(
    batch: list[tuple],
    week_start: datetime,
    week_end: datetime,
    worker_id: int,
    total: int,
    completed_counter: list,  # mutable list used as a shared counter
) -> dict:
    """
    Scrapes a batch of (course_id, tutor_name) tuples.
    Logs in once, then processes each course sequentially within the batch.
    Returns a dict keyed by (course_id, tutor_name).
    """
    results = {}
    # Stagger worker startup to avoid simultaneous Chrome launches crashing macOS
    time.sleep((worker_id - 1) * 8)
    driver  = build_driver()

    try:
        log.info(f"[Worker {worker_id}] Starting — {len(batch)} courses to scrape")
        login(driver)
        log.info(f"[Worker {worker_id}] Logged in")

        RESTART_EVERY = 100  # restart browser every N courses to free memory
        for i, (course_id, tutor_name) in enumerate(batch):
            # Periodic browser restart to prevent memory accumulation
            if i > 0 and i % RESTART_EVERY == 0:
                log.info(f"[Worker {worker_id}] Restarting browser after {i} courses to free memory...")
                try:
                    driver.quit()
                except Exception:
                    pass
                time.sleep(3)
                driver = build_driver()
                login(driver)
                log.info(f"[Worker {worker_id}] Browser restarted ✅")

            result = scrape_course(driver, course_id, tutor_name, week_start, week_end)
            results[(course_id, tutor_name)] = result

            # Increment shared counter and log progress
            completed_counter[0] += 1
            done = completed_counter[0]
            icon = "✅" if result["video found"] else ("⚠️" if result["scrape error"] else "✗")
            log.info(
                f"[Worker {worker_id}] [{done}/{total}] "
                f"Course {course_id} — {tutor_name} | "
                f"{icon} video={result['video found']} "
                f"duration={result['video duration']} "
                f"error={result['scrape error']}"
            )

    except Exception as e:
        log.error(f"[Worker {worker_id}] Fatal error: {e}")
    finally:
        driver.quit()
        log.info(f"[Worker {worker_id}] Done — browser closed")

    return results

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Orchestrate with parallel workers
# ─────────────────────────────────────────────────────────────────────────────

def run() -> pd.DataFrame:
    week_start, week_end = get_previous_week()

    df = fetch_sql_data()

    # Deduplicate — one scrape per unique (course id, tutor) combo
    # Only scrape rows where a Parent Update (not just Progress Update) was sent
    to_scrape = (
        df[df["parent update only sent"].astype(str) == "True"][["course id", "tutor"]]
        .drop_duplicates()
        .reset_index(drop=True)
    )
    combos = list(zip(to_scrape["course id"].astype(int), to_scrape["tutor"].astype(str)))
    total  = len(combos)
    log.info(f"{total} unique (course id, tutor) combos — running with {WORKERS} parallel workers")

    # Split combos into WORKERS roughly equal batches
    batches = [combos[i::WORKERS] for i in range(WORKERS)]
    # Remove empty batches if fewer combos than workers
    batches = [b for b in batches if b]

    # Shared mutable counter for progress logging across threads
    completed_counter = [0]

    scrape_cache = {}

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {
            executor.submit(
                worker_scrape_batch,
                batch,
                week_start,
                week_end,
                worker_id + 1,
                total,
                completed_counter,
            ): worker_id
            for worker_id, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            worker_id = futures[future]
            try:
                batch_results = future.result()
                scrape_cache.update(batch_results)
            except Exception as e:
                log.error(f"Worker {worker_id + 1} raised an exception: {e}")

    log.info(f"All workers finished — {len(scrape_cache)} courses scraped")

    # Map results back onto every row in the full dataframe
    video_found_col    = []
    video_duration_col = []
    scrape_error_col   = []

    for _, row in df.iterrows():
        if str(row["parent update only sent"]) == "True":
            key = (int(row["course id"]), str(row["tutor"]))
            res = scrape_cache.get(key, {})
            video_found_col.append(res.get("video found",    False))
            video_duration_col.append(res.get("video duration", "N/A"))
            scrape_error_col.append(res.get("scrape error",   None))
        else:
            video_found_col.append(None)
            video_duration_col.append("N/A")
            scrape_error_col.append(None)

    df["video found"]    = video_found_col
    df["video duration"] = video_duration_col
    df["scrape error"]   = scrape_error_col
    df["fetched_at"]     = datetime.now(timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")

    return df

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Save + push to GitHub
# ─────────────────────────────────────────────────────────────────────────────

def push_to_github(csv_content: str) -> None:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GITHUB_TOKEN or GITHUB_REPO not set — skipping GitHub push.")
        return

    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }

    existing_sha = None
    r = requests.get(api_url, headers=headers, timeout=15)
    if r.status_code == 200:
        existing_sha = r.json().get("sha")

    encoded = base64.b64encode(csv_content.encode("utf-8")).decode("utf-8")
    payload = {
        "message": f"sync: parent update videos {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
        "content": encoded,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    log.info(f"Pushed to GitHub: {GITHUB_REPO}/{GITHUB_PATH}")

def save_results(df: pd.DataFrame) -> None:
    csv_content = df.to_csv(index=False)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(csv_content)
    log.info(f"Saved locally -> {OUTPUT_FILE}")
    push_to_github(csv_content)

# ─────────────────────────────────────────────────────────────────────────────
# STREAMLIT LOADER — paste into your data_loaders.py
# ─────────────────────────────────────────────────────────────────────────────
#
# @st.cache_data(ttl=3600)
# def load_parent_update_videos():
#     import urllib.request, io
#     github_repo  = st.secrets["github"]["repo"]
#     github_token = st.secrets["github"]["token"]
#     github_path  = st.secrets["github"].get(
#         "parent_update_video_path", "data/parent_update_videos.csv"
#     )
#     req = urllib.request.Request(
#         f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}",
#         headers={"Authorization": f"token {github_token}"}
#     )
#     with urllib.request.urlopen(req, timeout=30) as resp:
#         csv_content = resp.read().decode("utf-8")
#     df = pd.read_csv(io.StringIO(csv_content))
#     fetched_at = df["fetched_at"].iloc[0] if "fetched_at" in df.columns and not df.empty else "unknown"
#     df = df.drop(columns=["fetched_at"], errors="ignore")
#     return df, fetched_at

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    missing = [k for k in [
        "REDSHIFT_HOST", "REDSHIFT_DB", "REDSHIFT_USER", "REDSHIFT_PASSWORD",
        "RP_ADMIN_EMAIL", "RP_ADMIN_PASSWORD",
    ] if not os.environ.get(k)]

    if missing:
        log.error(f"Missing environment variables: {missing}")
        sys.exit(1)

    log.info("=" * 60)
    log.info("sync_parent_update_videos.py — starting")
    log.info("=" * 60)

    result_df = run()

    sent  = (result_df["parent update sent"] == "True").sum()
    found = result_df["video found"].sum()
    errs  = result_df["scrape error"].notna().sum()

    log.info("── Summary ──────────────────────────────────────────")
    log.info(f"  Total rows      : {len(result_df)}")
    log.info(f"  Updates sent    : {sent}")
    log.info(f"  Videos found    : {found} / {sent} ({found/sent*100:.0f}%)" if sent else "  Updates sent: 0")
    log.info(f"  Scrape errors   : {errs}")

    save_results(result_df)
    log.info("Done ✅")