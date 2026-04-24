#!/usr/bin/env python3
"""
sync_ar_data.py
Pulls annual review KPI data for both 12-month and 3-month periods
and pushes to GitHub as ar_data_12m.csv and ar_data_3m.csv.
Run nightly via cron.
"""

import os, sys, base64, json
from datetime import datetime
import pandas as pd
import psycopg2
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Hardcoded date ranges (update before review season) ──────────────────────
AR_12M_START = "2025-04-17"
AR_12M_END   = "2026-04-17"
AR_3M_START  = "2026-01-17"
AR_3M_END    = "2026-04-17"

# ── Credentials ──────────────────────────────────────────────────────────────
RS_HOST     = os.environ["REDSHIFT_HOST"]
RS_PORT     = int(os.environ.get("REDSHIFT_PORT", 5439))
RS_DB       = os.environ["REDSHIFT_DB"]
RS_USER     = os.environ["REDSHIFT_USER"]
RS_PASSWORD = os.environ["REDSHIFT_PASSWORD"]

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]

LOG_PREFIX = f"[{datetime.now():%Y-%m-%d %H:%M:%S}]"


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get_redshift_connection():
    return psycopg2.connect(
        host=RS_HOST, port=RS_PORT, dbname=RS_DB,
        user=RS_USER, password=RS_PASSWORD,
        connect_timeout=30
    )


def fetch_ar_kpi(start, end):
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
            SELECT ws.supervisor_id, ws.first_day_of_week_sunday_start AS week,
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
    conn = get_redshift_connection()
    try:
        df = pd.read_sql(query, conn)
    finally:
        conn.close()
    return df


def push_csv_to_github(df, github_path, label):
    csv_content = df.to_csv(index=False)
    encoded = base64.b64encode(csv_content.encode()).decode()
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{github_path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"}

    # Get existing SHA if file exists
    resp = requests.get(url, headers=headers)
    sha = resp.json().get("sha") if resp.status_code == 200 else None

    payload = {
        "message": f"sync: update {label} ({datetime.now():%Y-%m-%d})",
        "content": encoded,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, headers=headers, data=json.dumps(payload))
    if resp.status_code in (200, 201):
        log(f"{label}: pushed to GitHub ✅ ({len(df)} rows)")
    else:
        log(f"{label}: GitHub push failed — {resp.status_code} {resp.text}")
        sys.exit(1)


if __name__ == "__main__":
    log("Starting annual review data sync...")

    log(f"Fetching 12-month data ({AR_12M_START} to {AR_12M_END})...")
    df_12m = fetch_ar_kpi(AR_12M_START, AR_12M_END)
    log(f"12-month: {len(df_12m)} rows fetched.")
    push_csv_to_github(df_12m, "data/ar_data_12m.csv", "ar_data_12m")

    log(f"Fetching 3-month data ({AR_3M_START} to {AR_3M_END})...")
    df_3m = fetch_ar_kpi(AR_3M_START, AR_3M_END)
    log(f"3-month: {len(df_3m)} rows fetched.")
    push_csv_to_github(df_3m, "data/ar_data_3m.csv", "ar_data_3m")

    log("Annual review sync complete ✅")