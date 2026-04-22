#!/usr/bin/env python3
"""
sync_snapshots.py
Saves weekly snapshots for grades, exams, archivable students, and unscheduled hours
for all FL teams. Runs weekly via cron regardless of dashboard loads.
"""

import os, sys, base64, json, io
from datetime import datetime
import pandas as pd
import psycopg2
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

RS_HOST     = os.environ["REDSHIFT_HOST"]
RS_PORT     = int(os.environ.get("REDSHIFT_PORT", 5439))
RS_DB       = os.environ["REDSHIFT_DB"]
RS_USER     = os.environ["REDSHIFT_USER"]
RS_PASSWORD = os.environ["REDSHIFT_PASSWORD"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]

# FL configs: (team_name, file_prefix)
FL_CONFIGS = [
    ("Team Cross",       ""),           # Ela uses no prefix
    ("Team De Groot",    "annelies_"),
    ("Team Plamondon",   "ian_"),
    ("Team St. Marie",   "geoff_"),
    ("Team Haase-Alvey", "kristin_"),
    ("Team Pencak",      "nikki_"),
    ("Team Marino",      "katherine_"),
]

def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

def get_redshift_connection():
    return psycopg2.connect(
        host=RS_HOST, port=RS_PORT, dbname=RS_DB,
        user=RS_USER, password=RS_PASSWORD, connect_timeout=30
    )

def get_week_key_and_date():
    today = pd.Timestamp.now()
    # Previous Sunday as week_date
    days_back = (today.dayofweek + 1) % 7 or 7
    week_date = (today - pd.to_timedelta(days_back, unit="d")).strftime("%Y-%m-%d")
    # Use Monday for ISO week_key
    week_key = (pd.Timestamp(week_date) + pd.Timedelta(days=1)).strftime("%Y-W%V")
    return week_key, week_date

def gh_read(path):
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{path}?cb={int(datetime.now().timestamp())}"
    resp = requests.get(url, headers={"Authorization": f"token {GITHUB_TOKEN}"}, timeout=15)
    if resp.status_code == 404:
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(resp.text))

def gh_write(path, df):
    csv_content = df.to_csv(index=False)
    encoded = base64.b64encode(csv_content.encode()).decode()
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    resp = requests.get(api_url, headers=headers)
    sha = resp.json().get("sha") if resp.status_code == 200 else None
    payload = {"message": f"sync: {path.split('/')[-1]} {datetime.now():%Y-%m-%d}", "content": encoded}
    if sha:
        payload["sha"] = sha
    resp = requests.put(api_url, headers=headers, data=json.dumps(payload))
    return resp.status_code in (200, 201)

def append_if_new(existing, new_rows_df, week_key):
    """Append new_rows_df to existing if week_key not already present."""
    if not existing.empty and week_key in existing.get("week_key", pd.Series()).values:
        log(f"  week_key {week_key} already exists, skipping")
        return existing, False
    updated = pd.concat([existing, new_rows_df], ignore_index=True) if not existing.empty else new_rows_df
    return updated, True

# ── Archivable + Unscheduled Hours ───────────────────────────────────────────
def fetch_archivable():
    log("Fetching archivable/unscheduled data from Redshift...")
    conn = get_redshift_connection()
    query = """
        SELECT
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            dw.teams.name AS team_name,
            dw.students.id AS student_id,
            dw.students.user_id,
            dw.courses.id AS course_id,
            dw.courses.brand_id,
            (dw.courses.provisioned_duration - dw.courses.delivered_duration) / 60.0 AS hours_remaining,
            CASE WHEN dw.sessions.starts_at > GETDATE() THEN 1 ELSE 0 END AS has_future_session,
            dw.sessions.starts_at
        FROM dw.tutoring_histories
        JOIN dw.enrollments ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
        JOIN dw.courses ON dw.courses.id = dw.enrollments.course_id
        JOIN dw.sessions ON dw.sessions.course_id = dw.courses.id
            AND dw.sessions.supervisor_id = dw.tutoring_histories.tutor_id
        JOIN dw.students ON dw.students.id = dw.enrollments.enrollee_id
        JOIN dw.employees ON dw.tutoring_histories.tutor_id = dw.employees.id
        JOIN dw.users tutor_users ON dw.employees.user_id = tutor_users.id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        WHERE dw.tutoring_histories.active = true
          AND dw.enrollments.unenrolled_at IS NULL
          AND dw.employees.end_date IS NULL
          AND dw.team_members.member_type = 'Employee'
          AND tutor_users.title = 'Tutor'
          AND dw.courses.brand_id IN (2,41,42,43,47)
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    log(f"  {len(df)} rows fetched")
    return df

def compute_archivable_snapshot(raw_df, team_name, week_key, week_date):
    df = raw_df[raw_df["team_name"] == team_name].copy()
    if df.empty:
        return pd.DataFrame()
    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        # Students with future sessions = active
        active_students = tdf[tdf["has_future_session"] == 1]["student_id"].unique()
        total = len(active_students)
        # Archivable = no future session
        all_students = tdf["student_id"].unique()
        archivable = [s for s in all_students if s not in active_students]
        # Unscheduled hours = hours remaining for active students with no future session scheduled
        unsched = tdf[tdf["student_id"].isin(active_students)].groupby("student_id")["hours_remaining"].max()
        unsched_total = round(unsched[unsched > 0].sum(), 2)
        rows.append({
            "tutor_name": tutor,
            "archivable_students": len(archivable),
            "total_students": total,
            "unscheduled_hours": unsched_total,
            "week_key": week_key,
            "week_date": week_date,
        })
    return pd.DataFrame(rows)

# ── Grades ────────────────────────────────────────────────────────────────────
def fetch_grades():
    log("Fetching grades data from Redshift...")
    conn = get_redshift_connection()
    query = """
        WITH cte_grades AS (
            SELECT orbit_stitch.study_areas.student_id,
                dw.subjects.name AS subject, sas.score, sas.updated_at
            FROM orbit_stitch.study_areas
            LEFT JOIN dw.subjects ON orbit_stitch.study_areas.subject_id = dw.subjects.id
            LEFT JOIN orbit_stitch.study_area_snapshots sas ON orbit_stitch.study_areas.id = sas.study_area_id
            WHERE dw.subjects.category_id IN (1,2,3,4,5,8,9,10,11)
              AND cast(dw.subjects.high_grade AS int) > 8
              AND orbit_stitch.study_areas.archived_at IS NULL
              AND orbit_stitch.study_areas._sdc_deleted_at IS NULL
        ),
        cte_last_30_days_sessions AS (
            SELECT dw.enrollments.enrollee_id AS student_id,
                dw.sessions.supervisor_id AS tutor_id,
                COUNT(DISTINCT dw.sessions.id) AS session_count
            FROM dw.sessions
            JOIN dw.courses ON dw.sessions.course_id = dw.courses.id
            JOIN dw.enrollments ON dw.enrollments.course_id = dw.courses.id
            WHERE dw.sessions.starts_at::DATE BETWEEN (GETDATE()::DATE)-31 AND (GETDATE()::DATE)-1
              AND dw.sessions.attendances_attended_count > 0
              AND dw.courses.brand_id IN (2,41,42,43,47)
            GROUP BY dw.enrollments.enrollee_id, dw.sessions.supervisor_id
        ),
        cte_future AS (
            SELECT dw.enrollments.enrollee_id AS student_id,
                dw.sessions.supervisor_id AS tutor_id
            FROM dw.sessions
            JOIN dw.courses ON dw.sessions.course_id = dw.courses.id
            JOIN dw.enrollments ON dw.enrollments.course_id = dw.courses.id
            WHERE dw.sessions.starts_at >= GETDATE()::DATE
              AND dw.courses.brand_id IN (2,41,42,43,47)
            GROUP BY dw.enrollments.enrollee_id, dw.sessions.supervisor_id
        )
        SELECT
            cte_future.tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            dw.teams.name AS team_name,
            cte_future.student_id,
            cte_grades.score,
            CAST(cte_grades.updated_at AS DATE) AS updated_at
        FROM cte_future
        JOIN cte_last_30_days_sessions lds1
            ON cte_future.student_id = lds1.student_id
            AND cte_future.tutor_id = lds1.tutor_id
        JOIN dw.students ON cte_future.student_id = dw.students.id
        JOIN dw.employees ON cte_future.tutor_id = dw.employees.id
        JOIN dw.users tutor_users ON dw.employees.user_id = tutor_users.id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        LEFT JOIN cte_grades ON dw.students.id = cte_grades.student_id
        WHERE dw.employees.end_date IS NULL
          AND dw.team_members.member_type = 'Employee'
          AND tutor_users.title = 'Tutor'
          AND lds1.session_count > 1
        GROUP BY cte_future.student_id, cte_future.tutor_id, tutor_name,
            team_name, cte_grades.score, cte_grades.updated_at
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    log(f"  {len(df)} rows fetched")
    return df

def compute_grades_snapshot(raw_df, team_name, week_key, week_date):
    df = raw_df[raw_df["team_name"] == team_name].copy()
    if df.empty:
        return pd.DataFrame()
    now = pd.Timestamp.now(tz="UTC")
    if "updated_at" in df.columns:
        df["updated_at"] = pd.to_datetime(df["updated_at"], errors="coerce", utc=True)
        df["days_since"] = (now - df["updated_at"]).dt.days
    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        total = tdf["student_id"].nunique()
        no_grades = int(tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
        has_any   = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded    = tdf[tdf["student_id"].isin(has_any[has_any].index)]
        if not graded.empty and "days_since" in graded.columns:
            latest = graded.groupby("student_id")["days_since"].min()
            stale  = int((latest > 90).sum())
            avg_d  = round(latest.mean(), 1)
        else:
            stale = 0; avg_d = None
        per_student = tdf.groupby("student_id").apply(
            lambda g: g["score"].notna().sum() / len(g) * 100 if len(g) > 0 else 0)
        pct_graded = round(per_student.mean(), 1)
        rows.append({
            "tutor_name": tutor, "total_students": total,
            "students_no_grades": no_grades, "pct_subjects_graded": pct_graded,
            "stale_grade_students": stale, "avg_days_since_update": avg_d,
            "week_key": week_key, "week_date": week_date,
        })
    return pd.DataFrame(rows)

# ── Exams ─────────────────────────────────────────────────────────────────────
def fetch_exams_from_github():
    log("Loading exam data from GitHub CSV...")
    df = gh_read("data/exam_data.csv")
    log(f"  {len(df)} rows loaded")
    return df

def compute_exams_snapshot(raw_df, team_name, week_key, week_date):
    df = raw_df[raw_df["team_name"] == team_name].copy() if "team_name" in raw_df.columns else pd.DataFrame()
    if df.empty:
        return pd.DataFrame()

    # Apply exam validity
    def safe_float(v, default=0):
        try:
            return float(v) if pd.notna(v) else default
        except (ValueError, TypeError):
            return default

    def is_valid(r):
        attempt_ok = pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")
        fam = str(r.get("subject",""))
        sat_fams = ["SAT","Digital SAT","PSAT/NMSQT","Digital PSAT","Digital PSAT/NMSQT","PSAT","PSAT 8/9"]
        act_fams = ["ACT","Digital ACT"]
        if fam in sat_fams:
            return attempt_ok and safe_float(r.get("sat_math")) >= 300                    and safe_float(r.get("sat_rw")) >= 300
        elif fam in act_fams:
            return attempt_ok and safe_float(r.get("act_english")) >= 10                    and safe_float(r.get("act_math")) >= 10                    and safe_float(r.get("act_reading")) >= 10
        return False

    df["exam_valid"] = df.apply(is_valid, axis=1)
    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        total = tdf["student_id"].nunique()
        eligible = tdf[tdf["attended_test_prep_hours"] >= 6]["student_id"].unique() \
                   if "attended_test_prep_hours" in tdf.columns else tdf["student_id"].unique()
        no_exam = 0; stale = 0
        for sid in eligible:
            sdf = tdf[tdf["student_id"] == sid]
            completed = sdf[sdf["exam_valid"] == True]
            if completed.empty:
                no_exam += 1
            else:
                latest = pd.to_datetime(completed["exam_date"], errors="coerce", utc=True).max() \
                         if "exam_date" in completed.columns else None
                if latest is not None and pd.notna(latest):
                    if (pd.Timestamp.now(tz="UTC") - latest).days > 90:
                        stale += 1
        pct = round((len(eligible) - no_exam) / len(eligible) * 100, 1) if len(eligible) > 0 else 0
        rows.append({
            "tutor_name": tutor, "total_students": total,
            "students_no_exam": no_exam, "students_stale_exam": stale,
            "pct_eligible_with_exam": pct,
            "week_key": week_key, "week_date": week_date,
        })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    log("Starting weekly snapshot sync...")
    week_key, week_date = get_week_key_and_date()
    log(f"Week: {week_key} ({week_date})")

    # Fetch raw data
    raw_arch    = fetch_archivable()
    raw_grades  = fetch_grades()
    raw_exams   = fetch_exams_from_github()

    for team_name, prefix in FL_CONFIGS:
        log(f"\nProcessing {team_name} (prefix: '{prefix}')...")

        # ── Archivable + Unscheduled (2-week rolling + history) ──
        arch_summary = compute_archivable_snapshot(raw_arch, team_name, week_key, week_date)
        if not arch_summary.empty:
            # Rolling 2-week file
            rolling_file = f"data/persistent/{prefix}archive_snapshots.csv"
            rolling = gh_read(rolling_file)
            if rolling.empty or week_key not in rolling.get("week_key", pd.Series()).values:
                updated = pd.concat([rolling, arch_summary], ignore_index=True) if not rolling.empty else arch_summary
                if "week_key" in updated.columns:
                    recent = sorted(updated["week_key"].unique())[-2:]
                    updated = updated[updated["week_key"].isin(recent)]
                if gh_write(rolling_file, updated):
                    log(f"  {prefix}archive_snapshots.csv updated ✅")
            # History file
            hist_file = f"data/persistent/{prefix}archive_snapshots_history.csv"
            hist = gh_read(hist_file)
            hist_updated, changed = append_if_new(hist, arch_summary, week_key)
            if changed:
                if gh_write(hist_file, hist_updated):
                    log(f"  {prefix}archive_snapshots_history.csv updated ✅")

        # ── Grades ──
        grades_summary = compute_grades_snapshot(raw_grades, team_name, week_key, week_date)
        if not grades_summary.empty:
            gfile = f"data/persistent/{prefix}grades_snapshots.csv"
            existing = gh_read(gfile)
            updated, changed = append_if_new(existing, grades_summary, week_key)
            if changed:
                if gh_write(gfile, updated):
                    log(f"  {prefix}grades_snapshots.csv updated ✅")

        # ── Exams ──
        exams_summary = compute_exams_snapshot(raw_exams, team_name, week_key, week_date)
        if not exams_summary.empty:
            efile = f"data/persistent/{prefix}exams_snapshots.csv"
            existing = gh_read(efile)
            updated, changed = append_if_new(existing, exams_summary, week_key)
            if changed:
                if gh_write(efile, updated):
                    log(f"  {prefix}exams_snapshots.csv updated ✅")

    log("\nWeekly snapshot sync complete ✅")
