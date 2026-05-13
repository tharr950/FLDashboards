#!/usr/bin/env python3
"""
sync_redshift_cache.py
Daily sync of key Redshift tables to GitHub as CSV fallback cache.
Cron: 0 3 * * * (3am daily, after sync_ar_data.py at 2am)
"""

import os, io, sys, base64, json
from datetime import datetime
import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

REDSHIFT_HOST = os.environ["REDSHIFT_HOST"]
REDSHIFT_PORT = int(os.environ.get("REDSHIFT_PORT", 5439))
REDSHIFT_DB   = os.environ["REDSHIFT_DB"]
REDSHIFT_USER = os.environ["REDSHIFT_USER"]
REDSHIFT_PASS = os.environ["REDSHIFT_PASSWORD"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ["GITHUB_REPO"]

def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)

def get_conn():
    return psycopg2.connect(
        host=REDSHIFT_HOST, port=REDSHIFT_PORT,
        dbname=REDSHIFT_DB, user=REDSHIFT_USER,
        password=REDSHIFT_PASS, connect_timeout=30
    )

def push_to_github(df, path, label):
    url     = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    df["_cached_at"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    encoded   = base64.b64encode(csv_bytes).decode("utf-8")
    r = requests.get(url, headers=headers, timeout=15)
    sha = r.json().get("sha") if r.status_code == 200 else None
    payload = {"message": f"cache: {label} {datetime.utcnow():%Y-%m-%d}", "content": encoded}
    if sha:
        payload["sha"] = sha
    r2 = requests.put(url, json=payload, headers=headers, timeout=30)
    if r2.status_code in (200, 201):
        log(f"✅ Pushed {label} ({len(df)} rows) → {path}")
    else:
        log(f"❌ Failed to push {label}: {r2.status_code} {r2.text[:100]}")

QUERIES = {
    "master_tutor": (
        "data/cache/master_tutor.csv",
        """
        SELECT DISTINCT
            e.id AS user_id,
            DATE(e.hire_date) AS hire_date,
            tu.first_name||' '||tu.last_name AS tutor_name,
            flu.first_name||' '||flu.last_name AS faculty_leader,
            CASE WHEN e.delivery_target < 30 THEN 'Adjunct' ELSE 'Professional' END AS tutor_type,
            t.name AS tier,
            e.delivery_target,
            e.accept_new_students,
            e.featured
        FROM dw.employees e
        JOIN dw.team_members tm ON tm.member_id = e.id
        JOIN dw.teams ON dw.teams.id = tm.team_id
        JOIN dw.users tu ON e.user_id = tu.id
        JOIN dw.employees mgr ON mgr.id = dw.teams.manager_id
        JOIN dw.users flu ON mgr.user_id = flu.id
        JOIN dw.tiers t ON e.tier_id = t.id
        WHERE e.type = 'Tutor'
          AND e.end_date IS NULL
          AND e.tier_id IS NOT NULL
          AND tu.title = 'Tutor'
        ORDER BY tutor_name
        """
    ),
    "archivable_unscheduled": (
        "data/cache/archivable_unscheduled.csv",
        """
        WITH cte_courses AS (
            SELECT dw.courses.id AS course_id,
                student_users.first_name||' '||student_users.last_name AS student_name,
                dw.brands.name AS brand,
                ROUND(dw.courses.provisioned_duration/60.00,2) AS provisioned_hours,
                ROUND(dw.courses.delivered_duration/60.00,2) AS delivered_hours,
                ROUND(dw.courses.duration/60.00,2) AS duration_hours
            FROM dw.courses
            JOIN dw.enrollments ON dw.courses.id = dw.enrollments.course_id
            JOIN dw.students ON dw.enrollments.enrollee_id = dw.students.id
            JOIN dw.users student_users ON dw.students.user_id = student_users.id
            JOIN dw.brands ON dw.courses.brand_id = dw.brands.id
            LEFT JOIN dw.sessions ON dw.courses.id = dw.sessions.course_id
            WHERE dw.courses.brand_id IN (2,41,42,43)
            GROUP BY 1,2,3,4,5,6
        )
        SELECT dw.tutoring_histories.tutor_id AS tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            fl_users.first_name||' '||fl_users.last_name AS faculty_leader,
            dw.tiers.name AS tier,
            dw.teams.name AS team_name,
            cte_courses.course_id,
            cte_courses.brand,
            cte_courses.student_name,
            MIN(dw.sessions.starts_at) AS first_session_day,
            MAX(dw.sessions.starts_at) AS last_session_day,
            CASE WHEN MAX(dw.sessions.starts_at) < (GETDATE()-30) THEN 1 ELSE 0 END AS should_archive,
            cte_courses.provisioned_hours - cte_courses.delivered_hours AS hours_remaining,
            CASE WHEN cte_courses.brand = 'Academics'
                 AND (cte_courses.provisioned_hours - cte_courses.duration_hours) < 0
                 THEN 0
                 ELSE cte_courses.provisioned_hours - cte_courses.duration_hours
            END AS unscheduled_hours
        FROM dw.tutoring_histories
        JOIN dw.employees ON dw.employees.id = dw.tutoring_histories.tutor_id
        JOIN dw.tiers ON dw.employees.tier_id = dw.tiers.id
        JOIN dw.users tutor_users ON tutor_users.id = dw.employees.user_id
        JOIN dw.team_members ON dw.team_members.member_id = dw.employees.id
        JOIN dw.teams ON dw.teams.id = dw.team_members.team_id
        JOIN dw.employees managers ON managers.id = dw.teams.manager_id
        JOIN dw.users fl_users ON fl_users.id = managers.user_id
        JOIN dw.enrollments ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
        JOIN dw.sessions ON dw.sessions.course_id = dw.enrollments.course_id
            AND dw.sessions.supervisor_id = dw.employees.id
        JOIN cte_courses ON dw.enrollments.course_id = cte_courses.course_id
        WHERE dw.tutoring_histories.active = TRUE
          AND dw.employees.end_date IS NULL
          AND dw.enrollments.unenrolled_at IS NULL
          AND dw.team_members.member_type = 'Employee'
        GROUP BY 1,2,3,4,5,6,7,8,12,13
        ORDER BY unscheduled_hours
        """
    ),
    "grades_data": (
        "data/cache/grades_data.csv",
        """
        WITH cte_grades AS (
            SELECT orbit_stitch.study_areas.student_id,
            dw.subjects.name AS subject, sas.score, sas.updated_at
            FROM orbit_stitch.study_areas
            LEFT JOIN dw.subjects ON orbit_stitch.study_areas.subject_id = dw.subjects.id
            LEFT JOIN orbit_stitch.study_area_snapshots sas ON orbit_stitch.study_areas.id = sas.study_area_id
            WHERE dw.subjects.category_id IN (1,2,3,4,5,8,9,10,11)
            AND CAST(dw.subjects.high_grade AS INT) > 8
            AND orbit_stitch.study_areas.archived_at IS NULL
            AND orbit_stitch.study_areas._sdc_deleted_at IS NULL
        ),
        cte_last_30_days_brands AS (
            SELECT dw.enrollments.enrollee_id AS student_id,
            dw.sessions.supervisor_id AS tutor_id,
            CASE WHEN dw.courses.brand_id = 2  THEN COUNT(DISTINCT dw.sessions.id) END AS private_tutoring_sessions,
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
            CASE WHEN dw.courses.brand_id = 2  THEN COUNT(DISTINCT dw.sessions.id) END AS private_tutoring_sessions,
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
            GROUP BY dw.enrollments.enrollee_id, dw.sessions.supervisor_id,
                dw.courses.brand_id, dw.students.graduation_year
        )
        SELECT cte_future.tutor_id,
            tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
            dw.teams.name AS team_name,
            cte_future.student_id,
            student_users.first_name||' '||student_users.last_name AS student_name,
            cte_future.grad_date,
            CASE WHEN cte_future.grad_date - (GETDATE()::DATE) >= 0
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
            cte_grades.subject, cte_grades.score,
            CAST(cte_grades.updated_at AS DATE) AS updated_at
        FROM cte_future
        JOIN cte_last_30_days_brands ON cte_future.student_id = cte_last_30_days_brands.student_id
            AND cte_future.tutor_id = cte_last_30_days_brands.tutor_id
        JOIN cte_last_30_days_sessions lds1 ON cte_future.student_id = lds1.student_id
            AND cte_future.tutor_id = lds1.tutor_id
        JOIN cte_last_30_days_sessions lds2 ON cte_future.student_id = lds2.student_id
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
    ),
    "availability_compliance": (
        "data/cache/availability_compliance.csv",
        """
        WITH cte_avail AS (
            SELECT a.employee_id,
                a.starts_at AT TIME ZONE 'America/Los_Angeles' AT TIME ZONE u.time_zone AS tutor_starts_at,
                u.first_name||' '||u.last_name AS tutor,
                addr.state, t.name AS team, u.time_zone AS tutor_time_zone
            FROM dw.availabilities a
            JOIN dw.employees e ON a.employee_id = e.id
            JOIN dw.users u ON u.id = e.user_id
            JOIN dw.team_members tm ON e.id = tm.member_id
            JOIN dw.teams t ON tm.team_id = t.id
            JOIN dw.addresses addr ON u.address_id = addr.id
        )
        SELECT a.tutor AS tutor_name, a.employee_id, a.tutor_time_zone, a.state, a.team,
            d.first_day_of_week_sunday_start AS week_start
        FROM cte_avail a
        JOIN rp_bi.dates d ON a.tutor_starts_at::DATE = d.full_date
        WHERE d.first_day_of_week_sunday_start = DATE_TRUNC('week', GETDATE())::DATE - 1
        GROUP BY a.tutor, a.employee_id, a.tutor_time_zone, a.state, a.team,
            d.first_day_of_week_sunday_start
        HAVING COUNT(DISTINCT a.tutor_starts_at::DATE) > 6
        ORDER BY a.team, a.tutor
        """
    ),
    "featured_tutors": (
        "data/cache/featured_tutors.csv",
        """
        SELECT tu.first_name||' '||tu.last_name AS tutor,
            flu.first_name||' '||flu.last_name AS faculty_leader,
            t.name AS tutor_tier,
            e.delivery_target
        FROM dw.employees e
        JOIN dw.users tu ON e.user_id = tu.id
        JOIN dw.team_members tm ON e.id = tm.member_id
        JOIN dw.teams ON dw.teams.id = tm.team_id
        JOIN dw.employees mgr ON dw.teams.manager_id = mgr.id
        JOIN dw.users flu ON mgr.user_id = flu.id
        JOIN dw.tiers t ON e.tier_id = t.id
        WHERE e.end_date IS NULL
          AND e.delivery_target > 0
          AND e.type = 'Tutor'
          AND tu.title = 'Tutor'
          AND e.featured IS TRUE
        ORDER BY faculty_leader, tutor_tier, tutor
        """
    ),
}

def main():
    log("sync_redshift_cache.py — starting")
    try:
        conn = get_conn()
        log("Connected to Redshift ✅")
    except Exception as e:
        log(f"❌ Could not connect to Redshift: {e}")
        sys.exit(1)

    for name, (path, query) in QUERIES.items():
        try:
            log(f"Fetching {name}...")
            df = pd.read_sql(query, conn)
            push_to_github(df, path, name)
        except Exception as e:
            log(f"❌ Error on {name}: {e}")

    conn.close()
    log("Done ✅")

if __name__ == "__main__":
    main()
