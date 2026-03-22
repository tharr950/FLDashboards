import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import os
import io
import psycopg2
import mysql.connector

# ─────────────────────────────────────────────
# REDSHIFT CONNECTION
# ─────────────────────────────────────────────

def get_redshift_connection():
    """Always opens a fresh connection — never cached, so it never goes stale."""
    creds = st.secrets["redshift"]
    return psycopg2.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password=creds["password"],
        connect_timeout=10
    )


# ─────────────────────────────────────────────
# REVOLUTION PREP MYSQL CONNECTION
# ─────────────────────────────────────────────

def get_rp_connection():
    """Opens a fresh MySQL connection to replica.revolutionprep.com."""
    import socket
    creds = st.secrets["rp_db"]
    host  = str(creds["host"])
    port  = int(creds.get("port", 3306))

    # Quick reachability check before handing off to mysql.connector
    # If port is firewalled/unreachable this fails in ~5s instead of hanging
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    result = sock.connect_ex((host, port))
    sock.close()
    if result != 0:
        # Detect the outbound IP Streamlit Cloud is using, to help with firewall whitelisting
        try:
            import urllib.request
            outbound_ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode()
        except Exception:
            outbound_ip = "unknown"
        raise ConnectionError(
            f"Cannot reach {host}:{port} — port may be blocked. "
            f"This server's outbound IP is {outbound_ip}. "
            f"Ask RP IT to whitelist this IP on port 3306."
        )

    return mysql.connector.connect(
        host=host,
        port=port,
        user=str(creds["user"]),
        password=str(creds["password"]),
        connection_timeout=30,
        charset="utf8mb4",
        auth_plugin="mysql_native_password",
    )


@st.cache_data(ttl=3600)
def load_archivable_unscheduled():
    conn = get_redshift_connection()
    query = """
        with cte_courses as
        (select
        dw.courses.id as course_id,
        student_users.first_name|| ' '|| student_users.last_name AS student_name,
        dw.brands.name as brand,
        round(dw.courses.provisioned_duration/60.00,2) as provisioned_hours,
        round(dw.courses.delivered_duration/60.00,2) as delivered_hours,
        round(dw.courses.duration/60.00,2) as duration_hours
        from dw.courses
        join dw.enrollments on dw.courses.id = dw.enrollments.course_id
        join dw.students on dw.enrollments.enrollee_id = dw.students.id
        join dw.users student_users on dw.students.user_id = student_users.id
        join dw.brands on dw.courses.brand_id = dw.brands.id
        left join dw.sessions on dw.courses.id = dw.sessions.course_id
        where 1=1
        and dw.courses.brand_id in (2,41,42,43)
        group by 1,2,3,4,5,6
        )
        select
        dw.tutoring_histories.tutor_id as tutor_id,
        tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
        dw.tiers.name as tier,
        dw.teams.name as team_name,
        cte_courses.course_id as course_id,
        cte_courses.brand,
        cte_courses.student_name,
        min(dw.sessions.starts_at) as first_session_day,
        max(dw.sessions.starts_at) as last_session_day,
        max(dw.sessions.starts_at) < (getdate() -30) as should_archive,
        cte_courses.provisioned_hours - cte_courses.delivered_hours as hours_remaining,
        case when cte_courses.brand = 'Academics' and (cte_courses.provisioned_hours - cte_courses.duration_hours)<0 then 0 else cte_courses.provisioned_hours - cte_courses.duration_hours end as unscheduled_hours
        from dw.tutoring_histories
        JOIN dw.employees ON dw.employees.id = dw.tutoring_histories.tutor_id
        join dw.tiers on dw.employees.tier_id = dw.tiers.id
        JOIN dw.users tutor_users ON tutor_users.id = dw.employees.user_id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        JOIN dw.enrollments ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
        join dw.sessions on (dw.sessions.course_id = dw.enrollments.course_id) and (dw.sessions.supervisor_id = dw.employees.id)
        join cte_courses on dw.enrollments.course_id = cte_courses.course_id
        where 1=1
        and dw.tutoring_histories.active = true
        AND dw.employees.end_date IS NULL
        AND dw.enrollments.unenrolled_at IS NULL
        AND dw.team_members.member_type = 'Employee'
        group by 1,2,3,4,5,6,7,11,12
        order by unscheduled_hours
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    fetched_at = pd.Timestamp.now().strftime("%B %d, %Y at %I:%M %p")
    return df, fetched_at


# ─────────────────────────────────────────────
# GRADES QUERY & LOADER
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_grades_data():
    conn = get_redshift_connection()
    query = """
        with cte_grades as (
            select
                orbit_stitch.study_areas.student_id,
                dw.subjects.name as subject,
                sas.score,
                sas.updated_at
            from orbit_stitch.study_areas
            left join dw.subjects on orbit_stitch.study_areas.subject_id = dw.subjects.id
            left join orbit_stitch.study_area_snapshots sas
                on orbit_stitch.study_areas.id = sas.study_area_id
            where 1=1
            AND dw.subjects.category_id in (1,2,3,4,5,8,9,10,11,12)
            AND orbit_stitch.study_areas.archived_at is null
            AND orbit_stitch.study_areas._sdc_deleted_at is null
        )
        select distinct
            dw.employees.id as tutor_id,
            tutor_users.first_name||' '|| tutor_users.last_name as tutor_name,
            dw.teams.name as team_name,
            dw.students.id as student_id,
            student_users.first_name|| ' '|| student_users.last_name as student_name,
            min(dw.sessions.starts_at) as first_session_day,
            max(dw.sessions.starts_at) as last_session_day,
            cte_grades.subject,
            cte_grades.score,
            cte_grades.updated_at
        from dw.tutoring_histories
        JOIN dw.enrollments ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
        join dw.sessions on (dw.sessions.course_id = dw.enrollments.course_id)
            and (dw.sessions.supervisor_id = dw.tutoring_histories.tutor_id)
        join dw.students on dw.enrollments.enrollee_id = dw.students.id
        join dw.users student_users on dw.students.user_id = student_users.id
        join dw.employees on dw.tutoring_histories.tutor_id = dw.employees.id
        join dw.users tutor_users on dw.employees.user_id = tutor_users.id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        left join cte_grades on dw.students.id = cte_grades.student_id
        where 1=1
        AND dw.tutoring_histories.active = true
        AND dw.enrollments.unenrolled_at IS null
        AND dw.employees.end_date IS null
        AND dw.team_members.member_type = 'Employee'
        group by 1,2,3,4,5,8,9,10
        having min(dw.sessions.starts_at) <= getdate()   -- exclude students not yet met with
           and max(dw.sessions.starts_at) > (getdate() - 30)
        order by 4
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    fetched_at = pd.Timestamp.now().strftime("%B %d, %Y at %I:%M %p")
    return df, fetched_at


# ─────────────────────────────────────────────
# AVAILABILITY COMPLIANCE QUERY & LOADER
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_availability_compliance():
    """
    Returns tutors who have posted 7+ days of availability
    in the current week OR the next week only.
    """
    conn = get_redshift_connection()
    today = pd.Timestamp.now()
    days_since_sunday = (today.weekday() + 1) % 7
    this_sunday = (today - pd.Timedelta(days=days_since_sunday)).strftime("%Y-%m-%d")
    next_sunday = (today - pd.Timedelta(days=days_since_sunday) + pd.Timedelta(weeks=1)).strftime("%Y-%m-%d")

    query = f"""
        with avail_deduped as (
            select distinct
                rp_bi.tutor_availabilities_daily.employee_id,
                rp_bi.tutor_availabilities_daily.full_date,
                rp_bi.dates.first_day_of_week_sunday_start as week_start
            from rp_bi.tutor_availabilities_daily
            join rp_bi.dates on rp_bi.tutor_availabilities_daily.full_date = rp_bi.dates.full_date
            where rp_bi.dates.first_day_of_week_sunday_start >= '{this_sunday}'
              and rp_bi.dates.first_day_of_week_sunday_start <= '{next_sunday}'
        )
        select
            dw.users.first_name||' '||dw.users.last_name as tutor_name,
            dw.employees.id as employee_id,
            dw.addresses.state,
            dw.teams.name as team,
            avail_deduped.week_start
        from avail_deduped
        join dw.employees on avail_deduped.employee_id = dw.employees.id
        join dw.users on dw.employees.user_id = dw.users.id
        join dw.team_members on dw.employees.id = dw.team_members.member_id
        join dw.teams on dw.team_members.team_id = dw.teams.id
        join dw.addresses on dw.users.address_id = dw.addresses.id
        where dw.teams.name <> 'Proctors'
          and dw.employees.end_date IS NULL
        group by 1,2,3,4,5
        having count(distinct avail_deduped.full_date) > 6
        order by 4,1
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    return df


# ─────────────────────────────────────────────
# EXAM DATA QUERY & LOADER
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_exam_data():
    """
    Reads exam data from a pre-built CSV pushed to GitHub by sync_exam_data.py.
    Falls back gracefully if the file isn't available yet.
    """
    try:
        github_repo  = st.secrets["github"]["repo"]    # e.g. "tylerharrington/fl-dashboards-data"
        github_token = st.secrets["github"]["token"]
        github_path  = st.secrets["github"].get("exam_data_path", "data/exam_data.csv")

        url = (
            f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}"
            f"?token={github_token}"
        )
        # Use a proper auth header instead of token in URL for private repos
        import urllib.request
        req = urllib.request.Request(
            f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}",
            headers={"Authorization": f"token {github_token}"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            csv_content = resp.read().decode("utf-8")

        import io
        df = pd.read_csv(io.StringIO(csv_content))

        # Extract fetched_at from the embedded column, then drop it
        if "fetched_at" in df.columns:
            fetched_at = df["fetched_at"].iloc[0] if not df.empty else "unknown"
            df = df.drop(columns=["fetched_at"])
        else:
            fetched_at = "unknown"

        return df, fetched_at

    except Exception as e:
        raise RuntimeError(
            f"Could not load exam data from GitHub: {e}\n"
            f"Make sure sync_exam_data.py has been run at least once and "
            f"[github] secrets are configured in Streamlit."
        )
# ─────────────────────────────────────────────
# SNAPSHOT HELPERS
# ─────────────────────────────────────────────

SNAPSHOT_FILE         = "annelies_archive_snapshots.csv"
GRADES_SNAPSHOT_FILE  = "annelies_grades_snapshots.csv"
EXAMS_SNAPSHOT_FILE   = "annelies_exams_snapshots.csv"


def save_weekly_snapshot(df):
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")

    summary = (
        df.groupby("tutor_name").agg(
            archivable_students=("should_archive",    lambda x: int(x.sum())),
            total_students     =("student_name",      "nunique"),
            unscheduled_hours  =("unscheduled_hours", "sum"),
        )
        .reset_index()
    )
    summary["week_key"]  = week_key
    summary["week_date"] = week_date

    if os.path.exists(SNAPSHOT_FILE):
        existing = pd.read_csv(SNAPSHOT_FILE)
        if week_key in existing["week_key"].values:
            return existing
        updated = pd.concat([existing, summary], ignore_index=True)
    else:
        updated = summary

    updated.to_csv(SNAPSHOT_FILE, index=False)
    return updated


def save_grades_weekly_snapshot(df):
    """
    Saves a per-tutor weekly grades summary.
    Columns captured:
      - total_students        : unique active students
      - students_no_grades    : students with no grade entries at all
      - pct_subjects_graded   : mean % of subjects that have a score across students
      - stale_grade_students  : students whose most recent grade update is >90 days old
      - avg_days_since_update : mean days since last grade update (graded students only)
    """
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")

    if os.path.exists(GRADES_SNAPSHOT_FILE):
        existing = pd.read_csv(GRADES_SNAPSHOT_FILE)
        if week_key in existing["week_key"].values:
            return existing
    else:
        existing = pd.DataFrame()

    now = pd.Timestamp.now()

    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        total_students     = tdf["student_id"].nunique()

        no_grade_students  = tdf[tdf["score"].isna()]["student_id"].nunique()

        # Stale = most recent updated_at across ALL subjects for a student is >90 days ago.
        # Only consider students who have at least one grade entered (others are "no grades", not "stale").
        has_any_grade = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_student_ids = has_any_grade[has_any_grade].index
        graded_df = tdf[tdf["student_id"].isin(graded_student_ids)].copy()
        if not graded_df.empty and "updated_at" in graded_df.columns:
            graded_df["updated_at"] = pd.to_datetime(graded_df["updated_at"], errors="coerce", utc=True)
            graded_df["days_since"] = (now.tz_localize("UTC") - graded_df["updated_at"]).dt.days
            # Most recent update across ALL subjects per student (min days = most recent)
            latest_per_student = graded_df.groupby("student_id")["days_since"].min()
            stale_count        = int((latest_per_student > 90).sum())
            avg_days           = round(latest_per_student.mean(), 1)
        else:
            stale_count = 0
            avg_days    = None

        # % subjects graded per student, then averaged across students
        per_student = tdf.groupby("student_id").apply(
            lambda g: g["score"].notna().sum() / len(g) * 100 if len(g) > 0 else 0
        )
        pct_graded = round(per_student.mean(), 1)

        rows.append({
            "tutor_name":             tutor,
            "total_students":         total_students,
            "students_no_grades":     no_grade_students,
            "pct_subjects_graded":    pct_graded,
            "stale_grade_students":   stale_count,
            "avg_days_since_update":  avg_days,
            "week_key":               week_key,
            "week_date":              week_date,
        })

    summary = pd.DataFrame(rows)

    if not existing.empty:
        updated = pd.concat([existing, summary], ignore_index=True)
    else:
        updated = summary

    updated.to_csv(GRADES_SNAPSHOT_FILE, index=False)
    return updated


def load_snapshots():
    if os.path.exists(SNAPSHOT_FILE):
        df = pd.read_csv(SNAPSHOT_FILE)
        df["week_date"] = pd.to_datetime(df["week_date"])
        return df
    return pd.DataFrame()


def load_grades_snapshots():
    if os.path.exists(GRADES_SNAPSHOT_FILE):
        df = pd.read_csv(GRADES_SNAPSHOT_FILE)
        df["week_date"] = pd.to_datetime(df["week_date"])
        return df
    return pd.DataFrame()


def save_exams_weekly_snapshot(df):
    """
    Saves a per-tutor weekly exam summary snapshot.
    Columns:
      - total_students            : unique test-prep students
      - students_no_exam          : students with 6+ hrs and no completed exam
      - students_stale_exam       : students with a completed exam but >90 days ago
      - avg_sat_improvement       : mean composite SAT/PSAT improvement (first→last)
      - avg_act_improvement       : mean composite ACT improvement (first→last)
      - pct_eligible_with_exam    : % of 6+ hr students who have a completed exam
    """
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")

    if os.path.exists(EXAMS_SNAPSHOT_FILE):
        existing = pd.read_csv(EXAMS_SNAPSHOT_FILE)
        if week_key in existing["week_key"].values:
            return existing
    else:
        existing = pd.DataFrame()

    now = pd.Timestamp.now(tz="UTC")
    rows = []

    for tutor, tdf in df.groupby("tutor_name"):
        total_students   = tdf["student_id"].nunique()
        eligible_ids     = tdf[tdf["test_prep_hours_delivered"] >= 6]["student_id"].unique()
        no_exam_count    = 0
        stale_exam_count = 0

        for sid in eligible_ids:
            sdf = tdf[tdf["student_id"] == sid]
            completed = sdf[sdf["exam_valid_composite"] == True]
            if completed.empty:
                no_exam_count += 1
            else:
                latest_date = pd.to_datetime(completed["exam_date"], errors="coerce", utc=True).max()
                if pd.notna(latest_date) and (now - latest_date).days > 90:
                    stale_exam_count += 1

        pct_eligible = (
            round((len(eligible_ids) - no_exam_count) / len(eligible_ids) * 100, 1)
            if len(eligible_ids) > 0 else None
        )

        rows.append({
            "tutor_name":             tutor,
            "total_students":         total_students,
            "students_no_exam":       no_exam_count,
            "students_stale_exam":    stale_exam_count,
            "pct_eligible_with_exam": pct_eligible,
            "week_key":               week_key,
            "week_date":              week_date,
        })

    summary = pd.DataFrame(rows)
    updated  = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
    updated.to_csv(EXAMS_SNAPSHOT_FILE, index=False)
    return updated


def load_exams_snapshots():
    if os.path.exists(EXAMS_SNAPSHOT_FILE):
        df = pd.read_csv(EXAMS_SNAPSHOT_FILE)
        df["week_date"] = pd.to_datetime(df["week_date"])
        return df
    return pd.DataFrame()


# ─────────────────────────────────────────────
# ANOMALY DETECTION
# ─────────────────────────────────────────────

def compute_anomalies(tutor_name, snap_arch, snap_grades, snap_exams, current):
    """
    Compare a tutor's current metrics against their own historical distribution.
    Returns a dict {metric_key: bool} — True means the current value is
    anomalously HIGH (> mean + 1 std dev) based on the tutor's own history.
    Requires at least 3 historical data points to flag an anomaly.

    current dict keys expected:
        arch_count, unsched_hrs, no_grades, stale_grades, no_exam, stale_exams
    """
    flags = {}

    def _check(history_series, current_val, key):
        clean = history_series.dropna()
        if len(clean) < 3 or current_val is None:
            flags[key] = False
            return
        mean, std = clean.mean(), clean.std()
        if std == 0:
            flags[key] = False
            return
        flags[key] = float(current_val) > mean + std

    # Arch snapshot: archivable_students, unscheduled_hours
    if not snap_arch.empty and tutor_name in snap_arch["tutor_name"].values:
        th = snap_arch[snap_arch["tutor_name"] == tutor_name].sort_values("week_date")
        _check(th["archivable_students"], current.get("arch_count"),   "arch_count")
        _check(th["unscheduled_hours"],   current.get("unsched_hrs"),  "unsched_hrs")
    else:
        flags["arch_count"]  = False
        flags["unsched_hrs"] = False

    # Grades snapshot: students_no_grades, stale_grade_students
    if not snap_grades.empty and tutor_name in snap_grades["tutor_name"].values:
        tg = snap_grades[snap_grades["tutor_name"] == tutor_name].sort_values("week_date")
        _check(tg["students_no_grades"],  current.get("no_grades"),    "no_grades")
        _check(tg["stale_grade_students"],current.get("stale_grades"), "stale_grades")
    else:
        flags["no_grades"]   = False
        flags["stale_grades"] = False

    # Exams snapshot: students_no_exam, students_stale_exam
    if not snap_exams.empty and tutor_name in snap_exams["tutor_name"].values:
        te = snap_exams[snap_exams["tutor_name"] == tutor_name].sort_values("week_date")
        _check(te["students_no_exam"],    current.get("no_exam"),      "no_exam")
        _check(te["students_stale_exam"], current.get("stale_exams"),  "stale_exams")
    else:
        flags["no_exam"]     = False
        flags["stale_exams"] = False

    return flags


# ─────────────────────────────────────────────
# FILE-BASED DATA LOADERS (existing)
# ─────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_dashboard_metrics():
    file = "Dashboard_Metrics.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetricFullData", header=3)
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_progressupdate_metrics():
    file = "Dashboard_Metrics.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="ProgressUpdateEmails", header=0)
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_tutor_concerns():
    file = "Tutor_Concerns.csv"
    if os.path.exists(file):
        try:
            df = pd.read_csv(file)
            return df
        except Exception as e:
            st.warning(f"Could not read {file}: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        try:
            return pd.read_excel(file, sheet_name="AnnualReview")
        except Exception as e:
            st.warning(f"Could not read {file}: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_grade_summary():
    file = "Ela_GradesSummary.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file)
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_concern_groupings():
    file = "Tutor_Concern_Groupings_Explanations_June2025.csv"
    if os.path.exists(file):
        return pd.read_csv(file)
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_monthly_metric_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_repurchases():
    file = "Repurchase_Summary_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="Sheet 1")
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_master_tutor():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        try:
            return pd.read_excel(file, sheet_name="MasterTutor")
        except Exception as e:
            st.error(f"Error reading MasterTutor sheet: {e}")
            return pd.DataFrame()
    else:
        st.error(f"{file} not found")
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_subject_additions():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="SubjectAddition")
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_monthly_metric():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_kpi_data():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()


# ─────────────────────────────────────────────
# WATCH LIST HELPERS
# ─────────────────────────────────────────────

WATCHLIST_FILE = "annelies_watchlist.csv"

def load_watchlist():
    if os.path.exists(WATCHLIST_FILE):
        df = pd.read_csv(WATCHLIST_FILE)
        return df["tutor_name"].dropna().tolist()
    return []

def save_watchlist(tutor_names):
    pd.DataFrame({"tutor_name": tutor_names}).to_csv(WATCHLIST_FILE, index=False)


WATCHLIST_BASELINE_FILE = "annelies_watchlist_baselines.csv"

def load_watchlist_baselines():
    """Load saved baselines for watched tutors (metrics at time of adding)."""
    if os.path.exists(WATCHLIST_BASELINE_FILE):
        return pd.read_csv(WATCHLIST_BASELINE_FILE)
    return pd.DataFrame()


def save_watchlist_baseline(tutor_name, arch_count, unsched_hrs,
                             no_grades, stale_grades, no_exam,
                             stale_exams, hours_per_exam, pct_unscheduled):
    """
    Save a baseline snapshot for a tutor when they are first added to the watch list.
    Core values (arch_count, unsched_hrs, no_grades, stale_grades, no_exam) are
    never overwritten once set. Any column that is missing or NaN is backfilled
    with the current live value so deltas work even for tutors added before all
    columns existed in the CSV.
    """
    today    = pd.Timestamp.now().strftime("%Y-%m-%d")
    existing = load_watchlist_baselines()

    all_cols = {
        "arch_count":      arch_count,
        "unsched_hrs":     unsched_hrs,
        "no_grades":       no_grades,
        "stale_grades":    stale_grades,
        "no_exam":         no_exam,
        "stale_exams":     stale_exams,
        "hours_per_exam":  hours_per_exam,
        "pct_unscheduled": pct_unscheduled,
    }

    if not existing.empty and tutor_name in existing["tutor_name"].values:
        idx = existing[existing["tutor_name"] == tutor_name].index[0]
        patched = False
        for col, val in all_cols.items():
            # Add missing column to DataFrame
            if col not in existing.columns:
                existing[col] = None
                patched = True
            cur = existing.at[idx, col]
            is_null = (cur is None) or (str(cur).strip() in ("", "nan", "None")) or \
                      (isinstance(cur, float) and pd.isna(cur))
            if is_null and val is not None and not (isinstance(val, float) and pd.isna(val)):
                existing.at[idx, col] = val
                patched = True
        if patched:
            existing.to_csv(WATCHLIST_BASELINE_FILE, index=False)
        return  # Never overwrite columns that already have values

    new_row = pd.DataFrame([{"tutor_name": tutor_name, "added_date": today, **all_cols}])
    updated = pd.concat([existing, new_row], ignore_index=True) if not existing.empty else new_row
    updated.to_csv(WATCHLIST_BASELINE_FILE, index=False)


def remove_watchlist_baseline(tutor_name):
    """Remove a tutor's baseline when they are removed from the watch list."""
    existing = load_watchlist_baselines()
    if not existing.empty:
        updated = existing[existing["tutor_name"] != tutor_name]
        updated.to_csv(WATCHLIST_BASELINE_FILE, index=False)


def migrate_watchlist_baselines(current_metrics: dict):
    """
    Ensure every tutor row in the baseline CSV has all expected columns.
    If a column is missing for a tutor, backfill it with the current live value
    so deltas start tracking from this point forward.
    current_metrics: {tutor_name: {col: value, ...}}
    """
    expected_cols = ["arch_count", "unsched_hrs", "no_grades", "stale_grades",
                     "no_exam", "stale_exams", "hours_per_exam", "pct_unscheduled"]
    existing = load_watchlist_baselines()
    if existing.empty:
        return
    changed = False
    for col in expected_cols:
        if col not in existing.columns:
            existing[col] = None
            changed = True
    for idx, row in existing.iterrows():
        tname = row["tutor_name"]
        metrics = current_metrics.get(tname, {})
        for col in expected_cols:
            val = existing.at[idx, col]
            is_missing = (val is None) or (isinstance(val, float) and pd.isna(val))
            if is_missing and col in metrics:
                existing.at[idx, col] = metrics[col]
                changed = True
    if changed:
        existing.to_csv(WATCHLIST_BASELINE_FILE, index=False)


WATCHLIST_NOTES_FILE      = "annelies_watchlist_notes.csv"
WATCHLIST_THRESHOLDS_FILE = "annelies_watchlist_thresholds.csv"

# Default thresholds — used when no custom threshold is set for a tutor
DEFAULT_THRESHOLDS = {
    "arch_count":       1,
    "unsched_hrs":      0,
    "pct_unscheduled":  10,
    "no_grades":        1,
    "stale_grades":     1,
    "no_exam":          1,
    "stale_exams":      1,
}

def load_watchlist_notes():
    if os.path.exists(WATCHLIST_NOTES_FILE):
        return pd.read_csv(WATCHLIST_NOTES_FILE)
    return pd.DataFrame(columns=["tutor_name","note","updated_at"])

def save_watchlist_note(tutor_name, note):
    existing = load_watchlist_notes()
    existing = existing[existing["tutor_name"] != tutor_name]
    new_row  = pd.DataFrame([{
        "tutor_name": tutor_name,
        "note":       note,
        "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    }])
    updated = pd.concat([existing, new_row], ignore_index=True)
    updated.to_csv(WATCHLIST_NOTES_FILE, index=False)

def delete_watchlist_note(tutor_name):
    existing = load_watchlist_notes()
    if not existing.empty:
        existing[existing["tutor_name"] != tutor_name].to_csv(WATCHLIST_NOTES_FILE, index=False)

def load_watchlist_thresholds():
    if os.path.exists(WATCHLIST_THRESHOLDS_FILE):
        return pd.read_csv(WATCHLIST_THRESHOLDS_FILE)
    return pd.DataFrame(columns=["tutor_name"] + list(DEFAULT_THRESHOLDS.keys()))

def save_watchlist_thresholds(tutor_name, thresholds_dict):
    existing = load_watchlist_thresholds()
    existing = existing[existing["tutor_name"] != tutor_name]
    new_row  = pd.DataFrame([{"tutor_name": tutor_name, **thresholds_dict}])
    updated  = pd.concat([existing, new_row], ignore_index=True)
    updated.to_csv(WATCHLIST_THRESHOLDS_FILE, index=False)

def get_tutor_thresholds(tutor_name):
    """Return thresholds for a tutor, falling back to defaults for any missing values."""
    df = load_watchlist_thresholds()
    if not df.empty and tutor_name in df["tutor_name"].values:
        row = df[df["tutor_name"] == tutor_name].iloc[0].to_dict()
        return {k: row.get(k, DEFAULT_THRESHOLDS[k]) for k in DEFAULT_THRESHOLDS}
    return DEFAULT_THRESHOLDS.copy()

def delete_watchlist_thresholds(tutor_name):
    existing = load_watchlist_thresholds()
    if not existing.empty:
        existing[existing["tutor_name"] != tutor_name].to_csv(
            WATCHLIST_THRESHOLDS_FILE, index=False)


# ─────────────────────────────────────────────
# LOGIN SNAPSHOT HELPERS
# ─────────────────────────────────────────────

LOGIN_SNAPSHOT_FILE = "annelies_login_snapshot.csv"

LOGIN_SNAPSHOT_COLS = [
    "team_archivable", "team_unscheduled_hrs",
    "team_no_grades",  "team_stale_grades",
    "team_no_exam",    "team_stale_exams",
    "login_ts",
]

def save_login_snapshot(arch_df, grades_df, exam_df):
    """Capture team-wide totals at login time and write to CSV."""
    team_archivable     = int(arch_df["should_archive"].sum()) if not arch_df.empty else 0
    team_unscheduled    = float(arch_df["unscheduled_hours"].sum()) if not arch_df.empty else 0.0

    if not grades_df.empty:
        team_no_grades  = int(
            grades_df.groupby("student_id")["score"]
            .apply(lambda s: s.isna().all()).sum()
        )
        has_any         = grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_ids      = has_any[has_any].index
        graded          = grades_df[grades_df["student_id"].isin(graded_ids)]
        if not graded.empty and "days_since_update" in graded.columns:
            latest      = graded.groupby("student_id")["days_since_update"].min()
            team_stale_grades = int((latest > 90).sum())
        else:
            team_stale_grades = 0
    else:
        team_no_grades  = 0
        team_stale_grades = 0

    if not exam_df.empty:
        # students with no valid completed exam
        valid_exam_ids  = exam_df[exam_df["exam_valid_composite"] == True]["student_id"].unique() \
                          if "exam_valid_composite" in exam_df.columns else []
        all_ids         = exam_df["student_id"].unique() if "student_id" in exam_df.columns else []
        team_no_exam    = int(len(set(all_ids) - set(valid_exam_ids)))
        # stale = last exam >90 days ago
        if "exam_date" in exam_df.columns:
            exam_df2    = exam_df.copy()
            exam_df2["exam_date"] = pd.to_datetime(exam_df2["exam_date"], errors="coerce", utc=True)
            now         = pd.Timestamp.now(tz="UTC")
            latest_exam = exam_df2.groupby("student_id")["exam_date"].max()
            team_stale_exams = int(((now - latest_exam).dt.days > 90).sum())
        else:
            team_stale_exams = 0
    else:
        team_no_exam    = 0
        team_stale_exams = 0

    snap = pd.DataFrame([{
        "team_archivable":    team_archivable,
        "team_unscheduled_hrs": round(team_unscheduled, 1),
        "team_no_grades":     team_no_grades,
        "team_stale_grades":  team_stale_grades,
        "team_no_exam":       team_no_exam,
        "team_stale_exams":   team_stale_exams,
        "login_ts":           pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    }])
    snap.to_csv(LOGIN_SNAPSHOT_FILE, index=False)
    return snap

def load_login_snapshot():
    """Load the snapshot from the previous login (if any)."""
    if os.path.exists(LOGIN_SNAPSHOT_FILE):
        return pd.read_csv(LOGIN_SNAPSHOT_FILE)
    return pd.DataFrame()

def _login_delta_html(label, current, previous, lower_is_better=True):
    """
    Returns an HTML badge showing current value + delta vs last login.
    lower_is_better=True: increase → red, decrease → green.
    """
    if previous is None or pd.isna(previous):
        delta_html = ""
    else:
        diff = int(current) - int(previous) if isinstance(current, (int, float)) else 0
        if diff == 0:
            delta_html = "<span style='color:#888; font-size:0.8rem;'> (no change)</span>"
        elif (diff > 0 and lower_is_better) or (diff < 0 and not lower_is_better):
            delta_html = f"<span style='color:#cc0000; font-size:0.8rem; font-weight:600;'> ▲ +{diff}</span>"
        else:
            delta_html = f"<span style='color:#1a6e36; font-size:0.8rem; font-weight:600;'> ▼ {diff}</span>"
    return f"<b>{label}:</b> {current}{delta_html}"


# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

def render_app(config):

    st.markdown("""
        <style>
            .main-title {
                font-size: 2.5em;
                font-weight: bold;
                color: #004466;
                margin-bottom: 0.3em;
            }
            .block-container {
                padding-top: 2rem;
            }
            section[data-testid="stSidebar"] {
                background-color: #F1F3F5;
            }
            .metric-label {
                font-size: 1.1rem;
                color: #666;
            }
        </style>
    """, unsafe_allow_html=True)

    grade_summary_df     = load_grade_summary()
    concern_groupings_df = load_concern_groupings()

    st.markdown('<div class="main-title">Annelies Tutor Data 📊</div>', unsafe_allow_html=True)

    st.sidebar.markdown("---")

    # ── Sidebar Navigation ───────────────────────
    _goto = st.session_state.pop("goto_page", None)
    _page_options = [
        "🏠 Home",
        "👀 Watched Tutors",
        "👤 Tutor Profile",
        "Concerns",
        "KPI Table",
        "KPI Trends",
        "Grades Summary",
        "Test Prep & Exams",
        "Archivable Students & Unscheduled Hours"
    ]
    _default_index = _page_options.index(_goto) if _goto in _page_options else 0
    page = st.sidebar.radio(
        "\U0001f4c2 Navigation", _page_options, index=_default_index
    )

    faculty_leader_name = "Annelies de Groot"
    master_tutor_df = load_master_tutor()
    annelies_tutors = master_tutor_df[master_tutor_df["Faculty Leader"] == faculty_leader_name]["Full Name"].sort_values().dropna().unique().tolist()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📋 Annual Reviews")

    annual_review_df = load_annual_reviews()
    monthly_metric_annual_review_df = load_monthly_metric_annual_reviews()
    repurchase_df = load_repurchases()
    annelies_tutors = master_tutor_df[master_tutor_df["Faculty Leader"] == "Annelies de Groot"]["Full Name"].dropna().sort_values().tolist()


    # ─────────────────────────────────────────────
    # PAGE: HOME
    # ─────────────────────────────────────────────

    if page == "🏠 Home":
        st.markdown('<div class="main-title">Good morning, Annelies 👋</div>', unsafe_allow_html=True)
        st.caption("Here's what needs your attention today.")
        _digest_placeholder = st.empty()

        # ── Load all data needed for the briefing ────
        load_errors = []
        with st.spinner("Loading your briefing..."):

            # Archivable / unscheduled
            try:
                raw_arch_df, arch_fetched_at = load_archivable_unscheduled()
                raw_arch_df["should_archive"] = raw_arch_df["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                home_arch_df = raw_arch_df[raw_arch_df["team_name"] == "Team De Groot"].copy()
            except Exception as e:
                home_arch_df = pd.DataFrame()
                load_errors.append(f"Archivable data: {e}")

            # Grades
            try:
                raw_home_grades, _ = load_grades_data()
                home_grades_df = raw_home_grades[raw_home_grades["team_name"] == "Team De Groot"].copy()
                now_utc = pd.Timestamp.now(tz="UTC")
                home_grades_df["updated_at"] = pd.to_datetime(home_grades_df["updated_at"], errors="coerce", utc=True)
                home_grades_df["days_since_update"] = (now_utc - home_grades_df["updated_at"]).dt.days
            except Exception as e:
                home_grades_df = pd.DataFrame()
                load_errors.append(f"Grades data: {e}")

            # Exam data
            try:
                raw_home_exam, _ = load_exam_data()
                home_exam_df = raw_home_exam[raw_home_exam["team_name"] == "Team De Groot"].copy()
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    home_exam_df[dc] = pd.to_datetime(home_exam_df[dc], errors="coerce", utc=True)
                for nc in ["score","act_english","act_math","act_reading","act_science",
                           "sat_math","sat_rw","test_prep_hours_delivered"]:
                    home_exam_df[nc] = pd.to_numeric(home_exam_df[nc], errors="coerce")
                # Compute validity once here so all sections can use it
                if not home_exam_df.empty:
                    _SAT_H = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT",
                              "Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                    _ACT_H = {"ACT","Digital ACT"}
                    home_exam_df["exam_family"] = home_exam_df["subject"].apply(
                        lambda x: "SAT/PSAT" if x in _SAT_H else ("ACT" if x in _ACT_H else "Other"))
                    home_exam_df["exam_valid_composite"] = home_exam_df.apply(
                        lambda r: (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                                   pd.notna(r["sat_rw"])   and r["sat_rw"]   >= 300)
                                  if r["exam_family"] == "SAT/PSAT"
                                  else ((pd.notna(r["act_english"]) and r["act_english"] >= 10 and
                                         pd.notna(r["act_math"])    and r["act_math"]    >= 10 and
                                         pd.notna(r["act_reading"]) and r["act_reading"] >= 10)
                                        if r["exam_family"] == "ACT" else False),
                        axis=1)
            except Exception as e:
                home_exam_df = pd.DataFrame()
                load_errors.append(f"Exam data: {e}")

            # KPI data
            try:
                kpi_home_df = load_kpi_data()
            except Exception as e:
                kpi_home_df = pd.DataFrame()
                load_errors.append(f"KPI data: {e}")

            # Availability compliance
            try:
                avail_df = load_availability_compliance()
                team_avail_df = avail_df[avail_df["team"] == "Team De Groot"].copy()
                if not team_avail_df.empty:
                    team_avail_df["week_start"] = pd.to_datetime(
                        team_avail_df["week_start"], errors="coerce")
            except Exception as e:
                team_avail_df = pd.DataFrame()
                load_errors.append(f"Availability data: {e}")

        # Show any load errors so we can diagnose
        if load_errors:
            with st.expander("⚠️ Some data failed to load — click to see details"):
                for err in load_errors:
                    st.warning(err)

        # ── Since-last-login delta ────────────────────
        # Save a snapshot on the FIRST render of the Home page each session,
        # then show deltas vs that snapshot on subsequent sessions.
        if "login_snapshot_saved" not in st.session_state:
            st.session_state["login_snapshot_saved"] = True
            prev_snap = load_login_snapshot()  # load BEFORE overwriting
            save_login_snapshot(home_arch_df, home_grades_df, home_exam_df)
        else:
            prev_snap = pd.DataFrame()  # already snapped this session — don't reload

        # Always compute current totals for digest use (even if no prev_snap)
        cur_arch    = int(home_arch_df["should_archive"].sum()) if not home_arch_df.empty else 0
        cur_unsched = round(float(home_arch_df["unscheduled_hours"].sum()), 1) if not home_arch_df.empty else 0.0
        cur_ng      = int(home_grades_df.groupby("student_id")["score"].apply(
            lambda s: s.isna().all()).sum()) if not home_grades_df.empty else 0
        if not home_grades_df.empty:
            _has  = home_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            _gids = _has[_has].index
            _graded = home_grades_df[home_grades_df["student_id"].isin(_gids)]
            cur_sg = int((_graded.groupby("student_id")["days_since_update"].min() > 90).sum()) \
                     if not _graded.empty and "days_since_update" in _graded.columns else 0
        else:
            cur_sg = 0

        # ── Since-last-login banner ───────────────────
        def _delta_badge(cur, prev_val, lower_is_better=True):
            try:
                diff = int(cur) - int(prev_val)
            except:
                return str(cur)
            if diff == 0:
                return f"{cur} <span style='color:#888;font-size:0.8rem'>(no change)</span>"
            color = "#cc0000" if (diff > 0 and lower_is_better) or (diff < 0 and not lower_is_better) else "#1a6e36"
            arrow = "▲" if diff > 0 else "▼"
            return f"{cur} <span style='color:{color};font-size:0.8rem;font-weight:600'>{arrow} {diff:+d}</span>"

        if not prev_snap.empty:
            ps         = prev_snap.iloc[0]
            prev_login = str(ps.get("login_ts", "your last visit"))
            items = []
            items.append(f"📦 Archivable: {_delta_badge(cur_arch, ps.get('team_archivable'))}")
            items.append(f"⏳ Unsched hrs: {_delta_badge(cur_unsched, ps.get('team_unscheduled_hrs'))}")
            items.append(f"📋 No grades: {_delta_badge(cur_ng, ps.get('team_no_grades'))}")
            items.append(f"📚 Stale grades: {_delta_badge(cur_sg, ps.get('team_stale_grades'))}")
            items_html = " &nbsp;|&nbsp; ".join(items)
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:10px;
                        padding:10px 16px; margin-bottom:16px; font-size:0.87rem; color:#333;'>
                🕐 <b>Since your last visit</b>
                <span style='color:#aaa; font-size:0.8rem;'>({prev_login})</span>
                &nbsp;—&nbsp; {items_html}
            </div>""", unsafe_allow_html=True)
        else:
            # First visit — show current state with no deltas
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:10px;
                        padding:10px 16px; margin-bottom:16px; font-size:0.87rem; color:#333;'>
                🕐 <b>First visit</b> — baseline saved.
                Deltas will appear on your next login.
                &nbsp;|&nbsp; 📦 {cur_arch} archivable
                &nbsp;|&nbsp; ⏳ {cur_unsched:.0f} unsched hrs
                &nbsp;|&nbsp; 📋 {cur_ng} no grades
                &nbsp;|&nbsp; 📚 {cur_sg} stale grades
            </div>""", unsafe_allow_html=True)

        # ── Helper: card renderer ─────────────────────
        def card(emoji, title, body, color="#fff"):
            bg = {"red":"#fff0f0","green":"#f0fff4","yellow":"#fffbea","blue":"#f0f4ff"}.get(color,"#fff")
            border = {"red":"#ffcccc","green":"#b2f5c8","yellow":"#ffe58f","blue":"#bfd7ff"}.get(color,"#ddd")
            st.markdown(f"""
            <div style='background:{bg}; border:1.5px solid {border}; border-radius:10px;
                        padding:14px 18px; margin-bottom:10px;'>
                <div style='font-size:1.05rem; font-weight:600; margin-bottom:4px;'>{emoji} {title}</div>
                <div style='font-size:0.92rem; color:#444;'>{body}</div>
            </div>""", unsafe_allow_html=True)

        # ── Availability banner ───────────────────────
        if not home_arch_df.empty:
            today          = pd.Timestamp.now()
            days_since_sun = (today.weekday() + 1) % 7
            this_sunday    = today - pd.Timedelta(days=days_since_sun)
            next_sunday    = this_sunday + pd.Timedelta(weeks=1)
            week_label     = this_sunday.strftime("%b %d")
            next_label     = next_sunday.strftime("%b %d")

            if team_avail_df.empty:
                # Nobody on the team has 7+ days posted — this is good
                st.markdown(f"""
                <div style='background:#f0fff4; border:1.5px solid #b2f5c8; border-radius:10px;
                            padding:16px 20px; margin-bottom:20px;
                            font-size:1.02rem; line-height:1.6; color:#276749;'>
                    ✅ <b>Availability Looks Good</b> — No tutors on Team De Groot have 7+ days
                    of availability posted for the current week ({week_label}) or
                    next week ({next_label}).
                </div>""", unsafe_allow_html=True)
            else:
                # Some tutors have 7+ days posted — flag them
                flagged_tutors = sorted(team_avail_df["tutor_name"].unique().tolist())
                names_list     = ", ".join(flagged_tutors)
                st.markdown(f"""
                <div style='background:#cc0000; color:white; border-radius:10px;
                            padding:16px 20px; margin-bottom:20px;
                            font-size:1.02rem; line-height:1.6;'>
                    🚨 <b>Excess Availability Posted</b> — The following tutor(s) have
                    <b>7+ days</b> of availability posted for the current week ({week_label})
                    or next week ({next_label}). Please review.<br>
                    <b style='font-size:1.08rem;'>{names_list}</b>
                </div>""", unsafe_allow_html=True)
                with st.expander("🔍 Debug — raw availability counts"):
                    st.dataframe(team_avail_df, use_container_width=True)

        # ─────────────────────────────────────────────
        # TOP STATS BAR
        # ─────────────────────────────────────────────
        st.markdown("### 📊 Team Snapshot")

        total_active = home_arch_df["student_name"].nunique() if not home_arch_df.empty else "—"
        total_tp     = home_exam_df["student_id"].nunique() if not home_exam_df.empty else "—"

        # KPI averages
        kpi_metrics = [
            "% to Delivery Target",
            "% to Availability Target",
            "% Sessions on Time",
            "% Parents Updates Done on Time",
            "% of Active Students with Progress Updates Completed in last 2 months"
        ]
        kpi_avgs = {}
        if not kpi_home_df.empty:
            kpi_home_df["Date Range Parsed"] = pd.to_datetime(
                kpi_home_df["Date Range"].str.split(" - ").str[0], errors="coerce")
            latest_kpi   = kpi_home_df["Date Range Parsed"].max()
            prev_kpi     = kpi_home_df[kpi_home_df["Date Range Parsed"] < latest_kpi]["Date Range Parsed"].max()
            latest_team  = kpi_home_df[
                (kpi_home_df["Date Range Parsed"] == latest_kpi) &
                (kpi_home_df["Faculty Leader"] == "Annelies de Groot")]
            prev_team    = kpi_home_df[
                (kpi_home_df["Date Range Parsed"] == prev_kpi) &
                (kpi_home_df["Faculty Leader"] == "Annelies de Groot")] if pd.notna(prev_kpi) else pd.DataFrame()
            for m in kpi_metrics:
                if m in latest_team.columns:
                    curr = latest_team[m].mean() * 100
                    prev = prev_team[m].mean() * 100 if not prev_team.empty and m in prev_team.columns else None
                    kpi_avgs[m] = {"curr": curr, "prev": prev,
                                   "delta": curr - prev if prev is not None else None}

        # ── Row 1: Students + KPIs ────────────────────
        st.caption("**📈 KPIs & Students**")
        snap_cols = st.columns(len(kpi_metrics) + 2)
        snap_cols[0].metric("Active Students",    total_active)
        snap_cols[1].metric("Test Prep Students", total_tp)
        kpi_short = {
            "% to Delivery Target":        "Delivery",
            "% to Availability Target":    "Availability",
            "% Sessions on Time":          "On Time",
            "% Parents Updates Done on Time": "Parent Updates",
            "% of Active Students with Progress Updates Completed in last 2 months": "Progress Updates"
        }
        for i, m in enumerate(kpi_metrics):
            short = kpi_short.get(m, m)
            if m in kpi_avgs:
                d = kpi_avgs[m]
                emoji = "🟢" if d["curr"] >= 90 else ("🟡" if d["curr"] >= 75 else "🔴")
                delta_str = f"{d['delta']:+.1f} pp" if d["delta"] is not None else None
                snap_cols[i+2].metric(f"{emoji} {short}", f"{d['curr']:.1f}%",
                                      delta=delta_str, delta_color="normal")
            else:
                snap_cols[i+2].metric(short, "—")

        st.markdown("<div style='margin-top:8px;'></div>", unsafe_allow_html=True)

        # ── Row 2: Health metrics ─────────────────────
        st.caption("**🏥 Team Health**")

        _snap2_arch    = int(home_arch_df["should_archive"].sum()) if not home_arch_df.empty else 0
        _snap2_unsched = round(float(home_arch_df["unscheduled_hours"].sum()), 1) if not home_arch_df.empty else 0.0

        _snap2_ng = 0
        _snap2_sg = 0
        if not home_grades_df.empty:
            _snap2_ng = int(home_grades_df.groupby("student_id")["score"]
                            .apply(lambda s: s.isna().all()).sum())
            _hany2    = home_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            _graded2  = home_grades_df[home_grades_df["student_id"].isin(_hany2[_hany2].index)]
            if not _graded2.empty and "days_since_update" in _graded2.columns:
                _snap2_sg = int((_graded2.groupby("student_id")["days_since_update"].min() > 90).sum())

        _snap2_ne = 0
        _snap2_se = 0
        if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
            _now_s2 = pd.Timestamp.now(tz="UTC")
            for _sid, _sdf in home_exam_df.groupby("student_id"):
                if pd.notna(_sdf["test_prep_hours_delivered"].iloc[0]) and \
                        _sdf["test_prep_hours_delivered"].iloc[0] >= 6:
                    if _sdf[_sdf["exam_valid_composite"] == True].empty:
                        _snap2_ne += 1
                    else:
                        _latest_e = pd.to_datetime(
                            _sdf[_sdf["exam_valid_composite"] == True]["exam_date"], utc=True).max()
                        if pd.notna(_latest_e) and (_now_s2 - _latest_e).days > 90:
                            _snap2_se += 1

        health_cols = st.columns(6)
        health_cols[0].metric("📦 Archivable",   _snap2_arch)
        health_cols[1].metric("⏳ Unsched Hrs",   f"{_snap2_unsched:.0f}")
        health_cols[2].metric("📋 No Grades",     _snap2_ng)
        health_cols[3].metric("📚 Stale Grades",  _snap2_sg)
        health_cols[4].metric("📝 No Exam",       _snap2_ne)
        health_cols[5].metric("🕐 Stale Exams",   _snap2_se)

        st.divider()

        # ─────────────────────────────────────────────
        # MOST IMPROVED / MOST DECLINED (week over week)
        # ─────────────────────────────────────────────
        _mi_snap_arch   = load_snapshots()
        _mi_snap_grades = load_grades_snapshots()
        _mi_snap_exams  = load_exams_snapshots()

        # Build a per-tutor week-over-week delta table across all three snapshots
        # Metrics: archivable_students (lower=better), unscheduled_hours (lower=better),
        #          students_no_grades (lower=better), stale_grade_students (lower=better),
        #          students_no_exam (lower=better), students_stale_exam (lower=better)
        _mi_rows = {}
        improved_list = []
        declined_list = []

        def _wow_delta(snap_df, tutor_col, metric_col, tutor):
            """Return (prev_val, curr_val, delta) for a tutor's most recent two weeks."""
            if snap_df.empty or tutor not in snap_df[tutor_col].values:
                return None, None, None
            ts = snap_df[snap_df[tutor_col] == tutor].sort_values("week_date")
            if len(ts) < 2:
                return None, None, None
            prev = ts.iloc[-2][metric_col]
            curr = ts.iloc[-1][metric_col]
            if pd.isna(prev) or pd.isna(curr):
                return None, None, None
            return float(prev), float(curr), float(curr) - float(prev)

        all_tutors_mi = set()
        for _snap in [_mi_snap_arch, _mi_snap_grades, _mi_snap_exams]:
            if not _snap.empty and "tutor_name" in _snap.columns:
                all_tutors_mi.update(
                    _snap[_snap["tutor_name"].isin(
                        home_arch_df["tutor_name"].unique() if not home_arch_df.empty else []
                    )]["tutor_name"].tolist()
                )

        _metric_specs = [
            # (snap_df,           metric_col,              label,                   lower_is_better)
            (_mi_snap_arch,   "archivable_students",   "Archivable Students",   True),
            (_mi_snap_arch,   "unscheduled_hours",     "Unscheduled Hours",     True),
            (_mi_snap_grades, "students_no_grades",    "No Grades",             True),
            (_mi_snap_grades, "stale_grade_students",  "Stale Grades",          True),
            (_mi_snap_exams,  "students_no_exam",      "No Exam",               True),
            (_mi_snap_exams,  "students_stale_exam",   "Stale Exams",           True),
        ]

        for tutor in all_tutors_mi:
            tutor_deltas = []
            for snap_df, metric_col, label, lib in _metric_specs:
                prev, curr, delta = _wow_delta(snap_df, "tutor_name", metric_col, tutor)
                if delta is not None:
                    tutor_deltas.append((label, prev, curr, delta, lib))
            if tutor_deltas:
                # Score = sum of signed improvement (negative delta on lower-is-better = improvement)
                score = sum(-d for _, _, _, d, lib in tutor_deltas if lib)
                _mi_rows[tutor] = {"score": score, "deltas": tutor_deltas}

        if len(_mi_rows) >= 2:
            # For each tutor, separately tally improved vs declined metrics
            # A tutor is "most improved" only if their improvement score > 0
            # and they have more improving metrics than declining ones.
            # Cards show ONLY the relevant direction's metrics.
            improved_list = []
            declined_list = []

            for tutor, data in _mi_rows.items():
                good = [(lbl, prev, curr, delta, lib)
                        for lbl, prev, curr, delta, lib in data["deltas"]
                        if lib and delta < -0.5]   # lower-is-better and went down
                bad  = [(lbl, prev, curr, delta, lib)
                        for lbl, prev, curr, delta, lib in data["deltas"]
                        if lib and delta > 0.5]    # lower-is-better and went up

                improvement_score = sum(-d for _, _, _, d, _ in good)
                decline_score     = sum( d for _, _, _, d, _ in bad)

                # Only put in improved if net improvement AND more metrics improved
                if improvement_score > decline_score and len(good) >= len(bad):
                    improved_list.append((tutor, good, improvement_score))
                # Only put in declined if net decline AND more metrics declined
                elif decline_score > improvement_score and len(bad) >= len(good):
                    declined_list.append((tutor, bad, decline_score))

            improved_list.sort(key=lambda x: x[2], reverse=True)
            declined_list.sort(key=lambda x: x[2], reverse=True)

            st.markdown("### 📊 Week-over-Week Movement")
            mi_col, md_col = st.columns(2)

            with mi_col:
                st.markdown("#### 📈 Most Improved")
                if improved_list:
                    for tutor, good_metrics, _ in improved_list[:3]:
                        meaningful = sorted(good_metrics, key=lambda x: abs(x[3]), reverse=True)[:3]
                        parts = [f"{lbl}: {prev:.0f}→{curr:.0f} (↓{abs(delta):.0f})"
                                 for lbl, prev, curr, delta, _ in meaningful]
                        desc = " · ".join(parts)
                        st.markdown(f"""
                        <div style='background:#f0fff4; border:1.5px solid #b2f5c8;
                                    border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                            <div style='font-weight:700; color:#276749; font-size:1rem;'>
                                📈 {tutor}
                            </div>
                            <div style='font-size:0.83rem; color:#444; margin-top:4px;'>
                                {desc}
                            </div>
                        </div>""", unsafe_allow_html=True)
                else:
                    st.caption("No tutors with clear improvement this week.")

            with md_col:
                st.markdown("#### 📉 Most Declined")
                if declined_list:
                    for tutor, bad_metrics, _ in declined_list[:3]:
                        meaningful = sorted(bad_metrics, key=lambda x: abs(x[3]), reverse=True)[:3]
                        parts = [f"{lbl}: {prev:.0f}→{curr:.0f} (↑{abs(delta):.0f})"
                                 for lbl, prev, curr, delta, _ in meaningful]
                        desc = " · ".join(parts)
                        st.markdown(f"""
                        <div style='background:#fff0f0; border:1.5px solid #ffcccc;
                                    border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                            <div style='font-weight:700; color:#9b1c1c; font-size:1rem;'>
                                📉 {tutor}
                            </div>
                            <div style='font-size:0.83rem; color:#444; margin-top:4px;'>
                                {desc}
                            </div>
                        </div>""", unsafe_allow_html=True)
                else:
                    st.caption("No tutors with clear decline this week.")

            st.divider()

        # ─────────────────────────────────────────────
        # THREE COLUMNS: NEEDS ATTENTION / WINS / WATCH
        # ─────────────────────────────────────────────
        col_red, col_green, col_yellow = st.columns(3)

        # ── 🚨 NEEDS ATTENTION ───────────────────────
        with col_red:
            st.markdown("### 🚨 Needs Attention")

            # KPI drops
            if kpi_avgs:
                big_drops = [(m, d) for m, d in kpi_avgs.items()
                             if d["delta"] is not None and d["delta"] <= -5]
                big_drops.sort(key=lambda x: x[1]["delta"])
                for m, d in big_drops[:3]:
                    short = m.replace("% of Active Students with Progress Updates Completed in last 2 months",
                                      "Progress Updates").replace("% to ","").replace("% ","")
                    card("📉", f"KPI Drop: {short}",
                         f"Down <b>{d['delta']:+.1f} pp</b> this period ({d['curr']:.1f}% now).",
                         color="red")

            # Archivable students
            if not home_arch_df.empty:
                arch_by_tutor = (home_arch_df[home_arch_df["should_archive"] == True]
                                 .groupby("tutor_name")["student_name"].nunique()
                                 .sort_values(ascending=False))
                for tutor, count in arch_by_tutor.head(3).items():
                    card("📦", f"Archivable Students",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"that should be archived.",
                         color="red")

            # No completed exam
            if not home_exam_df.empty:
                SAT_TYPES = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT",
                             "Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                ACT_TYPES = {"ACT","Digital ACT"}
                home_exam_df["exam_family"] = home_exam_df["subject"].apply(
                    lambda x: "SAT/PSAT" if x in SAT_TYPES else ("ACT" if x in ACT_TYPES else "Other"))

                def sat_composite_ok(r):
                    return (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                            pd.notna(r["sat_rw"])   and r["sat_rw"]   >= 300)
                def act_composite_ok(r):
                    return ((pd.isna(r["act_english"]) or r["act_english"] >= 10) and
                            (pd.isna(r["act_math"])    or r["act_math"]    >= 10) and
                            (pd.isna(r["act_reading"]) or r["act_reading"] >= 10))
                home_exam_df["exam_valid_composite"] = home_exam_df.apply(
                    lambda r: sat_composite_ok(r) if r["exam_family"] == "SAT/PSAT"
                              else (act_composite_ok(r) if r["exam_family"] == "ACT" else None), axis=1)

                no_exam_by_tutor = {}
                for tutor, tdf in home_exam_df.groupby("tutor_name"):
                    count = 0
                    for sid, sdf in tdf.groupby("student_id"):
                        if sdf["test_prep_hours_delivered"].iloc[0] >= 6:
                            if sdf[sdf["exam_valid_composite"] == True].empty:
                                count += 1
                    if count > 0:
                        no_exam_by_tutor[tutor] = count
                for tutor, count in sorted(no_exam_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:3]:
                    card("📝", "No Completed Exam",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with 6+ hrs but no completed exam.",
                         color="red")

            # No grades entered at all
            if not home_grades_df.empty:
                no_grades_by_tutor = {}
                for tutor, tdf in home_grades_df.groupby("tutor_name"):
                    no_g = tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum()
                    if no_g > 0:
                        no_grades_by_tutor[tutor] = int(no_g)
                for tutor, count in sorted(no_grades_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:2]:
                    card("📋", "No Grades Entered",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no grades entered at all.",
                         color="red")

            # KPI below threshold
            thresholds = {
                "% to Delivery Target": 70,
                "% Sessions on Time":   80,
            }
            for m, thresh in thresholds.items():
                if m in kpi_avgs and kpi_avgs[m]["curr"] < thresh:
                    card("⚠️", f"KPI Below Threshold",
                         f"<b>{m.replace('% to ','').replace('% ','')}</b> is at "
                         f"<b>{kpi_avgs[m]['curr']:.1f}%</b> — below the {thresh}% threshold.",
                         color="red")

        # ── ✅ WINS ───────────────────────────────────
        with col_green:
            st.markdown("### ✅ Wins")

            # KPI improvements
            if kpi_avgs:
                big_gains = [(m, d) for m, d in kpi_avgs.items()
                             if d["delta"] is not None and d["delta"] >= 3]
                big_gains.sort(key=lambda x: x[1]["delta"], reverse=True)
                for m, d in big_gains[:3]:
                    short = m.replace("% of Active Students with Progress Updates Completed in last 2 months",
                                      "Progress Updates").replace("% to ","").replace("% ","")
                    card("📈", f"KPI Gain: {short}",
                         f"Up <b>{d['delta']:+.1f} pp</b> this period ({d['curr']:.1f}% now).",
                         color="green")

            # KPIs above 90%
            strong_kpis = [(m, d) for m, d in kpi_avgs.items() if d["curr"] >= 90]
            for m, d in strong_kpis[:2]:
                short = m.replace("% of Active Students with Progress Updates Completed in last 2 months",
                                  "Progress Updates").replace("% to ","").replace("% ","")
                card("🌟", f"Strong KPI: {short}",
                     f"Team is at <b>{d['curr']:.1f}%</b> — great work!",
                     color="green")

            # Score improvers
            if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                try:
                    def _compute_imp_home(student_df, exam_family):
                        fsd = student_df["first_session_day"].iloc[0]
                        fam = student_df[student_df["exam_family"] == exam_family]
                        fam = fam[fam["exam_valid_composite"] == True].dropna(subset=["score"])
                        if fam.empty: return None
                        before = fam[fam["exam_date"] <= fsd]
                        after  = fam[fam["exam_date"] >  fsd]
                        if before.empty or after.empty: return None
                        b = before.sort_values("exam_date").iloc[-1]["score"]
                        e = after.sort_values("exam_date").iloc[-1]["score"]
                        return (e - b) if pd.notna(b) and pd.notna(e) else None

                    imp_rows = []
                    for (tid, tname, sid, sname), sdf in home_exam_df.groupby(
                            ["tutor_id","tutor_name","student_id","student_name"]):
                        for fam in ["SAT/PSAT","ACT"]:
                            imp = _compute_imp_home(sdf, fam)
                            if imp is not None and imp > 0:
                                imp_rows.append({"tutor_name": tname, "student_name": sname,
                                                 "exam_family": fam, "improvement": imp})

                    if imp_rows:
                        imp_home_df = pd.DataFrame(imp_rows).sort_values("improvement", ascending=False)
                        for _, row in imp_home_df.head(3).iterrows():
                            card("🎉", f"Score Improvement ({row['exam_family']})",
                                 f"<b>{row['student_name']}</b> ({row['tutor_name']}) improved by "
                                 f"<b>+{row['improvement']:.0f} pts</b>.",
                                 color="green")
                except Exception:
                    pass

            # No archivable students tutors
            if not home_arch_df.empty:
                all_tutors = set(home_arch_df["tutor_name"].unique())
                arch_tutors = set(home_arch_df[home_arch_df["should_archive"]==True]["tutor_name"].unique())
                clean_tutors = all_tutors - arch_tutors
                if clean_tutors:
                    card("✨", "Clean Dashboards",
                         f"<b>{len(clean_tutors)} tutor{'s' if len(clean_tutors)>1 else ''}</b> "
                         f"have zero archivable students. 👏",
                         color="green")

        # ── ⏳ WATCH LIST ─────────────────────────────
        with col_yellow:
            st.markdown("### ⏳ Watch List")

            # KPI hovering near threshold
            if kpi_avgs:
                near_threshold = [
                    (m, d) for m, d in kpi_avgs.items()
                    if d["curr"] is not None and 70 <= d["curr"] < 80
                ]
                for m, d in near_threshold[:2]:
                    short = m.replace("% of Active Students with Progress Updates Completed in last 2 months",
                                      "Progress Updates").replace("% to ","").replace("% ","")
                    card("⚡", f"KPI Near Threshold: {short}",
                         f"Currently at <b>{d['curr']:.1f}%</b> — close to the warning zone.",
                         color="yellow")

            # ── 🔔 Anomalies (vs tutor's own history) ──
            _snap_arch_h   = load_snapshots()
            _snap_grades_h = load_grades_snapshots()
            _snap_exams_h  = load_exams_snapshots()

            if not (_snap_arch_h.empty and _snap_grades_h.empty and _snap_exams_h.empty):
                anomaly_tutors = []
                for _t, _tdf in home_arch_df.groupby("tutor_name"):
                    _cur = {
                        "arch_count":  int(_tdf["should_archive"].sum()),
                        "unsched_hrs": float(_tdf["unscheduled_hours"].sum()),
                        "no_grades":   0, "stale_grades": 0,
                        "no_exam":     0, "stale_exams":  0,
                    }
                    if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                        _gdf = home_grades_df[home_grades_df["tutor_name"] == _t]
                        _cur["no_grades"]    = int(_gdf.groupby("student_id")["score"]
                                                   .apply(lambda s: s.isna().all()).sum())
                        _hany = _gdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                        _graded = _gdf[_gdf["student_id"].isin(_hany[_hany].index)]
                        if not _graded.empty and "days_since_update" in _graded.columns:
                            _cur["stale_grades"] = int(
                                (_graded.groupby("student_id")["days_since_update"].min() > 90).sum())
                    if not home_exam_df.empty and _t in home_exam_df["tutor_name"].values and \
                            "exam_valid_composite" in home_exam_df.columns:
                        _edf = home_exam_df[home_exam_df["tutor_name"] == _t]
                        _now_a = pd.Timestamp.now(tz="UTC")
                        _cur["no_exam"] = sum(
                            1 for sid, sdf in _edf.groupby("student_id")
                            if sdf["test_prep_hours_delivered"].iloc[0] >= 6
                            and sdf[sdf["exam_valid_composite"] == True].empty)
                        for sid, sdf in _edf.groupby("student_id"):
                            _comp = sdf[sdf["exam_valid_composite"] == True]
                            if not _comp.empty:
                                _latest = pd.to_datetime(_comp["exam_date"], utc=True).max()
                                if pd.notna(_latest) and (_now_a - _latest).days > 90:
                                    _cur["stale_exams"] += 1
                    _flags = compute_anomalies(_t, _snap_arch_h, _snap_grades_h,
                                               _snap_exams_h, _cur)
                    _spiked = [k for k, v in _flags.items() if v]
                    if _spiked:
                        _label_map = {
                            "arch_count":   "archivable students",
                            "unsched_hrs":  "unscheduled hours",
                            "no_grades":    "students with no grades",
                            "stale_grades": "stale grade entries",
                            "no_exam":      "students missing exams",
                            "stale_exams":  "stale exam scores",
                        }
                        _desc = ", ".join(_label_map.get(k, k) for k in _spiked)
                        anomaly_tutors.append((_t, _desc))

                for _t, _desc in anomaly_tutors[:4]:
                    card("🔔", f"Unusual Spike — {_t}",
                         f"<b>{_t}</b> is significantly higher than their own historical average on: "
                         f"<b>{_desc}</b>.",
                         color="yellow")

            # Unscheduled hours
            if not home_arch_df.empty:
                unsched = (home_arch_df[home_arch_df["unscheduled_hours"] > 0]
                           .groupby("tutor_name")["unscheduled_hours"].sum()
                           .sort_values(ascending=False))
                total_unsched = home_arch_df["unscheduled_hours"].sum()
                if total_unsched > 0:
                    card("⏳", "Unscheduled Hours",
                         f"Team has <b>{total_unsched:,.1f} total unscheduled hours</b>. "
                         f"Top: <b>{unsched.index[0]}</b> ({unsched.iloc[0]:.1f} hrs).",
                         color="yellow")

            # Stale exam scores
            if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                now_utc2 = pd.Timestamp.now(tz="UTC")
                stale_exam_by_tutor = {}
                for tutor, tdf in home_exam_df.groupby("tutor_name"):
                    count = 0
                    for sid, sdf in tdf.groupby("student_id"):
                        completed = sdf[sdf["exam_valid_composite"] == True]
                        if not completed.empty:
                            latest = pd.to_datetime(completed["exam_date"], utc=True).max()
                            if pd.notna(latest) and (now_utc2 - latest).days > 90:
                                count += 1
                    if count > 0:
                        stale_exam_by_tutor[tutor] = count
                for tutor, count in sorted(stale_exam_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:3]:
                    card("🕐", "Stale Practice Exam",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no completed exam in 90+ days.",
                         color="yellow")

            # Stale grades
            if not home_grades_df.empty:
                stale_by_tutor = {}
                for tutor, tdf in home_grades_df.groupby("tutor_name"):
                    has_any    = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                    graded_ids = has_any[has_any].index
                    graded     = tdf[tdf["student_id"].isin(graded_ids)]
                    if not graded.empty:
                        latest = graded.groupby("student_id")["days_since_update"].min()
                        stale  = int((latest > 90).sum())
                        if stale > 0:
                            stale_by_tutor[tutor] = stale
                for tutor, count in sorted(stale_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:2]:
                    card("📚", "Stale Grades",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no grade update in 90+ days.",
                         color="yellow")

            # Students not yet met with (exam students with no sessions yet)
            if not home_exam_df.empty:
                no_session_yet = (home_exam_df[home_exam_df["test_prep_hours_delivered"] == 0]
                                  ["student_id"].nunique())
                if no_session_yet > 0:
                    card("🆕", "New Students — No Sessions Yet",
                         f"<b>{no_session_yet} test prep student{'s' if no_session_yet>1 else ''}</b> "
                         f"enrolled but no sessions delivered yet.",
                         color="yellow")

        st.divider()

        # ── Watch List strip on Home page ─────────────
        watched = load_watchlist()
        if watched:
            st.markdown("### 👀 Watched Tutors")
            st.caption("Quick status for tutors on your watch list. Go to the Watch List page for full details.")

            wl_cols = st.columns(min(len(watched), 4))
            for i, tutor in enumerate(watched):
                issues = 0
                lines  = []

                # Archivable
                if not home_arch_df.empty:
                    arch_count = home_arch_df[
                        (home_arch_df["tutor_name"] == tutor) &
                        (home_arch_df["should_archive"] == True)
                    ]["student_name"].nunique()
                    if arch_count > 0:
                        issues += 1
                        lines.append(f"📦 {arch_count} archivable")

                # Unscheduled hrs
                if not home_arch_df.empty:
                    unsched_hrs = home_arch_df[home_arch_df["tutor_name"] == tutor]["unscheduled_hours"].sum()
                    if unsched_hrs > 0:
                        lines.append(f"⏳ {unsched_hrs:.0f} unsched hrs")

                # No grades
                if not home_grades_df.empty and tutor in home_grades_df["tutor_name"].values:
                    tgdf = home_grades_df[home_grades_df["tutor_name"] == tutor]
                    no_g = tgdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum()
                    if no_g > 0:
                        issues += 1
                        lines.append(f"📋 {int(no_g)} no grades")

                # Stale grades
                if not home_grades_df.empty and tutor in home_grades_df["tutor_name"].values:
                    tgdf   = home_grades_df[home_grades_df["tutor_name"] == tutor]
                    has_g  = tgdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                    gids   = has_g[has_g].index
                    graded = tgdf[tgdf["student_id"].isin(gids)]
                    if not graded.empty:
                        latest_g = graded.groupby("student_id")["days_since_update"].min()
                        stale_g  = int((latest_g > 90).sum())
                        if stale_g > 0:
                            lines.append(f"📚 {stale_g} stale grades")

                # No exam
                if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                    tedf = home_exam_df[home_exam_df["tutor_name"] == tutor]
                    no_ex = sum(
                        1 for sid, sdf in tedf.groupby("student_id")
                        if sdf["test_prep_hours_delivered"].iloc[0] >= 6
                        and sdf[sdf["exam_valid_composite"] == True].empty
                    )
                    if no_ex > 0:
                        issues += 1
                        lines.append(f"📝 {no_ex} no exam")

                dot   = "🔴" if issues >= 2 else ("🟡" if issues == 1 else "🟢")
                body  = "<br>".join(lines) if lines else "No issues detected"
                color = "red" if issues >= 2 else ("yellow" if issues == 1 else "green")
                with wl_cols[i % 4]:
                    st.markdown(f"""
                    <div style='background:{"#fff0f0" if color=="red" else "#fffbea" if color=="yellow" else "#f0fff4"};
                                border:1.5px solid {"#ffcccc" if color=="red" else "#ffe58f" if color=="yellow" else "#b2f5c8"};
                                border-radius:10px; padding:12px 14px; margin-bottom:8px;'>
                        <div style='font-weight:700; font-size:1rem;'>{dot} {tutor}</div>
                        <div style='font-size:0.85rem; color:#444; margin-top:4px;'>{body}</div>
                    </div>""", unsafe_allow_html=True)
        else:
            st.markdown("### 👀 Watch List")
            st.caption("No tutors on your watch list yet. Go to the **Watch List** page to add some.")

        st.divider()

        # ─────────────────────────────────────────────
        # DAILY DIGEST — fills the placeholder at the top
        # ─────────────────────────────────────────────
        _today_str = pd.Timestamp.now().strftime("%A, %B %-d")

        # ── Prose intro ───────────────────────────────
        _total_active  = home_arch_df["student_name"].nunique() if not home_arch_df.empty else 0
        _total_arch    = int(home_arch_df["should_archive"].sum()) if not home_arch_df.empty else 0
        _total_unsched = round(float(home_arch_df["unscheduled_hours"].sum()), 1) if not home_arch_df.empty else 0.0

        _kpi_sentence = ""
        if kpi_avgs:
            _worst = min(kpi_avgs.items(), key=lambda x: x[1]["curr"])
            _best  = max(kpi_avgs.items(), key=lambda x: x[1]["curr"])
            _ws    = _worst[0].replace("% of Active Students with Progress Updates Completed in last 2 months",
                                       "Progress Updates").replace("% to ","").replace("% ","")
            _bs    = _best[0].replace("% of Active Students with Progress Updates Completed in last 2 months",
                                       "Progress Updates").replace("% to ","").replace("% ","")
            _kpi_sentence = (f"Your strongest KPI is **{_bs}** at "
                             f"{kpi_avgs[_best[0]]['curr']:.0f}%; "
                             f"**{_ws}** needs the most attention at "
                             f"{kpi_avgs[_worst[0]]['curr']:.0f}%.")

        _intro = (
            f"Here's your briefing for **{_today_str}**. "
            f"Team De Groot has **{_total_active} active students**, "
            f"**{_total_arch} flagged for archiving**, "
            f"and **{_total_unsched:.0f} unscheduled hours** across the team. "
            f"{_kpi_sentence}"
        )

        # ── Since last login ──────────────────────────
        _login_lines = []
        if not prev_snap.empty:
            _ps = prev_snap.iloc[0]
            _pl = str(_ps.get("login_ts", "your last visit"))
            for _label, _cur_val, _prev_key, _lib in [
                ("archivable students",  cur_arch,    "team_archivable",     True),
                ("unscheduled hours",    cur_unsched, "team_unscheduled_hrs",True),
                ("students with no grades", cur_ng,  "team_no_grades",      True),
                ("stale grade entries",  cur_sg,      "team_stale_grades",   True),
            ]:
                try:
                    _diff = int(_cur_val) - int(_ps.get(_prev_key, _cur_val))
                except:
                    continue
                if _diff == 0:
                    continue
                _dir  = "up" if _diff > 0 else "down"
                _good = (_diff < 0) if _lib else (_diff > 0)
                _icon = "✅" if _good else "⚠️"
                _login_lines.append(f"{_icon} **{_label}** went {_dir} by {abs(_diff)} since {_pl}")

        # ── Needs attention ───────────────────────────
        _attn_lines = []
        if not home_arch_df.empty:
            _arch_top = (home_arch_df[home_arch_df["should_archive"] == True]
                         .groupby("tutor_name")["student_name"].nunique()
                         .sort_values(ascending=False))
            for _t, _c in _arch_top.head(2).items():
                _attn_lines.append(f"📦 **{_t}** has **{_c} student{'s' if _c>1 else ''}** flagged for archiving")

        if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
            _no_ex_top = {}
            for _t, _tdf in home_exam_df.groupby("tutor_name"):
                _c = sum(1 for sid, sdf in _tdf.groupby("student_id")
                         if sdf["test_prep_hours_delivered"].iloc[0] >= 6
                         and sdf[sdf["exam_valid_composite"] == True].empty)
                if _c > 0:
                    _no_ex_top[_t] = _c
            for _t, _c in sorted(_no_ex_top.items(), key=lambda x: x[1], reverse=True)[:2]:
                _attn_lines.append(f"📝 **{_t}** has **{_c} student{'s' if _c>1 else ''}** with 6+ hrs but no completed exam")

        if not home_grades_df.empty:
            for _t, _tdf in home_grades_df.groupby("tutor_name"):
                _ng = int(_tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
                if _ng > 0:
                    _attn_lines.append(f"📋 **{_t}** has **{_ng} student{'s' if _ng>1 else ''}** with no grades entered")
            _attn_lines = _attn_lines  # cap handled by [:5] below

        # ── Week-over-week ────────────────────────────
        _wow_lines = []
        if improved_list:
            for _t, _good, _ in improved_list[:2]:
                _top = sorted(_good, key=lambda x: abs(x[3]), reverse=True)[0]
                _wow_lines.append(f"📈 **{_t}** improved — {_top[0]} dropped from {_top[1]:.0f} to {_top[2]:.0f}")
        if declined_list:
            for _t, _bad, _ in declined_list[:2]:
                _top = sorted(_bad, key=lambda x: abs(x[3]), reverse=True)[0]
                _wow_lines.append(f"📉 **{_t}** declined — {_top[0]} rose from {_top[1]:.0f} to {_top[2]:.0f}")

        # ── Watched tutor status ──────────────────────
        _watched_lines = []
        _dg_watched = load_watchlist()
        for _t in _dg_watched:
            _wl_issues = []
            if not home_arch_df.empty:
                _wa = home_arch_df[
                    (home_arch_df["tutor_name"] == _t) &
                    (home_arch_df["should_archive"] == True)]["student_name"].nunique()
                if _wa > 0:
                    _wl_issues.append(f"{_wa} archivable")
            if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                _wng = int(home_grades_df[home_grades_df["tutor_name"] == _t]
                           .groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
                if _wng > 0:
                    _wl_issues.append(f"{_wng} no grades")
            if _wl_issues:
                _watched_lines.append(f"👁 **{_t}**: {', '.join(_wl_issues)}")
            else:
                _watched_lines.append(f"✅ **{_t}**: no issues detected")

        # ── Anomaly flags ─────────────────────────────
        _anomaly_lines = []
        if not (_snap_arch_h.empty and _snap_grades_h.empty and _snap_exams_h.empty):
            for _t, _tdf in home_arch_df.groupby("tutor_name"):
                _ac = {
                    "arch_count":  int(_tdf["should_archive"].sum()),
                    "unsched_hrs": float(_tdf["unscheduled_hours"].sum()),
                    "no_grades": 0, "stale_grades": 0, "no_exam": 0, "stale_exams": 0,
                }
                if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                    _gd = home_grades_df[home_grades_df["tutor_name"] == _t]
                    _ac["no_grades"] = int(_gd.groupby("student_id")["score"]
                                          .apply(lambda s: s.isna().all()).sum())
                _af = compute_anomalies(_t, _snap_arch_h, _snap_grades_h, _snap_exams_h, _ac)
                _spiked = [k.replace("_"," ") for k, v in _af.items() if v]
                if _spiked:
                    _anomaly_lines.append(f"🔔 **{_t}** is unusually high on: {', '.join(_spiked)}")

        # ── Assemble into placeholder ─────────────────
        with _digest_placeholder.expander("📋 Daily Digest", expanded=False):
            st.markdown(_intro)
            st.markdown("")

            if _login_lines:
                st.markdown("**Since your last login:**")
                for _l in _login_lines:
                    st.markdown(f"- {_l}")
                st.markdown("")

            if _attn_lines:
                st.markdown("**Needs attention:**")
                for _l in _attn_lines[:5]:
                    st.markdown(f"- {_l}")
                st.markdown("")

            if _wow_lines:
                st.markdown("**Week-over-week movement:**")
                for _l in _wow_lines:
                    st.markdown(f"- {_l}")
                st.markdown("")

            if _watched_lines:
                st.markdown("**Watched tutors:**")
                for _l in _watched_lines:
                    st.markdown(f"- {_l}")
                st.markdown("")

            if _anomaly_lines:
                st.markdown("**Anomaly flags:**")
                for _l in _anomaly_lines:
                    st.markdown(f"- {_l}")
                st.markdown("")

            if not any([_login_lines, _attn_lines, _wow_lines, _watched_lines, _anomaly_lines]):
                st.markdown("✅ Nothing unusual to report today.")

        st.caption("💡 Navigate to any page in the sidebar for full details. Data refreshes hourly.")

        if st.sidebar.button("🔄 Refresh All Data", key="refresh_home"):
            st.cache_data.clear()
            st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: WATCH LIST
    # ─────────────────────────────────────────────

    if page == "👀 Watched Tutors":
        st.markdown('<div class="main-title">👀 Watched Tutors</div>', unsafe_allow_html=True)
        st.caption("Tutors you're keeping a close eye on. Added tutors stay here until you remove them.")

        # ── Manage watch list ─────────────────────────
        all_tutors_wl = sorted(
            master_tutor_df[master_tutor_df["Faculty Leader"] == "Annelies de Groot"]["Full Name"]
            .dropna().unique().tolist()
        )
        current_watched = load_watchlist()

        st.markdown("### ➕ Manage Watch List")
        new_watched = st.multiselect(
            "Select tutors to watch:",
            options=all_tutors_wl,
            default=current_watched,
            key="watchlist_select"
        )

        col_save, col_clear = st.columns([1, 5])
        with col_save:
            if st.button("💾 Save Watch List", key="save_watchlist"):
                for removed in set(current_watched) - set(new_watched):
                    remove_watchlist_baseline(removed)
                    delete_watchlist_note(removed)
                    delete_watchlist_thresholds(removed)
                save_watchlist(new_watched)
                st.success(f"Watch list saved — {len(new_watched)} tutor(s) being watched.")
                st.rerun()
        with col_clear:
            if st.button("🗑️ Clear All", key="clear_watchlist"):
                for t in current_watched:
                    remove_watchlist_baseline(t)
                    delete_watchlist_note(t)
                    delete_watchlist_thresholds(t)
                save_watchlist([])
                st.success("Watch list cleared.")
                st.rerun()

        watched = load_watchlist()

        if not watched:
            st.info("Your watch list is empty. Select tutors above and click Save.")
            st.stop()

        st.divider()

        # ── Load data ─────────────────────────────────
        load_errors_wl = []
        with st.spinner("Loading watch list data..."):
            try:
                raw_wl_arch, _ = load_archivable_unscheduled()
                raw_wl_arch["should_archive"] = raw_wl_arch["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                wl_arch_df = raw_wl_arch[raw_wl_arch["team_name"] == "Team De Groot"].copy()
            except Exception as e:
                wl_arch_df = pd.DataFrame()
                load_errors_wl.append(f"Archivable: {e}")

            try:
                raw_wl_grades, _ = load_grades_data()
                wl_grades_df = raw_wl_grades[raw_wl_grades["team_name"] == "Team De Groot"].copy()
                now_wl = pd.Timestamp.now(tz="UTC")
                wl_grades_df["updated_at"] = pd.to_datetime(wl_grades_df["updated_at"], errors="coerce", utc=True)
                wl_grades_df["days_since_update"] = (now_wl - wl_grades_df["updated_at"]).dt.days
            except Exception as e:
                wl_grades_df = pd.DataFrame()
                load_errors_wl.append(f"Grades: {e}")

            try:
                raw_wl_exam, _ = load_exam_data()
                wl_exam_df = raw_wl_exam[raw_wl_exam["team_name"] == "Team De Groot"].copy()
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    wl_exam_df[dc] = pd.to_datetime(wl_exam_df[dc], errors="coerce", utc=True)
                for nc in ["score","act_english","act_math","act_reading","act_science",
                           "sat_math","sat_rw","test_prep_hours_delivered"]:
                    wl_exam_df[nc] = pd.to_numeric(wl_exam_df[nc], errors="coerce")

                SAT_TYPES_WL = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT",
                                "Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                ACT_TYPES_WL = {"ACT","Digital ACT"}
                wl_exam_df["exam_family"] = wl_exam_df["subject"].apply(
                    lambda x: "SAT/PSAT" if x in SAT_TYPES_WL
                              else ("ACT" if x in ACT_TYPES_WL else "Other"))

                def _sat_ok(r):
                    return (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                            pd.notna(r["sat_rw"])   and r["sat_rw"]   >= 300)
                def _act_ok(r):
                    return ((pd.isna(r["act_english"]) or r["act_english"] >= 10) and
                            (pd.isna(r["act_math"])    or r["act_math"]    >= 10) and
                            (pd.isna(r["act_reading"]) or r["act_reading"] >= 10))
                wl_exam_df["exam_valid_composite"] = wl_exam_df.apply(
                    lambda r: _sat_ok(r) if r["exam_family"] == "SAT/PSAT"
                              else (_act_ok(r) if r["exam_family"] == "ACT" else None), axis=1)
            except Exception as e:
                wl_exam_df = pd.DataFrame()
                load_errors_wl.append(f"Exams: {e}")

            try:
                wl_kpi_df = load_kpi_data()
            except Exception as e:
                wl_kpi_df = pd.DataFrame()
                load_errors_wl.append(f"KPI: {e}")

        if load_errors_wl:
            with st.expander("⚠️ Some data failed to load"):
                for err in load_errors_wl:
                    st.warning(err)

        # ── Render one card per watched tutor ─────────
        st.markdown(f"### 👀 Watching {len(watched)} tutor{'s' if len(watched)>1 else ''}")
        baselines       = load_watchlist_baselines()
        wl_snap_arch    = load_snapshots()
        wl_snap_grades  = load_grades_snapshots()
        wl_snap_exams   = load_exams_snapshots()

        import re as _re
        def _parse_end(s):
            if pd.isna(s): return pd.NaT
            s2 = s.replace("-","to").replace("–","to").replace("—","to")
            parts = s2.split("to")
            if len(parts) < 2: return pd.NaT
            end = _re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", parts[-1].strip())
            return pd.to_datetime(end, errors="coerce", dayfirst=False)

        def _metric_html(label, value, delta_text, is_bad=False, is_anomaly=False):
            border_color = "#cc0000" if is_bad else "#1a6e36"
            delta_color  = (
                "#1a6e36" if delta_text and "↓" in delta_text and is_bad else
                "#cc0000" if delta_text and "↑" in delta_text and is_bad else
                "#888"
            )
            delta_html = (
                f"<div style='font-size:0.75rem; color:{delta_color}; margin-top:4px;'>"
                f"{delta_text}</div>"
            ) if delta_text else ""
            anomaly_html = (
                "<div style='font-size:0.7rem; color:#b35c00; margin-top:3px;'>"
                "🔔 unusual vs history</div>"
            ) if is_anomaly else ""
            return f"""
            <div style='background:#f8f9fa; border-radius:8px; padding:14px 16px;
                        border-left:4px solid {border_color}; height:100%;'>
                <div style='font-size:0.78rem; color:#666; margin-bottom:6px;'>{label}</div>
                <div style='font-size:1.8rem; font-weight:700; color:{border_color};'>{value}</div>
                {delta_html}{anomaly_html}
            </div>"""

        for tutor in watched:

            # ── Compute current metrics ───────────────
            arch_count  = 0
            unsched_hrs = 0.0
            if not wl_arch_df.empty:
                tarch = wl_arch_df[wl_arch_df["tutor_name"] == tutor]
                arch_count  = tarch[tarch["should_archive"] == True]["student_name"].nunique()
                unsched_hrs = tarch["unscheduled_hours"].sum()

            no_grades_count = 0
            stale_count_wl  = 0
            if not wl_grades_df.empty and tutor in wl_grades_df["tutor_name"].values:
                tgdf = wl_grades_df[wl_grades_df["tutor_name"] == tutor]
                no_grades_count = int(tgdf.groupby("student_id")["score"]
                                       .apply(lambda s: s.isna().all()).sum())
                has_g  = tgdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                gids   = has_g[has_g].index
                graded = tgdf[tgdf["student_id"].isin(gids)]
                if not graded.empty:
                    latest_g = graded.groupby("student_id")["days_since_update"].min()
                    stale_count_wl = int((latest_g > 90).sum())

            no_exam_count = 0
            stale_exam_count_wl = 0
            hours_per_exam_wl   = None
            if not wl_exam_df.empty and tutor in wl_exam_df["tutor_name"].values:
                tedf = wl_exam_df[wl_exam_df["tutor_name"] == tutor]
                now_wl2 = pd.Timestamp.now(tz="UTC")
                no_exam_count = sum(
                    1 for sid, sdf in tedf.groupby("student_id")
                    if sdf["test_prep_hours_delivered"].iloc[0] >= 6
                    and sdf[sdf["exam_valid_composite"] == True].empty
                )
                # Stale exams: completed exam exists but >90 days ago
                for sid, sdf in tedf.groupby("student_id"):
                    completed = sdf[sdf["exam_valid_composite"] == True]
                    if not completed.empty:
                        latest_ex = pd.to_datetime(completed["exam_date"], utc=True).max()
                        if pd.notna(latest_ex) and (now_wl2 - latest_ex).days > 90:
                            stale_exam_count_wl += 1
                # Hours per exam
                total_hrs_wl = tedf["test_prep_hours_delivered"].iloc[0] if not tedf.empty else 0
                completed_ex = tedf[tedf["exam_valid_composite"] == True]["exam_id"].nunique()
                if completed_ex > 0 and pd.notna(total_hrs_wl):
                    hours_per_exam_wl = round(total_hrs_wl / completed_ex, 1)

            # Pct unscheduled
            pct_unscheduled_wl = 0.0
            if not wl_arch_df.empty:
                tarch_all = wl_arch_df[wl_arch_df["tutor_name"] == tutor]
                total_prov = tarch_all["hours_remaining"].sum() + tarch_all["unscheduled_hours"].sum()
                pct_unscheduled_wl = round(
                    tarch_all["unscheduled_hours"].sum() / total_prov * 100, 1
                ) if total_prov > 0 else 0.0

            # ── Save baseline if this tutor is new ────
            save_watchlist_baseline(tutor, arch_count, unsched_hrs,
                                    no_grades_count, stale_count_wl, no_exam_count,
                                    stale_exam_count_wl, hours_per_exam_wl, pct_unscheduled_wl)

            # ── Anomaly flags for this tutor ──────────
            anomaly_flags = compute_anomalies(tutor, wl_snap_arch, wl_snap_grades,
                                              wl_snap_exams, {
                "arch_count":   arch_count,
                "unsched_hrs":  unsched_hrs,
                "no_grades":    no_grades_count,
                "stale_grades": stale_count_wl,
                "no_exam":      no_exam_count,
                "stale_exams":  stale_exam_count_wl,
            })

            # ── Load baseline for deltas ──────────────
            bl = None
            added_date = None
            if not baselines.empty and tutor in baselines["tutor_name"].values:
                bl_row     = baselines[baselines["tutor_name"] == tutor].iloc[0]
                bl         = bl_row.to_dict()
                added_date = bl_row.get("added_date", None)
            # Reload baselines in case we just saved a new one
            baselines = load_watchlist_baselines()
            if not baselines.empty and tutor in baselines["tutor_name"].values:
                bl_row     = baselines[baselines["tutor_name"] == tutor].iloc[0]
                bl         = bl_row.to_dict()
                added_date = bl_row.get("added_date", None)

            def delta_str(current, baseline_key):
                if bl is None or baseline_key not in bl:
                    return None
                try:
                    baseline_val = bl[baseline_key]
                    # Treat None, NaN, empty string as missing — show no delta
                    if baseline_val is None or baseline_val == "" or (isinstance(baseline_val, float) and pd.isna(baseline_val)):
                        return None
                    # current may also be None (e.g. hours_per_exam when no exams)
                    if current is None:
                        return None
                    diff = float(current) - float(baseline_val)
                    if diff == 0: return "no change since added"
                    arrow = "↓" if diff < 0 else "↑"
                    sign  = "+" if diff > 0 else ""
                    return f"{arrow} {sign}{diff:g} since added ({added_date})"
                except Exception:
                    return None

            # ── Load custom thresholds for this tutor ─
            t = get_tutor_thresholds(tutor)

            issues = sum([
                arch_count       >= t["arch_count"],
                no_grades_count  >= t["no_grades"],
                stale_count_wl   >= t["stale_grades"],
                no_exam_count    >= t["no_exam"],
                stale_exam_count_wl >= t["stale_exams"],
                unsched_hrs      >= t["unsched_hrs"] if t["unsched_hrs"] > 0 else False,
                pct_unscheduled_wl >= t["pct_unscheduled"],
            ])
            header_color = "#cc0000" if issues >= 2 else ("#b35c00" if issues == 1 else "#1a6e36")
            status_dot   = "🔴" if issues >= 2 else ("🟡" if issues == 1 else "🟢")
            status_text  = f"{issues} issue{'s' if issues != 1 else ''}" if issues > 0 else "No issues"
            added_label  = f" · Watching since {added_date}" if added_date else ""

            # ── Load note for this tutor ──────────────
            notes_df   = load_watchlist_notes()
            tutor_note = ""
            note_date  = ""
            if not notes_df.empty and tutor in notes_df["tutor_name"].values:
                note_row   = notes_df[notes_df["tutor_name"] == tutor].iloc[0]
                tutor_note = str(note_row.get("note", ""))
                note_date  = str(note_row.get("updated_at", ""))

            # ── Card wrapper ──────────────────────────
            st.markdown(f"""
            <div style='border:2px solid {header_color}; border-radius:12px;
                        margin-bottom:8px; overflow:hidden;'>
                <div style='background:#ffffff; border-left:6px solid {header_color};
                            padding:14px 20px;
                            display:flex; justify-content:space-between; align-items:center;'>
                    <span style='color:{header_color}; font-size:1.2rem; font-weight:700;'>{tutor}</span>
                    <span style='color:#555; font-size:0.88rem; font-weight:400;'>
                        {status_dot} {status_text}{added_label}
                    </span>
                </div>
            </div>""", unsafe_allow_html=True)

            # ── Notes + Thresholds (right under the header) ──
            expander_label = f"📌 Notes & Alert Thresholds — {tutor}" if tutor_note \
                             else f"📝 Notes & Alert Thresholds — {tutor}"
            with st.expander(expander_label, expanded=False):

                note_col, thresh_col = st.columns([1, 1])

                with note_col:
                    st.markdown("**📝 Notes**")
                    if tutor_note:
                        st.info(f"{tutor_note}\n\n*Last updated: {note_date}*")
                    new_note = st.text_area(
                        "Edit note:" if tutor_note else "Add a note about this tutor:",
                        value=tutor_note,
                        height=120,
                        placeholder="e.g. Parent complaint on 3/15. Following up next week.",
                        key=f"note_{tutor}"
                    )
                    nc1, nc2 = st.columns(2)
                    with nc1:
                        if st.button("💾 Save Note", key=f"save_note_{tutor}"):
                            save_watchlist_note(tutor, new_note)
                            st.success("Note saved.")
                            st.rerun()
                    with nc2:
                        if tutor_note and st.button("🗑️ Delete Note", key=f"del_note_{tutor}"):
                            delete_watchlist_note(tutor)
                            st.success("Note deleted.")
                            st.rerun()

                with thresh_col:
                    st.markdown("**🚨 Alert Thresholds**")
                    st.caption("Flag this tutor's metric as red when it reaches or exceeds:")
                    tc1, tc2 = st.columns(2)
                    with tc1:
                        th_arch   = st.number_input("Archivable students ≥",
                                                     min_value=0, value=int(t["arch_count"]),
                                                     key=f"th_arch_{tutor}")
                        th_ng     = st.number_input("No grades ≥",
                                                     min_value=0, value=int(t["no_grades"]),
                                                     key=f"th_ng_{tutor}")
                        th_sg     = st.number_input("Stale grades ≥",
                                                     min_value=0, value=int(t["stale_grades"]),
                                                     key=f"th_sg_{tutor}")
                        th_ne     = st.number_input("No exam ≥",
                                                     min_value=0, value=int(t["no_exam"]),
                                                     key=f"th_ne_{tutor}")
                    with tc2:
                        th_se     = st.number_input("Stale exams ≥",
                                                     min_value=0, value=int(t["stale_exams"]),
                                                     key=f"th_se_{tutor}")
                        th_uh     = st.number_input("Unscheduled hrs ≥",
                                                     min_value=0, value=int(t["unsched_hrs"]),
                                                     key=f"th_uh_{tutor}")
                        th_pu     = st.number_input("% Unscheduled ≥",
                                                     min_value=0, value=int(t["pct_unscheduled"]),
                                                     key=f"th_pu_{tutor}")

                    thresh_btn_col, reset_btn_col = st.columns(2)
                    with thresh_btn_col:
                        if st.button("💾 Save Thresholds", key=f"save_thresh_{tutor}"):
                            save_watchlist_thresholds(tutor, {
                                "arch_count":       th_arch,
                                "unsched_hrs":      th_uh,
                                "pct_unscheduled":  th_pu,
                                "no_grades":        th_ng,
                                "stale_grades":     th_sg,
                                "no_exam":          th_ne,
                                "stale_exams":      th_se,
                            })
                            st.success("Thresholds saved.")
                            st.rerun()
                    with reset_btn_col:
                        if st.button("↩️ Reset to Defaults", key=f"reset_thresh_{tutor}"):
                            delete_watchlist_thresholds(tutor)
                            st.success("Reset to defaults.")
                            st.rerun()

            st.markdown("<div style='margin-top:4px;'></div>", unsafe_allow_html=True)

            # ── Note callout (visible below expander when note exists) ──
            if tutor_note:
                st.info(f"📌 **Note:** {tutor_note}  \n*Last updated: {note_date}*")

            # ── Metric rows (two rows of 4) ───────────
            mr1c1, mr1c2, mr1c3, mr1c4 = st.columns(4)
            mr2c1, mr2c2, mr2c3, mr2c4 = st.columns(4)

            hpe_display = f"{hours_per_exam_wl:.1f}" if hours_per_exam_wl is not None else "N/A"

            mr1c1.markdown(_metric_html("📦 Archivable Students", arch_count,
                                        delta_str(arch_count, "arch_count"),
                                        is_bad=arch_count >= t["arch_count"],
                                        is_anomaly=anomaly_flags.get("arch_count")), unsafe_allow_html=True)
            mr1c2.markdown(_metric_html("⏳ Unscheduled Hours", f"{unsched_hrs:.1f}",
                                        delta_str(unsched_hrs, "unsched_hrs"),
                                        is_bad=unsched_hrs >= t["unsched_hrs"] if t["unsched_hrs"] > 0 else False,
                                        is_anomaly=anomaly_flags.get("unsched_hrs")),
                           unsafe_allow_html=True)
            mr1c3.markdown(_metric_html("📊 % Hours Unscheduled", f"{pct_unscheduled_wl:.1f}%",
                                        delta_str(pct_unscheduled_wl, "pct_unscheduled"),
                                        is_bad=pct_unscheduled_wl >= t["pct_unscheduled"]), unsafe_allow_html=True)
            mr1c4.markdown(_metric_html("📋 No Grades Entered", no_grades_count,
                                        delta_str(no_grades_count, "no_grades"),
                                        is_bad=no_grades_count >= t["no_grades"],
                                        is_anomaly=anomaly_flags.get("no_grades")), unsafe_allow_html=True)
            mr2c1.markdown(_metric_html("📚 Stale Grades >90d", stale_count_wl,
                                        delta_str(stale_count_wl, "stale_grades"),
                                        is_bad=stale_count_wl >= t["stale_grades"],
                                        is_anomaly=anomaly_flags.get("stale_grades")), unsafe_allow_html=True)
            mr2c2.markdown(_metric_html("📝 No Completed Exam", no_exam_count,
                                        delta_str(no_exam_count, "no_exam"),
                                        is_bad=no_exam_count >= t["no_exam"],
                                        is_anomaly=anomaly_flags.get("no_exam")), unsafe_allow_html=True)
            mr2c3.markdown(_metric_html("🕐 Stale Exams >90d", stale_exam_count_wl,
                                        delta_str(stale_exam_count_wl, "stale_exams"),
                                        is_bad=stale_exam_count_wl >= t["stale_exams"],
                                        is_anomaly=anomaly_flags.get("stale_exams")), unsafe_allow_html=True)
            mr2c4.markdown(_metric_html("⚡ Avg Hrs / Exam", hpe_display,
                                        delta_str(hours_per_exam_wl, "hours_per_exam"),
                                        is_bad=False), unsafe_allow_html=True)

            st.markdown("<div style='margin-top:6px;'></div>", unsafe_allow_html=True)

            # ── KPI trend charts ───────────────────────
            if not wl_kpi_df.empty:
                wl_kpi_df["Date Range Parsed"] = pd.to_datetime(
                    wl_kpi_df["Date Range"].str.split(" - ").str[0], errors="coerce")
                tutor_kpi = wl_kpi_df[wl_kpi_df["Tutor Name"] == tutor].copy()

                if not tutor_kpi.empty:
                    kpi_trend_metrics = [
                        ("% to Delivery Target",           "Delivery %",       "#1f77b4"),
                        ("% to Availability Target",       "Availability %",   "#2ca02c"),
                        ("% Sessions on Time",             "Sessions On Time",  "#d62728"),
                        ("% Parents Updates Done on Time", "Parent Updates %", "#ff7f0e"),
                    ]

                    tutor_kpi["Date Parsed"] = tutor_kpi["Date Range"].apply(_parse_end)
                    tutor_kpi = tutor_kpi.sort_values("Date Parsed").tail(3)

                    kpi_chart_cols = st.columns(4)
                    for ci, (m, label, color) in enumerate(kpi_trend_metrics):
                        if m not in tutor_kpi.columns:
                            continue
                        vals = (tutor_kpi[m] * 100).round(1)
                        y_max = max(float(vals.max()) * 1.15, 110) if not vals.empty else 130

                        fig_kpi = go.Figure()
                        fig_kpi.add_trace(go.Scatter(
                            x=tutor_kpi["Date Range"],
                            y=vals,
                            mode="lines+markers+text",
                            line=dict(width=2.5, color=color),
                            marker=dict(size=8, color=color),
                            text=vals.apply(lambda v: f"{v:.0f}%"),
                            textposition="top center",
                            textfont=dict(size=10)
                        ))
                        fig_kpi.add_hrect(y0=90, y1=y_max,
                                          fillcolor="rgba(0,180,0,0.07)", line_width=0)
                        fig_kpi.add_hrect(y0=0, y1=75,
                                          fillcolor="rgba(220,0,0,0.05)", line_width=0)
                        fig_kpi.add_hline(y=90, line_dash="dash",
                                          line_color="rgba(0,150,0,0.4)", line_width=1)
                        fig_kpi.update_layout(
                            title=dict(text=label, font=dict(size=12, color="#444"),
                                       x=0, xanchor="left"),
                            height=200,
                            plot_bgcolor="white",
                            paper_bgcolor="white",
                            xaxis=dict(tickangle=15, gridcolor="#f5f5f5",
                                       tickfont=dict(size=9), showline=True,
                                       linecolor="#ddd"),
                            yaxis=dict(range=[0, y_max], ticksuffix="%",
                                       gridcolor="#f5f5f5", tickfont=dict(size=9),
                                       showline=True, linecolor="#ddd"),
                            margin=dict(l=10, r=10, t=30, b=50),
                            showlegend=False
                        )
                        kpi_chart_cols[ci].plotly_chart(fig_kpi, use_container_width=True,
                                                        key=f"wl_{tutor}_{ci}")
                else:
                    st.caption(f"No KPI trend data found for {tutor}.")

            st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

        if st.sidebar.button("🔄 Refresh Watch List Data", key="refresh_watchlist"):
            st.cache_data.clear()
            st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: TEST PREP & EXAMS
    # ─────────────────────────────────────────────

    if page == "Test Prep & Exams":
        st.markdown('<div class="main-title">Test Prep & Exams 📝</div>', unsafe_allow_html=True)

        st.info(
            "ℹ️ **About this page**\n\n"
            "Shows exam data for all active test-prep students (met with in the last 30 days). "
            "Students with **6+ hours** of test-prep tutoring should have at least one completed practice exam.\n\n"
            "**Completion rules:** SAT/PSAT sections < 300 or ACT sections < 10 are invalid — an invalid section "
            "invalidates the composite but valid sections can still show section-level improvement.\n\n"
            "**Score improvement** is measured from the most recent exam *before or on* the first session date "
            "to the last or highest completed exam with this tutor. Official exams always serve as the final benchmark.\n\n"
            "**Stale Exams:** A student is considered to have a stale exam if their most recent *completed* practice "
            "exam (one where all section thresholds were met) was recorded more than 90 days ago. "
            "Students with no completed exam at all are flagged separately under 'No Completed Exam'.\n\n"
            "**Once a week, this data is captured and stored for trend tracking.**",
            icon=None
        )

        with st.spinner("Loading live exam data from Revolution Prep database..."):
            try:
                raw_exam_df, exam_fetched_at = load_exam_data()
            except Exception as e:
                st.error(f"Could not connect to Revolution Prep database: {e}")
                st.stop()

        if raw_exam_df.empty:
            st.info("No exam data returned from the database.")
            st.stop()

        st.caption(f"🕐 Data last updated: **{exam_fetched_at}**")
        st.sidebar.markdown(f"🕐 **Exam data last updated**  \n{exam_fetched_at}")

        # Filter to Team De Groot
        team_exam_df = raw_exam_df[raw_exam_df["team_name"] == "Team De Groot"].copy()

        if team_exam_df.empty:
            st.warning("No exam records found for Team De Groot.")
            st.stop()

        # ── Normalise date columns ────────────────────────────
        for dc in ["first_session_day", "most_recent_session", "exam_date"]:
            team_exam_df[dc] = pd.to_datetime(team_exam_df[dc], errors="coerce", utc=True)

        for nc in ["score", "act_english", "act_math", "act_reading", "act_science", "sat_math", "sat_rw",
                   "test_prep_hours_delivered"]:
            team_exam_df[nc] = pd.to_numeric(team_exam_df[nc], errors="coerce")

        # ── Determine exam family ─────────────────────────────
        SAT_TYPES = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT","Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
        ACT_TYPES = {"ACT","Digital ACT"}

        team_exam_df["exam_family"] = team_exam_df["subject"].apply(
            lambda x: "SAT/PSAT" if x in SAT_TYPES else ("ACT" if x in ACT_TYPES else "Other")
        )

        # ── Official exam flag ────────────────────────────────
        # From exams_production rows: attempt is numeric. From orbit rows: attempt = 'n/a'
        team_exam_df["is_official"] = team_exam_df["exam_code"].str.upper().str.contains("OFFICIAL", na=False) | \
                                       (team_exam_df["attempt"].astype(str) != "n/a")

        # ── Section validity flags ────────────────────────────
        # SAT: both sat_math and sat_rw must be >= 300 to be "fully complete"
        # ACT: all present sections must be >= 10 (science may be NaN — that's OK)
        def sat_section_valid(row):
            m_ok  = pd.isna(row["sat_math"])  or row["sat_math"]  >= 300
            rw_ok = pd.isna(row["sat_rw"])    or row["sat_rw"]    >= 300
            return {"sat_math_valid": not pd.isna(row["sat_math"])  and row["sat_math"]  >= 300,
                    "sat_rw_valid":   not pd.isna(row["sat_rw"])    and row["sat_rw"]    >= 300,
                    "sat_composite_valid": (not pd.isna(row["sat_math"]) and row["sat_math"] >= 300) and
                                           (not pd.isna(row["sat_rw"])   and row["sat_rw"]   >= 300)}

        def act_section_valid(row):
            eng_ok  = pd.isna(row["act_english"]) or row["act_english"] >= 10
            math_ok = pd.isna(row["act_math"])    or row["act_math"]    >= 10
            read_ok = pd.isna(row["act_reading"]) or row["act_reading"] >= 10
            # science may be blank — don't count it against validity
            return {"act_english_valid": not pd.isna(row["act_english"]) and row["act_english"] >= 10,
                    "act_math_valid":    not pd.isna(row["act_math"])    and row["act_math"]    >= 10,
                    "act_reading_valid": not pd.isna(row["act_reading"]) and row["act_reading"] >= 10,
                    "act_science_valid": pd.isna(row["act_science"])     or row["act_science"]  >= 10,
                    "act_composite_valid": (pd.isna(row["act_english"]) or row["act_english"] >= 10) and
                                           (pd.isna(row["act_math"])    or row["act_math"]    >= 10) and
                                           (pd.isna(row["act_reading"]) or row["act_reading"] >= 10)}

        sat_validity = team_exam_df[team_exam_df["exam_family"] == "SAT/PSAT"].apply(sat_section_valid, axis=1, result_type="expand")
        act_validity = team_exam_df[team_exam_df["exam_family"] == "ACT"].apply(act_section_valid, axis=1, result_type="expand")

        # Initialise validity columns
        for col in ["sat_math_valid","sat_rw_valid","sat_composite_valid",
                    "act_english_valid","act_math_valid","act_reading_valid","act_science_valid","act_composite_valid"]:
            team_exam_df[col] = None

        if not sat_validity.empty:
            for col in sat_validity.columns:
                team_exam_df.loc[sat_validity.index, col] = sat_validity[col]
        if not act_validity.empty:
            for col in act_validity.columns:
                team_exam_df.loc[act_validity.index, col] = act_validity[col]

        # Overall "is this exam row valid for composite" flag
        team_exam_df["exam_valid_composite"] = team_exam_df.apply(
            lambda r: r["sat_composite_valid"] if r["exam_family"] == "SAT/PSAT"
                      else (r["act_composite_valid"] if r["exam_family"] == "ACT" else None),
            axis=1
        )

        # "Invalidity reason" for display
        def invalidity_reason(r):
            reasons = []
            if r["exam_family"] == "SAT/PSAT":
                if pd.notna(r["sat_math"])  and r["sat_math"]  < 300: reasons.append("Math < 300")
                if pd.notna(r["sat_rw"])    and r["sat_rw"]    < 300: reasons.append("R&W < 300")
            elif r["exam_family"] == "ACT":
                if pd.notna(r["act_english"]) and r["act_english"] < 10: reasons.append("English < 10")
                if pd.notna(r["act_math"])    and r["act_math"]    < 10: reasons.append("Math < 10")
                if pd.notna(r["act_reading"]) and r["act_reading"] < 10: reasons.append("Reading < 10")
                if pd.notna(r["act_science"]) and r["act_science"] < 10: reasons.append("Science < 10")
            return ", ".join(reasons) if reasons else ""

        team_exam_df["invalidity_reason"] = team_exam_df.apply(invalidity_reason, axis=1)

        # ── Days since most recent completed exam per student ─
        now_utc = pd.Timestamp.now(tz="UTC")
        completed_exam_df = team_exam_df[team_exam_df["exam_valid_composite"] == True].copy()

        latest_exam_per_student = (
            completed_exam_df.groupby(["tutor_id","student_id"])["exam_date"]
            .max().reset_index()
            .rename(columns={"exam_date": "latest_completed_exam_date"})
        )
        team_exam_df = team_exam_df.merge(latest_exam_per_student, on=["tutor_id","student_id"], how="left")
        team_exam_df["days_since_completed_exam"] = (now_utc - team_exam_df["latest_completed_exam_date"]).dt.days

        # ── Save weekly snapshot ──────────────────────────────
        exam_snap_df = save_exams_weekly_snapshot(team_exam_df)

        # ── Score improvement helper ──────────────────────────
        def compute_improvement(student_df, exam_family, mode="last", score_col="score",
                                section_cols=None):
            """
            Returns (baseline_score, endpoint_score, improvement, baseline_row, endpoint_row)
            mode: 'last' or 'highest'
            Baseline = most recent valid exam ON OR BEFORE first_session_day.
            Endpoint = most recent (last) or highest scoring valid exam AFTER first_session_day,
                       but if an official exam exists, that overrides as endpoint regardless.
            """
            fsd = student_df["first_session_day"].iloc[0]
            fam_df = student_df[student_df["exam_family"] == exam_family].copy()
            fam_df = fam_df[fam_df["exam_valid_composite"] == True].copy()
            if fam_df.empty:
                return None, None, None, None, None

            fam_df = fam_df.sort_values("exam_date")

            baseline_df = fam_df[fam_df["exam_date"] <= fsd]
            after_df    = fam_df[fam_df["exam_date"] >  fsd]

            if baseline_df.empty or after_df.empty:
                return None, None, None, None, None

            baseline_row = baseline_df.sort_values("exam_date").iloc[-1]  # most recent before/on first session

            # Official exams override endpoint
            official_after = after_df[after_df["is_official"] == True].dropna(subset=[score_col])
            after_df_valid  = after_df.dropna(subset=[score_col])

            if after_df_valid.empty:
                return None, None, None, None, None

            if not official_after.empty:
                if mode == "highest":
                    endpoint_row = official_after.loc[official_after[score_col].idxmax()]
                else:
                    endpoint_row = official_after.sort_values("exam_date").iloc[-1]
            else:
                if mode == "highest":
                    endpoint_row = after_df_valid.loc[after_df_valid[score_col].idxmax()]
                else:
                    endpoint_row = after_df_valid.sort_values("exam_date").iloc[-1]

            b_score = baseline_row[score_col]
            e_score = endpoint_row[score_col]
            improvement = (e_score - b_score) if pd.notna(b_score) and pd.notna(e_score) else None
            return b_score, e_score, improvement, baseline_row, endpoint_row

        # ── Build per-student improvement table ──────────────
        def build_student_improvement(df, mode="last"):
            records = []
            for (tutor_id, tutor_name, student_id, student_name), sdf in df.groupby(
                    ["tutor_id","tutor_name","student_id","student_name"]):
                hours = sdf["test_prep_hours_delivered"].iloc[0]
                fsd   = sdf["first_session_day"].iloc[0]
                mrs   = sdf["most_recent_session"].iloc[0]

                for fam in ["SAT/PSAT", "ACT"]:
                    sc = "score"
                    b, e, imp, b_row, e_row = compute_improvement(sdf, fam, mode=mode, score_col=sc)

                    # Section-level improvements (use valid sections even if composite invalid)
                    fam_all = sdf[sdf["exam_family"] == fam].copy()
                    fam_all = fam_all.sort_values("exam_date")
                    before_all = fam_all[fam_all["exam_date"] <= fsd]
                    after_all  = fam_all[fam_all["exam_date"] >  fsd]

                    def section_imp(section_col, valid_col):
                        if before_all.empty or after_all.empty:
                            return None, None, None
                        b_sec = before_all[before_all[valid_col] == True]
                        a_sec = after_all[after_all[valid_col] == True]
                        if b_sec.empty or a_sec.empty:
                            return None, None, None
                        # Drop NaN values in the section column before comparing
                        b_sec_valid = b_sec.dropna(subset=[section_col])
                        a_sec_valid = a_sec.dropna(subset=[section_col])
                        if b_sec_valid.empty or a_sec_valid.empty:
                            return None, None, None
                        bv = b_sec_valid.sort_values("exam_date").iloc[-1][section_col]
                        if mode == "highest":
                            idx = a_sec_valid[section_col].idxmax()
                            if pd.isna(idx):
                                return None, None, None
                            av = a_sec_valid.loc[idx][section_col]
                        else:
                            av = a_sec_valid.sort_values("exam_date").iloc[-1][section_col]
                        return bv, av, (av - bv) if pd.notna(bv) and pd.notna(av) else None

                    if fam == "SAT/PSAT":
                        bm, em, imp_m = section_imp("sat_math", "sat_math_valid")
                        br, er, imp_r = section_imp("sat_rw",   "sat_rw_valid")
                        sec_imps = {"sat_math_baseline": bm, "sat_math_endpoint": em, "sat_math_improvement": imp_m,
                                    "sat_rw_baseline":   br, "sat_rw_endpoint":   er, "sat_rw_improvement":   imp_r}
                    else:
                        beng, eeng, imp_eng   = section_imp("act_english", "act_english_valid")
                        bmat, emat, imp_mat   = section_imp("act_math",    "act_math_valid")
                        bred, ered, imp_red   = section_imp("act_reading",  "act_reading_valid")
                        bsci, esci, imp_sci   = section_imp("act_science",  "act_science_valid")
                        sec_imps = {"act_english_baseline": beng, "act_english_endpoint": eeng, "act_english_improvement": imp_eng,
                                    "act_math_baseline":    bmat, "act_math_endpoint":    emat, "act_math_improvement":    imp_mat,
                                    "act_reading_baseline": bred, "act_reading_endpoint":  ered, "act_reading_improvement":  imp_red,
                                    "act_science_baseline": bsci, "act_science_endpoint":  esci, "act_science_improvement":  imp_sci}

                    if b is not None or e is not None:
                        # Count completed exams after first session for hours-per-exam metric
                        fam_completed_after = sdf[
                            (sdf["exam_family"] == fam) &
                            (sdf["exam_valid_composite"] == True) &
                            (sdf["exam_date"] > fsd)
                        ]["exam_id"].nunique()
                        hours_per_exam = (
                            round(hours / fam_completed_after, 1)
                            if fam_completed_after > 0 and pd.notna(hours) else None
                        )
                        rec = {
                            "tutor_id": tutor_id, "tutor_name": tutor_name,
                            "student_id": student_id, "student_name": student_name,
                            "exam_family": fam,
                            "hours_delivered": hours,
                            "completed_exams": fam_completed_after,
                            "hours_per_exam": hours_per_exam,
                            "first_session_day": fsd,
                            "most_recent_session": mrs,
                            "baseline_score": b,
                            "endpoint_score": e,
                            "improvement": imp,
                            "baseline_date": b_row["exam_date"] if b_row is not None else None,
                            "endpoint_date": e_row["exam_date"] if e_row is not None else None,
                            "endpoint_is_official": e_row["is_official"] if e_row is not None else None,
                        }
                        rec.update(sec_imps)
                        records.append(rec)

            return pd.DataFrame(records)

        # ── Build per-tutor flag summary ──────────────────────
        def build_tutor_flag_summary(df):
            rows = []
            for tutor, tdf in df.groupby("tutor_name"):
                total_students  = tdf["student_id"].nunique()
                eligible_mask   = tdf.groupby("student_id")["test_prep_hours_delivered"].first() >= 6
                eligible_ids    = eligible_mask[eligible_mask].index.tolist()
                n_eligible      = len(eligible_ids)

                no_exam_ids, stale_exam_ids = [], []
                for sid in eligible_ids:
                    sdf = tdf[tdf["student_id"] == sid]
                    completed = sdf[sdf["exam_valid_composite"] == True]
                    if completed.empty:
                        no_exam_ids.append(sid)
                    else:
                        latest = pd.to_datetime(completed["exam_date"], utc=True).max()
                        if pd.notna(latest) and (now_utc - latest).days > 90:
                            stale_exam_ids.append(sid)

                rows.append({
                    "tutor_name":          tutor,
                    "total_students":      total_students,
                    "eligible_students":   n_eligible,
                    "no_exam_students":    len(no_exam_ids),
                    "stale_exam_students": len(stale_exam_ids),
                    "pct_with_exam": round((n_eligible - len(no_exam_ids)) / n_eligible * 100, 1) if n_eligible > 0 else None,
                })
            return pd.DataFrame(rows)

        tutor_flag_summary = build_tutor_flag_summary(team_exam_df)

        # ─────────────────────────────────────────────
        # TOP CONCERN FLAGS
        # ─────────────────────────────────────────────
        st.markdown("### 🚨 Top Tutors to Address")
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]

        fc1, fc2, fc3 = st.columns(3)

        with fc1:
            st.markdown("**Most Eligible Students With No Completed Exam (Top 5)**")
            top_no_exam = (tutor_flag_summary[tutor_flag_summary["no_exam_students"] > 0]
                           .sort_values("no_exam_students", ascending=False).head(5))
            if top_no_exam.empty:
                st.success("✅ All eligible students have a completed exam.")
            else:
                for rank, (_, row) in enumerate(top_no_exam.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['no_exam_students'])} students</span>",
                        unsafe_allow_html=True)

        with fc2:
            st.markdown("**Most Students With Stale Exams >90 Days (Top 5)**")
            top_stale = (tutor_flag_summary[tutor_flag_summary["stale_exam_students"] > 0]
                         .sort_values("stale_exam_students", ascending=False).head(5))
            if top_stale.empty:
                st.success("✅ All students have a recent completed exam.")
            else:
                for rank, (_, row) in enumerate(top_stale.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#b35c00; font-weight:bold'>{int(row['stale_exam_students'])} students</span>",
                        unsafe_allow_html=True)

        with fc3:
            st.markdown("**Lowest % Eligible Students With a Completed Exam (Top 5)**")
            top_low_pct = (tutor_flag_summary[tutor_flag_summary["eligible_students"] > 0]
                           .sort_values("pct_with_exam", ascending=True).head(5))
            if top_low_pct.empty:
                st.success("✅ No data to display.")
            else:
                for rank, (_, row) in enumerate(top_low_pct.iterrows()):
                    val = f"{row['pct_with_exam']:.0f}%" if pd.notna(row["pct_with_exam"]) else "N/A"
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#555; font-weight:bold'>{val}</span>",
                        unsafe_allow_html=True)

        st.divider()

        # ─────────────────────────────────────────────
        # TEAM OVERVIEW METRICS
        # ─────────────────────────────────────────────
        st.markdown("### 📊 Team Overview")

        total_tp_students  = team_exam_df["student_id"].nunique()
        total_eligible     = int(tutor_flag_summary["eligible_students"].sum())
        total_no_exam      = int(tutor_flag_summary["no_exam_students"].sum())
        total_stale        = int(tutor_flag_summary["stale_exam_students"].sum())
        pct_with_exam_team = round((total_eligible - total_no_exam) / total_eligible * 100, 1) if total_eligible > 0 else 0

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Test Prep Students", total_tp_students)
        m2.metric("Eligible (6+ hrs)",        total_eligible)
        m3.metric("No Completed Exam",         total_no_exam, delta_color="inverse")
        m4.metric("Stale Exam (>90 days)",     total_stale,   delta_color="inverse")
        m5.metric("% Eligible w/ Exam",        f"{pct_with_exam_team:.1f}%")

        # ── Score improvement & hours-per-exam summary ────────
        st.markdown("#### 📈 Score Improvement & Efficiency Summary")
        ov_mode = st.radio("Improvement mode", ["First → Last", "First → Highest"],
                           horizontal=True, key="overview_imp_mode")
        ov_mode_key = "last" if ov_mode == "First → Last" else "highest"
        ov_imp_df   = build_student_improvement(team_exam_df, mode=ov_mode_key)

        if not ov_imp_df.empty:
            ov_col1, ov_col2 = st.columns(2)

            for fam, col in [("SAT/PSAT", ov_col1), ("ACT", ov_col2)]:
                fam_ov = ov_imp_df[(ov_imp_df["exam_family"] == fam) & ov_imp_df["improvement"].notna()]
                with col:
                    st.markdown(f"**{fam}**")
                    if fam_ov.empty:
                        st.caption("Not enough data for improvement calculation.")
                    else:
                        avg_imp      = fam_ov["improvement"].mean()
                        pct_improved = (fam_ov["improvement"] > 0).mean() * 100
                        avg_hpe      = fam_ov["hours_per_exam"].dropna().mean()
                        n_students   = len(fam_ov)

                        ci1, ci2, ci3, ci4 = st.columns(4)
                        ci1.metric("Avg Improvement",    f"{avg_imp:+.0f} pts")
                        ci2.metric("% Improved",         f"{pct_improved:.0f}%")
                        ci3.metric("Avg Hrs / Exam",     f"{avg_hpe:.1f}" if pd.notna(avg_hpe) else "N/A")
                        ci4.metric("Students w/ Data",   n_students)

                        # Tutor-level improvement summary bar
                        tutor_ov = (fam_ov.groupby("tutor_name")
                                    .agg(avg_improvement=("improvement","mean"),
                                         avg_hrs_per_exam=("hours_per_exam","mean"),
                                         n=("student_name","count"))
                                    .reset_index()
                                    .sort_values("avg_improvement", ascending=True))

                        if not tutor_ov.empty:
                            max_abs_t = max(abs(tutor_ov["avg_improvement"].min()),
                                            abs(tutor_ov["avg_improvement"].max()), 1)
                            fig_ov = px.bar(
                                tutor_ov, x="avg_improvement", y="tutor_name",
                                orientation="h",
                                color="avg_improvement",
                                color_continuous_scale=["#cc0000","#ffffff","#006400"],
                                range_color=[-max_abs_t, max_abs_t],
                                text=tutor_ov["avg_improvement"].apply(lambda v: f"{v:+.0f}"),
                                title=f"Avg {fam} Composite Improvement by Tutor",
                                height=max(280, len(tutor_ov) * 30),
                                hover_data={"avg_hrs_per_exam": ":.1f", "n": True}
                            )
                            fig_ov.update_layout(
                                title=dict(x=0.5, xanchor="center"),
                                xaxis_title="Avg Score Change", yaxis_title="",
                                showlegend=False, coloraxis_showscale=False,
                                margin=dict(l=160, r=60, t=50, b=30)
                            )
                            fig_ov.add_vline(x=0, line_dash="dash", line_color="grey")
                            fig_ov.update_traces(textposition="outside")
                            st.plotly_chart(fig_ov, use_container_width=True)

        st.divider()

        # ─────────────────────────────────────────────
        # FILTERS
        # ─────────────────────────────────────────────
        st.markdown("### 🔍 Filters")
        ef1, ef2, ef3 = st.columns(3)
        with ef1:
            tutor_opts_e = ["All Tutors"] + sorted(team_exam_df["tutor_name"].dropna().unique().tolist())
            sel_tutor_e  = st.selectbox("Tutor", tutor_opts_e, key="exam_tutor")
        with ef2:
            exam_fam_opts = ["All Exam Types", "SAT/PSAT", "ACT"]
            sel_exam_fam  = st.selectbox("Exam Type", exam_fam_opts, key="exam_fam")
        with ef3:
            exam_status_opts = ["All Students", "Eligible (6+ hrs) Only",
                                "No Completed Exam", "Stale Exam (>90 days)"]
            sel_exam_status  = st.selectbox("Student Status", exam_status_opts, key="exam_status")

        view_exam_df = team_exam_df.copy()
        if sel_tutor_e != "All Tutors":
            view_exam_df = view_exam_df[view_exam_df["tutor_name"] == sel_tutor_e]
        if sel_exam_fam != "All Exam Types":
            view_exam_df = view_exam_df[view_exam_df["exam_family"] == sel_exam_fam]

        # Apply student-status filter (operates on student_id level)
        if sel_exam_status == "Eligible (6+ hrs) Only":
            elig_ids = view_exam_df.groupby("student_id")["test_prep_hours_delivered"].first()
            elig_ids = elig_ids[elig_ids >= 6].index
            view_exam_df = view_exam_df[view_exam_df["student_id"].isin(elig_ids)]
        elif sel_exam_status == "No Completed Exam":
            no_exam_ids = []
            for sid, sdf in view_exam_df.groupby("student_id"):
                if sdf["test_prep_hours_delivered"].iloc[0] >= 6 and sdf[sdf["exam_valid_composite"] == True].empty:
                    no_exam_ids.append(sid)
            view_exam_df = view_exam_df[view_exam_df["student_id"].isin(no_exam_ids)]
        elif sel_exam_status == "Stale Exam (>90 days)":
            stale_ids = []
            for sid, sdf in view_exam_df.groupby("student_id"):
                completed = sdf[sdf["exam_valid_composite"] == True]
                if not completed.empty:
                    latest = pd.to_datetime(completed["exam_date"], utc=True).max()
                    if pd.notna(latest) and (now_utc - latest).days > 90:
                        stale_ids.append(sid)
            view_exam_df = view_exam_df[view_exam_df["student_id"].isin(stale_ids)]

        single_tutor_exam = sel_tutor_e != "All Tutors"

        st.divider()

        # ─────────────────────────────────────────────
        # TABS
        # ─────────────────────────────────────────────
        tab_overview, tab_improvement, tab_detail, tab_trends = st.tabs([
            "📊 Team / Tutor Overview",
            "📈 Score Improvement",
            "📋 Exam Detail",
            "📅 Trends Over Time"
        ])

        # ── TAB: OVERVIEW ─────────────────────────────
        with tab_overview:
            if not single_tutor_exam:
                view_flag = build_tutor_flag_summary(view_exam_df)

                # Bar: no exam students
                no_exam_chart = view_flag[view_flag["no_exam_students"] > 0].sort_values("no_exam_students", ascending=True)
                if not no_exam_chart.empty:
                    n = len(no_exam_chart)
                    fig1 = px.bar(no_exam_chart, x="no_exam_students", y="tutor_name",
                                  orientation="h", color="no_exam_students",
                                  color_continuous_scale=["#ffe0e0","#cc0000"],
                                  text="no_exam_students",
                                  title="Eligible Students With No Completed Exam — By Tutor",
                                  height=max(350, n * 30))
                    fig1.update_layout(title=dict(x=0.5, xanchor="center"), showlegend=False,
                                       coloraxis_showscale=False, xaxis_title="# Students", yaxis_title="",
                                       yaxis=dict(autorange="reversed"), margin=dict(l=160, r=20, t=50, b=40))
                    fig1.update_traces(textposition="outside")
                    st.plotly_chart(fig1, use_container_width=True)
                else:
                    st.success("✅ All eligible students have at least one completed exam.")

                # Bar: % eligible with exam
                pct_chart = view_flag[view_flag["eligible_students"] > 0].sort_values("pct_with_exam", ascending=True)
                if not pct_chart.empty:
                    n = len(pct_chart)
                    fig2 = px.bar(pct_chart, x="pct_with_exam", y="tutor_name",
                                  orientation="h", color="pct_with_exam",
                                  color_continuous_scale=["#cc0000","#ffdd99","#006400"],
                                  text=pct_chart["pct_with_exam"].apply(lambda v: f"{v:.0f}%" if pd.notna(v) else ""),
                                  title="% of Eligible Students With a Completed Exam — By Tutor",
                                  height=max(350, n * 30))
                    fig2.update_layout(title=dict(x=0.5, xanchor="center"), showlegend=False,
                                       coloraxis_showscale=False, xaxis_title="% With Exam", yaxis_title="",
                                       xaxis=dict(range=[0,110]), yaxis=dict(autorange="reversed"),
                                       margin=dict(l=160, r=20, t=50, b=40))
                    fig2.update_traces(textposition="outside")
                    st.plotly_chart(fig2, use_container_width=True)

                # Bar: stale exams
                stale_chart = view_flag[view_flag["stale_exam_students"] > 0].sort_values("stale_exam_students", ascending=True)
                if not stale_chart.empty:
                    n = len(stale_chart)
                    fig3 = px.bar(stale_chart, x="stale_exam_students", y="tutor_name",
                                  orientation="h", color="stale_exam_students",
                                  color_continuous_scale=["#fff3cc","#b35c00"],
                                  text="stale_exam_students",
                                  title="Students With No Completed Exam in 90+ Days — By Tutor",
                                  height=max(350, n * 30))
                    fig3.update_layout(title=dict(x=0.5, xanchor="center"), showlegend=False,
                                       coloraxis_showscale=False, xaxis_title="# Students", yaxis_title="",
                                       yaxis=dict(autorange="reversed"), margin=dict(l=160, r=20, t=50, b=40))
                    fig3.update_traces(textposition="outside")
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.success("✅ No stale exams on the team.")

            else:
                # Single tutor: show their own metrics + days-since-exam per student
                sel_flag = tutor_flag_summary[tutor_flag_summary["tutor_name"] == sel_tutor_e]
                if not sel_flag.empty:
                    row = sel_flag.iloc[0]
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    sc1.metric("Total Students",    int(row["total_students"]))
                    sc2.metric("Eligible (6+ hrs)", int(row["eligible_students"]))
                    sc3.metric("No Completed Exam", int(row["no_exam_students"]), delta_color="inverse")
                    sc4.metric("Stale (>90 days)",  int(row["stale_exam_students"]), delta_color="inverse")

                # Days since last completed exam per student
                per_student_last = (
                    view_exam_df[view_exam_df["exam_valid_composite"] == True]
                    .groupby(["student_id","student_name"])["exam_date"].max()
                    .reset_index()
                )
                per_student_last["days_since"] = (now_utc - per_student_last["exam_date"]).dt.days.astype("Int64")
                per_student_last = per_student_last.sort_values("days_since", ascending=True)

                if not per_student_last.empty:
                    fig_days = px.bar(per_student_last, x="days_since", y="student_name",
                                      orientation="h",
                                      text=per_student_last["days_since"].apply(lambda d: f"{d}d" if pd.notna(d) else ""),
                                      title=f"{sel_tutor_e} — Days Since Last Completed Exam (per student)",
                                      height=max(300, len(per_student_last) * 30),
                                      color="days_since",
                                      color_continuous_scale=["#2a7a2a","#ffaa00","#cc0000"],
                                      range_color=[0, max(int(per_student_last["days_since"].max(skipna=True)), 91)])
                    fig_days.update_layout(title=dict(x=0.5, xanchor="center"),
                                           xaxis_title="Days Since Last Completed Exam",
                                           yaxis_title="", showlegend=False, coloraxis_showscale=False,
                                           margin=dict(l=160, r=20, t=50, b=40))
                    fig_days.add_vline(x=90, line_dash="dash", line_color="red",
                                       annotation_text="90-day threshold", annotation_position="top right")
                    fig_days.update_traces(textposition="outside")
                    st.plotly_chart(fig_days, use_container_width=True)

        # ── TAB: SCORE IMPROVEMENT ────────────────────
        with tab_improvement:
            st.markdown("#### Score Improvement Settings")
            imp_col1, imp_col2 = st.columns(2)
            with imp_col1:
                improvement_mode = st.radio("Improvement mode", ["First → Last", "First → Highest"],
                                            horizontal=True, key="imp_mode")
            with imp_col2:
                imp_fam = st.radio("Exam type", ["SAT/PSAT", "ACT"], horizontal=True, key="imp_fam")

            mode_key = "last" if improvement_mode == "First → Last" else "highest"
            imp_df   = build_student_improvement(view_exam_df, mode=mode_key)

            if imp_df.empty:
                st.info("Not enough data to calculate score improvement with the current filters. "
                        "Students need at least one exam before and one exam after their first session.")
            else:
                fam_imp_df = imp_df[imp_df["exam_family"] == imp_fam].copy()

                if fam_imp_df.empty:
                    st.info(f"No {imp_fam} improvement data available with current filters.")
                else:
                    # ── Composite improvement chart ───────────
                    comp_df = fam_imp_df.dropna(subset=["improvement"]).sort_values("improvement", ascending=True)

                    if not comp_df.empty:
                        color_vals = comp_df["improvement"].tolist()
                        max_abs = max(abs(min(color_vals)), abs(max(color_vals)), 1)

                        fig_imp = px.bar(
                            comp_df, x="improvement", y="student_name",
                            orientation="h",
                            color="improvement",
                            color_continuous_scale=["#cc0000","#ffffff","#006400"],
                            range_color=[-max_abs, max_abs],
                            text=comp_df["improvement"].apply(lambda v: f"{v:+.0f}"),
                            title=f"{imp_fam} Composite Score Improvement ({improvement_mode})",
                            height=max(350, len(comp_df) * 30),
                            hover_data={"student_name": True, "tutor_name": True,
                                        "baseline_score": True, "endpoint_score": True,
                                        "hours_delivered": True}
                        )
                        fig_imp.update_layout(
                            title=dict(x=0.5, xanchor="center"),
                            xaxis_title="Score Change", yaxis_title="",
                            showlegend=False, coloraxis_showscale=False,
                            margin=dict(l=180, r=60, t=50, b=40)
                        )
                        fig_imp.add_vline(x=0, line_dash="dash", line_color="grey")
                        fig_imp.update_traces(textposition="outside")
                        st.plotly_chart(fig_imp, use_container_width=True)

                        # Summary metrics
                        avg_imp  = comp_df["improvement"].mean()
                        pct_pos  = (comp_df["improvement"] > 0).mean() * 100
                        avg_hpe  = comp_df["hours_per_exam"].dropna().mean()
                        sm1, sm2, sm3, sm4 = st.columns(4)
                        sm1.metric("Avg Composite Improvement", f"{avg_imp:+.0f} pts")
                        sm2.metric("% Students Improved",       f"{pct_pos:.0f}%")
                        sm3.metric("Avg Hours / Exam",          f"{avg_hpe:.1f}" if pd.notna(avg_hpe) else "N/A")
                        sm4.metric("Students w/ Data",          len(comp_df))

                    st.divider()

                    # ── Section-level improvements ────────────
                    st.markdown("#### Section-Level Improvement")

                    if imp_fam == "SAT/PSAT":
                        section_map = {
                            "Math":             ("sat_math_baseline",   "sat_math_endpoint",   "sat_math_improvement"),
                            "Reading & Writing":("sat_rw_baseline",     "sat_rw_endpoint",     "sat_rw_improvement"),
                        }
                    else:
                        section_map = {
                            "English":  ("act_english_baseline", "act_english_endpoint", "act_english_improvement"),
                            "Math":     ("act_math_baseline",    "act_math_endpoint",    "act_math_improvement"),
                            "Reading":  ("act_reading_baseline", "act_reading_endpoint", "act_reading_improvement"),
                            "Science":  ("act_science_baseline", "act_science_endpoint", "act_science_improvement"),
                        }

                    for sec_name, (b_col, e_col, imp_col) in section_map.items():
                        if imp_col not in fam_imp_df.columns:
                            continue
                        sec_df = fam_imp_df.dropna(subset=[imp_col]).copy()
                        sec_df = sec_df.sort_values(imp_col, ascending=True)
                        if sec_df.empty:
                            continue

                        max_abs_s = max(abs(sec_df[imp_col].min()), abs(sec_df[imp_col].max()), 1)
                        fig_sec = px.bar(
                            sec_df, x=imp_col, y="student_name",
                            orientation="h",
                            color=imp_col,
                            color_continuous_scale=["#cc0000","#ffffff","#006400"],
                            range_color=[-max_abs_s, max_abs_s],
                            text=sec_df[imp_col].apply(lambda v: f"{v:+.0f}"),
                            title=f"{imp_fam} — {sec_name} Section ({improvement_mode})",
                            height=max(280, len(sec_df) * 28)
                        )
                        fig_sec.update_layout(
                            title=dict(x=0.5, xanchor="center"),
                            xaxis_title="Score Change", yaxis_title="",
                            showlegend=False, coloraxis_showscale=False,
                            margin=dict(l=180, r=60, t=50, b=30)
                        )
                        fig_sec.add_vline(x=0, line_dash="dash", line_color="grey")
                        fig_sec.update_traces(textposition="outside")
                        st.plotly_chart(fig_sec, use_container_width=True)

        # ── TAB: EXAM DETAIL ──────────────────────────
        with tab_detail:
            if view_exam_df.empty:
                st.info("No records match the current filters.")
            else:
                detail = view_exam_df.copy()

                # Format dates
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    if dc in detail.columns:
                        detail[dc] = detail[dc].dt.strftime("%Y-%m-%d")

                # Build status column
                def exam_status_label(r):
                    if pd.isna(r.get("exam_id")) or r.get("exam_id") is None:
                        return "No Exam Data"
                    if r.get("exam_valid_composite") == True:
                        suffix = " (Official)" if r.get("is_official") else ""
                        return f"✅ Valid{suffix}"
                    reason = r.get("invalidity_reason","")
                    return f"⚠️ Invalid — {reason}" if reason else "⚠️ Invalid"

                detail["exam_status"] = detail.apply(exam_status_label, axis=1)

                display_cols = [
                    "tutor_name","student_name","test_prep_hours_delivered",
                    "first_session_day","most_recent_session",
                    "exam_date","subject","exam_code","exam_status","score",
                    "act_english","act_math","act_reading","act_science",
                    "sat_math","sat_rw","invalidity_reason"
                ]
                display_cols = [c for c in display_cols if c in detail.columns]
                detail_display = detail[display_cols].rename(columns={
                    "tutor_name":              "Tutor",
                    "student_name":            "Student",
                    "test_prep_hours_delivered":"Hours Delivered",
                    "first_session_day":       "First Session",
                    "most_recent_session":     "Most Recent Session",
                    "exam_date":               "Exam Date",
                    "subject":                 "Exam Type",
                    "exam_code":               "Exam Code",
                    "exam_status":             "Status",
                    "score":                   "Composite Score",
                    "act_english":             "ACT English",
                    "act_math":                "ACT Math",
                    "act_reading":             "ACT Reading",
                    "act_science":             "ACT Science",
                    "sat_math":                "SAT Math",
                    "sat_rw":                  "SAT R&W",
                    "invalidity_reason":       "Why Invalid",
                }).drop_duplicates().sort_values(["Tutor","Student","Exam Date"])

                def highlight_exam_row(row):
                    status = str(row.get("Status",""))
                    if "Invalid" in status:
                        return ["background-color: #fff3cc"] * len(row)
                    if "No Exam" in status:
                        return ["background-color: #ffe5e5"] * len(row)
                    return [""] * len(row)

                st.markdown(
                    "✅ Green/white = valid exam &nbsp;&nbsp; "
                    "🟡 Yellow = invalid (section threshold not met) &nbsp;&nbsp; "
                    "🔴 Red = no exam data",
                    unsafe_allow_html=True
                )
                st.dataframe(
                    detail_display.style.apply(highlight_exam_row, axis=1),
                    use_container_width=True, hide_index=True
                )

                out_e = io.BytesIO()
                detail_display.to_excel(out_e, index=False)
                out_e.seek(0)
                st.download_button(
                    label="⬇️ Download Exam Detail",
                    data=out_e,
                    file_name="Exam_Detail_TeamCross.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        # ── TAB: TRENDS OVER TIME ─────────────────────
        with tab_trends:
            esnap = load_exams_snapshots()

            tutors_to_show_e = (
                [sel_tutor_e] if single_tutor_exam
                else sorted(team_exam_df["tutor_name"].dropna().unique().tolist())
            )

            trend_metric_e = st.selectbox(
                "Trend metric",
                ["no_exam_students","stale_exam_students","pct_eligible_with_exam"],
                format_func=lambda x: {
                    "no_exam_students":      "Students With No Completed Exam",
                    "stale_exam_students":   "Students With Stale Exam (>90d)",
                    "pct_eligible_with_exam":"% Eligible Students With a Completed Exam",
                }[x],
                key="exam_trend_metric"
            )

            if esnap.empty:
                st.caption("No historical snapshot data yet — trends will build automatically each week.")
            else:
                trend_color = {
                    "no_exam_students":      "#cc0000",
                    "stale_exam_students":   "#b35c00",
                    "pct_eligible_with_exam":"#006400",
                }[trend_metric_e]

                for tutor in tutors_to_show_e:
                    tsnap_e = esnap[esnap["tutor_name"] == tutor].sort_values("week_date")
                    if len(tsnap_e) < 2:
                        if single_tutor_exam:
                            st.caption(f"Only one week of data for {tutor} — trend will appear as more weeks accumulate.")
                        continue
                    fig_te = px.line(tsnap_e, x="week_date", y=trend_metric_e,
                                     markers=True,
                                     title=f"{tutor} — {trend_metric_e.replace('_',' ').title()} Week over Week",
                                     color_discrete_sequence=[trend_color])
                    fig_te.update_layout(title=dict(x=0.5, xanchor="center"),
                                         xaxis_title="Week", yaxis_title="",
                                         height=300, margin=dict(l=20, r=20, t=50, b=40))
                    fig_te.update_traces(line=dict(width=2.5))
                    st.plotly_chart(fig_te, use_container_width=True)

        # Sidebar refresh
        if st.sidebar.button("🔄 Refresh Exam Data", key="refresh_exams"):
            st.cache_data.clear()
            st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: GRADES SUMMARY
    # ─────────────────────────────────────────────

    if page == "Grades Summary":
        st.markdown('<div class="main-title">Grades Summary 📝</div>', unsafe_allow_html=True)

        st.info(
            "ℹ️ **About this page**\n\n"
            "Shows grade entry health for all active students (those met with in the last 30 days, "
            "with at least one session already completed). "
            "Tutors should update grades **at least quarterly** — students with no update in 90+ days are flagged. "
            "Students who have never had a grade entered are also surfaced.",
            icon=None
        )

        with st.spinner("Loading live grades data from Redshift..."):
            try:
                raw_grades_df, grades_fetched_at = load_grades_data()
            except Exception as e:
                st.error(f"Could not connect to Redshift: {e}")
                st.stop()

        if raw_grades_df.empty:
            st.info("No grades data returned from the database.")
            st.stop()

        st.caption(f"🕐 Data last updated: **{grades_fetched_at}**")
        st.sidebar.markdown(f"🕐 **Grades data last updated**  \n{grades_fetched_at}")

        # Filter to Team De Groot
        team_grades_df = raw_grades_df[raw_grades_df["team_name"] == "Team De Groot"].copy()

        if team_grades_df.empty:
            st.warning("No grades records found for Team De Groot.")
            st.stop()

        # ── Normalise date columns ────────────────────
        now = pd.Timestamp.now(tz="UTC")
        team_grades_df["updated_at"]      = pd.to_datetime(team_grades_df["updated_at"],      errors="coerce", utc=True)
        team_grades_df["first_session_day"] = pd.to_datetime(team_grades_df["first_session_day"], errors="coerce", utc=True)
        team_grades_df["last_session_day"]  = pd.to_datetime(team_grades_df["last_session_day"],  errors="coerce", utc=True)
        team_grades_df["days_since_update"] = (now - team_grades_df["updated_at"]).dt.days

        # ── Save weekly snapshot ──────────────────────
        grades_snap_df = save_grades_weekly_snapshot(team_grades_df)

        # ─────────────────────────────────────────────
        # TOP CONCERN FLAGS
        # ─────────────────────────────────────────────
        st.markdown("### 🚨 Top Tutors to Address")

        # Per-tutor aggregates (used for flags and charts)
        def build_tutor_summary(df):
            rows = []
            for tutor, tdf in df.groupby("tutor_name"):
                total_students   = tdf["student_id"].nunique()

                # Students with NO grade entry at all
                no_grade_ids = (
                    tdf.groupby("student_id")["score"]
                    .apply(lambda s: s.isna().all())
                )
                no_grade_students = int(no_grade_ids.sum())

                # % of subject rows that have a score
                pct_graded = (
                    tdf["score"].notna().sum() / len(tdf) * 100
                    if len(tdf) > 0 else 0
                )

                # Stale = most recent updated_at across ALL subjects for a student is >90 days.
                # Only applies to students who have at least one grade entered.
                has_any_grade = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_ids = has_any_grade[has_any_grade].index
                graded = tdf[tdf["student_id"].isin(graded_ids)]
                if not graded.empty:
                    latest_per_student = graded.groupby("student_id")["days_since_update"].min()
                    stale_students     = int((latest_per_student > 90).sum())
                    avg_days           = round(latest_per_student.mean(), 1)
                else:
                    stale_students = 0
                    avg_days       = None

                rows.append({
                    "tutor_name":           tutor,
                    "total_students":       total_students,
                    "students_no_grades":   no_grade_students,
                    "pct_subjects_graded":  round(pct_graded, 1),
                    "stale_grade_students": stale_students,
                    "avg_days_since_update": avg_days,
                })
            return pd.DataFrame(rows)

        tutor_summary = build_tutor_summary(team_grades_df)

        flag_c1, flag_c2, flag_c3 = st.columns(3)

        # Flag 1: Most students with NO grades at all
        with flag_c1:
            st.markdown("**Most Students With No Grades (Top 5)**")
            top_no_grades = (
                tutor_summary[tutor_summary["students_no_grades"] > 0]
                .sort_values("students_no_grades", ascending=False)
                .head(5)
            )
            if top_no_grades.empty:
                st.success("✅ All students have at least one grade entered.")
            else:
                medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]
                for rank, (_, row) in enumerate(top_no_grades.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"{int(row['students_no_grades'])} students</span>",
                        unsafe_allow_html=True
                    )

        # Flag 2: Most stale grades (>90 days since last update)
        with flag_c2:
            st.markdown("**Most Students With Stale Grades >90 Days (Top 5)**")
            top_stale = (
                tutor_summary[tutor_summary["stale_grade_students"] > 0]
                .sort_values("stale_grade_students", ascending=False)
                .head(5)
            )
            if top_stale.empty:
                st.success("✅ All graded students have been updated within 90 days.")
            else:
                for rank, (_, row) in enumerate(top_stale.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#b35c00; font-weight:bold'>"
                        f"{int(row['stale_grade_students'])} students</span>",
                        unsafe_allow_html=True
                    )

        # Flag 3: Lowest % of subjects graded
        with flag_c3:
            st.markdown("**Lowest % Subjects Graded (Top 5)**")
            top_low_pct = (
                tutor_summary.sort_values("pct_subjects_graded", ascending=True)
                .head(5)
            )
            if top_low_pct.empty:
                st.success("✅ No data to display.")
            else:
                for rank, (_, row) in enumerate(top_low_pct.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#555; font-weight:bold'>"
                        f"{row['pct_subjects_graded']:.1f}%</span>",
                        unsafe_allow_html=True
                    )

        st.divider()

        # ─────────────────────────────────────────────
        # TEAM-LEVEL SUMMARY METRICS
        # ─────────────────────────────────────────────
        st.markdown("### 📊 Team Overview")

        total_students_team    = team_grades_df["student_id"].nunique()
        no_grades_team         = int(
            team_grades_df.groupby("student_id")["score"]
            .apply(lambda s: s.isna().all()).sum()
        )
        pct_graded_team        = (
            team_grades_df["score"].notna().sum() / len(team_grades_df) * 100
            if len(team_grades_df) > 0 else 0
        )
        # Stale = most recent updated_at across ALL subjects for a student is >90 days.
        # Only for students who have at least one grade.
        has_any_grade_team = team_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_ids_team    = has_any_grade_team[has_any_grade_team].index
        graded_rows        = team_grades_df[team_grades_df["student_id"].isin(graded_ids_team)]
        stale_team         = 0
        if not graded_rows.empty:
            latest_per_student = graded_rows.groupby("student_id")["days_since_update"].min()
            stale_team         = int((latest_per_student > 90).sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Active Students",      total_students_team)
        m2.metric("Students — No Grades",        no_grades_team,
                  delta=f"{no_grades_team/total_students_team*100:.0f}% of roster" if total_students_team else None,
                  delta_color="inverse")
        m3.metric("% Subject Rows Graded",       f"{pct_graded_team:.1f}%")
        m4.metric("Students w/ Stale Grades",    stale_team,
                  delta="(>90 days since last update)", delta_color="inverse")

        st.divider()

        # ─────────────────────────────────────────────
        # FILTERS
        # ─────────────────────────────────────────────
        st.markdown("### 🔍 Filters")
        fc1, fc2 = st.columns(2)
        with fc1:
            tutor_opts_g = ["All Tutors"] + sorted(team_grades_df["tutor_name"].dropna().unique().tolist())
            sel_tutor_g  = st.selectbox("Tutor", tutor_opts_g, key="grades_tutor")
        with fc2:
            grade_filter_opts = ["All Students", "Missing Grades Only", "Stale Grades Only (>90 days)"]
            sel_grade_filter  = st.selectbox("Grade Status Filter", grade_filter_opts, key="grades_filter")

        view_grades_df = team_grades_df.copy()
        if sel_tutor_g != "All Tutors":
            view_grades_df = view_grades_df[view_grades_df["tutor_name"] == sel_tutor_g]

        if sel_grade_filter == "Missing Grades Only":
            # Keep rows for students who have ALL null scores
            missing_ids = (
                view_grades_df.groupby("student_id")["score"]
                .apply(lambda s: s.isna().all())
            )
            missing_ids = missing_ids[missing_ids].index
            view_grades_df = view_grades_df[view_grades_df["student_id"].isin(missing_ids)]
        elif sel_grade_filter == "Stale Grades Only (>90 days)":
            # Find students who have at least one grade AND whose most recent update (any subject) is >90 days ago
            has_any_grade_view = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded_ids_view    = has_any_grade_view[has_any_grade_view].index
            graded_view        = view_grades_df[view_grades_df["student_id"].isin(graded_ids_view)]
            if not graded_view.empty:
                latest_per = graded_view.groupby("student_id")["days_since_update"].min()
                stale_ids  = latest_per[latest_per > 90].index
                view_grades_df = view_grades_df[view_grades_df["student_id"].isin(stale_ids)]
            else:
                view_grades_df = pd.DataFrame(columns=view_grades_df.columns)

        single_tutor_grades = sel_tutor_g != "All Tutors"

        st.divider()

        # ─────────────────────────────────────────────
        # CHARTS & DETAIL TABS
        # ─────────────────────────────────────────────
        tab_team, tab_tutor, tab_detail = st.tabs([
            "📊 Team Charts",
            "👤 Tutor Breakdown",
            "📋 Student Detail"
        ])

        # ── TAB: Team Charts ──────────────────────────
        with tab_team:

            if not single_tutor_grades:

                # Chart 1: Students with no grades by tutor
                no_grades_chart = (
                    tutor_summary[tutor_summary["students_no_grades"] > 0]
                    .sort_values("students_no_grades", ascending=True)
                )
                if not no_grades_chart.empty:
                    n = len(no_grades_chart)
                    fig1 = px.bar(
                        no_grades_chart,
                        x="students_no_grades", y="tutor_name",
                        orientation="h",
                        color="students_no_grades",
                        color_continuous_scale=["#ffe0e0","#cc0000"],
                        text="students_no_grades",
                        title="Students With No Grades Entered — By Tutor",
                        height=max(350, n * 30)
                    )
                    fig1.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        showlegend=False, coloraxis_showscale=False,
                        xaxis_title="# Students", yaxis_title="",
                        yaxis=dict(autorange="reversed"),
                        margin=dict(l=160, r=20, t=50, b=40)
                    )
                    fig1.update_traces(textposition="outside")
                    st.plotly_chart(fig1, use_container_width=True)
                else:
                    st.success("✅ All students have at least one grade entered.")

                # Chart 2: % subjects graded by tutor
                pct_chart = tutor_summary.sort_values("pct_subjects_graded", ascending=True)
                n = len(pct_chart)
                fig2 = px.bar(
                    pct_chart,
                    x="pct_subjects_graded", y="tutor_name",
                    orientation="h",
                    color="pct_subjects_graded",
                    color_continuous_scale=["#cc0000","#ffdd99","#006400"],
                    text=pct_chart["pct_subjects_graded"].apply(lambda v: f"{v:.0f}%"),
                    title="% of Subject Rows With a Grade Entered — By Tutor",
                    height=max(350, n * 30)
                )
                fig2.update_layout(
                    title=dict(x=0.5, xanchor="center"),
                    showlegend=False, coloraxis_showscale=False,
                    xaxis_title="% Graded", yaxis_title="",
                    xaxis=dict(range=[0, 110]),
                    yaxis=dict(autorange="reversed"),
                    margin=dict(l=160, r=20, t=50, b=40)
                )
                fig2.update_traces(textposition="outside")
                st.plotly_chart(fig2, use_container_width=True)

                # Chart 3: Stale grades by tutor
                stale_chart = (
                    tutor_summary[tutor_summary["stale_grade_students"] > 0]
                    .sort_values("stale_grade_students", ascending=True)
                )
                if not stale_chart.empty:
                    n = len(stale_chart)
                    fig3 = px.bar(
                        stale_chart,
                        x="stale_grade_students", y="tutor_name",
                        orientation="h",
                        color="stale_grade_students",
                        color_continuous_scale=["#fff3cc","#b35c00"],
                        text="stale_grade_students",
                        title="Students With Grades Not Updated in 90+ Days — By Tutor",
                        height=max(350, n * 30)
                    )
                    fig3.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        showlegend=False, coloraxis_showscale=False,
                        xaxis_title="# Students", yaxis_title="",
                        yaxis=dict(autorange="reversed"),
                        margin=dict(l=160, r=20, t=50, b=40)
                    )
                    fig3.update_traces(textposition="outside")
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.success("✅ No stale grades on the team (all updated within 90 days).")

            else:
                # Single tutor selected — show their own summary bar charts
                sel_summary = tutor_summary[tutor_summary["tutor_name"] == sel_tutor_g]
                if not sel_summary.empty:
                    row = sel_summary.iloc[0]
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    sc1.metric("Total Students",       int(row["total_students"]))
                    sc2.metric("No Grades",            int(row["students_no_grades"]), delta_color="inverse")
                    sc3.metric("% Subjects Graded",    f"{row['pct_subjects_graded']:.1f}%")
                    sc4.metric("Stale Grade Students", int(row["stale_grade_students"]), delta_color="inverse")

                # Days-since-update: most recent update across ALL subjects per student.
                # Only show students who have at least one grade entered.
                has_grade = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_ids_tutor = has_grade[has_grade].index
                graded_tutor = view_grades_df[view_grades_df["student_id"].isin(graded_ids_tutor)].copy()
                if not graded_tutor.empty:
                    per_student_days = (
                        graded_tutor.groupby(["student_id","student_name"])["days_since_update"]
                        .min().reset_index()
                        .sort_values("days_since_update", ascending=True)
                    )
                    colors = per_student_days["days_since_update"].apply(
                        lambda d: "#cc0000" if d > 90 else ("#ffaa00" if d > 60 else "#2a7a2a")
                    )
                    fig_days = px.bar(
                        per_student_days,
                        x="days_since_update", y="student_name",
                        orientation="h",
                        text=per_student_days["days_since_update"].apply(lambda d: f"{d}d"),
                        title=f"{sel_tutor_g} — Days Since Last Grade Update (per student)",
                        height=max(300, len(per_student_days) * 30),
                        color="days_since_update",
                        color_continuous_scale=["#2a7a2a","#ffaa00","#cc0000"],
                        range_color=[0, max(per_student_days["days_since_update"].max(), 91)]
                    )
                    fig_days.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Days Since Last Grade Update",
                        yaxis_title="", showlegend=False, coloraxis_showscale=False,
                        margin=dict(l=160, r=20, t=50, b=40)
                    )
                    fig_days.add_vline(x=90, line_dash="dash", line_color="red",
                                       annotation_text="90-day threshold",
                                       annotation_position="top right")
                    fig_days.update_traces(textposition="outside")
                    st.plotly_chart(fig_days, use_container_width=True)

        # ── TAB: Tutor Breakdown (week-over-week trends) ──
        with tab_tutor:
            gsnap = load_grades_snapshots()

            if single_tutor_grades:
                tutors_to_show = [sel_tutor_g]
            else:
                tutors_to_show = sorted(team_grades_df["tutor_name"].dropna().unique().tolist())

            trend_metric = st.selectbox(
                "Trend metric",
                ["students_no_grades", "stale_grade_students", "pct_subjects_graded", "avg_days_since_update"],
                format_func=lambda x: {
                    "students_no_grades":     "Students With No Grades",
                    "stale_grade_students":   "Students With Stale Grades (>90d)",
                    "pct_subjects_graded":    "% Subjects Graded",
                    "avg_days_since_update":  "Avg Days Since Last Update"
                }[x],
                key="grades_trend_metric"
            )

            if gsnap.empty:
                st.caption("No historical snapshot data yet — trends will build automatically each week.")
            else:
                for tutor in tutors_to_show:
                    tsnap = gsnap[gsnap["tutor_name"] == tutor].sort_values("week_date")
                    if len(tsnap) < 2:
                        if single_tutor_grades:
                            st.caption(f"Only one week of data for {tutor} — trend will appear as more weeks accumulate.")
                        continue

                    color = {
                        "students_no_grades":    "#cc0000",
                        "stale_grade_students":  "#b35c00",
                        "pct_subjects_graded":   "#006400",
                        "avg_days_since_update": "#003f7f",
                    }[trend_metric]

                    fig_t = px.line(
                        tsnap, x="week_date", y=trend_metric,
                        markers=True,
                        title=f"{tutor} — {trend_metric.replace('_',' ').title()} Week over Week",
                        color_discrete_sequence=[color]
                    )
                    fig_t.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Week", yaxis_title="",
                        height=300, margin=dict(l=20, r=20, t=50, b=40)
                    )
                    fig_t.update_traces(line=dict(width=2.5))
                    st.plotly_chart(fig_t, use_container_width=True)

        # ── TAB: Student Detail ────────────────────────
        with tab_detail:
            if view_grades_df.empty:
                st.info("No records match the current filters.")
            else:
                # Build one row per student-subject with the prettiest columns
                detail_cols = [
                    "tutor_name", "student_name", "subject",
                    "score", "updated_at", "days_since_update",
                    "first_session_day", "last_session_day"
                ]
                detail_cols = [c for c in detail_cols if c in view_grades_df.columns]

                detail_display = view_grades_df[detail_cols].copy()

                # Format datetimes for readability
                for dc in ["updated_at", "first_session_day", "last_session_day"]:
                    if dc in detail_display.columns:
                        detail_display[dc] = detail_display[dc].dt.strftime("%Y-%m-%d")

                detail_display = detail_display.rename(columns={
                    "tutor_name":        "Tutor",
                    "student_name":      "Student",
                    "subject":           "Subject",
                    "score":             "Grade",
                    "updated_at":        "Grade Last Updated",
                    "days_since_update": "Days Since Update",
                    "first_session_day": "First Session",
                    "last_session_day":  "Last Session",
                }).sort_values(["Tutor","Student","Subject"])

                def highlight_grade_row(row):
                    days = row.get("Days Since Update")
                    grade = row.get("Grade")
                    if pd.isna(grade):
                        return ["background-color: #ffe5e5"] * len(row)
                    if pd.notna(days) and days > 90:
                        return ["background-color: #fff3cc"] * len(row)
                    return [""] * len(row)

                st.markdown(
                    "🔴 Red rows = no grade entered &nbsp;&nbsp; 🟡 Yellow rows = grade not updated in 90+ days",
                    unsafe_allow_html=True
                )

                st.dataframe(
                    detail_display.style.apply(highlight_grade_row, axis=1),
                    use_container_width=True,
                    hide_index=True
                )

                # Download
                output_g = io.BytesIO()
                detail_display.to_excel(output_g, index=False)
                output_g.seek(0)
                st.download_button(
                    label="⬇️ Download Grades Detail",
                    data=output_g,
                    file_name="Grades_Detail_TeamCross.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        # Sidebar refresh
        if st.sidebar.button("🔄 Refresh Grades Data", key="refresh_grades"):
            st.cache_data.clear()
            st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: ARCHIVABLE STUDENTS & UNSCHEDULED HOURS
    # ─────────────────────────────────────────────

    if page == "Archivable Students & Unscheduled Hours":
        st.markdown('<div class="main-title">Archivable Students & Unscheduled Hours 📦</div>', unsafe_allow_html=True)

        st.info(
            "ℹ️ **What is an Archivable Student?**\n\n"
            "An archivable student is a student currently appearing on a tutor's active dashboard "
            "who has **not had a session in the past 30 days** and has **no sessions scheduled in the future**. "
            "These students should be reviewed and archived so tutors' dashboards reflect only truly active students.",
            icon=None
        )

        with st.spinner("Loading live data from Redshift..."):
            try:
                raw_df, fetched_at = load_archivable_unscheduled()
            except Exception as e:
                st.error(f"Could not connect to Redshift: {e}")
                st.stop()

        if raw_df.empty:
            st.info("No data returned from the database.")
            st.stop()

        st.caption(f"🕐 Data last updated: **{fetched_at}**")

        st.sidebar.markdown("---")
        st.sidebar.markdown(f"🕐 **Data last updated**  \n{fetched_at}")

        raw_df["should_archive"] = raw_df["should_archive"].apply(
            lambda x: bool(x) if pd.notna(x) else False
        )

        full_team_df = raw_df[raw_df["team_name"] == "Team De Groot"].copy()

        if full_team_df.empty:
            st.warning("No records found for Team De Groot.")
            st.stop()

        snapshots_df = save_weekly_snapshot(full_team_df)

        def horizontal_bar(df, x_col, y_col, color_scale, title, x_label, height=None):
            n  = len(df)
            h  = height or max(350, n * 28)
            fig = px.bar(
                df, x=x_col, y=y_col,
                orientation="h",
                color=x_col,
                color_continuous_scale=color_scale,
                text=df[x_col].apply(lambda v: f"{v:.1f}" if isinstance(v, float) else str(v)),
                title=title,
                height=h
            )
            fig.update_layout(
                title=dict(x=0.5, xanchor="center"),
                showlegend=False, coloraxis_showscale=False,
                xaxis_title=x_label, yaxis_title="",
                yaxis=dict(autorange="reversed"),
                margin=dict(l=160, r=20, t=50, b=40)
            )
            fig.update_traces(textposition="outside")
            return fig

        def single_tutor_student_chart(tutor_df, value_col, color, title, x_label):
            plot_df = (
                tutor_df[tutor_df[value_col] > 0]
                .sort_values(value_col, ascending=True)
                [["student_name", value_col]]
                .copy()
            )
            if plot_df.empty:
                return None
            n   = len(plot_df)
            h   = max(300, n * 30)
            fig = px.bar(
                plot_df, x=value_col, y="student_name",
                orientation="h",
                text=plot_df[value_col].apply(lambda v: f"{v:.1f}" if isinstance(v, float) else str(v)),
                title=title,
                height=h,
                color_discrete_sequence=[color]
            )
            fig.update_layout(
                title=dict(x=0.5, xanchor="center"),
                xaxis_title=x_label, yaxis_title="",
                showlegend=False,
                margin=dict(l=160, r=20, t=50, b=40)
            )
            fig.update_traces(textposition="outside")
            return fig

        st.divider()

        st.markdown("### 🚨 Top Tutors to Address")
        flag_col1, flag_col2 = st.columns(2)

        with flag_col1:
            st.markdown("**Most Archivable Students (Top 5)**")
            top_archive = (
                full_team_df[full_team_df["should_archive"] == True]
                .groupby("tutor_name")["student_name"].nunique()
                .reset_index()
                .rename(columns={"tutor_name": "Tutor", "student_name": "Archivable Students"})
                .sort_values("Archivable Students", ascending=False)
                .head(5)
            )
            if top_archive.empty:
                st.success("✅ No archivable students on the team.")
            else:
                for i, row in top_archive.iterrows():
                    rank   = top_archive.index.get_loc(i) + 1
                    medal  = ["🥇","🥈","🥉","4️⃣","5️⃣"][rank - 1]
                    st.markdown(
                        f"{medal} **{row['Tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['Archivable Students'])} students</span>",
                        unsafe_allow_html=True
                    )

        with flag_col2:
            st.markdown("**Most Unscheduled Hours (Top 5)**")
            top_unsched = (
                full_team_df[full_team_df["unscheduled_hours"] > 0]
                .groupby("tutor_name")["unscheduled_hours"].sum()
                .reset_index()
                .rename(columns={"tutor_name": "Tutor", "unscheduled_hours": "Unscheduled Hours"})
                .sort_values("Unscheduled Hours", ascending=False)
                .head(5)
            )
            if top_unsched.empty:
                st.success("✅ No unscheduled hours on the team.")
            else:
                for i, row in top_unsched.iterrows():
                    rank  = top_unsched.index.get_loc(i) + 1
                    medal = ["🥇","🥈","🥉","4️⃣","5️⃣"][rank - 1]
                    st.markdown(
                        f"{medal} **{row['Tutor']}** — "
                        f"<span style='color:#003f7f; font-weight:bold'>{row['Unscheduled Hours']:.1f} hrs</span>",
                        unsafe_allow_html=True
                    )

        st.divider()

        st.markdown("### 🔍 Filters")
        f1, f2, f3, f4, f5 = st.columns(5)

        with f1:
            tutor_opts = ["All Tutors"] + sorted(full_team_df["tutor_name"].dropna().unique().tolist())
            sel_tutor  = st.selectbox("Tutor", tutor_opts)

        with f2:
            tier_opts  = ["All Tiers"] + sorted(full_team_df["tier"].dropna().unique().tolist())
            sel_tier   = st.selectbox("Tier", tier_opts)

        with f3:
            brand_opts = ["All Brands"] + sorted(full_team_df["brand"].dropna().unique().tolist())
            sel_brand  = st.selectbox("Brand", brand_opts)

        with f4:
            archive_opts = ["All Students", "Archivable Only", "Active Only"]
            sel_archive  = st.selectbox("Archivable Status", archive_opts)

        with f5:
            unsched_opts = ["All Students", "Has Unscheduled Hours Only"]
            sel_unsched  = st.selectbox("Unscheduled Hours", unsched_opts)

        view_df = full_team_df.copy()
        if sel_tutor  != "All Tutors":  view_df = view_df[view_df["tutor_name"] == sel_tutor]
        if sel_tier   != "All Tiers":   view_df = view_df[view_df["tier"]        == sel_tier]
        if sel_brand  != "All Brands":  view_df = view_df[view_df["brand"]       == sel_brand]
        if sel_archive == "Archivable Only": view_df = view_df[view_df["should_archive"] == True]
        if sel_archive == "Active Only":     view_df = view_df[view_df["should_archive"] == False]
        if sel_unsched == "Has Unscheduled Hours Only": view_df = view_df[view_df["unscheduled_hours"] > 0]

        single_tutor_selected = sel_tutor != "All Tutors"

        st.divider()

        total_students    = view_df["student_name"].nunique()
        to_archive        = view_df[view_df["should_archive"] == True]["student_name"].nunique()
        pct_archivable    = (to_archive / total_students * 100) if total_students > 0 else 0
        total_unscheduled = view_df["unscheduled_hours"].sum()
        total_remaining   = view_df["hours_remaining"].sum()

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Students",          total_students)
        c2.metric("Archivable Students",     to_archive)
        c3.metric("% Archivable",            f"{pct_archivable:.1f}%")
        c4.metric("Total Unscheduled Hours", f"{total_unscheduled:,.1f} hrs")
        c5.metric("Total Hours Remaining",   f"{total_remaining:,.1f} hrs")

        st.divider()

        tab1, tab2 = st.tabs(["📦 Archivable Students", "⏳ Unscheduled Hours"])

        with tab1:
            st.markdown(
                "**Archivable students** are flagged when their last session was more than 30 days ago "
                "and no future sessions are scheduled."
            )

            archive_df = view_df[view_df["should_archive"] == True].copy()

            if archive_df.empty:
                st.success("✅ No students flagged for archiving with the current filters.")
            else:
                if single_tutor_selected:
                    archive_df["days_since"] = (
                        pd.Timestamp.now() - pd.to_datetime(archive_df["last_session_day"])
                    ).dt.days
                    fig = single_tutor_student_chart(
                        archive_df.assign(days_since=archive_df["days_since"]),
                        value_col="days_since",
                        color="#cc0000",
                        title=f"{sel_tutor} — Days Since Last Session (Archivable Students)",
                        x_label="Days Since Last Session"
                    )
                    if fig:
                        st.plotly_chart(fig, use_container_width=True)

                    st.markdown("#### 📈 Trend: Archivable Students Over Time")
                    snap_df = load_snapshots()
                    if snap_df.empty or sel_tutor not in snap_df["tutor_name"].values:
                        st.caption("No historical data yet — trend will build automatically each week as the app runs.")
                    else:
                        tutor_snap = snap_df[snap_df["tutor_name"] == sel_tutor].sort_values("week_date")
                        if len(tutor_snap) < 2:
                            st.caption("Only one week of data so far — trend will appear once more weeks are recorded.")
                        else:
                            fig_trend = px.line(
                                tutor_snap, x="week_date", y="archivable_students",
                                markers=True,
                                title=f"{sel_tutor} — Archivable Students Week over Week",
                                labels={"week_date": "Week", "archivable_students": "# Archivable Students"},
                                color_discrete_sequence=["#cc0000"]
                            )
                            fig_trend.update_layout(
                                title=dict(x=0.5, xanchor="center"),
                                xaxis_title="Week", yaxis_title="# Students",
                                height=320, margin=dict(l=20, r=20, t=50, b=40)
                            )
                            fig_trend.update_traces(line=dict(width=2.5))
                            st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    archive_by_tutor = (
                        archive_df.groupby("tutor_name")["student_name"]
                        .nunique().reset_index()
                        .rename(columns={"tutor_name": "Tutor", "student_name": "Students to Archive"})
                        .sort_values("Students to Archive", ascending=False)
                    )
                    fig = horizontal_bar(
                        archive_by_tutor,
                        x_col="Students to Archive", y_col="Tutor",
                        color_scale=["#ffe0e0", "#cc0000"],
                        title="Archivable Students by Tutor",
                        x_label="# Students"
                    )
                    st.plotly_chart(fig, use_container_width=True)

                st.markdown("#### Student Detail")
                display_cols = ["tutor_name", "student_name", "brand", "tier",
                                "first_session_day", "last_session_day",
                                "hours_remaining", "unscheduled_hours"]
                display_cols = [c for c in display_cols if c in archive_df.columns]

                archive_display = archive_df[display_cols].copy().rename(columns={
                    "tutor_name":        "Tutor",
                    "student_name":      "Student",
                    "brand":             "Brand",
                    "tier":              "Tier",
                    "first_session_day": "First Session",
                    "last_session_day":  "Last Session",
                    "hours_remaining":   "Hours Remaining",
                    "unscheduled_hours": "Unscheduled Hours"
                })

                def highlight_archive(row):
                    return ["background-color: #ffe5e5"] * len(row)

                st.dataframe(
                    archive_display.style.apply(highlight_archive, axis=1),
                    use_container_width=True, hide_index=True
                )

                output = io.BytesIO()
                archive_display.to_excel(output, index=False)
                output.seek(0)
                st.download_button(
                    label="⬇️ Download Archivable Students",
                    data=output,
                    file_name="Archivable_Students_TeamCross.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        with tab2:
            st.markdown(
                "Students with hours purchased but **not yet scheduled**. "
                "Unscheduled hours = provisioned hours minus duration hours (scheduled + delivered)."
            )

            unsched_df = view_df[view_df["unscheduled_hours"] > 0].copy()
            unsched_df = unsched_df.sort_values("unscheduled_hours", ascending=False)

            if unsched_df.empty:
                st.success("✅ No unscheduled hours found with the current filters.")
            else:
                if single_tutor_selected:
                    fig = single_tutor_student_chart(
                        unsched_df,
                        value_col="unscheduled_hours",
                        color="#003f7f",
                        title=f"{sel_tutor} — Unscheduled Hours by Student",
                        x_label="Unscheduled Hours"
                    )
                    if fig:
                        st.plotly_chart(fig, use_container_width=True)

                    st.markdown("#### 📈 Trend: Unscheduled Hours Over Time")
                    snap_df = load_snapshots()
                    if snap_df.empty or sel_tutor not in snap_df["tutor_name"].values:
                        st.caption("No historical data yet — trend will build automatically each week as the app runs.")
                    else:
                        tutor_snap = snap_df[snap_df["tutor_name"] == sel_tutor].sort_values("week_date")
                        if len(tutor_snap) < 2:
                            st.caption("Only one week of data so far — trend will appear once more weeks are recorded.")
                        else:
                            fig_trend = px.line(
                                tutor_snap, x="week_date", y="unscheduled_hours",
                                markers=True,
                                title=f"{sel_tutor} — Unscheduled Hours Week over Week",
                                labels={"week_date": "Week", "unscheduled_hours": "Unscheduled Hours"},
                                color_discrete_sequence=["#003f7f"]
                            )
                            fig_trend.update_layout(
                                title=dict(x=0.5, xanchor="center"),
                                xaxis_title="Week", yaxis_title="Hours",
                                height=320, margin=dict(l=20, r=20, t=50, b=40)
                            )
                            fig_trend.update_traces(line=dict(width=2.5))
                            st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    unsched_by_tutor = (
                        unsched_df.groupby("tutor_name")["unscheduled_hours"]
                        .sum().reset_index()
                        .rename(columns={"tutor_name": "Tutor", "unscheduled_hours": "Unscheduled Hours"})
                        .sort_values("Unscheduled Hours", ascending=False)
                    )
                    fig = horizontal_bar(
                        unsched_by_tutor,
                        x_col="Unscheduled Hours", y_col="Tutor",
                        color_scale=["#ddeeff", "#003f7f"],
                        title="Total Unscheduled Hours by Tutor",
                        x_label="Hours"
                    )
                    st.plotly_chart(fig, use_container_width=True)

                st.markdown("#### Student Detail")
                display_cols = ["tutor_name", "student_name", "brand", "tier",
                                "first_session_day", "last_session_day",
                                "should_archive", "hours_remaining", "unscheduled_hours"]
                display_cols = [c for c in display_cols if c in unsched_df.columns]

                unsched_display = unsched_df[display_cols].copy().rename(columns={
                    "tutor_name":        "Tutor",
                    "student_name":      "Student",
                    "brand":             "Brand",
                    "tier":              "Tier",
                    "first_session_day": "First Session",
                    "last_session_day":  "Last Session",
                    "should_archive":    "Archivable?",
                    "hours_remaining":   "Hours Remaining",
                    "unscheduled_hours": "Unscheduled Hours"
                })

                st.dataframe(unsched_display, use_container_width=True, hide_index=True)

                output = io.BytesIO()
                unsched_display.to_excel(output, index=False)
                output.seek(0)
                st.download_button(
                    label="⬇️ Download Unscheduled Hours",
                    data=output,
                    file_name="Unscheduled_Hours_TeamCross.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

        if st.sidebar.button("🔄 Refresh Live Data"):
            st.cache_data.clear()
            st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: ANNUAL REVIEWS
    # ─────────────────────────────────────────────

    if page == "Annual Reviews":
        st.markdown('<div class="main-title">Annual Reviews 📋</div>', unsafe_allow_html=True)
        selected_annual_tutor = st.selectbox("Select a Tutor:", annelies_tutors)

        if selected_annual_tutor:
            tutor_review = annual_review_df[annual_review_df["tutor_name"] == selected_annual_tutor]
            tutor_review_repurchase = repurchase_df[repurchase_df["Tutor Name"] == selected_annual_tutor]
            tutor_review_monthly_metric = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tutor Name"] == selected_annual_tutor]

            if not tutor_review.empty:
                row = tutor_review.iloc[0]
                tutor_tier = row["tier"]

                row_repurchase = tutor_review_repurchase.iloc[0]
                tutor_tier_repurchase = row_repurchase["Current Tier"]
                tutor_deliverytarget = row_repurchase["Delivery Target"]

                row_monthly_metric = tutor_review_monthly_metric
                tutor_tier_monthly_metric = row_monthly_metric["Tier"].iloc[0]

                team_df = annual_review_df[annual_review_df["fl"] == "Annelies de Groot"]
                tier_df = annual_review_df[annual_review_df["tier"] == tutor_tier]

                team_repurchase_df = repurchase_df[repurchase_df["Team Name"] == "Team De Groot"]
                tier_repurchase_df = repurchase_df[repurchase_df["Current Tier"] == tutor_tier]
                tierdelivery_repurchase_df = repurchase_df[
                    (repurchase_df["Current Tier"] == tutor_tier) &
                    (repurchase_df["Delivery Target"] == tutor_deliverytarget)
                ]

                team_monthly_metric_df = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Faculty Leader"] == "Annelies de Groot"]
                tier_monthly_metric_df = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tier"] == tutor_tier_monthly_metric]

                metrics = {
                    "sessions_on_time": "Sessions On Time (%)",
                    "% Parents Updates Done on Time": "Percent of Parent Updates Completed on Time",
                    "prep_time": "Prep Time (%)",
                    "Repurchases Weighted": "Weighted Repurchase",
                    "average_nps": "Average NPS",
                    "% of Active Students with Progress Updates Completed in last 2 months": "Progress Update Average Percentage",
                    "current_sci": "Current SCI",
                    "availability_percent": "Percent to Availability (%)",
                    "delivery_percent": "Percent to Delivery (%)"
                }

                subject_df = load_subject_additions()

                for col, label in metrics.items():
                    if col == "availability_percent":
                        st.divider()
                        st.subheader("Subject Additions")
                        if "tutor_name" in subject_df.columns:
                            tutor_subjects = subject_df.loc[
                                subject_df["tutor_name"].str.strip().str.lower() == selected_annual_tutor.strip().lower(),
                                "subject"
                            ].dropna().tolist()
                        else:
                            st.error("Column 'tutor_name' not found in Subject Addition sheet.")
                            tutor_subjects = []

                        if len(tutor_subjects) == 0:
                            st.markdown("<p style='color: gray; font-style: italic; font-size: 1.1rem;'>None</p>", unsafe_allow_html=True)
                        else:
                            for subj in tutor_subjects:
                                st.markdown(
                                    f"""
                                    <div style='
                                        background-color: #f8f9fa;
                                        border-radius: 8px;
                                        padding: 10px 15px;
                                        margin: 6px 0;
                                        font-size: 1.1rem;
                                        font-weight: 500;
                                        color: #333;
                                        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                                    '>
                                    📘 {subj}
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )

                    if col in ["% Parents Updates Done on Time", "% of Active Students with Progress Updates Completed in last 2 months"]:
                        tutor_value_monthly_metric = np.nanmean(row_monthly_metric[col].values)
                    elif col in ["Repurchases Weighted"]:
                        tutor_value_repurchase = row_repurchase[col]
                    else:
                        tutor_value = row[col]

                    if col in ["sessions_on_time", "prep_time", "availability_percent", "delivery_percent",
                               "% Parents Updates Done on Time", "% of Active Students with Progress Updates Completed in last 2 months"]:
                        if col in ["% Parents Updates Done on Time", "% of Active Students with Progress Updates Completed in last 2 months"]:
                            tutor_value_display = f"{tutor_value_monthly_metric * 100:.0f}%"
                            tutor_value_plot = tutor_value_monthly_metric * 100
                            team_avg = team_monthly_metric_df[col].mean() * 100
                            tier_avg = tier_monthly_metric_df[col].mean() * 100
                        else:
                            tutor_value_display = f"{tutor_value * 100:.0f}%"
                            tutor_value_plot = tutor_value * 100
                            team_avg = team_df[col].mean() * 100
                            tier_avg = tier_df[col].mean() * 100
                    else:
                        if col in ["Repurchases Weighted"]:
                            tutor_value_display = f"{tutor_value_repurchase:.1f}"
                            tutor_value_plot = tutor_value_repurchase
                            team_avg = tierdelivery_repurchase_df[col].mean()
                            tier_avg = tier_repurchase_df[col].mean()
                        else:
                            tutor_value_display = f"{tutor_value:.1f}"
                            tutor_value_plot = tutor_value
                            team_avg = team_df[col].mean()
                            tier_avg = tier_df[col].mean()

                    st.markdown("<hr>", unsafe_allow_html=True)
                    st.markdown(f"<h3 style='text-align:center'>{label}</h3>", unsafe_allow_html=True)

                    if col == "Repurchases Weighted":
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor, "Tier/Delivery Target"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue", "lightgrey"]
                        ))
                        fig_team.update_layout(
                            title=dict(text="VS Tier/Delivery Target", x=0.5, xanchor='center', font=dict(size=16)),
                            yaxis_title="Value", xaxis_title="", height=300,
                            margin=dict(l=20, r=20, t=40, b=20)
                        )
                    else:
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor, "Team Avg"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue", "lightgrey"]
                        ))
                        fig_team.update_layout(
                            title=dict(text="VS Team", x=0.5, xanchor='center', font=dict(size=16)),
                            yaxis_title="Value", xaxis_title="", height=300,
                            margin=dict(l=20, r=20, t=40, b=20)
                        )

                    fig_tier = go.Figure(go.Bar(
                        x=[selected_annual_tutor, "Tier Avg"],
                        y=[tutor_value_plot, tier_avg],
                        marker_color=["blue", "lightgrey"]
                    ))
                    fig_tier.update_layout(
                        title=dict(text="VS Tier", x=0.5, xanchor='center', font=dict(size=16)),
                        yaxis_title="Value", xaxis_title="", height=300,
                        margin=dict(l=20, r=20, t=40, b=20)
                    )

                    col1, col2, col3 = st.columns([1, 1, 1])
                    with col1:
                        st.markdown(
                            f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_annual_tutor}<br>{tutor_value_display}</div>",
                            unsafe_allow_html=True
                        )
                    with col2:
                        st.plotly_chart(fig_team, use_container_width=True)
                    with col3:
                        st.plotly_chart(fig_tier, use_container_width=True)


    # ─────────────────────────────────────────────
    # PAGE: KPI TRENDS
    # ─────────────────────────────────────────────

    if page == "KPI Trends":
        st.markdown('<div class="main-title">📈 KPI Trends</div>', unsafe_allow_html=True)

        monthly_df = load_monthly_metric()
        annual_df  = load_annual_reviews()
        master_df  = load_master_tutor()

        selected_tutor = st.selectbox(
            "Select a Tutor:",
            annelies_tutors,
            index=annelies_tutors.index(st.session_state.pop("kpi_trends_tutor", None) or annelies_tutors[0])
                  if annelies_tutors else 0,
            key="kpi_trends_selectbox"
        )

        if selected_tutor:
            tutor_df   = monthly_df[monthly_df["Tutor Name"] == selected_tutor].copy()
            tutor_tier = annual_df.loc[annual_df["tutor_name"] == selected_tutor, "tier"].values
            tutor_tier = tutor_tier[0] if len(tutor_tier) > 0 else None

            import re
            def extract_end_date(range_str):
                if pd.isna(range_str):
                    return pd.NaT
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            tutor_df["Date Parsed"] = tutor_df["Date Range"].apply(extract_end_date)

            annelies_team = master_df[master_df["Faculty Leader"] == "Annelies de Groot"]["Full Name"].dropna()
            team_df = monthly_df[monthly_df["Tutor Name"].isin(annelies_team)].copy()
            team_df["Date Parsed"] = team_df["Date Range"].apply(extract_end_date)

            if tutor_tier:
                tier_tutors = annual_df[annual_df["tier"] == tutor_tier]["tutor_name"]
                tier_df = monthly_df[monthly_df["Tutor Name"].isin(tier_tutors)].copy()
                tier_df["Date Parsed"] = tier_df["Date Range"].apply(extract_end_date)
            else:
                tier_df = pd.DataFrame()

            metrics = {
                "% to Delivery Target": "% to Delivery Target",
                "% to Availability Target": "% to Availability Target",
                "% Sessions on Time": "% Sessions on Time",
                "% Parents Updates Done on Time": "% to Parent Updates Completed",
                "% of Active Students with Progress Updates Completed in last 2 months": "% Progress Updates Completed",
                "Weighted Repurchases": "Weighted Repurchases",
                "Ratio of PPW Events with Attached PPWs": "PPW Attachment Ratio"
            }

            percent_metrics = [
                "% to Delivery Target",
                "% to Availability Target",
                "% Sessions on Time",
                "% Parents Updates Done on Time",
                "% of Active Students with Progress Updates Completed in last 2 months"
            ]

            for metric, label in metrics.items():
                st.markdown("<hr>", unsafe_allow_html=True)
                st.markdown(f"<h3 style='text-align:center'>{label}</h3>", unsafe_allow_html=True)

                if metric in percent_metrics:
                    tutor_df[metric] = tutor_df[metric] * 100
                    team_df[metric]  = team_df[metric] * 100
                    if not tier_df.empty:
                        tier_df[metric] = tier_df[metric] * 100

                tutor_plot_df = tutor_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed").tail(6)
                team_plot_df  = team_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
                tier_plot_df  = tier_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed") if not tier_df.empty else pd.DataFrame()

                latest_value   = tutor_plot_df[metric].iloc[-1] if not tutor_plot_df.empty else None
                latest_display = f"{latest_value:.0f}%" if metric in percent_metrics else f"{latest_value:.2f}"

                if not team_plot_df.empty:
                    team_grouped = team_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    team_grouped = team_grouped.sort_values("Date Parsed").tail(6)
                    team_grouped = team_grouped.merge(
                        team_plot_df[["Date Parsed", "Date Range"]], on="Date Parsed", how="left"
                    ).drop_duplicates(subset=["Date Parsed"])

                    fig_team = px.line(team_grouped, x="Date Range", y=metric, title="VS Team", markers=True)
                    fig_team.add_scatter(
                        x=tutor_plot_df["Date Range"], y=tutor_plot_df[metric],
                        mode="lines+markers", name=selected_tutor, line=dict(width=3)
                    )
                    fig_team.update_layout(
                        title=dict(x=0.5, xanchor='center', font=dict(size=16)),
                        xaxis=dict(tickangle=30), yaxis_title=None, xaxis_title=None,
                        height=350, margin=dict(l=20, r=20, t=50, b=40)
                    )

                if not tier_plot_df.empty:
                    tier_grouped = tier_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    tier_grouped = tier_grouped.sort_values("Date Parsed").tail(6)
                    tier_grouped = tier_grouped.merge(
                        tier_plot_df[["Date Parsed", "Date Range"]], on="Date Parsed", how="left"
                    ).drop_duplicates(subset=["Date Parsed"])

                    fig_tier = px.line(tier_grouped, x="Date Range", y=metric, title="VS Tier", markers=True)
                    fig_tier.add_scatter(
                        x=tutor_plot_df["Date Range"], y=tutor_plot_df[metric],
                        mode="lines+markers", name=selected_tutor, line=dict(width=3)
                    )
                    fig_tier.update_layout(
                        title=dict(x=0.5, xanchor='center', font=dict(size=16)),
                        xaxis=dict(tickangle=30), yaxis_title=None, xaxis_title=None,
                        height=350, margin=dict(l=20, r=20, t=50, b=40)
                    )

                row1_col1, row1_col2 = st.columns([1, 3])
                with row1_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True
                    )
                with row1_col2:
                    if not team_plot_df.empty:
                        st.plotly_chart(fig_team, use_container_width=True)

                row2_col1, row2_col2 = st.columns([1, 3])
                with row2_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True
                    )
                with row2_col2:
                    if not tier_plot_df.empty:
                        st.plotly_chart(fig_tier, use_container_width=True)


    # ─────────────────────────────────────────────
    # PAGE: TUTOR PROFILE
    # ─────────────────────────────────────────────

    if page == "👤 Tutor Profile":
        st.markdown('<div class="main-title">👤 Tutor Profile</div>', unsafe_allow_html=True)

        # ── Tutor selector ────────────────────────
        profile_default = st.session_state.pop("profile_tutor", None)
        profile_index   = annelies_tutors.index(profile_default) \
                          if profile_default in annelies_tutors else 0
        profile_tutor   = st.selectbox(
            "Select a tutor:", annelies_tutors,
            index=profile_index, key="profile_page_selectbox"
        )

        if not profile_tutor:
            st.info("Select a tutor above to view their profile.")
            st.stop()

        st.markdown(f"## {profile_tutor}")
        st.markdown("---")

        # ── Load all data (reuse cached loaders) ──
        p_errors = []
        with st.spinner(f"Loading data for {profile_tutor}…"):
            try:
                raw_p_arch, _  = load_archivable_unscheduled()
                raw_p_arch["should_archive"] = raw_p_arch["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                p_arch = raw_p_arch[
                    (raw_p_arch["team_name"] == "Team De Groot") &
                    (raw_p_arch["tutor_name"] == profile_tutor)
                ].copy()
            except Exception as e:
                p_arch = pd.DataFrame()
                p_errors.append(f"Archivable: {e}")

            try:
                raw_p_grades, _ = load_grades_data()
                p_grades = raw_p_grades[
                    (raw_p_grades["team_name"] == "Team De Groot") &
                    (raw_p_grades["tutor_name"] == profile_tutor)
                ].copy()
                _now = pd.Timestamp.now(tz="UTC")
                p_grades["updated_at"] = pd.to_datetime(p_grades["updated_at"], errors="coerce", utc=True)
                p_grades["days_since_update"] = (_now - p_grades["updated_at"]).dt.days
            except Exception as e:
                p_grades = pd.DataFrame()
                p_errors.append(f"Grades: {e}")

            try:
                raw_p_exam, _ = load_exam_data()
                p_exam = raw_p_exam[
                    (raw_p_exam["team_name"] == "Team De Groot") &
                    (raw_p_exam["tutor_name"] == profile_tutor)
                ].copy()
                for dc in ["first_session_day", "most_recent_session", "exam_date"]:
                    p_exam[dc] = pd.to_datetime(p_exam[dc], errors="coerce", utc=True)
                for nc in ["score", "act_english", "act_math", "act_reading", "act_science",
                           "sat_math", "sat_rw", "test_prep_hours_delivered"]:
                    p_exam[nc] = pd.to_numeric(p_exam[nc], errors="coerce")
                SAT_TYPES_P = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT",
                               "Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                ACT_TYPES_P = {"ACT","Digital ACT"}
                p_exam["exam_family"] = p_exam["subject"].apply(
                    lambda x: "SAT/PSAT" if x in SAT_TYPES_P
                              else ("ACT" if x in ACT_TYPES_P else "Other"))
                def _sat_ok_p(r):
                    return (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                            pd.notna(r["sat_rw"])   and r["sat_rw"]   >= 300)
                def _act_ok_p(r):
                    return (pd.notna(r["act_english"]) and r["act_english"] >= 10 and
                            pd.notna(r["act_math"])    and r["act_math"]    >= 10 and
                            pd.notna(r["act_reading"]) and r["act_reading"] >= 10)
                p_exam["exam_valid_composite"] = p_exam.apply(
                    lambda r: _sat_ok_p(r) if r["exam_family"] == "SAT/PSAT"
                              else (_act_ok_p(r) if r["exam_family"] == "ACT" else False), axis=1)
                p_exam["is_official"] = p_exam["subject"].str.lower().str.contains("official", na=False)
            except Exception as e:
                p_exam = pd.DataFrame()
                p_errors.append(f"Exams: {e}")

            try:
                p_kpi_df = load_kpi_data()
                p_kpi    = p_kpi_df[p_kpi_df["Tutor Name"] == profile_tutor].copy() \
                           if not p_kpi_df.empty else pd.DataFrame()
            except Exception as e:
                p_kpi = pd.DataFrame()
                p_errors.append(f"KPI: {e}")

            try:
                p_monthly  = load_monthly_metric()
                p_monthly_t = p_monthly[p_monthly["Tutor Name"] == profile_tutor].copy() \
                              if not p_monthly.empty else pd.DataFrame()
                p_master   = load_master_tutor()
                p_annual   = load_annual_reviews()
            except Exception as e:
                p_monthly_t = pd.DataFrame()
                p_master    = pd.DataFrame()
                p_annual    = pd.DataFrame()
                p_errors.append(f"Monthly KPI: {e}")

        if p_errors:
            with st.expander("⚠️ Some data failed to load"):
                for err in p_errors:
                    st.warning(err)

        # ── Watchlist status & notes ──────────────
        st.markdown("### 👀 Watchlist Status")
        watched_list = load_watchlist()
        on_watchlist = profile_tutor in watched_list
        if on_watchlist:
            st.success(f"✅ **{profile_tutor}** is on your watch list.")
            bl_df = load_watchlist_baselines()
            notes_df = load_watchlist_notes()
            if not notes_df.empty and profile_tutor in notes_df["tutor_name"].values:
                note_row = notes_df[notes_df["tutor_name"] == profile_tutor].iloc[0]
                st.info(f"📌 **Note:** {note_row['note']}\n\n*Last updated: {note_row.get('updated_at','')}*")
            else:
                st.caption("No notes saved for this tutor.")
        else:
            st.warning(f"⚠️ **{profile_tutor}** is not currently on your watch list.")

        st.markdown("---")

        # ── Archivable status & unscheduled hours ──
        st.markdown("### 📦 Archivable Students & Unscheduled Hours")
        if p_arch.empty:
            st.info("No archivable/unscheduled data found for this tutor.")
        else:
            arch_students   = p_arch[p_arch["should_archive"] == True]
            n_arch          = len(arch_students)
            unsched_total   = round(p_arch["unscheduled_hours"].sum(), 1)
            total_students  = p_arch["student_name"].nunique()
            total_prov      = p_arch["hours_remaining"].sum() + p_arch["unscheduled_hours"].sum()
            pct_unsched     = round(p_arch["unscheduled_hours"].sum() / total_prov * 100, 1) \
                              if total_prov > 0 else 0.0

            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("Active Students",      total_students)
            pc2.metric("Archivable Students",  n_arch,
                       delta=f"{n_arch} flagged" if n_arch > 0 else None,
                       delta_color="inverse")
            pc3.metric("Unscheduled Hours",    f"{unsched_total:.1f}")
            pc4.metric("% Hours Unscheduled",  f"{pct_unsched:.1f}%")

            if not arch_students.empty:
                st.markdown("**Students flagged for archiving:**")
                show_cols = [c for c in ["student_name","brand","hours_remaining",
                                          "unscheduled_hours"] if c in arch_students.columns]
                st.dataframe(arch_students[show_cols].sort_values(
                    "unscheduled_hours", ascending=False), use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── Grades summary ────────────────────────
        st.markdown("### 📚 Grades Summary")
        if p_grades.empty:
            st.info("No grades data found for this tutor.")
        else:
            total_g_students = p_grades["student_id"].nunique()
            no_grade_ids     = p_grades.groupby("student_id")["score"].apply(lambda s: s.isna().all())
            n_no_grades      = int(no_grade_ids.sum())
            has_any          = p_grades.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded_ids       = has_any[has_any].index
            graded_g         = p_grades[p_grades["student_id"].isin(graded_ids)]
            if not graded_g.empty:
                latest_per = graded_g.groupby("student_id")["days_since_update"].min()
                n_stale    = int((latest_per > 90).sum())
                avg_days   = round(latest_per.mean(), 1)
            else:
                n_stale  = 0
                avg_days = None

            gc1, gc2, gc3 = st.columns(3)
            gc1.metric("Students with Grades",  total_g_students - n_no_grades)
            gc2.metric("No Grades Entered",     n_no_grades,
                       delta=f"{n_no_grades} missing" if n_no_grades > 0 else None,
                       delta_color="inverse")
            gc3.metric("Stale Grades (>90d)",   n_stale,
                       delta=f"avg {avg_days}d since update" if avg_days else None,
                       delta_color="inverse" if n_stale > 0 else "off")

            # Student-level breakdown
            if not p_grades.empty:
                st.markdown("**Student grade detail:**")
                g_summary = []
                for sid, sdf in p_grades.groupby("student_id"):
                    sname       = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else sid
                    n_subjects  = sdf["subject"].nunique() if "subject" in sdf.columns else 0
                    n_entered   = int(sdf["score"].notna().sum())
                    last_update = sdf["days_since_update"].min() if n_entered > 0 else None
                    stale_flag  = "⚠️" if last_update is not None and last_update > 90 else \
                                  ("✅" if last_update is not None else "❌")
                    g_summary.append({
                        "Student":         sname,
                        "Subjects":        n_subjects,
                        "Grades Entered":  n_entered,
                        "Days Since Update": int(last_update) if last_update is not None else "—",
                        "Status":          stale_flag,
                    })
                g_sum_df = pd.DataFrame(g_summary).sort_values("Days Since Update",
                    key=lambda x: pd.to_numeric(x, errors="coerce"), ascending=False)
                st.dataframe(g_sum_df, use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── Exam / test prep history ──────────────
        st.markdown("### 📝 Exam & Test Prep History")
        if p_exam.empty:
            st.info("No exam data found for this tutor.")
        else:
            p_now = pd.Timestamp.now(tz="UTC")
            ex_students = p_exam["student_id"].nunique() if "student_id" in p_exam.columns else 0
            valid_ids   = p_exam[p_exam["exam_valid_composite"] == True]["student_id"].unique()
            no_exam_ids = [sid for sid, sdf in p_exam.groupby("student_id")
                           if sdf["test_prep_hours_delivered"].iloc[0] >= 6
                           and sdf[sdf["exam_valid_composite"] == True].empty]

            stale_exam_count = 0
            for sid, sdf in p_exam.groupby("student_id"):
                completed = sdf[sdf["exam_valid_composite"] == True]
                if not completed.empty:
                    latest_ex = pd.to_datetime(completed["exam_date"], utc=True).max()
                    if pd.notna(latest_ex) and (p_now - latest_ex).days > 90:
                        stale_exam_count += 1

            total_hrs    = p_exam["test_prep_hours_delivered"].iloc[0] \
                           if not p_exam.empty else 0
            n_completed  = p_exam[p_exam["exam_valid_composite"] == True]["exam_id"].nunique() \
                           if "exam_id" in p_exam.columns else len(valid_ids)
            hrs_per_exam = round(total_hrs / n_completed, 1) if n_completed > 0 and pd.notna(total_hrs) else None

            ec1, ec2, ec3, ec4 = st.columns(4)
            ec1.metric("Test Prep Students",   ex_students)
            ec2.metric("No Completed Exam",    len(no_exam_ids),
                       delta=f"{len(no_exam_ids)} flagged" if no_exam_ids else None,
                       delta_color="inverse")
            ec3.metric("Stale Exams (>90d)",   stale_exam_count,
                       delta_color="inverse" if stale_exam_count > 0 else "off")
            ec4.metric("Avg Hrs / Exam",
                       f"{hrs_per_exam:.1f}" if hrs_per_exam else "N/A")

            # Per-student exam table
            ex_rows = []
            for sid, sdf in p_exam.groupby("student_id"):
                sname       = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else str(sid)
                hrs         = sdf["test_prep_hours_delivered"].iloc[0]
                valid       = sdf[sdf["exam_valid_composite"] == True]
                n_valid     = len(valid)
                latest_date = pd.to_datetime(valid["exam_date"], utc=True).max() \
                              if not valid.empty else None
                days_ago    = int((p_now - latest_date).days) if latest_date is not None and pd.notna(latest_date) else None
                best_score  = valid["score"].max() if not valid.empty else None
                status      = "✅ Current" if days_ago is not None and days_ago <= 90 \
                              else ("⚠️ Stale" if days_ago is not None else \
                              ("❌ None (6+ hrs)" if (pd.notna(hrs) and hrs >= 6) else "—"))
                ex_rows.append({
                    "Student":         sname,
                    "Hours Delivered": round(float(hrs), 1) if pd.notna(hrs) else "—",
                    "Valid Exams":     n_valid,
                    "Best Score":      int(best_score) if pd.notna(best_score) else "—",
                    "Days Since Exam": days_ago if days_ago is not None else "—",
                    "Status":          status,
                })
            ex_df = pd.DataFrame(ex_rows).sort_values(
                "Days Since Exam", key=lambda x: pd.to_numeric(x, errors="coerce"),
                ascending=False, na_position="last")
            st.dataframe(ex_df, use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── KPI trend charts ──────────────────────
        st.markdown("### 📈 KPI Trends")
        if p_monthly_t.empty:
            st.info("No KPI trend data found for this tutor.")
        else:
            import re as _re2
            def _parse_end_p(s):
                if pd.isna(s): return pd.NaT
                s2 = s.replace("-","to").replace("–","to").replace("—","to")
                parts = s2.split("to")
                if len(parts) < 2: return pd.NaT
                end = _re2.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", parts[-1].strip())
                return pd.to_datetime(end, errors="coerce", dayfirst=False)

            p_monthly_t["Date Parsed"] = p_monthly_t["Date Range"].apply(_parse_end_p)
            p_monthly_t = p_monthly_t.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")

            # Team average for context
            team_names_p  = p_master[p_master["Faculty Leader"] == "Annelies de Groot"]["Full Name"].dropna()
            team_monthly_p = p_monthly[p_monthly["Tutor Name"].isin(team_names_p)].copy() \
                             if not p_monthly.empty else pd.DataFrame()
            if not team_monthly_p.empty:
                team_monthly_p["Date Parsed"] = team_monthly_p["Date Range"].apply(_parse_end_p)

            kpi_metrics_p = {
                "% to Delivery Target":        ("Delivery %",       True),
                "% to Availability Target":    ("Availability %",   True),
                "% Sessions on Time":          ("Sessions On Time", True),
                "% Parents Updates Done on Time": ("Parent Updates %", True),
            }

            kpi_cols = st.columns(2)
            for ci, (metric, (label, is_pct)) in enumerate(kpi_metrics_p.items()):
                if metric not in p_monthly_t.columns:
                    continue
                plot_t = p_monthly_t[["Date Range","Date Parsed", metric]].dropna().tail(8).copy()
                if is_pct:
                    plot_t[metric] = plot_t[metric] * 100

                fig_p = px.line(plot_t, x="Date Range", y=metric,
                                title=label, markers=True,
                                labels={metric: "%", "Date Range": ""})

                if not team_monthly_p.empty and metric in team_monthly_p.columns:
                    team_plot_p = team_monthly_p.dropna(subset=["Date Parsed"]).copy()
                    if is_pct:
                        team_plot_p[metric] = team_plot_p[metric] * 100
                    tg = team_plot_p.groupby("Date Parsed")[metric].mean().reset_index()
                    tg = tg.merge(team_plot_p[["Date Parsed","Date Range"]],
                                  on="Date Parsed", how="left").drop_duplicates("Date Parsed")
                    tg = tg.sort_values("Date Parsed").tail(8)
                    fig_p.add_scatter(x=tg["Date Range"], y=tg[metric],
                                      mode="lines+markers", name="Team Avg",
                                      line=dict(dash="dash", color="gray"))

                fig_p.add_hline(y=100, line_dash="dot", line_color="#aaa")
                fig_p.update_layout(
                    height=280, margin=dict(l=10,r=10,t=40,b=30),
                    xaxis=dict(tickangle=30), yaxis_title=None, xaxis_title=None,
                    legend=dict(orientation="h", y=-0.3),
                    title=dict(x=0.5, xanchor="center")
                )
                with kpi_cols[ci % 2]:
                    st.plotly_chart(fig_p, use_container_width=True,
                                    key=f"profile_kpi_{profile_tutor}_{ci}")


    # ─────────────────────────────────────────────
    # PAGE: CONCERNS
    # ─────────────────────────────────────────────

    if page == "Concerns":
        st.markdown('<div class="main-title">Tutor Concerns 📌</div>', unsafe_allow_html=True)

        concerns_df = load_tutor_concerns()
        fl_df = concerns_df[concerns_df["Faculty Leader Name"] == faculty_leader_name]

        if fl_df.empty:
            st.info("No concern data available for your team.")
        else:
            import re
            def extract_end_date(range_str):
                if pd.isna(range_str):
                    return pd.NaT
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            fl_df["Date"] = fl_df["Date"].apply(extract_end_date)
            latest_date = fl_df["Date"].max()
            latest_df   = fl_df[fl_df["Date"] == latest_date]

            st.subheader(f"Team Overview (Latest Date: {latest_date.date()})")

            concern_counts = latest_df.groupby("Concern Group")["Tutor Name"].nunique().sort_index(ascending=False)
            st.markdown("**Number of Tutors in Each Concern Group**")
            st.bar_chart(concern_counts)

            for group in sorted(latest_df["Concern Group"].unique(), reverse=True):
                st.markdown(f"### Concern Group {group}")
                tutors_in_group = latest_df[latest_df["Concern Group"] == group]["Tutor Name"].tolist()
                st.write(", ".join(tutors_in_group))

            st.download_button(
                label="Download Latest Tutor Concerns",
                data=latest_df.to_csv(index=False),
                file_name=f"Tutor_Concerns_{faculty_leader_name}_{latest_date.date()}.csv",
                mime="text/csv"
            )

            st.markdown("---")

            tutor_names    = fl_df["Tutor Name"].dropna().unique().tolist()
            selected_tutor = st.selectbox("Select a Tutor", tutor_names)

            if selected_tutor:
                tutor_df = fl_df[fl_df["Tutor Name"] == selected_tutor].sort_values("Date")

                fig = px.line(
                    tutor_df, x="Date", y="Concern Group", markers=True,
                    title=f"{selected_tutor} Concern Score Over Time"
                )
                fig.update_yaxes(range=[1, 5], dtick=1, title="Concern Group", autorange=False)
                st.plotly_chart(fig, use_container_width=True)

                st.subheader(f"{selected_tutor} Details")
                st.dataframe(tutor_df[["Date", "Concern Group", "Reasons"]])

                st.download_button(
                    label=f"Download {selected_tutor} Concerns",
                    data=tutor_df.to_csv(index=False),
                    file_name=f"{selected_tutor}_Concerns.csv",
                    mime="text/csv"
                )


    # ─────────────────────────────────────────────
    # PAGE: KPI TABLE
    # ─────────────────────────────────────────────

    if page == "KPI Table":

        df = load_kpi_data()
        df["Date Range Parsed"] = pd.to_datetime(df["Date Range"].str.split(" - ").str[0], errors="coerce")
        latest_range_parsed = df["Date Range Parsed"].max()
        latest_range = df.loc[df["Date Range Parsed"] == latest_range_parsed, "Date Range"].iloc[0]
        leader_name  = "Annelies de Groot"
        team_df = df[(df["Date Range"] == latest_range) & (df["Faculty Leader"] == leader_name)].copy()

        metrics = [
            "% to Delivery Target",
            "% to Availability Target",
            "% Sessions on Time",
            "% Parents Updates Done on Time",
            "% of Active Students with Progress Updates Completed in last 2 months"
        ]

        for m in metrics:
            team_df[m] = team_df[m] * 100

        st.title("Team KPI Overview")
        st.caption(f"Faculty Leader: {leader_name} | Latest Date Range: {latest_range}")
        st.divider()
        st.divider()

        st.subheader("Team Summary KPIs")
        n_metrics    = len(metrics)
        n_cols       = 3
        rows_needed  = (n_metrics + n_cols - 1) // n_cols
        for r in range(rows_needed):
            cols = st.columns(n_cols)
            for i, col in enumerate(cols):
                idx = r * n_cols + i
                if idx < n_metrics:
                    metric = metrics[idx]
                    avg    = team_df[metric].mean(skipna=True)
                    color  = "🟢" if avg >= 90 else ("🟡" if avg >= 75 else "🔴")
                    col.metric(label=f"{color} {metric}", value=f"{avg:.1f}%")

        st.divider()
        st.divider()
        st.subheader("📊 Team KPI Changes from Previous Period")

        df["Date Range Parsed"] = pd.to_datetime(df["Date Range"].str.split(" - ").str[0], errors="coerce")
        date_ranges_sorted = df.sort_values("Date Range Parsed")["Date Range"].dropna().unique().tolist()

        if len(date_ranges_sorted) < 2:
            st.info("Not enough time periods available to calculate changes.")
        else:
            latest_range = date_ranges_sorted[-1]
            prev_range   = date_ranges_sorted[-2]

            latest_team = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == latest_range)]
            prev_team   = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == prev_range)]

            latest_avg = latest_team[metrics].mean()
            prev_avg   = prev_team[metrics].mean()

            change_df = pd.DataFrame({
                "Metric": metrics,
                f"{prev_range} Avg":   prev_avg.values,
                f"{latest_range} Avg": latest_avg.values,
                "Change (pp)": (latest_avg - prev_avg).values
            })

            for c in [f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]:
                change_df[c] = change_df[c] * 100

            def format_change(val):
                if pd.isna(val):
                    return ""
                arrow = "⬆️" if val > 0 else ("⬇️" if val < 0 else "➡️")
                return f"{arrow} {val:+.1f} pp"

            change_df["Change Display"] = change_df["Change (pp)"].apply(format_change)

            def style_change(val):
                color = "lightgreen" if val > 0 else ("lightcoral" if val < 0 else "white")
                return f"background-color: {color}; font-weight: bold; text-align: center"

            styled_df = change_df[["Metric", f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]].copy()
            styled_df_display = styled_df.style.format({
                f"{prev_range} Avg":   "{:.1f}%",
                f"{latest_range} Avg": "{:.1f}%",
                "Change (pp)":         "{:+.1f} pp"
            }).applymap(style_change, subset=["Change (pp)"])
            st.write(styled_df_display)

            max_abs_change = max(abs(change_df["Change (pp)"].max()), abs(change_df["Change (pp)"].min()))

            col1, col2, col3 = st.columns([1, 12, 1])
            with col2:
                fig_change = px.bar(
                    change_df,
                    x="Metric", y="Change (pp)",
                    color="Change (pp)",
                    color_continuous_scale=["red", "white", "green"],
                    text=change_df["Change (pp)"].apply(lambda x: f"{x:+.1f} pp"),
                    title=f"Change in Team Averages: {prev_range} → {latest_range}",
                    height=600
                )
                fig_change.update_layout(
                    title_x=0.20, xaxis_title="",
                    yaxis_title="Change (percentage points)",
                    margin=dict(l=20, r=20, t=60, b=40),
                    coloraxis_colorbar=dict(title="Change"),
                )
                fig_change.update_coloraxes(cmin=-max_abs_change, cmax=max_abs_change)
                st.plotly_chart(fig_change, use_container_width=True)

        st.divider()
        st.divider()
        st.subheader("Team Metrics Comparison vs Other Teams")

        tier_options  = ["All"] + sorted(df["Tier"].dropna().unique())
        selected_tier = st.selectbox("Filter by Tier (optional):", tier_options, index=0)

        if selected_tier != "All":
            df_filtered  = df[(df["Tier"] == selected_tier) & (df["Date Range"] == latest_range)]
            title_prefix = f"{selected_tier} Tier Team Comparison"
        else:
            df_filtered  = df[df["Date Range"] == latest_range]
            title_prefix = "Team Comparison"

        leader_group = df_filtered.groupby("Faculty Leader")[metrics].mean()

        for metric in metrics:
            st.markdown(f"### {metric}")
            plot_df = leader_group.reset_index().sort_values(by=metric, ascending=False)
            plot_df[metric + "_pct"] = plot_df[metric] * 100
            color_map = {fl: ("blue" if fl == leader_name else "lightgray") for fl in plot_df["Faculty Leader"]}

            fig = px.bar(
                plot_df, x="Faculty Leader", y=metric + "_pct",
                color="Faculty Leader", color_discrete_map=color_map,
                text=plot_df[metric + "_pct"].apply(lambda x: f"{x:.1f}%"),
                labels={metric + "_pct": "Percent"}, height=400
            )
            y_max = 130 if metric == "% to Availability Target" else 100
            fig.update_layout(
                title=dict(text=f"{title_prefix}: {metric}", x=0.5, xanchor="center"),
                showlegend=False, margin=dict(l=20, r=20, t=50, b=40),
                yaxis=dict(range=[0, y_max], tickformat=".0f%")
            )
            col1, col2, col3 = st.columns([1, 4, 1])
            with col2:
                st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.divider()
        st.subheader("Team KPI Table")

        dashboard_df = load_dashboard_metrics()

        if dashboard_df.empty:
            st.warning("Dashboard metrics file not found or empty.")
        else:
            team_dashboard_df = dashboard_df[dashboard_df["Faculty Leader Name"] == leader_name].copy()

            kpi_thresholds = {
                "% to Delivery Target":    (0.70, 0.80),
                "% to Availability Target": (0.80, 1.00),
                "Prep Time %":             (0.20, 0.10),
                "% Parents Updates Done on Time": (0.75, 0.90),
                "% Sessions on Time":      (0.80, 0.95),
                "% of Active Students with Progress Updates Completed": (0.50, 0.80)
            }

            def highlight_kpi(val, metric):
                if pd.isna(val):
                    return ""
                low, high = kpi_thresholds.get(metric, (None, None))
                if low is None:
                    return ""
                if metric == "Prep Time %":
                    if val < high:  return "background-color: lightgreen"
                    elif val > low: return "background-color: lightcoral"
                else:
                    if val > high:  return "background-color: lightgreen"
                    elif val < low: return "background-color: lightcoral"
                return ""

            styled_df = team_dashboard_df.style
            for metric in kpi_thresholds.keys():
                if metric in team_dashboard_df.columns:
                    styled_df = styled_df.applymap(
                        lambda v, m=metric: highlight_kpi(v, m), subset=[metric]
                    )

            styled_df = styled_df.format({
                col: "{:.2f}" if "%" not in col else "{:.1%}"
                for col in team_dashboard_df.select_dtypes(include=['float', 'int']).columns
            })
            styled_df = styled_df.set_table_styles([
                {"selector": "th", "props": [("text-align", "center"), ("white-space", "normal"), ("word-wrap", "break-word")]},
                {"selector": "td", "props": [("text-align", "center")]}
            ])
            st.write(styled_df)

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                team_dashboard_df.to_excel(writer, index=False, sheet_name="Team KPI Leaderboard")
                workbook  = writer.book
                worksheet = writer.sheets["Team KPI Leaderboard"]

                fmt_center     = workbook.add_format({'align': 'center'})
                fmt_percentage = workbook.add_format({'num_format': '0.00%', 'align': 'center'})
                fmt_decimal    = workbook.add_format({'num_format': '0.00',  'align': 'center'})
                fmt_text_wrap  = workbook.add_format({'text_wrap': True,     'align': 'center'})

                for col_num, col_name in enumerate(team_dashboard_df.columns):
                    if col_name in kpi_thresholds:
                        worksheet.set_column(col_num, col_num, 15, fmt_percentage)
                    elif pd.api.types.is_numeric_dtype(team_dashboard_df[col_name]):
                        worksheet.set_column(col_num, col_num, 15, fmt_decimal)
                    else:
                        worksheet.set_column(col_num, col_num, 20, fmt_text_wrap)

                fmt_green = '#90EE90'
                fmt_red   = '#F08080'

                for col_num, col_name in enumerate(team_dashboard_df.columns):
                    if col_name in kpi_thresholds:
                        low, high    = kpi_thresholds[col_name]
                        col_letter   = chr(65 + col_num)
                        if col_name != "Prep Time %":
                            worksheet.conditional_format(f"{col_letter}2:{col_letter}{len(team_dashboard_df)+1}",
                                {'type': 'cell', 'criteria': '>', 'value': high,
                                 'format': workbook.add_format({'bg_color': fmt_green, 'align': 'center'})})
                            worksheet.conditional_format(f"{col_letter}2:{col_letter}{len(team_dashboard_df)+1}",
                                {'type': 'cell', 'criteria': '<', 'value': low,
                                 'format': workbook.add_format({'bg_color': fmt_red, 'align': 'center'})})
                        else:
                            worksheet.conditional_format(f"{col_letter}2:{col_letter}{len(team_dashboard_df)+1}",
                                {'type': 'cell', 'criteria': '<', 'value': high,
                                 'format': workbook.add_format({'bg_color': fmt_green, 'align': 'center'})})
                            worksheet.conditional_format(f"{col_letter}2:{col_letter}{len(team_dashboard_df)+1}",
                                {'type': 'cell', 'criteria': '>', 'value': low,
                                 'format': workbook.add_format({'bg_color': fmt_red, 'align': 'center'})})

            output.seek(0)
            st.download_button(
                label="Download Team KPI Data",
                data=output,
                file_name=f"{leader_name.replace(' ', '_')}_Dashboard_Metrics.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

        st.subheader("Progress Update Emails")
        progress_df = load_progressupdate_metrics()

        if progress_df.empty:
            st.warning("Progress Update Emails sheet not found or empty.")
        else:
            leader_to_team = {
                "Annelies de Groot":           "Team De Groot",
                "Kristin Haase-Alvey": "Team Haase-Alvey",
                "Ian Plamondon":       "Team Plamondon",
                "Geoff St. Marie":     "Team St. Marie",
                "Annelies de Groot":   "Team De Groot"
            }
            team_name = leader_to_team.get(leader_name)
            if not team_name:
                st.warning(f"No team mapping found for {leader_name}.")
                team_progress_df = pd.DataFrame()
            else:
                team_progress_df = progress_df[progress_df["team"] == team_name].copy()

            if not team_progress_df.empty:
                st.dataframe(team_progress_df, use_container_width=True)

                output = io.BytesIO()
                with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                    team_progress_df.to_excel(writer, index=False, sheet_name="Progress Update Emails")
                    workbook  = writer.book
                    worksheet = writer.sheets["Progress Update Emails"]

                    fmt_percentage = workbook.add_format({'num_format': '0.00%', 'align': 'center'})
                    fmt_decimal    = workbook.add_format({'num_format': '0.00',  'align': 'center'})
                    fmt_text_wrap  = workbook.add_format({'text_wrap': True,     'align': 'center'})

                    for col_num, col_name in enumerate(team_progress_df.columns):
                        if "%" in col_name:
                            worksheet.set_column(col_num, col_num, 15, fmt_percentage)
                        elif pd.api.types.is_numeric_dtype(team_progress_df[col_name]):
                            worksheet.set_column(col_num, col_num, 15, fmt_decimal)
                        else:
                            worksheet.set_column(col_num, col_num, 20, fmt_text_wrap)

                output.seek(0)
                st.download_button(
                    label="Download Progress Update Emails",
                    data=output,
                    file_name=f"{leader_name.replace(' ', '_')}_Progress_Update_Emails.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.info(f"No Progress Update Emails found for {leader_name}.")