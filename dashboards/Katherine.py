import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import os
import io
import psycopg2
import mysql.connector

import requests as _requests
import base64 as _base64

# ─────────────────────────────────────────────
# GITHUB PERSISTENT STORAGE HELPERS
# ─────────────────────────────────────────────

def gh_read(filename: str) -> pd.DataFrame:
    """Read a persistent CSV from GitHub. Returns empty DataFrame if not found."""
    try:
        repo  = st.secrets["github"]["repo"]
        token = st.secrets["github"]["token"]
        path  = f"data/persistent/{filename}"
        ts    = int(pd.Timestamp.now().timestamp())
        url   = f"https://raw.githubusercontent.com/{repo}/main/{path}?cb={ts}"
        resp  = _requests.get(url, headers={"Authorization": f"token {token}"}, timeout=15)
        if resp.status_code == 404:
            return pd.DataFrame()
        resp.raise_for_status()
        return pd.read_csv(io.StringIO(resp.text))
    except Exception as e:
        st.warning(f"⚠️ Could not read {filename} from GitHub: {e}")
        return pd.DataFrame()


def gh_write(filename: str, df: pd.DataFrame) -> bool:
    """Write a persistent CSV to GitHub. Returns True on success."""
    try:
        repo  = st.secrets["github"]["repo"]
        token = st.secrets["github"]["token"]
        path  = f"data/persistent/{filename}"
        api   = f"https://api.github.com/repos/{repo}/contents/{path}"
        heads = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        encoded   = _base64.b64encode(csv_bytes).decode("utf-8")
        # Get existing SHA if file exists
        existing_sha = None
        r = _requests.get(api, headers=heads, timeout=15)
        if r.status_code == 200:
            existing_sha = r.json().get("sha")
        payload = {
            "message": f"persist: {filename} {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')} UTC",
            "content": encoded,
        }
        if existing_sha:
            payload["sha"] = existing_sha
        r2 = _requests.put(api, headers=heads, json=payload, timeout=30)
        r2.raise_for_status()
        return True
    except Exception as e:
        st.warning(f"⚠️ Could not save {filename} to GitHub: {e}")
        return False



# ─────────────────────────────────────────────
# REDSHIFT CONNECTION
# ─────────────────────────────────────────────

def get_redshift_connection():
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
    import socket
    creds = st.secrets["rp_db"]
    host  = str(creds["host"])
    port  = int(creds.get("port", 3306))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    result = sock.connect_ex((host, port))
    sock.close()
    if result != 0:
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
        host=host, port=port,
        user=str(creds["user"]), password=str(creds["password"]),
        connection_timeout=30, charset="utf8mb4",
        auth_plugin="mysql_native_password",
    )

# ─────────────────────────────────────────────
# DATA LOADERS
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600)
def load_brand_permissions():
    """Load tutor brand permissions from GitHub CSV."""
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = "data/brand_permissions.csv"
        ts   = int(pd.Timestamp.now().timestamp())
        url  = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?cb={ts}"
        resp = _requests.get(url, headers={"Authorization": f"token {github_token}"}, timeout=15)
        if resp.status_code == 404:
            return pd.DataFrame()
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if "fetched_at" in df.columns:
            df = df.drop(columns=["fetched_at"])
        return df
    except Exception as e:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_archivable_unscheduled():
    conn = get_redshift_connection()
    query = """
        with cte_courses as (
        select dw.courses.id as course_id,
        dw.students.id as student_id,
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
        where 1=1 and dw.courses.brand_id in (2,41,42,43)
        group by 1,2,3,4,5,6,7)
        select dw.tutoring_histories.tutor_id as tutor_id,
        tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
        dw.tiers.name as tier, dw.teams.name as team_name,
        cte_courses.course_id as course_id, cte_courses.student_id,
        cte_courses.brand, cte_courses.student_name,
        min(dw.sessions.starts_at) as first_session_day,
        max(dw.sessions.starts_at) as last_session_day,
        max(dw.sessions.starts_at) < (getdate() -30) as should_archive,
        cte_courses.provisioned_hours - cte_courses.delivered_hours as hours_remaining,
        case when cte_courses.brand = 'Academics' and (cte_courses.provisioned_hours - cte_courses.duration_hours)<0
             then 0 else cte_courses.provisioned_hours - cte_courses.duration_hours end as unscheduled_hours
        from dw.tutoring_histories
        JOIN dw.employees ON dw.employees.id = dw.tutoring_histories.tutor_id
        join dw.tiers on dw.employees.tier_id = dw.tiers.id
        JOIN dw.users tutor_users ON tutor_users.id = dw.employees.user_id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        JOIN dw.enrollments ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
        join dw.sessions on (dw.sessions.course_id = dw.enrollments.course_id)
             and (dw.sessions.supervisor_id = dw.employees.id)
        join cte_courses on dw.enrollments.course_id = cte_courses.course_id
        where 1=1 and dw.tutoring_histories.active = true
        AND dw.employees.end_date IS NULL AND dw.enrollments.unenrolled_at IS NULL
        AND dw.team_members.member_type = 'Employee'
        group by 1,2,3,4,5,6,7,8,12,13 order by unscheduled_hours
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/archivable_unscheduled.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached archivable data from {cached_at}.")
            return df, cached_at
        st.error("Cache mode ON but no cached archivable data found.")
        return pd.DataFrame(), "unavailable"
    try:
        df = pd.read_sql(query, conn)
        conn.close()
        fetched_at = pd.Timestamp.now().strftime("%B %d, %Y at %I:%M %p")
        return df, fetched_at
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/archivable_unscheduled.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached archivable/unscheduled data from {cached_at}.")
            return df, cached_at
        st.error(f"Archivable data unavailable: {e}")
        return pd.DataFrame(), "unavailable"


@st.cache_data(ttl=3600)
def load_grades_data():
    conn = get_redshift_connection()
    query = """
        WITH cte_grades AS (
            SELECT orbit_stitch.study_areas.student_id,
            dw.subjects.name AS subject, sas.score, sas.updated_at
            FROM orbit_stitch.study_areas
            LEFT JOIN dw.subjects ON orbit_stitch.study_areas.subject_id = dw.subjects.id
            LEFT JOIN orbit_stitch.study_area_snapshots sas ON orbit_stitch.study_areas.id = sas.study_area_id
            WHERE 1=1
            AND dw.subjects.category_id IN (1,2,3,4,5,8,9,10,11)
            AND cast(dw.subjects.high_grade AS int) > 8
            AND orbit_stitch.study_areas.archived_at IS NULL
            AND orbit_stitch.study_areas._sdc_deleted_at IS NULL
        ),
        cte_last_30_days_brands AS (
            SELECT dw.enrollments.enrollee_id AS student_id,
            dw.sessions.supervisor_id AS tutor_id,
            CASE WHEN dw.courses.brand_id = 2 THEN COUNT(DISTINCT dw.sessions.id) END AS private_tutoring_sessions,
            CASE WHEN dw.courses.brand_id = 42 THEN COUNT(DISTINCT dw.sessions.id) END AS buc_sessions,
            CASE WHEN dw.courses.brand_id = 43 THEN COUNT(DISTINCT dw.sessions.id) END AS trial_sessions,
            CASE WHEN dw.courses.brand_id = 41 THEN COUNT(DISTINCT dw.sessions.id) END AS academics_sessions,
            CASE WHEN dw.courses.brand_id = 47 THEN COUNT(DISTINCT dw.sessions.id) END AS school_pay_sessions
            FROM dw.sessions
            JOIN dw.courses ON dw.sessions.course_id = dw.courses.id
            JOIN dw.enrollments ON dw.enrollments.course_id = dw.courses.id
            WHERE dw.sessions.starts_at::DATE BETWEEN (GETDATE()::DATE)-31 AND (GETDATE()::DATE)-1
            AND dw.sessions.attendances_attended_count > 0
            AND dw.courses.brand_id IN (2,41,42,43,47)
            GROUP BY dw.enrollments.enrollee_id, dw.sessions.supervisor_id, dw.courses.brand_id
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
            dw.sessions.supervisor_id AS tutor_id,
            CAST((dw.students.graduation_year||'-06-30') AS DATE) AS grad_date,
            CASE WHEN dw.courses.brand_id = 2 THEN COUNT(DISTINCT dw.sessions.id) END AS private_tutoring_sessions,
            CASE WHEN dw.courses.brand_id = 42 THEN COUNT(DISTINCT dw.sessions.id) END AS buc_sessions,
            CASE WHEN dw.courses.brand_id = 43 THEN COUNT(DISTINCT dw.sessions.id) END AS trial_sessions,
            CASE WHEN dw.courses.brand_id = 41 THEN COUNT(DISTINCT dw.sessions.id) END AS academics_sessions,
            CASE WHEN dw.courses.brand_id = 47 THEN COUNT(DISTINCT dw.sessions.id) END AS school_pay_sessions
            FROM dw.sessions
            JOIN dw.courses ON dw.sessions.course_id = dw.courses.id
            JOIN dw.enrollments ON dw.enrollments.course_id = dw.courses.id
            JOIN dw.students ON dw.students.id = dw.enrollments.enrollee_id
            WHERE dw.sessions.starts_at >= (GETDATE()::DATE)
            AND dw.courses.brand_id IN (2,41,42,43,47)
            AND (dw.students.graduation_year IS NULL
                OR dw.sessions.starts_at >= CAST((dw.students.graduation_year - 4) || '-07-01' AS DATE))
            GROUP BY dw.enrollments.enrollee_id, dw.sessions.supervisor_id,
                dw.courses.brand_id, dw.students.graduation_year
        )
        SELECT
            cte_future.tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            dw.teams.name AS team_name,
            cte_future.student_id,
            student_users.first_name||' '||student_users.last_name AS student_name,
            cte_future.grad_date,
            CASE WHEN cte_future.grad_date - (getdate()::DATE) >= 0
                THEN 12 - FLOOR((cte_future.grad_date - (GETDATE()::DATE))::FLOAT/365)
                ELSE 12 - CEILING((cte_future.grad_date - (GETDATE()::DATE))::FLOAT/365) END AS grade_lvl,
            COUNT(DISTINCT lds2.tutor_id) AS tutor_count,
            CASE WHEN MAX(cte_last_30_days_brands.private_tutoring_sessions) > 0
                OR MAX(cte_future.private_tutoring_sessions) > 0 THEN TRUE ELSE FALSE END AS private_tutoring,
            CASE WHEN MAX(cte_last_30_days_brands.buc_sessions) > 0
                OR MAX(cte_future.buc_sessions) > 0 THEN TRUE ELSE FALSE END AS buc,
            CASE WHEN MAX(cte_last_30_days_brands.academics_sessions) > 0
                OR MAX(cte_future.academics_sessions) > 0 THEN TRUE ELSE FALSE END AS academics,
            CASE WHEN MAX(cte_last_30_days_brands.school_pay_sessions) > 0
                OR MAX(cte_future.school_pay_sessions) > 0 THEN TRUE ELSE FALSE END AS school_pay,
            cte_grades.subject,
            cte_grades.score,
            CAST(cte_grades.updated_at AS DATE) AS updated_at
        FROM cte_future
        JOIN cte_last_30_days_brands
            ON cte_future.student_id = cte_last_30_days_brands.student_id
            AND cte_future.tutor_id = cte_last_30_days_brands.tutor_id
        JOIN cte_last_30_days_sessions lds1
            ON cte_future.student_id = lds1.student_id
            AND cte_future.tutor_id = lds1.tutor_id
        JOIN cte_last_30_days_sessions lds2
            ON cte_future.student_id = lds2.student_id
        JOIN dw.students ON cte_future.student_id = dw.students.id
        JOIN dw.users student_users ON dw.students.user_id = student_users.id
        JOIN dw.employees ON cte_future.tutor_id = dw.employees.id
        JOIN dw.users tutor_users ON dw.employees.user_id = tutor_users.id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        LEFT JOIN cte_grades ON dw.students.id = cte_grades.student_id
        WHERE dw.employees.end_date IS NULL
        AND dw.team_members.member_type = 'Employee'
        AND tutor_users.title = 'Tutor'
        GROUP BY cte_future.student_id, student_name, cte_future.tutor_id,
            tutor_name, team_name, lds1.session_count, cte_grades.subject,
            cte_grades.score, cte_grades.updated_at, cte_future.grad_date
        HAVING lds1.session_count > 1
        ORDER BY 1,4
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/grades_data.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached grades data from {cached_at}.")
            return df, cached_at
        st.error("Cache mode ON but no cached grades data found.")
        return pd.DataFrame(), "unavailable"
    try:
        df = pd.read_sql(query, conn)
        conn.close()
        fetched_at = pd.Timestamp.now().strftime("%B %d, %Y at %I:%M %p")
        return df, fetched_at
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/grades_data.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached grades data from {cached_at}.")
            return df, cached_at
        st.error(f"Grades data unavailable: {e}")
        return pd.DataFrame(), "unavailable"


@st.cache_data(ttl=3600)
def load_availability_compliance():
    conn = get_redshift_connection()
    today = pd.Timestamp.now()
    days_since_sunday = (today.weekday() + 1) % 7
    this_sunday = (today - pd.Timedelta(days=days_since_sunday)).strftime("%Y-%m-%d")
    query = """
        WITH cte_availabilities_by_tutor_time_zone AS (
            SELECT
                availabilities.employee_id,
                availabilities.starts_at,
                availabilities.starts_at AT TIME ZONE 'America/Los_Angeles' AT TIME ZONE users.time_zone AS tutor_starts_at,
                users.first_name||' '||users.last_name AS tutor,
                addresses.state,
                teams.name AS team,
                users.time_zone AS tutor_time_zone
            FROM dw.availabilities
            JOIN dw.employees ON availabilities.employee_id = employees.id
            JOIN dw.users ON users.id = employees.user_id
            JOIN dw.team_members ON employees.id = team_members.member_id
            JOIN dw.teams ON team_members.team_id = teams.id
            JOIN dw.addresses ON users.address_id = addresses.id
        )
        SELECT
            ttz.tutor AS tutor_name,
            ttz.employee_id,
            ttz.tutor_time_zone,
            ttz.state,
            ttz.team,
            dates.first_day_of_week_sunday_start AS week_start
        FROM cte_availabilities_by_tutor_time_zone ttz
        JOIN rp_bi.dates ON ttz.tutor_starts_at::DATE = dates.full_date
        WHERE dates.first_day_of_week_sunday_start = '{this_sunday}'
        GROUP BY ttz.tutor, ttz.employee_id, ttz.tutor_time_zone, ttz.state, ttz.team,
                 dates.first_day_of_week_sunday_start
        HAVING COUNT(DISTINCT ttz.tutor_starts_at::DATE) > 6
        ORDER BY ttz.team, ttz.tutor
    """.format(this_sunday=this_sunday)
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/availability_compliance.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached availability data from {cached_at}.")
            return df
        st.error("Cache mode ON but no cached availability data found.")
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        conn.close()
        return df
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/availability_compliance.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached availability data from {cached_at}.")
            return df
        return pd.DataFrame()


def load_nps_scores(start_date: str, end_date: str, team_name: str):
    """Load NPS scores for a team within a date range."""
    conn = get_redshift_connection()
    query = f"""
        WITH cte_remove_nps_dups AS (
            SELECT parent_id, student_id, created_at, responded_at, id,
                   ROW_NUMBER() OVER (PARTITION BY parent_id, student_id, created_at, responded_at ORDER BY id) AS dupnumber
            FROM dw.nps_histories
        )
        SELECT DISTINCT
            nps_histories.id AS nps_id,
            nps_histories.responded_at AS nps_responded_at,
            nps_histories.comment AS nps_comment,
            nps_histories.parent_id,
            nps_histories.student_id AS nps_student_id,
            student_users.first_name||' '||student_users.last_name AS student_name,
            nps_histories.tutor_id,
            employee_users.first_name||' '||employee_users.last_name AS tutor_name,
            employees.tutor_type,
            teams.name AS tutor_team,
            tiers.name AS tier_name,
            nps_histories.score AS nps
        FROM dw.nps_histories
        LEFT JOIN dw.students ON nps_histories.student_id = students.id
        LEFT JOIN dw.users student_users ON students.user_id = student_users.id
        LEFT JOIN dw.employees ON nps_histories.tutor_id = employees.id
        LEFT JOIN dw.tiers ON employees.tier_id = tiers.id
        LEFT JOIN dw.team_members ON employees.id = team_members.member_id
        LEFT JOIN dw.teams ON team_members.team_id = teams.id
        LEFT JOIN dw.users employee_users ON employees.user_id = employee_users.id
        WHERE nps_histories.responded_at >= '{start_date}'
          AND nps_histories.responded_at <= '{end_date}'
          AND nps_histories.id IN (SELECT id FROM cte_remove_nps_dups WHERE dupnumber = 1)
          AND teams.name = '{team_name}'
        ORDER BY nps_histories.responded_at DESC
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            # Filter by date range using hire_date or just return all
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached KPI data from {cached_at}.")
            return df
        st.error("Cache mode ON but no cached AR KPI data found.")
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached KPI data from {cached_at}.")
            return df
        raise
    finally:
        try: conn.close()
        except: pass
    return df

def load_progress_updates(as_of_date: str, last_session_from: str, team_name: str):
    """Load progress update compliance data for active students."""
    conn = get_redshift_connection()
    query = f"""
        SELECT DISTINCT
            employees.id AS tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            tiers.name AS tier,
            student_users.first_name||' '||student_users.last_name AS student_name,
            enrollments.course_id,
            MAX(sessions.starts_at) AS last_session,
            MAX(tutoring_histories.progress_update_last_sent_at) AS last_progress_update,
            SUM(sessions.duration)/60.0 AS hours_delivered,
            CASE
                WHEN EXTRACT(days FROM MAX(sessions.starts_at) - MAX(tutoring_histories.progress_update_last_sent_at)) > 60
                  OR MAX(tutoring_histories.progress_update_last_sent_at) IS NULL
                THEN FALSE ELSE TRUE
            END AS on_time
        FROM dw.sessions
        JOIN dw.courses ON courses.id = sessions.course_id
        JOIN dw.enrollments ON enrollments.course_id = courses.id
        JOIN dw.tutoring_histories ON tutoring_histories.enrollment_id = enrollments.id
            AND sessions.supervisor_id = tutoring_histories.tutor_id
        JOIN dw.employees ON tutoring_histories.tutor_id = employees.id
        JOIN dw.users tutor_users ON employees.user_id = tutor_users.id
        JOIN dw.tiers ON employees.tier_id = tiers.id
        JOIN dw.students ON enrollments.enrollee_id = students.id
        JOIN dw.users student_users ON students.user_id = student_users.id
        JOIN dw.team_members ON employees.id = team_members.member_id
        JOIN dw.teams ON team_members.team_id = teams.id
        WHERE sessions.starts_at < '{as_of_date}'
          AND courses.brand_id IN (41, 42, 2)
          AND teams.name = '{team_name}'
        GROUP BY 1,2,3,4,5
        HAVING SUM(sessions.duration)/60.0 >= 6
           AND MAX(sessions.starts_at) >= '{last_session_from}'
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            # Filter by date range using hire_date or just return all
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached KPI data from {cached_at}.")
            return df
        st.error("Cache mode ON but no cached AR KPI data found.")
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached KPI data from {cached_at}.")
            return df
        raise
    finally:
        try: conn.close()
        except: pass
    return df

@st.cache_data(ttl=3600)
def load_cancellation_data():
    """Load cancellation data from Excel file."""
    file = "CancellationData.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file)
    return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_ar_kpi(start, end):
    conn = get_redshift_connection()
    query = f"""
        WITH time_period AS (
            SELECT '{start}'::date AS day_start, '{end}'::date AS day_end
        ),
        cte_sessions AS (
            SELECT id as session_id, starts_at, launched_at, supervisor_id, duration, course_id,
                DATEDIFF(minute, starts_at, launched_at) minutes_launched_late,
                attendances_attended_count,
                CASE WHEN automatic_attendance IS TRUE THEN 1 ELSE 0 END AS automatic_attendance
            FROM dw.sessions
            WHERE sessions.starts_at >= (SELECT day_start FROM time_period)
              AND sessions.starts_at < (SELECT day_end FROM time_period)
        ),
        cte_deliveries AS (
            SELECT employee_id, first_day_of_week_sunday_start AS first_day_of_week,
                instruction_actual AS delivery, instruction_target AS delivery_target,
                CASE WHEN instruction_actual >= instruction_target THEN 1 ELSE 0 END AS meeting_target,
                availability_target
            FROM rp_bi.tutor_capacity
            WHERE first_day_of_week_sunday_start >= (SELECT day_start FROM time_period)
              AND first_day_of_week_sunday_start < (SELECT day_end FROM time_period)
        ),
        cte_availabilities AS (
            SELECT employee_id, first_day_of_week_sunday_start AS first_day_of_week,
                SUM(availability_hours) AS availability
            FROM rp_bi.tutor_availabilities_weekly
            WHERE first_day_of_week_sunday_start >= (SELECT day_start FROM time_period)
              AND first_day_of_week_sunday_start < (SELECT day_end FROM time_period)
            GROUP BY employee_id, first_day_of_week_sunday_start
        ),
        cte_sessiondelivery AS (
            SELECT supervisor_id AS employee_id,
                (SUM(duration)/60.0) AS delivery_hours,
                COUNT(launched_at)*1.0 AS launched_sessions,
                COUNT(DISTINCT course_id) AS course_count
            FROM cte_sessions GROUP BY supervisor_id
        ),
        cte_ontime AS (
            SELECT supervisor_id AS employee_id,
                COUNT(DISTINCT session_id)*1.0 AS ontime_sessions
            FROM cte_sessions WHERE minutes_launched_late < 2
            GROUP BY supervisor_id
        ),
        weekly_sessions AS (
            SELECT s.id AS session_id, s.starts_at AS session_start,
                d.first_day_of_week_sunday_start, s.attendances_attended_count,
                c.id AS course_id, b.name AS brand_name, st.id AS student_id,
                s.supervisor_id,
                CASE WHEN b.name = 'Trial' THEN s.id
                     WHEN b.name IN ('Group Course','Small Group Course','Boot Camp','SGC','Boot Camps') THEN c.id
                     ELSE st.id END AS update_unit_id,
                CASE WHEN s.attendances_attended_count > 0 THEN 1 ELSE 0 END AS update_required_flag
            FROM dw.sessions s
            JOIN dw.courses c ON s.course_id = c.id
            JOIN dw.brands b ON b.id = c.brand_id
            JOIN dw.enrollments en ON en.course_id = c.id
            JOIN dw.students st ON st.id = en.enrollee_id
            JOIN rp_bi.dates d ON s.starts_at::date = d.full_date
            WHERE s.starts_at::date >= (SELECT day_start FROM time_period)
              AND s.starts_at::date < (SELECT day_end FROM time_period)
              AND b.name NOT IN ('Special Events','Seminar','Professional Development',
                  '1-on-1 Meetings','Group Meetings','Special Event','Self Study','Parent Event')
        ),
        updates_sent AS (
            SELECT DISTINCT ca.employee_id, s.course_id, d.first_day_of_week_sunday_start
            FROM dw.contact_activities ca
            JOIN dw.sessions s ON ca.regarding_id = s.id
            JOIN rp_bi.dates d ON ca.created_at::date = d.full_date
            WHERE ca.type = 'Contact::Message'
              AND ca.message_type IN ('Parent Update','Progress Update')
              AND ca.regarding_type = 'Session'
              AND ca.created_at::date >= (SELECT day_start FROM time_period)
              AND ca.created_at::date < (SELECT day_end FROM time_period)
            UNION ALL
            SELECT DISTINCT ca.employee_id, ca.regarding_id AS course_id, d.first_day_of_week_sunday_start
            FROM dw.contact_activities ca
            JOIN rp_bi.dates d ON CAST(ca.created_at AS DATE) = d.full_date
            WHERE ca.type = 'Contact::Message'
              AND ca.message_type IN ('Parent Update','Progress Update')
              AND ca.regarding_type = 'Course'
              AND CAST(ca.created_at AS DATE) >= (SELECT day_start FROM time_period)
              AND CAST(ca.created_at AS DATE) < (SELECT day_end FROM time_period)
        ),
        tutor_updates AS (
            SELECT DISTINCT ws.supervisor_id, ws.first_day_of_week_sunday_start AS week,
                COUNT(DISTINCT CASE WHEN ws.update_required_flag = 1 THEN ws.update_unit_id END) AS updates_required,
                COUNT(DISTINCT CASE WHEN ws.update_required_flag = 1 AND us.course_id IS NOT NULL THEN ws.update_unit_id END) AS updates_sent_on_time
            FROM weekly_sessions ws
            LEFT JOIN updates_sent us ON ws.course_id = us.course_id
                AND ws.supervisor_id = us.employee_id
                AND ws.first_day_of_week_sunday_start = us.first_day_of_week_sunday_start
            GROUP BY ws.supervisor_id, ws.first_day_of_week_sunday_start
        ),
        cte_parent_updates AS (
            SELECT supervisor_id,
                ROUND((SUM(updates_sent_on_time::decimal) / NULLIF(SUM(updates_required),0)), 3) AS percent_parent_updates
            FROM tutor_updates WHERE updates_required >= 0
            GROUP BY supervisor_id
        ),
        cte_NPS AS (
            SELECT employees.id AS tutor_id,
                COUNT(DISTINCT nps_histories.id) AS number_of_nps,
                SUM(nps_histories.score*1.0)/COUNT(DISTINCT nps_histories.id)*1.0 AS avg_nps_score
            FROM dw.nps_histories
            JOIN dw.employees ON employees.id = nps_histories.tutor_id
            WHERE nps_histories.created_at >= (SELECT day_start FROM time_period)
              AND nps_histories.created_at < (SELECT day_end FROM time_period)
            GROUP BY employees.id
        ),
        cte_prep1 AS (
            SELECT cte_sessions.supervisor_id AS tutor_id, cte_sessions.starts_at,
                brands.name AS category,
                CASE WHEN cte_sessions.attendances_attended_count = 0 THEN 'PrepTime' ELSE 'Attended Session' END AS type,
                cte_sessions.duration/60.0 AS duration_hours,
                dates.first_day_of_week_sunday_start AS week_of
            FROM cte_sessions
            JOIN rp_bi.dates ON (datepart(year,cte_sessions.starts_at)=dates.year
                AND datepart(month,cte_sessions.starts_at)=dates.month
                AND datepart(day,cte_sessions.starts_at)=dates.day)
            JOIN dw.courses ON cte_sessions.course_id = courses.id
            JOIN dw.brands ON courses.brand_id = brands.id
            UNION ALL
            SELECT events.attendee_id, events.starts_at, events.category,
                REPLACE(events.type,'Event::',''), events.duration/60.0,
                dates.first_day_of_week_sunday_start
            FROM dw.events
            JOIN rp_bi.dates ON (datepart(year,events.starts_at)=dates.year
                AND datepart(month,events.starts_at)=dates.month
                AND datepart(day,events.starts_at)=dates.day)
            WHERE events.starts_at >= (SELECT day_start FROM time_period)
              AND events.starts_at < (SELECT day_end FROM time_period)
              AND events.category IN ('Session Preparation','Email or Slack Communication')
        ),
        cte_prep2 AS (
            SELECT tutor_id,
                CASE WHEN type='PrepTime' THEN SUM(duration_hours) ELSE NULL END AS Total_Prep,
                CASE WHEN type='Attended Session' THEN SUM(duration_hours) ELSE NULL END AS Attended_Sessions
            FROM cte_prep1 GROUP BY tutor_id, type
        )
        SELECT DISTINCT
            dw.employees.id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            dw.tiers.name AS tier,
            dw.employees.skill_score AS current_sci,
            manager_users.first_name||' '||manager_users.last_name AS fl_name,
            DATE(dw.employees.hire_date) AS hire_date,
            dw.employees.delivery_target,
            ROUND(AVG(cte_deliveries.delivery),2) AS avg_delivery,
            ROUND(AVG(cte_deliveries.delivery)/(dw.employees.delivery_target),4) AS delivery_pct,
            COUNT(DISTINCT CASE WHEN cte_deliveries.meeting_target=1 THEN cte_deliveries.first_day_of_week END) AS weeks_at_target,
            ROUND(AVG(cte_availabilities.availability),2) AS avg_availability,
            ROUND(AVG(cte_availabilities.availability)/(dw.employees.availability_target),4) AS availability_pct,
            (cte_ontime.ontime_sessions/cte_sessiondelivery.launched_sessions)*1.00 AS sessions_on_time_pct,
            cte_parent_updates.percent_parent_updates AS parent_update_pct,
            cte_NPS.number_of_nps,
            CASE WHEN cte_NPS.number_of_nps>0 THEN cte_NPS.avg_nps_score END AS avg_nps,
            COUNT(DISTINCT CASE WHEN cte_sessions.automatic_attendance=1 THEN cte_sessions.session_id END) AS autoattendance_sessions,
            CASE WHEN MAX(cte_prep2.Attended_Sessions)>0
                THEN MAX(cte_prep2.Total_Prep)/MAX(cte_prep2.Attended_Sessions) END AS prep_time_ratio
        FROM dw.employees
        JOIN dw.users tutor_users ON employees.user_id = tutor_users.id
        JOIN dw.tiers ON employees.tier_id = tiers.id
        JOIN dw.team_members ON employees.id = team_members.member_id
        JOIN dw.teams ON team_members.team_id = teams.id
        JOIN dw.employees managers ON teams.manager_id = managers.id
        JOIN dw.users manager_users ON managers.user_id = manager_users.id
        LEFT JOIN cte_sessions ON employees.id = cte_sessions.supervisor_id
        LEFT JOIN cte_ontime ON employees.id = cte_ontime.employee_id
        LEFT JOIN cte_sessiondelivery ON employees.id = cte_sessiondelivery.employee_id
        LEFT JOIN cte_parent_updates ON employees.id = cte_parent_updates.supervisor_id
        LEFT JOIN cte_deliveries ON employees.id = cte_deliveries.employee_id
        LEFT JOIN cte_availabilities ON employees.id = cte_availabilities.employee_id
        LEFT JOIN cte_NPS ON cte_NPS.tutor_id = employees.id
        LEFT JOIN cte_prep2 ON employees.id = cte_prep2.tutor_id
        WHERE employees.end_date IS NULL
          AND employees.delivery_target > 0
          AND employees.type = 'Tutor'
          AND tutor_users.title = 'Tutor'
        GROUP BY employees.id, tutor_users.first_name, tutor_users.last_name,
            dw.tiers.name, employees.skill_score, manager_users.first_name, manager_users.last_name,
            employees.hire_date, employees.delivery_target, cte_ontime.ontime_sessions,
            cte_sessiondelivery.launched_sessions, cte_nps.number_of_nps, cte_nps.avg_nps_score,
            employees.availability_target, cte_parent_updates.percent_parent_updates
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            # Filter by date range using hire_date or just return all
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached KPI data from {cached_at}.")
            return df
        st.error("Cache mode ON but no cached AR KPI data found.")
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        return df
    except Exception as e:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/ar_kpi.csv")
        if not df.empty:
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached KPI data from {cached_at}.")
            return df
        raise
    finally:
        try: conn.close()
        except: pass
    return df

@st.cache_data(ttl=3600)
def load_low_delivery_not_accepting(faculty_leader: str):
    """Tutors with accepting off and low delivery in next 3 weeks."""
    conn = get_redshift_connection()
    query = f"""
        SELECT u.first_name||' '||u.last_name AS tutor,
            e.delivery_target,
            ROUND(AVG(tc.instruction_actual)::numeric, 1) AS avg_delivery_next_3wks,
            ROUND(AVG(tc.instruction_actual)::numeric / NULLIF(e.delivery_target,0) * 100, 1) AS delivery_pct
        FROM dw.employees e
        JOIN dw.users u ON e.user_id = u.id
        JOIN dw.team_members ON e.id = team_members.member_id
        JOIN dw.teams ON team_members.team_id = teams.id
        JOIN dw.employees managers ON teams.manager_id = managers.id
        JOIN dw.users fl_users ON managers.user_id = fl_users.id
        JOIN rp_bi.tutor_capacity tc ON tc.employee_id = e.id
        WHERE e.end_date IS NULL
          AND e.type = 'Tutor'
          AND u.title = 'Tutor'
          AND e.accept_new_students = FALSE
          AND fl_users.first_name||' '||fl_users.last_name = '{faculty_leader}'
          AND tc.first_day_of_week_sunday_start >= CURRENT_DATE
          AND tc.first_day_of_week_sunday_start <= CURRENT_DATE + 21
        GROUP BY u.first_name, u.last_name, e.delivery_target
        HAVING AVG(tc.instruction_actual) < e.delivery_target * 0.80
        ORDER BY delivery_pct
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/low_delivery.csv")
        if not df.empty:
            keep = [c for c in df.columns if c not in ["faculty_leader","_cached_at"]]
            return df[df["faculty_leader"] == faculty_leader][keep] if "faculty_leader" in df.columns else df[keep]
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        return df
    except Exception:
        df = _gh_read_cache("data/cache/low_delivery.csv")
        if not df.empty:
            keep = [c for c in df.columns if c not in ["faculty_leader","_cached_at"]]
            return df[df["faculty_leader"] == faculty_leader][keep] if "faculty_leader" in df.columns else df[keep]
        return pd.DataFrame()
    finally:
        try: conn.close()
        except: pass
@st.cache_data(ttl=3600)
def load_study_areas():
    """Load goal scores and starting scores from orbit_stitch.study_areas."""
    conn = get_redshift_connection()
    query = """
        SELECT
            sa.student_id,
            sa.subject_id,
            sa.goal_score,
            sa.starting_score,
            s.name AS subject_name,
            CASE
                WHEN sa.subject_id IN (43, 356, 239)          THEN 'ACT'
                WHEN sa.subject_id IN (51, 315, 147)          THEN 'SAT'
                WHEN sa.subject_id IN (316, 50, 342, 195, 240, 344) THEN 'PSAT'
                ELSE 'Other'
            END AS exam_family
        FROM orbit_stitch.study_areas sa
        LEFT JOIN dw.subjects s ON sa.subject_id = s.id
        WHERE sa._sdc_deleted_at IS NULL
          AND (sa.goal_score IS NOT NULL OR sa.starting_score IS NOT NULL)
    """
    try:
        df = pd.read_sql(query, conn)
        df["goal_score"]     = pd.to_numeric(df["goal_score"],     errors="coerce")
        df["starting_score"] = pd.to_numeric(df["starting_score"], errors="coerce")
        return df
    except Exception as e:
        return pd.DataFrame()
    finally:
        conn.close()

@st.cache_data(ttl=3600)
def load_featured_tutors():
    """Load currently featured tutors from Redshift."""
    conn = get_redshift_connection()
    query = """
        SELECT tutors.first_name||' '||tutors.last_name AS tutor,
            manager_users.first_name||' '||manager_users.last_name AS faculty_leader,
            tiers.name AS tutor_tier,
            e.delivery_target
        FROM dw.employees e
        JOIN dw.users tutors ON e.user_id = tutors.id
        JOIN dw.team_members ON e.id = team_members.member_id
        JOIN dw.teams ON team_members.team_id = teams.id
        JOIN dw.employees managers ON teams.manager_id = managers.id
        JOIN dw.users manager_users ON managers.user_id = manager_users.id
        JOIN dw.tiers ON e.tier_id = tiers.id
        WHERE e.end_date IS NULL
          AND e.delivery_target > 0
          AND e.type = 'Tutor'
          AND tutors.title = 'Tutor'
          AND e.featured IS TRUE
        ORDER BY faculty_leader, tutor_tier, tutor
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/featured_tutors.csv")
        if not df.empty and "_cached_at" in df.columns:
            df = df.drop(columns=["_cached_at"])
        return df
    try:
        df = pd.read_sql(query, conn)
        return df
    except Exception:
        df = _gh_read_cache("data/cache/featured_tutors.csv")
        if not df.empty and "_cached_at" in df.columns:
            df = df.drop(columns=["_cached_at"])
        return df
    finally:
        try: conn.close()
        except: pass

@st.cache_data(ttl=3600)
def load_rematch_tracker():
    """Load rematch tracker from Excel file."""
    file = "rematch trcker.xlsx"
    if os.path.exists(file):
        df = pd.read_excel(file)
        # Parse rematch date — format is like "4.1.2025"
        def parse_rematch_date(s):
            try:
                parts = str(s).strip().split(".")
                if len(parts) == 3:
                    return pd.Timestamp(f"{parts[2]}-{parts[0].zfill(2)}-{parts[1].zfill(2)}")
            except:
                pass
            return pd.NaT
        df["Rematch Date Parsed"] = df["Rematch Date"].apply(parse_rematch_date)
        return df
    return pd.DataFrame()

def load_ppw_data(start_date: str, end_date: str, team_name: str):
    """Load PPW (first session attachment) data for a given date range and team."""
    conn = get_redshift_connection()
    query = f"""
        WITH first_course AS (
            SELECT
                students.id AS student_id,
                MIN(courses.starts_at) AS course_start
            FROM dw.students
            JOIN dw.enrollments ON enrollments.enrollee_id = students.id
            JOIN dw.courses ON courses.id = enrollments.course_id
            WHERE courses.brand_id IN (2, 42, 41, 43, 36)
            GROUP BY students.id
        )
        SELECT
            courses.starts_at,
            courses.brand_id,
            employees.id AS tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            student_users.first_name||' '||student_users.last_name AS student_name,
            CASE
                WHEN orbit_stitch.attachments.updated_at IS NOT NULL
                AND (EXTRACT(DAY FROM (orbit_stitch.attachments.created_at - courses.starts_at))*24
                     + EXTRACT(HOUR FROM (orbit_stitch.attachments.created_at - courses.starts_at)) < 72)
                THEN 1 ELSE 0
            END AS attachment_uploaded
        FROM first_course
        JOIN dw.students ON first_course.student_id = students.id
        JOIN dw.users student_users ON students.user_id = student_users.id
        JOIN dw.enrollments ON students.id = enrollments.enrollee_id
        JOIN dw.courses ON (courses.id = enrollments.course_id AND courses.starts_at = first_course.course_start)
        JOIN dw.sessions ON (sessions.course_id = courses.id AND sessions.starts_at = courses.starts_at)
        JOIN dw.employees ON sessions.supervisor_id = employees.id
        JOIN dw.users tutor_users ON employees.user_id = tutor_users.id
        JOIN dw.team_members ON employees.id = team_members.member_id
        JOIN dw.teams ON team_members.team_id = teams.id
        LEFT JOIN orbit_stitch.attachments ON (
            orbit_stitch.attachments.attachable_id = sessions.id
            AND orbit_stitch.attachments.attachable_type = 'Session'
        )
        WHERE courses.starts_at >= '{start_date}'
          AND courses.starts_at <= '{end_date}'
          AND teams.name = '{team_name}'
        ORDER BY tutor_name, courses.starts_at
    """
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    # Map brand_id to name
    brand_map = {2: 'Private Tutoring', 41: 'Back-Up Care Tutoring', 42: 'Academics',
                 43: 'School Pay', 36: 'Trial'}
    df['brand'] = df['brand_id'].map(brand_map).fillna(df['brand_id'].astype(str))
    return df

@st.cache_data(ttl=3600)
def load_exam_data():
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = st.secrets["github"].get("exam_data_path", "data/exam_data.csv")
        import urllib.request
        ts   = int(pd.Timestamp.now().timestamp())
        url  = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?cb={ts}"
        resp = _requests.get(url, headers={"Authorization": f"token {github_token}"}, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if "fetched_at" in df.columns:
            fetched_at = df["fetched_at"].iloc[0] if not df.empty else "unknown"
            df = df.drop(columns=["fetched_at"])
        else:
            fetched_at = "unknown"
        return df, fetched_at
    except Exception as e:
        raise RuntimeError(f"Could not load exam data from GitHub: {e}")




@st.cache_data(ttl=3600)
def load_progress_history():
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = "data/progress_updates_history.json"
        import urllib.request, json as _json
        ts  = int(pd.Timestamp.now().timestamp())
        url = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?ts={ts}"
        req = urllib.request.Request(url, headers={"Authorization": f"token {github_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        df = pd.DataFrame(data)
        df["sent_at"]   = pd.to_datetime(df["sent_at"],   errors="coerce")
        df["scored_at"] = pd.to_datetime(df["scored_at"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600)
def load_parent_update_history():
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = "data/parent_update_videos_history.csv"
        import urllib.request, io
        ts  = int(pd.Timestamp.now().timestamp())
        url = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?ts={ts}"
        req = urllib.request.Request(url, headers={"Authorization": f"token {github_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return pd.read_csv(io.StringIO(resp.read().decode("utf-8")))
    except Exception:
        return pd.DataFrame()

@st.cache_data(ttl=3600)
def load_progress_scores():
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = st.secrets["github"].get("progress_scores_path", "data/scored_progress_updates.json")
        import urllib.request, json as _json
        ts  = int(pd.Timestamp.now().timestamp())
        url = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?ts={ts}"
        req = urllib.request.Request(url, headers={"Authorization": f"token {github_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        df = pd.DataFrame(data)
        df["sent_at"]   = pd.to_datetime(df["sent_at"],   errors="coerce")
        df["scored_at"] = pd.to_datetime(df["scored_at"], errors="coerce")
        fetched_at = df["scored_at"].max().strftime("%Y-%m-%d") if not df.empty else "unknown"
        return df, fetched_at
    except Exception as e:
        raise RuntimeError(f"Could not load progress scores from GitHub: {e}")

@st.cache_data(ttl=3600)
def load_parent_update_videos():
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        github_path  = st.secrets["github"].get("parent_update_video_path", "data/parent_update_videos.csv")
        import urllib.request
        ts   = int(pd.Timestamp.now().timestamp())
        url  = f"https://raw.githubusercontent.com/{github_repo}/main/{github_path}?cb={ts}"
        resp = _requests.get(url, headers={"Authorization": f"token {github_token}"}, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if "fetched_at" in df.columns:
            fetched_at = df["fetched_at"].iloc[0] if not df.empty else "unknown"
            df = df.drop(columns=["fetched_at"])
        else:
            fetched_at = "unknown"
        return df, fetched_at
    except Exception as e:
        raise RuntimeError(f"Could not load parent update video data from GitHub: {e}")


# ─────────────────────────────────────────────
# VIDEO HELPER FUNCTIONS
# ─────────────────────────────────────────────

def duration_to_secs(d):
    try:
        if pd.isna(d) or str(d).strip() in ("N/A", "", "nan", "None"):
            return None
        parts = str(d).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except:
        return None

def secs_to_duration(s):
    if s is None or pd.isna(s):
        return "N/A"
    return f"{int(s) // 60}:{int(s) % 60:02d}"

def build_video_tutor_summary(df):
    """Build per-tutor video summary from filtered video dataframe."""
    rows = []
    for tutor, tdf in df.groupby("tutor"):
        updates_required = int(tdf["update required"].sum()) if "update required" in tdf.columns else 0
        sent_df          = tdf[tdf["parent update sent"].astype(str) == "True"]
        updates_sent     = len(sent_df)
        parent_update_pct = round(updates_sent / updates_required * 100, 1) if updates_required > 0 else 0.0
        # Video metrics only for Parent Updates (not Progress Updates)
        parent_only_df   = tdf[tdf["parent update only sent"].astype(str) == "True"]                            if "parent update only sent" in tdf.columns else sent_df
        parent_only_sent = len(parent_only_df)
        videos_found     = int(parent_only_df["video found"].fillna(False).astype(bool).sum())
        pct_with_video   = round(videos_found / parent_only_sent * 100, 1) if parent_only_sent > 0 else 0.0
        # Homework metrics — based on all sent updates
        hw_df            = sent_df[sent_df["homework mentioned"].astype(str) == "True"]                            if "homework mentioned" in sent_df.columns else pd.DataFrame()
        homework_count   = len(hw_df)
        pct_with_homework = round(homework_count / updates_sent * 100, 1) if updates_sent > 0 else 0.0
        secs_series      = sent_df["duration_secs"].dropna()
        rows.append({
            "tutor_name":        tutor,
            "updates_required":  updates_required,
            "updates_sent":      updates_sent,
            "parent_only_sent":  parent_only_sent,
            "parent_update_pct": parent_update_pct,
            "videos_found":      videos_found,
            "pct_with_video":    pct_with_video,
            "homework_count":    homework_count,
            "pct_with_homework": pct_with_homework,
            "longest_secs":      secs_series.max()    if not secs_series.empty else None,
            "shortest_secs":     secs_series.min()    if not secs_series.empty else None,
            "median_secs":       secs_series.median() if not secs_series.empty else None,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# SNAPSHOT FILES
# ─────────────────────────────────────────────

SNAPSHOT_FILE         = "archive_snapshots.csv"
SNAPSHOT_HISTORY_FILE = "archive_snapshots_history.csv"
GRADES_SNAPSHOT_FILE = "grades_snapshots.csv"
EXAMS_SNAPSHOT_FILE  = "exams_snapshots.csv"
VIDEO_SNAPSHOT_FILE  = "video_snapshots.csv"
PROGRESS_SNAPSHOT_FILE = "katherine_progress_score_snapshots.csv"


def save_weekly_snapshot(df):
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")
    summary = (
        df.groupby("tutor_name").agg(
            archivable_students=("should_archive", lambda x: int(x.sum())),
            total_students=("student_name", "nunique"),
            unscheduled_hours=("unscheduled_hours", "sum"),
        ).reset_index()
    )
    summary["week_key"]  = week_key
    summary["week_date"] = week_date
    # Save 2-week rolling snapshot (used by home page change indicators)
    existing = gh_read(SNAPSHOT_FILE)
    if not existing.empty and week_key in existing["week_key"].values:
        pass  # already saved this week
    else:
        updated = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
        # Keep only 2 most recent weeks
        if "week_key" in updated.columns:
            recent_keys = sorted(updated["week_key"].unique())[-2:]
            updated = updated[updated["week_key"].isin(recent_keys)]
        gh_write(SNAPSHOT_FILE, updated)

    # Also save to full history file (used by annual reviews)
    history = gh_read(SNAPSHOT_HISTORY_FILE)
    if history.empty or week_key not in history.get("week_key", pd.Series()).values:
        history_updated = pd.concat([history, summary], ignore_index=True) if not history.empty else summary
        gh_write(SNAPSHOT_HISTORY_FILE, history_updated)

    return gh_read(SNAPSHOT_FILE)


def save_grades_weekly_snapshot(df):
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")
    existing = gh_read(GRADES_SNAPSHOT_FILE)
    if not existing.empty and week_key in existing["week_key"].values:
        return existing
    now = pd.Timestamp.now()
    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        total_students    = tdf["student_id"].nunique()
        no_grade_students = tdf[tdf["score"].isna()]["student_id"].nunique()
        has_any_grade     = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_ids        = has_any_grade[has_any_grade].index
        graded_df         = tdf[tdf["student_id"].isin(graded_ids)].copy()
        if not graded_df.empty and "updated_at" in graded_df.columns:
            graded_df["updated_at"] = pd.to_datetime(graded_df["updated_at"], errors="coerce", utc=True)
            graded_df["days_since"] = (now.tz_localize("UTC") - graded_df["updated_at"]).dt.days
            latest_per_student = graded_df.groupby("student_id")["days_since"].min()
            stale_count = int((latest_per_student > 90).sum())
            avg_days    = round(latest_per_student.mean(), 1)
        else:
            stale_count = 0; avg_days = None
        per_student = tdf.groupby("student_id").apply(
            lambda g: g["score"].notna().sum() / len(g) * 100 if len(g) > 0 else 0)
        pct_graded = round(per_student.mean(), 1)
        rows.append({
            "tutor_name": tutor, "total_students": total_students,
            "students_no_grades": no_grade_students, "pct_subjects_graded": pct_graded,
            "stale_grade_students": stale_count, "avg_days_since_update": avg_days,
            "week_key": week_key, "week_date": week_date,
        })
    summary = pd.DataFrame(rows)
    updated = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
    gh_write(GRADES_SNAPSHOT_FILE, updated)
    return updated


def save_exams_weekly_snapshot(df):
    today     = pd.Timestamp.now()
    week_key  = today.strftime("%Y-W%V")
    week_date = (today - pd.to_timedelta(today.dayofweek, unit="d")).strftime("%Y-%m-%d")
    existing = gh_read(EXAMS_SNAPSHOT_FILE)
    if not existing.empty and week_key in existing["week_key"].values:
        return existing
    now = pd.Timestamp.now(tz="UTC")
    rows = []
    for tutor, tdf in df.groupby("tutor_name"):
        total_students = tdf["student_id"].nunique()
        eligible_ids   = tdf[tdf["attended_test_prep_hours"] >= 6]["student_id"].unique()
        no_exam_count  = 0; stale_exam_count = 0
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
            "tutor_name": tutor, "total_students": total_students,
            "students_no_exam": no_exam_count, "students_stale_exam": stale_exam_count,
            "pct_eligible_with_exam": pct_eligible, "week_key": week_key, "week_date": week_date,
        })
    summary = pd.DataFrame(rows)
    updated = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
    gh_write(EXAMS_SNAPSHOT_FILE, updated)
    return updated


def save_video_weekly_snapshot(video_summary_df, raw_video_df=None):
    """Save per-tutor weekly video metrics snapshot."""
    # Get week_date from raw video data (most reliable source)
    week_date = None
    if raw_video_df is not None and "week of" in raw_video_df.columns:
        vals = raw_video_df["week of"].dropna()
        if not vals.empty:
            week_date = pd.to_datetime(vals.iloc[0]).strftime("%Y-%m-%d")
    if week_date is None and "week of" in video_summary_df.columns:
        vals = video_summary_df["week of"].dropna()
        if not vals.empty:
            week_date = pd.to_datetime(vals.iloc[0]).strftime("%Y-%m-%d")
    if week_date is None:
        # Should never happen, but fallback to previous Sunday
        today = pd.Timestamp.now()
        days_back = (today.dayofweek + 1) % 7 or 7
        week_date = (today - pd.to_timedelta(days_back, unit="d")).strftime("%Y-%m-%d")
    # Use Monday of the week for ISO week_key (ISO weeks start Monday)
    # week_date is Sunday, so Monday = Sunday + 1 day
    week_key = (pd.Timestamp(week_date) + pd.Timedelta(days=1)).strftime("%Y-W%V")
    existing = gh_read(VIDEO_SNAPSHOT_FILE)
    summary = video_summary_df.copy()
    summary["week_key"]  = week_key
    summary["week_date"] = week_date
    if not existing.empty and week_key in existing["week_key"].values:
        # Always overwrite with latest data for this week
        existing = existing[existing["week_key"] != week_key]
    updated = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
    gh_write(VIDEO_SNAPSHOT_FILE, updated)
    return updated


def load_snapshots():
    df = gh_read(SNAPSHOT_FILE)
    if not df.empty and "week_date" in df.columns:
        df["week_date"] = pd.to_datetime(df["week_date"])
    return df

def load_grades_snapshots():
    df = gh_read(GRADES_SNAPSHOT_FILE)
    if not df.empty and "week_date" in df.columns:
        df["week_date"] = pd.to_datetime(df["week_date"])
    return df

def load_exams_snapshots():
    df = gh_read(EXAMS_SNAPSHOT_FILE)
    if not df.empty and "week_date" in df.columns:
        df["week_date"] = pd.to_datetime(df["week_date"])
    return df

def load_video_snapshots():
    df = gh_read(VIDEO_SNAPSHOT_FILE)
    if not df.empty and "week_date" in df.columns:
        df["week_date"] = pd.to_datetime(df["week_date"])
    return df

def save_progress_weekly_snapshot(score_summary_df, week_date=None):
    """Save per-tutor weekly progress update score snapshot."""
    if week_date is None:
        today = pd.Timestamp.now()
        days_back = (today.dayofweek + 1) % 7 or 7
        week_date = (today - pd.Timedelta(days=days_back)).strftime("%Y-%m-%d")
    week_key = (pd.Timestamp(week_date) + pd.Timedelta(days=1)).strftime("%Y-W%V")
    existing = gh_read(PROGRESS_SNAPSHOT_FILE)
    summary = score_summary_df.copy()
    summary["week_key"]  = week_key
    summary["week_date"] = week_date
    if not existing.empty and week_key in existing["week_key"].values:
        new_total = len(summary)
        old_total = len(existing[existing["week_key"] == week_key])
        if new_total <= old_total:
            return existing
        existing = existing[existing["week_key"] != week_key]
    updated = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
    gh_write(PROGRESS_SNAPSHOT_FILE, updated)
    return updated

def load_progress_snapshots():
    df = gh_read(PROGRESS_SNAPSHOT_FILE)
    if not df.empty and "week_date" in df.columns:
        df["week_date"] = pd.to_datetime(df["week_date"])
    return df


# ─────────────────────────────────────────────
# ANOMALY DETECTION
# ─────────────────────────────────────────────

def compute_anomalies(tutor_name, snap_arch, snap_grades, snap_exams, snap_video, current):
    flags = {}

    def _check(history_series, current_val, key, lower_is_better=True):
        clean = history_series.dropna()
        if len(clean) < 3 or current_val is None:
            flags[key] = False
            return
        mean, std = clean.mean(), clean.std()
        if std == 0:
            flags[key] = False
            return
        if lower_is_better:
            flags[key] = float(current_val) > mean + std
        else:
            flags[key] = float(current_val) < mean - std

    if not snap_arch.empty and tutor_name in snap_arch["tutor_name"].values:
        th = snap_arch[snap_arch["tutor_name"] == tutor_name].sort_values("week_date")
        _check(th["archivable_students"], current.get("arch_count"),  "arch_count")
        _check(th["unscheduled_hours"],   current.get("unsched_hrs"), "unsched_hrs")
    else:
        flags["arch_count"] = False; flags["unsched_hrs"] = False

    if not snap_grades.empty and tutor_name in snap_grades["tutor_name"].values:
        tg = snap_grades[snap_grades["tutor_name"] == tutor_name].sort_values("week_date")
        _check(tg["students_no_grades"],   current.get("no_grades"),   "no_grades")
        _check(tg["stale_grade_students"], current.get("stale_grades"), "stale_grades")
    else:
        flags["no_grades"] = False; flags["stale_grades"] = False

    if not snap_exams.empty and tutor_name in snap_exams["tutor_name"].values:
        te = snap_exams[snap_exams["tutor_name"] == tutor_name].sort_values("week_date")
        _check(te["students_no_exam"],    current.get("no_exam"),    "no_exam")
        _check(te["students_stale_exam"], current.get("stale_exams"),"stale_exams")
    else:
        flags["no_exam"] = False; flags["stale_exams"] = False

    if not snap_video.empty and tutor_name in snap_video["tutor_name"].values:
        tv = snap_video[snap_video["tutor_name"] == tutor_name].sort_values("week_date")
        _check(tv["pct_with_video"], current.get("pct_with_video"), "pct_with_video", lower_is_better=False)
    else:
        flags["pct_with_video"] = False

    return flags


# ─────────────────────────────────────────────
# FILE-BASED DATA LOADERS
# ─────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_dashboard_metrics():
    file = "Dashboard_Metrics.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetricFullData", header=3)
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_progressupdate_metrics():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="ProgressUpdateEmails", header=0)
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_tutor_concerns():
    file = "Tutor_Concerns.csv"
    if os.path.exists(file):
        try:
            return pd.read_csv(file)
        except Exception as e:
            st.warning(f"Could not read {file}: {e}")
            return pd.DataFrame()
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
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_grade_summary():
    file = "Ela_GradesSummary.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file)
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_concern_groupings():
    file = "Tutor_Concern_Groupings_Explanations_June2025.csv"
    if os.path.exists(file):
        return pd.read_csv(file)
    return pd.DataFrame()

def get_concern_movements(concerns_df, fl_name, min_change=2):
    """Get tutors who moved 2+ concern groups between latest and previous date."""
    if concerns_df.empty:
        return pd.DataFrame(), pd.DataFrame(), None, None
    import re
    def extract_end_date(range_str):
        if pd.isna(range_str): return pd.NaT
        clean_str = range_str.replace("-","to").replace("\u2013","to").replace("\u2014","to")
        parts = clean_str.split("to")
        if len(parts) < 2: return pd.NaT
        end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", parts[-1].strip())
        try:
            return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
        except:
            return pd.NaT
    fl_df = concerns_df[concerns_df["Faculty Leader Name"] == fl_name].copy()
    if fl_df.empty:
        return pd.DataFrame(), pd.DataFrame(), None, None
    fl_df["Date"] = fl_df["Date"].apply(extract_end_date)
    all_dates = sorted(fl_df["Date"].dropna().unique())
    if len(all_dates) < 2:
        return pd.DataFrame(), pd.DataFrame(), None, None
    latest_date = all_dates[-1]
    prev_date   = all_dates[-2]
    latest_df   = fl_df[fl_df["Date"] == latest_date]
    prev_df     = fl_df[fl_df["Date"] == prev_date]
    merged = latest_df[["Tutor Name","Concern Group","Reasons"]].merge(
        prev_df[["Tutor Name","Concern Group"]].rename(columns={"Concern Group":"Prev Group"}),
        on="Tutor Name", how="inner")
    merged["Change"] = (
        pd.to_numeric(merged["Concern Group"], errors="coerce") -
        pd.to_numeric(merged["Prev Group"],    errors="coerce"))
    big_moves   = merged[merged["Change"].abs() >= min_change].sort_values("Change")
    worsened    = big_moves[big_moves["Change"] > 0].sort_values("Change", ascending=False)
    improved    = big_moves[big_moves["Change"] < 0].sort_values("Change")
    return worsened, improved, prev_date, latest_date

@st.cache_data(ttl=60)
def load_monthly_metric_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_repurchases():
    file = "Repurchase_Summary_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="Sheet 1")
    return pd.DataFrame()

# FORCE_CACHE_MODE is set via sidebar toggle at runtime
FORCE_CACHE_MODE = False  # overridden below after sidebar renders

@st.cache_data(ttl=60)
def _gh_read_cache(path):
    """Load a cached CSV from GitHub data/cache/."""
    try:
        github_repo  = st.secrets["github"]["repo"]
        github_token = st.secrets["github"]["token"]
        ts   = int(pd.Timestamp.now().timestamp())
        url  = f"https://raw.githubusercontent.com/{github_repo}/main/{path}?cb={ts}"
        resp = _requests.get(url, headers={"Authorization": f"token {github_token}"}, timeout=15)
        if resp.status_code == 200 and resp.text.strip():
            return pd.read_csv(io.StringIO(resp.text))
    except Exception:
        pass
    return pd.DataFrame()

def load_master_tutor():
    """Load tutor roster live from Redshift."""
    conn = get_redshift_connection()
    query = """
        SELECT DISTINCT
            e1.id AS user_id,
            DATE(e1.hire_date) AS hire_date,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            fl_users.first_name||' '||fl_users.last_name AS faculty_leader,
            CASE WHEN e1.delivery_target < 30 THEN 'Adjunct' ELSE 'Professional' END AS tutor_type,
            dw.tiers.name AS tier
        FROM dw.employees e1
        JOIN dw.team_members ON dw.team_members.member_id = e1.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        JOIN dw.users tutor_users ON e1.user_id = tutor_users.id
        JOIN dw.employees e2 ON e2.id = dw.teams.manager_id
        JOIN dw.users fl_users ON e2.user_id = fl_users.id
        JOIN dw.tiers ON e1.tier_id = dw.tiers.id
        WHERE e1.type = 'Tutor'
        AND e1.end_date IS NULL
        AND e1.tier_id IS NOT NULL
        AND tutor_users.title = 'Tutor'
        ORDER BY tutor_name
    """
    if FORCE_CACHE_MODE:
        try: conn.close()
        except: pass
        df = _gh_read_cache("data/cache/master_tutor.csv")
        if not df.empty:
            df["Full Name"]      = df["tutor_name"]
            df["Faculty Leader"] = df["faculty_leader"]
            df["Tier"]           = df["tier"]
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Cache mode ON — using cached tutor roster from {cached_at}.")
            return df
        st.error("Cache mode ON but no cached tutor roster found.")
        return pd.DataFrame()
    try:
        df = pd.read_sql(query, conn)
        df["Full Name"]       = df["tutor_name"]
        df["Faculty Leader"]  = df["faculty_leader"]
        df["Tier"]            = df["tier"]
        return df
    except Exception as e:
        df = _gh_read_cache("data/cache/master_tutor.csv")
        if not df.empty:
            df["Full Name"]      = df["tutor_name"]
            df["Faculty Leader"] = df["faculty_leader"]
            df["Tier"]           = df["tier"]
            cached_at = df["_cached_at"].iloc[0] if "_cached_at" in df.columns else "unknown"
            st.warning(f"⚠️ Redshift unavailable — using cached tutor roster from {cached_at}.")
            return df
        st.error(f"Error loading tutor roster and no cache available: {e}")
        return pd.DataFrame()
    finally:
        try: conn.close()
        except: pass
    st.error(f"{file} not found")
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_subject_additions():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="SubjectAddition")
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_monthly_metric():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    return pd.DataFrame()

@st.cache_data(ttl=60)
def load_kpi_data():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    return pd.DataFrame()


# ─────────────────────────────────────────────
# WATCH LIST HELPERS
# ─────────────────────────────────────────────

WATCHLIST_FILE            = "katherine_watchlist.csv"
WATCHLIST_BASELINE_FILE   = "katherine_watchlist_baselines.csv"
WATCHLIST_NOTES_FILE      = "katherine_watchlist_notes.csv"
WATCHLIST_THRESHOLDS_FILE = "katherine_watchlist_thresholds.csv"

DEFAULT_THRESHOLDS = {
    "arch_count":      1,
    "unsched_hrs":     0,
    "pct_unscheduled": 10,
    "no_grades":       1,
    "stale_grades":    1,
    "no_exam":         1,
    "stale_exams":     1,
    "pct_with_video":  80,
}

def load_watchlist():
    df = gh_read(WATCHLIST_FILE)
    if not df.empty and "tutor_name" in df.columns:
        return df["tutor_name"].dropna().tolist()
    return []

def save_watchlist(tutor_names):
    gh_write(WATCHLIST_FILE, pd.DataFrame({"tutor_name": tutor_names}))

def load_watchlist_baselines():
    return gh_read(WATCHLIST_BASELINE_FILE)

def save_watchlist_baseline(tutor_name, arch_count, unsched_hrs, no_grades, stale_grades,
                             no_exam, stale_exams, hours_per_exam, pct_unscheduled,
                             pct_with_video=None):
    today    = pd.Timestamp.now().strftime("%Y-%m-%d")
    existing = load_watchlist_baselines()
    all_cols = {
        "arch_count": arch_count, "unsched_hrs": unsched_hrs,
        "no_grades": no_grades, "stale_grades": stale_grades,
        "no_exam": no_exam, "stale_exams": stale_exams,
        "hours_per_exam": hours_per_exam, "pct_unscheduled": pct_unscheduled,
        "pct_with_video": pct_with_video,
    }
    if not existing.empty and tutor_name in existing["tutor_name"].values:
        idx = existing[existing["tutor_name"] == tutor_name].index[0]
        patched = False
        for col, val in all_cols.items():
            if col not in existing.columns:
                existing[col] = None; patched = True
            cur = existing.at[idx, col]
            is_null = (cur is None) or (str(cur).strip() in ("", "nan", "None")) or \
                      (isinstance(cur, float) and pd.isna(cur))
            if is_null and val is not None and not (isinstance(val, float) and pd.isna(val)):
                existing.at[idx, col] = val; patched = True
        if patched:
            gh_write(WATCHLIST_BASELINE_FILE, existing)
        return
    new_row = pd.DataFrame([{"tutor_name": tutor_name, "added_date": today, **all_cols}])
    updated = pd.concat([existing, new_row], ignore_index=True) if not existing.empty else new_row
    gh_write(WATCHLIST_BASELINE_FILE, updated)

def remove_watchlist_baseline(tutor_name):
    existing = load_watchlist_baselines()
    if not existing.empty:
        gh_write(WATCHLIST_BASELINE_FILE, existing[existing["tutor_name"] != tutor_name])

def migrate_watchlist_baselines(current_metrics: dict):
    expected_cols = ["arch_count","unsched_hrs","no_grades","stale_grades",
                     "no_exam","stale_exams","hours_per_exam","pct_unscheduled","pct_with_video"]
    existing = load_watchlist_baselines()
    if existing.empty:
        return
    changed = False
    for col in expected_cols:
        if col not in existing.columns:
            existing[col] = None; changed = True
    for idx, row in existing.iterrows():
        tname   = row["tutor_name"]
        metrics = current_metrics.get(tname, {})
        for col in expected_cols:
            val = existing.at[idx, col]
            is_missing = (val is None) or (isinstance(val, float) and pd.isna(val))
            if is_missing and col in metrics:
                existing.at[idx, col] = metrics[col]; changed = True
    if changed:
        gh_write(WATCHLIST_BASELINE_FILE, existing)

def load_watchlist_notes():
    df = gh_read(WATCHLIST_NOTES_FILE)
    return df if not df.empty else pd.DataFrame(columns=["tutor_name","note","updated_at"])

def save_watchlist_note(tutor_name, note):
    existing = load_watchlist_notes()
    existing = existing[existing["tutor_name"] != tutor_name]
    new_row  = pd.DataFrame([{"tutor_name": tutor_name, "note": note,
                               "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}])
    gh_write(WATCHLIST_NOTES_FILE, pd.concat([existing, new_row], ignore_index=True))

def delete_watchlist_note(tutor_name):
    existing = load_watchlist_notes()
    if not existing.empty:
        gh_write(WATCHLIST_NOTES_FILE, existing[existing["tutor_name"] != tutor_name])

def load_watchlist_thresholds():
    df = gh_read(WATCHLIST_THRESHOLDS_FILE)
    return df if not df.empty else pd.DataFrame(columns=["tutor_name"] + list(DEFAULT_THRESHOLDS.keys()))

def save_watchlist_thresholds(tutor_name, thresholds_dict):
    existing = load_watchlist_thresholds()
    existing = existing[existing["tutor_name"] != tutor_name]
    gh_write(WATCHLIST_THRESHOLDS_FILE,
             pd.concat([existing, pd.DataFrame([{"tutor_name": tutor_name, **thresholds_dict}])],
                       ignore_index=True))

def get_tutor_thresholds(tutor_name):
    df = load_watchlist_thresholds()
    if not df.empty and tutor_name in df["tutor_name"].values:
        row = df[df["tutor_name"] == tutor_name].iloc[0].to_dict()
        return {k: row.get(k, DEFAULT_THRESHOLDS[k]) for k in DEFAULT_THRESHOLDS}
    return DEFAULT_THRESHOLDS.copy()

def delete_watchlist_thresholds(tutor_name):
    existing = load_watchlist_thresholds()
    if not existing.empty:
        existing[existing["tutor_name"] != tutor_name].to_csv(WATCHLIST_THRESHOLDS_FILE, index=False)


# ─────────────────────────────────────────────
# LOGIN SNAPSHOT HELPERS
# ─────────────────────────────────────────────

LOGIN_SNAPSHOT_FILE = "katherine_login_snapshot.csv"
LAST_SEEN_FILE      = "katherine_last_seen_data.csv"

def save_login_snapshot(arch_df, grades_df, exam_df, video_summary_df=None):
    team_archivable  = int(arch_df["should_archive"].sum()) if not arch_df.empty else 0
    team_unscheduled = float(arch_df["unscheduled_hours"].sum()) if not arch_df.empty else 0.0
    if not grades_df.empty:
        team_no_grades = int(grades_df.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
        has_any   = grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded    = grades_df[grades_df["student_id"].isin(has_any[has_any].index)]
        if not graded.empty and "days_since_update" in graded.columns:
            latest = graded.groupby("student_id")["days_since_update"].min()
            team_stale_grades = int((latest > 90).sum())
        else:
            team_stale_grades = 0
    else:
        team_no_grades = 0; team_stale_grades = 0
    if not exam_df.empty:
        valid_exam_ids = exam_df[exam_df["exam_valid_composite"] == True]["student_id"].unique() \
                         if "exam_valid_composite" in exam_df.columns else []
        all_ids = exam_df["student_id"].unique() if "student_id" in exam_df.columns else []
        team_no_exam = int(len(set(all_ids) - set(valid_exam_ids)))
        if "exam_date" in exam_df.columns:
            exam_df2 = exam_df.copy()
            exam_df2["exam_date"] = pd.to_datetime(exam_df2["exam_date"], errors="coerce", utc=True)
            now = pd.Timestamp.now(tz="UTC")
            latest_exam = exam_df2.groupby("student_id")["exam_date"].max()
            team_stale_exams = int(((now - latest_exam).dt.days > 90).sum())
        else:
            team_stale_exams = 0
    else:
        team_no_exam = 0; team_stale_exams = 0
    team_pct_video = None
    if video_summary_df is not None and not video_summary_df.empty:
        total_sent  = video_summary_df["updates_sent"].sum()
        total_found = video_summary_df["videos_found"].sum()
        team_pct_video = round(total_found / total_sent * 100, 1) if total_sent > 0 else None
    snap = pd.DataFrame([{
        "team_archivable":      team_archivable,
        "team_unscheduled_hrs": round(team_unscheduled, 1),
        "team_no_grades":       team_no_grades,
        "team_stale_grades":    team_stale_grades,
        "team_no_exam":         team_no_exam,
        "team_stale_exams":     team_stale_exams,
        "team_pct_video":       team_pct_video,
        "login_ts":             pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
    }])
    gh_write(LOGIN_SNAPSHOT_FILE, snap)
    return snap

def load_login_snapshot():
    return gh_read(LOGIN_SNAPSHOT_FILE)

def _login_delta_html(label, current, previous, lower_is_better=True):
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


def generate_tutor_pdf(
    tutor_name, generated_date,
    p_arch=None, p_grades=None, p_exam=None,
    p_video_row=None, p_video_df=None,
    p_monthly_t=None, p_kpi_df=None,
    concern_df=None, faculty_leader=None,
    inc_arch=True, inc_grades=True, inc_exams=True,
    inc_video=True, inc_kpi=True, inc_concern=True,
    inc_scores=True, p_scores_df=None,
):
    """Generate a PDF report for a tutor profile page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                     TableStyle, HRFlowable, KeepTogether)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    import io

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
                            rightMargin=0.5*inch, leftMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)

    styles = getSampleStyleSheet()
    title_style   = ParagraphStyle("title",   parent=styles["Heading1"],
                                   fontSize=18, spaceAfter=4, textColor=colors.HexColor("#1a3a5c"))
    h2_style      = ParagraphStyle("h2",      parent=styles["Heading2"],
                                   fontSize=13, spaceAfter=4, textColor=colors.HexColor("#2c5f8a"))
    h3_style      = ParagraphStyle("h3",      parent=styles["Heading3"],
                                   fontSize=11, spaceAfter=2, textColor=colors.HexColor("#444"))
    normal_style  = ParagraphStyle("normal",  parent=styles["Normal"], fontSize=9)
    caption_style = ParagraphStyle("caption", parent=styles["Normal"], fontSize=8,
                                   textColor=colors.gray, alignment=TA_CENTER)

    def section_divider():
        return [Spacer(1, 8), HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#ccc")),
                Spacer(1, 8)]

    def make_legend(items):
        """items = list of (color_hex, label) tuples"""
        from reportlab.platypus import HRFlowable
        legend_data = [[
            Table([[""]], colWidths=[0.15*inch], rowHeights=[0.15*inch],
                  style=TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor(c)),
                                    ("BOX",(0,0),(-1,-1),0.5,colors.HexColor("#999"))])),
            Paragraph(label, ParagraphStyle("leg", parent=getSampleStyleSheet()["Normal"],
                                            fontSize=7, textColor=colors.HexColor("#555")))
        ] for c, label in items]
        leg_table = Table(legend_data, colWidths=[0.2*inch, 1.5*inch])
        leg_table.setStyle(TableStyle([
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ("PADDING",(0,0),(-1,-1),2),
        ]))
        return leg_table

    def make_table(data, col_widths=None, header=True):
        if not data or len(data) < 1:
            return None
        t = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
        style = [
            ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
            ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
            ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f8fc")]),
            ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
            ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
            ("PADDING",     (0,0), (-1,-1), 4),
        ] if header else [
            ("FONTSIZE",    (0,0), (-1,-1), 8),
            ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
            ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
            ("PADDING",     (0,0), (-1,-1), 4),
        ]
        t.setStyle(TableStyle(style))
        return t

    story = []

    # ── Title ─────────────────────────────────────────────────────────────
    story.append(Paragraph(f"Tutor Profile: {tutor_name}", title_style))
    story.append(Paragraph(f"Faculty Leader: {faculty_leader or '—'} &nbsp;|&nbsp; Generated: {generated_date}",
                           caption_style))
    story.extend(section_divider())

    if inc_arch:
        # ── Archivable & Unscheduled ───────────────────────────────────────────
        if inc_arch:
         story.append(Paragraph("📦 Archivable Students & Unscheduled Hours", h2_style))
        if p_arch is not None and not p_arch.empty:
            arch_students = p_arch[p_arch["should_archive"] == True]
            n_arch        = len(arch_students)
            unsched_total = round(p_arch["unscheduled_hours"].sum(), 1)
            total_students= p_arch["student_name"].nunique()
            total_prov    = p_arch["hours_remaining"].sum() + p_arch["unscheduled_hours"].sum()
            pct_unsched   = round(p_arch["unscheduled_hours"].sum() / total_prov * 100, 1)                         if total_prov > 0 else 0.0
            summary_data = [
                ["Active Students", "Archivable", "Unscheduled Hours", "% Hours Unscheduled"],
                [str(total_students), str(n_arch), f"{unsched_total:.1f}", f"{pct_unsched:.1f}%"]
            ]
            t = make_table(summary_data)
            if t: story.append(t)
            if not arch_students.empty:
                story.append(Spacer(1, 6))
                story.append(Paragraph("Students flagged for archiving:", h3_style))
                show_cols = [c for c in ["student_name","brand","hours_remaining","unscheduled_hours"]
                             if c in arch_students.columns]
                arch_sorted = arch_students[show_cols].sort_values("unscheduled_hours", ascending=False)
                rows = [show_cols] + arch_sorted.fillna("—").values.tolist()
                t2 = Table([[str(v) for v in r] for r in rows], repeatRows=1)
                style_cmds = [
                    ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
                    ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                    ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                    ("FONTSIZE",    (0,0), (-1,-1), 8),
                    ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
                    ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
                    ("PADDING",     (0,0), (-1,-1), 4),
                ]
                # All archivable rows are red
                for i in range(1, len(rows)):
                    style_cmds.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#ffe5e5")))
                t2.setStyle(TableStyle(style_cmds))
                story.append(t2)
            story.append(Spacer(1, 4))
            story.append(make_legend([("#ffe5e5", "Flagged for archiving")]))
        else:
            story.append(Paragraph("No archivable/unscheduled data found.", normal_style))
        story.extend(section_divider())

    if inc_grades:
        # ── Grades ────────────────────────────────────────────────────────────
        if inc_grades:
         story.append(Paragraph("📚 Grades Summary", h2_style))
        if p_grades is not None and not p_grades.empty:
            total_g   = p_grades["student_id"].nunique()
            no_grades = int(p_grades.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
            has_any   = p_grades.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded    = p_grades[p_grades["student_id"].isin(has_any[has_any].index)]
            n_stale   = 0
            if not graded.empty and "days_since_update" in graded.columns:
                latest_per = graded.groupby("student_id")["days_since_update"].min()
                n_stale    = int((latest_per > 90).sum())
            summary_data = [
                ["Total Students", "No Grades", "Stale Grades (>90d)"],
                [str(total_g), str(no_grades), str(n_stale)]
            ]
            t = make_table(summary_data)
            if t: story.append(t)
            story.append(Spacer(1, 6))
            grade_rows = []
            grade_row_colors = []
            for sid, sdf in p_grades.groupby("student_id"):
                sname    = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else str(sid)
                subjects = sdf["subject"].nunique() if "subject" in sdf.columns else 0
                n_entered= int(sdf["score"].notna().sum())
                days     = sdf["days_since_update"].min() if n_entered > 0 else None
                if n_entered == 0:
                    status = "No Grades"
                    grade_row_colors.append(colors.HexColor("#ffe5e5"))
                elif days is not None and days > 90:
                    status = f"Stale ({int(days)}d)"
                    grade_row_colors.append(colors.HexColor("#fffbea"))
                else:
                    status = f"Current ({int(days)}d)" if days is not None else "—"
                    grade_row_colors.append(colors.white)
                grade_rows.append([sname, str(subjects), str(n_entered), status])
            if grade_rows:
                # Sort: no grades first, then stale, then current, alpha within groups
                _gs_order = {"No Grades": 0, "Stale": 1}
                grade_combined = list(zip(grade_rows, grade_row_colors))
                grade_combined.sort(key=lambda x: (
                    next((v for k,v in _gs_order.items() if k in x[0][-1]), 2),
                    x[0][0].lower()))
                grade_rows       = [g[0] for g in grade_combined]
                grade_row_colors = [g[1] for g in grade_combined]
                header = [["Student", "Subjects", "Grades Entered", "Status"]]
                rows   = header + grade_rows
                t2 = Table(rows, repeatRows=1)
                style_cmds = [
                    ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
                    ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                    ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                    ("FONTSIZE",    (0,0), (-1,-1), 8),
                    ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
                    ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
                    ("PADDING",     (0,0), (-1,-1), 4),
                ]
                for i, bg in enumerate(grade_row_colors):
                    style_cmds.append(("BACKGROUND", (0, i+1), (-1, i+1), bg))
                t2.setStyle(TableStyle(style_cmds))
                story.append(t2)
            story.append(Spacer(1, 4))
            story.append(make_legend([
                ("#ffe5e5", "No grades entered"),
                ("#fffbea", "Stale grades (>90 days)"),
                ("#ffffff", "Current"),
            ]))
        else:
            story.append(Paragraph("No grades data found.", normal_style))
        story.extend(section_divider())

    if inc_exams:
        # ── Exams ─────────────────────────────────────────────────────────────
        if inc_exams:
         story.append(Paragraph("📝 Exam & Test Prep History", h2_style))
        if p_exam is not None and not p_exam.empty:
            p_now      = pd.Timestamp.now(tz="UTC")
            ex_students= p_exam["student_id"].nunique()
            total_hrs  = p_exam["attended_test_prep_hours"].iloc[0] if not p_exam.empty else 0
            n_completed= p_exam[p_exam["exam_valid_composite"] == True]["exam_id"].nunique()                      if "exam_id" in p_exam.columns else 0
            hrs_per    = round(total_hrs / n_completed, 1) if n_completed > 0 and pd.notna(total_hrs) else None
            summary_data = [
                ["Test Prep Students", "Completed Exams", "Total Hours", "Avg Hrs/Exam"],
                [str(ex_students), str(n_completed),
                 f"{float(total_hrs):.1f}" if pd.notna(total_hrs) else "—",
                 f"{hrs_per:.1f}" if hrs_per else "—"]
            ]
            t = make_table(summary_data)
            if t: story.append(t)
            story.append(Spacer(1, 6))
            ex_rows = []
            ex_row_colors = []
            for sid, sdf in p_exam.groupby("student_id"):
                sname    = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else str(sid)
                hrs      = sdf["attended_test_prep_hours"].iloc[0]
                valid    = sdf[sdf["exam_valid_composite"].astype(str) == "True"]
                best     = valid["score"].max() if not valid.empty else None
                valid_dated = valid[valid["exam_date"].notna()] if not valid.empty else valid
                latest   = pd.to_datetime(valid_dated["exam_date"], utc=True).max() if not valid_dated.empty else None
                days_ago = int((p_now - latest).days) if latest is not None and pd.notna(latest) else None
                if valid.empty and pd.notna(hrs) and float(hrs) >= 6:
                    status = "No Exam (6+ hrs)"
                    ex_row_colors.append(colors.HexColor("#ffe5e5"))
                elif not valid.empty and (days_ago is None or days_ago > 90):
                    status = f"Stale ({days_ago}d)" if days_ago else "Stale"
                    ex_row_colors.append(colors.HexColor("#fffbea"))
                elif not valid.empty and days_ago is not None and days_ago <= 90:
                    status = f"Current ({days_ago}d)"
                    ex_row_colors.append(colors.white)
                else:
                    status = "—"
                    ex_row_colors.append(colors.white)
                ex_rows.append([sname,
                                f"{float(hrs):.1f}" if pd.notna(hrs) else "—",
                                str(len(valid)),
                                str(int(best)) if pd.notna(best) else "—",
                                status])
            if ex_rows:
                # Sort: no exam first, then stale, then current, alpha within groups
                _es_order = {"No Exam": 0, "Stale": 1, "Current": 2}
                ex_combined = list(zip(ex_rows, ex_row_colors))
                ex_combined.sort(key=lambda x: (
                    next((v for k,v in _es_order.items() if k in x[0][-1]), 3),
                    x[0][0].lower()))
                ex_rows       = [e[0] for e in ex_combined]
                ex_row_colors = [e[1] for e in ex_combined]
                header = [["Student","Hours","Valid Exams","Best Score","Status"]]
                rows   = header + ex_rows
                t2 = Table(rows, repeatRows=1)
                style_cmds = [
                    ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
                    ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                    ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                    ("FONTSIZE",    (0,0), (-1,-1), 8),
                    ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
                    ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
                    ("PADDING",     (0,0), (-1,-1), 4),
                ]
                for i, bg in enumerate(ex_row_colors):
                    style_cmds.append(("BACKGROUND", (0, i+1), (-1, i+1), bg))
                t2.setStyle(TableStyle(style_cmds))
                story.append(t2)
            story.append(Spacer(1, 4))
            story.append(make_legend([
                ("#ffe5e5", "No exam (6+ hours tutoring)"),
                ("#fffbea", "Stale exam (>90 days)"),
                ("#ffffff", "Current or < 6 hrs tutoring"),
            ]))
        story.extend(section_divider())

    if inc_video:
        # ── Video ─────────────────────────────────────────────────────────────
        if inc_video:
         story.append(Paragraph("📹 Parent Update Videos", h2_style))
        if p_video_row is not None:
            summary_data = [
                ["Required", "Sent", "Parent Update %", "Videos Found", "Video %", "Median Duration"],
                [str(int(p_video_row.get("updates_required", 0))),
                 str(int(p_video_row.get("updates_sent", 0))),
                 f"{p_video_row.get('parent_update_pct', 0):.0f}%",
                 str(int(p_video_row.get("videos_found", 0))),
                 f"{p_video_row.get('pct_with_video', 0):.0f}%",
                 str(p_video_row.get("median_secs", "—"))]
            ]
            t = make_table(summary_data)
            if t: story.append(t)
            if p_video_df is not None and not p_video_df.empty:
                story.append(Spacer(1, 6))
                vid_cols = [c for c in ["student","brand","parent update sent","parent update only sent","video found","video duration"]
                            if c in p_video_df.columns]
                # Show all rows — color code by status
                vid_display_cols = [c for c in ["student","brand","parent update sent","video found","video duration"]
                                     if c in p_video_df.columns]
                vid_rows = []
                vid_row_colors = []
                for _, vrow in p_video_df.iterrows():
                    sent     = str(vrow.get("parent update sent","")).strip()
                    pu_only  = str(vrow.get("parent update only sent","")).strip()
                    vid      = str(vrow.get("video found","")).strip()
                    if sent != "True":
                        vid_row_colors.append(colors.HexColor("#ffe5e5"))  # red — not sent
                    elif pu_only == "True" and vid != "True":
                        vid_row_colors.append(colors.HexColor("#fffbea"))  # yellow — no video
                    else:
                        vid_row_colors.append(colors.white)
                    vid_rows.append([str(vrow.get(c,"—")) for c in vid_display_cols])
                if vid_rows:
                    header = [vid_display_cols]
                    rows   = header + vid_rows
                    t2 = Table([[str(v) for v in r] for r in rows], repeatRows=1)
                    style_cmds = [
                        ("BACKGROUND",  (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
                        ("TEXTCOLOR",   (0,0), (-1,0),  colors.white),
                        ("FONTNAME",    (0,0), (-1,0),  "Helvetica-Bold"),
                        ("FONTSIZE",    (0,0), (-1,-1), 8),
                        ("GRID",        (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
                        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
                        ("PADDING",     (0,0), (-1,-1), 4),
                    ]
                    for i, bg in enumerate(vid_row_colors):
                        style_cmds.append(("BACKGROUND", (0, i+1), (-1, i+1), bg))
                    t2.setStyle(TableStyle(style_cmds))
                    story.append(t2)
            story.append(Spacer(1, 4))
            story.append(make_legend([
                ("#ffe5e5", "Parent update not sent"),
                ("#fffbea", "Sent but no video attached"),
                ("#ffffff", "Sent with video"),
            ]))
        else:
            story.append(Paragraph("No video data found.", normal_style))
        story.extend(section_divider())

    if inc_scores:
        # ── Progress Update Quality Scores ────────────────────────────────────
        story.append(Paragraph("📝 Progress Update Quality Scores", h2_style))
        if p_scores_df is not None and not p_scores_df.empty:
            _ps = p_scores_df.copy()
            _ps["sent_at"] = pd.to_datetime(_ps["sent_at"], errors="coerce")
            avg_total    = _ps["total"].mean()
            avg_worked   = _ps["what_worked_on"].mean()
            avg_goals    = _ps["goals"].mean()
            avg_velocity = _ps["velocity"].mean()
            avg_plan     = _ps["plan_forward"].mean()
            summary_data = [
                ["# Updates", "Avg Total (/10)", "Worked On (/2)", "Goals (/2)", "Velocity (/3)", "Plan (/3)"],
                [str(len(_ps)), f"{avg_total:.1f}", f"{avg_worked:.1f}",
                 f"{avg_goals:.1f}", f"{avg_velocity:.1f}", f"{avg_plan:.1f}"]
            ]
            t = make_table(summary_data)
            if t: story.append(t)
            story.append(Spacer(1, 6))
            # Individual updates
            _ps_rows = []
            for _, row in _ps.sort_values("sent_at", ascending=False).head(20).iterrows():
                _ps_rows.append([
                    str(row["sent_at"].strftime("%Y-%m-%d") if pd.notna(row["sent_at"]) else "—"),
                    str(row.get("student_name","—")),
                    str(int(row.get("total", 0))),
                    str(int(row.get("what_worked_on", 0))),
                    str(int(row.get("goals", 0))),
                    str(int(row.get("velocity", 0))),
                    str(int(row.get("plan_forward", 0))),
                ])
            if _ps_rows:
                _ps_table = [["Date","Student","Total","Worked On","Goals","Velocity","Plan"]] + _ps_rows
                t3 = Table(_ps_table, repeatRows=1)
                _ps_colors = []
                for row in _ps_rows:
                    score = int(row[2]) if row[2].isdigit() else 0
                    if score < 5:   _ps_colors.append(colors.HexColor("#ffe5e5"))
                    elif score < 7: _ps_colors.append(colors.HexColor("#fffbea"))
                    else:           _ps_colors.append(colors.white)
                style_cmds = [
                    ("BACKGROUND", (0,0), (-1,0),  colors.HexColor("#2c5f8a")),
                    ("TEXTCOLOR",  (0,0), (-1,0),  colors.white),
                    ("FONTNAME",   (0,0), (-1,0),  "Helvetica-Bold"),
                    ("FONTSIZE",   (0,0), (-1,-1), 8),
                    ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#ddd")),
                    ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
                    ("PADDING",    (0,0), (-1,-1), 4),
                ]
                for i, bg in enumerate(_ps_colors):
                    style_cmds.append(("BACKGROUND", (0, i+1), (-1, i+1), bg))
                t3.setStyle(TableStyle(style_cmds))
                story.append(t3)
            story.append(Spacer(1, 4))
            story.append(make_legend([
                ("#ffe5e5", "Total score < 5"),
                ("#fffbea", "Total score 5–6"),
                ("#ffffff", "Total score 7+"),
            ]))
        else:
            story.append(Paragraph("No progress update scores found.", normal_style))
        story.extend(section_divider())

    if inc_kpi:
        # ── KPI Trends ────────────────────────────────────────────────────────
        if inc_kpi:
         story.append(Paragraph("📈 KPI Trends", h2_style))
        if p_monthly_t is not None and not p_monthly_t.empty:
            import re as _re2
            def _parse_end(s):
                if pd.isna(s): return pd.NaT
                s2 = str(s).replace("-","to").replace("\u2013","to")
                parts = s2.split("to")
                end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                return pd.to_datetime(end, errors="coerce", dayfirst=False)
            kpi_t = p_monthly_t.copy()
            kpi_t["Date Parsed"] = kpi_t["Date Range"].apply(_parse_end)
            kpi_t = kpi_t.dropna(subset=["Date Parsed"]).drop_duplicates(
                subset=["Date Parsed"], keep="first").sort_values("Date Parsed").tail(8)
            kpi_metrics = ["% to Delivery Target","% to Availability Target",
                           "% Sessions on Time","% Parents Updates Done on Time"]
            kpi_cols_avail = ["Date Range"] + [m for m in kpi_metrics if m in kpi_t.columns]
            kpi_display = kpi_t[kpi_cols_avail].copy()
            for m in kpi_metrics:
                if m in kpi_display.columns:
                    kpi_display[m] = kpi_display[m].apply(
                        lambda v: f"{v*100:.0f}%" if pd.notna(v) else "—")
            rows = [kpi_cols_avail] + kpi_display.fillna("—").values.tolist()
            t = make_table([[str(v) for v in r] for r in rows])
            if t: story.append(t)
        else:
            story.append(Paragraph("No KPI data found.", normal_style))

        # ── Concern History ───────────────────────────────────────────────────
    if inc_concern and concern_df is not None and not concern_df.empty:
        story.extend(section_divider())
        story.append(Paragraph("📌 Concern Group History", h2_style))
        tutor_concerns = concern_df[concern_df["Tutor Name"] == tutor_name].copy()
        if not tutor_concerns.empty:
            import re as _re3
            def _parse_concern_date(s):
                if pd.isna(s): return pd.NaT
                s2 = str(s).replace("-","to").replace("\u2013","to")
                parts = s2.split("to")
                end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                return pd.to_datetime(end, errors="coerce", dayfirst=False)
            tutor_concerns["Date Parsed"] = tutor_concerns["Date"].apply(_parse_concern_date)
            tutor_concerns = tutor_concerns.sort_values("Date Parsed").tail(8)
            rows = [["Date","Concern Group","Reasons"]]
            for _, row in tutor_concerns.iterrows():
                rows.append([str(row.get("Date","—")),
                            str(row.get("Concern Group","—")),
                            str(row.get("Reasons","—"))[:80]])
            t = make_table([[str(v) for v in r] for r in rows],
                           col_widths=[1.5*inch, 1*inch, 5*inch])
            if t: story.append(t)
        else:
            story.append(Paragraph("No concern data found for this tutor.", normal_style))

    doc.build(story)
    buf.seek(0)
    return buf.getvalue()

def render_app(config):

    st.markdown("""
        <style>
            .main-title { font-size: 2.5em; font-weight: bold; color: #004466; margin-bottom: 0.3em; }
            .block-container { padding-top: 2rem; }
            section[data-testid="stSidebar"] { background-color: #F1F3F5; }
            .metric-label { font-size: 1.1rem; color: #666; }
        </style>
    """, unsafe_allow_html=True)

    grade_summary_df     = load_grade_summary()
    concern_groupings_df = load_concern_groupings()

    st.markdown('<div class="main-title">Katherine Tutor Data 📊</div>', unsafe_allow_html=True)
    st.sidebar.markdown("---")

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
        "📹 Parent Update Videos",
        "📝 Progress Update Quality Scores",
        "Archivable Students & Unscheduled Hours",
        "📋 Annual Reviews",
        "🔰 90-Day Review",
        "📄 PPW Report (Tableau)",
        "📊 Progress Updates (Tableau)",
        "⭐ NPS Scores (Tableau)",
    ]
    _default_index = _page_options.index(_goto) if _goto in _page_options else 0
    with st.sidebar.expander("⚙️ Admin", expanded=False):
        import sys as _sys
        _mod = _sys.modules[__name__]
        _prev_force = st.session_state.get("_cache_mode_prev", False)
        _force = st.checkbox("🔴 Use cached data (fallback mode)", value=_prev_force,
                             help="Forces dashboard to load from GitHub cache instead of live Redshift. Use when DB is down.")
        # If toggle changed, clear all cached data so functions re-run with new mode
        if _force != _prev_force:
            st.cache_data.clear()
            st.session_state["_cache_mode_prev"] = _force
            st.rerun()
        _mod.FORCE_CACHE_MODE = _force
        if _force:
            st.warning("⚠️ Fallback mode ON — ALL data loading from GitHub cache.")

    page = st.sidebar.radio("\U0001f4c2 Navigation", _page_options, index=_default_index)

    faculty_leader_name = "Katherine Marino"
    master_tutor_df = load_master_tutor()
    annelies_tutors = master_tutor_df[(master_tutor_df["Faculty Leader"] == faculty_leader_name) & (~master_tutor_df["Full Name"].isin(["Katherine Marino', 'Annelies de Groot', 'Ian Plamondon', 'Geoff St. Marie', 'Kristin Haase-Alvey"]))]["Full Name"].sort_values().dropna().unique().tolist()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📋 Annual Reviews")

    annual_review_df = load_annual_reviews()
    monthly_metric_annual_review_df = load_monthly_metric_annual_reviews()
    repurchase_df = load_repurchases()
    annelies_tutors = master_tutor_df[(master_tutor_df["Faculty Leader"] == "Katherine Marino") & (~master_tutor_df["Full Name"].isin(["Katherine Marino', 'Annelies de Groot', 'Ian Plamondon', 'Geoff St. Marie', 'Kristin Haase-Alvey"]))]["Full Name"].dropna().sort_values().tolist()

    # ─────────────────────────────────────────────
    # PAGE: HOME
    # ─────────────────────────────────────────────

    if page == "🏠 Home":
        st.markdown('<div class="main-title">Good morning, Katherine 👋</div>', unsafe_allow_html=True)
        st.caption("Here's what needs your attention today.")
        _digest_placeholder = st.empty()

        load_errors = []
        with st.spinner("Loading your briefing..."):

            try:
                raw_arch_df, arch_fetched_at = load_archivable_unscheduled()
                raw_arch_df["should_archive"] = raw_arch_df["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                home_arch_df = raw_arch_df[raw_arch_df["team_name"] == "Team Marino"].copy()
            except Exception as e:
                home_arch_df = pd.DataFrame(); load_errors.append(f"Archivable data: {e}")

            try:
                raw_home_grades, _ = load_grades_data()
                home_grades_df = raw_home_grades[raw_home_grades["team_name"] == "Team Marino"].copy()
                now_utc = pd.Timestamp.now(tz="UTC")
                home_grades_df["updated_at"] = pd.to_datetime(home_grades_df["updated_at"], errors="coerce", utc=True)
                home_grades_df["days_since_update"] = (now_utc - home_grades_df["updated_at"]).dt.days
            except Exception as e:
                home_grades_df = pd.DataFrame(); load_errors.append(f"Grades data: {e}")

            try:
                raw_home_exam, _ = load_exam_data()
                home_exam_df = raw_home_exam[raw_home_exam["team_name"] == "Team Marino"].copy()
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    home_exam_df[dc] = pd.to_datetime(home_exam_df[dc], errors="coerce", utc=True)
                for nc in ["score","act_english","act_math","act_reading","act_science",
                           "sat_math","sat_rw","attended_test_prep_hours"]:
                    home_exam_df[nc] = pd.to_numeric(home_exam_df[nc], errors="coerce")
                if not home_exam_df.empty:
                    _SAT_H = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT","Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                    _ACT_H = {"ACT","Digital ACT"}
                    home_exam_df["exam_family"] = home_exam_df["subject"].apply(
                        lambda x: "SAT/PSAT" if x in _SAT_H else ("ACT" if x in _ACT_H else "Other"))
                    home_exam_df["exam_valid_composite"] = home_exam_df.apply(
                        lambda r: (pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")) and (
                                  (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                                   pd.notna(r["sat_rw"])   and r["sat_rw"]   >= 300)
                                  if r["exam_family"] == "SAT/PSAT"
                                  else ((pd.notna(r["act_english"]) and r["act_english"] >= 10 and
                                         pd.notna(r["act_math"])    and r["act_math"]    >= 10 and
                                         pd.notna(r["act_reading"]) and r["act_reading"] >= 10)
                                        if r["exam_family"] == "ACT" else False)), axis=1)
            except Exception as e:
                home_exam_df = pd.DataFrame(); load_errors.append(f"Exam data: {e}")

            try:
                kpi_home_df = load_kpi_data()
            except Exception as e:
                kpi_home_df = pd.DataFrame(); load_errors.append(f"KPI data: {e}")

            try:
                avail_df = load_availability_compliance()
                team_avail_df = avail_df[avail_df["team"] == "Team Marino"].copy()
                if not team_avail_df.empty:
                    team_avail_df["week_start"] = pd.to_datetime(team_avail_df["week_start"], errors="coerce")
            except Exception as e:
                team_avail_df = pd.DataFrame(); load_errors.append(f"Availability data: {e}")

            # Video data
            home_video_df         = pd.DataFrame()
            home_video_summary_df = pd.DataFrame()
            video_fetched_at_home = None
            try:
                raw_video, video_fetched_at_home = load_parent_update_videos()
                home_video_df = raw_video[
                    (raw_video["faculty leader"] == "Team Marino") &
                    (raw_video["tutor"] != "Katherine Marino")
                ].copy()
                home_video_df["duration_secs"] = home_video_df["video duration"].apply(duration_to_secs)
                if not home_video_df.empty:
                    home_video_summary_df = build_video_tutor_summary(home_video_df)
            except Exception as e:
                load_errors.append(f"Video data: {e}")

        if load_errors:
            with st.expander("⚠️ Some data failed to load — click to see details"):
                for err in load_errors:
                    st.warning(err)

        # ── New data announcement banner ──────────────────────────────────
        try:
            concerns_home_df = load_tutor_concerns()
            _last_seen_file  = LAST_SEEN_FILE
            _last_seen_data  = gh_read(_last_seen_file)
            _latest_concern_date = None
            _latest_kpi_date     = None

            if not concerns_home_df.empty and "Date" in concerns_home_df.columns:
                import re as _re
                def _parse_date(s):
                    if pd.isna(s): return pd.NaT
                    s2 = str(s).replace("-","to").replace("\u2013","to")
                    parts = s2.split("to")
                    end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                    return pd.to_datetime(end, errors="coerce", dayfirst=False)
                _latest_concern_date = concerns_home_df["Date"].apply(_parse_date).max()

            if not kpi_home_df.empty and "Date Range Parsed" in kpi_home_df.columns:
                _latest_kpi_date = kpi_home_df["Date Range Parsed"].max()

            _stored_concern = pd.to_datetime(
                _last_seen_data.iloc[0]["last_concern_date"]
                if not _last_seen_data.empty and "last_concern_date" in _last_seen_data.columns
                else None, errors="coerce")
            _stored_kpi = pd.to_datetime(
                _last_seen_data.iloc[0]["last_kpi_date"]
                if not _last_seen_data.empty and "last_kpi_date" in _last_seen_data.columns
                else None, errors="coerce")
            _stored_banner_ts = pd.to_datetime(
                _last_seen_data.iloc[0]["banner_shown_at"]
                if not _last_seen_data.empty and "banner_shown_at" in _last_seen_data.columns
                else None, errors="coerce")

            _new_concern = pd.notna(_latest_concern_date) and (
                pd.isna(_stored_concern) or _latest_concern_date > _stored_concern)
            _new_kpi = pd.notna(_latest_kpi_date) and (
                pd.isna(_stored_kpi) or _latest_kpi_date > _stored_kpi)

            _show_banner = False
            if _new_concern or _new_kpi:
                _show_banner = True
                _new_row = pd.DataFrame([{
                    "last_concern_date": str(_latest_concern_date.date()) if pd.notna(_latest_concern_date) else "",
                    "last_kpi_date":     str(_latest_kpi_date.date())     if pd.notna(_latest_kpi_date)     else "",
                    "banner_shown_at":   str(pd.Timestamp.now().date()),
                }])
                gh_write(_last_seen_file, _new_row)
            elif pd.notna(_stored_banner_ts):
                days_since = (pd.Timestamp.now() - _stored_banner_ts).days
                _show_banner = days_since <= 3

            if _show_banner:
                _banner_parts = []
                if _new_concern and pd.notna(_latest_concern_date):
                    _banner_parts.append(f"📌 Tutor Concern data updated through **{_latest_concern_date.date()}**")
                if _new_kpi and pd.notna(_latest_kpi_date):
                    _banner_parts.append(f"📊 KPI data updated through **{_latest_kpi_date.date()}**")
                if not _banner_parts:
                    _banner_parts.append("📊 Monthly KPI & Tutor Concern data has been refreshed")
                st.info("🆕 **New Monthly Data Available!** &nbsp; " + " &nbsp;|&nbsp; ".join(_banner_parts))
        except Exception:
            pass

        # Save weekly video snapshot
        if not home_video_summary_df.empty:
            save_video_weekly_snapshot(home_video_summary_df, raw_video_df=home_video_df)

        # Login snapshot
        if "login_snapshot_saved" not in st.session_state:
            st.session_state["login_snapshot_saved"] = True
            prev_snap = load_login_snapshot()
            st.session_state["prev_snap"] = prev_snap
            save_login_snapshot(home_arch_df, home_grades_df, home_exam_df,
                                home_video_summary_df if not home_video_summary_df.empty else None)
        else:
            prev_snap = st.session_state.get("prev_snap", pd.DataFrame())

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

        cur_video_pct = None
        if not home_video_summary_df.empty:
            total_sent  = home_video_summary_df["updates_sent"].sum()
            total_found = home_video_summary_df["videos_found"].sum()
            cur_video_pct = round(total_found / total_sent * 100, 1) if total_sent > 0 else None

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
            if cur_video_pct is not None:
                prev_video = ps.get("team_pct_video")
                items.append(f"📹 Video rate: {_delta_badge(f'{cur_video_pct:.0f}%', f'{float(prev_video):.0f}%' if prev_video and not pd.isna(prev_video) else None, lower_is_better=False)}")
            items_html = " &nbsp;|&nbsp; ".join(items)
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:10px;
                        padding:10px 16px; margin-bottom:16px; font-size:0.87rem; color:#333;'>
                🕐 <b>Since your last visit</b>
                <span style='color:#aaa; font-size:0.8rem;'>({prev_login})</span>
                &nbsp;—&nbsp; {items_html}
            </div>""", unsafe_allow_html=True)
        else:
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:10px;
                        padding:10px 16px; margin-bottom:16px; font-size:0.87rem; color:#333;'>
                🕐 <b>First visit</b> — baseline saved. Deltas will appear on your next login.
                &nbsp;|&nbsp; 📦 {cur_arch} archivable
                &nbsp;|&nbsp; ⏳ {cur_unsched:.0f} unsched hrs
                &nbsp;|&nbsp; 📋 {cur_ng} no grades
                &nbsp;|&nbsp; 📚 {cur_sg} stale grades
                {f"&nbsp;|&nbsp; 📹 {cur_video_pct:.0f}% video rate" if cur_video_pct is not None else ""}
            </div>""", unsafe_allow_html=True)

        def card(emoji, title, body, color="#fff", details=None):
            bg     = {"red":"#fff0f0","green":"#f0fff4","yellow":"#fffbea","blue":"#f0f4ff"}.get(color,"#fff")
            border = {"red":"#ffcccc","green":"#b2f5c8","yellow":"#ffe58f","blue":"#bfd7ff"}.get(color,"#ddd")
            st.markdown(f"""
            <div style='background:{bg}; border:1.5px solid {border}; border-radius:10px;
                        padding:14px 18px; margin-bottom:6px;'>
                <div style='font-size:1.05rem; font-weight:600; margin-bottom:4px;'>{emoji} {title}</div>
                <div style='font-size:0.92rem; color:#444;'>{body}</div>
            </div>""", unsafe_allow_html=True)
            if details:
                with st.expander(f"👥 See {len(details)} student{'s' if len(details)>1 else ''}"):
                    for name in details:
                        st.markdown(f"- {name}")

        # Availability banner
        if not home_arch_df.empty:
            today          = pd.Timestamp.now()
            days_since_sun = (today.weekday() + 1) % 7
            this_sunday    = today - pd.Timedelta(days=days_since_sun)
            next_sunday    = this_sunday + pd.Timedelta(weeks=1)
            week_label     = this_sunday.strftime("%b %d")
            next_label     = next_sunday.strftime("%b %d")
            if team_avail_df.empty:
                st.markdown(f"""
                <div style='background:#f0fff4; border:1.5px solid #b2f5c8; border-radius:10px;
                            padding:16px 20px; margin-bottom:20px;
                            font-size:1.02rem; line-height:1.6; color:#276749;'>
                    ✅ <b>Availability Looks Good</b> — No tutors on Team Marino have 7+ days
                    of availability posted for the current week ({week_label}) or
                    next week ({next_label}).
                </div>""", unsafe_allow_html=True)
            else:
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

        # ── TEAM SNAPSHOT ──
        # ── Low Delivery + Not Accepting Alert ───────────────────────────
        try:
            _low_del_df = load_low_delivery_not_accepting("Katherine Marino")
            if not _low_del_df.empty:
                with st.expander(f"🚨 {len(_low_del_df)} tutor(s) — Accepting OFF + Low Delivery (next 3 wks)", expanded=False):
                    st.caption("These tutors have accepting new students turned off AND are projected below 80% of their delivery target over the next 3 weeks.")
                    _ld_display = _low_del_df.rename(columns={
                        "tutor": "Tutor",
                        "delivery_target": "Target (hrs)",
                        "avg_delivery_next_3wks": "Avg Delivery (next 3 wks)",
                        "delivery_pct": "% of Target"
                    })
                    _ld_display["% of Target"] = _ld_display["% of Target"].apply(lambda x: f"{x:.1f}%")
                    st.dataframe(_ld_display, use_container_width=True, hide_index=True)
        except Exception:
            pass

        # ── Featured Tutors ──────────────────────────────────────────────
        try:
            _featured_df = load_featured_tutors()
            if not _featured_df.empty:
                _my_featured = _featured_df[_featured_df["faculty_leader"] == "Katherine Marino"].copy()
                _all_featured = _featured_df.copy()
                with st.expander(f"⭐ Featured Tutors — Your Team: {len(_my_featured)} | All Teams: {len(_all_featured)}", expanded=False):
                    if _my_featured.empty:
                        st.info("No tutors on your team are currently featured.")
                    else:
                        _tier_counts = _my_featured.groupby("tutor_tier").size().reset_index(name="Your Team")
                        _all_tier_counts = _all_featured.groupby("tutor_tier").size().reset_index(name="All Teams")
                        _tier_summary = _tier_counts.merge(_all_tier_counts, on="tutor_tier", how="outer").fillna(0)
                        _tier_summary[["Your Team","All Teams"]] = _tier_summary[["Your Team","All Teams"]].astype(int)
                        _tier_summary = _tier_summary.rename(columns={"tutor_tier":"Tier"})
                        c1, c2 = st.columns([1, 2])
                        with c1:
                            st.markdown("**By Tier**")
                            st.dataframe(_tier_summary, use_container_width=True, hide_index=True)
                        with c2:
                            st.markdown("**Featured Tutors on Your Team**")
                            _display_feat = _my_featured[["tutor","tutor_tier"]].rename(columns={"tutor":"Tutor","tutor_tier":"Tier"})
                            st.dataframe(_display_feat, use_container_width=True, hide_index=True)
        except Exception:
            pass

        st.markdown("### 📊 Team Snapshot")

        # Exam sync status
        try:
            _, _exam_fetched_at = load_exam_data()
            try:
                _exam_ts   = pd.to_datetime(_exam_fetched_at, format="%B %d, %Y at %I:%M %p")
                _hours_old = (pd.Timestamp.now() - _exam_ts).total_seconds() / 3600
                if _hours_old < 24:
                    _sync_color = "#1a6e36"; _sync_icon = "✅"
                    _sync_msg = f"Exam data is fresh — last synced {_exam_fetched_at}"
                elif _hours_old < 48:
                    _sync_color = "#b35c00"; _sync_icon = "⚠️"
                    _sync_msg = f"Exam data is {_hours_old:.0f} hours old — last synced {_exam_fetched_at}. Consider running sync_exam_data.py."
                else:
                    _sync_color = "#cc0000"; _sync_icon = "🚨"
                    _sync_msg = f"Exam data is {_hours_old:.0f} hours old — run sync_exam_data.py now."
            except Exception:
                _sync_color = "#888"; _sync_icon = "🕐"
                _sync_msg = f"Exam data last synced: {_exam_fetched_at}"
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:8px;
                        padding:7px 14px; margin-bottom:10px; font-size:0.83rem; color:{_sync_color};'>
                {_sync_icon} <b>Exam Data Sync:</b> {_sync_msg}
            </div>""", unsafe_allow_html=True)
        except Exception:
            st.markdown("""
            <div style='background:#fff3cd; border:1px solid #ffe58f; border-radius:8px;
                        padding:7px 14px; margin-bottom:10px; font-size:0.83rem; color:#b35c00;'>
                ⚠️ <b>Exam Data Sync:</b> Could not load exam data — check GitHub secrets and run sync_exam_data.py.
            </div>""", unsafe_allow_html=True)

        # Video sync status
        if video_fetched_at_home:
            try:
                _vts  = pd.to_datetime(video_fetched_at_home.replace(" UTC",""), format="%B %d, %Y at %I:%M %p")
                _vhrs = (pd.Timestamp.now() - _vts).total_seconds() / 3600
                if _vhrs < 24 * 8:
                    _vc = "#1a6e36"; _vi = "✅"
                    _vm = f"Video data is current — last synced {video_fetched_at_home}"
                else:
                    _vc = "#cc0000"; _vi = "🚨"
                    _vm = f"Video data is {_vhrs/24:.0f} days old — run sync_parent_update_videos.py."
            except Exception:
                _vc = "#888"; _vi = "🕐"
                _vm = f"Video data last synced: {video_fetched_at_home}"
            st.markdown(f"""
            <div style='background:#f7f9fc; border:1px solid #d0d7e0; border-radius:8px;
                        padding:7px 14px; margin-bottom:10px; font-size:0.83rem; color:{_vc};'>
                {_vi} <b>Video Data Sync:</b> {_vm}
            </div>""", unsafe_allow_html=True)

        total_active = home_arch_df["student_name"].nunique() if not home_arch_df.empty else "—"
        total_tp     = home_exam_df["student_id"].nunique() if not home_exam_df.empty else "—"

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
            latest_kpi  = kpi_home_df["Date Range Parsed"].max()
            prev_kpi    = kpi_home_df[kpi_home_df["Date Range Parsed"] < latest_kpi]["Date Range Parsed"].max()
            latest_team = kpi_home_df[
                (kpi_home_df["Date Range Parsed"] == latest_kpi) &
                (kpi_home_df["Faculty Leader"] == "Katherine Marino")]
            prev_team   = kpi_home_df[
                (kpi_home_df["Date Range Parsed"] == prev_kpi) &
                (kpi_home_df["Faculty Leader"] == "Katherine Marino")] if pd.notna(prev_kpi) else pd.DataFrame()
            for m in kpi_metrics:
                if m in latest_team.columns:
                    curr = latest_team[m].mean() * 100
                    prev = prev_team[m].mean() * 100 if not prev_team.empty and m in prev_team.columns else None
                    kpi_avgs[m] = {"curr": curr, "prev": prev,
                                   "delta": curr - prev if prev is not None else None}

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
        st.caption("**🏥 Team Health**")

        _snap2_arch    = int(home_arch_df["should_archive"].sum()) if not home_arch_df.empty else 0
        _snap2_unsched = round(float(home_arch_df["unscheduled_hours"].sum()), 1) if not home_arch_df.empty else 0.0
        _snap2_ng = 0; _snap2_sg = 0
        if not home_grades_df.empty:
            _snap2_ng = int(home_grades_df.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
            _hany2    = home_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            _graded2  = home_grades_df[home_grades_df["student_id"].isin(_hany2[_hany2].index)]
            if not _graded2.empty and "days_since_update" in _graded2.columns:
                _snap2_sg = int((_graded2.groupby("student_id")["days_since_update"].min() > 90).sum())
        _snap2_ne = 0; _snap2_se = 0
        if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
            _now_s2 = pd.Timestamp.now(tz="UTC")
            for _sid, _sdf in home_exam_df.groupby("student_id"):
                if pd.notna(_sdf["attended_test_prep_hours"].iloc[0]) and \
                        _sdf["attended_test_prep_hours"].iloc[0] >= 6:
                    if _sdf[_sdf["exam_valid_composite"] == True].empty:
                        _snap2_ne += 1
                    else:
                        _latest_e = pd.to_datetime(
                            _sdf[_sdf["exam_valid_composite"] == True]["exam_date"], utc=True).max()
                        if pd.notna(_latest_e) and (_now_s2 - _latest_e).days > 90:
                            _snap2_se += 1

        health_cols = st.columns(7)
        health_cols[0].metric("📦 Archivable",   _snap2_arch)
        health_cols[1].metric("⏳ Unsched Hrs",   f"{_snap2_unsched:.0f}")
        health_cols[2].metric("📋 No Grades",     _snap2_ng)
        health_cols[3].metric("📚 Stale Grades",  _snap2_sg)
        health_cols[4].metric("📝 No Exam",       _snap2_ne)
        health_cols[5].metric("🕐 Stale Exams",   _snap2_se)
        health_cols[6].metric("📹 Video Rate",
                               f"{cur_video_pct:.0f}%" if cur_video_pct is not None else "—")

        st.divider()

        # ── Week-over-week movement ──
        _mi_snap_arch   = load_snapshots()
        _mi_snap_grades = load_grades_snapshots()
        _mi_snap_exams  = load_exams_snapshots()
        _mi_snap_video  = load_video_snapshots()

        _mi_rows = {}
        improved_list = []
        declined_list = []

        def _wow_delta(snap_df, tutor_col, metric_col, tutor):
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
        for _snap in [_mi_snap_arch, _mi_snap_grades, _mi_snap_exams, _mi_snap_video]:
            if not _snap.empty and "tutor_name" in _snap.columns:
                all_tutors_mi.update(
                    _snap[_snap["tutor_name"].isin(
                        home_arch_df["tutor_name"].unique() if not home_arch_df.empty else []
                    )]["tutor_name"].tolist()
                )

        _metric_specs = [
            (_mi_snap_arch,   "archivable_students", "Archivable Students", True),
            (_mi_snap_arch,   "unscheduled_hours",   "Unscheduled Hours",   True),
            (_mi_snap_grades, "students_no_grades",  "No Grades",           True),
            (_mi_snap_grades, "stale_grade_students","Stale Grades",        True),
            (_mi_snap_exams,  "students_no_exam",    "No Exam",             True),
            (_mi_snap_exams,  "students_stale_exam", "Stale Exams",         True),
            (_mi_snap_video,  "pct_with_video",      "Video Rate %",        False),
        ]

        for tutor in all_tutors_mi:
            tutor_deltas = []
            for snap_df, metric_col, label, lib in _metric_specs:
                prev, curr, delta = _wow_delta(snap_df, "tutor_name", metric_col, tutor)
                if delta is not None:
                    tutor_deltas.append((label, prev, curr, delta, lib))
            if tutor_deltas:
                score = sum(-d for _, _, _, d, lib in tutor_deltas if lib) + \
                        sum(d for _, _, _, d, lib in tutor_deltas if not lib)
                _mi_rows[tutor] = {"score": score, "deltas": tutor_deltas}

        if len(_mi_rows) >= 2:
            improved_list = []; declined_list = []
            for tutor, data in _mi_rows.items():
                good = [(lbl, prev, curr, delta, lib)
                        for lbl, prev, curr, delta, lib in data["deltas"]
                        if (lib and delta < -0.5) or (not lib and delta > 0.5)]
                bad  = [(lbl, prev, curr, delta, lib)
                        for lbl, prev, curr, delta, lib in data["deltas"]
                        if (lib and delta > 0.5) or (not lib and delta < -0.5)]
                improvement_score = sum(abs(d) for _, _, _, d, _ in good)
                decline_score     = sum(abs(d) for _, _, _, d, _ in bad)
                if improvement_score > decline_score and len(good) >= len(bad):
                    improved_list.append((tutor, good, improvement_score))
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
                        parts = [f"{lbl}: {prev:.0f}→{curr:.0f}"
                                 for lbl, prev, curr, delta, _ in meaningful]
                        st.markdown(f"""
                        <div style='background:#f0fff4; border:1.5px solid #b2f5c8;
                                    border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                            <div style='font-weight:700; color:#276749; font-size:1rem;'>📈 {tutor}</div>
                            <div style='font-size:0.83rem; color:#444; margin-top:4px;'>{" · ".join(parts)}</div>
                        </div>""", unsafe_allow_html=True)
                else:
                    st.caption("No tutors with clear improvement this week.")
            with md_col:
                st.markdown("#### 📉 Most Declined")
                if declined_list:
                    for tutor, bad_metrics, _ in declined_list[:3]:
                        meaningful = sorted(bad_metrics, key=lambda x: abs(x[3]), reverse=True)[:3]
                        parts = [f"{lbl}: {prev:.0f}→{curr:.0f}"
                                 for lbl, prev, curr, delta, _ in meaningful]
                        st.markdown(f"""
                        <div style='background:#fff0f0; border:1.5px solid #ffcccc;
                                    border-radius:10px; padding:12px 16px; margin-bottom:8px;'>
                            <div style='font-weight:700; color:#9b1c1c; font-size:1rem;'>📉 {tutor}</div>
                            <div style='font-size:0.83rem; color:#444; margin-top:4px;'>{" · ".join(parts)}</div>
                        </div>""", unsafe_allow_html=True)
                else:
                    st.caption("No tutors with clear decline this week.")
            st.divider()

        # ── THREE COLUMNS ──
        col_red, col_green, col_yellow = st.columns(3)

        with col_red:
            st.markdown("### 🚨 Needs Attention")

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

            if not home_arch_df.empty:
                arch_by_tutor = (home_arch_df[home_arch_df["should_archive"] == True]
                                 .groupby("tutor_name")["student_name"].nunique()
                                 .sort_values(ascending=False))
                for tutor, count in arch_by_tutor.head(3).items():
                    _arch_students = sorted(home_arch_df[
                        (home_arch_df["tutor_name"] == tutor) &
                        (home_arch_df["should_archive"] == True)
                    ]["student_name"].dropna().unique().tolist())
                    card("📦", "Archivable Students",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"that should be archived.",
                         color="red", details=_arch_students)

            if not home_exam_df.empty:
                SAT_TYPES = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT",
                             "Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
                ACT_TYPES = {"ACT","Digital ACT"}
                home_exam_df["exam_family"] = home_exam_df["subject"].apply(
                    lambda x: "SAT/PSAT" if x in SAT_TYPES else ("ACT" if x in ACT_TYPES else "Other"))
                def sat_composite_ok(r):
                    return (pd.notna(r["sat_math"]) and r["sat_math"] >= 300 and
                            pd.notna(r["sat_rw"]) and r["sat_rw"] >= 300)
                def act_composite_ok(r):
                    return ((pd.isna(r["act_english"]) or r["act_english"] >= 10) and
                            (pd.isna(r["act_math"]) or r["act_math"] >= 10) and
                            (pd.isna(r["act_reading"]) or r["act_reading"] >= 10))
                home_exam_df["exam_valid_composite"] = home_exam_df.apply(
                    lambda r: (pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")) and (
                        sat_composite_ok(r) if r["exam_family"] == "SAT/PSAT"
                        else (act_composite_ok(r) if r["exam_family"] == "ACT" else False)), axis=1)
                no_exam_by_tutor = {}
                for tutor, tdf in home_exam_df.groupby("tutor_name"):
                    count = sum(1 for sid, sdf in tdf.groupby("student_id")
                                if sdf["attended_test_prep_hours"].iloc[0] >= 6
                                and sdf[sdf["exam_valid_composite"] == True].empty)
                    if count > 0:
                        no_exam_by_tutor[tutor] = count
                for tutor, count in sorted(no_exam_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:3]:
                    _no_exam_students = sorted(set(
                        sname for sid, sdf in home_exam_df[home_exam_df["tutor_name"] == tutor].groupby("student_id")
                        if sdf["attended_test_prep_hours"].iloc[0] >= 6
                        and sdf[sdf["exam_valid_composite"] == True].empty
                        for sname in sdf["student_name"].dropna().unique()
                    ))
                    card("📝", "No Completed Exam",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with 6+ hrs but no completed exam.",
                         color="red", details=_no_exam_students)

            if not home_grades_df.empty:
                no_grades_by_tutor = {}
                for tutor, tdf in home_grades_df.groupby("tutor_name"):
                    no_g = tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum()
                    if no_g > 0:
                        no_grades_by_tutor[tutor] = int(no_g)
                for tutor, count in sorted(no_grades_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:2]:
                    _no_grades_students = sorted(
                        home_grades_df[
                            (home_grades_df["tutor_name"] == tutor) &
                            (home_grades_df.groupby("student_id")["score"]
                             .transform(lambda s: s.isna().all()))
                        ]["student_name"].dropna().unique().tolist()
                    )
                    card("📋", "No Grades Entered",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no grades entered at all.",
                         color="red", details=_no_grades_students)

            # Low parent update rate flag
            if not home_video_summary_df.empty:
                low_pu = home_video_summary_df[
                    (home_video_summary_df["parent_update_pct"] < 80) &
                    (home_video_summary_df["updates_required"] > 0)
                ].sort_values("parent_update_pct")
                for _, row in low_pu.head(3).iterrows():
                    card("📬", "Low Parent Update Rate",
                         f"<b>{row['tutor_name']}</b> sent only "
                         f"<b>{row['parent_update_pct']:.0f}%</b> of required parent updates "
                         f"({int(row['updates_sent'])}/{int(row['updates_required'])}).",
                         color="red")
                # Low video rate flag
                low_video = home_video_summary_df[
                    (home_video_summary_df["pct_with_video"] < 80) &
                    (home_video_summary_df["updates_sent"] > 0)
                ].sort_values("pct_with_video")
                for _, row in low_video.head(3).iterrows():
                    card("📹", "Low Video Rate",
                         f"<b>{row['tutor_name']}</b> attached videos to only "
                         f"<b>{row['pct_with_video']:.0f}%</b> of parent updates "
                         f"({int(row['videos_found'])}/{int(row['updates_sent'])}).",
                         color="red")
                # Low homework mention flag
                low_hw = home_video_summary_df[
                    (home_video_summary_df["pct_with_homework"] < 80) &
                    (home_video_summary_df["updates_sent"] > 0)
                ].sort_values("pct_with_homework")
                for _, row in low_hw.head(3).iterrows():
                    card("📝", "Low Homework Mention Rate",
                         f"<b>{row['tutor_name']}</b> mentioned homework in only "
                         f"<b>{row['pct_with_homework']:.0f}%</b> of updates "
                         f"({int(row['homework_count'])}/{int(row['updates_sent'])}).",
                         color="red")

            # Concern group movement — worsened
            try:
                _cg_df = load_tutor_concerns()
                _worsened, _improved, _prev_d, _latest_d = get_concern_movements(
                    _cg_df, faculty_leader_name)
                if not _worsened.empty:
                    for _, _crow in _worsened.head(3).iterrows():
                        _chg = int(_crow["Change"])
                        _reasons = str(_crow.get("Reasons","")).strip()
                        card("📌", "Concern Group Worsened",
                             f"<b>{_crow['Tutor Name']}</b> moved from Group "
                             f"<b>{int(_crow['Prev Group'])}</b> to Group "
                             f"<b>{int(_crow['Concern Group'])}</b> "
                             f"(<span style='color:#cc0000'>▲ {_chg:+d}</span>)"
                             + (f" — {_reasons}" if _reasons and _reasons != 'nan' else ""),
                             color="red")
            except Exception:
                pass

            thresholds = {"% to Delivery Target": 70, "% Sessions on Time": 80}
            for m, thresh in thresholds.items():
                if m in kpi_avgs and kpi_avgs[m]["curr"] < thresh:
                    card("⚠️", "KPI Below Threshold",
                         f"<b>{m.replace('% to ','').replace('% ','')}</b> is at "
                         f"<b>{kpi_avgs[m]['curr']:.1f}%</b> — below the {thresh}% threshold.",
                         color="red")

        with col_green:
            st.markdown("### ✅ Wins")

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

            strong_kpis = [(m, d) for m, d in kpi_avgs.items() if d["curr"] >= 90]
            for m, d in strong_kpis[:2]:
                short = m.replace("% of Active Students with Progress Updates Completed in last 2 months",
                                  "Progress Updates").replace("% to ","").replace("% ","")
                card("🌟", f"Strong KPI: {short}",
                     f"Team is at <b>{d['curr']:.1f}%</b> — great work!", color="green")

            if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                try:
                    def _compute_imp_home(student_df, exam_family):
                        fsd = student_df["first_session_day"].iloc[0]
                        fam = student_df[student_df["exam_family"] == exam_family]
                        fam = fam[fam["exam_valid_composite"] == True].dropna(subset=["score"])
                        if fam.empty: return None
                        before = fam[fam["exam_date"] <= fsd]
                        after  = fam[fam["exam_date"] >  fsd]
                        if after.empty: return None
                        if not before.empty:
                            b_row = before.sort_values("exam_date").iloc[-1]
                            a_fam = after
                        else:
                            b_row = after.sort_values("exam_date").iloc[0]
                            a_fam = after.iloc[1:]
                        if a_fam.empty: return None
                        b = b_row["score"]
                        e = a_fam.sort_values("exam_date").iloc[-1]["score"]
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
                                 f"<b>+{row['improvement']:.0f} pts</b>.", color="green")
                except Exception:
                    pass

            if not home_arch_df.empty:
                all_tutors = set(home_arch_df["tutor_name"].unique())
                arch_tutors = set(home_arch_df[home_arch_df["should_archive"]==True]["tutor_name"].unique())
                clean_tutors = all_tutors - arch_tutors
                if clean_tutors:
                    card("✨", "Clean Dashboards",
                         f"<b>{len(clean_tutors)} tutor{'s' if len(clean_tutors)>1 else ''}</b> "
                         f"have zero archivable students. 👏", color="green")

            # Concern group movement — improved
            try:
                if "_improved" in dir() and not _improved.empty:
                    for _, _crow in _improved.head(3).iterrows():
                        _chg = int(_crow["Change"])
                        card("📌", "Concern Group Improved",
                             f"<b>{_crow['Tutor Name']}</b> moved from Group "
                             f"<b>{int(_crow['Prev Group'])}</b> to Group "
                             f"<b>{int(_crow['Concern Group'])}</b> "
                             f"(<span style='color:#1a6e36'>▼ {_chg}</span>)",
                             color="green")
            except Exception:
                pass

            # Perfect video rate win
            if not home_video_summary_df.empty:
                perfect_video = home_video_summary_df[
                    (home_video_summary_df["pct_with_video"] == 100) &
                    (home_video_summary_df["updates_sent"] > 0)
                ]
                if not perfect_video.empty:
                    names = ", ".join(perfect_video["tutor_name"].tolist()[:5])
                    card("🎬", "Perfect Video Rate",
                         f"<b>{len(perfect_video)} tutor{'s' if len(perfect_video)>1 else ''}</b> "
                         f"attached a video to every parent update: {names}",
                         color="green")

        with col_yellow:
            st.markdown("### ⏳ Watch List")

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

            _snap_arch_h   = load_snapshots()
            _snap_grades_h = load_grades_snapshots()
            _snap_exams_h  = load_exams_snapshots()
            _snap_video_h  = load_video_snapshots()

            if not (_snap_arch_h.empty and _snap_grades_h.empty and _snap_exams_h.empty):
                anomaly_tutors = []
                for _t, _tdf in home_arch_df.groupby("tutor_name"):
                    _cur = {
                        "arch_count":    int(_tdf["should_archive"].sum()),
                        "unsched_hrs":   float(_tdf["unscheduled_hours"].sum()),
                        "no_grades":     0, "stale_grades": 0,
                        "no_exam":       0, "stale_exams":  0,
                        "pct_with_video": None,
                    }
                    if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                        _gdf = home_grades_df[home_grades_df["tutor_name"] == _t]
                        _cur["no_grades"] = int(_gdf.groupby("student_id")["score"]
                                               .apply(lambda s: s.isna().all()).sum())
                        _hany   = _gdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                        _graded = _gdf[_gdf["student_id"].isin(_hany[_hany].index)]
                        if not _graded.empty and "days_since_update" in _graded.columns:
                            _cur["stale_grades"] = int(
                                (_graded.groupby("student_id")["days_since_update"].min() > 90).sum())
                    if not home_exam_df.empty and _t in home_exam_df["tutor_name"].values and \
                            "exam_valid_composite" in home_exam_df.columns:
                        _edf   = home_exam_df[home_exam_df["tutor_name"] == _t]
                        _now_a = pd.Timestamp.now(tz="UTC")
                        _cur["no_exam"] = sum(
                            1 for sid, sdf in _edf.groupby("student_id")
                            if sdf["attended_test_prep_hours"].iloc[0] >= 6
                            and sdf[sdf["exam_valid_composite"] == True].empty)
                        for sid, sdf in _edf.groupby("student_id"):
                            _comp = sdf[sdf["exam_valid_composite"] == True]
                            if not _comp.empty:
                                _latest = pd.to_datetime(_comp["exam_date"], utc=True).max()
                                if pd.notna(_latest) and (_now_a - _latest).days > 90:
                                    _cur["stale_exams"] += 1
                    if not home_video_summary_df.empty and _t in home_video_summary_df["tutor_name"].values:
                        _vrow = home_video_summary_df[home_video_summary_df["tutor_name"] == _t].iloc[0]
                        _cur["pct_with_video"] = float(_vrow["pct_with_video"])
                    _flags  = compute_anomalies(_t, _snap_arch_h, _snap_grades_h,
                                                _snap_exams_h, _snap_video_h, _cur)
                    _spiked = [k for k, v in _flags.items() if v]
                    if _spiked:
                        _label_map = {
                            "arch_count":    "archivable students",
                            "unsched_hrs":   "unscheduled hours",
                            "no_grades":     "students with no grades",
                            "stale_grades":  "stale grade entries",
                            "no_exam":       "students missing exams",
                            "stale_exams":   "stale exam scores",
                            "pct_with_video":"video attachment rate drop",
                        }
                        _desc = ", ".join(_label_map.get(k, k) for k in _spiked)
                        card("🔔", f"Unusual Spike — {_t}",
                             f"<b>{_t}</b> is significantly different from their own historical average on: "
                             f"<b>{_desc}</b>.", color="yellow")

            # Video duration flags
            if not home_video_df.empty:
                short_videos = home_video_df[home_video_df["duration_secs"] < 10]
                long_videos  = home_video_df[home_video_df["duration_secs"] > 300]
                if not short_videos.empty:
                    tutors_short = short_videos["tutor"].unique().tolist()
                    card("⚡", "Suspiciously Short Videos (<10s)",
                         f"<b>{len(short_videos)} video{'s' if len(short_videos)>1 else ''}</b> "
                         f"under 10 seconds detected. Tutors: {', '.join(tutors_short[:3])}. "
                         f"May be accidental uploads.", color="yellow")
                if not long_videos.empty:
                    tutors_long = long_videos["tutor"].unique().tolist()
                    card("⏱️", "Very Long Videos (>5 min)",
                         f"<b>{len(long_videos)} video{'s' if len(long_videos)>1 else ''}</b> "
                         f"over 5 minutes detected. Tutors: {', '.join(tutors_long[:3])}.",
                         color="yellow")

            if not home_arch_df.empty:
                unsched = (home_arch_df[home_arch_df["unscheduled_hours"] > 0]
                           .groupby("tutor_name")["unscheduled_hours"].sum()
                           .sort_values(ascending=False))
                total_unsched = home_arch_df["unscheduled_hours"].sum()
                if total_unsched > 0:
                    _unsched_details = [f"{t} — {h:.1f} hrs" for t, h in unsched.head(10).items()]
                    card("⏳", "Unscheduled Hours",
                         f"Team has <b>{total_unsched:,.1f} total unscheduled hours</b>. "
                         f"Top: <b>{unsched.index[0]}</b> ({unsched.iloc[0]:.1f} hrs).",
                         color="yellow", details=_unsched_details)

            if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                now_utc2 = pd.Timestamp.now(tz="UTC")
                stale_exam_by_tutor = {}
                stale_exam_students = {}
                for tutor, tdf in home_exam_df.groupby("tutor_name"):
                    _stale_names = []
                    for sid, sdf in tdf.groupby("student_id"):
                        completed = sdf[sdf["exam_valid_composite"] == True]
                        if not completed.empty:
                            latest = pd.to_datetime(completed["exam_date"], utc=True).max()
                            if pd.notna(latest) and (now_utc2 - latest).days > 90:
                                _sname = sdf["student_name"].iloc[0]
                                _days  = (now_utc2 - latest).days
                                _stale_names.append(f"{_sname} — {_days}d since last exam")
                    if _stale_names:
                        stale_exam_by_tutor[tutor] = len(_stale_names)
                        stale_exam_students[tutor]  = sorted(_stale_names)
                for tutor, count in sorted(stale_exam_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:3]:
                    card("🕐", "Stale Practice Exam",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no completed exam in 90+ days.",
                         color="yellow", details=stale_exam_students.get(tutor))

            if not home_grades_df.empty:
                stale_by_tutor = {}
                stale_grade_students = {}
                for tutor, tdf in home_grades_df.groupby("tutor_name"):
                    has_any    = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                    graded_ids = has_any[has_any].index
                    graded     = tdf[tdf["student_id"].isin(graded_ids)]
                    if not graded.empty:
                        latest = graded.groupby(["student_id","student_name"])["days_since_update"].min().reset_index()
                        stale_rows = latest[latest["days_since_update"] > 90].sort_values("days_since_update", ascending=False)
                        if not stale_rows.empty:
                            stale_by_tutor[tutor]       = len(stale_rows)
                            stale_grade_students[tutor]  = [
                                f"{row['student_name']} — {int(row['days_since_update'])}d since update"
                                for _, row in stale_rows.iterrows()
                            ]
                for tutor, count in sorted(stale_by_tutor.items(),
                                           key=lambda x: x[1], reverse=True)[:2]:
                    card("📚", "Stale Grades",
                         f"<b>{tutor}</b> has <b>{count} student{'s' if count>1 else ''}</b> "
                         f"with no grade update in 90+ days.",
                         color="yellow", details=stale_grade_students.get(tutor))

            if not home_exam_df.empty:
                no_session_yet = (home_exam_df[home_exam_df["attended_test_prep_hours"] == 0]
                                  ["student_id"].nunique())
                if no_session_yet > 0:
                    card("🆕", "New Students — No Sessions Yet",
                         f"<b>{no_session_yet} test prep student{'s' if no_session_yet>1 else ''}</b> "
                         f"enrolled but no sessions delivered yet.", color="yellow")

        st.divider()

        # Watch list strip
        watched = load_watchlist()
        if watched:
            st.markdown("### 👀 Watched Tutors")
            st.caption("Quick status for tutors on your watch list. Go to the Watch List page for full details.")
            wl_cols = st.columns(min(len(watched), 4))
            for i, tutor in enumerate(watched):
                issues = 0; lines = []
                if not home_arch_df.empty:
                    arch_count = home_arch_df[
                        (home_arch_df["tutor_name"] == tutor) &
                        (home_arch_df["should_archive"] == True)
                    ]["student_name"].nunique()
                    if arch_count > 0:
                        issues += 1; lines.append(f"📦 {arch_count} archivable")
                    unsched_hrs = home_arch_df[home_arch_df["tutor_name"] == tutor]["unscheduled_hours"].sum()
                    if unsched_hrs > 0:
                        lines.append(f"⏳ {unsched_hrs:.0f} unsched hrs")
                if not home_grades_df.empty and tutor in home_grades_df["tutor_name"].values:
                    tgdf = home_grades_df[home_grades_df["tutor_name"] == tutor]
                    no_g = tgdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum()
                    if no_g > 0:
                        issues += 1; lines.append(f"📋 {int(no_g)} no grades")
                    has_g  = tgdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                    graded = tgdf[tgdf["student_id"].isin(has_g[has_g].index)]
                    if not graded.empty:
                        latest_g = graded.groupby("student_id")["days_since_update"].min()
                        stale_g  = int((latest_g > 90).sum())
                        if stale_g > 0:
                            lines.append(f"📚 {stale_g} stale grades")
                if not home_exam_df.empty and "exam_valid_composite" in home_exam_df.columns:
                    tedf = home_exam_df[home_exam_df["tutor_name"] == tutor]
                    no_ex = sum(
                        1 for sid, sdf in tedf.groupby("student_id")
                        if sdf["attended_test_prep_hours"].iloc[0] >= 6
                        and sdf[sdf["exam_valid_composite"] == True].empty
                    )
                    if no_ex > 0:
                        issues += 1; lines.append(f"📝 {no_ex} no exam")
                if not home_video_summary_df.empty and tutor in home_video_summary_df["tutor_name"].values:
                    vrow = home_video_summary_df[home_video_summary_df["tutor_name"] == tutor].iloc[0]
                    if vrow["parent_update_pct"] < 80:
                        issues += 1; lines.append(f"📬 {vrow['parent_update_pct']:.0f}% parent update rate")
                    if vrow["pct_with_video"] < 80:
                        issues += 1; lines.append(f"📹 {vrow['pct_with_video']:.0f}% video rate")
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

        # Daily digest
        _today_str     = pd.Timestamp.now().strftime("%A, %B %-d")
        _total_active  = home_arch_df["student_name"].nunique() if not home_arch_df.empty else 0
        _total_arch    = int(home_arch_df["should_archive"].sum()) if not home_arch_df.empty else 0
        _total_unsched = round(float(home_arch_df["unscheduled_hours"].sum()), 1) if not home_arch_df.empty else 0.0
        _kpi_sentence  = ""
        if kpi_avgs:
            _worst = min(kpi_avgs.items(), key=lambda x: x[1]["curr"])
            _best  = max(kpi_avgs.items(), key=lambda x: x[1]["curr"])
            _ws = _worst[0].replace("% of Active Students with Progress Updates Completed in last 2 months",
                                    "Progress Updates").replace("% to ","").replace("% ","")
            _bs = _best[0].replace("% of Active Students with Progress Updates Completed in last 2 months",
                                   "Progress Updates").replace("% to ","").replace("% ","")
            _kpi_sentence = (f"Your strongest KPI is **{_bs}** at {kpi_avgs[_best[0]]['curr']:.0f}%; "
                             f"**{_ws}** needs the most attention at {kpi_avgs[_worst[0]]['curr']:.0f}%.")
        _video_sentence = f" The team's video attachment rate is **{cur_video_pct:.0f}%** this week." \
                          if cur_video_pct is not None else ""
        _intro = (f"Here's your briefing for **{_today_str}**. "
                  f"Team Marino has **{_total_active} active students**, "
                  f"**{_total_arch} flagged for archiving**, "
                  f"and **{_total_unsched:.0f} unscheduled hours** across the team. "
                  f"{_kpi_sentence}{_video_sentence}")

        _login_lines = []
        if not prev_snap.empty:
            _ps = prev_snap.iloc[0]
            _pl = str(_ps.get("login_ts", "your last visit"))
            for _label, _cur_val, _prev_key, _lib in [
                ("archivable students",  cur_arch,    "team_archivable",      True),
                ("unscheduled hours",    cur_unsched, "team_unscheduled_hrs", True),
                ("students with no grades", cur_ng,  "team_no_grades",       True),
                ("stale grade entries",  cur_sg,      "team_stale_grades",    True),
            ]:
                try:
                    _diff = int(_cur_val) - int(_ps.get(_prev_key, _cur_val))
                except:
                    continue
                if _diff == 0: continue
                _dir  = "up" if _diff > 0 else "down"
                _good = (_diff < 0) if _lib else (_diff > 0)
                _icon = "✅" if _good else "⚠️"
                _login_lines.append(f"{_icon} **{_label}** went {_dir} by {abs(_diff)} since {_pl}")

        _attn_lines = []
        if not home_arch_df.empty:
            _arch_top = (home_arch_df[home_arch_df["should_archive"] == True]
                         .groupby("tutor_name")["student_name"].nunique()
                         .sort_values(ascending=False))
            for _t, _c in _arch_top.head(2).items():
                _attn_lines.append(f"📦 **{_t}** has **{_c} student{'s' if _c>1 else ''}** flagged for archiving")
        if not home_video_summary_df.empty:
            _low_v = home_video_summary_df[home_video_summary_df["pct_with_video"] < 80]
            for _, _vr in _low_v.head(2).iterrows():
                _attn_lines.append(
                    f"📹 **{_vr['tutor_name']}** video rate is **{_vr['pct_with_video']:.0f}%** "
                    f"({int(_vr['videos_found'])}/{int(_vr['updates_sent'])})")

        _wow_lines = []
        if improved_list:
            for _t, _good, _ in improved_list[:2]:
                _top = sorted(_good, key=lambda x: abs(x[3]), reverse=True)[0]
                _wow_lines.append(f"📈 **{_t}** improved — {_top[0]} shifted from {_top[1]:.0f} to {_top[2]:.0f}")
        if declined_list:
            for _t, _bad, _ in declined_list[:2]:
                _top = sorted(_bad, key=lambda x: abs(x[3]), reverse=True)[0]
                _wow_lines.append(f"📉 **{_t}** declined — {_top[0]} shifted from {_top[1]:.0f} to {_top[2]:.0f}")

        _watched_lines = []
        _dg_watched = load_watchlist()
        for _t in _dg_watched:
            _wl_issues = []
            if not home_arch_df.empty:
                _wa = home_arch_df[
                    (home_arch_df["tutor_name"] == _t) &
                    (home_arch_df["should_archive"] == True)]["student_name"].nunique()
                if _wa > 0: _wl_issues.append(f"{_wa} archivable")
            if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                _wng = int(home_grades_df[home_grades_df["tutor_name"] == _t]
                           .groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
                if _wng > 0: _wl_issues.append(f"{_wng} no grades")
            if not home_video_summary_df.empty and _t in home_video_summary_df["tutor_name"].values:
                _vr = home_video_summary_df[home_video_summary_df["tutor_name"] == _t].iloc[0]
                if _vr["parent_update_pct"] < 80:
                    _wl_issues.append(f"{_vr['parent_update_pct']:.0f}% parent update rate")
                if _vr["pct_with_video"] < 80:
                    _wl_issues.append(f"{_vr['pct_with_video']:.0f}% video rate")
            if _wl_issues:
                _watched_lines.append(f"👁 **{_t}**: {', '.join(_wl_issues)}")
            else:
                _watched_lines.append(f"✅ **{_t}**: no issues detected")

        _anomaly_lines = []
        if not (_snap_arch_h.empty and _snap_grades_h.empty and _snap_exams_h.empty):
            for _t, _tdf in home_arch_df.groupby("tutor_name"):
                _ac = {
                    "arch_count":    int(_tdf["should_archive"].sum()),
                    "unsched_hrs":   float(_tdf["unscheduled_hours"].sum()),
                    "no_grades":     0, "stale_grades": 0,
                    "no_exam":       0, "stale_exams":  0,
                    "pct_with_video": None,
                }
                if not home_grades_df.empty and _t in home_grades_df["tutor_name"].values:
                    _gd = home_grades_df[home_grades_df["tutor_name"] == _t]
                    _ac["no_grades"] = int(_gd.groupby("student_id")["score"]
                                          .apply(lambda s: s.isna().all()).sum())
                if not home_video_summary_df.empty and _t in home_video_summary_df["tutor_name"].values:
                    _ac["pct_with_video"] = float(
                        home_video_summary_df[home_video_summary_df["tutor_name"] == _t].iloc[0]["pct_with_video"])
                _af     = compute_anomalies(_t, _snap_arch_h, _snap_grades_h, _snap_exams_h, _snap_video_h, _ac)
                _spiked = [k.replace("_"," ") for k, v in _af.items() if v]
                if _spiked:
                    _anomaly_lines.append(f"🔔 **{_t}** is unusually high on: {', '.join(_spiked)}")

        with _digest_placeholder.expander("📋 Daily Digest", expanded=False):
            st.markdown(_intro)
            st.markdown("")
            if _login_lines:
                st.markdown("**Since your last login:**")
                for _l in _login_lines: st.markdown(f"- {_l}")
                st.markdown("")
            if _attn_lines:
                st.markdown("**Needs attention:**")
                for _l in _attn_lines[:5]: st.markdown(f"- {_l}")
                st.markdown("")
            if _wow_lines:
                st.markdown("**Week-over-week movement:**")
                for _l in _wow_lines: st.markdown(f"- {_l}")
                st.markdown("")
            if _watched_lines:
                st.markdown("**Watched tutors:**")
                for _l in _watched_lines: st.markdown(f"- {_l}")
                st.markdown("")
            if _anomaly_lines:
                st.markdown("**Anomaly flags:**")
                for _l in _anomaly_lines: st.markdown(f"- {_l}")
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

        all_tutors_wl = sorted(
            master_tutor_df[master_tutor_df["Faculty Leader"] == "Katherine Marino"]["Full Name"]
            .dropna().unique().tolist()
        )
        current_watched = load_watchlist()

        st.markdown("### ➕ Manage Watch List")
        new_watched = st.multiselect(
            "Select tutors to watch:",
            options=all_tutors_wl,
            default=[t for t in current_watched if t in all_tutors_wl],
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
                st.session_state["_watchlist_cache"] = new_watched
                st.success(f"Watch list saved — {len(new_watched)} tutor(s) being watched.")
                st.rerun()
        with col_clear:
            if st.button("🗑️ Clear All", key="clear_watchlist"):
                for t in current_watched:
                    remove_watchlist_baseline(t)
                    delete_watchlist_note(t)
                    delete_watchlist_thresholds(t)
                save_watchlist([])
                st.session_state["_watchlist_cache"] = []
                st.success("Watch list cleared.")
                st.rerun()

        watched = st.session_state.get("_watchlist_cache", load_watchlist())
        if not watched:
            st.info("Your watch list is empty. Select tutors above and click Save.")
            st.stop()

        st.divider()

        load_errors_wl = []
        with st.spinner("Loading watch list data..."):
            try:
                raw_wl_arch, _ = load_archivable_unscheduled()
                raw_wl_arch["should_archive"] = raw_wl_arch["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                wl_arch_df = raw_wl_arch[raw_wl_arch["team_name"] == "Team Marino"].copy()
            except Exception as e:
                wl_arch_df = pd.DataFrame(); load_errors_wl.append(f"Archivable: {e}")

            try:
                raw_wl_grades, _ = load_grades_data()
                wl_grades_df = raw_wl_grades[raw_wl_grades["team_name"] == "Team Marino"].copy()
                now_wl = pd.Timestamp.now(tz="UTC")
                wl_grades_df["updated_at"] = pd.to_datetime(wl_grades_df["updated_at"], errors="coerce", utc=True)
                wl_grades_df["days_since_update"] = (now_wl - wl_grades_df["updated_at"]).dt.days
            except Exception as e:
                wl_grades_df = pd.DataFrame(); load_errors_wl.append(f"Grades: {e}")

            try:
                raw_wl_exam, _ = load_exam_data()
                wl_exam_df = raw_wl_exam[raw_wl_exam["team_name"] == "Team Marino"].copy()
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    wl_exam_df[dc] = pd.to_datetime(wl_exam_df[dc], errors="coerce", utc=True)
                for nc in ["score","act_english","act_math","act_reading","act_science",
                           "sat_math","sat_rw","attended_test_prep_hours"]:
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
                    lambda r: (pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")) and (
                        _sat_ok(r) if r["exam_family"] == "SAT/PSAT"
                        else (_act_ok(r) if r["exam_family"] == "ACT" else False)), axis=1)
            except Exception as e:
                wl_exam_df = pd.DataFrame(); load_errors_wl.append(f"Exams: {e}")

            try:
                wl_kpi_df = load_kpi_data()
            except Exception as e:
                wl_kpi_df = pd.DataFrame(); load_errors_wl.append(f"KPI: {e}")

            wl_video_df         = pd.DataFrame()
            wl_video_summary_df = pd.DataFrame()
            try:
                raw_wl_video, _ = load_parent_update_videos()
                wl_video_df = raw_wl_video[
                    (raw_wl_video["faculty leader"] == "Team Marino") &
                    (raw_wl_video["tutor"] != "Katherine Marino")
                ].copy()
                wl_video_df["duration_secs"] = wl_video_df["video duration"].apply(duration_to_secs)
                if not wl_video_df.empty:
                    wl_video_summary_df = build_video_tutor_summary(wl_video_df)
            except Exception as e:
                load_errors_wl.append(f"Video: {e}")

        if load_errors_wl:
            with st.expander("⚠️ Some data failed to load"):
                for err in load_errors_wl:
                    st.warning(err)

        st.markdown(f"### 👀 Watching {len(watched)} tutor{'s' if len(watched)>1 else ''}")
        baselines      = load_watchlist_baselines()
        wl_snap_arch   = load_snapshots()
        wl_snap_grades = load_grades_snapshots()
        wl_snap_exams  = load_exams_snapshots()
        wl_snap_video  = load_video_snapshots()

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
            delta_html   = (
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

            arch_count = 0; unsched_hrs = 0.0
            if not wl_arch_df.empty:
                tarch       = wl_arch_df[wl_arch_df["tutor_name"] == tutor]
                arch_count  = tarch[tarch["should_archive"] == True]["student_name"].nunique()
                unsched_hrs = tarch["unscheduled_hours"].sum()

            no_grades_count = 0; stale_count_wl = 0
            if not wl_grades_df.empty and tutor in wl_grades_df["tutor_name"].values:
                tgdf = wl_grades_df[wl_grades_df["tutor_name"] == tutor]
                no_grades_count = int(tgdf.groupby("student_id")["score"]
                                       .apply(lambda s: s.isna().all()).sum())
                has_g  = tgdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded = tgdf[tgdf["student_id"].isin(has_g[has_g].index)]
                if not graded.empty:
                    latest_g       = graded.groupby("student_id")["days_since_update"].min()
                    stale_count_wl = int((latest_g > 90).sum())

            no_exam_count = 0; stale_exam_count_wl = 0; hours_per_exam_wl = None
            if not wl_exam_df.empty and tutor in wl_exam_df["tutor_name"].values:
                tedf    = wl_exam_df[wl_exam_df["tutor_name"] == tutor]
                now_wl2 = pd.Timestamp.now(tz="UTC")
                no_exam_count = sum(
                    1 for sid, sdf in tedf.groupby("student_id")
                    if sdf["attended_test_prep_hours"].iloc[0] >= 6
                    and sdf[sdf["exam_valid_composite"] == True].empty
                )
                for sid, sdf in tedf.groupby("student_id"):
                    completed = sdf[sdf["exam_valid_composite"] == True]
                    if not completed.empty:
                        latest_ex = pd.to_datetime(completed["exam_date"], utc=True).max()
                        if pd.notna(latest_ex) and (now_wl2 - latest_ex).days > 90:
                            stale_exam_count_wl += 1
                total_hrs_wl = tedf["attended_test_prep_hours"].iloc[0] if not tedf.empty else 0
                completed_ex = tedf[tedf["exam_valid_composite"] == True]["exam_id"].nunique() \
                               if "exam_id" in tedf.columns else 0
                if completed_ex > 0 and pd.notna(total_hrs_wl):
                    hours_per_exam_wl = round(total_hrs_wl / completed_ex, 1)

            pct_unscheduled_wl = 0.0
            if not wl_arch_df.empty:
                tarch_all  = wl_arch_df[wl_arch_df["tutor_name"] == tutor]
                total_prov = tarch_all["hours_remaining"].sum() + tarch_all["unscheduled_hours"].sum()
                pct_unscheduled_wl = round(
                    tarch_all["unscheduled_hours"].sum() / total_prov * 100, 1
                ) if total_prov > 0 else 0.0

            pct_with_video_wl = None; videos_found_wl = None
            updates_sent_wl   = None; median_dur_wl   = "N/A"
            if not wl_video_summary_df.empty and tutor in wl_video_summary_df["tutor_name"].values:
                vrow              = wl_video_summary_df[wl_video_summary_df["tutor_name"] == tutor].iloc[0]
                pct_with_video_wl = float(vrow["pct_with_video"])
                videos_found_wl   = int(vrow["videos_found"])
                updates_sent_wl   = int(vrow["updates_sent"])
                median_dur_wl     = secs_to_duration(vrow["median_secs"])

            save_watchlist_baseline(
                tutor, arch_count, unsched_hrs, no_grades_count, stale_count_wl,
                no_exam_count, stale_exam_count_wl, hours_per_exam_wl,
                pct_unscheduled_wl, pct_with_video_wl
            )

            anomaly_flags = compute_anomalies(
                tutor, wl_snap_arch, wl_snap_grades, wl_snap_exams, wl_snap_video, {
                    "arch_count":    arch_count,
                    "unsched_hrs":   unsched_hrs,
                    "no_grades":     no_grades_count,
                    "stale_grades":  stale_count_wl,
                    "no_exam":       no_exam_count,
                    "stale_exams":   stale_exam_count_wl,
                    "pct_with_video": pct_with_video_wl,
                }
            )

            baselines = load_watchlist_baselines()
            bl = None; added_date = None
            if not baselines.empty and tutor in baselines["tutor_name"].values:
                bl_row     = baselines[baselines["tutor_name"] == tutor].iloc[0]
                bl         = bl_row.to_dict()
                added_date = bl_row.get("added_date", None)

            def delta_str(current, baseline_key):
                if bl is None or baseline_key not in bl:
                    return None
                try:
                    baseline_val = bl[baseline_key]
                    if baseline_val is None or baseline_val == "" or \
                            (isinstance(baseline_val, float) and pd.isna(baseline_val)):
                        return None
                    if current is None:
                        return None
                    diff = float(current) - float(baseline_val)
                    if diff == 0: return "no change since added"
                    arrow = "↓" if diff < 0 else "↑"
                    sign  = "+" if diff > 0 else ""
                    return f"{arrow} {sign}{diff:g} since added ({added_date})"
                except Exception:
                    return None

            t             = get_tutor_thresholds(tutor)
            video_thresh  = t.get("pct_with_video", 80)

            issues = sum([
                arch_count >= t["arch_count"],
                no_grades_count >= t["no_grades"],
                stale_count_wl >= t["stale_grades"],
                no_exam_count >= t["no_exam"],
                stale_exam_count_wl >= t["stale_exams"],
                unsched_hrs >= t["unsched_hrs"] if t["unsched_hrs"] > 0 else False,
                pct_unscheduled_wl >= t["pct_unscheduled"],
                (pct_with_video_wl is not None and pct_with_video_wl < video_thresh),
            ])
            header_color = "#cc0000" if issues >= 2 else ("#b35c00" if issues == 1 else "#1a6e36")
            status_dot   = "🔴" if issues >= 2 else ("🟡" if issues == 1 else "🟢")
            status_text  = f"{issues} issue{'s' if issues != 1 else ''}" if issues > 0 else "No issues"
            added_label  = f" · Watching since {added_date}" if added_date else ""

            notes_df   = load_watchlist_notes()
            tutor_note = ""; note_date = ""
            if not notes_df.empty and tutor in notes_df["tutor_name"].values:
                note_row   = notes_df[notes_df["tutor_name"] == tutor].iloc[0]
                tutor_note = str(note_row.get("note", ""))
                note_date  = str(note_row.get("updated_at", ""))

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
                        value=tutor_note, height=120,
                        placeholder="e.g. Parent complaint on 3/15. Following up next week.",
                        key=f"note_{tutor}"
                    )
                    nc1, nc2 = st.columns(2)
                    with nc1:
                        if st.button("💾 Save Note", key=f"save_note_{tutor}"):
                            save_watchlist_note(tutor, new_note)
                            st.success("Note saved."); st.rerun()
                    with nc2:
                        if tutor_note and st.button("🗑️ Delete Note", key=f"del_note_{tutor}"):
                            delete_watchlist_note(tutor)
                            st.success("Note deleted."); st.rerun()

                with thresh_col:
                    st.markdown("**🚨 Alert Thresholds**")
                    st.caption("Flag this tutor's metric as red when it reaches or exceeds:")
                    tc1, tc2 = st.columns(2)
                    with tc1:
                        th_arch  = st.number_input("Archivable students ≥", min_value=0,
                                                    value=int(t["arch_count"]), key=f"th_arch_{tutor}")
                        th_ng    = st.number_input("No grades ≥", min_value=0,
                                                    value=int(t["no_grades"]), key=f"th_ng_{tutor}")
                        th_sg    = st.number_input("Stale grades ≥", min_value=0,
                                                    value=int(t["stale_grades"]), key=f"th_sg_{tutor}")
                        th_ne    = st.number_input("No exam ≥", min_value=0,
                                                    value=int(t["no_exam"]), key=f"th_ne_{tutor}")
                        th_video = st.number_input("Video rate below %", min_value=0, max_value=100,
                                                    value=int(t.get("pct_with_video", 80)),
                                                    key=f"th_video_{tutor}")
                    with tc2:
                        th_se = st.number_input("Stale exams ≥", min_value=0,
                                                 value=int(t["stale_exams"]), key=f"th_se_{tutor}")
                        th_uh = st.number_input("Unscheduled hrs ≥", min_value=0,
                                                 value=int(t["unsched_hrs"]), key=f"th_uh_{tutor}")
                        th_pu = st.number_input("% Unscheduled ≥", min_value=0,
                                                 value=int(t["pct_unscheduled"]), key=f"th_pu_{tutor}")

                    thresh_btn_col, reset_btn_col = st.columns(2)
                    with thresh_btn_col:
                        if st.button("💾 Save Thresholds", key=f"save_thresh_{tutor}"):
                            save_watchlist_thresholds(tutor, {
                                "arch_count":      th_arch,
                                "unsched_hrs":     th_uh,
                                "pct_unscheduled": th_pu,
                                "no_grades":       th_ng,
                                "stale_grades":    th_sg,
                                "no_exam":         th_ne,
                                "stale_exams":     th_se,
                                "pct_with_video":  th_video,
                            })
                            st.success("Thresholds saved."); st.rerun()
                    with reset_btn_col:
                        if st.button("↩️ Reset to Defaults", key=f"reset_thresh_{tutor}"):
                            delete_watchlist_thresholds(tutor)
                            st.success("Reset to defaults."); st.rerun()

            st.markdown("<div style='margin-top:4px;'></div>", unsafe_allow_html=True)

            if tutor_note:
                st.info(f"📌 **Note:** {tutor_note}  \n*Last updated: {note_date}*")

            # Metric rows — 3 rows of 3 to include video
            mr1c1, mr1c2, mr1c3 = st.columns(3)
            mr2c1, mr2c2, mr2c3 = st.columns(3)
            mr3c1, mr3c2, mr3c3 = st.columns(3)

            hpe_display          = f"{hours_per_exam_wl:.1f}" if hours_per_exam_wl is not None else "N/A"
            video_pct_display    = f"{pct_with_video_wl:.0f}%" if pct_with_video_wl is not None else "N/A"
            video_counts_display = f"{videos_found_wl}/{updates_sent_wl}" if videos_found_wl is not None else "N/A"

            mr1c1.markdown(_metric_html(
                "📦 Archivable Students", arch_count,
                delta_str(arch_count, "arch_count"),
                is_bad=arch_count >= t["arch_count"],
                is_anomaly=anomaly_flags.get("arch_count")), unsafe_allow_html=True)
            mr1c2.markdown(_metric_html(
                "⏳ Unscheduled Hours", f"{unsched_hrs:.1f}",
                delta_str(unsched_hrs, "unsched_hrs"),
                is_bad=unsched_hrs >= t["unsched_hrs"] if t["unsched_hrs"] > 0 else False,
                is_anomaly=anomaly_flags.get("unsched_hrs")), unsafe_allow_html=True)
            mr1c3.markdown(_metric_html(
                "📊 % Hours Unscheduled", f"{pct_unscheduled_wl:.1f}%",
                delta_str(pct_unscheduled_wl, "pct_unscheduled"),
                is_bad=pct_unscheduled_wl >= t["pct_unscheduled"]), unsafe_allow_html=True)
            mr2c1.markdown(_metric_html(
                "📋 No Grades Entered", no_grades_count,
                delta_str(no_grades_count, "no_grades"),
                is_bad=no_grades_count >= t["no_grades"],
                is_anomaly=anomaly_flags.get("no_grades")), unsafe_allow_html=True)
            mr2c2.markdown(_metric_html(
                "📚 Stale Grades >90d", stale_count_wl,
                delta_str(stale_count_wl, "stale_grades"),
                is_bad=stale_count_wl >= t["stale_grades"],
                is_anomaly=anomaly_flags.get("stale_grades")), unsafe_allow_html=True)
            mr2c3.markdown(_metric_html(
                "📝 No Completed Exam", no_exam_count,
                delta_str(no_exam_count, "no_exam"),
                is_bad=no_exam_count >= t["no_exam"],
                is_anomaly=anomaly_flags.get("no_exam")), unsafe_allow_html=True)
            mr3c1.markdown(_metric_html(
                "🕐 Stale Exams >90d", stale_exam_count_wl,
                delta_str(stale_exam_count_wl, "stale_exams"),
                is_bad=stale_exam_count_wl >= t["stale_exams"],
                is_anomaly=anomaly_flags.get("stale_exams")), unsafe_allow_html=True)
            mr3c2.markdown(_metric_html(
                "📹 Video Rate", video_pct_display,
                delta_str(pct_with_video_wl, "pct_with_video"),
                is_bad=(pct_with_video_wl is not None and pct_with_video_wl < video_thresh),
                is_anomaly=anomaly_flags.get("pct_with_video")), unsafe_allow_html=True)
            mr3c3.markdown(_metric_html(
                "🎬 Videos / Updates", video_counts_display,
                None, is_bad=False), unsafe_allow_html=True)

            st.markdown("<div style='margin-top:6px;'></div>", unsafe_allow_html=True)

            # Drill-through expanders
            drill_items = []

            if arch_count > 0 and not wl_arch_df.empty:
                _arch_names = sorted(wl_arch_df[
                    (wl_arch_df["tutor_name"] == tutor) &
                    (wl_arch_df["should_archive"] == True)
                ]["student_name"].dropna().unique().tolist())
                if _arch_names:
                    drill_items.append(("📦 Archivable Students", _arch_names))

            if unsched_hrs > 0 and not wl_arch_df.empty:
                _unsched_df = wl_arch_df[
                    (wl_arch_df["tutor_name"] == tutor) &
                    (wl_arch_df["unscheduled_hours"] > 0)
                ][["student_name","unscheduled_hours"]].dropna()
                _unsched_df = _unsched_df.sort_values("unscheduled_hours", ascending=False)
                if not _unsched_df.empty:
                    drill_items.append(("⏳ Unscheduled Hours", [
                        f"{row['student_name']} — {row['unscheduled_hours']:.1f} hrs"
                        for _, row in _unsched_df.iterrows()
                    ]))

            if no_grades_count > 0 and not wl_grades_df.empty:
                _tgdf     = wl_grades_df[wl_grades_df["tutor_name"] == tutor]
                _no_g_ids = _tgdf.groupby("student_id")["score"].apply(lambda s: s.isna().all())
                _no_g_names = sorted(_tgdf[_tgdf["student_id"].isin(
                    _no_g_ids[_no_g_ids].index)]["student_name"].dropna().unique().tolist())
                if _no_g_names:
                    drill_items.append(("📋 No Grades Entered", _no_g_names))

            if stale_count_wl > 0 and not wl_grades_df.empty:
                _tgdf  = wl_grades_df[wl_grades_df["tutor_name"] == tutor]
                _hany  = _tgdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                _graded = _tgdf[_tgdf["student_id"].isin(_hany[_hany].index)]
                if not _graded.empty:
                    _latest_g = _graded.groupby(["student_id","student_name"])["days_since_update"].min().reset_index()
                    _stale_g  = _latest_g[_latest_g["days_since_update"] > 90].sort_values("days_since_update", ascending=False)
                    if not _stale_g.empty:
                        drill_items.append(("📚 Stale Grades >90d", [
                            f"{row['student_name']} — {int(row['days_since_update'])}d since update"
                            for _, row in _stale_g.iterrows()
                        ]))

            if no_exam_count > 0 and not wl_exam_df.empty:
                _tedf = wl_exam_df[wl_exam_df["tutor_name"] == tutor]
                _no_ex_names = sorted(set(
                    sname
                    for sid, sdf in _tedf.groupby("student_id")
                    if sdf["attended_test_prep_hours"].iloc[0] >= 6
                    and sdf[sdf["exam_valid_composite"] == True].empty
                    for sname in sdf["student_name"].dropna().unique()
                ))
                if _no_ex_names:
                    drill_items.append(("📝 No Completed Exam", _no_ex_names))

            if stale_exam_count_wl > 0 and not wl_exam_df.empty:
                _tedf  = wl_exam_df[wl_exam_df["tutor_name"] == tutor]
                _now_d = pd.Timestamp.now(tz="UTC")
                _stale_ex_names = []
                for sid, sdf in _tedf.groupby("student_id"):
                    _comp = sdf[sdf["exam_valid_composite"] == True]
                    if not _comp.empty:
                        _latest = pd.to_datetime(_comp["exam_date"], utc=True).max()
                        if pd.notna(_latest) and (_now_d - _latest).days > 90:
                            _stale_ex_names.append(
                                f"{sdf['student_name'].iloc[0]} — {(_now_d - _latest).days}d since last exam")
                if _stale_ex_names:
                    drill_items.append(("🕐 Stale Exams >90d", sorted(_stale_ex_names)))

            # Video drill-through
            if pct_with_video_wl is not None and pct_with_video_wl < 100 and not wl_video_df.empty:
                _tvdf = wl_video_df[
                    (wl_video_df["tutor"] == tutor) &
                    (wl_video_df["video found"] == False)
                ]
                if not _tvdf.empty:
                    _no_vid_names = sorted(_tvdf["student"].dropna().unique().tolist())
                    drill_items.append(("📹 Missing Video", _no_vid_names))

            if drill_items:
                with st.expander(f"👥 Student detail — {tutor}", expanded=False):
                    for label, names in drill_items:
                        st.markdown(f"**{label}**")
                        for name in names:
                            st.markdown(f"- {name}")
                        st.markdown("")

            # KPI trend charts
            if not wl_kpi_df.empty:
                wl_kpi_df["Date Range Parsed"] = pd.to_datetime(
                    wl_kpi_df["Date Range"].str.split(" - ").str[0], errors="coerce")
                tutor_kpi = wl_kpi_df[wl_kpi_df["Tutor Name"] == tutor].copy()
                if not tutor_kpi.empty:
                    kpi_trend_metrics = [
                        ("% to Delivery Target",           "Delivery %",        "#1f77b4"),
                        ("% to Availability Target",       "Availability %",    "#2ca02c"),
                        ("% Sessions on Time",             "Sessions On Time",  "#d62728"),
                        ("% Parents Updates Done on Time", "Parent Updates %",  "#ff7f0e"),
                    ]
                    tutor_kpi["Date Parsed"] = tutor_kpi["Date Range"].apply(_parse_end)
                    tutor_kpi = tutor_kpi.sort_values("Date Parsed").tail(3)
                    kpi_chart_cols = st.columns(4)
                    for ci, (m, label, color) in enumerate(kpi_trend_metrics):
                        if m not in tutor_kpi.columns:
                            continue
                        vals  = (tutor_kpi[m] * 100).round(1)
                        y_max = max(float(vals.max()) * 1.15, 110) if not vals.empty else 130
                        fig_kpi = go.Figure()
                        fig_kpi.add_trace(go.Scatter(
                            x=tutor_kpi["Date Range"], y=vals,
                            mode="lines+markers+text",
                            line=dict(width=2.5, color=color),
                            marker=dict(size=8, color=color),
                            text=vals.apply(lambda v: f"{v:.0f}%"),
                            textposition="top center", textfont=dict(size=10)
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
                            height=200, plot_bgcolor="white", paper_bgcolor="white",
                            xaxis=dict(tickangle=15, gridcolor="#f5f5f5",
                                       tickfont=dict(size=9), showline=True, linecolor="#ddd"),
                            yaxis=dict(range=[0, y_max], ticksuffix="%",
                                       gridcolor="#f5f5f5", tickfont=dict(size=9),
                                       showline=True, linecolor="#ddd"),
                            margin=dict(l=10, r=10, t=30, b=50), showlegend=False
                        )
                        kpi_chart_cols[ci].plotly_chart(fig_kpi, use_container_width=True,
                                                         key=f"wl_{tutor}_{ci}")
                else:
                    st.caption(f"No KPI trend data found for {tutor}.")

            st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

        if st.sidebar.button("🔄 Refresh Watch List Data", key="refresh_watchlist"):
            st.cache_data.clear(); st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: TUTOR PROFILE
    # ─────────────────────────────────────────────

    if page == "👤 Tutor Profile":
        st.markdown('<div class="main-title">👤 Tutor Profile</div>', unsafe_allow_html=True)

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

        # Featured badge
        try:
            _feat_df = load_featured_tutors()
            if not _feat_df.empty and profile_tutor in _feat_df["tutor"].values:
                st.markdown(
                    "<div style='background:#ffd700; color:#5a4000; border-radius:8px; "
                    "padding:10px 16px; font-size:1rem; font-weight:700; margin-bottom:10px;'>"
                    "⭐ This tutor is currently <u>Featured</u></div>",
                    unsafe_allow_html=True)
        except Exception:
            pass

        # Low delivery + not accepting flag
        try:
            _ld_flag_df = load_low_delivery_not_accepting("Katherine Marino")
            if not _ld_flag_df.empty and profile_tutor in _ld_flag_df["tutor"].values:
                st.markdown(
                    "<div style='background:#ff000018; color:#c62828; border:1px solid #c62828; "
                    "border-radius:8px; padding:10px 16px; font-size:0.95rem; font-weight:600; margin-bottom:10px;'>"
                    "🚨 This tutor has <b>accepting new students turned OFF</b> and is projected <b>below 80% of their delivery target</b> over the next 3 weeks.</div>",
                    unsafe_allow_html=True)
        except Exception:
            pass

        # Brand permissions pills
        try:
            bp_df = load_brand_permissions()
            if not bp_df.empty and "tutor_name" in bp_df.columns:
                tutor_brands = bp_df[bp_df["tutor_name"] == profile_tutor]["brand_name"].dropna().unique().tolist()
                tutor_brands = sorted(set(tutor_brands))
                if tutor_brands:
                    _brand_colors = {
                        "Private Tutoring":           "#1f77b4",
                        "Back-Up Care Tutoring":      "#ff7f0e",
                        "Academics":                  "#2ca02c",
                        "Trial":                      "#9467bd",
                        "Small Group Course":         "#e377c2",
                        "Group Course":               "#8c564b",
                        "Boot Camp":                  "#d62728",
                        "School-Pay Private Tutoring":"#17becf",
                        "School-Pay Small Group Course":"#bcbd22",
                        "Test Prep 101":              "#7f7f7f",
                        "Revolution Now":             "#aec7e8",
                        "Tutoring":                   "#ffbb78",
                    }
                    pills_html = " ".join([
                        f"<span style='background:{_brand_colors.get(b,'#888')}22; "
                        f"color:{_brand_colors.get(b,'#888')}; "
                        f"border:1px solid {_brand_colors.get(b,'#888')}; "
                        f"border-radius:12px; padding:3px 10px; "
                        f"font-size:0.78rem; font-weight:600; margin:2px; "
                        f"display:inline-block;'>{b}</span>"
                        for b in tutor_brands
                    ])
                    st.markdown(f"<div style='margin-bottom:8px;'>{pills_html}</div>",
                                unsafe_allow_html=True)
        except Exception:
            pass

        st.markdown("---")

        p_errors = []
        with st.spinner(f"Loading data for {profile_tutor}…"):
            try:
                raw_p_arch, _ = load_archivable_unscheduled()
                raw_p_arch["should_archive"] = raw_p_arch["should_archive"].apply(
                    lambda x: bool(x) if pd.notna(x) else False)
                p_arch = raw_p_arch[
                    (raw_p_arch["team_name"] == "Team Marino") &
                    (raw_p_arch["tutor_name"] == profile_tutor)
                ].copy()
            except Exception as e:
                p_arch = pd.DataFrame(); p_errors.append(f"Archivable: {e}")

            try:
                raw_p_grades, _ = load_grades_data()
                p_grades = raw_p_grades[
                    (raw_p_grades["team_name"] == "Team Marino") &
                    (raw_p_grades["tutor_name"] == profile_tutor)
                ].copy()
                _now = pd.Timestamp.now(tz="UTC")
                p_grades["updated_at"] = pd.to_datetime(p_grades["updated_at"], errors="coerce", utc=True)
                p_grades["days_since_update"] = (_now - p_grades["updated_at"]).dt.days
            except Exception as e:
                p_grades = pd.DataFrame(); p_errors.append(f"Grades: {e}")

            try:
                raw_p_exam, _ = load_exam_data()
                p_exam = raw_p_exam[
                    (raw_p_exam["team_name"] == "Team Marino") &
                    (raw_p_exam["tutor_name"] == profile_tutor)
                ].copy()
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    p_exam[dc] = pd.to_datetime(p_exam[dc], errors="coerce", utc=True)
                for nc in ["score","act_english","act_math","act_reading","act_science",
                           "sat_math","sat_rw","attended_test_prep_hours"]:
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
                    lambda r: (pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")) and (
                        _sat_ok_p(r) if r["exam_family"] == "SAT/PSAT"
                        else (_act_ok_p(r) if r["exam_family"] == "ACT" else False)), axis=1)
                p_exam["is_official"] = p_exam["subject"].str.lower().str.contains("official", na=False)
            except Exception as e:
                p_exam = pd.DataFrame(); p_errors.append(f"Exams: {e}")

            try:
                p_kpi_df = load_kpi_data()
                p_kpi    = p_kpi_df[p_kpi_df["Tutor Name"] == profile_tutor].copy() \
                           if not p_kpi_df.empty else pd.DataFrame()
            except Exception as e:
                p_kpi = pd.DataFrame(); p_errors.append(f"KPI: {e}")

            try:
                p_monthly   = load_monthly_metric()
                p_monthly_t = p_monthly[p_monthly["Tutor Name"] == profile_tutor].copy() \
                              if not p_monthly.empty else pd.DataFrame()
                p_master    = load_master_tutor()
                p_annual    = load_annual_reviews()
            except Exception as e:
                p_monthly_t = pd.DataFrame(); p_master = pd.DataFrame(); p_annual = pd.DataFrame()
                p_errors.append(f"Monthly KPI: {e}")

            p_scores_df = pd.DataFrame()
            try:
                _p_scores_all = load_progress_history()
                if _p_scores_all.empty:
                    _p_scores_all, _ = load_progress_scores()
                if not _p_scores_all.empty:
                    p_scores_df = _p_scores_all[
                        _p_scores_all["tutor"] == profile_tutor
                    ].copy()
                    p_scores_df["sent_at"] = pd.to_datetime(p_scores_df["sent_at"], errors="coerce")
            except Exception as e:
                p_errors.append(f"Progress Scores: {e}")

            p_video_df  = pd.DataFrame()
            p_video_row = None
            try:
                raw_p_video, _ = load_parent_update_videos()
                p_video_df = raw_p_video[
                    (raw_p_video["faculty leader"] == "Team Marino") &
                    (raw_p_video["tutor"] == profile_tutor)
                ].copy()
                p_video_df["duration_secs"] = p_video_df["video duration"].apply(duration_to_secs)
                if not p_video_df.empty:
                    all_video = raw_p_video[
                        (raw_p_video["faculty leader"] == "Team Marino") &
                        (raw_p_video["tutor"] != "Katherine Marino")
                    ].copy()
                    all_video["duration_secs"] = all_video["video duration"].apply(duration_to_secs)
                    all_summary = build_video_tutor_summary(all_video)
                    if profile_tutor in all_summary["tutor_name"].values:
                        p_video_row = all_summary[all_summary["tutor_name"] == profile_tutor].iloc[0]
            except Exception as e:
                p_errors.append(f"Video: {e}")

        if p_errors:
            with st.expander("⚠️ Some data failed to load"):
                for err in p_errors:
                    st.warning(err)

        # Watchlist status
        st.markdown("### 👀 Watchlist Status")
        watched_list = load_watchlist()
        on_watchlist = profile_tutor in watched_list
        if on_watchlist:
            st.success(f"✅ **{profile_tutor}** is on your watch list.")
            notes_df_p = load_watchlist_notes()
            if not notes_df_p.empty and profile_tutor in notes_df_p["tutor_name"].values:
                note_row_p = notes_df_p[notes_df_p["tutor_name"] == profile_tutor].iloc[0]
                st.info(f"📌 **Note:** {note_row_p['note']}\n\n*Last updated: {note_row_p.get('updated_at','')}*")
            else:
                st.caption("No notes saved for this tutor.")
        else:
            st.warning(f"⚠️ **{profile_tutor}** is not currently on your watch list.")

        st.markdown("---")

        # ── PDF Download ──────────────────────────────────────────────────
        try:
            _concern_df_pdf = load_tutor_concerns()
        except Exception:
            _concern_df_pdf = pd.DataFrame()

        with st.expander("⬇️ Download Tutor Profile as PDF", expanded=False):
            st.markdown("**Select sections to include:**")
            _pdf_col1, _pdf_col2, _pdf_col3 = st.columns(3)
            with _pdf_col1:
                _inc_arch    = st.checkbox("📦 Archivable & Unscheduled", value=True, key="pdf_inc_arch")
                _inc_grades  = st.checkbox("📚 Grades Summary",           value=True, key="pdf_inc_grades")
            with _pdf_col2:
                _inc_exams   = st.checkbox("📝 Exam History",             value=True, key="pdf_inc_exams")
                _inc_video   = st.checkbox("📹 Parent Update Videos",     value=True, key="pdf_inc_video")
            with _pdf_col3:
                _inc_kpi     = st.checkbox("📈 KPI Trends",               value=True, key="pdf_inc_kpi")
                _inc_concern = st.checkbox("📌 Concern History",          value=True, key="pdf_inc_concern")
                _inc_scores  = st.checkbox("📝 Update Quality Scores",    value=True, key="pdf_inc_scores")

            if st.button("Generate PDF", key="download_pdf"):
                with st.spinner("Generating PDF..."):
                    try:
                        pdf_bytes = generate_tutor_pdf(
                            tutor_name     = profile_tutor,
                            generated_date = pd.Timestamp.now().strftime("%B %d, %Y"),
                            p_arch         = p_arch,
                            p_grades       = p_grades,
                            p_exam         = p_exam,
                            p_video_row    = p_video_row,
                            p_video_df     = p_video_df,
                            p_monthly_t    = p_monthly_t,
                            p_kpi_df       = p_kpi_df,
                            concern_df     = _concern_df_pdf,
                            faculty_leader = faculty_leader_name,
                            inc_arch       = _inc_arch,
                            inc_grades     = _inc_grades,
                            inc_exams      = _inc_exams,
                            inc_video      = _inc_video,
                            inc_kpi        = _inc_kpi,
                            inc_concern    = _inc_concern,
                            inc_scores     = _inc_scores,
                            p_scores_df    = p_scores_df,
                        )
                        st.download_button(
                            label     = "📄 Click to Download PDF",
                            data      = pdf_bytes,
                            file_name = f"{profile_tutor.replace(' ','_')}_Profile_{pd.Timestamp.now().strftime('%Y%m%d')}.pdf",
                            mime      = "application/pdf",
                            key       = "pdf_download_btn"
                        )
                    except Exception as e:
                        st.error(f"Could not generate PDF: {e}")

        st.markdown("---")

        # Active Student Brand Breakdown
        st.markdown("### 👥 Active Students by Brand")
        if not p_arch.empty:
            active_df = p_arch[p_arch["should_archive"] == False]
            if not active_df.empty:
                brand_counts = active_df.groupby("brand")["student_name"].nunique().reset_index()
                brand_counts.columns = ["Brand", "Students"]
                brand_counts = brand_counts.sort_values("Students", ascending=False)
                brand_cols = st.columns(min(len(brand_counts), 4))
                brand_colors = {
                    "Private Tutoring":    "#1f77b4",
                    "Back-Up Care Tutoring": "#ff7f0e",
                    "Academics":           "#2ca02c",
                    "Trial":               "#9467bd",
                }
                for i, (_, row) in enumerate(brand_counts.iterrows()):
                    with brand_cols[i % len(brand_cols)]:
                        color = brand_colors.get(row["Brand"], "#888")
                        st.markdown(f"""
                        <div style='background:#f7f9fc; border-left:4px solid {color};
                                    border-radius:6px; padding:10px 14px; margin-bottom:8px;'>
                            <div style='font-size:0.8rem; color:#666;'>{row["Brand"]}</div>
                            <div style='font-size:1.6rem; font-weight:700; color:{color};'>{int(row["Students"])}</div>
                            <div style='font-size:0.75rem; color:#999;'>active student{"s" if row["Students"] != 1 else ""}</div>
                        </div>""", unsafe_allow_html=True)
            else:
                st.info("No active students found for this tutor.")
        else:
            st.info("No student data available.")

        st.markdown("---")

        # ── Subject & Student Breakdown ───────────────────────────────────
        st.markdown("### 📚 Subjects by Student")
        with st.expander("View subject breakdown", expanded=False):
            _subj_rows = []

            # Academic subjects — query study areas using student_id for accuracy
            try:
                _active_sids = []
                if p_arch is not None and not p_arch.empty and "student_id" in p_arch.columns:
                    # Only non-archivable students
                    _active_sids = p_arch[p_arch["should_archive"] != True]["student_id"].dropna().unique().tolist()

                if _active_sids:
                    _sa_conn = get_redshift_connection()
                    _sid_list = ",".join(str(int(s)) for s in _active_sids)
                    _sa_q = f"""
                        SELECT DISTINCT
                            su.first_name||' '||su.last_name AS student_name,
                            sub.name AS subject
                        FROM orbit_stitch.study_areas sa
                        JOIN dw.subjects sub ON sa.subject_id = sub.id
                        JOIN dw.students st ON sa.student_id = st.id
                        JOIN dw.users su ON st.user_id = su.id
                        WHERE sa.student_id IN ({_sid_list})
                          AND sa.archived_at IS NULL
                          AND sa._sdc_deleted_at IS NULL
                          AND sub.category_id IN (1,2,3,4,5,8,9,10,11)
                          AND CAST(sub.high_grade AS INT) > 8
                    """
                    _sa_df = pd.read_sql(_sa_q, _sa_conn)
                    _sa_conn.close()
                    for _, row in _sa_df.iterrows():
                        _subj_rows.append({
                            "Student": row["student_name"],
                            "Subject": row["subject"],
                            "Type": "Academic"
                        })
            except Exception:
                if p_grades is not None and not p_grades.empty:
                    for _, row in p_grades[["student_name","subject"]].dropna().drop_duplicates().iterrows():
                        _subj_rows.append({
                            "Student": row["student_name"],
                            "Subject": row["subject"],
                            "Type": "Academic"
                        })

            # Test prep from exam data — only active (non-archivable) students
            _active_student_names = set(
                p_arch[p_arch["should_archive"] != True]["student_name"].dropna().unique().tolist()
            ) if p_arch is not None and not p_arch.empty and "should_archive" in p_arch.columns else set()
            if p_exam is not None and not p_exam.empty:
                _tp_map = {
                    "SAT": "SAT", "Digital SAT": "SAT", "Paper SAT": "SAT",
                    "ACT": "ACT", "Digital ACT": "ACT",
                    "PSAT/NMSQT": "PSAT", "Digital PSAT": "PSAT",
                    "Digital PSAT/NMSQT": "PSAT", "PSAT": "PSAT", "PSAT 8/9": "PSAT",
                    "Paper PSAT/NMSQT": "PSAT", "Paper PSAT 8/9": "PSAT",
                }
                for _, row in p_exam[["student_name","subject"]].dropna().drop_duplicates().iterrows():
                    if _active_student_names and row["student_name"] not in _active_student_names:
                        continue
                    mapped = _tp_map.get(str(row["subject"]), str(row["subject"]))
                    _subj_rows.append({
                        "Student": row["student_name"],
                        "Subject": mapped,
                        "Type": "Test Prep"
                    })

            if _subj_rows:
                _subj_df = pd.DataFrame(_subj_rows).drop_duplicates()

                # Summary: subject → # students
                _summary = (_subj_df.groupby(["Subject","Type"])["Student"]
                            .nunique().reset_index()
                            .rename(columns={"Student":"# Students"})
                            .sort_values(["Type","# Students"], ascending=[True, False]))

                col1, col2 = st.columns([1, 2])
                with col1:
                    st.markdown("**Subject Summary**")
                    st.dataframe(_summary, use_container_width=True, hide_index=True)
                with col2:
                    st.markdown("**Student → Subjects**")
                    _detail = (_subj_df.groupby("Student")["Subject"]
                               .apply(lambda x: ", ".join(sorted(x.unique())))
                               .reset_index()
                               .rename(columns={"Subject":"Subjects"})
                               .sort_values("Student"))
                    st.dataframe(_detail, use_container_width=True, hide_index=True)
            else:
                st.info("No subject data available for this tutor.")

        st.markdown("---")

        # Archivable
        st.markdown("### 📦 Archivable Students & Unscheduled Hours")
        if p_arch.empty:
            st.info("No archivable/unscheduled data found for this tutor.")
        else:
            arch_students  = p_arch[p_arch["should_archive"] == True]
            n_arch         = len(arch_students)
            unsched_total  = round(p_arch["unscheduled_hours"].sum(), 1)
            total_students = p_arch["student_name"].nunique()
            total_prov     = p_arch["hours_remaining"].sum() + p_arch["unscheduled_hours"].sum()
            pct_unsched    = round(p_arch["unscheduled_hours"].sum() / total_prov * 100, 1) \
                             if total_prov > 0 else 0.0
            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("Active Students",     total_students)
            pc2.metric("Archivable Students", n_arch,
                       delta=f"{n_arch} flagged" if n_arch > 0 else None,
                       delta_color="inverse")
            pc3.metric("Unscheduled Hours",   f"{unsched_total:.1f}")
            pc4.metric("% Hours Unscheduled", f"{pct_unsched:.1f}%")
            if not arch_students.empty:
                st.markdown("**Students flagged for archiving:**")
                show_cols = [c for c in ["student_name","brand","hours_remaining","unscheduled_hours"]
                             if c in arch_students.columns]
                arch_display_p = arch_students[show_cols].sort_values(
                    ["unscheduled_hours","student_name"], ascending=[False, True]).rename(columns={
                    "student_name":"Student","brand":"Brand",
                    "hours_remaining":"Hours Remaining","unscheduled_hours":"Unscheduled Hours"})
                st.dataframe(arch_display_p, use_container_width=True, hide_index=True)

        st.markdown("---")


        # Grades
        st.markdown("### 📚 Grades Summary")
        if p_grades.empty:
            st.info("No grades data found for this tutor.")
        else:
            # Solo tutor toggle
            _g_solo = st.checkbox("Solo Tutor Only", value=False, key="profile_grades_solo",
                help="Only show students where this tutor is their only tutor in the last 30 days")
            _g_df = p_grades[p_grades["tutor_count"] == 1].copy() \
                    if _g_solo and "tutor_count" in p_grades.columns else p_grades.copy()

            def _grade_metrics(df):
                total   = df["student_id"].nunique()
                no_ids  = df.groupby("student_id")["score"].apply(lambda s: s.isna().all())
                n_no    = int(no_ids.sum())
                has_any = df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded  = df[df["student_id"].isin(has_any[has_any].index)]
                if not graded.empty:
                    latest = graded.groupby("student_id")["days_since_update"].min()
                    n_st   = int((latest > 90).sum())
                    avg_d  = round(latest.mean(), 1)
                else:
                    n_st = 0; avg_d = None
                return total, n_no, n_st, avg_d

            total_all, n_no_all, n_stale_all, avg_all = _grade_metrics(p_grades)
            solo_df = p_grades[p_grades["tutor_count"] == 1] if "tutor_count" in p_grades.columns else pd.DataFrame()
            total_solo, n_no_solo, n_stale_solo, avg_solo = _grade_metrics(solo_df) if not solo_df.empty else (0,0,0,None)

            st.markdown("**All Students**")
            gc1, gc2, gc3 = st.columns(3)
            gc1.metric("Students with Grades", total_all - n_no_all)
            gc2.metric("No Grades Entered", n_no_all,
                       delta=f"{n_no_all} missing" if n_no_all > 0 else None,
                       delta_color="inverse")
            gc3.metric("Stale Grades (>90d)", n_stale_all,
                       delta=f"avg {avg_all}d since update" if avg_all else None,
                       delta_color="inverse" if n_stale_all > 0 else "off")

            if not solo_df.empty:
                st.markdown("**Solo Tutor Students Only**")
                gs1, gs2, gs3 = st.columns(3)
                gs1.metric("Students with Grades", total_solo - n_no_solo)
                gs2.metric("No Grades Entered", n_no_solo,
                           delta=f"{n_no_solo} missing" if n_no_solo > 0 else None,
                           delta_color="inverse")
                gs3.metric("Stale Grades (>90d)", n_stale_solo,
                           delta=f"avg {avg_solo}d since update" if avg_solo else None,
                           delta_color="inverse" if n_stale_solo > 0 else "off")

            # Brand breakdown on tutor profile
            if "private_tutoring" in p_grades.columns:
                st.markdown("**Grade Metrics by Brand**")
                _brand_cols_p = {
                    "Private Tutoring": "private_tutoring",
                    "Back-Up Care":     "buc",
                    "Academics":        "academics",
                    "School Pay":       "school_pay",
                }
                _brand_rows_p = []
                for _bl, _bc in _brand_cols_p.items():
                    _b = _brand_grade_summary(_g_df, _bc) if "_brand_grade_summary" in dir() else None
                    if _b is None and _bc in _g_df.columns:
                        _bdf = _g_df[_g_df[_bc].astype(str) == "True"]
                        if not _bdf.empty:
                            _tot = _bdf["student_id"].nunique()
                            _no  = int(_bdf.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
                            _ha  = _bdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                            _gr  = _bdf[_bdf["student_id"].isin(_ha[_ha].index)]
                            _st  = int((_gr.groupby("student_id")["days_since_update"].min() > 90).sum()) if not _gr.empty else 0
                            _b   = {"students": _tot, "no_grades": _no, "stale": _st,
                                    "pct_graded": round((_tot - _no) / _tot * 100, 1) if _tot > 0 else 0}
                    if _b:
                        _brand_rows_p.append({
                            "Brand":         _bl,
                            "Students":      _b["students"],
                            "No Grades":     _b["no_grades"],
                            "Stale (>90d)":  _b["stale"],
                            "% With Grades": f"{_b['pct_graded']:.0f}%",
                        })
                if _brand_rows_p:
                    st.dataframe(pd.DataFrame(_brand_rows_p), use_container_width=True, hide_index=True)

            st.markdown("**Student grade detail:**")
            g_summary = []
            for sid, sdf in _g_df.groupby("student_id"):
                sname       = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else sid
                n_subjects  = sdf["subject"].nunique() if "subject" in sdf.columns else 0
                n_entered   = int(sdf["score"].notna().sum())
                last_update = sdf["days_since_update"].min() if n_entered > 0 else None
                stale_flag  = "⚠️" if last_update is not None and last_update > 90 else \
                              ("✅" if last_update is not None else "❌")
                grade_lvl   = int(sdf["grade_lvl"].iloc[0]) if "grade_lvl" in sdf.columns and pd.notna(sdf["grade_lvl"].iloc[0]) else "—"
                tutor_cnt   = int(sdf["tutor_count"].iloc[0]) if "tutor_count" in sdf.columns and pd.notna(sdf["tutor_count"].iloc[0]) else "—"
                brands = []
                if "private_tutoring" in sdf.columns and sdf["private_tutoring"].iloc[0]: brands.append("Private")
                if "buc" in sdf.columns and sdf["buc"].iloc[0]: brands.append("BUC")
                if "academics" in sdf.columns and sdf["academics"].iloc[0]: brands.append("Academics")
                if "school_pay" in sdf.columns and sdf["school_pay"].iloc[0]: brands.append("School Pay")
                g_summary.append({
                    "Student":           sname,
                    "Grade Lvl":         grade_lvl,
                    "# Tutors":          tutor_cnt,
                    "Brands":            ", ".join(brands) if brands else "—",
                    "Subjects":          n_subjects,
                    "Grades Entered":    n_entered,
                    "Days Since Update": int(last_update) if last_update is not None else "—",
                    "Status":            stale_flag,
                })
            g_sum_df = pd.DataFrame(g_summary)
            _status_order = {"❌": 0, "⚠️": 1, "✅": 2, "—": 3}
            g_sum_df["_sort_status"] = g_sum_df["Status"].map(_status_order).fillna(3)
            g_sum_df["_sort_name"]   = g_sum_df["Student"].str.lower()
            g_sum_df = g_sum_df.sort_values(["_sort_status","_sort_name"]).drop(
                columns=["_sort_status","_sort_name"])
            st.dataframe(g_sum_df, use_container_width=True, hide_index=True)

        st.markdown("---")

        # Exams
        st.markdown("### 📝 Exam & Test Prep History")
        if p_exam.empty:
            st.info("No exam data found for this tutor.")
        else:
            p_now       = pd.Timestamp.now(tz="UTC")
            ex_students = p_exam["student_id"].nunique() if "student_id" in p_exam.columns else 0
            valid_ids   = p_exam[p_exam["exam_valid_composite"] == True]["student_id"].unique()
            no_exam_ids = [sid for sid, sdf in p_exam.groupby("student_id")
                           if sdf["attended_test_prep_hours"].iloc[0] >= 6
                           and sdf[sdf["exam_valid_composite"] == True].empty]
            stale_exam_count = 0
            for sid, sdf in p_exam.groupby("student_id"):
                completed = sdf[sdf["exam_valid_composite"] == True]
                if not completed.empty:
                    latest_ex = pd.to_datetime(completed["exam_date"], utc=True).max()
                    if pd.notna(latest_ex) and (p_now - latest_ex).days > 90:
                        stale_exam_count += 1
            # Avg hrs/exam: average of per-student (hrs / completed_exams), matching Test Prep tab
            _hpe_vals = []
            for _sid, _sdf in p_exam.groupby("student_id"):
                _hrs  = _sdf["attended_test_prep_hours"].iloc[0]
                _comp = _sdf[_sdf["exam_valid_composite"] == True]["exam_id"].nunique()                         if "exam_id" in _sdf.columns else _sdf[_sdf["exam_valid_composite"] == True].shape[0]
                if _comp > 0 and pd.notna(_hrs):
                    _hpe_vals.append(_hrs / _comp)
            hrs_per_exam = round(sum(_hpe_vals) / len(_hpe_vals), 1) if _hpe_vals else None
            total_hrs    = p_exam.groupby("student_id")["attended_test_prep_hours"].first().sum() if not p_exam.empty else 0
            n_completed  = p_exam[p_exam["exam_valid_composite"] == True]["exam_id"].nunique() \
                           if "exam_id" in p_exam.columns else len(valid_ids)
            ec1, ec2, ec3, ec4 = st.columns(4)
            ec1.metric("Test Prep Students", ex_students)
            ec2.metric("No Completed Exam", len(no_exam_ids),
                       delta=f"{len(no_exam_ids)} flagged" if no_exam_ids else None,
                       delta_color="inverse")
            ec3.metric("Stale Exams (>90d)", stale_exam_count,
                       delta_color="inverse" if stale_exam_count > 0 else "off")
            ec4.metric("Avg Hrs / Exam", f"{hrs_per_exam:.1f}" if hrs_per_exam else "N/A")
            ex_rows = []
            # Load study areas for goal/starting scores — matched by exam family
            try:
                _sa_p = load_study_areas()
                _TP_IDS = {43, 51, 315, 316, 342, 195, 50, 356}
                if not _sa_p.empty and "exam_family" in _sa_p.columns:
                    _sa_p = (_sa_p[_sa_p["subject_id"].isin(_TP_IDS)]
                        .sort_values("goal_score", ascending=False, na_position="last")
                        .groupby(["student_id","exam_family"]).first().reset_index()
                        [["student_id","exam_family","goal_score","starting_score"]])
                else:
                    _sa_p = pd.DataFrame()
            except Exception:
                _sa_p = pd.DataFrame()

            # Map exam_family in p_exam to SAT/PSAT/ACT
            SAT_SUBJ  = {51, 315, 147}
            PSAT_SUBJ = {316, 50, 342, 195, 240, 344}
            ACT_SUBJ  = {43, 356, 239}
            def _map_fam(row):
                subj = row.get("subject","")
                fam  = row.get("exam_family","")
                if fam == "ACT": return "ACT"
                if fam in ("SAT/PSAT","SAT"): 
                    psat_names = {"PSAT/NMSQT","Digital PSAT","Digital PSAT/NMSQT","PSAT","PSAT 8/9","Paper PSAT/NMSQT","Paper PSAT 8/9"}
                    return "PSAT" if subj in psat_names else "SAT"
                return fam

            for sid, sdf in p_exam.groupby("student_id"):
                sname = sdf["student_name"].iloc[0] if "student_name" in sdf.columns else str(sid)
                hrs   = sdf["attended_test_prep_hours"].iloc[0]
                valid = sdf[sdf["exam_valid_composite"] == True].copy()
                if not valid.empty:
                    valid["exam_family_split"] = valid.apply(_map_fam, axis=1)
                else:
                    valid["exam_family_split"] = []

                def _get_fam_data(fam_name):
                    fv = valid[valid["exam_family_split"] == fam_name] if not valid.empty else pd.DataFrame()
                    best  = fv["score"].max() if not fv.empty else None
                    n     = len(fv)
                    lat   = pd.to_datetime(fv["exam_date"], utc=True).max() if not fv.empty else None
                    days  = int((p_now - lat).days) if lat is not None and pd.notna(lat) else None
                    _sar  = None
                    if not _sa_p.empty and sid in _sa_p["student_id"].values:
                        _sm = _sa_p[(_sa_p["student_id"]==sid) & (_sa_p["exam_family"]==fam_name)]
                        if not _sm.empty:
                            _sar = _sm.sort_values("goal_score", ascending=False, na_position="last").iloc[0]
                    gs  = float(_sar["goal_score"])     if _sar is not None and pd.notna(_sar["goal_score"])     else None
                    ss  = float(_sar["starting_score"]) if _sar is not None and pd.notna(_sar["starting_score"]) else None
                    gst = ("✅ Met" if best is not None and gs is not None and float(best) >= gs
                           else ("❌ Not Met" if gs is not None and best is not None else "—"))
                    st  = ("✅ Current" if days is not None and days <= 90
                           else ("⚠️ Stale" if days is not None else "—"))
                    return n, best, gs, ss, gst, days, st

                sat_n,  sat_best,  sat_goal,  sat_start,  sat_gst,  sat_days,  sat_st  = _get_fam_data("SAT")
                psat_n, psat_best, psat_goal, psat_start, psat_gst, psat_days, psat_st = _get_fam_data("PSAT")
                act_n,  act_best,  act_goal,  act_start,  act_gst,  act_days,  act_st  = _get_fam_data("ACT")

                overall_days = min([d for d in [sat_days, psat_days, act_days] if d is not None], default=None)
                overall_status = ("✅ Current" if overall_days is not None and overall_days <= 90
                                  else ("⚠️ Stale" if overall_days is not None else
                                  ("❌ None (6+ hrs)" if (pd.notna(hrs) and hrs >= 6) else "—")))

                ex_rows.append({
                    "Student":          sname,
                    "Hours Delivered":  round(float(hrs), 1) if pd.notna(hrs) else "—",
                    "SAT Exams":        sat_n  if sat_n  > 0 else "—",
                    "SAT Best":         int(sat_best)  if sat_best  is not None and pd.notna(sat_best)  else "—",
                    "SAT Start":        int(sat_start) if sat_start is not None else "—",
                    "SAT Goal":         int(sat_goal)  if sat_goal  is not None else "—",
                    "SAT Goal Status":  sat_gst  if sat_n  > 0 else "—",
                    "PSAT Exams":       psat_n if psat_n > 0 else "—",
                    "PSAT Best":        int(psat_best)  if psat_best  is not None and pd.notna(psat_best)  else "—",
                    "PSAT Start":       int(psat_start) if psat_start is not None else "—",
                    "PSAT Goal":        int(psat_goal)  if psat_goal  is not None else "—",
                    "PSAT Goal Status": psat_gst if psat_n > 0 else "—",
                    "ACT Exams":        act_n  if act_n  > 0 else "—",
                    "ACT Best":         int(act_best)  if act_best  is not None and pd.notna(act_best)  else "—",
                    "ACT Start":        int(act_start) if act_start is not None else "—",
                    "ACT Goal":         int(act_goal)  if act_goal  is not None else "—",
                    "ACT Goal Status":  act_gst  if act_n  > 0 else "—",
                    "Status":           overall_status,
                })
            ex_df = pd.DataFrame(ex_rows)
            _ex_status_order = {"❌ None (6+ hrs)": 0, "⚠️ Stale": 1, "✅ Current": 2, "—": 3}
            ex_df["_sort_status"] = ex_df["Status"].map(_ex_status_order).fillna(3)
            ex_df["_sort_name"]   = ex_df["Student"].str.lower()
            ex_df = ex_df.sort_values(["_sort_status","_sort_name"]).drop(
                columns=["_sort_status","_sort_name"])

            # Color-shade SAT/PSAT/ACT column groups
            SAT_COLS  = [c for c in ["SAT Exams","SAT Best","SAT Start","SAT Goal","SAT Goal Status"] if c in ex_df.columns]
            PSAT_COLS = [c for c in ["PSAT Exams","PSAT Best","PSAT Start","PSAT Goal","PSAT Goal Status"] if c in ex_df.columns]
            ACT_COLS  = [c for c in ["ACT Exams","ACT Best","ACT Start","ACT Goal","ACT Goal Status"] if c in ex_df.columns]

            def _shade_cols(df):
                styles = pd.DataFrame("", index=df.index, columns=df.columns)
                for col in SAT_COLS:
                    if col in styles.columns:
                        styles[col] = "background-color: #e8f4fd"
                for col in PSAT_COLS:
                    if col in styles.columns:
                        styles[col] = "background-color: #fef9e7"
                for col in ACT_COLS:
                    if col in styles.columns:
                        styles[col] = "background-color: #eafaf1"
                return styles

            st.dataframe(ex_df.style.apply(_shade_cols, axis=None),
                         use_container_width=True, hide_index=True)

        st.markdown("---")

        # Video section
        st.markdown("### 📹 Parent Update Videos")
        if p_video_row is None or p_video_df.empty:
            st.info("No parent update video data found for this tutor.")
        else:
            pv1, pv2, pv3, pv4, pv5, pv6, pv7 = st.columns(7)
            pv1.metric("Updates Required", int(p_video_row["updates_required"]))
            pv2.metric("Updates Sent",     int(p_video_row["updates_sent"]))
            pv3.metric("Parent Update %",  f"{p_video_row['parent_update_pct']:.0f}%",
                       delta_color="inverse" if p_video_row["parent_update_pct"] < 80 else "off")
            pv4.metric("Videos Found",     int(p_video_row["videos_found"]))
            pv5.metric("Video Rate",       f"{p_video_row['pct_with_video']:.0f}%",
                       delta_color="inverse" if p_video_row["pct_with_video"] < 80 else "off")
            pv6.metric("HW Mentions",      int(p_video_row.get("homework_count", 0)))
            pv7.metric("HW %",             f"{p_video_row.get('pct_with_homework', 0):.0f}%",
                       delta_color="inverse" if p_video_row.get("pct_with_homework", 0) < 80 else "off")
            pv_med = st.columns(1)[0]
            pv_med.metric("Median Duration", secs_to_duration(p_video_row["median_secs"]))

            short_v = p_video_df[p_video_df["duration_secs"] < 10]
            long_v  = p_video_df[p_video_df["duration_secs"] > 300]
            if not short_v.empty:
                st.warning(f"⚡ **{len(short_v)} video{'s' if len(short_v)>1 else ''}** under 10 seconds — may be accidental uploads.")
            if not long_v.empty:
                st.info(f"⏱️ **{len(long_v)} video{'s' if len(long_v)>1 else ''}** over 5 minutes detected.")

            # Video trend
            p_video_snap = load_video_snapshots()
            if not p_video_snap.empty and profile_tutor in p_video_snap["tutor_name"].values:
                tv_snap = p_video_snap[p_video_snap["tutor_name"] == profile_tutor].sort_values("week_date")
                if len(tv_snap) >= 2:
                    fig_vt = px.line(tv_snap, x="week_date", y="pct_with_video",
                                     markers=True,
                                     title=f"{profile_tutor} — Video Rate Week over Week",
                                     color_discrete_sequence=["#7b2d8b"])
                    fig_vt.add_hline(y=80, line_dash="dash", line_color="#cc0000",
                                     annotation_text="80% threshold")
                    fig_vt.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Week", yaxis_title="% With Video",
                        yaxis=dict(range=[0,105]), height=280,
                        margin=dict(l=20, r=20, t=50, b=40))
                    st.plotly_chart(fig_vt, use_container_width=True)

            st.markdown("**Student-level video detail:**")
            v_detail = p_video_df[
                [c for c in ["student","week of","sessions attended",
                              "video found","video duration","scrape error"]
                 if c in p_video_df.columns]
            ].rename(columns={
                "student":           "Student",
                "week of":           "Week Of",
                "sessions attended": "Sessions",
                "video found":       "Video Found",
                "video duration":    "Duration",
                "scrape error":      "Error",
            })
            def _highlight_v(row):
                if row.get("Video Found") == False:
                    return ["background-color: #ffe5e5"] * len(row)
                return [""] * len(row)
            st.dataframe(v_detail.style.apply(_highlight_v, axis=1),
                         use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── Progress Update Quality Scores ────────────────────────────────────
        st.markdown("### 📝 Progress Update Quality Scores")
        if p_scores_df.empty:
            st.info("No progress update scores found for this tutor.")
        else:
            # Latest week summary
            latest_week = p_scores_df["sent_at"].max()
            week_start  = pd.Timestamp(latest_week).to_period("W-SAT").start_time
            week_end    = week_start + pd.Timedelta(days=7)
            latest_df   = p_scores_df[
                (p_scores_df["sent_at"] >= week_start) &
                (p_scores_df["sent_at"] <  week_end)
            ]
            all_time_df = p_scores_df

            ps1, ps2, ps3, ps4, ps5 = st.columns(5)
            ps1.metric("Avg Total (Latest Week)",    f"{latest_df['total'].mean():.1f} / 10" if not latest_df.empty else "—")
            ps2.metric("Avg What Worked On",         f"{latest_df['what_worked_on'].mean():.1f} / 2" if not latest_df.empty else "—")
            ps3.metric("Avg Goals",                  f"{latest_df['goals'].mean():.1f} / 2" if not latest_df.empty else "—")
            ps4.metric("Avg Velocity",               f"{latest_df['velocity'].mean():.1f} / 3" if not latest_df.empty else "—")
            ps5.metric("Avg Plan Forward",           f"{latest_df['plan_forward'].mean():.1f} / 3" if not latest_df.empty else "—")

            ps6, ps7 = st.columns(2)
            ps6.metric("Updates This Week",          len(latest_df))
            ps7.metric("Avg Total (All Time)",       f"{all_time_df['total'].mean():.1f} / 10")

            # Trend chart
            _p_snap = load_progress_snapshots()
            if not _p_snap.empty and "tutor" in _p_snap.columns and profile_tutor in _p_snap["tutor"].values:
                _tv_p = _p_snap[_p_snap["tutor"] == profile_tutor].sort_values("week_date")
                if len(_tv_p) >= 2:
                    fig_pt = px.line(_tv_p, x="week_date", y="avg_total",
                                     markers=True,
                                     title=f"{profile_tutor} — Avg Total Score Week over Week",
                                     color_discrete_sequence=["#004466"])
                    fig_pt.add_hline(y=7, line_dash="dash", line_color="#cc0000",
                                     annotation_text="7.0 target")
                    fig_pt.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Week", yaxis_title="Avg Total Score",
                        yaxis=dict(range=[0, 11]), height=280,
                        margin=dict(l=20, r=20, t=50, b=40),
                        plot_bgcolor="white", paper_bgcolor="white")
                    st.plotly_chart(fig_pt, use_container_width=True)

            # Individual updates table
            st.markdown("**Individual updates:**")
            _p_detail = p_scores_df.sort_values("sent_at", ascending=False)[[
                "sent_at","student_name","what_worked_on","goals","velocity","plan_forward","total","notes"
            ]].copy()
            _p_detail["sent_at"] = _p_detail["sent_at"].dt.strftime("%Y-%m-%d")
            _p_detail = _p_detail.rename(columns={
                "sent_at":        "Date",
                "student_name":   "Student",
                "what_worked_on": "Worked On",
                "goals":          "Goals",
                "velocity":       "Velocity",
                "plan_forward":   "Plan",
                "total":          "Total",
                "notes":          "AI Notes",
            })
            def _highlight_ps(row):
                score = row.get("Total", 10)
                if score < 5: return ["background-color: #ffe5e5"] * len(row)
                if score < 7: return ["background-color: #fff3cc"] * len(row)
                return [""] * len(row)
            st.dataframe(
                _p_detail.style.apply(_highlight_ps, axis=1),
                use_container_width=True, hide_index=True,
                column_config={
                    "Worked On": st.column_config.NumberColumn("Worked On", help="0-2", format="%d"),
                    "Goals":     st.column_config.NumberColumn("Goals",     help="0-2", format="%d"),
                    "Velocity":  st.column_config.NumberColumn("Velocity",  help="0-3", format="%d"),
                    "Plan":      st.column_config.NumberColumn("Plan",      help="0-3", format="%d"),
                    "Total":     st.column_config.NumberColumn("Total",     help="0-10", format="%d"),
                    "AI Notes":  st.column_config.TextColumn("AI Notes", width="large"),
                }
            )

        st.markdown("---")

        # KPI trends
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

            team_names_p   = p_master[p_master["Faculty Leader"] == "Katherine Marino"]["Full Name"].dropna()
            team_monthly_p = p_monthly[p_monthly["Tutor Name"].isin(team_names_p)].copy() \
                             if not p_monthly.empty else pd.DataFrame()
            if not team_monthly_p.empty:
                team_monthly_p["Date Parsed"] = team_monthly_p["Date Range"].apply(_parse_end_p)

            kpi_metrics_p = {
                "% to Delivery Target":           ("Delivery %",       True),
                "% to Availability Target":       ("Availability %",   True),
                "% Sessions on Time":             ("Sessions On Time", True),
                "% Parents Updates Done on Time": ("Parent Updates %", True),
            }

            kpi_cols = st.columns(2)
            for ci, (metric, (label, is_pct)) in enumerate(kpi_metrics_p.items()):
                if metric not in p_monthly_t.columns:
                    continue
                plot_t = p_monthly_t[["Date Range","Date Parsed",metric]].dropna()                     .drop_duplicates(subset=["Date Parsed"], keep="first")                     .sort_values("Date Parsed").tail(8).copy()
                if is_pct:
                    plot_t[metric] = plot_t[metric] * 100
                _p_dates     = plot_t["Date Parsed"].tolist()
                _p_ticklabels = plot_t["Date Range"].tolist()
                fig_p = px.line(plot_t, x="Date Parsed", y=metric,
                                title=label, markers=True,
                                labels={metric: "%", "Date Parsed": ""})
                if not team_monthly_p.empty and metric in team_monthly_p.columns:
                    team_plot_p = team_monthly_p.dropna(subset=["Date Parsed"]).copy()
                    if is_pct:
                        team_plot_p[metric] = team_plot_p[metric] * 100
                    tg = team_plot_p.groupby("Date Parsed")[metric].mean().reset_index()
                    tg = tg[tg["Date Parsed"].isin(_p_dates)].sort_values("Date Parsed")
                    fig_p.add_scatter(x=tg["Date Parsed"], y=tg[metric],
                                      mode="lines+markers", name="Team Avg",
                                      line=dict(dash="dash", color="gray"))
                fig_p.add_hline(y=100, line_dash="dot", line_color="#aaa")
                fig_p.update_layout(
                    height=300, margin=dict(l=10,r=60,t=40,b=80),
                    xaxis=dict(tickangle=45, tickvals=_p_dates, ticktext=_p_ticklabels,
                               tickmode="array", automargin=True),
                    yaxis_title=None, xaxis_title=None,
                    legend=dict(orientation="h", y=-0.5),
                    title=dict(x=0.5, xanchor="center")
                )
                with kpi_cols[ci % 2]:
                    st.plotly_chart(fig_p, use_container_width=True,
                                    key=f"profile_kpi_{profile_tutor}_{ci}")


    # ─────────────────────────────────────────────
    # PAGE: PARENT UPDATE VIDEOS
    # ─────────────────────────────────────────────

    if page == "📹 Parent Update Videos":
        st.markdown('<div class="main-title">📹 Parent Update Videos</div>', unsafe_allow_html=True)


        try:
            raw_video_df, video_fetched_at = load_parent_update_videos()
        except Exception as e:
            st.error(f"Could not load parent update video data: {e}")
            st.stop()

        if raw_video_df.empty:
            st.info("No data available yet — run sync_parent_update_videos.py first.")
            st.stop()

        st.caption(f"🕐 Data last updated: **{video_fetched_at}**")
        st.sidebar.markdown(f"🕐 **Video data last updated**  \n{video_fetched_at}")

        # Load from history if available, fall back to current week
        _vid_history = load_parent_update_history()
        if not _vid_history.empty and "faculty leader" in _vid_history.columns:
            _all_video_df = _vid_history[
                (_vid_history["faculty leader"] == "Team Marino") &
                (_vid_history["tutor"] != "Katherine Marino")
            ].copy()
        else:
            _all_video_df = raw_video_df[
                (raw_video_df["faculty leader"] == "Team Marino") &
                (raw_video_df["tutor"] != "Katherine Marino")
            ].copy()

        if _all_video_df.empty:
            st.warning("No parent update rows found for Team Marino.")
            st.stop()

        # Week selector
        _all_video_df["week of"] = pd.to_datetime(_all_video_df["week of"], errors="coerce").dt.date
        _available_weeks_v = sorted(_all_video_df["week of"].dropna().unique(), reverse=True)
        _week_labels_v     = {w: f"Week of {w.strftime('%b %d, %Y')}" for w in _available_weeks_v}
        _selected_week_v   = st.selectbox(
            "Select Week Of (Sunday):",
            options=_available_weeks_v,
            format_func=lambda w: _week_labels_v[w],
            key="video_week_select"
        )
        team_video_df = _all_video_df[_all_video_df["week of"] == _selected_week_v].copy()

        if team_video_df.empty:
            st.warning("No data found for selected week.")
            st.stop()

        team_video_df["duration_secs"] = team_video_df["video duration"].apply(duration_to_secs)
        video_summary_df = build_video_tutor_summary(team_video_df)

        save_video_weekly_snapshot(video_summary_df)

        # Team overview
        st.markdown("### 📊 Team Overview")
        week_of = str(_selected_week_v)
        st.caption(f"Week of: **{week_of}**")

        sent_video_df      = team_video_df[team_video_df["parent update sent"].astype(str) == "True"]
        total_required_team= int(team_video_df["update required"].sum()) if "update required" in team_video_df.columns else 0
        total_updates_team = len(sent_video_df)
        parent_update_pct_team = round(total_updates_team / total_required_team * 100, 1) if total_required_team > 0 else 0
        parent_only_video_df = team_video_df[team_video_df["parent update only sent"].astype(str) == "True"]                                if "parent update only sent" in team_video_df.columns else sent_video_df
        total_parent_only_team = len(parent_only_video_df)
        total_videos_team  = int(parent_only_video_df["video found"].fillna(False).astype(bool).sum())
        pct_team           = round(total_videos_team / total_parent_only_team * 100, 1) \
                             if total_parent_only_team > 0 else 0
        hw_team_df         = sent_video_df[sent_video_df["homework mentioned"].astype(str) == "True"]                              if "homework mentioned" in sent_video_df.columns else pd.DataFrame()
        hw_team_count      = len(hw_team_df)
        pct_hw_team        = round(hw_team_count / total_updates_team * 100, 1)                              if total_updates_team > 0 else 0
        all_secs           = parent_only_video_df["duration_secs"].dropna()
        short_count        = int((all_secs < 10).sum())
        long_count         = int((all_secs > 300).sum())

        m1, m2, m3, m4, m5, m6, m7, m8, m9, m10 = st.columns(10)
        m1.metric("Updates Required", total_required_team)
        m2.metric("Updates Sent",     total_updates_team)
        m3.metric("Parent Update %",  f"{parent_update_pct_team:.1f}%",
                  delta_color="inverse" if parent_update_pct_team < 80 else "off")
        m4.metric("Videos Attached",  total_videos_team)
        m5.metric("% With Video",     f"{pct_team:.1f}%",
                  delta_color="inverse" if pct_team < 80 else "off")
        m6.metric("% Mention HW",     f"{pct_hw_team:.1f}%",
                  delta_color="inverse" if pct_hw_team < 80 else "off")
        m7.metric("Median Duration",  secs_to_duration(all_secs.median()) if not all_secs.empty else "N/A")
        m8.metric("Avg Duration",     secs_to_duration(all_secs.mean())   if not all_secs.empty else "N/A")
        m9.metric("⚡ Short (<10s)",  short_count,
                  delta_color="inverse" if short_count > 0 else "off")
        m10.metric("⏱️ Long (>5min)", long_count)

        st.divider()

        # Top concern flags
        st.markdown("### 🚨 Tutors to Address")
        fc1, fc2, fc3, fc4 = st.columns(4)
        medals_v = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        with fc1:
            st.markdown("**Low Parent Update % (Top 5)**")
            low_comp_t = video_summary_df[
                (video_summary_df["parent_update_pct"] < 80) &
                (video_summary_df["updates_required"] > 0)
            ].sort_values("parent_update_pct")
            if low_comp_t.empty:
                st.success("✅ All tutors at 80%+ parent update rate.")
            else:
                for rank, (_, row) in enumerate(low_comp_t.head(5).iterrows()):
                    st.markdown(
                        f"{medals_v[min(rank,4)]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"{row['parent_update_pct']:.0f}%</span> "
                        f"({int(row['updates_sent'])}/{int(row['updates_required'])} sent)",
                        unsafe_allow_html=True)
        with fc2:
            st.markdown("**No Videos Attached (Top 5)**")
            no_video_t = video_summary_df[
                (video_summary_df["videos_found"] == 0) &
                (video_summary_df["updates_sent"] > 0)
            ].sort_values("updates_sent", ascending=False)
            if no_video_t.empty:
                st.success("✅ All tutors attached at least one video!")
            else:
                for rank, (_, row) in enumerate(no_video_t.head(5).iterrows()):
                    st.markdown(
                        f"{medals_v[min(rank,4)]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"0 / {int(row['updates_sent'])} with video</span>",
                        unsafe_allow_html=True)
        with fc3:
            st.markdown("**Lowest Video Rate (Top 5)**")
            low_rate_t = video_summary_df[
                (video_summary_df["pct_with_video"] < 80) &
                (video_summary_df["updates_sent"] > 0)
            ].sort_values("pct_with_video")
            if low_rate_t.empty:
                st.success("✅ All tutors at 80%+ video rate.")
            else:
                for rank, (_, row) in enumerate(low_rate_t.head(5).iterrows()):
                    st.markdown(
                        f"{medals_v[min(rank,4)]} **{row['tutor_name']}** — "
                        f"<span style='color:#b35c00; font-weight:bold'>"
                        f"{row['pct_with_video']:.0f}%</span>",
                        unsafe_allow_html=True)
        with fc4:
            st.markdown("**Suspicious Video Lengths**")
            short_vids = team_video_df[team_video_df["duration_secs"] < 10][
                ["tutor","student","video duration"]]
            long_vids  = team_video_df[team_video_df["duration_secs"] > 300][
                ["tutor","student","video duration"]]
            if short_vids.empty and long_vids.empty:
                st.success("✅ No unusual video lengths detected.")
            else:
                if not short_vids.empty:
                    st.markdown(f"**⚡ Under 10 seconds ({len(short_vids)}):**")
                    for _, r in short_vids.head(3).iterrows():
                        st.markdown(f"- {r['tutor']} / {r['student']} — {r['video duration']}")
                if not long_vids.empty:
                    st.markdown(f"**⏱️ Over 5 minutes ({len(long_vids)}):**")
                    for _, r in long_vids.head(3).iterrows():
                        st.markdown(f"- {r['tutor']} / {r['student']} — {r['video duration']}")

        st.divider()

        # % With video chart
        st.markdown("### 📈 % of Updates With Video — By Tutor")
        chart_df = video_summary_df.sort_values("pct_with_video", ascending=True)
        n = len(chart_df)
        fig_pct = px.bar(
            chart_df, x="pct_with_video", y="tutor_name", orientation="h",
            color="pct_with_video",
            color_continuous_scale=["#cc0000","#ffdd99","#006400"],
            text=chart_df["pct_with_video"].apply(lambda v: f"{v:.0f}%"),
            title="% of Parent Updates With a Video Attached — By Tutor",
            height=max(350, n * 30),
        )
        fig_pct.add_vline(x=80, line_dash="dash", line_color="#cc0000",
                          annotation_text="80% threshold", annotation_position="top right")
        fig_pct.update_layout(
            title=dict(x=0.5, xanchor="center"),
            showlegend=False,
            xaxis=dict(range=[0,110], title="% With Video"),
            yaxis_title="", margin=dict(l=160, r=20, t=50, b=40)
        )
        fig_pct.update_traces(textposition="outside")
        st.plotly_chart(fig_pct, use_container_width=True)

        # Tutor summary table
        st.markdown("### 📋 Tutor Summary Table")
        display_summary = video_summary_df.copy()
        display_summary["longest"]  = display_summary["longest_secs"].apply(secs_to_duration)
        display_summary["shortest"] = display_summary["shortest_secs"].apply(secs_to_duration)
        display_summary["median"]   = display_summary["median_secs"].apply(secs_to_duration)
        display_summary = display_summary[[
            "tutor_name","updates_required","updates_sent","parent_update_pct",
            "videos_found","pct_with_video","homework_count","pct_with_homework","longest","shortest","median"
        ]].rename(columns={
            "tutor_name":        "Tutor",
            "updates_required":  "Required",
            "updates_sent":      "Sent",
            "parent_update_pct": "Parent Update %",
            "videos_found":      "Videos Found",
            "pct_with_video":    "% With Video",
            "homework_count":    "HW Mentions",
            "pct_with_homework": "HW %",
            "longest":           "Longest",
            "shortest":          "Shortest",
            "median":           "Median Duration",
        })

        def highlight_pct(row):
            pct = row.get("% With Video", 100)
            if pct == 0:  return ["background-color: #ffe5e5"] * len(row)
            if pct < 80:  return ["background-color: #fff3cc"] * len(row)
            return [""] * len(row)

        st.dataframe(display_summary.style.apply(highlight_pct, axis=1),
                     use_container_width=True, hide_index=True)

        st.divider()

        # Trends over time
        st.markdown("### 📅 Trends Over Time")
        video_snap = load_video_snapshots()
        if video_snap.empty:
            st.caption("No historical data yet — trends will build automatically each week.")
        else:
            trend_metric_v = st.selectbox(
                "Trend metric",
                ["parent_update_pct","pct_with_video","videos_found","updates_sent","median_secs"],
                format_func=lambda x: {
                    "parent_update_pct": "% Parent Updates Sent on Time",
                    "pct_with_video":    "% With Video",
                    "videos_found":      "Videos Found",
                    "updates_sent":      "Updates Sent",
                    "median_secs":       "Median Duration (secs)",
                }[x], key="video_trend_metric"
            )
            _tutor_opts_trend = sorted(video_snap["tutor_name"].dropna().unique().tolist())
            trend_tutor_v = st.selectbox(
                "Filter by tutor",
                ["All Tutors"] + _tutor_opts_trend,
                index=1 if _tutor_opts_trend else 0,
                key="video_trend_tutor"
            )
            tutors_to_plot = [trend_tutor_v] if trend_tutor_v != "All Tutors" else _tutor_opts_trend
            for tutor in tutors_to_plot:
                tsnap_v = video_snap[video_snap["tutor_name"] == tutor].sort_values("week_date")
                if len(tsnap_v) < 2:
                    if trend_tutor_v != "All Tutors":
                        st.caption(f"Only one week of data for {tutor} — trend will appear as more weeks accumulate.")
                    continue
                fig_vt = px.line(tsnap_v, x="week_date", y=trend_metric_v,
                                 markers=True,
                                 title=f"{tutor} — {trend_metric_v.replace('_',' ').title()} Week over Week",
                                 color_discrete_sequence=["#7b2d8b"])
                if trend_metric_v in ("pct_with_video", "parent_update_pct"):
                    fig_vt.add_hline(y=80, line_dash="dash", line_color="#cc0000",
                                     annotation_text="80% threshold")
                fig_vt.update_layout(
                    title=dict(x=0.5, xanchor="center"),
                    xaxis_title="Week", yaxis_title="", height=300,
                    margin=dict(l=20, r=20, t=50, b=40))
                fig_vt.update_traces(line=dict(width=2.5))
                st.plotly_chart(fig_vt, use_container_width=True)

        st.divider()

        # Row-level detail
        st.markdown("### 🔍 Row-Level Detail")
        st.caption("Use the filters below to quickly browse by tutor and week without reloading the full page.")
        _det_col1, _det_col2 = st.columns(2)
        with _det_col1:
            tutor_opts_v = ["All Tutors"] + sorted(annelies_tutors)
            sel_tutor_v  = st.selectbox("Filter by Tutor", tutor_opts_v, key="video_tutor_filter")
        with _det_col2:
            _det_week_opts = sorted(_all_video_df["week of"].dropna().unique(), reverse=True)
            _det_week_labels = {w: f"Week of {w.strftime('%b %d, %Y')}" for w in _det_week_opts}
            sel_det_week_v = st.selectbox(
                "Filter by Week",
                options=_det_week_opts,
                format_func=lambda w: _det_week_labels[w],
                index=list(_det_week_opts).index(_selected_week_v) if _selected_week_v in _det_week_opts else 0,
                key="video_detail_week"
            )
        view_video_df = _all_video_df[_all_video_df["week of"] == sel_det_week_v].copy()
        view_video_df["duration_secs"] = view_video_df["video duration"].apply(duration_to_secs)
        if sel_tutor_v != "All Tutors":
            view_video_df = view_video_df[view_video_df["tutor"] == sel_tutor_v]

        # Add progress update indicator
        if "parent update only sent" in view_video_df.columns:
            view_video_df["update type"] = view_video_df.apply(
                lambda r: "Progress Update Only" if (
                    str(r["parent update sent"]) == "True" and
                    str(r["parent update only sent"]) != "True"
                ) else ("Parent Update" if str(r["parent update only sent"]) == "True" else "—"),
                axis=1
            )
        detail_cols_v = [c for c in ["tutor","student","brand","week of","sessions attended",
                                      "parent update sent","update type","homework mentioned",
                                      "video found","video duration",
                                      "scrape error"] if c in view_video_df.columns]
        detail_display_v = view_video_df[detail_cols_v].rename(columns={
            "update type":       "Update Type",
            "homework mentioned": "HW Mentioned",
            "tutor":              "Tutor",
            "student":            "Student",
            "brand":              "Brand",
            "week of":            "Week Of",
            "sessions attended":  "Sessions Attended",
            "parent update sent": "Update Sent",
            "video found":        "Video Found",
            "video duration":     "Video Duration",
            "scrape error":       "Scrape Error",
        }).sort_values(["Tutor","Student"])

        def highlight_video_row(row):
            if row.get("Video Found") == False:
                return ["background-color: #ffe5e5"] * len(row)
            return [""] * len(row)

        st.dataframe(detail_display_v.style.apply(highlight_video_row, axis=1),
                     use_container_width=True, hide_index=True)

        out_v = io.BytesIO()
        detail_display_v.to_excel(out_v, index=False)
        out_v.seek(0)
        st.download_button(
            label="⬇️ Download Video Detail",
            data=out_v,
            file_name=f"Parent_Update_Videos_{sel_tutor_v.replace(' ','_') if sel_tutor_v != 'All Tutors' else 'Katherine_Marino'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        if st.sidebar.button("🔄 Refresh Video Data", key="refresh_videos"):
            st.cache_data.clear(); st.rerun()


# ─────────────────────────────────────────────
    # PAGE: GRADES SUMMARY
    # ─────────────────────────────────────────────

    if page == "Grades Summary":
        st.markdown('<div class="main-title">Grades Summary 📝</div>', unsafe_allow_html=True)


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

        with st.expander("ℹ️ About this data"):
            st.markdown("""
**How students appear in this report:**
- The student attended **at least 2 sessions** with this tutor in the last 30 days (1-on-1 tutoring only)
- The student has **at least one future session** scheduled with this tutor
- The student is in **9th grade or higher**
- Subjects only show if their "high grade" is 9th grade or higher

**Brand** is based on sessions in the last 30 days **or** sessions scheduled going forward. Trials are excluded as a brand filter (can't have 2 past + 1 future with trials only).

**Multiple tutors** is based on the last 30 days. When filtering to "Solo Tutor Only", only students where this tutor is their **only** tutor are shown.
            """)

        team_grades_df = raw_grades_df[raw_grades_df["team_name"] == "Team Marino"].copy()

        if team_grades_df.empty:
            st.warning("No grades records found for Team Marino.")
            st.stop()

        now = pd.Timestamp.now(tz="UTC")
        team_grades_df["updated_at"]        = pd.to_datetime(team_grades_df["updated_at"],        errors="coerce", utc=True)
        team_grades_df["days_since_update"] = (now - team_grades_df["updated_at"]).dt.days

        grades_snap_df = save_grades_weekly_snapshot(team_grades_df)

        st.markdown("### 🚨 Top Tutors to Address")

        def _brand_grade_summary(df, brand_col):
            """Return grade metrics for students with a given brand."""
            if brand_col not in df.columns:
                return None
            bdf = df[df[brand_col].astype(str) == "True"]
            if bdf.empty:
                return None
            total  = bdf["student_id"].nunique()
            no_ids = bdf.groupby("student_id")["score"].apply(lambda s: s.isna().all())
            n_no   = int(no_ids.sum())
            has_any= bdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded = bdf[bdf["student_id"].isin(has_any[has_any].index)]
            if not graded.empty:
                latest = graded.groupby("student_id")["days_since_update"].min()
                n_st   = int((latest > 90).sum())
            else:
                n_st = 0
            pct_g  = round((total - n_no) / total * 100, 1) if total > 0 else 0
            return {"students": total, "no_grades": n_no, "stale": n_st, "pct_graded": pct_g}

        def build_tutor_summary(df):
            rows = []
            for tutor, tdf in df.groupby("tutor_name"):
                total_students   = tdf["student_id"].nunique()
                no_grade_ids     = tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all())
                no_grade_students = int(no_grade_ids.sum())
                pct_graded       = (
                    tdf["score"].notna().sum() / len(tdf) * 100
                    if len(tdf) > 0 else 0
                )
                has_any_grade = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_ids    = has_any_grade[has_any_grade].index
                graded        = tdf[tdf["student_id"].isin(graded_ids)]
                if not graded.empty:
                    latest_per_student = graded.groupby("student_id")["days_since_update"].min()
                    stale_students     = int((latest_per_student > 90).sum())
                    avg_days           = round(latest_per_student.mean(), 1)
                else:
                    stale_students = 0; avg_days = None
                rows.append({
                    "tutor_name":            tutor,
                    "total_students":        total_students,
                    "students_no_grades":    no_grade_students,
                    "pct_subjects_graded":   round(pct_graded, 1),
                    "stale_grade_students":  stale_students,
                    "avg_days_since_update": avg_days,
                })
            return pd.DataFrame(rows)

        tutor_summary = build_tutor_summary(team_grades_df)
        medals        = ["🥇","🥈","🥉","4️⃣","5️⃣"]
        flag_c1, flag_c2, flag_c3 = st.columns(3)

        with flag_c1:
            st.markdown("**Most Students With No Grades (Top 5)**")
            top_no_grades = (
                tutor_summary[tutor_summary["students_no_grades"] > 0]
                .sort_values("students_no_grades", ascending=False).head(5)
            )
            if top_no_grades.empty:
                st.success("✅ All students have at least one grade entered.")
            else:
                for rank, (_, row) in enumerate(top_no_grades.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"{int(row['students_no_grades'])} students</span>",
                        unsafe_allow_html=True)

        with flag_c2:
            st.markdown("**Most Students With Stale Grades >90 Days (Top 5)**")
            top_stale = (
                tutor_summary[tutor_summary["stale_grade_students"] > 0]
                .sort_values("stale_grade_students", ascending=False).head(5)
            )
            if top_stale.empty:
                st.success("✅ All graded students have been updated within 90 days.")
            else:
                for rank, (_, row) in enumerate(top_stale.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#b35c00; font-weight:bold'>"
                        f"{int(row['stale_grade_students'])} students</span>",
                        unsafe_allow_html=True)

        with flag_c3:
            st.markdown("**Lowest % Subjects Graded (Top 5)**")
            top_low_pct = tutor_summary.sort_values("pct_subjects_graded", ascending=True).head(5)
            if top_low_pct.empty:
                st.success("✅ No data to display.")
            else:
                for rank, (_, row) in enumerate(top_low_pct.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#555; font-weight:bold'>"
                        f"{row['pct_subjects_graded']:.1f}%</span>",
                        unsafe_allow_html=True)

        st.divider()
        st.markdown("### 📊 Team Overview")

        total_students_team = team_grades_df["student_id"].nunique()
        no_grades_team      = int(
            team_grades_df.groupby("student_id")["score"].apply(lambda s: s.isna().all()).sum())
        pct_graded_team     = (
            team_grades_df["score"].notna().sum() / len(team_grades_df) * 100
            if len(team_grades_df) > 0 else 0
        )
        has_any_grade_team = team_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_ids_team    = has_any_grade_team[has_any_grade_team].index
        graded_rows        = team_grades_df[team_grades_df["student_id"].isin(graded_ids_team)]
        stale_team         = 0
        if not graded_rows.empty:
            latest_per_student = graded_rows.groupby("student_id")["days_since_update"].min()
            stale_team         = int((latest_per_student > 90).sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Active Students",   total_students_team)
        m2.metric("Students — No Grades",    no_grades_team,
                  delta=f"{no_grades_team/total_students_team*100:.0f}% of roster" if total_students_team else None,
                  delta_color="inverse")
        m3.metric("% Subject Rows Graded",   f"{pct_graded_team:.1f}%")
        m4.metric("Students w/ Stale Grades",stale_team,
                  delta="(>90 days since last update)", delta_color="inverse")

        st.divider()
        st.markdown("### 🔍 Filters")
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            tutor_opts_g = ["All Tutors"] + sorted(annelies_tutors)
            sel_tutor_g  = st.selectbox("Tutor", tutor_opts_g, key="grades_tutor")
        with fc2:
            grade_filter_opts = ["All Students","Missing Grades Only","Stale Grades Only (>90 days)"]
            sel_grade_filter  = st.selectbox("Grade Status Filter", grade_filter_opts, key="grades_filter")
        with fc3:
            solo_tutor_only = st.checkbox("Solo Tutor Only", value=False, key="grades_solo_tutor",
                help="Only show students where this tutor is their only tutor in the last 30 days")

        view_grades_df = team_grades_df.copy()
        if solo_tutor_only and "tutor_count" in view_grades_df.columns:
            view_grades_df = view_grades_df[view_grades_df["tutor_count"] == 1]
        if sel_tutor_g != "All Tutors":
            view_grades_df = view_grades_df[view_grades_df["tutor_name"] == sel_tutor_g]
        if sel_grade_filter == "Missing Grades Only":
            missing_ids    = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.isna().all())
            view_grades_df = view_grades_df[view_grades_df["student_id"].isin(missing_ids[missing_ids].index)]
        elif sel_grade_filter == "Stale Grades Only (>90 days)":
            has_any_v = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded_v  = view_grades_df[view_grades_df["student_id"].isin(has_any_v[has_any_v].index)]
            if not graded_v.empty:
                latest_per = graded_v.groupby("student_id")["days_since_update"].min()
                stale_ids  = latest_per[latest_per > 90].index
                view_grades_df = view_grades_df[view_grades_df["student_id"].isin(stale_ids)]
            else:
                view_grades_df = pd.DataFrame(columns=view_grades_df.columns)

        single_tutor_grades = sel_tutor_g != "All Tutors"
        st.divider()

        tab_team, tab_tutor, tab_detail = st.tabs([
            "📊 Team Charts", "👤 Tutor Breakdown", "📋 Student Detail"])

        with tab_team:
            # Brand filter for charts
            _brand_filter_opts = ["All Brands","Private Tutoring","Back-Up Care","Academics","School Pay"]
            _brand_col_map     = {
                "Private Tutoring": "private_tutoring",
                "Back-Up Care":     "buc",
                "Academics":        "academics",
                "School Pay":       "school_pay",
            }
            _sel_brand_chart = st.selectbox("Filter by Brand", _brand_filter_opts, key="grades_brand_chart_filter")
            if _sel_brand_chart != "All Brands":
                _bc = _brand_col_map[_sel_brand_chart]
                _chart_df = team_grades_df[team_grades_df[_bc].astype(str) == "True"]                             if _bc in team_grades_df.columns else team_grades_df
                _chart_summary = build_tutor_summary(_chart_df)
            else:
                _chart_df      = team_grades_df
                _chart_summary = tutor_summary
            if not single_tutor_grades:
                no_grades_chart = (
                    _chart_summary[_chart_summary["students_no_grades"] > 0]
                    .sort_values("students_no_grades", ascending=True)
                )
                if not no_grades_chart.empty:
                    n = len(no_grades_chart)
                    fig1 = px.bar(
                        no_grades_chart, x="students_no_grades", y="tutor_name",
                        orientation="h", color="students_no_grades",
                        color_continuous_scale=["#ffe0e0","#cc0000"],
                        text="students_no_grades",
                        title="Students With No Grades Entered — By Tutor",
                        height=max(350, n * 30))
                    fig1.update_layout(
                        title=dict(x=0.5, xanchor="center"), showlegend=False,
                        coloraxis_showscale=False, xaxis_title="# Students", yaxis_title="",
                        yaxis=dict(autorange="reversed"), margin=dict(l=160, r=20, t=50, b=40))
                    fig1.update_traces(textposition="outside")
                    st.plotly_chart(fig1, use_container_width=True)
                else:
                    st.success("✅ All students have at least one grade entered.")

                pct_chart = _chart_summary.sort_values("pct_subjects_graded", ascending=True)
                n = len(pct_chart)
                fig2 = px.bar(
                    pct_chart, x="pct_subjects_graded", y="tutor_name",
                    orientation="h", color="pct_subjects_graded",
                    color_continuous_scale=["#cc0000","#ffdd99","#006400"],
                    text=pct_chart["pct_subjects_graded"].apply(lambda v: f"{v:.0f}%"),
                    title="% of Subject Rows With a Grade Entered — By Tutor",
                    height=max(350, n * 30))
                fig2.update_layout(
                    title=dict(x=0.5, xanchor="center"), showlegend=False,
                    coloraxis_showscale=False, xaxis_title="% Graded", yaxis_title="",
                    xaxis=dict(range=[0, 110]), yaxis=dict(autorange="reversed"),
                    margin=dict(l=160, r=20, t=50, b=40))
                fig2.update_traces(textposition="outside")
                st.plotly_chart(fig2, use_container_width=True)

                stale_chart = (
                    _chart_summary[_chart_summary["stale_grade_students"] > 0]
                    .sort_values("stale_grade_students", ascending=True)
                )
                if not stale_chart.empty:
                    n = len(stale_chart)
                    fig3 = px.bar(
                        stale_chart, x="stale_grade_students", y="tutor_name",
                        orientation="h", color="stale_grade_students",
                        color_continuous_scale=["#fff3cc","#b35c00"],
                        text="stale_grade_students",
                        title="Students With Grades Not Updated in 90+ Days — By Tutor",
                        height=max(350, n * 30))
                    fig3.update_layout(
                        title=dict(x=0.5, xanchor="center"), showlegend=False,
                        coloraxis_showscale=False, xaxis_title="# Students", yaxis_title="",
                        yaxis=dict(autorange="reversed"), margin=dict(l=160, r=20, t=50, b=40))
                    fig3.update_traces(textposition="outside")
                    st.plotly_chart(fig3, use_container_width=True)
                else:
                    st.success("✅ No stale grades on the team (all updated within 90 days).")
            else:
                sel_summary = tutor_summary[tutor_summary["tutor_name"] == sel_tutor_g]
                if not sel_summary.empty:
                    row = sel_summary.iloc[0]
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    sc1.metric("Total Students",       int(row["total_students"]))
                    sc2.metric("No Grades",            int(row["students_no_grades"]), delta_color="inverse")
                    sc3.metric("% Subjects Graded",    f"{row['pct_subjects_graded']:.1f}%")
                    sc4.metric("Stale Grade Students", int(row["stale_grade_students"]), delta_color="inverse")

                has_grade    = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_tutor = view_grades_df[view_grades_df["student_id"].isin(has_grade[has_grade].index)].copy()
                if not graded_tutor.empty:
                    per_student_days = (
                        graded_tutor.groupby(["student_id","student_name"])["days_since_update"]
                        .min().reset_index().sort_values("days_since_update", ascending=True)
                    )
                    fig_days = px.bar(
                        per_student_days, x="days_since_update", y="student_name",
                        orientation="h",
                        text=per_student_days["days_since_update"].apply(lambda d: f"{d}d"),
                        title=f"{sel_tutor_g} — Days Since Last Grade Update (per student)",
                        height=max(300, len(per_student_days) * 30),
                        color="days_since_update",
                        color_continuous_scale=["#2a7a2a","#ffaa00","#cc0000"],
                        range_color=[0, max(per_student_days["days_since_update"].max(), 91)])
                    fig_days.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Days Since Last Grade Update",
                        yaxis_title="", showlegend=False, coloraxis_showscale=False,
                        margin=dict(l=160, r=20, t=50, b=40))
                    fig_days.add_vline(x=90, line_dash="dash", line_color="red",
                                       annotation_text="90-day threshold",
                                       annotation_position="top right")
                    fig_days.update_traces(textposition="outside")
                    st.plotly_chart(fig_days, use_container_width=True)

        with tab_tutor:

            gsnap = load_grades_snapshots()
            tutors_to_show = [sel_tutor_g] if single_tutor_grades else \
                              sorted(team_grades_df["tutor_name"].dropna().unique().tolist())
            trend_metric = st.selectbox(
                "Trend metric",
                ["students_no_grades","stale_grade_students","pct_subjects_graded","avg_days_since_update"],
                format_func=lambda x: {
                    "students_no_grades":    "Students With No Grades",
                    "stale_grade_students":  "Students With Stale Grades (>90d)",
                    "pct_subjects_graded":   "% Subjects Graded",
                    "avg_days_since_update": "Avg Days Since Last Update"
                }[x], key="grades_trend_metric"
            )
            if gsnap.empty:
                st.caption("No historical snapshot data yet — trends will build automatically each week.")
            else:
                for tutor in tutors_to_show:
                    tsnap = gsnap[gsnap["tutor_name"] == tutor].sort_values("week_date")
                    if len(tsnap) < 2:
                        if single_tutor_grades:
                            st.caption(f"Only one week of data for {tutor} — trend will appear once more weeks are recorded.")
                        continue
                    color = {
                        "students_no_grades":    "#cc0000",
                        "stale_grade_students":  "#b35c00",
                        "pct_subjects_graded":   "#006400",
                        "avg_days_since_update": "#003f7f",
                    }[trend_metric]
                    fig_t = px.line(
                        tsnap, x="week_date", y=trend_metric, markers=True,
                        title=f"{tutor} — {trend_metric.replace('_',' ').title()} Week over Week",
                        color_discrete_sequence=[color])
                    fig_t.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Week", yaxis_title="",
                        height=300, margin=dict(l=20, r=20, t=50, b=40))
                    fig_t.update_traces(line=dict(width=2.5))
                    st.plotly_chart(fig_t, use_container_width=True)

        with tab_detail:
            # Brand breakdown
            st.markdown("#### 📊 Grade Metrics by Brand")
            _brand_cols_map2 = {
                "Private Tutoring": "private_tutoring",
                "Back-Up Care":     "buc",
                "Academics":        "academics",
                "School Pay":       "school_pay",
            }
            _brand_rows2 = []
            for _bl2, _bc2 in _brand_cols_map2.items():
                _b2 = _brand_grade_summary(view_grades_df, _bc2)
                if _b2:
                    _brand_rows2.append({
                        "Brand":         _bl2,
                        "Students":      _b2["students"],
                        "No Grades":     _b2["no_grades"],
                        "Stale (>90d)":  _b2["stale"],
                        "% With Grades": f"{_b2['pct_graded']:.0f}%",
                    })
            if _brand_rows2:
                st.dataframe(pd.DataFrame(_brand_rows2), use_container_width=True, hide_index=True)
            st.divider()
            if view_grades_df.empty:
                st.info("No records match the current filters.")
            else:
                detail_cols_g = [
                    "tutor_name","student_name","grade_lvl","tutor_count",
                    "private_tutoring","buc","academics","school_pay",
                    "subject","score","updated_at","days_since_update"
                ]
                detail_cols_g  = [c for c in detail_cols_g if c in view_grades_df.columns]
                detail_display = view_grades_df[detail_cols_g].copy()
                if "updated_at" in detail_display.columns:
                    detail_display["updated_at"] = detail_display["updated_at"].apply(
                        lambda x: x.strftime("%Y-%m-%d") if pd.notna(x) else "—")
                detail_display = detail_display.rename(columns={
                    "tutor_name":        "Tutor",
                    "student_name":      "Student",
                    "grade_lvl":         "Grade Level",
                    "tutor_count":       "# Tutors",
                    "private_tutoring":  "Private",
                    "buc":               "BUC",
                    "academics":         "Academics",
                    "school_pay":        "School Pay",
                    "subject":           "Subject",
                    "score":             "Grade",
                    "updated_at":        "Grade Last Updated",
                    "days_since_update": "Days Since Update",
                }).sort_values(["Tutor","Student","Subject"])

                def highlight_grade_row(row):
                    days  = row.get("Days Since Update")
                    grade = row.get("Grade")
                    if pd.isna(grade):
                        return ["background-color: #ffe5e5"] * len(row)
                    if pd.notna(days) and days > 90:
                        return ["background-color: #fff3cc"] * len(row)
                    return [""] * len(row)

                st.markdown(
                    "🔴 Red rows = no grade entered &nbsp;&nbsp; "
                    "🟡 Yellow rows = grade not updated in 90+ days",
                    unsafe_allow_html=True)
                st.dataframe(
                    detail_display.style.apply(highlight_grade_row, axis=1),
                    use_container_width=True, hide_index=True)

                output_g = io.BytesIO()
                detail_display.to_excel(output_g, index=False)
                output_g.seek(0)
                st.download_button(
                    label="⬇️ Download Grades Detail",
                    data=output_g,
                    file_name=f"Grades_Detail_{sel_tutor_g.replace(' ','_') if sel_tutor_g != 'All Tutors' else 'Katherine_Marino'}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        if st.sidebar.button("🔄 Refresh Grades Data", key="refresh_grades"):
            st.cache_data.clear(); st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: TEST PREP & EXAMS
    # ─────────────────────────────────────────────

    if page == "Test Prep & Exams":
        st.markdown('<div class="main-title">Test Prep & Exams 📝</div>', unsafe_allow_html=True)
        with st.expander("ℹ️ About this data"):
            st.markdown("""
**For a student to show:**
- Tutor and student must have **at least 1 attended test prep session in the last 45 days**
- Have completed **at least 4 hours** of attended test prep tutoring **OR** have had sessions over the course of **4 weeks**

**Notes:**
- If a tutor doesn't complete subject allocation for a session (e.g. auto-attendance), the session will not be included in completed test prep hours or velocity calculations
- **Valid exams** are defined as:
  - Student made an attempt in all sections
  - On SAT/PSAT: all section scores must be ≥ 300
  - On ACT: all section scores (except Science) must be ≥ 10
  - Only **first attempts** of exams count
- **Baseline exam**: the last valid exam before/on first tutoring session. If none exists, the first valid exam after the first tutoring session is used
- If test prep is covered in **multiple brands**: hours are combined, session dates use the earliest across all brands, hours remaining is based on the brand with the most hours remaining
- If test prep is covered in an **Academics session**: it is counted in delivered sessions, but Academics are not included for sessions scheduled in the future
            """)


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

        team_exam_df = raw_exam_df[raw_exam_df["team_name"] == "Team Marino"].copy()
        if team_exam_df.empty:
            st.warning("No exam records found for Team Marino.")
            st.stop()

        for dc in ["first_session_day","most_recent_session","exam_date"]:
            team_exam_df[dc] = pd.to_datetime(team_exam_df[dc], errors="coerce", utc=True)
        for nc in ["score","act_english","act_math","act_reading","act_science",
                   "sat_math","sat_rw","attended_test_prep_hours"]:
            team_exam_df[nc] = pd.to_numeric(team_exam_df[nc], errors="coerce")

        SAT_TYPES = {"SAT","Digital SAT","PSAT/NMSQT","Digital PSAT","Digital PSAT/NMSQT","PSAT","PSAT 8/9"}
        ACT_TYPES = {"ACT","Digital ACT"}
        team_exam_df["exam_family"] = team_exam_df["subject"].apply(
            lambda x: "SAT/PSAT" if x in SAT_TYPES else ("ACT" if x in ACT_TYPES else "Other"))
        team_exam_df["is_official"] = (
            team_exam_df["exam_code"].str.upper().str.contains("OFFICIAL", na=False) |
            (team_exam_df["attempt"].astype(str) != "n/a"))

        def sat_section_valid(row):
            return {
                "sat_math_valid": not pd.isna(row["sat_math"]) and row["sat_math"] >= 300,
                "sat_rw_valid":   not pd.isna(row["sat_rw"])   and row["sat_rw"]   >= 300,
                "sat_composite_valid": (
                    not pd.isna(row["sat_math"]) and row["sat_math"] >= 300 and
                    not pd.isna(row["sat_rw"])   and row["sat_rw"]   >= 300
                )
            }

        def act_section_valid(row):
            return {
                "act_english_valid": not pd.isna(row["act_english"]) and row["act_english"] >= 10,
                "act_math_valid":    not pd.isna(row["act_math"])    and row["act_math"]    >= 10,
                "act_reading_valid": not pd.isna(row["act_reading"]) and row["act_reading"] >= 10,
                "act_science_valid": pd.isna(row["act_science"])     or row["act_science"]  >= 10,
                "act_composite_valid": (
                    (pd.isna(row["act_english"]) or row["act_english"] >= 10) and
                    (pd.isna(row["act_math"])    or row["act_math"]    >= 10) and
                    (pd.isna(row["act_reading"]) or row["act_reading"] >= 10)
                )
            }

        sat_validity = team_exam_df[team_exam_df["exam_family"] == "SAT/PSAT"].apply(
            sat_section_valid, axis=1, result_type="expand")
        act_validity = team_exam_df[team_exam_df["exam_family"] == "ACT"].apply(
            act_section_valid, axis=1, result_type="expand")

        for col in ["sat_math_valid","sat_rw_valid","sat_composite_valid",
                    "act_english_valid","act_math_valid","act_reading_valid",
                    "act_science_valid","act_composite_valid"]:
            team_exam_df[col] = None
        if not sat_validity.empty:
            for col in sat_validity.columns:
                team_exam_df.loc[sat_validity.index, col] = sat_validity[col]
        if not act_validity.empty:
            for col in act_validity.columns:
                team_exam_df.loc[act_validity.index, col] = act_validity[col]

        team_exam_df["exam_valid_composite"] = team_exam_df.apply(
            lambda r: (pd.isna(r.get("attempt")) or str(r.get("attempt","")) in ("1","1.0","n/a","nan")) and (
                r["sat_composite_valid"] if r["exam_family"] == "SAT/PSAT"
                else (r["act_composite_valid"] if r["exam_family"] == "ACT" else False)), axis=1)

        def invalidity_reason(r):
            reasons = []
            if r["exam_family"] == "SAT/PSAT":
                if pd.notna(r["sat_math"]) and r["sat_math"] < 300: reasons.append("Math < 300")
                if pd.notna(r["sat_rw"])   and r["sat_rw"]   < 300: reasons.append("R&W < 300")
            elif r["exam_family"] == "ACT":
                if pd.notna(r["act_english"]) and r["act_english"] < 10: reasons.append("English < 10")
                if pd.notna(r["act_math"])    and r["act_math"]    < 10: reasons.append("Math < 10")
                if pd.notna(r["act_reading"]) and r["act_reading"] < 10: reasons.append("Reading < 10")
                if pd.notna(r["act_science"]) and r["act_science"] < 10: reasons.append("Science < 10")
            return ", ".join(reasons) if reasons else ""

        team_exam_df["invalidity_reason"] = team_exam_df.apply(invalidity_reason, axis=1)

        now_utc            = pd.Timestamp.now(tz="UTC")
        completed_exam_df  = team_exam_df[team_exam_df["exam_valid_composite"] == True].copy()
        latest_exam_per_st = (
            completed_exam_df.groupby(["tutor_id","student_id"])["exam_date"]
            .max().reset_index()
            .rename(columns={"exam_date":"latest_completed_exam_date"}))
        team_exam_df = team_exam_df.merge(latest_exam_per_st, on=["tutor_id","student_id"], how="left")
        team_exam_df["days_since_completed_exam"] = (
            now_utc - team_exam_df["latest_completed_exam_date"]).dt.days

        exam_snap_df = save_exams_weekly_snapshot(team_exam_df)

        def build_tutor_flag_summary(df):
            rows = []
            for tutor, tdf in df.groupby("tutor_name"):
                total_students = tdf["student_id"].nunique()
                eligible_mask  = tdf.groupby("student_id")["attended_test_prep_hours"].first() >= 6
                eligible_ids   = eligible_mask[eligible_mask].index.tolist()
                no_exam_ids    = []; stale_exam_ids = []
                for sid in eligible_ids:
                    sdf       = tdf[tdf["student_id"] == sid]
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
                    "eligible_students":   len(eligible_ids),
                    "no_exam_students":    len(no_exam_ids),
                    "stale_exam_students": len(stale_exam_ids),
                    "pct_with_exam": round(
                        (len(eligible_ids) - len(no_exam_ids)) / len(eligible_ids) * 100, 1)
                        if len(eligible_ids) > 0 else None,
                })
            return pd.DataFrame(rows)

        # ── Score improvement helper ──────────────────────────
        def compute_improvement(student_df, exam_family, mode="last", score_col="score"):
            fsd    = student_df["first_session_day"].iloc[0]
            fam_df = student_df[student_df["exam_family"] == exam_family].copy()
            fam_df = fam_df[fam_df["exam_valid_composite"] == True].copy()
            if fam_df.empty:
                return None, None, None, None, None
            fam_df      = fam_df.sort_values("exam_date")
            baseline_df = fam_df[fam_df["exam_date"] <= fsd]
            after_df    = fam_df[fam_df["exam_date"] >  fsd]
            if after_df.empty:
                return None, None, None, None, None
            # Baseline: last exam before/on first session, or if none, first exam after
            if not baseline_df.empty:
                baseline_row = baseline_df.sort_values("exam_date").iloc[-1]
            else:
                baseline_row = after_df.sort_values("exam_date").iloc[0]
                after_df     = after_df.iloc[1:]  # remove baseline from after
                if after_df.empty:
                    return None, None, None, None, None
            official_after  = after_df[after_df["is_official"] == True].dropna(subset=[score_col])
            after_df_valid  = after_df.dropna(subset=[score_col])
            if after_df_valid.empty:
                return None, None, None, None, None
            if not official_after.empty:
                endpoint_row = official_after.sort_values("exam_date").iloc[-1]                                if mode == "last" else                                official_after.loc[official_after[score_col].idxmax()]
            else:
                endpoint_row = after_df_valid.sort_values("exam_date").iloc[-1]                                if mode == "last" else                                after_df_valid.loc[after_df_valid[score_col].idxmax()]
            b_score     = baseline_row[score_col]
            e_score     = endpoint_row[score_col]
            improvement = (e_score - b_score) if pd.notna(b_score) and pd.notna(e_score) else None
            return b_score, e_score, improvement, baseline_row, endpoint_row

        def build_student_improvement(df, mode="last"):
            records = []
            for (tutor_id, tutor_name, student_id, student_name), sdf in df.groupby(
                    ["tutor_id","tutor_name","student_id","student_name"]):
                hours = sdf["attended_test_prep_hours"].iloc[0]
                fsd   = sdf["first_session_day"].iloc[0]
                mrs   = sdf["most_recent_session"].iloc[0]
                for fam in ["SAT/PSAT", "ACT"]:
                    b, e, imp, b_row, e_row = compute_improvement(sdf, fam, mode=mode)
                    fam_all    = sdf[sdf["exam_family"] == fam].copy().sort_values("exam_date")
                    before_all = fam_all[fam_all["exam_date"] <= fsd]
                    after_all  = fam_all[fam_all["exam_date"] >  fsd]
                    def section_imp(section_col, valid_col):
                        if before_all.empty or after_all.empty: return None, None, None
                        b_sec = before_all[before_all[valid_col] == True].dropna(subset=[section_col])
                        a_sec = after_all[after_all[valid_col] == True].dropna(subset=[section_col])
                        if b_sec.empty or a_sec.empty: return None, None, None
                        bv = b_sec.sort_values("exam_date").iloc[-1][section_col]
                        av = a_sec.loc[a_sec[section_col].idxmax()][section_col]                              if mode == "highest" else                              a_sec.sort_values("exam_date").iloc[-1][section_col]
                        return bv, av, (av - bv) if pd.notna(bv) and pd.notna(av) else None
                    if fam == "SAT/PSAT":
                        bm, em, imp_m = section_imp("sat_math", "sat_math_valid")
                        br, er, imp_r = section_imp("sat_rw",   "sat_rw_valid")
                        sec_imps = {
                            "sat_math_baseline": bm, "sat_math_endpoint": em, "sat_math_improvement": imp_m,
                            "sat_rw_baseline":   br, "sat_rw_endpoint":   er, "sat_rw_improvement":   imp_r,
                        }
                    else:
                        beng, eeng, imp_eng = section_imp("act_english", "act_english_valid")
                        bmat, emat, imp_mat = section_imp("act_math",    "act_math_valid")
                        bred, ered, imp_red = section_imp("act_reading", "act_reading_valid")
                        bsci, esci, imp_sci = section_imp("act_science", "act_science_valid")
                        sec_imps = {
                            "act_english_baseline": beng, "act_english_endpoint": eeng, "act_english_improvement": imp_eng,
                            "act_math_baseline":    bmat, "act_math_endpoint":    emat, "act_math_improvement":    imp_mat,
                            "act_reading_baseline": bred, "act_reading_endpoint": ered, "act_reading_improvement": imp_red,
                            "act_science_baseline": bsci, "act_science_endpoint": esci, "act_science_improvement": imp_sci,
                        }
                    if b is not None or e is not None:
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
                            "exam_family": fam, "hours_delivered": hours,
                            "completed_exams": fam_completed_after,
                            "hours_per_exam": hours_per_exam,
                            "first_session_day": fsd, "most_recent_session": mrs,
                            "baseline_score": b, "endpoint_score": e, "improvement": imp,
                            "baseline_date":  b_row["exam_date"] if b_row is not None else None,
                            "endpoint_date":  e_row["exam_date"] if e_row is not None else None,
                            "endpoint_is_official": e_row["is_official"] if e_row is not None else None,
                        }
                        rec.update(sec_imps)
                        records.append(rec)
            return pd.DataFrame(records)

        tutor_flag_summary = build_tutor_flag_summary(team_exam_df)
        medals_e = ["🥇","🥈","🥉","4️⃣","5️⃣"]

        st.markdown("### 🚨 Top Tutors to Address")
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
                        f"{medals_e[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"{int(row['no_exam_students'])} students</span>",
                        unsafe_allow_html=True)
        with fc2:
            st.markdown("**Most Students With Stale Exams >90 Days (Top 5)**")
            top_stale_e = (tutor_flag_summary[tutor_flag_summary["stale_exam_students"] > 0]
                           .sort_values("stale_exam_students", ascending=False).head(5))
            if top_stale_e.empty:
                st.success("✅ All students have a recent completed exam.")
            else:
                for rank, (_, row) in enumerate(top_stale_e.iterrows()):
                    st.markdown(
                        f"{medals_e[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#b35c00; font-weight:bold'>"
                        f"{int(row['stale_exam_students'])} students</span>",
                        unsafe_allow_html=True)
        with fc3:
            st.markdown("**Lowest % Eligible Students With a Completed Exam (Top 5)**")
            top_low_pct_e = (tutor_flag_summary[tutor_flag_summary["eligible_students"] > 0]
                             .sort_values("pct_with_exam", ascending=True).head(5))
            if top_low_pct_e.empty:
                st.success("✅ No data to display.")
            else:
                for rank, (_, row) in enumerate(top_low_pct_e.iterrows()):
                    val = f"{row['pct_with_exam']:.0f}%" if pd.notna(row["pct_with_exam"]) else "N/A"
                    st.markdown(
                        f"{medals_e[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#555; font-weight:bold'>{val}</span>",
                        unsafe_allow_html=True)

        st.divider()
        st.markdown("### 📊 Team Overview")
        total_tp_students  = team_exam_df["student_id"].nunique()
        total_eligible     = int(tutor_flag_summary["eligible_students"].sum())
        total_no_exam      = int(tutor_flag_summary["no_exam_students"].sum())
        total_stale_e      = int(tutor_flag_summary["stale_exam_students"].sum())
        pct_with_exam_team = round(
            (total_eligible - total_no_exam) / total_eligible * 100, 1) if total_eligible > 0 else 0
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Test Prep Students", total_tp_students)
        m2.metric("Eligible (6+ hrs)",        total_eligible)
        m3.metric("No Completed Exam",        total_no_exam, delta_color="inverse")
        m4.metric("Stale Exam (>90 days)",    total_stale_e, delta_color="inverse")
        m5.metric("% Eligible w/ Exam",       f"{pct_with_exam_team:.1f}%")
        # ── Score improvement & hours-per-exam team summary ──
        st.markdown("#### 📈 Score Improvement & Efficiency Summary")
        ov_mode     = st.radio("Improvement mode", ["First → Last", "First → Highest"],
                               horizontal=True, key="overview_imp_mode")
        ov_mode_key = "last" if ov_mode == "First → Last" else "highest"
        ov_imp_df   = build_student_improvement(team_exam_df, mode=ov_mode_key)

        if not ov_imp_df.empty:
            ov_col1, ov_col2 = st.columns(2)
            for fam, col in [("SAT/PSAT", ov_col1), ("ACT", ov_col2)]:
                fam_ov = ov_imp_df[
                    (ov_imp_df["exam_family"] == fam) & ov_imp_df["improvement"].notna()]
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
                        ci1.metric("Avg Improvement",  f"{avg_imp:+.0f} pts")
                        ci2.metric("% Improved",        f"{pct_improved:.0f}%")
                        ci3.metric("Avg Hrs / Exam",    f"{avg_hpe:.1f}" if pd.notna(avg_hpe) else "N/A")
                        ci4.metric("Students w/ Data",  n_students)
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
                                orientation="h", color="avg_improvement",
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
                                showlegend=False,
                                margin=dict(l=160, r=60, t=50, b=30))
                            fig_ov.add_vline(x=0, line_dash="dash", line_color="grey")
                            fig_ov.update_traces(textposition="outside")
                            st.plotly_chart(fig_ov, use_container_width=True)


        st.divider()
        st.markdown("### 🔍 Filters")
        ef1, ef2, ef3 = st.columns(3)
        with ef1:
            tutor_opts_e = ["All Tutors"] + sorted(team_exam_df["tutor_name"].dropna().unique().tolist())
            sel_tutor_e  = st.selectbox("Tutor", tutor_opts_e, key="exam_tutor")
        with ef2:
            exam_fam_opts = ["All Exam Types","SAT/PSAT","ACT"]
            sel_exam_fam  = st.selectbox("Exam Type", exam_fam_opts, key="exam_fam")
        with ef3:
            exam_status_opts = ["All Students","Eligible (6+ hrs) Only",
                                "No Completed Exam","Stale Exam (>90 days)"]
            sel_exam_status  = st.selectbox("Student Status", exam_status_opts, key="exam_status")

        view_exam_df = team_exam_df.copy()
        if sel_tutor_e != "All Tutors":
            view_exam_df = view_exam_df[view_exam_df["tutor_name"] == sel_tutor_e]
        if sel_exam_fam != "All Exam Types":
            view_exam_df = view_exam_df[view_exam_df["exam_family"] == sel_exam_fam]
        if sel_exam_status == "Eligible (6+ hrs) Only":
            elig_ids     = view_exam_df.groupby("student_id")["attended_test_prep_hours"].first()
            view_exam_df = view_exam_df[view_exam_df["student_id"].isin(elig_ids[elig_ids >= 6].index)]
        elif sel_exam_status == "No Completed Exam":
            no_exam_ids  = [sid for sid, sdf in view_exam_df.groupby("student_id")
                            if sdf["attended_test_prep_hours"].iloc[0] >= 6
                            and sdf[sdf["exam_valid_composite"] == True].empty]
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

        tab_ov_e, tab_imp_e, tab_det_e, tab_tr_e = st.tabs([
            "📊 Team / Tutor Overview", "📈 Score Improvement",
            "📋 Exam Detail", "📅 Trends Over Time"])

        with tab_ov_e:
            if not single_tutor_exam:
                view_flag    = build_tutor_flag_summary(view_exam_df)
                no_exam_chart = view_flag[view_flag["no_exam_students"] > 0].sort_values("no_exam_students", ascending=True)
                if not no_exam_chart.empty:
                    n    = len(no_exam_chart)
                    fig1 = px.bar(no_exam_chart, x="no_exam_students", y="tutor_name",
                                  orientation="h", color="no_exam_students",
                                  color_continuous_scale=["#ffe0e0","#cc0000"],
                                  text="no_exam_students",
                                  title="Eligible Students With No Completed Exam — By Tutor",
                                  height=max(350, n * 30))
                    fig1.update_layout(title=dict(x=0.5, xanchor="center"), showlegend=False,
                                       coloraxis_showscale=False, xaxis_title="# Students",
                                       yaxis_title="", yaxis=dict(autorange="reversed"),
                                       margin=dict(l=160, r=20, t=50, b=40))
                    fig1.update_traces(textposition="outside")
                    st.plotly_chart(fig1, use_container_width=True)
                else:
                    st.success("✅ All eligible students have at least one completed exam.")
            else:
                sel_flag = tutor_flag_summary[tutor_flag_summary["tutor_name"] == sel_tutor_e]
                if not sel_flag.empty:
                    row = sel_flag.iloc[0]
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    sc1.metric("Total Students",    int(row["total_students"]))
                    sc2.metric("Eligible (6+ hrs)", int(row["eligible_students"]))
                    sc3.metric("No Completed Exam", int(row["no_exam_students"]), delta_color="inverse")
                    sc4.metric("Stale (>90 days)",  int(row["stale_exam_students"]), delta_color="inverse")
                per_student_last = (
                    view_exam_df[view_exam_df["exam_valid_composite"] == True]
                    .groupby(["student_id","student_name"])["exam_date"].max().reset_index())
                per_student_last["days_since"] = (
                    now_utc - per_student_last["exam_date"]).dt.days.astype("Int64")
                per_student_last = per_student_last.sort_values("days_since", ascending=True)
                if not per_student_last.empty:
                    fig_days = px.bar(
                        per_student_last, x="days_since", y="student_name", orientation="h",
                        text=per_student_last["days_since"].apply(lambda d: f"{d}d" if pd.notna(d) else ""),
                        title=f"{sel_tutor_e} — Days Since Last Completed Exam (per student)",
                        height=max(300, len(per_student_last) * 30),
                        color="days_since",
                        color_continuous_scale=["#2a7a2a","#ffaa00","#cc0000"],
                        range_color=[0, max(int(per_student_last["days_since"].max(skipna=True)), 91)])
                    fig_days.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Days Since Last Completed Exam",
                        yaxis_title="", showlegend=False, coloraxis_showscale=False,
                        margin=dict(l=160, r=20, t=50, b=40))
                    fig_days.add_vline(x=90, line_dash="dash", line_color="red",
                                       annotation_text="90-day threshold",
                                       annotation_position="top right")
                    fig_days.update_traces(textposition="outside")
                    st.plotly_chart(fig_days, use_container_width=True)

        with tab_imp_e:
            st.markdown("#### Score Improvement Settings")
            imp_col1, imp_col2 = st.columns(2)
            with imp_col1:
                improvement_mode = st.radio("Improvement mode",
                                            ["First → Last", "First → Highest"],
                                            horizontal=True, key="imp_mode")
            with imp_col2:
                imp_fam = st.radio("Exam type", ["SAT/PSAT", "ACT"],
                                   horizontal=True, key="imp_fam")
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
                    comp_df = fam_imp_df.dropna(subset=["improvement"]).sort_values("improvement", ascending=True)
                    if not comp_df.empty:
                        color_vals = comp_df["improvement"].tolist()
                        max_abs    = max(abs(min(color_vals)), abs(max(color_vals)), 1)
                        fig_imp = px.bar(
                            comp_df, x="improvement", y="student_name",
                            orientation="h", color="improvement",
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
                            showlegend=False,
                            margin=dict(l=180, r=60, t=50, b=40))
                        fig_imp.add_vline(x=0, line_dash="dash", line_color="grey")
                        fig_imp.update_traces(textposition="outside")
                        st.plotly_chart(fig_imp, use_container_width=True)
                        avg_imp = comp_df["improvement"].mean()
                        pct_pos = (comp_df["improvement"] > 0).mean() * 100
                        avg_hpe = comp_df["hours_per_exam"].dropna().mean()
                        sm1, sm2, sm3, sm4 = st.columns(4)
                        sm1.metric("Avg Composite Improvement", f"{avg_imp:+.0f} pts")
                        sm2.metric("% Students Improved",       f"{pct_pos:.0f}%")
                        sm3.metric("Avg Hours / Exam",          f"{avg_hpe:.1f}" if pd.notna(avg_hpe) else "N/A")
                        sm4.metric("Students w/ Data",          len(comp_df))
                    st.divider()
                    st.markdown("#### Section-Level Improvement")
                    if imp_fam == "SAT/PSAT":
                        section_map = {
                            "Math":              ("sat_math_baseline","sat_math_endpoint","sat_math_improvement"),
                            "Reading & Writing": ("sat_rw_baseline",  "sat_rw_endpoint",  "sat_rw_improvement"),
                        }
                    else:
                        section_map = {
                            "English": ("act_english_baseline","act_english_endpoint","act_english_improvement"),
                            "Math":    ("act_math_baseline",   "act_math_endpoint",   "act_math_improvement"),
                            "Reading": ("act_reading_baseline","act_reading_endpoint","act_reading_improvement"),
                            "Science": ("act_science_baseline","act_science_endpoint","act_science_improvement"),
                        }
                    for sec_name, (b_col, e_col, imp_col) in section_map.items():
                        if imp_col not in fam_imp_df.columns: continue
                        sec_df = fam_imp_df.dropna(subset=[imp_col]).sort_values(imp_col, ascending=True)
                        if sec_df.empty: continue
                        max_abs_s = max(abs(sec_df[imp_col].min()), abs(sec_df[imp_col].max()), 1)
                        fig_sec = px.bar(
                            sec_df, x=imp_col, y="student_name",
                            orientation="h", color=imp_col,
                            color_continuous_scale=["#cc0000","#ffffff","#006400"],
                            range_color=[-max_abs_s, max_abs_s],
                            text=sec_df[imp_col].apply(lambda v: f"{v:+.0f}"),
                            title=f"{imp_fam} — {sec_name} Section ({improvement_mode})",
                            height=max(280, len(sec_df) * 28))
                        fig_sec.update_layout(
                            title=dict(x=0.5, xanchor="center"),
                            xaxis_title="Score Change", yaxis_title="",
                            showlegend=False,
                            margin=dict(l=180, r=60, t=50, b=30))
                        fig_sec.add_vline(x=0, line_dash="dash", line_color="grey")
                        fig_sec.update_traces(textposition="outside")
                        st.plotly_chart(fig_sec, use_container_width=True)

        with tab_det_e:
            if view_exam_df.empty:
                st.info("No records match the current filters.")
            else:
                detail_e = view_exam_df.copy()

                # Join goal/starting scores from study_areas
                try:
                    _sa_df = load_study_areas()
                    if not _sa_df.empty and "student_id" in detail_e.columns:
                        # For each student, get the most relevant study area
                        # (SAT subject_id=51, Digital SAT=315, ACT=43, PSAT=316)
                        TEST_SUBJECT_IDS = {43, 51, 315, 316, 342, 195, 50, 356}
                        _sa_relevant = _sa_df[_sa_df["subject_id"].isin(TEST_SUBJECT_IDS)].copy()
                        # Take one row per student (prefer non-null goal_score)
                        # Match goal/starting score by exam family
                        _sa_relevant["exam_family"] = _sa_relevant["exam_family"] if "exam_family" in _sa_relevant.columns else "Other"
                        _sa_best = (_sa_relevant
                            .sort_values("goal_score", ascending=False, na_position="last")
                            .groupby(["student_id","exam_family"]).first().reset_index()
                            [["student_id","exam_family","goal_score","starting_score"]])
                        # detail_e already has exam_family column
                        detail_e = detail_e.merge(_sa_best, on=["student_id","exam_family"], how="left")
                        # Flag if student has scored at or above goal — only when exam families match
                        def _goal_met(row):
                            if pd.isna(row.get("goal_score")) or pd.isna(row.get("score")):
                                return None
                            return "✅ At/Above Goal" if float(row["score"]) >= float(row["goal_score"]) else "❌ Below Goal"
                        detail_e["goal_status"] = detail_e.apply(_goal_met, axis=1)
                    else:
                        detail_e["goal_score"] = None
                        detail_e["starting_score"] = None
                        detail_e["goal_status"] = None
                except Exception:
                    detail_e["goal_score"] = None
                    detail_e["starting_score"] = None
                    detail_e["goal_status"] = None
                for dc in ["first_session_day","most_recent_session","exam_date"]:
                    if dc in detail_e.columns:
                        detail_e[dc] = detail_e[dc].dt.strftime("%Y-%m-%d")

                def exam_status_label(r):
                    if pd.isna(r.get("exam_id")) or r.get("exam_id") is None:
                        return "No Exam Data"
                    if r.get("exam_valid_composite") == True:
                        suffix = " (Official)" if r.get("is_official") else ""
                        return f"✅ Valid{suffix}"
                    reason = r.get("invalidity_reason","")
                    return f"⚠️ Invalid — {reason}" if reason else "⚠️ Invalid"

                detail_e["exam_status"] = detail_e.apply(exam_status_label, axis=1)
                display_cols_e = [
                    "tutor_name","student_name","grade_lvl","attended_test_prep_hours",
                    "attended_velocity","hours_remaining",
                    "first_session_day","most_recent_session","exam_date",
                    "subject","exam_code","exam_status","score",
                    "starting_score","goal_score","goal_status",
                    "act_english","act_math","act_reading","act_science",
                    "sat_math","sat_rw","invalidity_reason"
                ]
                display_cols_e = [c for c in display_cols_e if c in detail_e.columns]
                detail_display_e = detail_e[display_cols_e].rename(columns={
                    "tutor_name":               "Tutor",
                    "student_name":             "Student",
                    "grade_lvl":                "Grade",
                    "attended_test_prep_hours": "Hours Delivered",
                    "attended_velocity":        "Hrs/Week",
                    "hours_remaining":          "Hours Remaining",
                    "first_session_day":        "First Session",
                    "most_recent_session":      "Most Recent Session",
                    "exam_date":                "Exam Date",
                    "subject":                  "Exam Type",
                    "exam_code":                "Exam Code",
                    "exam_status":              "Status",
                    "score":                    "Composite Score",
                    "act_english":              "ACT English",
                    "act_math":                 "ACT Math",
                    "act_reading":              "ACT Reading",
                    "act_science":              "ACT Science",
                    "sat_math":                 "SAT Math",
                    "sat_rw":                   "SAT R&W",
                    "starting_score":           "Starting Score",
                    "goal_score":               "Goal Score",
                    "goal_status":              "Goal Status",
                    "invalidity_reason":        "Why Invalid",
                }).drop_duplicates().sort_values(["Tutor","Student","Exam Date"])

                def highlight_exam_row(row):
                    status = str(row.get("Status",""))
                    if "Invalid" in status: return ["background-color: #fff3cc"] * len(row)
                    if "No Exam" in status: return ["background-color: #ffe5e5"] * len(row)
                    return [""] * len(row)

                st.markdown(
                    "✅ Green/white = valid exam &nbsp;&nbsp; "
                    "🟡 Yellow = invalid &nbsp;&nbsp; 🔴 Red = no exam data",
                    unsafe_allow_html=True)
                st.dataframe(
                    detail_display_e.style.apply(highlight_exam_row, axis=1),
                    use_container_width=True, hide_index=True)
                out_e = io.BytesIO()
                detail_display_e.to_excel(out_e, index=False)
                out_e.seek(0)
                st.download_button(
                    label="⬇️ Download Exam Detail",
                    data=out_e,
                    file_name=f"Exam_Detail_{sel_tutor_e.replace(' ','_') if sel_tutor_e != 'All Tutors' else 'Katherine_Marino'}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with tab_tr_e:
            esnap = load_exams_snapshots()
            tutors_to_show_e = [sel_tutor_e] if single_tutor_exam else \
                                sorted(team_exam_df["tutor_name"].dropna().unique().tolist())
            trend_metric_e = st.selectbox(
                "Trend metric",
                ["students_no_exam","students_stale_exam","pct_eligible_with_exam"],
                format_func=lambda x: {
                    "students_no_exam":      "Students With No Completed Exam",
                    "students_stale_exam":   "Students With Stale Exam (>90d)",
                    "pct_eligible_with_exam":"% Eligible Students With a Completed Exam",
                }[x], key="exam_trend_metric")
            if esnap.empty:
                st.caption("No historical snapshot data yet — trends will build automatically each week.")
            else:
                trend_color_e = {
                    "students_no_exam":      "#cc0000",
                    "students_stale_exam":   "#b35c00",
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
                                     color_discrete_sequence=[trend_color_e])
                    fig_te.update_layout(
                        title=dict(x=0.5, xanchor="center"),
                        xaxis_title="Week", yaxis_title="",
                        height=300, margin=dict(l=20, r=20, t=50, b=40))
                    fig_te.update_traces(line=dict(width=2.5))
                    st.plotly_chart(fig_te, use_container_width=True)

        if st.sidebar.button("🔄 Refresh Exam Data", key="refresh_exams"):
            st.cache_data.clear(); st.rerun()


    # ─────────────────────────────────────────────

    # ─────────────────────────────────────────────
    # PAGE: UPDATE QUALITY SCORES
    # ─────────────────────────────────────────────

    if page == "📝 Progress Update Quality Scores":
        st.markdown('<div class="main-title">📝 Progress Update Quality Scores</div>', unsafe_allow_html=True)

        with st.expander("ℹ️ About this data"):
            st.markdown("""
**What is this page?**
Each progress update sent by a tutor is automatically scored across 4 dimensions. Scores are updated weekly.

**Scoring rubric (max total: 10):**
- **What Worked On (0–2):** 0 = no specifics or broad subject only; 1 = one specific topic or concept named; 2 = two or more specific topics, or one topic plus measurable score improvement
- **Goals (0–2):** 0 = no goal or vague; 1 = goal restated (e.g. "the goal is a 1300"); 2 = explicit progress shown from X to Y, or gap quantified
- **Velocity (0–3):** 0 = no recommendation; 1 = vague or frequency only or duration only; 2 = specific frequency AND duration both stated; 3 = score 2 plus explicit package size recommended
- **Plan Forward (0–3):** 0 = no plan; 1 = vague or single topic; 2 = outlined plan connecting content to goal; 3 = score 2 plus explicit hours/timeline tied to goal

**Tutors with no updates sent that week will appear with blank scores.**
            """)

        try:
            raw_scores_df, scores_fetched_at = load_progress_scores()
        except Exception as e:
            st.error(f"Could not load progress update scores: {e}")
            st.stop()

        if raw_scores_df.empty:
            st.info("No scored data available yet — run analyze_progress_updates.py first.")
            st.stop()

        st.caption(f"🕐 Data last scored: **{scores_fetched_at}**")
        st.sidebar.markdown(f"🕐 **Scores last updated**  \n{scores_fetched_at}")

        # Use full history if available, fall back to current week
        _history_df = load_progress_history()
        if not _history_df.empty and "tutor" in _history_df.columns:
            raw_scores_df = _history_df

        team_scores_df = raw_scores_df[raw_scores_df["tutor"].isin(annelies_tutors)].copy()

        if team_scores_df.empty:
            st.warning(f"No scored progress updates found for Team Marino.")
            st.stop()

        # Week selector — Sunday of each week in data
        team_scores_df["week_sunday"] = team_scores_df["sent_at"].dt.to_period("W-SAT").apply(
            lambda p: p.start_time.date()
        )
        available_weeks = sorted(team_scores_df["week_sunday"].dropna().unique(), reverse=True)
        week_labels     = {w: f"Week of {w.strftime('%b %d, %Y')}" for w in available_weeks}
        selected_week   = st.selectbox(
            "Select Week Of (Sunday):",
            options=available_weeks,
            format_func=lambda w: week_labels[w],
            key="qs_week_select"
        )
        week_start  = pd.Timestamp(selected_week)
        week_end    = week_start + pd.Timedelta(days=7)
        filtered_df = team_scores_df[
            (team_scores_df["sent_at"] >= week_start) &
            (team_scores_df["sent_at"] <  week_end)
        ].copy()

        if filtered_df.empty:
            st.info("No updates found in this date range.")
            st.stop()

        st.divider()

        st.markdown("### 📊 Team Overview")
        st.caption(f"{len(filtered_df)} updates scored · Max total score is 10")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Avg Total",          f"{filtered_df['total'].mean():.1f} / 10")
        m2.metric("Avg What Worked On", f"{filtered_df['what_worked_on'].mean():.1f} / 2")
        m3.metric("Avg Goals",          f"{filtered_df['goals'].mean():.1f} / 2")
        m4.metric("Avg Velocity",       f"{filtered_df['velocity'].mean():.1f} / 3")
        m5.metric("Avg Plan Forward",   f"{filtered_df['plan_forward'].mean():.1f} / 3")

        m6, m7, m8, m9 = st.columns(4)
        m6.metric("% No What Worked On", f"{(filtered_df['what_worked_on']==0).mean()*100:.0f}%",
                  delta_color="inverse" if (filtered_df["what_worked_on"]==0).mean() > 0.1 else "off")
        m7.metric("% No Goal",           f"{(filtered_df['goals']==0).mean()*100:.0f}%",
                  delta_color="inverse" if (filtered_df["goals"]==0).mean() > 0.1 else "off")
        m8.metric("% No Velocity",       f"{(filtered_df['velocity']==0).mean()*100:.0f}%",
                  delta_color="inverse" if (filtered_df["velocity"]==0).mean() > 0.1 else "off")
        m9.metric("% No Plan Forward",   f"{(filtered_df['plan_forward']==0).mean()*100:.0f}%",
                  delta_color="inverse" if (filtered_df["plan_forward"]==0).mean() > 0.1 else "off")

        st.divider()

        # Build agg — include ALL team tutors even if no updates sent
        if not filtered_df.empty:
            _agg = (
                filtered_df.groupby("tutor")
                .agg(
                    updates        = ("update_id",      "count"),
                    avg_total      = ("total",          "mean"),
                    avg_worked_on  = ("what_worked_on", "mean"),
                    avg_goals      = ("goals",           "mean"),
                    avg_velocity   = ("velocity",        "mean"),
                    avg_plan       = ("plan_forward",    "mean"),
                    pct_zero_goals = ("goals",    lambda x: (x==0).mean()*100),
                    pct_zero_vel   = ("velocity", lambda x: (x==0).mean()*100),
                )
                .reset_index()
            )
        else:
            _agg = pd.DataFrame(columns=["tutor","updates","avg_total","avg_worked_on",
                                          "avg_goals","avg_velocity","avg_plan",
                                          "pct_zero_goals","pct_zero_vel"])
        # Add tutors with no updates
        missing_tutors = [t for t in annelies_tutors if t not in _agg["tutor"].values]
        if missing_tutors:
            _missing = pd.DataFrame([{
                "tutor": t, "updates": 0,
                "avg_total": None, "avg_worked_on": None, "avg_goals": None,
                "avg_velocity": None, "avg_plan": None,
                "pct_zero_goals": None, "pct_zero_vel": None,
            } for t in missing_tutors])
            _agg = pd.concat([_agg, _missing], ignore_index=True)
        tutor_agg = _agg.sort_values("tutor").reset_index(drop=True)

        # Save weekly snapshot
        snap_week_date = filtered_df["sent_at"].max().strftime("%Y-%m-%d") if not filtered_df.empty else None
        save_progress_weekly_snapshot(tutor_agg, week_date=snap_week_date)

        st.markdown("### 🚨 Tutors to Address")
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣"]

        # Compute raw zero counts per tutor
        if not filtered_df.empty:
            zero_counts = (
                filtered_df.groupby("tutor")
                .agg(
                    updates           = ("update_id",      "count"),
                    zero_worked_on    = ("what_worked_on", lambda x: (x==0).sum()),
                    zero_goals        = ("goals",          lambda x: (x==0).sum()),
                    zero_velocity     = ("velocity",       lambda x: (x==0).sum()),
                    zero_plan         = ("plan_forward",   lambda x: (x==0).sum()),
                )
                .reset_index()
            )
        else:
            zero_counts = pd.DataFrame(columns=["tutor","updates","zero_worked_on",
                                                 "zero_goals","zero_velocity","zero_plan"])

        fc1, fc2, fc3, fc4 = st.columns(4)

        with fc1:
            st.markdown("**Lowest Avg Total Score**")
            low_total = tutor_agg.dropna(subset=["avg_total"])
            low_total = low_total[low_total["updates"] >= 2].sort_values("avg_total")
            if low_total.empty:
                st.success("✅ Not enough data per tutor yet.")
            else:
                for rank, (_, row) in enumerate(low_total.head(5).iterrows()):
                    st.markdown(
                        f"{medals[min(rank,4)]} **{row['tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{row['avg_total']:.1f} / 10</span> "
                        f"({int(row['updates'])} updates)",
                        unsafe_allow_html=True)

        with fc2:
            st.markdown("**Most Missing Goals**")
            no_goals = zero_counts[zero_counts["zero_goals"] > 0].sort_values("zero_goals", ascending=False)
            if no_goals.empty:
                st.success("✅ All tutors included a goal in every update.")
            else:
                for rank, (_, row) in enumerate(no_goals.head(5).iterrows()):
                    st.markdown(
                        f"{medals[min(rank,4)]} **{row['tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['zero_goals'])} of {int(row['updates'])} missing</span>",
                        unsafe_allow_html=True)

        with fc3:
            st.markdown("**Most Missing Velocity**")
            no_vel = zero_counts[zero_counts["zero_velocity"] > 0].sort_values("zero_velocity", ascending=False)
            if no_vel.empty:
                st.success("✅ All tutors included a velocity recommendation.")
            else:
                for rank, (_, row) in enumerate(no_vel.head(5).iterrows()):
                    st.markdown(
                        f"{medals[min(rank,4)]} **{row['tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['zero_velocity'])} of {int(row['updates'])} missing</span>",
                        unsafe_allow_html=True)

        with fc4:
            st.markdown("**Most Missing Plan Forward**")
            no_plan = zero_counts[zero_counts["zero_plan"] > 0].sort_values("zero_plan", ascending=False)
            if no_plan.empty:
                st.success("✅ All tutors included a plan forward.")
            else:
                for rank, (_, row) in enumerate(no_plan.head(5).iterrows()):
                    st.markdown(
                        f"{medals[min(rank,4)]} **{row['tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['zero_plan'])} of {int(row['updates'])} missing</span>",
                        unsafe_allow_html=True)

        st.divider()

        st.markdown("### 📈 Avg Total Score — By Tutor")
        chart_df = tutor_agg.dropna(subset=["avg_total"]).copy()
        chart_df["avg_total"] = pd.to_numeric(chart_df["avg_total"], errors="coerce")
        chart_df = chart_df.dropna(subset=["avg_total"]).sort_values("avg_total", ascending=True)
        n_qs = len(chart_df)
        fig_bar = px.bar(
            chart_df, x="avg_total", y="tutor", orientation="h",
            color="avg_total",
            color_continuous_scale=["#cc0000","#ffdd99","#006400"],
            text=chart_df["avg_total"].apply(lambda v: f"{v:.1f}" if v is not None and v == v else "—"),
            title="Avg Total Score — By Tutor",
            height=max(350, n_qs * 30),
        )
        fig_bar.add_vline(x=7, line_dash="dash", line_color="#cc0000",
                          annotation_text="7.0 target", annotation_position="top right")
        fig_bar.update_layout(
            title=dict(x=0.5, xanchor="center"),
            showlegend=False, coloraxis_showscale=False,
            xaxis=dict(range=[0, 11], title="Avg Total Score"),
            yaxis=dict(autorange="reversed"),
            yaxis_title="", margin=dict(l=160, r=20, t=50, b=40)
        )
        fig_bar.update_traces(textposition="outside")
        st.plotly_chart(fig_bar, use_container_width=True)

        st.divider()

        st.markdown("### 📋 Tutor Summary Table")
        display_agg = tutor_agg.copy()
        for col in ["avg_total","avg_worked_on","avg_goals","avg_velocity","avg_plan"]:
            display_agg[col] = pd.to_numeric(display_agg[col], errors="coerce").round(1)
        display_agg["pct_zero_goals"] = display_agg["pct_zero_goals"].apply(
            lambda v: f"{int(round(v))}%" if v is not None and v == v else "—")
        display_agg["pct_zero_vel"] = display_agg["pct_zero_vel"].apply(
            lambda v: f"{int(round(v))}%" if v is not None and v == v else "—")
        display_agg = display_agg.sort_values("tutor", ascending=True).rename(columns={
            "tutor":          "Tutor",
            "updates":        "# Updates",
            "avg_total":      "Avg Total (/10)",
            "avg_worked_on":  "What Worked On (/2)",
            "avg_goals":      "Goals (/2)",
            "avg_velocity":   "Velocity (/3)",
            "avg_plan":       "Plan Forward (/3)",
            "pct_zero_goals": "% No Goal",
            "pct_zero_vel":   "% No Velocity",
        })

        def highlight_score_row(row):
            score = row.get("Avg Total (/10)", 10)
            if score < 5:  return ["background-color: #ffe5e5"] * len(row)
            if score < 7:  return ["background-color: #fff3cc"] * len(row)
            return [""] * len(row)

        st.dataframe(display_agg.style.apply(highlight_score_row, axis=1),
                     use_container_width=True, hide_index=True)

        st.divider()

        st.markdown("### 📅 Trends Over Time")
        progress_snap = load_progress_snapshots()
        if progress_snap.empty:
            st.caption("No historical data yet — trends will build automatically each week.")
        else:
            trend_metric_p = st.selectbox(
                "Trend metric",
                ["avg_total","avg_worked_on","avg_goals","avg_velocity","avg_plan"],
                format_func=lambda x: {
                    "avg_total":     "Avg Total Score",
                    "avg_worked_on": "Avg What Worked On",
                    "avg_goals":     "Avg Goals",
                    "avg_velocity":  "Avg Velocity",
                    "avg_plan":      "Avg Plan Forward",
                }[x], key="progress_trend_metric"
            )
            trend_tutor_p = st.selectbox(
                "Filter by tutor (optional)",
                ["All Tutors"] + sorted(progress_snap["tutor"].dropna().unique().tolist()),
                key="progress_trend_tutor"
            )
            tutors_to_plot_p = [trend_tutor_p] if trend_tutor_p != "All Tutors" else                                 sorted(progress_snap["tutor"].dropna().unique().tolist())
            for tutor in tutors_to_plot_p:
                tsnap_p = progress_snap[progress_snap["tutor"] == tutor].sort_values("week_date")
                if len(tsnap_p) < 2:
                    if trend_tutor_p != "All Tutors":
                        st.caption(f"Only one week of data for {tutor} — trend will appear as more weeks accumulate.")
                    continue
                fig_pt = px.line(tsnap_p, x="week_date", y=trend_metric_p,
                                 markers=True,
                                 title=f"{tutor} — {trend_metric_p.replace('_',' ').title()} Week over Week",
                                 color_discrete_sequence=["#004466"])
                if trend_metric_p == "avg_total":
                    fig_pt.add_hline(y=7, line_dash="dash", line_color="#cc0000",
                                     annotation_text="7.0 target")
                fig_pt.update_layout(
                    title=dict(x=0.5, xanchor="center"),
                    xaxis_title="Week", yaxis_title="", height=300,
                    margin=dict(l=20, r=20, t=50, b=40),
                    plot_bgcolor="white", paper_bgcolor="white",
                )
                fig_pt.update_traces(line=dict(width=2.5))
                st.plotly_chart(fig_pt, use_container_width=True)

        st.divider()

        st.markdown("### 🔍 Row-Level Detail")
        tutor_opts = ["All Tutors"] + sorted(annelies_tutors)
        sel_tutor  = st.selectbox("Filter by Tutor", tutor_opts, key="qs_tutor_filter")
        min_score  = st.slider("Minimum total score", 0, 10, 0, key="qs_score_filter")

        detail_df = filtered_df.copy()
        if sel_tutor != "All Tutors":
            detail_df = detail_df[detail_df["tutor"] == sel_tutor]
        detail_df = detail_df[detail_df["total"] >= min_score].sort_values("sent_at", ascending=False)
        st.caption(f"Showing {len(detail_df)} updates")

        available_cols = [c for c in ["sent_at","tutor","student_name",
            "what_worked_on","goals","velocity","plan_forward","total","notes","body"]
            if c in detail_df.columns]
        detail_display = detail_df[available_cols].copy()
        detail_display["sent_at"] = detail_display["sent_at"].dt.strftime("%Y-%m-%d")
        detail_display = detail_display.sort_values(["tutor","sent_at"]).rename(columns={
            "sent_at":        "Date",
            "tutor":          "Tutor",
            "student_name":   "Student",
            "what_worked_on": "What Worked On",
            "goals":          "Goals",
            "velocity":       "Velocity",
            "plan_forward":   "Plan Forward",
            "total":          "Total",
            "notes":          "AI Notes",
            "body":           "Progress Update",
        })

        def highlight_detail_row(row):
            score = row.get("Total", 10)
            if score < 5:  return ["background-color: #ffe5e5"] * len(row)
            if score < 7:  return ["background-color: #fff3cc"] * len(row)
            return [""] * len(row)

        col_config = {
            "What Worked On":  st.column_config.NumberColumn("Worked On",       help="0-2",  format="%d"),
            "Goals":           st.column_config.NumberColumn("Goals",           help="0-2",  format="%d"),
            "Velocity":        st.column_config.NumberColumn("Velocity",        help="0-3",  format="%d"),
            "Plan Forward":    st.column_config.NumberColumn("Plan",            help="0-3",  format="%d"),
            "Total":           st.column_config.NumberColumn("Total",           help="0-10", format="%d"),
            "AI Notes":        st.column_config.TextColumn("AI Notes",          width="large"),
            "Progress Update": st.column_config.TextColumn("Progress Update",   width="large"),
        }

        st.dataframe(
            detail_display.style.apply(highlight_detail_row, axis=1),
            use_container_width=True, hide_index=True,
            column_config=col_config,
        )

        out_qs = io.BytesIO()
        detail_display.to_excel(out_qs, index=False)
        out_qs.seek(0)
        st.download_button(
            label="⬇️ Download Detail",
            data=out_qs,
            file_name=f"Update_Quality_Scores_{sel_tutor.replace(' ','_') if sel_tutor != 'All Tutors' else 'Katherine_Marino'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

        if st.sidebar.button("🔄 Refresh Score Data", key="refresh_qs"):
            st.cache_data.clear(); st.rerun()


        # PAGE: ARCHIVABLE STUDENTS & UNSCHEDULED HOURS
    # ─────────────────────────────────────────────

    if page == "Archivable Students & Unscheduled Hours":
        st.markdown('<div class="main-title">Archivable Students & Unscheduled Hours 📦</div>', unsafe_allow_html=True)

        st.info(
            "ℹ️ **What is an Archivable Student?**\n\n"
            "An archivable student is a student currently appearing on a tutor's active dashboard "
            "who has **not had a session in the past 30 days** and has **no sessions scheduled in the future**.",
            icon=None
        )

        with st.spinner("Loading live data from Redshift..."):
            try:
                raw_df, fetched_at = load_archivable_unscheduled()
            except Exception as e:
                st.error(f"Could not connect to Redshift: {e}"); st.stop()

        if raw_df.empty:
            st.info("No data returned from the database."); st.stop()

        st.caption(f"🕐 Data last updated: **{fetched_at}**")
        st.sidebar.markdown("---")
        st.sidebar.markdown(f"🕐 **Data last updated**  \n{fetched_at}")

        raw_df["should_archive"] = raw_df["should_archive"].apply(
            lambda x: bool(x) if pd.notna(x) else False)
        full_team_df = raw_df[raw_df["team_name"] == "Team Marino"].copy()
        if full_team_df.empty:
            st.warning("No records found for Team Marino."); st.stop()

        snapshots_df = save_weekly_snapshot(full_team_df)

        def horizontal_bar(df, x_col, y_col, color_scale, title, x_label, height=None):
            n   = len(df); h = height or max(350, n * 28)
            fig = px.bar(df, x=x_col, y=y_col, orientation="h", color=x_col,
                         color_continuous_scale=color_scale,
                         text=df[x_col].apply(lambda v: f"{v:.1f}" if isinstance(v, float) else str(v)),
                         title=title, height=h)
            fig.update_layout(
                title=dict(x=0.5, xanchor="center"), showlegend=False,
                coloraxis_showscale=False, xaxis_title=x_label, yaxis_title="",
                yaxis=dict(autorange="reversed"), margin=dict(l=160, r=20, t=50, b=40))
            fig.update_traces(textposition="outside")
            return fig

        def single_tutor_student_chart(tutor_df, value_col, color, title, x_label):
            plot_df = (
                tutor_df[tutor_df[value_col] > 0]
                .sort_values(value_col, ascending=True)
                [["student_name", value_col]].copy()
            )
            if plot_df.empty: return None
            n   = len(plot_df); h = max(300, n * 30)
            fig = px.bar(plot_df, x=value_col, y="student_name", orientation="h",
                         text=plot_df[value_col].apply(
                             lambda v: f"{v:.1f}" if isinstance(v, float) else str(v)),
                         title=title, height=h, color_discrete_sequence=[color])
            fig.update_layout(
                title=dict(x=0.5, xanchor="center"), xaxis_title=x_label,
                yaxis_title="", showlegend=False, margin=dict(l=160, r=20, t=50, b=40))
            fig.update_traces(textposition="outside")
            return fig

        st.divider()
        st.markdown("### 🚨 Top Tutors to Address")
        flag_col1, flag_col2 = st.columns(2)
        medals_a = ["🥇","🥈","🥉","4️⃣","5️⃣"]

        with flag_col1:
            st.markdown("**Most Archivable Students (Top 5)**")
            top_archive = (
                full_team_df[full_team_df["should_archive"] == True]
                .groupby("tutor_name")["student_name"].nunique().reset_index()
                .rename(columns={"tutor_name":"Tutor","student_name":"Archivable Students"})
                .sort_values("Archivable Students", ascending=False).head(5)
            )
            if top_archive.empty:
                st.success("✅ No archivable students on the team.")
            else:
                for i, row in top_archive.iterrows():
                    rank = top_archive.index.get_loc(i) + 1
                    st.markdown(
                        f"{medals_a[rank-1]} **{row['Tutor']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>"
                        f"{int(row['Archivable Students'])} students</span>",
                        unsafe_allow_html=True)

        with flag_col2:
            st.markdown("**Most Unscheduled Hours (Top 5)**")
            top_unsched = (
                full_team_df[full_team_df["unscheduled_hours"] > 0]
                .groupby("tutor_name")["unscheduled_hours"].sum().reset_index()
                .rename(columns={"tutor_name":"Tutor","unscheduled_hours":"Unscheduled Hours"})
                .sort_values("Unscheduled Hours", ascending=False).head(5)
            )
            if top_unsched.empty:
                st.success("✅ No unscheduled hours on the team.")
            else:
                for i, row in top_unsched.iterrows():
                    rank = top_unsched.index.get_loc(i) + 1
                    st.markdown(
                        f"{medals_a[rank-1]} **{row['Tutor']}** — "
                        f"<span style='color:#003f7f; font-weight:bold'>"
                        f"{row['Unscheduled Hours']:.1f} hrs</span>",
                        unsafe_allow_html=True)

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
            archive_opts = ["All Students","Archivable Only","Active Only"]
            sel_archive  = st.selectbox("Archivable Status", archive_opts)
        with f5:
            unsched_opts = ["All Students","Has Unscheduled Hours Only"]
            sel_unsched  = st.selectbox("Unscheduled Hours", unsched_opts)

        view_df = full_team_df.copy()
        if sel_tutor  != "All Tutors":  view_df = view_df[view_df["tutor_name"] == sel_tutor]
        if sel_tier   != "All Tiers":   view_df = view_df[view_df["tier"]        == sel_tier]
        if sel_brand  != "All Brands":  view_df = view_df[view_df["brand"]       == sel_brand]
        if sel_archive == "Archivable Only": view_df = view_df[view_df["should_archive"] == True]
        if sel_archive == "Active Only":     view_df = view_df[view_df["should_archive"] == False]
        if sel_unsched == "Has Unscheduled Hours Only":
            view_df = view_df[view_df["unscheduled_hours"] > 0]

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

        tab1, tab2 = st.tabs(["📦 Archivable Students","⏳ Unscheduled Hours"])

        with tab1:
            st.markdown(
                "**Archivable students** are flagged when their last session was more than 30 days ago "
                "and no future sessions are scheduled.")
            archive_df = view_df[view_df["should_archive"] == True].copy()
            if archive_df.empty:
                st.success("✅ No students flagged for archiving with the current filters.")
            else:
                if single_tutor_selected:
                    archive_df["days_since"] = (
                        pd.Timestamp.now() - pd.to_datetime(archive_df["last_session_day"])).dt.days
                    fig = single_tutor_student_chart(
                        archive_df.assign(days_since=archive_df["days_since"]),
                        "days_since","#cc0000",
                        f"{sel_tutor} — Days Since Last Session (Archivable Students)",
                        "Days Since Last Session")
                    if fig: st.plotly_chart(fig, use_container_width=True)

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
                                labels={"week_date":"Week","archivable_students":"# Archivable Students"},
                                color_discrete_sequence=["#cc0000"])
                            fig_trend.update_layout(
                                title=dict(x=0.5, xanchor="center"),
                                xaxis_title="Week", yaxis_title="# Students",
                                height=320, margin=dict(l=20, r=20, t=50, b=40))
                            fig_trend.update_traces(line=dict(width=2.5))
                            st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    archive_by_tutor = (
                        archive_df.groupby("tutor_name")["student_name"].nunique().reset_index()
                        .rename(columns={"tutor_name":"Tutor","student_name":"Students to Archive"})
                        .sort_values("Students to Archive", ascending=False))
                    st.plotly_chart(horizontal_bar(
                        archive_by_tutor, "Students to Archive","Tutor",
                        ["#ffe0e0","#cc0000"],"Archivable Students by Tutor","# Students"),
                        use_container_width=True)

                st.markdown("#### Student Detail")
                display_cols_a = ["tutor_name","student_name","brand","tier",
                                   "first_session_day","last_session_day",
                                   "hours_remaining","unscheduled_hours"]
                display_cols_a = [c for c in display_cols_a if c in archive_df.columns]
                archive_display = archive_df[display_cols_a].copy().sort_values(
                    ["tutor_name","student_name"]).rename(columns={
                    "tutor_name":"Tutor","student_name":"Student","brand":"Brand","tier":"Tier",
                    "first_session_day":"First Session","last_session_day":"Last Session",
                    "hours_remaining":"Hours Remaining","unscheduled_hours":"Unscheduled Hours"})
                st.dataframe(
                    archive_display.style.apply(
                        lambda row: ["background-color: #ffe5e5"] * len(row), axis=1),
                    use_container_width=True, hide_index=True)
                output = io.BytesIO()
                archive_display.to_excel(output, index=False); output.seek(0)
                st.download_button(
                    label="⬇️ Download Archivable Students", data=output,
                    file_name=f"Archivable_Students_Katherine_Marino.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        with tab2:
            st.markdown(
                "Students with hours purchased but **not yet scheduled**. "
                "Unscheduled hours = provisioned hours minus duration hours (scheduled + delivered).")
            unsched_df = view_df[view_df["unscheduled_hours"] > 0].copy()
            unsched_df = unsched_df.sort_values("unscheduled_hours", ascending=False)
            if unsched_df.empty:
                st.success("✅ No unscheduled hours found with the current filters.")
            else:
                if single_tutor_selected:
                    fig = single_tutor_student_chart(
                        unsched_df,"unscheduled_hours","#003f7f",
                        f"{sel_tutor} — Unscheduled Hours by Student","Unscheduled Hours")
                    if fig: st.plotly_chart(fig, use_container_width=True)

                    st.markdown("#### 📈 Trend: Unscheduled Hours Over Time")
                    snap_df = load_snapshots()
                    if snap_df.empty or sel_tutor not in snap_df["tutor_name"].values:
                        st.caption("No historical data yet — trend will build automatically each week as the app runs.")
                    else:
                        tutor_snap = snap_df[snap_df["tutor_name"] == sel_tutor].sort_values("week_date")
                        if len(tutor_snap) < 2:
                            st.caption("Only one week of data so far.")
                        else:
                            fig_trend = px.line(
                                tutor_snap, x="week_date", y="unscheduled_hours",
                                markers=True,
                                title=f"{sel_tutor} — Unscheduled Hours Week over Week",
                                labels={"week_date":"Week","unscheduled_hours":"Unscheduled Hours"},
                                color_discrete_sequence=["#003f7f"])
                            fig_trend.update_layout(
                                title=dict(x=0.5, xanchor="center"),
                                xaxis_title="Week", yaxis_title="Hours",
                                height=320, margin=dict(l=20, r=20, t=50, b=40))
                            fig_trend.update_traces(line=dict(width=2.5))
                            st.plotly_chart(fig_trend, use_container_width=True)
                else:
                    unsched_by_tutor = (
                        unsched_df.groupby("tutor_name")["unscheduled_hours"].sum().reset_index()
                        .rename(columns={"tutor_name":"Tutor","unscheduled_hours":"Unscheduled Hours"})
                        .sort_values("Unscheduled Hours", ascending=False))
                    st.plotly_chart(horizontal_bar(
                        unsched_by_tutor,"Unscheduled Hours","Tutor",
                        ["#ddeeff","#003f7f"],"Total Unscheduled Hours by Tutor","Hours"),
                        use_container_width=True)

                display_cols_u = ["tutor_name","student_name","brand","tier",
                                   "first_session_day","last_session_day",
                                   "should_archive","hours_remaining","unscheduled_hours"]
                display_cols_u = [c for c in display_cols_u if c in unsched_df.columns]
                unsched_display = unsched_df[display_cols_u].copy().rename(columns={
                    "tutor_name":"Tutor","student_name":"Student","brand":"Brand","tier":"Tier",
                    "first_session_day":"First Session","last_session_day":"Last Session",
                    "should_archive":"Archivable?","hours_remaining":"Hours Remaining",
                    "unscheduled_hours":"Unscheduled Hours"})
                st.dataframe(unsched_display, use_container_width=True, hide_index=True)
                output = io.BytesIO()
                unsched_display.to_excel(output, index=False); output.seek(0)
                st.download_button(
                    label="⬇️ Download Unscheduled Hours", data=output,
                    file_name=f"Unscheduled_Hours_Katherine_Marino.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        if st.sidebar.button("🔄 Refresh Live Data"):
            st.cache_data.clear(); st.rerun()


    # ─────────────────────────────────────────────
    # PAGE: PPW REPORT
    # ─────────────────────────────────────────────
    elif page == "⭐ NPS Scores (Tableau)":
        st.markdown("## ⭐ NPS Scores")
        st.caption("Net Promoter Score responses for your team.")

        col1, col2 = st.columns(2)
        with col1:
            nps_start = st.date_input("Response Date From",
                value=pd.Timestamp.now() - pd.DateOffset(years=1), key="nps_start")
        with col2:
            nps_end = st.date_input("Response Date To",
                value=pd.Timestamp.now(), key="nps_end")

        if st.button("🔄 Load NPS Data"):
            st.session_state["nps_start_val"] = str(nps_start)
            st.session_state["nps_end_val"]   = str(nps_end)

        nps_start_val = st.session_state.get("nps_start_val")
        nps_end_val   = st.session_state.get("nps_end_val")

        if not nps_start_val:
            st.info("Select a date range and click Load NPS Data.")
        else:
            try:
                with st.spinner("Loading NPS data..."):
                    nps_df = load_nps_scores(nps_start_val, nps_end_val, "Team Marino")
            except Exception as _e:
                st.error(f"Query error: {_e}")
                nps_df = pd.DataFrame()

            if nps_df.empty:
                st.warning("No NPS data found for this date range.")
            else:
                total       = len(nps_df)
                avg_score   = round(nps_df["nps"].mean(), 2)
                promoters   = int((nps_df["nps"] >= 9).sum())
                detractors  = int((nps_df["nps"] <= 6).sum())
                nps_score   = round((promoters - detractors) / total * 100, 1)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total Responses", total)
                c2.metric("Avg Score",       avg_score)
                c3.metric("NPS Score",       nps_score)
                c4.metric("Promoters (9-10)", promoters)
                st.divider()

                # ── Score distribution ──
                st.subheader("Score Distribution")
                score_dist = nps_df["nps"].value_counts().sort_index().reset_index()
                score_dist.columns = ["Score", "Count"]
                fig_dist = px.bar(score_dist, x="Score", y="Count",
                                  color="Score",
                                  color_continuous_scale=["#c62828","#f57f17","#2e7d32"],
                                  title="NPS Score Distribution")
                fig_dist.update_layout(height=300, showlegend=False,
                                       margin=dict(l=20,r=20,t=40,b=20))
                st.plotly_chart(fig_dist, use_container_width=True)
                st.divider()

                # ── Tutor summary ──
                st.subheader("By Tutor")
                tutor_nps = nps_df.groupby("tutor_name").agg(
                    Responses=("nps", "count"),
                    Avg_Score=("nps", "mean"),
                    Promoters=("nps", lambda x: int((x >= 9).sum())),
                    Detractors=("nps", lambda x: int((x <= 6).sum())),
                ).reset_index()
                tutor_nps["Avg Score"]  = tutor_nps["Avg_Score"].round(2)
                tutor_nps["NPS Score"]  = ((tutor_nps["Promoters"] - tutor_nps["Detractors"]) / tutor_nps["Responses"] * 100).round(1)
                tutor_nps = tutor_nps.rename(columns={"tutor_name": "Tutor"})
                tutor_nps = tutor_nps[["Tutor","Responses","Avg Score","NPS Score","Promoters","Detractors"]].sort_values("Tutor")
                st.dataframe(tutor_nps, use_container_width=True, hide_index=True)
                st.divider()

                # ── Detail view ──
                st.subheader("Detail View")
                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    nps_tutor_list = ["All Tutors"] + sorted(nps_df["tutor_name"].dropna().unique().tolist())
                    sel_nps_tutor  = st.selectbox("Filter by Tutor", nps_tutor_list, key="nps_tutor")
                with col_f2:
                    score_opts = ["All Scores", "Promoters (9-10)", "Passives (7-8)", "Detractors (0-6)"]
                    sel_score  = st.selectbox("Filter by Score", score_opts, key="nps_score_filter")

                detail = nps_df.copy()
                if sel_nps_tutor != "All Tutors":
                    detail = detail[detail["tutor_name"] == sel_nps_tutor]
                if sel_score == "Promoters (9-10)":
                    detail = detail[detail["nps"] >= 9]
                elif sel_score == "Passives (7-8)":
                    detail = detail[detail["nps"].between(7, 8)]
                elif sel_score == "Detractors (0-6)":
                    detail = detail[detail["nps"] <= 6]

                display = detail[["tutor_name","tier_name","student_name","nps","nps_responded_at","nps_comment"]].sort_values(["tutor_name","student_name"]).copy()
                display["nps_responded_at"] = pd.to_datetime(display["nps_responded_at"]).dt.strftime("%m/%d/%Y")
                display["nps_comment"]      = display["nps_comment"].fillna("")
                display = display.rename(columns={
                    "tutor_name": "Tutor", "tier_name": "Tier", "student_name": "Student",
                    "nps": "Score", "nps_responded_at": "Response Date", "nps_comment": "Comment"})
                st.dataframe(display, use_container_width=True, hide_index=True)

    elif page == "📊 Progress Updates (Tableau)":
        st.markdown("## 📊 Progress Update Summary")
        st.caption("Students with 6+ hours delivered who require a progress update.")

        col1, col2 = st.columns(2)
        with col1:
            pu_as_of = st.date_input("Sessions Before", value=pd.Timestamp.now(),
                                      key="pu_as_of")
        with col2:
            pu_since = st.date_input("Last Session On/After",
                                      value=pd.Timestamp.now() - pd.DateOffset(months=1),
                                      key="pu_since")

        if st.button("🔄 Load Progress Update Data"):
            st.session_state["pu_as_of_val"]  = str(pu_as_of)
            st.session_state["pu_since_val"]   = str(pu_since)

        pu_as_of_val  = st.session_state.get("pu_as_of_val")
        pu_since_val  = st.session_state.get("pu_since_val")

        if not pu_as_of_val:
            st.info("Select dates and click Load Progress Update Data.")
        else:
            try:
                with st.spinner("Loading progress update data..."):
                    pu_df = load_progress_updates(pu_as_of_val, pu_since_val, "Team Marino")
            except Exception as _e:
                st.error(f"Query error: {_e}")
                pu_df = pd.DataFrame()

            if pu_df.empty:
                st.warning("No data found for this date range.")
            else:
                total_students  = len(pu_df)
                on_time_count   = int(pu_df["on_time"].sum())
                needs_update    = total_students - on_time_count
                pct_on_time     = round(on_time_count / total_students * 100, 1) if total_students else 0

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total Active Students", total_students)
                c2.metric("Updates Current",       on_time_count)
                c3.metric("Needs Update",          needs_update)
                c4.metric("% Current",             f"{pct_on_time}%")
                st.divider()

                # ── Tutor summary ──
                st.subheader("By Tutor")
                tutor_pu = pu_df.groupby("tutor_name").agg(
                    Students=("student_name", "count"),
                    Requiring_Update=("on_time", lambda x: int((~x).sum())),
                    Current=("on_time", lambda x: int(x.sum())),
                ).reset_index()
                tutor_pu["% Current"] = (tutor_pu["Current"] / tutor_pu["Students"] * 100).round(1)
                tutor_pu = tutor_pu.rename(columns={
                    "tutor_name": "Tutor", "Requiring_Update": "Needs Update"}).sort_values("Tutor")

                def _pu_color(val):
                    if val >= 80: return "color: #2e7d32; font-weight: bold"
                    elif val >= 60: return "color: #f57f17; font-weight: bold"
                    else: return "color: #c62828; font-weight: bold"

                st.dataframe(tutor_pu.style.map(_pu_color, subset=["% Current"]),
                             use_container_width=True, hide_index=True)
                st.divider()

                # ── Detail view ──
                st.subheader("Detail View")
                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    tutor_list = ["All Tutors"] + sorted(pu_df["tutor_name"].unique().tolist())
                    sel_tutor  = st.selectbox("Filter by Tutor", tutor_list, key="pu_tutor")
                with col_f2:
                    status_opts = ["All", "Needs Update", "Current"]
                    sel_status  = st.selectbox("Filter by Status", status_opts, key="pu_status")

                detail = pu_df.copy()
                if sel_tutor != "All Tutors":
                    detail = detail[detail["tutor_name"] == sel_tutor]
                if sel_status == "Needs Update":
                    detail = detail[detail["on_time"] == False]
                elif sel_status == "Current":
                    detail = detail[detail["on_time"] == True]

                display = detail[["tutor_name","tier","student_name","hours_delivered",
                                   "last_session","last_progress_update","on_time"]].sort_values(["tutor_name","student_name"]).copy()
                display["last_session"]         = pd.to_datetime(display["last_session"]).dt.strftime("%m/%d/%Y")
                display["last_progress_update"] = pd.to_datetime(display["last_progress_update"]).dt.strftime("%m/%d/%Y").fillna("Never")
                display["hours_delivered"]      = display["hours_delivered"].round(1)
                display["on_time"]              = display["on_time"].map({True: "✅", False: "❌"})
                display = display.rename(columns={
                    "tutor_name": "Tutor", "tier": "Tier", "student_name": "Student",
                    "hours_delivered": "Hours", "last_session": "Last Session",
                    "last_progress_update": "Last Progress Update", "on_time": "Current"})
                st.dataframe(display, use_container_width=True, hide_index=True)

    elif page == "📄 PPW Report (Tableau)":
        st.markdown("## 📄 PPW Report")
        st.caption("First-session parent progress write-up attachment data.")

        col1, col2 = st.columns(2)
        with col1:
            start_date = st.date_input("Course Start From", value=pd.Timestamp.now().replace(day=1) - pd.DateOffset(months=1))
        with col2:
            end_date = st.date_input("Course Start To", value=pd.Timestamp.now())

        if st.button("🔄 Load PPW Data"):
            st.session_state["ppw_start"] = str(start_date)
            st.session_state["ppw_end"]   = str(end_date)

        ppw_start = st.session_state.get("ppw_start")
        ppw_end   = st.session_state.get("ppw_end")

        if not ppw_start:
            st.info("Select a date range and click Load PPW Data.")
        else:
            try:
                with st.spinner("Loading PPW data..."):
                    ppw_df = load_ppw_data(ppw_start, ppw_end, "Team Marino")
                st.caption(f"Debug: {len(ppw_df)} rows, dates {ppw_start} to {ppw_end}, team: Katherine Marino")
            except Exception as _e:
                st.error(f"Query error: {_e}")
                ppw_df = pd.DataFrame()
            if ppw_df.empty:
                st.warning("No PPW data found for this date range.")
            else:
                total    = len(ppw_df)
                uploaded = int(ppw_df["attachment_uploaded"].sum())
                pct      = round(uploaded / total * 100, 1) if total > 0 else 0
                c1, c2, c3 = st.columns(3)
                c1.metric("Total First Sessions", total)
                c2.metric("PPWs Uploaded", uploaded)
                c3.metric("% Uploaded", f"{pct}%")
                st.divider()

                st.subheader("By Tutor")
                tutor_summary = ppw_df.groupby("tutor_name").agg(
                    First_Sessions=("student_name", "count"),
                    PPWs_Uploaded=("attachment_uploaded", "sum"),
                ).reset_index()
                tutor_summary["% Uploaded"] = (tutor_summary["PPWs_Uploaded"] / tutor_summary["First_Sessions"] * 100).round(1)
                tutor_summary = tutor_summary.rename(columns={
                    "tutor_name": "Tutor", "First_Sessions": "First Sessions", "PPWs_Uploaded": "PPWs Uploaded"})

                def _color_pct(val):
                    if val >= 80: color = "#2e7d32"
                    elif val >= 60: color = "#f57f17"
                    else: color = "#c62828"
                    return f"color: {color}; font-weight: bold"

                st.dataframe(tutor_summary.style.map(_color_pct, subset=["% Uploaded"]),
                             use_container_width=True, hide_index=True)
                st.divider()

                st.subheader("Detail View")
                tutor_list = ["All Tutors"] + sorted(ppw_df["tutor_name"].unique().tolist())
                selected_tutor = st.selectbox("Filter by Tutor", tutor_list)
                detail_df = ppw_df if selected_tutor == "All Tutors" else ppw_df[ppw_df["tutor_name"] == selected_tutor]
                display = detail_df[["tutor_name","student_name","brand","starts_at","attachment_uploaded"]].sort_values(["tutor_name","student_name"]).copy()
                display["starts_at"] = pd.to_datetime(display["starts_at"]).dt.strftime("%m/%d/%Y")
                display["attachment_uploaded"] = display["attachment_uploaded"].map({1: "✅", 0: "❌"})
                display = display.rename(columns={
                    "tutor_name": "Tutor", "student_name": "Student",
                    "brand": "Brand", "starts_at": "Course Start", "attachment_uploaded": "PPW Uploaded"})
                st.dataframe(display, use_container_width=True, hide_index=True)

    # ─────────────────────────────────────────────
    # PAGE: ANNUAL REVIEWS
    # ─────────────────────────────────────────────

    if page == "Annual Reviews":
        st.markdown('<div class="main-title">Annual Reviews 📋</div>', unsafe_allow_html=True)
        selected_annual_tutor = st.selectbox("Select a Tutor:", annelies_tutors)

        if selected_annual_tutor:
            tutor_review                = annual_review_df[annual_review_df["tutor_name"] == selected_annual_tutor]
            tutor_review_repurchase     = repurchase_df[repurchase_df["Tutor Name"] == selected_annual_tutor]
            tutor_review_monthly_metric = monthly_metric_annual_review_df[
                monthly_metric_annual_review_df["Tutor Name"] == selected_annual_tutor]

            if not tutor_review.empty:
                row                      = tutor_review.iloc[0]
                tutor_tier               = row["tier"]
                row_repurchase           = tutor_review_repurchase.iloc[0]
                tutor_tier_repurchase    = row_repurchase["Current Tier"]
                tutor_deliverytarget     = row_repurchase["Delivery Target"]
                row_monthly_metric       = tutor_review_monthly_metric
                tutor_tier_monthly_metric = row_monthly_metric["Tier"].iloc[0]

                team_df  = annual_review_df[annual_review_df["fl"] == "Katherine Marino"]
                tier_df  = annual_review_df[annual_review_df["tier"] == tutor_tier]

                team_repurchase_df      = repurchase_df[repurchase_df["Team Name"] == "Team De Groot"]
                tier_repurchase_df      = repurchase_df[repurchase_df["Current Tier"] == tutor_tier]
                tierdelivery_repurchase_df = repurchase_df[
                    (repurchase_df["Current Tier"] == tutor_tier) &
                    (repurchase_df["Delivery Target"] == tutor_deliverytarget)]

                team_monthly_metric_df = monthly_metric_annual_review_df[
                    monthly_metric_annual_review_df["Faculty Leader"] == "Katherine Marino"]
                tier_monthly_metric_df = monthly_metric_annual_review_df[
                    monthly_metric_annual_review_df["Tier"] == tutor_tier_monthly_metric]

                metrics = {
                    "sessions_on_time":         "Sessions On Time (%)",
                    "% Parents Updates Done on Time": "Percent of Parent Updates Completed on Time",
                    "prep_time":                "Prep Time (%)",
                    "Repurchases Weighted":     "Weighted Repurchase",
                    "average_nps":              "Average NPS",
                    "% of Active Students with Progress Updates Completed in last 2 months":
                        "Progress Update Average Percentage",
                    "current_sci":              "Current SCI",
                    "availability_percent":     "Percent to Availability (%)",
                    "delivery_percent":         "Percent to Delivery (%)"
                }
                subject_df = load_subject_additions()

                for col, label in metrics.items():
                    if col == "availability_percent":
                        st.divider()
                        st.subheader("Subject Additions")
                        if "tutor_name" in subject_df.columns:
                            tutor_subjects = subject_df.loc[
                                subject_df["tutor_name"].str.strip().str.lower() ==
                                selected_annual_tutor.strip().lower(), "subject"
                            ].dropna().tolist()
                        else:
                            st.error("Column 'tutor_name' not found in Subject Addition sheet.")
                            tutor_subjects = []
                        if len(tutor_subjects) == 0:
                            st.markdown(
                                "<p style='color: gray; font-style: italic; font-size: 1.1rem;'>None</p>",
                                unsafe_allow_html=True)
                        else:
                            for subj in tutor_subjects:
                                st.markdown(f"""
                                <div style='background-color: #f8f9fa; border-radius: 8px;
                                            padding: 10px 15px; margin: 6px 0; font-size: 1.1rem;
                                            font-weight: 500; color: #333;
                                            box-shadow: 0 1px 3px rgba(0,0,0,0.1);'>
                                📘 {subj}
                                </div>""", unsafe_allow_html=True)

                    if col in ["% Parents Updates Done on Time",
                               "% of Active Students with Progress Updates Completed in last 2 months"]:
                        tutor_value_monthly_metric = np.nanmean(row_monthly_metric[col].values)
                    elif col in ["Repurchases Weighted"]:
                        tutor_value_repurchase = row_repurchase[col]
                    else:
                        tutor_value = row[col]

                    if col in ["sessions_on_time","prep_time","availability_percent","delivery_percent",
                               "% Parents Updates Done on Time",
                               "% of Active Students with Progress Updates Completed in last 2 months"]:
                        if col in ["% Parents Updates Done on Time",
                                   "% of Active Students with Progress Updates Completed in last 2 months"]:
                            tutor_value_display = f"{tutor_value_monthly_metric * 100:.0f}%"
                            tutor_value_plot    = tutor_value_monthly_metric * 100
                            team_avg = team_monthly_metric_df[col].mean() * 100
                            tier_avg = tier_monthly_metric_df[col].mean() * 100
                        else:
                            tutor_value_display = f"{tutor_value * 100:.0f}%"
                            tutor_value_plot    = tutor_value * 100
                            team_avg = team_df[col].mean() * 100
                            tier_avg = tier_df[col].mean() * 100
                    else:
                        if col in ["Repurchases Weighted"]:
                            tutor_value_display = f"{tutor_value_repurchase:.1f}"
                            tutor_value_plot    = tutor_value_repurchase
                            team_avg = tierdelivery_repurchase_df[col].mean()
                            tier_avg = tier_repurchase_df[col].mean()
                        else:
                            tutor_value_display = f"{tutor_value:.1f}"
                            tutor_value_plot    = tutor_value
                            team_avg = team_df[col].mean()
                            tier_avg = tier_df[col].mean()

                    st.markdown("<hr>", unsafe_allow_html=True)
                    st.markdown(f"<h3 style='text-align:center'>{label}</h3>", unsafe_allow_html=True)

                    if col == "Repurchases Weighted":
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor,"Tier/Delivery Target"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue","lightgrey"]))
                        fig_team.update_layout(
                            title=dict(text="VS Tier/Delivery Target", x=0.5,
                                       xanchor="center", font=dict(size=16)),
                            yaxis_title="Value", xaxis_title="",
                            height=300, margin=dict(l=20, r=20, t=40, b=20))
                    else:
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor,"Team Avg"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue","lightgrey"]))
                        fig_team.update_layout(
                            title=dict(text="VS Team", x=0.5,
                                       xanchor="center", font=dict(size=16)),
                            yaxis_title="Value", xaxis_title="",
                            height=300, margin=dict(l=20, r=20, t=40, b=20))

                    fig_tier = go.Figure(go.Bar(
                        x=[selected_annual_tutor,"Tier Avg"],
                        y=[tutor_value_plot, tier_avg],
                        marker_color=["blue","lightgrey"]))
                    fig_tier.update_layout(
                        title=dict(text="VS Tier", x=0.5,
                                   xanchor="center", font=dict(size=16)),
                        yaxis_title="Value", xaxis_title="",
                        height=300, margin=dict(l=20, r=20, t=40, b=20))

                    col1, col2, col3 = st.columns([1, 1, 1])
                    with col1:
                        st.markdown(
                            f"<div style='font-size:24px; font-weight:bold; text-align:center;'>"
                            f"{selected_annual_tutor}<br>{tutor_value_display}</div>",
                            unsafe_allow_html=True)
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
            index=annelies_tutors.index(
                st.session_state.pop("kpi_trends_tutor", None) or annelies_tutors[0])
            if annelies_tutors else 0,
            key="kpi_trends_selectbox"
        )

        if selected_tutor:
            tutor_df   = monthly_df[monthly_df["Tutor Name"] == selected_tutor].copy()
            tutor_tier = annual_df.loc[annual_df["tutor_name"] == selected_tutor, "tier"].values
            tutor_tier = tutor_tier[0] if len(tutor_tier) > 0 else None

            import re
            def extract_end_date(range_str):
                if pd.isna(range_str): return pd.NaT
                clean_str = range_str.replace("-","to").replace("–","to").replace("—","to")
                parts = clean_str.split("to")
                if len(parts) < 2: return pd.NaT
                end_str = parts[-1].strip()
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            tutor_df["Date Parsed"] = tutor_df["Date Range"].apply(extract_end_date)
            annelies_team = master_df[master_df["Faculty Leader"] == "Katherine Marino"]["Full Name"].dropna()
            team_df       = monthly_df[monthly_df["Tutor Name"].isin(annelies_team)].copy()
            team_df["Date Parsed"] = team_df["Date Range"].apply(extract_end_date)

            if tutor_tier:
                tier_tutors = annual_df[annual_df["tier"] == tutor_tier]["tutor_name"]
                tier_df     = monthly_df[monthly_df["Tutor Name"].isin(tier_tutors)].copy()
                tier_df["Date Parsed"] = tier_df["Date Range"].apply(extract_end_date)
            else:
                tier_df = pd.DataFrame()

            metrics = {
                "% to Delivery Target":        "% to Delivery Target",
                "% to Availability Target":    "% to Availability Target",
                "% Sessions on Time":          "% Sessions on Time",
                "% Parents Updates Done on Time": "% to Parent Updates Completed",
                "% of Active Students with Progress Updates Completed in last 2 months":
                    "% Progress Updates Completed",
                "Weighted Repurchases":        "Weighted Repurchases",
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

                tutor_plot_df = tutor_df.dropna(subset=["Date Parsed"]).drop_duplicates(subset=["Date Parsed"], keep="first").sort_values("Date Parsed").tail(6)
                team_plot_df  = team_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
                tier_plot_df  = tier_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed") \
                                if not tier_df.empty else pd.DataFrame()

                latest_value   = tutor_plot_df[metric].iloc[-1] if not tutor_plot_df.empty else None
                latest_display = f"{latest_value:.0f}%" if metric in percent_metrics \
                                 else f"{latest_value:.2f}"

                # Use tutor's dates as reference — align team/tier to same date range
                _tutor_dates = tutor_plot_df["Date Parsed"].tolist()
                _date_label_map = dict(zip(tutor_plot_df["Date Parsed"], tutor_plot_df["Date Range"]))

                if not team_plot_df.empty:
                    team_grouped = team_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    team_grouped = team_grouped[team_grouped["Date Parsed"].isin(_tutor_dates)].sort_values("Date Parsed")
                    fig_team = px.line(team_grouped, x="Date Parsed", y=metric,
                                       title="VS Team", markers=True)
                    fig_team.add_scatter(
                        x=tutor_plot_df["Date Parsed"], y=tutor_plot_df[metric],
                        mode="lines+markers", name=selected_tutor, line=dict(width=3))
                    fig_team.update_layout(
                        title=dict(x=0.5, xanchor="center", font=dict(size=16)),
                        xaxis=dict(tickangle=30, tickvals=_tutor_dates,
                                   ticktext=[_date_label_map[d] for d in _tutor_dates]),
                        yaxis_title=None, xaxis_title=None,
                        height=350, margin=dict(l=20, r=20, t=50, b=40))

                if not tier_plot_df.empty:
                    tier_grouped = tier_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    tier_grouped = tier_grouped[tier_grouped["Date Parsed"].isin(_tutor_dates)].sort_values("Date Parsed")
                    fig_tier = px.line(tier_grouped, x="Date Parsed", y=metric,
                                       title="VS Tier", markers=True)
                    fig_tier.add_scatter(
                        x=tutor_plot_df["Date Parsed"], y=tutor_plot_df[metric],
                        mode="lines+markers", name=selected_tutor, line=dict(width=3))
                    fig_tier.update_layout(
                        title=dict(x=0.5, xanchor="center", font=dict(size=16)),
                        xaxis=dict(tickangle=30, tickvals=_tutor_dates,
                                   ticktext=[_date_label_map[d] for d in _tutor_dates]),
                        yaxis_title=None, xaxis_title=None,
                        height=350, margin=dict(l=20, r=20, t=50, b=40))

                row1_col1, row1_col2 = st.columns([1, 3])
                with row1_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>"
                        f"{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True)
                with row1_col2:
                    if not team_plot_df.empty:
                        st.plotly_chart(fig_team, use_container_width=True)

                row2_col1, row2_col2 = st.columns([1, 3])
                with row2_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>"
                        f"{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True)
                with row2_col2:
                    if not tier_plot_df.empty:
                        st.plotly_chart(fig_tier, use_container_width=True)


    # ─────────────────────────────────────────────
    # PAGE: CONCERNS
    # ─────────────────────────────────────────────

    if page == "Concerns":
        st.markdown('<div class="main-title">Tutor Concerns 📌</div>', unsafe_allow_html=True)

        concerns_df = load_tutor_concerns()
        fl_df       = concerns_df[concerns_df["Faculty Leader Name"] == faculty_leader_name]

        if fl_df.empty:
            st.info("No concern data available for your team.")
        else:
            import re
            def extract_end_date(range_str):
                if pd.isna(range_str): return pd.NaT
                clean_str = range_str.replace("-","to").replace("–","to").replace("—","to")
                parts     = clean_str.split("to")
                if len(parts) < 2: return pd.NaT
                end_str   = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", parts[-1].strip())
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            fl_df["Date"] = fl_df["Date"].apply(extract_end_date)
            latest_date   = fl_df["Date"].max()
            latest_df     = fl_df[fl_df["Date"] == latest_date]

            all_dates = sorted(fl_df["Date"].dropna().unique())
            if len(all_dates) >= 2:
                prev_date = all_dates[-2]
                prev_df   = fl_df[fl_df["Date"] == prev_date]
                merged    = latest_df[["Tutor Name","Concern Group"]].merge(
                    prev_df[["Tutor Name","Concern Group"]].rename(
                        columns={"Concern Group":"Prev Group"}),
                    on="Tutor Name", how="inner")
                merged["Change"] = (
                    pd.to_numeric(merged["Concern Group"], errors="coerce") -
                    pd.to_numeric(merged["Prev Group"],    errors="coerce"))
                big_jumps = merged[merged["Change"].abs() >= 2].sort_values("Change")

                if not big_jumps.empty:
                    st.markdown("### 🚨 Concern Group Movement Alerts")
                    st.caption(
                        f"Tutors who moved **2+ concern groups** between "
                        f"{prev_date.date()} and {latest_date.date()}")
                    for _, row in big_jumps.iterrows():
                        tname     = row["Tutor Name"]
                        change    = int(row["Change"])
                        prev_g    = int(row["Prev Group"])
                        curr_g    = int(row["Concern Group"])
                        direction = "⬆️ Worsened" if change > 0 else "⬇️ Improved"
                        color     = "#fff0f0" if change > 0 else "#f0fff4"
                        border    = "#ffcccc" if change > 0 else "#b2f5c8"
                        arrow     = "▲" if change > 0 else "▼"

                        kpi_context = ""
                        try:
                            _concern_kpi_df = load_kpi_data()
                        except Exception:
                            _concern_kpi_df = pd.DataFrame()
                        if not _concern_kpi_df.empty and "Tutor Name" in _concern_kpi_df.columns:
                            tutor_kpi = _concern_kpi_df[_concern_kpi_df["Tutor Name"] == tname].copy()
                            if not tutor_kpi.empty:
                                tutor_kpi["Date Range Parsed"] = pd.to_datetime(
                                    tutor_kpi["Date Range"].str.split(" - ").str[0], errors="coerce")
                                tutor_kpi = tutor_kpi.sort_values("Date Range Parsed").tail(2)
                                if len(tutor_kpi) >= 2:
                                    kpi_cols_c = [
                                        "% to Delivery Target",
                                        "% to Availability Target",
                                        "% Sessions on Time",
                                        "% Parents Updates Done on Time",
                                    ]
                                    changes = []
                                    for kc in kpi_cols_c:
                                        if kc in tutor_kpi.columns:
                                            v1   = tutor_kpi.iloc[-2][kc]
                                            v2   = tutor_kpi.iloc[-1][kc]
                                            if pd.notna(v1) and pd.notna(v2):
                                                diff = (v2 - v1) * 100
                                                if abs(diff) >= 3:
                                                    short     = kc.replace("% to ","").replace("% ","")
                                                    arrow_kpi = "↑" if diff > 0 else "↓"
                                                    changes.append(f"{short} {arrow_kpi}{abs(diff):.0f}pp")
                                    if changes:
                                        kpi_context = "KPI shifts: " + ", ".join(changes)

                        latest_reasons = ""
                        latest_row_c   = latest_df[latest_df["Tutor Name"] == tname]
                        if not latest_row_c.empty and "Reasons" in latest_row_c.columns:
                            reasons_val = latest_row_c.iloc[0].get("Reasons","")
                            if pd.notna(reasons_val) and str(reasons_val).strip():
                                latest_reasons = f"Reasons: {reasons_val}"

                        detail_parts = [p for p in [kpi_context, latest_reasons] if p]
                        detail_html  = (
                            f"<div style='font-size:0.82rem; color:#555; margin-top:5px;'>"
                            f"{' &nbsp;|&nbsp; '.join(detail_parts)}</div>"
                        ) if detail_parts else ""

                        st.markdown(f"""
                        <div style='background:{color}; border:1.5px solid {border}; border-radius:10px;
                                    padding:12px 16px; margin-bottom:8px;'>
                            <div style='font-weight:700; font-size:1rem;'>
                                {direction} &nbsp; <b>{tname}</b> &nbsp;
                                <span style='color:#888; font-weight:400;'>
                                    Group {prev_g} {arrow} Group {curr_g} ({change:+d})
                                </span>
                            </div>
                            {detail_html}
                        </div>""", unsafe_allow_html=True)
                    st.divider()

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
                mime="text/csv")

            st.markdown("---")
            tutor_names    = sorted(fl_df["Tutor Name"].dropna().unique().tolist())
            selected_tutor = st.selectbox("Select a Tutor", tutor_names)

            if selected_tutor:
                tutor_df = fl_df[fl_df["Tutor Name"] == selected_tutor].sort_values("Date")
                fig = px.line(tutor_df, x="Date", y="Concern Group", markers=True,
                              title=f"{selected_tutor} Concern Score Over Time")
                fig.update_yaxes(range=[1, 5], dtick=1, title="Concern Group", autorange=False)
                st.plotly_chart(fig, use_container_width=True)
                st.subheader(f"{selected_tutor} Details")
                st.dataframe(tutor_df[["Date","Concern Group","Reasons"]])
                st.download_button(
                    label=f"Download {selected_tutor} Concerns",
                    data=tutor_df.to_csv(index=False),
                    file_name=f"{selected_tutor}_Concerns.csv",
                    mime="text/csv")


    # ─────────────────────────────────────────────
    # PAGE: KPI TABLE
    # ─────────────────────────────────────────────

    if page == "KPI Table":
        df = load_kpi_data()
        df["Date Range Parsed"] = pd.to_datetime(
            df["Date Range"].str.split(" - ").str[0], errors="coerce")
        latest_range_parsed = df["Date Range Parsed"].max()
        latest_range        = df.loc[df["Date Range Parsed"] == latest_range_parsed, "Date Range"].iloc[0]
        leader_name         = "Katherine Marino"
        team_df             = df[(df["Date Range"] == latest_range) &
                                  (df["Faculty Leader"] == leader_name)].copy()

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
        st.divider(); st.divider()

        st.subheader("Team Summary KPIs")
        n_metrics   = len(metrics); n_cols = 3
        rows_needed = (n_metrics + n_cols - 1) // n_cols
        for r in range(rows_needed):
            cols = st.columns(n_cols)
            for i, col in enumerate(cols):
                idx = r * n_cols + i
                if idx < n_metrics:
                    metric = metrics[idx]
                    avg    = team_df[metric].mean(skipna=True)
                    color  = "🟢" if avg >= 90 else ("🟡" if avg >= 75 else "🔴")
                    col.metric(label=f"{color} {metric}", value=f"{avg:.1f}%")

        st.divider(); st.divider()
        st.subheader("📊 Team KPI Changes from Previous Period")

        df["Date Range Parsed"]  = pd.to_datetime(
            df["Date Range"].str.split(" - ").str[0], errors="coerce")
        date_ranges_sorted = df.sort_values("Date Range Parsed")["Date Range"].dropna().unique().tolist()

        if len(date_ranges_sorted) < 2:
            st.info("Not enough time periods available to calculate changes.")
        else:
            latest_range = date_ranges_sorted[-1]
            prev_range   = date_ranges_sorted[-2]
            latest_team  = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == latest_range)]
            prev_team    = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == prev_range)]
            latest_avg   = latest_team[metrics].mean()
            prev_avg     = prev_team[metrics].mean()
            change_df    = pd.DataFrame({
                "Metric":              metrics,
                f"{prev_range} Avg":   prev_avg.values,
                f"{latest_range} Avg": latest_avg.values,
                "Change (pp)":         (latest_avg - prev_avg).values
            })
            for c in [f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]:
                change_df[c] = change_df[c] * 100

            def style_change(val):
                color = "lightgreen" if val > 0 else ("lightcoral" if val < 0 else "white")
                return f"background-color: {color}; font-weight: bold; text-align: center"

            styled_df = change_df[["Metric",f"{prev_range} Avg",f"{latest_range} Avg","Change (pp)"]].copy()
            styled_df_display = styled_df.style.format({
                f"{prev_range} Avg":   "{:.1f}%",
                f"{latest_range} Avg": "{:.1f}%",
                "Change (pp)":         "{:+.1f} pp"
            }).map(style_change, subset=["Change (pp)"])
            st.write(styled_df_display)

            max_abs_change = max(abs(change_df["Change (pp)"].max()),
                                 abs(change_df["Change (pp)"].min()))
            col1, col2, col3 = st.columns([1, 12, 1])
            with col2:
                fig_change = px.bar(
                    change_df, x="Metric", y="Change (pp)",
                    color="Change (pp)",
                    color_continuous_scale=["red","white","green"],
                    text=change_df["Change (pp)"].apply(lambda x: f"{x:+.1f} pp"),
                    title=f"Change in Team Averages: {prev_range} → {latest_range}",
                    height=600)
                fig_change.update_layout(
                    title_x=0.20, xaxis_title="",
                    yaxis_title="Change (percentage points)",
                    margin=dict(l=20, r=20, t=60, b=40),
                    coloraxis_colorbar=dict(title="Change"))
                fig_change.update_coloraxes(cmin=-max_abs_change, cmax=max_abs_change)
                st.plotly_chart(fig_change, use_container_width=True)

        st.divider(); st.divider()
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
            color_map = {fl: ("blue" if fl == leader_name else "lightgray")
                         for fl in plot_df["Faculty Leader"]}
            fig = px.bar(
                plot_df, x="Faculty Leader", y=metric + "_pct",
                color="Faculty Leader", color_discrete_map=color_map,
                text=plot_df[metric + "_pct"].apply(lambda x: f"{x:.1f}%"),
                labels={metric + "_pct":"Percent"}, height=400)
            y_max = 130 if metric == "% to Availability Target" else 100
            fig.update_layout(
                title=dict(text=f"{title_prefix}: {metric}", x=0.5, xanchor="center"),
                showlegend=False, margin=dict(l=20, r=20, t=50, b=40),
                yaxis=dict(range=[0, y_max], tickformat=".0f%"))
            col1, col2, col3 = st.columns([1, 4, 1])
            with col2:
                st.plotly_chart(fig, use_container_width=True)

        st.divider(); st.divider()
        st.subheader("Team KPI Table")
        dashboard_df = load_dashboard_metrics()
        if dashboard_df.empty:
            st.warning("Dashboard metrics file not found or empty.")
        else:
            team_dashboard_df = dashboard_df[dashboard_df["Faculty Leader Name"] == leader_name].copy()
            kpi_thresholds = {
                "% to Delivery Target":    (0.70, 0.80),
                "% to Availability Target":(0.80, 1.00),
                "Prep Time %":             (0.20, 0.10),
                "% Parents Updates Done on Time": (0.75, 0.90),
                "% Sessions on Time":      (0.80, 0.95),
                "% of Active Students with Progress Updates Completed": (0.50, 0.80)
            }

            def highlight_kpi(val, metric):
                if pd.isna(val): return ""
                low, high = kpi_thresholds.get(metric, (None, None))
                if low is None: return ""
                if metric == "Prep Time %":
                    if val < high:  return "background-color: lightgreen"
                    elif val > low: return "background-color: lightcoral"
                else:
                    if val > high:  return "background-color: lightgreen"
                    elif val < low: return "background-color: lightcoral"
                return ""

            styled_df_kpi = team_dashboard_df.style
            for metric in kpi_thresholds.keys():
                if metric in team_dashboard_df.columns:
                    styled_df_kpi = styled_df_kpi.map(
                        lambda v, m=metric: highlight_kpi(v, m), subset=[metric])
            styled_df_kpi = styled_df_kpi.format({
                col: "{:.2f}" if "%" not in col else "{:.1%}"
                for col in team_dashboard_df.select_dtypes(include=["float","int"]).columns
            }).set_table_styles([
                {"selector":"th","props":[("text-align","center"),
                                           ("white-space","normal"),("word-wrap","break-word")]},
                {"selector":"td","props":[("text-align","center")]}
            ])
            st.write(styled_df_kpi)

            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                team_dashboard_df.to_excel(writer, index=False, sheet_name="Team KPI Leaderboard")
                workbook  = writer.book
                worksheet = writer.sheets["Team KPI Leaderboard"]
                fmt_pct   = workbook.add_format({"num_format":"0.00%","align":"center"})
                fmt_dec   = workbook.add_format({"num_format":"0.00", "align":"center"})
                fmt_wrap  = workbook.add_format({"text_wrap":True,    "align":"center"})
                for col_num, col_name in enumerate(team_dashboard_df.columns):
                    if col_name in kpi_thresholds:
                        worksheet.set_column(col_num, col_num, 15, fmt_pct)
                    elif pd.api.types.is_numeric_dtype(team_dashboard_df[col_name]):
                        worksheet.set_column(col_num, col_num, 15, fmt_dec)
                    else:
                        worksheet.set_column(col_num, col_num, 20, fmt_wrap)
            output.seek(0)
            st.download_button(
                label="Download Team KPI Data", data=output,
                file_name=f"{leader_name.replace(' ','_')}_Dashboard_Metrics.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        st.subheader("Progress Update Emails")
        progress_df = load_progressupdate_metrics()
        if progress_df.empty:
            st.warning("Progress Update Emails sheet not found or empty.")
        else:
            leader_to_team = {
                "Katherine Marino":           "Team Marino",
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
                output.seek(0)
                st.download_button(
                    label="Download Progress Update Emails", data=output,
                    file_name=f"{leader_name.replace(' ','_')}_Progress_Update_Emails.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                st.info(f"No Progress Update Emails found for {leader_name}.")
    # ─────────────────────────────────────────────────────────────────────────
    # ANNUAL REVIEWS PAGE
    # ─────────────────────────────────────────────────────────────────────────
    elif page == "🔰 90-Day Review":
        st.markdown("## 🔰 90-Day Review")
        st.caption("Performance metrics for tutors in their first 90 days.")

        # Load snapshot data needed by cards
        ar_skills_df = pd.DataFrame()  # loaded after tutor selected
        try:
            ar_video_snap = load_video_snapshots()
        except Exception:
            ar_video_snap = pd.DataFrame()
        try:
            ar_exam_snap = load_exams_snapshots()
        except Exception:
            ar_exam_snap = pd.DataFrame()
        try:
            ar_grades_snap = load_grades_snapshots()
        except Exception:
            ar_grades_snap = pd.DataFrame()
        try:
            ar_arch_snap = gh_read(SNAPSHOT_HISTORY_FILE)
            if ar_arch_snap is None or ar_arch_snap.empty:
                ar_arch_snap = load_snapshots()
        except Exception:
            ar_arch_snap = pd.DataFrame()

        team_tutors_df = master_tutor_df[
            (master_tutor_df["Faculty Leader"] == "Katherine Marino") &
            (~master_tutor_df["Full Name"].isin(["Katherine Marino"]))
        ].copy()
        team_tutors_df["hire_date"] = pd.to_datetime(team_tutors_df["hire_date"], errors="coerce")
        today = pd.Timestamp.now().normalize()
        team_tutors_df["days_since_hire"] = (today - team_tutors_df["hire_date"]).dt.days

        recent_hires = team_tutors_df[team_tutors_df["days_since_hire"] <= 150]["Full Name"].tolist()
        all_90d_names = sorted(team_tutors_df["Full Name"].dropna().unique().tolist())
        non_recent = [n for n in all_90d_names if n not in recent_hires]

        def _90d_label(n):
            return f"\u2b50 {n} (recent hire)" if n in recent_hires else n

        display_options = ["\u2014 Select —"] + sorted(recent_hires) + (["\u2500\u2500\u2500\u2500\u2500"] if non_recent else []) + non_recent
        display_labels  = ["\u2014 Select —"] + [_90d_label(n) for n in sorted(recent_hires)] + (["\u2500\u2500\u2500\u2500\u2500"] if non_recent else []) + non_recent

        sel_90d_idx = st.selectbox("Select Tutor", range(len(display_options)),
                                   format_func=lambda i: display_labels[i], key="90d_tutor_select")
        nr90_tutor = display_options[sel_90d_idx] if sel_90d_idx > 0 and "\u2500" not in display_options[sel_90d_idx] else None

        if not nr90_tutor or nr90_tutor == "\u2014 Select —":
            st.info("Select a tutor above to view their 90-day review.")
            if recent_hires:
                st.markdown(f"**\u2b50 Tutors within 150-day review window ({len(recent_hires)}):** " + ", ".join(sorted(recent_hires)))
        else:
            tutor_row_90d = team_tutors_df[team_tutors_df["Full Name"] == nr90_tutor].iloc[0]
            hire_date_90d = tutor_row_90d["hire_date"]
            days_in_90d   = int(tutor_row_90d["days_since_hire"]) if pd.notna(tutor_row_90d["days_since_hire"]) else None

            st.markdown(f"### {nr90_tutor} \u2014 90-Day Review")
            hire_str_90d = hire_date_90d.strftime("%B %d, %Y") if pd.notna(hire_date_90d) else "Unknown"
            if days_in_90d is not None:
                st.caption(f"Hired: **{hire_str_90d}** | Days since hire: **{days_in_90d}**")
            else:
                st.caption(f"Hired: **{hire_str_90d}**")

            # Check if BUC-only tutor
            try:
                _bp_df = load_brand_permissions()
                if not _bp_df.empty and "tutor_name" in _bp_df.columns:
                    _tutor_brands = set(_bp_df[_bp_df["tutor_name"] == nr90_tutor]["brand_name"].dropna().unique().tolist())
                    _is_buc_only = bool(_tutor_brands) and _tutor_brands <= {"Back-Up Care Tutoring"}
                else:
                    _is_buc_only = False
            except Exception:
                _is_buc_only = False

            # Display brands
            try:
                _bp_df2 = load_brand_permissions()
                if not _bp_df2.empty and "tutor_name" in _bp_df2.columns:
                    _tutor_brands_list = sorted(_bp_df2[_bp_df2["tutor_name"] == nr90_tutor]["brand_name"].dropna().unique().tolist())
                    if _tutor_brands_list:
                        _brand_colors = {
                            "Private Tutoring": "#1f77b4",
                            "Back-Up Care Tutoring": "#ff7f0e",
                            "Academics": "#2ca02c",
                            "Trial": "#9467bd",
                            "Small Group Course": "#e377c2",
                            "Group Course": "#8c564b",
                            "Boot Camp": "#d62728",
                            "School-Pay Private Tutoring": "#17becf",
                            "Tutoring": "#ffbb78",
                        }
                        pills_html = " ".join([
                            f"<span style='background:{_brand_colors.get(b,'#888')}22; "
                            f"color:{_brand_colors.get(b,'#888')}; border:1px solid {_brand_colors.get(b,'#888')}; "
                            f"border-radius:12px; padding:2px 10px; font-size:0.85em; font-weight:600'>{b}</span>"
                            for b in _tutor_brands_list
                        ])
                        st.markdown(f"**Approved Brands:** {pills_html}", unsafe_allow_html=True)
            except Exception:
                pass

            if _is_buc_only:
                st.warning("⚠️ **BUC-Only Tutor** — This tutor is approved for Back-Up Care only and is **not eligible for a raise** at their 90-day review.")

            # Load skills using hire date as start
            try:
                _skills_start = hire_date_90d.strftime("%Y-%m-%d") if pd.notna(hire_date_90d) else "2024-01-01"
                _conn_sk = get_redshift_connection()
                _skills_q = f"""
                    SELECT dw.employees.id AS emp_id,
                        dw.users.first_name||' '||dw.users.last_name AS tutor_name,
                        dw.categories.name AS category,
                        dw.subjects.name AS subject,
                        dw.skills.created_at AS created,
                        dw.subjects.difficulty AS subject_sci
                    FROM dw.skills
                    JOIN dw.employees ON employees.id = skills.tutor_id
                    JOIN dw.users ON dw.employees.user_id = dw.users.id
                    JOIN dw.subjects ON skills.subject_id = subjects.id
                    JOIN dw.categories ON subjects.category_id = categories.id
                    WHERE dw.employees.end_date IS NULL
                      AND dw.skills.created_at >= '{_skills_start}'
                    ORDER BY tutor_name, created
                """
                ar_skills_df = pd.read_sql(_skills_q, _conn_sk)
                _conn_sk.close()
            except Exception:
                ar_skills_df = pd.DataFrame()

            end_90d    = today.strftime("%Y-%m-%d")
            start_1m   = (today - pd.DateOffset(months=1)).strftime("%Y-%m-%d")
            start_6w   = (today - pd.DateOffset(weeks=6)).strftime("%Y-%m-%d")
            start_8w   = (today - pd.DateOffset(weeks=8)).strftime("%Y-%m-%d")
            if pd.notna(hire_date_90d):
                hire_sql = hire_date_90d.strftime("%Y-%m-%d")
                if start_1m < hire_sql: start_1m = hire_sql
                if start_6w < hire_sql: start_6w = hire_sql
                if start_8w < hire_sql: start_8w = hire_sql

            # Helper formatters for 90-day review
            def fmt_pct_90(v, decimals=1):
                if v is None or (isinstance(v, float) and pd.isna(v)): return "—"
                return f"{float(v)*100:.{decimals}f}%"
            def fmt_num_90(v, decimals=1):
                if v is None or (isinstance(v, float) and pd.isna(v)): return "—"
                return f"{float(v):.{decimals}f}"

            # ── Subject Groupings & Raise Calculator ──────────────────────────
            # Each grouping: (display_name, sublists, is_and_logic)
            # AND logic: ALL sublists need at least one match
            # OR logic: at least one subject from any sublist must match
            GROUPINGS_90D = [
                ("AP Chemistry AND Chemistry",
                 [["AP Chemistry"],
                  ["Chemistry", "Chemistry (Honors)", "General Chemistry (College-Level)"]],
                 True),
                ("AP Biology AND Biology",
                 [["AP Biology"],
                  ["Biology", "Biology (Honors)", "Biology (College-Level)"]],
                 True),
                ("AP Physics AND Physics",
                 [["AP Physics 1: Algebra-Based", "AP Physics 2: Algebra-Based",
                   "AP Physics C: Electricity and Magnetism", "AP Physics C: Mechanics"],
                  ["Physics ", "Physics (Honors)", "Physics (College-Level)"]],
                 True),
                ("AP Precalculus AND Pre-Calculus",
                 [["AP Precalculus"],
                  ["Pre-Calculus"]],
                 True),
                ("AP US History AND US History",
                 [["AP United States History"],
                  ["U.S. History", "U.S. History (College-Level)"]],
                 True),
                ("AP Calculus AB AND Calculus",
                 [["AP Calculus AB"],
                  ["Calculus", "Calculus (College-Level)", "Multivariable Calculus",
                   "Integrated Math (with Calculus)", "Integrated Math"]],
                 True),
                ("AP Calculus BC AND Calculus",
                 [["AP Calculus BC"],
                  ["Calculus", "Calculus (College-Level)", "Multivariable Calculus",
                   "Integrated Math (with Calculus)", "Integrated Math"]],
                 True),
                ("AP World History AND World History",
                 [["AP World History: Modern"],
                  ["World History", "World History (College-Level)", "World History (SAT Subject Test)"]],
                 True),
                ("College Essay Writing AND High School English",
                 [["College Essay Writing"],
                  ["High School English", "10th Grade English", "11th Grade English",
                   "12th Grade English", "9th Grade English", "English Language (Honors)",
                   "English Literature (Honors)", "High School Writing"]],
                 True),
                ("Spanish (any)",
                 [["Spanish", "Spanish (Level 2)", "Spanish (Level 3)", "Spanish (College-Level)",
                   "AP Spanish", "AP Spanish Language and Culture", "AP Spanish Literature and Culture",
                   "Elementary Spanish", "Middle School Spanish"]],
                 False),
                ("Middle School Math &/or Middle School English",
                 [["Middle School-Level English", "Middle School-Level Math",
                   "Middle School Writing"]],
                 False),
                ("AP Computer Science &/or AP CS Principles",
                 [["AP Computer Science A", "AP Computer Science Principles"]],
                 False),
                ("Elementary School Math &/or Elementary School English",
                 [["Elementary-Level Math", "Elementary-Level English Language Arts",
                   "Elementary-Level Science"]],
                 False),
                ("At least one EF Foundations Course AND Executive Functioning",
                 [["Executive Function: Foundations (College)",
                   "Executive Function: Foundations (Elementary)",
                   "Executive Function: Foundations (High School)",
                   "Executive Function: Foundations (Middle School)"],
                  ["Executive Functioning", "Elementary Executive Functioning",
                   "Middle School Executive Functioning"]],
                 True),
                ("SSAT (any level)",
                 [["SSAT", "SSAT: Elementary Level (Grades 3-4)",
                   "SSAT: Middle Level (Grades 5-7)", "SSAT: Upper Level (Grades 8-11)"]],
                 False),
            ]

            RAISE_TABLE = {
                # (review_result, grouping_bucket) -> rate
                ("Meeting Expectations",   "0-3"):  "No raise",
                ("Meeting Expectations",   "4-6"):  "$26/hr",
                ("Meeting Expectations",   "7-15"): "$27/hr",
                ("Exceeding Expectations", "0-3"):  "$27/hr",
                ("Exceeding Expectations", "4-6"):  "$28/hr",
                ("Exceeding Expectations", "7-15"): "$29/hr",
                ("Not Meeting Expectations", "0-3"):  "Not eligible for raise",
                ("Not Meeting Expectations", "4-6"):  "Not eligible for raise",
                ("Not Meeting Expectations", "7-15"): "Not eligible for raise",
            }

            def _grouping_bucket(n):
                if n <= 3: return "0-3"
                if n <= 6: return "4-6"
                return "7-15"

            with st.expander("📊 Subject Groupings & Raise Calculator", expanded=True):
                tutor_subjects = set(ar_skills_df[ar_skills_df["tutor_name"] == nr90_tutor]["subject"].str.strip().tolist()) if not ar_skills_df.empty else set()

                completed_groups = []
                missing_groups   = []
                for grp_name, grp_sublists, is_and in GROUPINGS_90D:
                    if is_and:
                        # ALL sublists must have at least one match
                        completed = all(
                            any(s.strip() in tutor_subjects for s in sublist)
                            for sublist in grp_sublists
                        )
                    else:
                        # At least one subject from any sublist must match
                        completed = any(
                            any(s.strip() in tutor_subjects for s in sublist)
                            for sublist in grp_sublists
                        )
                    if completed:
                        completed_groups.append(grp_name)
                    else:
                        missing_groups.append(grp_name)

                n_groups = len(completed_groups)
                bucket   = _grouping_bucket(n_groups)

                cg1, cg2, cg3 = st.columns(3)
                cg1.metric("Groupings Completed", n_groups)
                cg2.metric("Grouping Bucket", bucket)
                cg3.metric("Total Possible", len(GROUPINGS_90D))

                st.markdown("**Review outcome → raise rate:**")
                rev_result = st.selectbox("Select Review Result",
                    ["— Select —", "Meeting Expectations", "Exceeding Expectations", "Not Meeting Expectations"],
                    key="90d_review_result")
                if rev_result != "— Select —":
                    rate = RAISE_TABLE.get((rev_result, bucket), "—")
                    color = "#2e7d32" if "$" in rate else "#c62828"
                    st.markdown(f"<h3 style='color:{color}'>Raise Rate: {rate}</h3>", unsafe_allow_html=True)

                if completed_groups:
                    st.markdown("**✅ Completed Groupings:**")
                    for g in completed_groups:
                        st.markdown(f"- {g}")
                if missing_groups:
                    st.markdown("**❌ Incomplete Groupings:**")
                    for g in missing_groups:
                        st.markdown(f"- {g}")

            st.divider()
            with st.spinner("Loading 90-day data..."):
                try:
                    d_1m_90 = load_ar_kpi(start_1m, end_90d)
                    d_6w_90 = load_ar_kpi(start_6w, end_90d)
                    d_8w_90 = load_ar_kpi(start_8w, end_90d)
                except Exception as _e:
                    st.error(f"Error loading data: {_e}")
                    d_1m_90 = d_6w_90 = d_8w_90 = pd.DataFrame()

            if d_1m_90.empty:
                st.warning("No data found for this tutor.")
            else:
                def _get_row_90(df, tutor):
                    r = df[df["tutor_name"] == tutor]
                    return r.iloc[0] if not r.empty else None

                def _safe_90(r, col):
                    if r is None: return None
                    v = r.get(col)
                    return None if pd.isna(v) else float(v)

                def fmt_pct_90(v, decimals=1):
                    if v is None or (isinstance(v, float) and pd.isna(v)): return "—"
                    return f"{float(v)*100:.{decimals}f}%"

                def fmt_num_90(v, decimals=1):
                    if v is None or (isinstance(v, float) and pd.isna(v)): return "—"
                    return f"{float(v):.{decimals}f}"

                def _fmt_prep_90(v):
                    return f"{v*100:.1f}%" if v is not None else "—"

                def _card_90(title, content_fn):
                    with st.expander(f"**{title}**", expanded=True):
                        content_fn()

                def _delta_90(v, compare, higher_is_better=True, label="vs comparison"):
                    if v is None or compare is None or pd.isna(v) or pd.isna(compare): return None, "off"
                    diff = float(v) - float(compare)
                    color = "normal" if (diff > 0) == higher_is_better else "inverse"
                    arrow = "▲" if diff > 0 else "▼"
                    return f"{arrow} {abs(diff)*100:.1f}pp {label}", color

                def _delta_num_90(v, compare, higher_is_better=True, label="vs comparison"):
                    if v is None or compare is None or pd.isna(v) or pd.isna(compare): return None, "off"
                    diff = float(v) - float(compare)
                    color = "normal" if (diff > 0) == higher_is_better else "inverse"
                    arrow = "▲" if diff > 0 else "▼"
                    return f"{arrow} {abs(diff):.1f} {label}", color

                def _delta_target_90(v, target, higher_is_better=True):
                    if v is None or pd.isna(v): return None, "off"
                    diff = float(v) - target
                    color = "normal" if (diff > 0) == higher_is_better else "inverse"
                    arrow = "▲" if diff > 0 else "▼"
                    return f"{arrow} {abs(diff)*100:.1f}pp vs {target*100:.0f}% target", color

                def _peer_avg_90(df, col):
                    if df.empty or col not in df.columns: return None
                    vals = pd.to_numeric(df[col], errors="coerce").dropna()
                    return vals.mean() if not vals.empty else None

                r1m = _get_row_90(d_1m_90, nr90_tutor)
                r6w = _get_row_90(d_6w_90, nr90_tutor)
                r8w = _get_row_90(d_8w_90, nr90_tutor)

                # Peers: tutors hired within 150 days (same new-hire cohort)
                tutor_tier_90 = r1m.get("tier") if r1m is not None else None

                # Get hire dates from master_tutor_df to find cohort peers
                new_hire_names = team_tutors_df[team_tutors_df["days_since_hire"] <= 150]["Full Name"].tolist()
                peers_90 = d_1m_90[
                    (d_1m_90["tutor_name"].isin(new_hire_names)) &
                    (d_1m_90["tutor_name"] != nr90_tutor)
                ] if new_hire_names else pd.DataFrame()

                st.divider()
                st.caption(f"📅 Last Month: {start_1m} to {end_90d} | Last 6 Wks: {start_6w} to {end_90d} | Last 8 Wks: {start_8w} to {end_90d}")
                if tutor_tier_90:
                    st.caption(f"Tier: **{tutor_tier_90}** | New-hire peers (within 150 days): **{len(peers_90)}**")

                # ── 1. Sessions on Time
                def _90s1():
                    v1=_safe_90(r1m,"sessions_on_time_pct"); v6=_safe_90(r6w,"sessions_on_time_pct"); v8=_safe_90(r8w,"sessions_on_time_pct")
                    d1,dc1=_delta_target_90(v1,0.90); d6,dc6=_delta_target_90(v6,0.90); d8,dc8=_delta_target_90(v8,0.90)
                    c1,c2,c3=st.columns(3)
                    c1.metric("Last Month",fmt_pct_90(v1),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",fmt_pct_90(v6),delta=d6,delta_color=dc6)
                    c3.metric("Last 8 Wks",fmt_pct_90(v8),delta=d8,delta_color=dc8)
                    st.caption("Target: 90%+")
                _card_90("1. Sessions Launched on Time", _90s1)

                # ── 2. Parent Updates on Time
                def _90s2():
                    v1=_safe_90(r1m,"parent_update_pct"); v6=_safe_90(r6w,"parent_update_pct"); v8=_safe_90(r8w,"parent_update_pct")
                    d1,dc1=_delta_target_90(v1,0.90); d6,dc6=_delta_target_90(v6,0.90); d8,dc8=_delta_target_90(v8,0.90)
                    c1,c2,c3=st.columns(3)
                    c1.metric("Last Month",fmt_pct_90(v1),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",fmt_pct_90(v6),delta=d6,delta_color=dc6)
                    c3.metric("Last 8 Wks",fmt_pct_90(v8),delta=d8,delta_color=dc8)
                    st.caption("Target: 90%+")
                _card_90("2. Parent Updates Sent on Time", _90s2)

                # ── 3. Prep Time
                def _90s3():
                    v1=_safe_90(r1m,"prep_time_ratio"); v6=_safe_90(r6w,"prep_time_ratio"); v8=_safe_90(r8w,"prep_time_ratio")
                    p1=_peer_avg_90(peers_90,"prep_time_ratio")
                    d1,dc1=_delta_90(v1,p1,higher_is_better=False,label="vs peer avg")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("Last Month",_fmt_prep_90(v1),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",_fmt_prep_90(v6))
                    c3.metric("Last 8 Wks",_fmt_prep_90(v8))
                    c4.metric("Peer Avg (1M)",_fmt_prep_90(p1))
                    st.caption("Prep hrs as % of attended hrs. Lower is better.")
                _card_90("3. Prep Time Percentage", _90s3)

                # ── 4. Cancellations
                def _90s4():
                    cancel_df = load_cancellation_data()
                    if not cancel_df.empty:
                        row = cancel_df[cancel_df["Tutor Name"].str.strip() == nr90_tutor]
                        count = int(row["Count of Days Cancelled"].iloc[0]) if not row.empty else 0
                        all_avg = round(cancel_df["Count of Days Cancelled"].mean(), 1)
                        d,dc = _delta_num_90(count, all_avg, higher_is_better=False, label="vs all-tutor avg")
                        c1,c2=st.columns(2)
                        c1.metric("Days Cancelled", count, delta=d, delta_color=dc)
                        c2.metric("All-Tutor Avg", fmt_num_90(all_avg,1))
                        st.caption("Lower is better.")
                    else:
                        st.info("No cancellation data available.")
                    st.warning("⚠️ Data is manually updated and may be stale. Verify before using in a review.")
                _card_90("4. Cancellations", _90s4)

                # ── 5. Exams Data
                def _90s5():
                    if ar_exam_snap.empty:
                        st.info("No exam snapshot data available yet.")
                        return
                    tutor_es = ar_exam_snap[ar_exam_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_es.empty:
                        st.info(f"No exam data found for {nr90_tutor}.")
                        return
                    all_latest = ar_exam_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
                    all_avg_pct = all_latest["pct_eligible_with_exam"].mean() if "pct_eligible_with_exam" in all_latest.columns else None
                    latest = tutor_es.iloc[-1]
                    prev = tutor_es.iloc[-2] if len(tutor_es) >= 2 else None
                    lat_pct = latest["pct_eligible_with_exam"]
                    prev_pct = prev["pct_eligible_with_exam"] if prev is not None else None
                    d_prev,dc_prev = _delta_num_90(lat_pct,prev_pct,higher_is_better=True,label="vs prev week") if prev_pct is not None else (None,"off")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("% With Exam (now)", f"{lat_pct:.1f}%", delta=d_prev, delta_color=dc_prev)
                    c2.metric("# No Exam (now)", fmt_num_90(latest["students_no_exam"],0))
                    c3.metric("All-Tutor Avg %", f"{all_avg_pct:.1f}%" if all_avg_pct else "—")
                    c4.metric("# Stale Exam (now)", fmt_num_90(latest.get("students_stale_exam"),0))
                _card_90("5. Exams Data", _90s5)

                # ── 6. Grades Data
                def _90s6():
                    if ar_grades_snap.empty:
                        st.info("No grades snapshot data available yet.")
                        return
                    tutor_gs = ar_grades_snap[ar_grades_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_gs.empty:
                        st.info(f"No grades data found for {nr90_tutor}.")
                        return
                    all_latest = ar_grades_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
                    all_avg = all_latest["pct_subjects_graded"].mean() if "pct_subjects_graded" in all_latest.columns else None
                    latest = tutor_gs.iloc[-1]
                    prev = tutor_gs.iloc[-2] if len(tutor_gs) >= 2 else None
                    lat_pct = latest["pct_subjects_graded"]
                    prev_pct = prev["pct_subjects_graded"] if prev is not None else None
                    d_prev,dc_prev = _delta_num_90(lat_pct,prev_pct,higher_is_better=True,label="vs prev week") if prev_pct is not None else (None,"off")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("% Graded (now)", f"{lat_pct:.1f}%", delta=d_prev, delta_color=dc_prev)
                    c2.metric("# No Grades (now)", fmt_num_90(latest["students_no_grades"],0))
                    c3.metric("All-Tutor Avg %", f"{all_avg:.1f}%" if all_avg else "—")
                    c4.metric("Stale Grades (now)", fmt_num_90(latest["stale_grade_students"],0))
                _card_90("6. Grades Data", _90s6)

                # ── 7. Rematches
                def _90s7():
                    rematch_df = load_rematch_tracker()
                    if rematch_df.empty:
                        st.info("No rematch data available.")
                        return
                    cutoff_start = pd.to_datetime(start_8w)
                    cutoff_end   = pd.to_datetime(end_90d)
                    tutor_rm = rematch_df[
                        (rematch_df["Former Tutor"].str.strip() == nr90_tutor) &
                        (rematch_df["Rematch Date Parsed"] >= cutoff_start) &
                        (rematch_df["Rematch Date Parsed"] <= cutoff_end)
                    ].copy()
                    count = len(tutor_rm)
                    st.metric("Rematches (last 8 wks)", count)
                    if count > 0:
                        display = tutor_rm[["Rematch Date Parsed","Student Name","Reason for Rematch Request","Does the Rematch Seem Valid?"]].copy()
                        display["Rematch Date Parsed"] = display["Rematch Date Parsed"].dt.strftime("%m/%d/%Y")
                        display = display.rename(columns={"Rematch Date Parsed":"Date","Student Name":"Student","Reason for Rematch Request":"Reason","Does the Rematch Seem Valid?":"Valid?"})
                        st.dataframe(display, use_container_width=True, hide_index=True)
                    st.warning("⚠️ Data is manually updated and may be stale. Verify before using in a review.")
                _card_90("7. Rematches", _90s7)

                # ── 8. Weighted Repurchase
                def _90s8():
                    wr_col = "Weighted Repurchases"
                    tutor_mm = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tutor Name"] == nr90_tutor].copy()
                    if tutor_mm.empty or wr_col not in tutor_mm.columns:
                        st.info("No monthly metric data found.")
                        return
                    def _pe(s):
                        if pd.isna(s): return pd.NaT
                        s2 = str(s).replace("-","to").replace("–","to")
                        parts = s2.split("to")
                        end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                        return pd.to_datetime(end, errors="coerce", dayfirst=False)
                    tutor_mm["Date Parsed"] = tutor_mm["Date Range"].apply(_pe)
                    tutor_mm = tutor_mm.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
                    wr_total = pd.to_numeric(tutor_mm[wr_col], errors="coerce").sum()
                    st.metric("Total Repurchases (all data)", fmt_num_90(wr_total,1))
                    st.caption("Limited history for new tutors — showing all available data.")
                _card_90("8. Weighted Repurchase", _90s8)

                # ── 9. Archivable Students
                def _90s9():
                    if ar_arch_snap.empty:
                        st.info("No archivable snapshot data available yet.")
                        return
                    tutor_as = ar_arch_snap[ar_arch_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_as.empty:
                        st.info(f"No archivable data found for {nr90_tutor}.")
                        return
                    all_latest = ar_arch_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
                    all_avg = all_latest["pct_archivable"].mean() if "pct_archivable" in all_latest.columns else None
                    latest = tutor_as.iloc[-1]
                    prev = tutor_as.iloc[-2] if len(tutor_as) >= 2 else None
                    lat_pct = latest["pct_archivable"] if "pct_archivable" in latest else None
                    prev_pct = prev["pct_archivable"] if prev is not None and "pct_archivable" in prev else None
                    d_prev,dc_prev = _delta_num_90(lat_pct,prev_pct,higher_is_better=False,label="vs prev week") if prev_pct is not None else (None,"off")
                    c1,c2,c3=st.columns(3)
                    c1.metric("# Archivable (now)", fmt_num_90(latest["archivable_students"],0))
                    c2.metric("% Archivable (now)", f"{lat_pct:.1f}%" if lat_pct else "—", delta=d_prev, delta_color=dc_prev)
                    c3.metric("All-Tutor Avg %", f"{all_avg:.1f}%" if all_avg else "—")
                    st.caption("Lower is better.")
                _card_90("9. Archivable Students", _90s9)

                # ── 10. Unscheduled Hours
                def _90s10():
                    if ar_arch_snap.empty:
                        st.info("No unscheduled hours data available yet.")
                        return
                    tutor_as = ar_arch_snap[ar_arch_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_as.empty:
                        st.info(f"No data found for {nr90_tutor}.")
                        return
                    all_latest = ar_arch_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
                    all_avg = all_latest["unscheduled_hours"].mean()
                    latest = tutor_as.iloc[-1]
                    prev = tutor_as.iloc[-2] if len(tutor_as) >= 2 else None
                    lat_hrs = latest["unscheduled_hours"]
                    prev_hrs = prev["unscheduled_hours"] if prev is not None else None
                    d_prev,dc_prev = _delta_num_90(lat_hrs,prev_hrs,higher_is_better=False,label="vs prev week") if prev_hrs is not None else (None,"off")
                    c1,c2,c3=st.columns(3)
                    c1.metric("Unscheduled Hrs (now)", fmt_num_90(lat_hrs,1), delta=d_prev, delta_color=dc_prev)
                    c2.metric("All-Tutor Avg (hrs)", fmt_num_90(all_avg,1))
                    c3.metric("Total Students", fmt_num_90(latest["total_students"],0))
                    st.caption("Lower is better.")
                _card_90("10. Unscheduled Hours", _90s10)

                # ── 11. Auto-Attendance
                def _90s11():
                    v1=_safe_90(r1m,"autoattendance_sessions"); v6=_safe_90(r6w,"autoattendance_sessions"); v8=_safe_90(r8w,"autoattendance_sessions")
                    p1=_peer_avg_90(d_1m_90,"autoattendance_sessions")
                    d1,dc1=_delta_num_90(v1,p1,higher_is_better=False,label="vs all-tutor avg")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("Last Month",fmt_num_90(v1,0),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",fmt_num_90(v6,0))
                    c3.metric("Last 8 Wks",fmt_num_90(v8,0))
                    c4.metric("All-Tutor Avg (1M)",fmt_num_90(p1,1))
                    st.caption("Lower is better.")
                _card_90("11. Auto-Attendance", _90s11)

                # ── 12. NPS
                def _90s12():
                    v1=_safe_90(r1m,"avg_nps"); v6=_safe_90(r6w,"avg_nps"); v8=_safe_90(r8w,"avg_nps")
                    c1,c2,c3=st.columns(3)
                    c1.metric("Avg NPS (1M)",fmt_num_90(v1,2))
                    c2.metric("Avg NPS (6W)",fmt_num_90(v6,2))
                    c3.metric("Avg NPS (8W)",fmt_num_90(v8,2))
                    c1b,c2b,c3b=st.columns(3)
                    c1b.metric("# Responses (1M)",fmt_num_90(_safe_90(r1m,"number_of_nps"),0))
                    c2b.metric("# Responses (6W)",fmt_num_90(_safe_90(r6w,"number_of_nps"),0))
                    c3b.metric("# Responses (8W)",fmt_num_90(_safe_90(r8w,"number_of_nps"),0))
                _card_90("12. NPS", _90s12)

                # ── 13. Parent Update Video
                def _90s13():
                    if ar_video_snap.empty:
                        st.info("No video snapshot data available yet.")
                        return
                    tutor_vs = ar_video_snap[ar_video_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_vs.empty:
                        st.info(f"No video data found for {nr90_tutor}.")
                        return
                    all_avg = ar_video_snap.groupby("tutor_name")["pct_with_video"].last().mean()
                    latest = tutor_vs.iloc[-1]
                    tutor_avg = tutor_vs["pct_with_video"].mean()
                    d,dc = _delta_num_90(tutor_avg,all_avg,higher_is_better=True,label="vs all-tutor avg")
                    c1,c2,c3=st.columns(3)
                    c1.metric("Most Recent Week %", f"{latest['pct_with_video']:.1f}%")
                    c2.metric("Avg All Weeks", f"{tutor_avg:.1f}%", delta=d, delta_color=dc)
                    c3.metric("All-Tutor Avg", f"{all_avg:.1f}%")
                    if len(tutor_vs) >= 2:
                        fig_v = px.line(tutor_vs, x="week_date", y="pct_with_video", markers=True,
                                        title="% With Video", color_discrete_sequence=["#7b2d8b"])
                        fig_v.add_hline(y=80, line_dash="dash", line_color="#cc0000", annotation_text="80%")
                        fig_v.update_layout(height=220, margin=dict(l=20,r=20,t=40,b=20), xaxis_title="", yaxis_title="%")
                        st.plotly_chart(fig_v, use_container_width=True)
                _card_90("13. Parent Update Video", _90s13)

                # ── 14. Parent Update Homework
                def _90s14():
                    if ar_video_snap.empty or "pct_with_homework" not in ar_video_snap.columns:
                        st.info("No homework data available yet.")
                        return
                    tutor_vs = ar_video_snap[ar_video_snap["tutor_name"] == nr90_tutor].sort_values("week_date")
                    if tutor_vs.empty:
                        st.info(f"No data found for {nr90_tutor}.")
                        return
                    all_avg = ar_video_snap.groupby("tutor_name")["pct_with_homework"].last().mean()
                    tutor_avg = tutor_vs["pct_with_homework"].mean()
                    latest = tutor_vs.iloc[-1]
                    d,dc = _delta_num_90(tutor_avg,all_avg,higher_is_better=True,label="vs all-tutor avg")
                    c1,c2,c3=st.columns(3)
                    c1.metric("Most Recent Week %", f"{latest['pct_with_homework']:.1f}%")
                    c2.metric("Avg All Weeks", f"{tutor_avg:.1f}%", delta=d, delta_color=dc)
                    c3.metric("All-Tutor Avg", f"{all_avg:.1f}%")
                    if len(tutor_vs) >= 2:
                        fig_h = px.line(tutor_vs, x="week_date", y="pct_with_homework", markers=True,
                                        title="% Mentioning Homework", color_discrete_sequence=["#1565c0"])
                        fig_h.update_layout(height=220, margin=dict(l=20,r=20,t=40,b=20), xaxis_title="", yaxis_title="%")
                        st.plotly_chart(fig_h, use_container_width=True)
                _card_90("14. Parent Update Mentioning Homework", _90s14)

                # ── 15. Progress Updates
                def _90s15():
                    pu_col = "% of Active Students with Progress Updates Completed in last 2 months"
                    tutor_mm = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tutor Name"] == nr90_tutor].copy()
                    if tutor_mm.empty or pu_col not in tutor_mm.columns:
                        st.info("No progress update data found.")
                        return
                    def _pe(s):
                        if pd.isna(s): return pd.NaT
                        s2 = str(s).replace("-","to").replace("–","to")
                        parts = s2.split("to")
                        end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                        return pd.to_datetime(end, errors="coerce", dayfirst=False)
                    tutor_mm["Date Parsed"] = tutor_mm["Date Range"].apply(_pe)
                    tutor_mm = tutor_mm.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
                    tutor_mm[pu_col] = pd.to_numeric(tutor_mm[pu_col], errors="coerce")
                    pu_avg = tutor_mm[pu_col].mean()
                    st.metric("Avg % Progress Updates (all data)", fmt_pct_90(pu_avg))
                    st.caption("Limited history for new tutors — showing all available data.")
                _card_90("15. Progress Updates", _90s15)

                # ── 16. Current SCI
                def _90s16():
                    sci = _safe_90(r1m,"current_sci")
                    p_sci = _peer_avg_90(peers_90,"current_sci")
                    d,dc = _delta_num_90(sci,p_sci,higher_is_better=True,label="vs peer avg")
                    c1,c2=st.columns(2)
                    c1.metric("Current SCI", fmt_num_90(sci,1), delta=d, delta_color=dc)
                    c2.metric("Peer Avg (same tier)", fmt_num_90(p_sci,1))
                    st.caption("Point-in-time value.")
                _card_90("16. Current SCI", _90s16)

                # ── 17. SCI Growth
                def _90s17():
                    tutor_skills = ar_skills_df[ar_skills_df["tutor_name"] == nr90_tutor].copy() if not ar_skills_df.empty else pd.DataFrame()
                    if tutor_skills.empty:
                        st.info("No new subjects added during review period.")
                        return
                    tutor_skills["created"] = pd.to_datetime(tutor_skills["created"], errors="coerce")
                    tutor_skills = tutor_skills.sort_values("created")
                    sci_gained = tutor_skills["subject_sci"].sum() if "subject_sci" in tutor_skills.columns else None
                    c1,c2,c3=st.columns(3)
                    c1.metric("Subjects Added", len(tutor_skills))
                    c2.metric("SCI Points Gained", fmt_num_90(sci_gained,1) if sci_gained else "—")
                    c3.metric("Current SCI", fmt_num_90(_safe_90(r1m,"current_sci"),1))
                    display_skills = tutor_skills[["created","category","subject","subject_sci"]].copy()
                    display_skills["created"] = display_skills["created"].dt.strftime("%Y-%m-%d")
                    display_skills = display_skills.rename(columns={"created":"Date Added","category":"Category","subject":"Subject","subject_sci":"SCI Value"})
                    st.dataframe(display_skills, use_container_width=True, hide_index=True)
                _card_90("17. SCI Growth", _90s17)

                # ── 18. Availability %
                def _90s18():
                    v1=_safe_90(r1m,"availability_pct"); v6=_safe_90(r6w,"availability_pct"); v8=_safe_90(r8w,"availability_pct")
                    c1,c2,c3=st.columns(3)
                    c1.metric("Last Month",fmt_pct_90(v1))
                    c2.metric("Last 6 Wks",fmt_pct_90(v6))
                    c3.metric("Last 8 Wks",fmt_pct_90(v8))
                _card_90("18. Availability Percentage", _90s18)

                # ── 19. Delivery %
                def _90s19():
                    v1=_safe_90(r1m,"delivery_pct"); v6=_safe_90(r6w,"delivery_pct"); v8=_safe_90(r8w,"delivery_pct")
                    p1=_peer_avg_90(peers_90,"delivery_pct")
                    d1,dc1=_delta_90(v1,p1,higher_is_better=True,label="vs peer avg")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("Last Month",fmt_pct_90(v1),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",fmt_pct_90(v6))
                    c3.metric("Last 8 Wks",fmt_pct_90(v8))
                    c4.metric("Peer Avg (1M)",fmt_pct_90(p1))
                _card_90("19. Delivery Percentage", _90s19)

                # ── 20. Weeks at Target
                def _90s20():
                    v1=_safe_90(r1m,"weeks_at_target"); v6=_safe_90(r6w,"weeks_at_target"); v8=_safe_90(r8w,"weeks_at_target")
                    p1=_peer_avg_90(peers_90,"weeks_at_target")
                    d1,dc1=_delta_num_90(v1,p1,higher_is_better=True,label="vs peer avg")
                    c1,c2,c3,c4=st.columns(4)
                    c1.metric("Last Month",fmt_num_90(v1,0),delta=d1,delta_color=dc1)
                    c2.metric("Last 6 Wks",fmt_num_90(v6,0))
                    c3.metric("Last 8 Wks",fmt_num_90(v8,0))
                    c4.metric("Peer Avg (1M)",fmt_num_90(p1,1))
                _card_90("20. Weeks Meeting Delivery Target", _90s20)

    if page == "📋 Annual Reviews":
        st.markdown('<div class="main-title">📋 Annual Reviews</div>', unsafe_allow_html=True)


        # ── Date ranges (hardcoded) ──────────────────────────────────────────
        AR_12M_START = "2025-04-27"
        AR_12M_END   = "2026-04-26"
        AR_3M_START  = "2026-02-01"
        AR_3M_END    = "2026-04-26"

        st.caption(f"📅 12-month period: **{AR_12M_START}** to **{AR_12M_END}** | 3-month period: **{AR_3M_START}** to **{AR_3M_END}**")

        # ── Tutor selector ───────────────────────────────────────────────────
        ar_tutor = st.selectbox("Select Tutor", ["— Select —"] + sorted(annelies_tutors), key="ar_tutor_select")
        if ar_tutor == "— Select —":
            st.info("Select a tutor above to load their annual review.")
            st.stop()

        st.divider()

        # ── Load KPI data from Redshift ──────────────────────────────────────
        @st.cache_data(ttl=3600)
        def load_ar_from_github(filename):
            """Load AR data from GitHub CSV (synced nightly)."""
            token = st.secrets["github"]["token"]
            repo  = st.secrets["github"]["repo"]
            url   = f"https://raw.githubusercontent.com/{repo}/main/data/{filename}"
            import requests, io
            resp = requests.get(url, headers={"Authorization": f"token {token}"})
            if resp.status_code == 200:
                return pd.read_csv(io.StringIO(resp.text))
            return pd.DataFrame()

        # load_ar_kpi defined at module level

        if "ar_data_loaded" not in st.session_state:
            st.session_state["ar_data_loaded"] = False

        # Try loading from GitHub CSV first (fast), fall back to live Redshift
        if not st.session_state["ar_data_loaded"]:
            _gh_12m = load_ar_from_github("ar_data_12m.csv")
            _gh_3m  = load_ar_from_github("ar_data_3m.csv")
            if not _gh_12m.empty and not _gh_3m.empty:
                st.session_state["ar_12m"] = _gh_12m
                st.session_state["ar_3m"]  = _gh_3m
                st.session_state["ar_data_loaded"] = True
                st.session_state["ar_source"] = "github"

        if not st.session_state["ar_data_loaded"]:
            st.info("📊 Pre-cached data not available yet. Click below to load live from Redshift (may take 30-60s).")
            if st.button("📥 Load Annual Review Data", key="ar_load_btn"):
                with st.spinner("Loading annual review data..."):
                    try:
                        st.session_state["ar_12m"] = load_ar_kpi(AR_12M_START, AR_12M_END)
                        st.session_state["ar_3m"]  = load_ar_kpi(AR_3M_START,  AR_3M_END)
                        st.session_state["ar_data_loaded"] = True
                        st.session_state["ar_source"] = "live"
                        st.rerun()
                    except Exception as e:
                        st.error(f"Could not load annual review data: {e}")
                        st.stop()
            else:
                st.stop()

        ar_12m = st.session_state["ar_12m"]
        ar_3m  = st.session_state["ar_3m"]
        ar_source = st.session_state.get("ar_source","unknown")

        @st.cache_data(ttl=3600)
        def load_ar_skills():
            import sys as _sys
            _force = getattr(_sys.modules[__name__], 'FORCE_CACHE_MODE', False)
            if _force:
                df = _gh_read_cache("data/cache/ar_skills.csv")
                if not df.empty and "_cached_at" in df.columns:
                    df = df.drop(columns=["_cached_at"])
                return df
            conn = get_redshift_connection()
            query = """
                SELECT
                    dw.employees.id AS emp_id,
                    dw.users.first_name||' '||dw.users.last_name AS tutor_name,
                    dw.categories.name AS category,
                    dw.subjects.name AS subject,
                    dw.skills.created_at AS created,
                    dw.subjects.difficulty AS subject_sci
                FROM dw.skills
                JOIN dw.employees ON employees.id = skills.tutor_id
                JOIN dw.users ON dw.employees.user_id = dw.users.id
                JOIN dw.subjects ON skills.subject_id = subjects.id
                JOIN dw.categories ON subjects.category_id = categories.id
                WHERE dw.employees.end_date IS NULL
                  AND dw.employees.delivery_target > 0
                  AND dw.skills.created_at >= '2025-05-01'
                ORDER BY tutor_name, created
            """
            try:
                df = pd.read_sql(query, conn)
            finally:
                conn.close()
            return df

        try:
            ar_skills_df = load_ar_skills()
        except Exception as e:
            ar_skills_df = _gh_read_cache("data/cache/ar_skills.csv")
            if not ar_skills_df.empty:
                st.warning("⚠️ Redshift unavailable — using cached skills data.")

        # Load video snapshots for AR cards 13 & 14
        try:
            ar_video_snap = load_video_snapshots()
            if not ar_video_snap.empty and "week_date" in ar_video_snap.columns:
                ar_video_snap["week_date"] = pd.to_datetime(ar_video_snap["week_date"])
        except Exception as e:
            ar_video_snap = pd.DataFrame()

        # Load exam + grades snapshots for AR cards 5 & 6
        try:
            ar_exam_snap = load_exams_snapshots()
            if not ar_exam_snap.empty and "week_date" in ar_exam_snap.columns:
                ar_exam_snap["week_date"] = pd.to_datetime(ar_exam_snap["week_date"])
        except Exception as e:
            ar_exam_snap = pd.DataFrame()

        try:
            ar_grades_snap = load_grades_snapshots()
            if not ar_grades_snap.empty and "week_date" in ar_grades_snap.columns:
                ar_grades_snap["week_date"] = pd.to_datetime(ar_grades_snap["week_date"])
        except Exception as e:
            ar_grades_snap = pd.DataFrame()

        # Load archive snapshots for AR cards 9 & 10 (use full history if available)
        try:
            ar_arch_snap = gh_read(SNAPSHOT_HISTORY_FILE)
            if ar_arch_snap.empty:
                ar_arch_snap = load_snapshots()  # fallback to 2-week rolling
            if not ar_arch_snap.empty and "week_date" in ar_arch_snap.columns:
                ar_arch_snap["week_date"] = pd.to_datetime(ar_arch_snap["week_date"])
                if "archivable_students" in ar_arch_snap.columns and "total_students" in ar_arch_snap.columns:
                    ar_arch_snap["pct_archivable"] = ar_arch_snap.apply(
                        lambda r: round(r["archivable_students"] / r["total_students"] * 100, 1)
                        if r["total_students"] > 0 else 0, axis=1)
        except Exception as e:
            ar_arch_snap = pd.DataFrame()
        if ar_source == "github":
            st.caption("📦 Data loaded from nightly cache.")
        else:
            st.caption("🔴 Data loaded live from Redshift.")

        # Get tutor rows
        t12 = ar_12m[ar_12m["tutor_name"] == ar_tutor]
        t3  = ar_3m[ar_3m["tutor_name"]   == ar_tutor]
        if t12.empty:
            st.warning(f"No annual review data found for {ar_tutor}.")
            st.stop()

        t12r = t12.iloc[0]
        t3r  = t3.iloc[0] if not t3.empty else None

        # Peer group: same tier + delivery target
        tier_val   = t12r["tier"]
        del_target = t12r["delivery_target"]
        peers_12m  = ar_12m[(ar_12m["tier"] == tier_val) & (ar_12m["delivery_target"] == del_target)]
        peers_3m   = ar_3m[(ar_3m["tier"]   == tier_val) & (ar_3m["delivery_target"]  == del_target)]
        all_12m    = ar_12m.copy()

        def fmt_pct(v, decimals=1):
            if v is None or pd.isna(v): return "—"
            return f"{float(v)*100:.{decimals}f}%"

        def fmt_num(v, decimals=1):
            if v is None or pd.isna(v): return "—"
            return f"{float(v):.{decimals}f}"

        def peer_avg(df, col):
            if col not in df.columns or df[col].dropna().empty: return None
            return df[col].dropna().mean()

        def ar_card(number, title, content_fn, coming_soon=False):
            """Render a metric card with consistent styling."""
            with st.container(border=True):
                st.markdown(f"**{number}. {title}**")
                if coming_soon:
                    st.caption("🚧 Coming soon")
                else:
                    content_fn()

        def ar_delta(tutor_val, compare_val, higher_is_better=True):
            """Return streamlit delta string and color for metric."""
            if tutor_val is None or pd.isna(tutor_val): return None, "off"
            if compare_val is None or pd.isna(compare_val): return None, "off"
            diff = float(tutor_val) - float(compare_val)
            if diff == 0: return "= peer avg", "off"
            label = f"{'▲' if diff > 0 else '▼'} {abs(diff)*100:.1f}pp vs peer"
            color = "normal" if (diff > 0) == higher_is_better else "inverse"
            return label, color

        def ar_delta_target(tutor_val, target, higher_is_better=True, label="vs target"):
            """Return delta vs a fixed target."""
            if tutor_val is None or pd.isna(tutor_val): return None, "off"
            diff = float(tutor_val) - float(target)
            if diff == 0: return f"= {label}", "off"
            arrow = "▲" if diff > 0 else "▼"
            color = "normal" if (diff > 0) == higher_is_better else "inverse"
            return f"{arrow} {abs(diff)*100:.1f}pp {label}", color

        def ar_delta_num(tutor_val, compare_val, higher_is_better=True, label="vs peer"):
            """Return delta for non-percentage numeric values."""
            if tutor_val is None or pd.isna(tutor_val): return None, "off"
            if compare_val is None or pd.isna(compare_val): return None, "off"
            diff = float(tutor_val) - float(compare_val)
            if diff == 0: return f"= {label}", "off"
            arrow = "▲" if diff > 0 else "▼"
            color = "normal" if (diff > 0) == higher_is_better else "inverse"
            return f"{arrow} {abs(diff):.1f} {label}", color

        # ── Header ──────────────────────────────────────────────────────────
        st.markdown(f"### {ar_tutor} — Annual Review")
        st.caption(f"Tier: **{tier_val}** | Delivery Target: **{del_target}h/wk** | Peer group size (same tier+target): **{len(peers_12m)}** tutors")
        st.divider()

        # ── 1. Sessions Launched on Time ─────────────────────────────────────
        def _s1():
            v12 = t12r.get("sessions_on_time_pct")
            v3  = t3r.get("sessions_on_time_pct") if t3r is not None else None
            d12, dc12 = ar_delta_target(v12, 0.90, higher_is_better=True, label="vs 90% target")
            d3,  dc3  = ar_delta_target(v3,  0.90, higher_is_better=True, label="vs 90% target")
            c1, c2 = st.columns(2)
            c1.metric("12-Month", fmt_pct(v12), delta=d12, delta_color=dc12)
            c2.metric("3-Month",  fmt_pct(v3),  delta=d3,  delta_color=dc3)
            st.caption("Target: 90%+. Not compared to peers.")
        ar_card(1, "Sessions Launched on Time", _s1)

        # ── 2. Parent Updates Sent on Time ───────────────────────────────────
        def _s2():
            v12 = t12r.get("parent_update_pct")
            v3  = t3r.get("parent_update_pct") if t3r is not None else None
            d12, dc12 = ar_delta_target(v12, 0.90, higher_is_better=True, label="vs 90% target")
            d3,  dc3  = ar_delta_target(v3,  0.90, higher_is_better=True, label="vs 90% target")
            c1, c2 = st.columns(2)
            c1.metric("12-Month", fmt_pct(v12), delta=d12, delta_color=dc12)
            c2.metric("3-Month",  fmt_pct(v3),  delta=d3,  delta_color=dc3)
            st.caption("Target: 90%+. Not compared to peers.")
        ar_card(2, "Parent Updates Sent on Time", _s2)

        # ── 3. Prep Time Percentage ──────────────────────────────────────────
        def _s3():
            v12 = t12r.get("prep_time_ratio")
            v3  = t3r.get("prep_time_ratio") if t3r is not None else None
            p12 = peer_avg(peers_12m, "prep_time_ratio")
            p3  = peer_avg(peers_3m,  "prep_time_ratio")
            # Format as percentage
            def fmt_prep(v):
                if v is None or pd.isna(v): return "—"
                return f"{float(v)*100:.1f}%"
            d12, dc12 = ar_delta(v12, p12, higher_is_better=False)
            d3,  dc3  = ar_delta(v3,  p3,  higher_is_better=False)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("12-Month",       fmt_prep(v12), delta=d12, delta_color=dc12)
            c2.metric("3-Month",        fmt_prep(v3),  delta=d3,  delta_color=dc3)
            c3.metric("Peer Avg (12M)", fmt_prep(p12))
            c4.metric("Peer Avg (3M)",  fmt_prep(p3))
            st.caption(f"Prep hrs as % of attended hrs. Compared to {len(peers_12m)} peers — same tier ({tier_val}) & delivery target ({del_target}h/wk).")
        ar_card(3, "Prep Time Percentage", _s3)

        # ── 4. Cancellations by Tutor ────────────────────────────────────────
        # ── 4. Cancellations by Tutor ────────────────────────────────────────
        def _s4():
            cancel_df = load_cancellation_data()
            if cancel_df.empty:
                st.info("No cancellation data available.")
                return

            tutor_row = cancel_df[cancel_df["Tutor Name"].str.strip() == ar_tutor]
            tutor_count = int(tutor_row["Count of Days Cancelled"].iloc[0]) if not tutor_row.empty else 0

            peer_avg = round(cancel_df["Count of Days Cancelled"].mean(), 1)
            d, dc = ar_delta_num(tutor_count, peer_avg, higher_is_better=False, label="vs all-tutor avg")

            c1, c2 = st.columns(2)
            c1.metric("Days Cancelled", tutor_count, delta=d, delta_color=dc)
            c2.metric("All-Tutor Avg", fmt_num(peer_avg, 1))
            st.caption("Count of days with tutor-initiated cancellations during the review period. Lower is better.")
        ar_card(4, "Cancellations by Tutor", _s4)

        # ── 5. Exams Data ────────────────────────────────────────────────────
        def _s5():
            if ar_exam_snap.empty:
                st.info("No exam snapshot data available yet.")
                return
            tutor_es = ar_exam_snap[ar_exam_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            if tutor_es.empty:
                st.info(f"No exam data found for {ar_tutor}.")
                return
            all_latest  = ar_exam_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
            all_avg_pct = all_latest["pct_eligible_with_exam"].mean() if "pct_eligible_with_exam" in all_latest.columns else None

            latest   = tutor_es.iloc[-1]
            prev     = tutor_es.iloc[-2] if len(tutor_es) >= 2 else None
            lat_pct  = latest["pct_eligible_with_exam"]
            prev_pct = prev["pct_eligible_with_exam"] if prev is not None else None
            d_peer, dc_peer = ar_delta_num(lat_pct, all_avg_pct, higher_is_better=True, label="vs all-tutor avg")
            d_prev, dc_prev = ar_delta_num(lat_pct, prev_pct, higher_is_better=True, label="vs prev week")                               if prev_pct is not None else (None, "off")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("% With Exam (now)",    f"{lat_pct:.1f}%", delta=d_prev, delta_color=dc_prev)
            c2.metric("# No Exam (now)",       fmt_num(latest["students_no_exam"], 0))
            c3.metric("All-Tutor Avg %",       f"{all_avg_pct:.1f}%" if all_avg_pct else "—")
            c4.metric("# Stale Exam (now)",    fmt_num(latest.get("students_stale_exam"), 0))
            if prev is not None:
                st.caption(f"Previous week: {prev_pct:.1f}% — week of {prev['week_date'].strftime('%b %d') if hasattr(prev['week_date'], 'strftime') else prev['week_date']}")
            st.caption("% of eligible test-prep students with at least one recorded exam. Compared to all tutors. *(Full trend history accumulating weekly.)*")
        ar_card(5, "Exams Data", _s5)

        # ── 6. Grades Data ───────────────────────────────────────────────────
        def _s6():
            if ar_grades_snap.empty:
                st.info("No grades snapshot data available yet.")
                return
            tutor_gs = ar_grades_snap[ar_grades_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            if tutor_gs.empty:
                st.info(f"No grades data found for {ar_tutor}.")
                return
            all_latest     = ar_grades_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
            all_avg_graded = all_latest["pct_subjects_graded"].mean() if "pct_subjects_graded" in all_latest.columns else None

            latest   = tutor_gs.iloc[-1]
            prev     = tutor_gs.iloc[-2] if len(tutor_gs) >= 2 else None
            lat_pct  = latest["pct_subjects_graded"]
            prev_pct = prev["pct_subjects_graded"] if prev is not None else None
            d_peer, dc_peer = ar_delta_num(lat_pct, all_avg_graded, higher_is_better=True, label="vs all-tutor avg")
            d_prev, dc_prev = ar_delta_num(lat_pct, prev_pct, higher_is_better=True, label="vs prev week")                               if prev_pct is not None else (None, "off")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("% Graded (now)",       f"{lat_pct:.1f}%", delta=d_prev, delta_color=dc_prev)
            c2.metric("# No Grades (now)",     fmt_num(latest["students_no_grades"], 0))
            c3.metric("All-Tutor Avg %",       f"{all_avg_graded:.1f}%" if all_avg_graded else "—")
            c4.metric("Stale Grades (now)",    fmt_num(latest["stale_grade_students"], 0))
            if prev is not None:
                st.caption(f"Previous week: {prev_pct:.1f}% — week of {prev['week_date'].strftime('%b %d') if hasattr(prev['week_date'], 'strftime') else prev['week_date']}")
            st.caption("% of subject rows with a grade entered. Compared to all tutors. *(Full trend history accumulating weekly.)*")
        ar_card(6, "Grades Data", _s6)

        # ── 7. Rematches ─────────────────────────────────────────────────────
        # ── 7. Rematches ─────────────────────────────────────────────────────
        def _s7():
            rematch_df = load_rematch_tracker()
            if rematch_df.empty:
                st.info("No rematch data available.")
                return

            # Filter to this tutor and within 12-month review period
            cutoff_start = pd.to_datetime(AR_12M_START)
            cutoff_end   = pd.to_datetime(AR_12M_END)
            tutor_rematches = rematch_df[
                (rematch_df["Former Tutor"].str.strip() == ar_tutor) &
                (rematch_df["Rematch Date Parsed"] >= cutoff_start) &
                (rematch_df["Rematch Date Parsed"] <= cutoff_end)
            ].copy()

            # All tutors for peer comparison
            all_rematches = rematch_df[
                (rematch_df["Rematch Date Parsed"] >= cutoff_start) &
                (rematch_df["Rematch Date Parsed"] <= cutoff_end)
            ]
            all_tutor_counts = all_rematches.groupby("Former Tutor").size()
            peer_avg = round(all_tutor_counts.mean(), 1) if not all_tutor_counts.empty else 0

            count = len(tutor_rematches)
            d, dc = ar_delta_num(count, peer_avg, higher_is_better=False, label="vs all-tutor avg")

            c1, c2 = st.columns(2)
            c1.metric("Rematches (12M)", count, delta=d, delta_color=dc)
            c2.metric("All-Tutor Avg",   fmt_num(peer_avg, 1))

            if count > 0:
                st.markdown("**Rematch Details:**")
                display = tutor_rematches[["Rematch Date Parsed","Student Name","Reason for Rematch Request",
                                           "Does the Rematch Seem Valid?","FL Thoughts"]].copy()
                display["Rematch Date Parsed"] = display["Rematch Date Parsed"].dt.strftime("%m/%d/%Y")
                display = display.rename(columns={
                    "Rematch Date Parsed": "Date", "Student Name": "Student",
                    "Reason for Rematch Request": "Reason",
                    "Does the Rematch Seem Valid?": "Valid?", "FL Thoughts": "FL Thoughts"})
                st.dataframe(display, use_container_width=True, hide_index=True)
            st.caption("Counts rematches where this tutor was the former tutor during the 12-month review period.")
        ar_card(7, "Rematches", _s7)

        # ── 8. Weighted Repurchase ───────────────────────────────────────────
        # ── 8. Weighted Repurchase ───────────────────────────────────────────
        def _s8():
            tutor_mm = monthly_metric_annual_review_df[
                monthly_metric_annual_review_df["Tutor Name"] == ar_tutor].copy()
            if tutor_mm.empty:
                st.info("No monthly metric data found for this tutor.")
                return

            def _parse_end_ar(s):
                if pd.isna(s): return pd.NaT
                s2 = str(s).replace("-","to").replace("–","to")
                parts = s2.split("to")
                end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                return pd.to_datetime(end, errors="coerce", dayfirst=False)

            tutor_mm["Date Parsed"] = tutor_mm["Date Range"].apply(_parse_end_ar)
            tutor_mm = tutor_mm.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")

            # 12-month and 3-month totals
            cutoff_12m = pd.to_datetime(AR_12M_START)
            cutoff_3m  = pd.to_datetime(AR_3M_START)
            wr_col = "Weighted Repurchases"
            if wr_col not in tutor_mm.columns:
                st.info("Weighted Repurchases column not found in data.")
                return

            wr_12m = pd.to_numeric(tutor_mm[tutor_mm["Date Parsed"] >= cutoff_12m][wr_col], errors="coerce").sum()
            wr_3m  = pd.to_numeric(tutor_mm[tutor_mm["Date Parsed"] >= cutoff_3m][wr_col], errors="coerce").sum()

            # Peer avg — same tier & delivery target
            peer_mm_12m = monthly_metric_annual_review_df[
                (monthly_metric_annual_review_df["Tier"] == tier_val) &
                (monthly_metric_annual_review_df["Tutor Name"] != ar_tutor)
            ].copy()
            peer_mm_12m["Date Parsed"] = peer_mm_12m["Date Range"].apply(_parse_end_ar)
            peer_wr = peer_mm_12m[peer_mm_12m["Date Parsed"] >= cutoff_12m].groupby("Tutor Name")[wr_col].apply(
                lambda x: pd.to_numeric(x, errors="coerce").sum()).mean()

            d, dc = ar_delta_num(wr_12m, peer_wr, higher_is_better=True, label="vs peer avg")
            c1, c2, c3 = st.columns(3)
            c1.metric("12-Month Total", fmt_num(wr_12m, 1), delta=d, delta_color=dc)
            c2.metric("3-Month Total",  fmt_num(wr_3m, 1))
            c3.metric("Peer Avg (12M, same tier)", fmt_num(peer_wr, 1))

            # Trend chart
            tutor_wr = tutor_mm[tutor_mm["Date Parsed"] >= cutoff_12m][["Date Range","Date Parsed",wr_col]].copy()
            tutor_wr[wr_col] = pd.to_numeric(tutor_wr[wr_col], errors="coerce")
            if len(tutor_wr) >= 2:
                fig_wr = px.bar(tutor_wr, x="Date Range", y=wr_col,
                                title="Weighted Repurchases by Month",
                                color_discrete_sequence=["#7b2d8b"])
                fig_wr.update_layout(height=250, margin=dict(l=20,r=20,t=40,b=60),
                                     xaxis_title="", yaxis_title="Repurchases",
                                     xaxis_tickangle=-30,
                                     title=dict(x=0.5, xanchor="center"))
                st.plotly_chart(fig_wr, use_container_width=True)
            st.caption(f"Compared to {len(peers_12m)} peers — same tier ({tier_val}) & delivery target.")
        ar_card(8, "Weighted Repurchase", _s8)

        # ── 9. Archivable Students ───────────────────────────────────────────
        def _s9():
            if ar_arch_snap.empty:
                st.info("No archivable student snapshot data available yet.")
                return
            tutor_as = ar_arch_snap[ar_arch_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            if tutor_as.empty:
                st.info(f"No archivable data found for {ar_tutor}.")
                return
            all_latest  = ar_arch_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
            all_avg_pct = all_latest["pct_archivable"].mean() if "pct_archivable" in all_latest.columns else None

            latest   = tutor_as.iloc[-1]
            prev     = tutor_as.iloc[-2] if len(tutor_as) >= 2 else None
            lat_pct  = latest["pct_archivable"] if "pct_archivable" in latest else None
            prev_pct = prev["pct_archivable"]   if prev is not None and "pct_archivable" in prev else None
            d_peer, dc_peer = ar_delta_num(lat_pct, all_avg_pct, higher_is_better=False, label="vs all-tutor avg")
            d_prev, dc_prev = ar_delta_num(lat_pct, prev_pct,    higher_is_better=False, label="vs prev week")                               if prev_pct is not None else (None, "off")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("# Archivable (now)",    fmt_num(latest["archivable_students"], 0))
            c2.metric("% Archivable (now)",    f"{lat_pct:.1f}%" if lat_pct else "—",
                      delta=d_prev, delta_color=dc_prev)
            c3.metric("All-Tutor Avg %",       f"{all_avg_pct:.1f}%" if all_avg_pct else "—")
            c4.metric("vs All-Tutor Avg",      f"{lat_pct - all_avg_pct:+.1f}pp" if lat_pct and all_avg_pct else "—")
            if prev is not None:
                st.caption(f"Previous week: {int(prev['archivable_students'])} archivable ({prev_pct:.1f}%) — week of {prev['week_date'].strftime('%b %d') if hasattr(prev['week_date'], 'strftime') else prev['week_date']}")
            st.caption("% of active students who are archivable. Lower is better. Compared to all tutors. *(Full trend history coming next review cycle.)*")
        ar_card(9, "Archivable Students", _s9)

        # ── 10. Unscheduled Hours ────────────────────────────────────────────
        def _s10():
            if ar_arch_snap.empty:
                st.info("No unscheduled hours snapshot data available yet.")
                return
            tutor_as = ar_arch_snap[ar_arch_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            if tutor_as.empty:
                st.info(f"No unscheduled hours data found for {ar_tutor}.")
                return
            all_latest  = ar_arch_snap.sort_values("week_date").groupby("tutor_name").last().reset_index()
            all_avg_hrs = all_latest["unscheduled_hours"].mean()

            latest   = tutor_as.iloc[-1]
            prev     = tutor_as.iloc[-2] if len(tutor_as) >= 2 else None
            lat_hrs  = latest["unscheduled_hours"]
            prev_hrs = prev["unscheduled_hours"] if prev is not None else None
            d_peer, dc_peer = ar_delta_num(lat_hrs, all_avg_hrs, higher_is_better=False, label="vs all-tutor avg")
            d_prev, dc_prev = ar_delta_num(lat_hrs, prev_hrs, higher_is_better=False, label="vs prev week")                               if prev_hrs is not None else (None, "off")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Unscheduled Hrs (now)", fmt_num(lat_hrs, 1),
                      delta=d_prev, delta_color=dc_prev)
            c2.metric("All-Tutor Avg (hrs)",   fmt_num(all_avg_hrs, 1))
            c3.metric("vs All-Tutor Avg",      f"{lat_hrs - all_avg_hrs:+.1f} hrs" if all_avg_hrs else "—")
            c4.metric("Total Students",         fmt_num(latest["total_students"], 0))
            if prev is not None:
                st.caption(f"Previous week: {prev_hrs:.1f} hrs — week of {prev['week_date'].strftime('%b %d') if hasattr(prev['week_date'], 'strftime') else prev['week_date']}")
            st.caption("Unscheduled hours for active students only. Lower is better. Compared to all tutors. *(Full trend history coming next review cycle.)*")
        ar_card(10, "Unscheduled Hours", _s10)

        # ── 11. Auto-Attendance ──────────────────────────────────────────────
        def _s11():
            aa_12      = t12r.get("autoattendance_sessions")
            aa_3       = t3r.get("autoattendance_sessions") if t3r is not None else None
            aa_peer_12 = peer_avg(all_12m, "autoattendance_sessions")
            d12, dc12  = ar_delta_num(aa_12, aa_peer_12, higher_is_better=False, label="vs all-tutor avg")
            c1, c2, c3 = st.columns(3)
            c1.metric("12-Month (sessions)", fmt_num(aa_12, 0), delta=d12, delta_color=dc12)
            c2.metric("3-Month (sessions)",  fmt_num(aa_3, 0))
            c3.metric("All-Tutor Avg (12M)", fmt_num(aa_peer_12, 1))
            st.caption("Compared to all tutors. Lower is better.")
        ar_card(11, "Auto-Attendance", _s11)

        # ── 12. NPS ──────────────────────────────────────────────────────────
        def _s12():
            c1, c2, c3 = st.columns(3)
            c1.metric("Avg Score (12M)",   fmt_num(t12r.get("avg_nps"), 2))
            c2.metric("Avg Score (3M)",    fmt_num(t3r.get("avg_nps") if t3r is not None else None, 2))
            c3.metric("# Responses (12M)", fmt_num(t12r.get("number_of_nps"), 0))
            st.caption("Not compared to peers. Individual survey scores and comments coming soon.")
        ar_card(12, "NPS", _s12)

        # ── 13. Parent Update Video ──────────────────────────────────────────
        def _s13():
            if ar_video_snap.empty:
                st.info("No video snapshot data available yet.")
                return
            tutor_vs  = ar_video_snap[ar_video_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            all_vs    = ar_video_snap.copy()

            # Most recent week metrics
            if tutor_vs.empty:
                st.info(f"No video data found for {ar_tutor}.")
                return

            latest    = tutor_vs.iloc[-1]
            all_latest = all_vs.groupby("tutor_name")["pct_with_video"].last()
            all_avg   = all_latest.mean()

            # Averages across all weeks
            tutor_avg = tutor_vs["pct_with_video"].mean()
            d, dc     = ar_delta_num(tutor_avg, all_avg, higher_is_better=True, label="vs all-tutor avg")

            c1, c2, c3 = st.columns(3)
            c1.metric("Most Recent Week %",   f"{latest['pct_with_video']:.1f}%")
            c2.metric("Avg Across All Weeks", f"{tutor_avg:.1f}%", delta=d, delta_color=dc)
            c3.metric("All-Tutor Avg",        f"{all_avg:.1f}%")

            # Trend chart
            if len(tutor_vs) >= 2:
                fig_v = px.line(tutor_vs, x="week_date", y="pct_with_video",
                                markers=True, title="% With Video — Week over Week",
                                color_discrete_sequence=["#7b2d8b"])
                fig_v.add_hline(y=80, line_dash="dash", line_color="#cc0000",
                                annotation_text="80% threshold")
                fig_v.update_layout(height=250, margin=dict(l=20,r=20,t=40,b=20),
                                    xaxis_title="", yaxis_title="%",
                                    title=dict(x=0.5, xanchor="center"))
                st.plotly_chart(fig_v, use_container_width=True)
            else:
                st.caption("Not enough weeks of data for a trend chart yet.")

            st.caption("Based on weekly video snapshots. Compared to all tutors on team.")
        ar_card(13, "Parent Update Video", _s13)

        # ── 14. Parent Update Mentioning Homework ────────────────────────────
        def _s14():
            if ar_video_snap.empty or "pct_with_homework" not in ar_video_snap.columns:
                st.info("No homework mention data available yet.")
                return
            tutor_vs  = ar_video_snap[ar_video_snap["tutor_name"] == ar_tutor].sort_values("week_date")
            all_vs    = ar_video_snap.copy()

            if tutor_vs.empty:
                st.info(f"No homework mention data found for {ar_tutor}.")
                return

            latest    = tutor_vs.iloc[-1]
            all_latest = all_vs.groupby("tutor_name")["pct_with_homework"].last()
            all_avg   = all_latest.mean()
            tutor_avg = tutor_vs["pct_with_homework"].mean()
            d, dc     = ar_delta_num(tutor_avg, all_avg, higher_is_better=True, label="vs all-tutor avg")

            c1, c2, c3 = st.columns(3)
            c1.metric("Most Recent Week %",   f"{latest['pct_with_homework']:.1f}%")
            c2.metric("Avg Across All Weeks", f"{tutor_avg:.1f}%", delta=d, delta_color=dc)
            c3.metric("All-Tutor Avg",        f"{all_avg:.1f}%")

            if len(tutor_vs) >= 2:
                fig_h = px.line(tutor_vs, x="week_date", y="pct_with_homework",
                                markers=True, title="% Updates Mentioning Homework — Week over Week",
                                color_discrete_sequence=["#1565c0"])
                fig_h.update_layout(height=250, margin=dict(l=20,r=20,t=40,b=20),
                                    xaxis_title="", yaxis_title="%",
                                    title=dict(x=0.5, xanchor="center"))
                st.plotly_chart(fig_h, use_container_width=True)
            else:
                st.caption("Not enough weeks of data for a trend chart yet.")

            st.caption("Based on weekly video snapshots. Compared to all tutors on team.")
        ar_card(14, "Parent Update Mentioning Homework", _s14)

        # ── 15. Progress Updates ─────────────────────────────────────────────
        # ── 15. Progress Updates ─────────────────────────────────────────────
        def _s15():
            pu_col = "% of Active Students with Progress Updates Completed in last 2 months"
            tutor_mm = monthly_metric_annual_review_df[
                monthly_metric_annual_review_df["Tutor Name"] == ar_tutor].copy()
            if tutor_mm.empty or pu_col not in tutor_mm.columns:
                st.info("No progress update data found for this tutor.")
                return

            def _parse_end_pu(s):
                if pd.isna(s): return pd.NaT
                s2 = str(s).replace("-","to").replace("–","to")
                parts = s2.split("to")
                end = parts[-1].strip() if len(parts)>1 else parts[0].strip()
                return pd.to_datetime(end, errors="coerce", dayfirst=False)

            tutor_mm["Date Parsed"] = tutor_mm["Date Range"].apply(_parse_end_pu)
            tutor_mm = tutor_mm.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
            tutor_mm[pu_col] = pd.to_numeric(tutor_mm[pu_col], errors="coerce")

            cutoff_12m = pd.to_datetime(AR_12M_START)
            cutoff_3m  = pd.to_datetime(AR_3M_START)

            pu_12m = tutor_mm[tutor_mm["Date Parsed"] >= cutoff_12m][pu_col].mean()
            pu_3m  = tutor_mm[tutor_mm["Date Parsed"] >= cutoff_3m][pu_col].mean()

            # Peer avg
            peer_mm = monthly_metric_annual_review_df[
                (monthly_metric_annual_review_df["Tier"] == tier_val) &
                (monthly_metric_annual_review_df["Tutor Name"] != ar_tutor)
            ].copy()
            peer_mm["Date Parsed"] = peer_mm["Date Range"].apply(_parse_end_pu)
            peer_pu = peer_mm[peer_mm["Date Parsed"] >= cutoff_12m].groupby("Tutor Name")[pu_col].apply(
                lambda x: pd.to_numeric(x, errors="coerce").mean()).mean()

            d12, dc12 = ar_delta(pu_12m, peer_pu, higher_is_better=True)
            d3,  dc3  = ar_delta(pu_3m,  peer_pu, higher_is_better=True)
            c1, c2, c3 = st.columns(3)
            c1.metric("Avg % (12M)", fmt_pct(pu_12m), delta=d12, delta_color=dc12)
            c2.metric("Avg % (3M)",  fmt_pct(pu_3m),  delta=d3,  delta_color=dc3)
            c3.metric("Peer Avg (12M, same tier)", fmt_pct(peer_pu))

            # Trend
            tutor_pu_trend = tutor_mm[tutor_mm["Date Parsed"] >= cutoff_12m][["Date Range","Date Parsed",pu_col]].copy()
            if len(tutor_pu_trend) >= 2:
                fig_pu = px.line(tutor_pu_trend, x="Date Range", y=pu_col,
                                 markers=True, title="% Progress Updates Completed — by Month",
                                 color_discrete_sequence=["#1565c0"])
                fig_pu.add_hline(y=0.8, line_dash="dash", line_color="#f57f17",
                                 annotation_text="80% threshold")
                fig_pu.update_layout(height=250, margin=dict(l=20,r=20,t=40,b=60),
                                     xaxis_title="", yaxis_title="%",
                                     xaxis_tickangle=-30,
                                     title=dict(x=0.5, xanchor="center"))
                st.plotly_chart(fig_pu, use_container_width=True)
            st.caption(f"% of active students with a progress update in the last 2 months. Compared to peers — same tier ({tier_val}).")
        ar_card(15, "Progress Updates", _s15)

        # ── 16. Current SCI ──────────────────────────────────────────────────
        def _s16():
            sci_val   = t12r.get("current_sci")
            sci_peers = peer_avg(peers_12m, "current_sci")
            d, dc     = ar_delta_num(sci_val, sci_peers, higher_is_better=True, label="vs peer avg")
            c1, c2 = st.columns(2)
            c1.metric("Current Score",        fmt_num(sci_val, 1), delta=d, delta_color=dc)
            c2.metric("Peer Avg (same tier)",  fmt_num(sci_peers, 1))
            st.caption(f"Compared to {len(peers_12m)} peers in same tier ({tier_val}). SCI is a point-in-time value.")
        ar_card(16, "Current SCI", _s16)

        # ── 17. SCI Growth ───────────────────────────────────────────────────
        def _s17():
            tutor_skills = ar_skills_df[ar_skills_df["tutor_name"] == ar_tutor].copy()                            if not ar_skills_df.empty else pd.DataFrame()
            sci_start = None
            sci_end   = t12r.get("current_sci")

            if tutor_skills.empty:
                st.info("No new subjects added during the review period.")
            else:
                tutor_skills["created"] = pd.to_datetime(tutor_skills["created"], errors="coerce")
                tutor_skills = tutor_skills.sort_values("created")

                # SCI change: sum of difficulty of added subjects
                sci_gained = tutor_skills["subject_sci"].sum() if "subject_sci" in tutor_skills.columns else None

                c1, c2, c3 = st.columns(3)
                c1.metric("Subjects Added", len(tutor_skills))
                c2.metric("SCI Points Gained", fmt_num(sci_gained, 1) if sci_gained else "—")
                c3.metric("Current SCI", fmt_num(sci_end, 1))

                st.markdown("**Subjects Added During Review Period:**")
                display_skills = tutor_skills[["created","category","subject","subject_sci"]].copy()
                display_skills["created"] = display_skills["created"].dt.strftime("%Y-%m-%d")
                display_skills = display_skills.rename(columns={
                    "created":     "Date Added",
                    "category":    "Category",
                    "subject":     "Subject",
                    "subject_sci": "SCI Value",
                })
                st.dataframe(display_skills, use_container_width=True, hide_index=True)
        ar_card(17, "SCI Growth", _s17)

        # ── 18. Availability Percentage ──────────────────────────────────────
        def _s18():
            v12 = t12r.get("availability_pct")
            v3  = t3r.get("availability_pct") if t3r is not None else None
            c1, c2 = st.columns(2)
            c1.metric("12-Month", fmt_pct(v12))
            c2.metric("3-Month",  fmt_pct(v3))
            st.caption("Not compared to peers — target varies based on whether delivery target is met.")
        ar_card(18, "Availability Percentage", _s18)

        # ── 19. Delivery Percentage ───────────────────────────────────────────
        def _s19():
            v12 = t12r.get("delivery_pct")
            v3  = t3r.get("delivery_pct") if t3r is not None else None
            p12 = peer_avg(peers_12m, "delivery_pct")
            p3  = peer_avg(peers_3m,  "delivery_pct")
            d12, dc12 = ar_delta(v12, p12, higher_is_better=True)
            d3,  dc3  = ar_delta(v3,  p3,  higher_is_better=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("12-Month",       fmt_pct(v12), delta=d12, delta_color=dc12)
            c2.metric("3-Month",        fmt_pct(v3),  delta=d3,  delta_color=dc3)
            c3.metric("Peer Avg (12M)", fmt_pct(p12))
            c4.metric("Peer Avg (3M)",  fmt_pct(p3))
            st.caption(f"Compared to {len(peers_12m)} peers — same tier ({tier_val}) & delivery target ({del_target}h/wk).")
        ar_card(19, "Delivery Percentage", _s19)

        # ── 20. Weeks Meeting Delivery Target ────────────────────────────────
        def _s20():
            v12 = t12r.get("weeks_at_target")
            v3  = t3r.get("weeks_at_target") if t3r is not None else None
            p12 = peer_avg(peers_12m, "weeks_at_target")
            p3  = peer_avg(peers_3m,  "weeks_at_target")
            d12, dc12 = ar_delta_num(v12, p12, higher_is_better=True, label="vs peer avg")
            d3,  dc3  = ar_delta_num(v3,  p3,  higher_is_better=True, label="vs peer avg")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("12-Month (weeks)",  fmt_num(v12, 0), delta=d12, delta_color=dc12)
            c2.metric("3-Month (weeks)",   fmt_num(v3,  0), delta=d3,  delta_color=dc3)
            c3.metric("Peer Avg (12M)",    fmt_num(p12, 1))
            c4.metric("Peer Avg (3M)",     fmt_num(p3,  1))
            st.caption(f"Compared to {len(peers_12m)} peers — same tier ({tier_val}) & delivery target ({del_target}h/wk).")
        ar_card(20, "Weeks Meeting Delivery Target", _s20)
