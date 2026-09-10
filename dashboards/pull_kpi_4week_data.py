#!/usr/bin/env python3
"""
pull_kpi_4week_data.py

STEP 1 of building the master metrics file used to populate tutor KPI
Trackers. Connects to Redshift (same .env / psycopg2 pattern as
sync_redshift_cache.py), runs the "KPI 4 Week Data" query, and saves the
result to an Excel file.

This is intentionally just the query -> file step. Appending the
additional data pulled from the FL dashboard history files is a separate
step to layer on next -- not done here.

Supports --start-date YYYY-MM-DD --end-date YYYY-MM-DD to run the query
for a specific historical period instead of the default rolling 4-week
window ending today (e.g. re-running a past period after a data issue).
See metrics_period.py -- day_start/day_end are passed straight into the
query as bind parameters, so there's no separate date math on the SQL
side to keep in sync.
"""

import os
import sys
from datetime import datetime

import pandas as pd
import psycopg2
from dotenv import load_dotenv

from metrics_period import parse_period_args

# Same .env this whole dashboards project already uses.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

REDSHIFT_HOST = os.environ["REDSHIFT_HOST"]
REDSHIFT_PORT = int(os.environ.get("REDSHIFT_PORT", 5439))
REDSHIFT_DB = os.environ["REDSHIFT_DB"]
REDSHIFT_USER = os.environ["REDSHIFT_USER"]
REDSHIFT_PASS = os.environ["REDSHIFT_PASSWORD"]

MAX_ATTEMPTS = 3

# Output file -- one sheet, one query's worth of data. Where this file
# should ultimately live so the KPI Tracker automation script can pick it
# up is the next thing to wire up; for now it saves next to this script.
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "KPI_4Week_Metrics.xlsx")

QUERY = """
WITH time_period AS (
  SELECT
    %(day_start)s::DATE AS day_start,
    %(day_end)s::DATE AS day_end,
    %(day_end)s::DATE - 55 AS progress_start,
    %(day_end)s::DATE - 83 AS repurchase_start
	),
cte_sessions AS
(
	SELECT
		id,
		starts_at,
		attendances_count,
		attendances_attended_count,
		automatic_attendance,
		supervisor_id,
		duration,
		launched_at,
		course_id
	FROM dw.sessions
	WHERE 1=1
		AND sessions.starts_at::DATE >= (SELECT day_start FROM time_period)
		AND sessions.starts_at::DATE <= (SELECT day_end FROM time_period)
),
-- availability and delivery information
cte_avail_del AS
(
	SELECT
		tutor_capacity.employee_id AS employee_id,
		tutor_capacity.first_day_of_week_sunday_start AS first_day_of_week,
		tutor_capacity.instruction_actual AS delivery,
		SUM(COALESCE(tutor_availabilities_weekly.availability_hours,0)) AS availability
	FROM rp_bi.tutor_capacity
		LEFT JOIN rp_bi.tutor_availabilities_weekly
			ON (tutor_availabilities_weekly.employee_id = tutor_capacity.employee_id
			AND tutor_availabilities_weekly.first_day_of_week_sunday_start = tutor_capacity.first_day_of_week_sunday_start)
	WHERE 1=1
		AND tutor_capacity.first_day_of_week_sunday_start >= (SELECT day_start FROM time_period)
		AND tutor_capacity.first_day_of_week_sunday_start <= (SELECT day_end FROM time_period)
	GROUP BY tutor_capacity.employee_id,tutor_capacity.first_day_of_week_sunday_start, tutor_capacity.instruction_actual
),
-- prep time, attended, and unattended sessions
cte_prep1 AS
(
	SELECT
		employees.id AS tutor_id,
		cte_sessions.starts_at AS starts_at,
		CASE WHEN cte_sessions.attendances_attended_count = 0
			THEN 'Unattended Session'
			ELSE 'Attended Session'
		END AS type,
		cte_sessions.duration/60.0 AS duration_hours
	FROM cte_sessions
		JOIN dw.employees
			ON cte_sessions.supervisor_id = employees.id
	UNION
	SELECT
		employees.id AS tutor_id,
		events.starts_at AS starts_at,
		replace(events.type,'Event::','') AS type,
		events.duration/60.0 AS duration_hours
	FROM dw.events
		JOIN dw.employees
			ON (events.attendee_id = employees.id
			AND events.attendee_type = 'Employee')
	WHERE 1=1
		AND events.starts_at::DATE >= (SELECT day_start FROM time_period)
		AND events.starts_at::DATE <= (SELECT day_end FROM time_period)
		AND events.category in ('Session Preparation', 'Email or Slack Communication')
),
cte_prep2 AS
(
	SELECT
		cte_prep1.tutor_id,
		CASE WHEN cte_prep1.type = 'PrepTime'
			THEN SUM(cte_prep1.duration_hours)
		END AS session_and_email_prep,
		CASE WHEN cte_prep1.type = 'Unattended Session'
			THEN SUM(cte_prep1.duration_hours)
		END AS unattended_sessions,
		CASE WHEN cte_prep1.type = 'Attended Session'
			THEN SUM(cte_prep1.duration_hours)
		END AS Attended_Sessions
	FROM cte_prep1
	GROUP BY  cte_prep1.tutor_id,cte_prep1.type
),
-- launched on time data
cte_ontime AS
(
	SELECT
		cte_sessions.supervisor_id AS employee_id,
		COUNT(DISTINCT cte_sessions.id)*1.0 AS launched_sessions,
		COUNT(DISTINCT CASE WHEN ((EXTRACT(hours from cte_sessions.launched_at - cte_sessions.starts_at)*60 + EXTRACT(minutes from cte_sessions.launched_at - cte_sessions.starts_at)) < 2 )
			THEN cte_sessions.id END)
			AS ontime_sessions
	FROM cte_sessions
	WHERE 1 = 1
		AND cte_sessions.launched_at IS NOT NULL
	GROUP BY cte_sessions.supervisor_id
),
cte_attendance as
(
	select
		cte_sessions.supervisor_id as employee_id,
		count(cte_sessions.id) as autoattendance
	from cte_sessions
	where 1 = 1
		and cte_sessions.automatic_attendance is true
	group by cte_sessions.supervisor_id
),
--- Parent Updates
weekly_sessions AS
(
	SELECT
	dates.first_day_of_week_sunday_start,
	courses.id AS course_id,
	cte_sessions.supervisor_id,
	CASE
		WHEN brands.name = 'Trial' THEN cte_sessions.id
		WHEN brands.name IN ('Group Course','Small Group Course','Boot Camp','SGC','Boot Camps') THEN courses.id
		ELSE students.id
		END AS update_unit_id,
	CASE
		WHEN cte_sessions.attendances_attended_count > 0 THEN 1
		ELSE 0
		END AS update_required_flag
	FROM cte_sessions
		JOIN dw.courses
			ON cte_sessions.course_id = courses.id
		JOIN dw.brands
			ON brands.id = courses.brand_id
		JOIN dw.enrollments
			ON enrollments.course_id = courses.id
		JOIN dw.students
			ON students.id = enrollments.enrollee_id
		JOIN rp_bi.dates
			ON cte_sessions.starts_at::date = dates.full_date
	WHERE brands.name NOT IN ('Special Events','Seminar','Professional Development','1-on-1 Meetings','Group Meetings','Special Event','Self Study','Parent Event')
),
updates_sent AS
(
  -- Both types count for compliance rate
  SELECT DISTINCT
  	ca.employee_id,
  	s.course_id,
  	d.first_day_of_week_sunday_start
  FROM dw.contact_activities ca
  	JOIN cte_sessions s
  		ON ca.regarding_id = s.id
  	JOIN rp_bi.dates d
		ON ca.created_at::date = d.full_date
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Session'
    AND ca.created_at::date >=	(SELECT day_start FROM time_period)
	AND ca.created_at::date <= (SELECT day_end FROM time_period)
  UNION ALL
  -- Catch updates stored at course level (e.g. Progress Updates)
  SELECT DISTINCT ca.employee_id,
  	ca.regarding_id AS course_id,
  	d.first_day_of_week_sunday_start
  FROM dw.contact_activities ca
  	JOIN rp_bi.dates d
		ON CAST(ca.created_at AS DATE) = d.full_date
  WHERE ca.type = 'Contact::Message'
    AND ca.message_type IN ('Parent Update','Progress Update')
    AND ca.regarding_type = 'Course'
    AND CAST(ca.created_at AS DATE) >=	(SELECT day_start FROM time_period)
	AND CAST(ca.created_at AS DATE) <= (SELECT day_end FROM time_period)
),
tutor_updates AS
(
	  SELECT
		ws.supervisor_id,
		ws.first_day_of_week_sunday_start as week,
		COUNT(DISTINCT CASE
			WHEN ws.update_required_flag = 1
			THEN ws.update_unit_id
			END) AS updates_required,
		COUNT(DISTINCT CASE
			WHEN ws.update_required_flag = 1
			AND us.course_id IS NOT NULL
			THEN ws.update_unit_id
			END) AS updates_sent_on_time
	FROM weekly_sessions ws
		LEFT JOIN updates_sent us
			ON (ws.course_id = us.course_id
			AND ws.supervisor_id = us.employee_id
			AND ws.first_day_of_week_sunday_start = us.first_day_of_week_sunday_start)
	GROUP BY ws.supervisor_id,ws.first_day_of_week_sunday_start
),
cte_parent_updates AS
(
	SELECT
		supervisor_id,
		sum(updates_required),
		sum(updates_sent_on_time),
		ROUND(
		(SUM(updates_sent_on_time::decimal) / SUM(NULLIF(updates_required,0))),
		3
		) AS percent_parent_updates
	FROM tutor_updates
	WHERE updates_required >= 0
	GROUP BY supervisor_id
),
archivable_unscheduled AS
(
	SELECT
		tutoring_histories.tutor_id AS tutor_id,
		courses.id AS course_id,
		CASE WHEN MAX(sessions.starts_at) IS NULL
			THEN tutoring_histories.created_at < (getdate() -30)
			ELSE MAX(sessions.starts_at) < (getdate() -30)
			END AS should_archive,
		ROUND(courses.provisioned_duration/60.00,2) - ROUND(courses.delivered_duration/60.00,2) as hours_remaining,
		CASE WHEN (ROUND(courses.provisioned_duration/60.00,2) - ROUND(courses.duration/60.00,2))<0
			THEN 0
			ELSE ROUND(courses.provisioned_duration/60.00,2) - ROUND(courses.duration/60.00,2)
		END AS unscheduled_hours
	FROM dw.tutoring_histories
		JOIN dw.employees
			ON dw.employees.id = dw.tutoring_histories.tutor_id
		LEFT JOIN dw.enrollments
			ON dw.enrollments.id = dw.tutoring_histories.enrollment_id
		LEFT JOIN dw.sessions
			ON (sessions.course_id = enrollments.course_id
			AND sessions.supervisor_id = dw.employees.id)
		LEFT JOIN dw.courses
			ON enrollments.course_id = courses.id
	WHERE 1=1
		AND dw.tutoring_histories.active = TRUE
		AND enrollments.unenrolled_at IS NULL
		AND courses.brand_id in (2,41,42,43,47)
	GROUP BY tutoring_histories.tutor_id,unscheduled_hours,courses.id,courses.provisioned_duration,courses.duration,courses.delivered_duration,tutoring_histories.created_at
),
-- PPWs
first_course AS
(
	SELECT
		enrollments.enrollee_id AS student_id,
		sessions.id AS session_id
	FROM dw.sessions
		JOIN dw.courses
			ON (courses.id = sessions.course_id
			AND courses.brand_id in (2,41,42,43,47))
		JOIN dw.enrollments
			ON sessions.course_id = enrollments.course_id
	QUALIFY ROW_NUMBER() OVER (
   		PARTITION BY enrollments.enrollee_id
   		ORDER BY sessions.starts_at ASC, session_id ASC
	) = 1
),
first_session_details AS
(
	SELECT DISTINCT
		cte_sessions.supervisor_id AS tutor_id,
		first_course.student_id,
		CASE WHEN orbit_stitch.attachments.updated_at IS NOT null
				AND (EXTRACT(day from (orbit_stitch.attachments.created_at - cte_sessions.starts_at))*24 + EXTRACT(hour from (orbit_stitch.attachments.created_at - cte_sessions.starts_at))<72)
			THEN 1 ELSE 0
			END AS attachment_uploaded
	FROM first_course
		LEFT JOIN cte_sessions
			ON cte_sessions.id = first_course.session_id
		LEFT JOIN orbit_stitch.attachments
			ON (attachments.attachable_id = cte_sessions.id
			AND attachments.attachable_type = 'Session')
),
cte_progressupdates AS
(
	SELECT
		tutoring_histories.tutor_id AS employee_id,
		enrollments.course_id AS course_id,
		CASE WHEN EXTRACT(days from MAX(sessions.starts_at) - MAX(tutoring_histories.progress_update_last_sent_at))>60
				OR MAX(tutoring_histories.progress_update_last_sent_at) IS NULL
			THEN 'late'
		END AS lateprogress
	FROM dw.sessions
		JOIN dw.courses
			ON courses.id = sessions.course_id
		JOIN dw.enrollments
			ON enrollments.course_id = courses.id
		JOIN dw.tutoring_histories
			ON (tutoring_histories.enrollment_id = enrollments.id
			AND sessions.supervisor_id = tutoring_histories.tutor_id)
	WHERE 1=1
	    AND sessions.starts_at <= (SELECT day_end FROM time_period)
	    AND courses.brand_id IN (41,42,47,2)
	GROUP BY tutoring_histories.tutor_id, enrollments.course_id
	HAVING SUM(sessions.duration)/60.0 >= 6
		AND MAX(sessions.starts_at) >= (SELECT progress_start FROM time_period)
),
-- repurchase
cte_bookings AS
(
	SELECT
	bookings.student_id,
	bookings.id AS purchase_id,
	bookings.brand_id AS brand,
	bookings.duration/60.0 AS hours,
	bookings.booked_at AS booked_at,
	bookings.amount AS booking_amount
	FROM dw.bookings
	WHERE 1=1
		AND bookings.brand_id IN (2,41,42) -- limits to 1 on 1 tutoring, but not trials
		AND bookings.booked_at >= (SELECT time_period.repurchase_start FROM time_period)
		AND bookings.booked_at <= (SELECT time_period.day_end FROM time_period)
		AND bookings.item_type = 'TutorPackage'
		AND bookings.discount IS NULL
		AND bookings.amount > 0															-- must pay for the hours
	UNION
	--adds BUC purchases
	SELECT
		tutor_packages.student_id ,
		orbit_stitch.affiliate_reservations.id AS purchase_id,
		42 AS brand,
		orbit_stitch.affiliate_reservations.number_of_hours AS hours,
		orbit_stitch.affiliate_reservations.created_at AS created_at,
		orbit_stitch.affiliate_reservations.number_of_hours*39 AS booking_amount
	FROM orbit_stitch.affiliate_reservations
		LEFT JOIN dw.tutor_packages
			ON affiliate_reservations.tutor_package_id = tutor_packages.id
	WHERE 1=1
		AND orbit_stitch.affiliate_reservations.created_at >= (select time_period.repurchase_start from time_period)
		AND orbit_stitch.affiliate_reservations.created_at <= (select time_period.day_end from time_period)
		AND orbit_stitch.affiliate_reservations.status <> 'canceled'
	-- Adding school pay private tutoring hour transfers
	UNION ALL
	Select
		tp.student_id ,
		tp.id AS purchase_id,
		47 AS brand,
		tp.duration/60.0 AS hours,
		tp.won_at AS created_at,
		0 AS booking_amount
	FROM dw.tutor_packages tp
	WHERE tp.transfer_type = 'School Pay'
),
cte_first_brand_session AS
(
	SELECT
		cte_bookings.student_id AS student_id,
		cte_bookings.booked_at,
		sessions.supervisor_id  AS tutor_id,
		courses.brand_id as brand,
		MIN(sessions.starts_at) AS first_session,
		MIN(CASE WHEN sessions.attendances_attended_count >0
			THEN sessions.starts_at
			END) AS first_attended_session,
		MAX(sessions.starts_at) AS last_scheduled_session
	FROM cte_bookings
		JOIN dw.students
			ON cte_bookings.student_id = students.id
		JOIN dw.enrollments
			ON students.id = enrollments.enrollee_id
		JOIN dw.courses
			ON enrollments.course_id = courses.id
		JOIN dw.sessions
			ON courses.id = sessions.course_id
	WHERE 1=1
		AND courses.brand_id IN (2,41,42,43,47)							-- includes trials for first_session consideration
	GROUP BY cte_bookings.student_id,
		cte_bookings.booked_at,
		sessions.supervisor_id,
		courses.brand_id
),
cte_new_student AS
(
	SELECT
		cte_first_brand_session.student_id,
		cte_first_brand_session.tutor_id,
		MIN(cte_first_brand_session.first_session) AS first_session,
		MIN(cte_first_brand_session.first_attended_session) AS first_attended_session,
		MAX(cte_first_brand_session.last_scheduled_session) AS last_scheduled_session
	FROM cte_first_brand_session
	GROUP BY cte_first_brand_session.student_id,
		cte_first_brand_session.tutor_id
),
cte_student_detail AS
(
	SELECT
		cte_first_brand_session.student_id,
		cte_first_brand_session.booked_at,
		COUNT(DISTINCT cte_first_brand_session.brand) AS past_brand_count,
		COUNT(DISTINCT cte_first_brand_session.tutor_id) AS past_tutor_count
	FROM cte_first_brand_session
	WHERE 1=1
		AND cte_first_brand_session.booked_at >= cte_first_brand_session.first_attended_session
	GROUP BY cte_first_brand_session.student_id,
		cte_first_brand_session.booked_at
),
cte_repurchases AS
(
SELECT DISTINCT
	cte_new_student.tutor_id,
	cte_new_student.student_id,
	student_users.first_name||' '||student_users.last_name AS student_name,
	cte_bookings.purchase_id,
	cte_bookings.booked_at::DATE AS booked_at_date,
	b.name AS booked_brand,
	cte_student_detail.past_brand_count ,
	cte_bookings.hours,
	cte_bookings.booking_amount
FROM cte_bookings
	JOIN cte_student_detail
		ON (cte_bookings.student_id = cte_student_detail.student_id
		AND cte_student_detail.booked_at = cte_bookings.booked_at)
	JOIN cte_new_student
		ON cte_new_student.student_id = cte_student_detail.student_id
	JOIN cte_first_brand_session
		ON (cte_first_brand_session.student_id = cte_student_detail.student_id
		AND cte_first_brand_session.tutor_id = cte_new_student.tutor_id
		AND cte_first_brand_session.brand = cte_bookings.brand)
	JOIN dw.students
		ON students.id = cte_bookings.student_id
	JOIN dw.users student_users
		ON students.user_id = student_users.id
	JOIN dw.brands b
		ON cte_bookings.brand = b.id
WHERE 1=1
	AND cte_new_student.first_attended_session <= cte_bookings.booked_at
	AND cte_first_brand_session.last_scheduled_session >= cte_bookings.booked_at
	),
cte_repurchase_scores AS
(
	SELECT
		cte_repurchases.tutor_id,
		SUM(cte_repurchases.hours) AS total_hours,
		SUM(cte_repurchases.booking_amount) AS total_bookings,
		COUNT(DISTINCT cte_repurchases.student_id) AS student_repurchase_count
	FROM cte_repurchases
	GROUP BY tutor_id
),
-- new and total students
cte_students AS
(
	SELECT DISTINCT
		sessions.supervisor_id AS tutor_id,
		enrollments.enrollee_id AS student_id,
		MIN(sessions.starts_at) AS first_session,
		MAX(sessions.starts_at) AS last_scheduled_session,
		CASE WHEN MIN(sessions.starts_at) >= (SELECT day_start FROM time_period)
			THEN 'new'
		END as new_student
	FROM dw.sessions
		JOIN dw.courses
			on sessions.course_id = courses.id
		JOIN dw.enrollments
			ON enrollments.course_id = courses.id
	WHERE 1=1
		AND courses.brand_id IN (2,41,42,43,47)
	GROUP BY supervisor_id, enrollments.enrollee_id
	HAVING MAX(sessions.starts_at::DATE) >= (SELECT day_start FROM time_period)
		AND MIN(sessions.starts_at::DATE) <= (SELECT day_end FROM time_period)
		AND COUNT(CASE WHEN sessions.starts_at::DATE BETWEEN (SELECT day_start FROM time_period) AND (SELECT day_end FROM time_period) THEN sessions.id END) >0
)
SELECT
	(SELECT day_start FROM time_period) AS period_start,
	(SELECT day_end FROM time_period) AS period_end,
	employees.id,
	tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
	tiers."name" AS tier,
	manager_users.first_name||' '||manager_users.last_name AS FL,
	date(employees.hire_date) AS hire_date,
	employees.delivery_target AS delivery_target,
		round(avg(cte_avail_del.delivery),2) AS average_delivery_actual, 										--weeks with 0 delivery count
	CASE WHEN employees.delivery_target >0 THEN round(avg(cte_avail_del.delivery)/(employees.delivery_target),4) END AS delivery_percent,
	employees.availability_target,
	round(avg(cte_avail_del.availability),2) AS average_availability_actual, 							-- weeks with 0 availability count
	CASE WHEN employees.availability_target >0 THEN round(avg(cte_avail_del.availability)/(employees.availability_target),4) END AS availability_percent,
	MAX(cte_prep2.session_and_email_prep) AS session_and_email_prep,
	cte_parent_updates.percent_parent_updates AS ParentUpdates,
	(cte_ontime.ontime_sessions/cte_ontime.launched_sessions)*1.00 AS SessionsOnTime,
	COUNT(DISTINCT CASE WHEN first_session_details.attachment_uploaded = 1
		THEN first_session_details.student_id
		END) AS completed_ppw,
	COUNT(DISTINCT first_session_details.student_id) AS required_ppw,
	CASE WHEN COUNT(cte_progressupdates.course_id)>0
		THEN 1.00 - (COUNT(cte_progressupdates.lateprogress)*1.00/COUNT(cte_progressupdates.course_id)*1.00)
	END AS ProgressUpdates,
	ISNULL(cte_repurchase_scores.total_hours,0) AS repurchase_hours,
	cte_attendance.autoattendance AS autoattenance_sessions,
	COUNT(DISTINCT CASE WHEN cte_students.new_student = 'new' THEN cte_students.student_id END) AS new_students,
	COUNT(DISTINCT cte_students.student_id) AS total_students,
	MAX(cte_prep2.attended_sessions) AS attended_sessions,
	MAX(cte_prep2.unattended_sessions)  AS unattended_sessions,
	CASE WHEN COUNT(DISTINCT archivable_unscheduled.course_id) >0
	THEN COUNT(DISTINCT CASE WHEN archivable_unscheduled.should_archive = TRUE
		THEN archivable_unscheduled.course_id END)/COUNT(DISTINCT archivable_unscheduled.course_id)::FLOAT
		END AS archivable_percent,
	CASE WHEN sum(archivable_unscheduled.hours_remaining) >0
		THEN SUM(archivable_unscheduled.unscheduled_hours)/SUM(archivable_unscheduled.hours_remaining) END AS unscheduled_percent
FROM dw.employees
		JOIN dw.users tutor_users
		ON employees.user_id = tutor_users.id
	JOIN dw.tiers
		ON employees.tier_id = tiers.id
	JOIN dw.team_members
		ON employees.id = team_members.member_id
	JOIN dw.teams
		ON team_members.team_id = teams.id
	JOIN dw.employees managers
		ON teams.manager_id = managers.id
	JOIN dw.users manager_users
		ON managers.user_id = manager_users.id
	LEFT JOIN cte_avail_del
		ON employees.id = cte_avail_del.employee_id
	LEFT JOIN cte_parent_updates
		ON employees.id = cte_parent_updates.supervisor_id
	LEFT JOIN cte_progressupdates
		ON employees.id = cte_progressupdates.employee_id
	LEFT JOIN cte_ontime
		ON employees.id = cte_ontime.employee_id
	LEFT JOIN first_session_details
		ON first_session_details.tutor_id = employees.id
	LEFT JOIN archivable_unscheduled
		ON archivable_unscheduled.tutor_id = employees.id
	LEFT JOIN cte_repurchase_scores
		ON employees.id = cte_repurchase_scores.tutor_id
	LEFT JOIN cte_prep2
		ON employees.id =cte_prep2.tutor_id
	LEFT JOIN cte_attendance
		ON dw.employees.id = cte_attendance.employee_id
	LEFT JOIN cte_students
		ON cte_students.tutor_id = dw.employees.id
WHERE 1 = 1
	AND employees.end_date IS NULL
	AND employees.delivery_target >0
	AND employees.type = 'Tutor'
	AND tutor_users.title = 'Tutor'
	AND team_members.team_id <> 14
GROUP BY employees.id,tutor_users.first_name,tutor_users.last_name,tiers."name",manager_users.first_name,manager_users.last_name,
employees.hire_date,employees.delivery_target,employees.availability_target,cte_parent_updates.percent_parent_updates,
cte_ontime.ontime_sessions,cte_ontime.launched_sessions,cte_attendance.autoattendance,cte_repurchase_scores.total_hours
ORDER BY tutor_name
"""


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get_conn():
    return psycopg2.connect(
        host=REDSHIFT_HOST, port=REDSHIFT_PORT,
        dbname=REDSHIFT_DB, user=REDSHIFT_USER,
        password=REDSHIFT_PASS, connect_timeout=30
    )


def main():
    day_start, day_end = parse_period_args()
    log(f"pull_kpi_4week_data.py -- starting (period {day_start} to {day_end})")

    conn = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            conn = get_conn()
            log(f"Connected to Redshift (attempt {attempt})")
            break
        except Exception as e:
            log(f"Connection attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
            if attempt < MAX_ATTEMPTS:
                import time
                time.sleep(15 * attempt)
            else:
                log("All connection attempts failed -- aborting.")
                sys.exit(1)

    try:
        df = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                log(f"Running KPI 4-week query (attempt {attempt})...")
                df = pd.read_sql(QUERY, conn, params={
                    "day_start": day_start.isoformat(),
                    "day_end": day_end.isoformat(),
                })
                log(f"Got {len(df)} rows.")
                break
            except Exception as e:
                log(f"Query attempt {attempt}/{MAX_ATTEMPTS} failed: {e}")
                if attempt < MAX_ATTEMPTS:
                    import time
                    time.sleep(15 * attempt)
                else:
                    log("All query attempts failed -- aborting.")
                    sys.exit(1)
    finally:
        conn.close()

    df.to_excel(OUTPUT_PATH, index=False, sheet_name="KPI_4Week")
    log(f"Saved -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
