"""
sync_exam_data.py
-----------------
Runs locally on a schedule (cron / Task Scheduler).
Queries the RP MySQL replica, saves exam data as a CSV,
and pushes it to a private GitHub repo so Streamlit Cloud
can read it without needing direct MySQL access.

SETUP (one time):
  pip install mysql-connector-python pandas PyGithub python-dotenv

Create a .env file in the same directory as this script:
  RP_HOST=replica.revolutionprep.com
  RP_PORT=3306
  RP_USER=tyler.harrington
  RP_PASSWORD=your_password_here
  GITHUB_TOKEN=your_github_personal_access_token
  GITHUB_REPO=your-username/your-private-repo-name
  GITHUB_FILE_PATH=data/exam_data.csv   # path inside the repo

HOW TO SCHEDULE (Mac):
  1. Open Terminal
  2. Run: crontab -e
  3. Add this line to run every day at 6am:
       0 6 * * * /usr/bin/python3 /path/to/sync_exam_data.py >> /path/to/sync_log.txt 2>&1
  4. Save and exit

HOW TO SCHEDULE (Windows):
  1. Open Task Scheduler
  2. Create Basic Task → Daily → Set time
  3. Action: Start a program → python.exe
  4. Arguments: C:\\path\\to\\sync_exam_data.py
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



# ── Load credentials from .env or environment ────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # If dotenv not installed, fall back to env vars

RP_HOST       = os.environ["RP_HOST"]
RP_PORT       = int(os.environ.get("RP_PORT", 3306))
RP_USER       = os.environ["RP_USER"]
RP_PASSWORD   = os.environ["RP_PASSWORD"]
GITHUB_TOKEN  = os.environ["GITHUB_TOKEN"]
GITHUB_REPO   = os.environ["GITHUB_REPO"]        # e.g. "tylerharrington/fl-dashboards-data"
GITHUB_PATH   = os.environ.get("GITHUB_FILE_PATH", "data/exam_data.csv")

QUERY = """
    with rp_exams as (
        select
            exams_production.transcripts.id,
            exams_production.transcripts.created_at,
            orbit_production.students.id as student_id,
            exams_production.exams.exam_type,
            exams_production.transcripts.attempt,
            exams_production.transcripts.score,
            exams_production.exams.form_code as Exam_Code,
            exams_production.transcripts.complete,
            exams_production.transcripts.all_sections_scored,
            CASE When exams_production.exams.exam_type = 'ACT' and exams_production.subjects.name = 'English'
            then exams_production.transcript_subjects.scaled_score END as ACTEnglish,
            CASE When exams_production.exams.exam_type = 'ACT' and exams_production.subjects.name = 'Science'
            then exams_production.transcript_subjects.scaled_score END as ACTScience,
            CASE When exams_production.exams.exam_type = 'ACT' and exams_production.subjects.name like ('Math%')
            then exams_production.transcript_subjects.scaled_score END as ACTMath,
            CASE When exams_production.exams.exam_type = 'ACT' and exams_production.subjects.name = 'Reading'
            then exams_production.transcript_subjects.scaled_score END as ACTReading,
            CASE When exams_production.exams.exam_type like '%SAT%' and exams_production.subjects.name = 'Reading and Writing'
            then (case when exams_production.transcript_subjects.scaled_score is null
                then right(exams_production.transcript_subjects.scaled_score_range,locate('..',exams_production.transcript_subjects.scaled_score_range)-1)
                else exams_production.transcript_subjects.scaled_score end)
            END as SATReadingWritingHigh,
            CASE When exams_production.exams.exam_type like '%SAT%' and exams_production.subjects.name = 'Reading and Writing'
            then (case when exams_production.transcript_subjects.scaled_score is null
                then left(exams_production.transcript_subjects.scaled_score_range,locate('..',exams_production.transcript_subjects.scaled_score_range)-1)
                else exams_production.transcript_subjects.scaled_score end)
            END as SATReadingWritingLow,
            CASE When exams_production.exams.exam_type like '%SAT%' and exams_production.subjects.name in ('Math', 'Mathematics')
            then (case when exams_production.transcript_subjects.scaled_score is null
                then right(exams_production.transcript_subjects.scaled_score_range,locate('..',exams_production.transcript_subjects.scaled_score_range)-1)
                else exams_production.transcript_subjects.scaled_score end)
            END as SATMathHigh,
            CASE When exams_production.exams.exam_type like '%SAT%' and exams_production.subjects.name in ('Math', 'Mathematics')
            then (case when exams_production.transcript_subjects.scaled_score is null
                then left(exams_production.transcript_subjects.scaled_score_range,locate('..',exams_production.transcript_subjects.scaled_score_range)-1)
                else exams_production.transcript_subjects.scaled_score end)
            END as SATMathLow
        from exams_production.transcripts
        join exams_production.exams on exams_production.transcripts.exam_id = exams_production.exams.id
        join exams_production.users on exams_production.transcripts.user_id = exams_production.users.id
        join orbit_production.users on orbit_production.users.id = exams_production.users.handle
        join orbit_production.students on orbit_production.users.id = orbit_production.students.user_id
        join exams_production.transcript_subjects on exams_production.transcript_subjects.transcript_id = exams_production.transcripts.id
        join exams_production.exam_subjects on exams_production.exam_subjects.id = exams_production.transcript_subjects.exam_subject_id
        join exams_production.subjects on exams_production.subjects.id = exams_production.exam_subjects.subject_id
        where exams_production.transcripts.created_at >= (curdate() - interval 2 year)
    ),
    cte_exams as (
        select
            rp_exams.id as id,
            rp_exams.Created_At as created_at,
            rp_exams.Student_ID as Student_ID,
            rp_exams.exam_type as exam_type,
            rp_exams.Exam_Code as Exam_Code,
            rp_exams.attempt as attempt,
            rp_exams.complete as complete,
            rp_exams.all_sections_scored,
            rp_exams.score as score,
            max(rp_exams.ACTEnglish) as ACTEnglish,
            max(rp_exams.ACTMath) as ACTMath,
            max(rp_exams.ACTReading) as ACTReading,
            max(rp_exams.ACTScience) as ACTScience,
            round((max(rp_exams.SATMathHigh) + max(rp_exams.SATMathLow)+1)/2,-1) as SATMath,
            round((max(rp_exams.SATReadingWritingHigh) + max(rp_exams.SATReadingWritingLow)+1)/2,-1) as SATReadingWriting
        FROM rp_exams
        group by 1,2,3,4,5,6,7,8,9
        UNION
        select
            orbit_production.study_area_snapshots.id as id,
            orbit_production.study_area_snapshots.date as created_at,
            orbit_production.students.id as student_id,
            orbit_production.subject_translations.name as exam_type,
            CASE When orbit_production.subject_translations.name = 'ACT' then 'Official Exam'
            Else orbit_production.study_area_snapshots.kind END as Exam_Code,
            'n/a' as attempt,
            'n/a' as complete,
            'n/a' as all_sections_scored,
            cast(orbit_production.study_area_snapshots.score as decimal) as score,
            CASE When orbit_production.subject_translations.name like '%ACT'
            then substring_index(substring_index(orbit_production.study_area_snapshots.data,'"',4),'"',-1) END as ACTEnglish,
            CASE When orbit_production.subject_translations.name like '%ACT'
            then substring_index(substring_index(orbit_production.study_area_snapshots.data,'"',8),'"',-1) END as ACTMath,
            CASE When orbit_production.subject_translations.name like '%ACT'
            then substring_index(substring_index(orbit_production.study_area_snapshots.data,'"',12),'"',-1) END as ACTReading,
            CASE When orbit_production.subject_translations.name like '%ACT' and length(substring_index(substring_index(replace(orbit_production.study_area_snapshots.data,':',''),'"',16),'"',-1)) <4
            then substring_index(substring_index(replace(orbit_production.study_area_snapshots.data,':',''),'"',16),'"',-1) END as ACTScience,
            CASE When orbit_production.subject_translations.name like '%SAT%'
            then substring_index(substring_index(orbit_production.study_area_snapshots.data,'"',4),'"',-1) END as SATMath,
            CASE When orbit_production.subject_translations.name like '%SAT%'
            then substring_index(substring_index(orbit_production.study_area_snapshots.data,'"',8),'"',-1) END as SATReadingWriting
        from orbit_production.study_area_snapshots
        join orbit_production.study_areas on orbit_production.study_areas.id = orbit_production.study_area_snapshots.study_area_id
        join orbit_production.subjects on orbit_production.subjects.id = orbit_production.study_areas.subject_id
        join orbit_production.subject_translations on (orbit_production.subject_translations.subject_id = orbit_production.subjects.id and orbit_production.subject_translations.locale = 'en')
        join orbit_production.students on orbit_production.study_areas.student_id = orbit_production.students.id
        join orbit_production.users studentusers on orbit_production.students.user_id = studentusers.id
        and orbit_production.subject_translations.name in ('ACT', 'SAT', 'Digital SAT', 'PSAT/NMSQT', 'Digital PSAT','Digital PSAT/NMSQT','PSAT','Digital ACT', 'PSAT 8/9')
        where orbit_production.study_area_snapshots.date >= (curdate() - interval 2 year)
    )
    select distinct
        orbit_production.employees.id as tutor_id,
        concat(tutor_users.first_name,' ', tutor_users.last_name) as tutor_name,
        orbit_production.teams.name as team_name,
        orbit_production.students.id as student_id,
        concat(student_users.first_name,' ', student_users.last_name) as student_name,
        min(orbit_production.sessions.starts_at) as first_session_day,
        max(orbit_production.sessions.starts_at) as most_recent_session,
        SUM(orbit_production.sessions.duration/60.0) as test_prep_hours_delivered,
        cte_exams.id as exam_id,
        cte_exams.Created_At as exam_date,
        cte_exams.exam_type as subject,
        cte_exams.Exam_Code as exam_code,
        cte_exams.attempt as attempt,
        cte_exams.complete as complete,
        cte_exams.all_sections_scored,
        case when cte_exams.exam_type in ('SAT', 'Digital SAT', 'PSAT/NMSQT', 'Digital PSAT','Digital PSAT/NMSQT','PSAT', 'PSAT 8/9')
            then cte_exams.SATMath + cte_exams.SATReadingWriting
            else cte_exams.score
        end as score,
        cte_exams.ACTEnglish as act_english,
        cte_exams.ACTMath as act_math,
        cte_exams.ACTReading as act_reading,
        cte_exams.ACTScience as act_science,
        cte_exams.SATMath as sat_math,
        cte_exams.SATReadingWriting as sat_rw
    from orbit_production.tutoring_histories
    JOIN orbit_production.enrollments ON orbit_production.enrollments.id = orbit_production.tutoring_histories.enrollment_id
    join orbit_production.sessions on (orbit_production.sessions.course_id = orbit_production.enrollments.course_id)
        and (orbit_production.sessions.supervisor_id = orbit_production.tutoring_histories.tutor_id)
    join orbit_production.session_allotments sa on orbit_production.sessions.id = sa.session_id
    join orbit_production.students on orbit_production.enrollments.enrollee_id = orbit_production.students.id
    join orbit_production.users student_users on orbit_production.students.user_id = student_users.id
    join orbit_production.employees on orbit_production.tutoring_histories.tutor_id = orbit_production.employees.id
    join orbit_production.users tutor_users on orbit_production.employees.user_id = tutor_users.id
    JOIN orbit_production.team_members ON orbit_production.team_members.member_id = orbit_production.employees.id
    JOIN orbit_production.teams ON orbit_production.teams.id = orbit_production.team_members.team_id
    left join cte_exams on cte_exams.student_id = orbit_production.students.id
    where 1=1
    AND orbit_production.tutoring_histories.active = true
    AND orbit_production.enrollments.unenrolled_at IS null
    AND orbit_production.employees.end_date IS null
    AND orbit_production.team_members.member_type = 'Employee'
    and sa.subject_id in (43,316,342,195,50,51,315,356)
    group by 1,2,3,4,5,9,10,11,12,13,14,15,16,17,18,19,20,21,22
    having max(orbit_production.sessions.starts_at) > (curdate() - interval 30 day)
       and min(orbit_production.sessions.starts_at) <= curdate()
"""


def fetch_exam_data():
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Connecting to MySQL...")
    conn = mysql.connector.connect(
        host=RP_HOST,
        port=RP_PORT,
        user=RP_USER,
        password=RP_PASSWORD,
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
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Fetched {len(df):,} rows.")
    return df


def push_to_github(df):
    from github import Github
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Pushing to GitHub ({GITHUB_REPO}/{GITHUB_PATH})...")

    # Add a fetched_at column so the app knows when data was last refreshed
    df["fetched_at"] = datetime.now().strftime("%B %d, %Y at %I:%M %p")

    csv_bytes = df.to_csv(index=False).encode("utf-8")

    g    = Github(GITHUB_TOKEN)
    repo = g.get_repo(GITHUB_REPO)

    commit_msg = f"Auto-update exam data — {datetime.now():%Y-%m-%d %H:%M}"

    try:
        # File already exists — update it
        existing = repo.get_contents(GITHUB_PATH)
        repo.update_file(GITHUB_PATH, commit_msg, csv_bytes, existing.sha)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Updated existing file.")
    except Exception:
        # File doesn't exist yet — create it
        repo.create_file(GITHUB_PATH, commit_msg, csv_bytes)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Created new file.")


if __name__ == "__main__":
    def _run():
        df = fetch_exam_data()
        push_to_github(df)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ Done.")
    run_with_retry(_run)
