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
    WITH cte_exams AS (
        SELECT
            exams_production.transcripts.id,
            exams_production.transcripts.created_at,
            orbit_production.students.id AS student_id,
            exams_production.exams.exam_type,
            exams_production.transcripts.attempt,
            exams_production.transcripts.score,
            exams_production.exams.form_code AS Exam_Code,
            exams_production.transcripts.complete,
            exams_production.transcripts.all_sections_scored,
            MAX(CASE WHEN exams_production.exams.exam_type = 'ACT' AND exams_production.subjects.name = 'English'
                THEN exams_production.transcript_subjects.scaled_score END) AS ACTEnglish,
            MAX(CASE WHEN exams_production.exams.exam_type = 'ACT' AND exams_production.subjects.name = 'Science'
                THEN exams_production.transcript_subjects.scaled_score END) AS ACTScience,
            MAX(CASE WHEN exams_production.exams.exam_type = 'ACT' AND exams_production.subjects.name LIKE ('Math%')
                THEN exams_production.transcript_subjects.scaled_score END) AS ACTMath,
            MAX(CASE WHEN exams_production.exams.exam_type = 'ACT' AND exams_production.subjects.name = 'Reading'
                THEN exams_production.transcript_subjects.scaled_score END) AS ACTReading,
            MAX(CASE WHEN exams_production.exams.exam_type LIKE '%SAT%' AND exams_production.subjects.name = 'Reading and Writing'
                THEN (CASE WHEN exams_production.transcript_subjects.scaled_score IS NULL
                    THEN RIGHT(exams_production.transcript_subjects.scaled_score_range,3)
                    ELSE exams_production.transcript_subjects.scaled_score END)
            END) AS SATReadingWritingHigh,
            MAX(CASE WHEN exams_production.exams.exam_type LIKE '%SAT%' AND exams_production.subjects.name = 'Reading and Writing'
                THEN (CASE WHEN exams_production.transcript_subjects.scaled_score IS NULL
                    THEN LEFT(exams_production.transcript_subjects.scaled_score_range,3)
                    ELSE exams_production.transcript_subjects.scaled_score END)
            END) AS SATReadingWritingLow,
            MAX(CASE WHEN exams_production.exams.exam_type LIKE '%SAT%' AND exams_production.subjects.name LIKE ('Math%')
                THEN (CASE WHEN exams_production.transcript_subjects.scaled_score IS NULL
                    THEN RIGHT(exams_production.transcript_subjects.scaled_score_range,3)
                    ELSE exams_production.transcript_subjects.scaled_score END)
            END) AS SATMathHigh,
            MAX(CASE WHEN exams_production.exams.exam_type LIKE '%SAT%' AND exams_production.subjects.name LIKE ('Math%')
                THEN (CASE WHEN exams_production.transcript_subjects.scaled_score IS NULL
                    THEN LEFT(exams_production.transcript_subjects.scaled_score_range,3)
                    ELSE exams_production.transcript_subjects.scaled_score END)
            END) AS SATMathLow
        FROM exams_production.transcripts
        JOIN exams_production.exams ON exams_production.transcripts.exam_id = exams_production.exams.id
        JOIN exams_production.users ON exams_production.transcripts.user_id = exams_production.users.id
        JOIN orbit_production.users ON orbit_production.users.id = exams_production.users.handle
        JOIN orbit_production.students ON orbit_production.users.id = orbit_production.students.user_id
        JOIN exams_production.transcript_subjects ON exams_production.transcript_subjects.transcript_id = exams_production.transcripts.id
        JOIN exams_production.exam_subjects ON exams_production.exam_subjects.id = exams_production.transcript_subjects.exam_subject_id
        JOIN exams_production.subjects ON exams_production.subjects.id = exams_production.exam_subjects.subject_id
        GROUP BY exams_production.transcripts.id
        UNION ALL
        SELECT
            orbit_production.study_area_snapshots.id AS id,
            orbit_production.study_area_snapshots.date AS created_at,
            orbit_production.students.id AS student_id,
            orbit_production.subject_translations.name AS exam_type,
            'n/a' AS attempt,
            CAST(orbit_production.study_area_snapshots.score AS decimal) AS score,
            CASE WHEN orbit_production.subject_translations.name = 'ACT' THEN 'Official Exam'
                ELSE orbit_production.study_area_snapshots.kind END AS Exam_Code,
            'n/a' AS complete,
            'n/a' AS all_sections_scored,
            CASE WHEN orbit_production.subject_translations.name LIKE '%ACT'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',4),'"',-1) END AS ACTEnglish,
            CASE WHEN orbit_production.subject_translations.name LIKE '%ACT' AND LENGTH(SUBSTRING_INDEX(SUBSTRING_INDEX(replace(orbit_production.study_area_snapshots.data,':',''),'"',16),'"',-1)) <4
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(replace(orbit_production.study_area_snapshots.data,':',''),'"',16),'"',-1) END AS ACTScience,
            CASE WHEN orbit_production.subject_translations.name LIKE '%ACT'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',8),'"',-1) END AS ACTMath,
            CASE WHEN orbit_production.subject_translations.name LIKE '%ACT'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',12),'"',-1) END AS ACTReading,
            CASE WHEN orbit_production.subject_translations.name LIKE '%SAT%'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',8),'"',-1) END AS SATReadingWritingHigh,
            CASE WHEN orbit_production.subject_translations.name LIKE '%SAT%'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',8),'"',-1) END AS SATReadingWritingLow,
            CASE WHEN orbit_production.subject_translations.name LIKE '%SAT%'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',4),'"',-1) END AS SATMathHigh,
            CASE WHEN orbit_production.subject_translations.name LIKE '%SAT%'
                THEN SUBSTRING_INDEX(SUBSTRING_INDEX(orbit_production.study_area_snapshots.data,'"',4),'"',-1) END AS SATMathLow
        FROM orbit_production.study_area_snapshots
        JOIN orbit_production.study_areas ON orbit_production.study_areas.id = orbit_production.study_area_snapshots.study_area_id
        JOIN orbit_production.subjects ON orbit_production.subjects.id = orbit_production.study_areas.subject_id
        JOIN orbit_production.subject_translations ON (orbit_production.subject_translations.subject_id = orbit_production.subjects.id AND orbit_production.subject_translations.locale = 'en')
        JOIN orbit_production.students ON orbit_production.study_areas.student_id = orbit_production.students.id
        JOIN orbit_production.users studentusers ON orbit_production.students.user_id = studentusers.id
        WHERE orbit_production.subject_translations.name IN ('ACT','SAT','Digital SAT','PSAT/NMSQT','Digital PSAT','Digital PSAT/NMSQT','PSAT','Digital ACT','PSAT 8/9')
    ),
    cte_sessions AS (
        SELECT
            enrollments.enrollee_id AS student_id,
            sessions.supervisor_id AS tutor_id,
            sessions.id AS session_id,
            sessions.starts_at,
            courses.brand_id,
            (courses.provisioned_duration - courses.delivered_duration)/60.0 AS remaining_hours,
            sa.subject_id,
            sessions.attendances_attended_count,
            sa.minutes / 60.0 AS hours
        FROM sessions
        JOIN courses ON sessions.course_id = courses.id
        JOIN enrollments ON enrollments.course_id = courses.id
        JOIN session_allotments sa ON sessions.id = sa.session_id
        WHERE courses.brand_id IN (2,41,42,43,44,47)
    ),
    cte_past_sessions AS (
        SELECT
            cte_sessions.student_id,
            cte_sessions.tutor_id,
            MIN(cte_sessions.starts_at) AS first_session,
            MAX(cte_sessions.starts_at) AS most_recent_session,
            SUM(cte_sessions.hours) AS attended_hours,
            CEILING(DATEDIFF(MAX(cte_sessions.starts_at), MIN(cte_sessions.starts_at))/7) AS weeks_attended,
            MAX(cte_sessions.remaining_hours) AS remaining_hours
        FROM cte_sessions
        WHERE cte_sessions.starts_at < CURRENT_DATE()
          AND cte_sessions.subject_id IN (43,316,342,195,50,51,315,356)
          AND cte_sessions.hours > 0
          AND cte_sessions.attendances_attended_count > 0
        GROUP BY student_id, tutor_id
    ),
    cte_last_45_days_brands AS (
        SELECT
            cte_sessions.student_id,
            CASE WHEN students.graduation_year < 20000
                THEN CAST(CONCAT(students.graduation_year,'-06-30') AS DATE)
                ELSE NULL END AS grad_date,
            cte_sessions.tutor_id,
            CASE WHEN cte_sessions.brand_id = 2 THEN COUNT(DISTINCT cte_sessions.session_id) END AS private_tutoring_sessions,
            CASE WHEN cte_sessions.brand_id = 42 THEN COUNT(DISTINCT cte_sessions.session_id) END AS buc_sessions,
            CASE WHEN cte_sessions.brand_id = 43 THEN COUNT(DISTINCT cte_sessions.session_id) END AS trial_sessions,
            CASE WHEN cte_sessions.brand_id = 41 THEN COUNT(DISTINCT cte_sessions.session_id) END AS academics_sessions,
            CASE WHEN cte_sessions.brand_id = 47 THEN COUNT(DISTINCT cte_sessions.session_id) END AS school_pay_sessions
        FROM cte_sessions
        JOIN students ON students.id = cte_sessions.student_id
        WHERE cte_sessions.starts_at >= CURRENT_DATE() - INTERVAL 46 DAY
          AND cte_sessions.starts_at < CURRENT_DATE()
          AND cte_sessions.attendances_attended_count > 0
          AND cte_sessions.subject_id IN (43,316,342,195,50,51,315,356)
          AND cte_sessions.hours > 0
        GROUP BY student_id, tutor_id, brand_id
    ),
    cte_future AS (
        SELECT
            cte_sessions.student_id,
            cte_sessions.tutor_id,
            MAX(cte_sessions.starts_at) AS last_scheduled_session,
            CASE WHEN cte_sessions.brand_id = 2 THEN COUNT(DISTINCT cte_sessions.session_id) END AS private_tutoring_sessions,
            CASE WHEN cte_sessions.brand_id = 42 THEN COUNT(DISTINCT cte_sessions.session_id) END AS buc_sessions,
            CASE WHEN cte_sessions.brand_id = 43 THEN COUNT(DISTINCT cte_sessions.session_id) END AS trial_sessions,
            CASE WHEN cte_sessions.brand_id = 47 THEN COUNT(DISTINCT cte_sessions.session_id) END AS school_pay_sessions
        FROM cte_sessions
        WHERE cte_sessions.starts_at >= CURRENT_DATE()
          AND cte_sessions.brand_id IN (2,42,43,47)
        GROUP BY student_id, tutor_id, brand_id
    ),
    cte_last_45_days_sessions AS (
        SELECT
            cte_sessions.student_id,
            cte_sessions.tutor_id,
            COUNT(DISTINCT cte_sessions.session_id) AS session_count
        FROM cte_sessions
        WHERE cte_sessions.starts_at >= CURRENT_DATE() - INTERVAL 46 DAY
          AND cte_sessions.starts_at < CURRENT_DATE()
          AND cte_sessions.attendances_attended_count > 0
          AND cte_sessions.subject_id IN (43,316,342,195,50,51,315,356)
          AND cte_sessions.hours > 0
        GROUP BY student_id, tutor_id
    )
    SELECT
        cte_past_sessions.tutor_id,
        CONCAT(tutor_users.first_name,' ',tutor_users.last_name) AS tutor_name,
        teams.name AS team_name,
        cte_past_sessions.student_id,
        CONCAT(student_users.first_name,' ',student_users.last_name) AS student_name,
        IFNULL(CASE WHEN DATEDIFF(cte_last_45_days_brands.grad_date, CURRENT_DATE()) >= 0
            THEN (12 - FLOOR(CAST(DATEDIFF(cte_last_45_days_brands.grad_date, CURRENT_DATE())/365 AS FLOAT)))
            ELSE (12 - CEILING(CAST(DATEDIFF(cte_last_45_days_brands.grad_date, CURRENT_DATE())/365 AS FLOAT)))
        END, NULL) AS grade_lvl,
        cte_past_sessions.first_session AS first_session_day,
        cte_past_sessions.most_recent_session,
        MAX(cte_future.last_scheduled_session) AS last_scheduled_session,
        cte_past_sessions.attended_hours AS attended_test_prep_hours,
        cte_past_sessions.weeks_attended,
        cte_past_sessions.attended_hours / cte_past_sessions.weeks_attended AS attended_velocity,
        cte_past_sessions.remaining_hours AS hours_remaining,
        COUNT(DISTINCT lds2.tutor_id) AS tutor_count,
        CASE WHEN MAX(cte_last_45_days_brands.private_tutoring_sessions) > 0
            OR MAX(cte_future.private_tutoring_sessions) > 0 THEN TRUE ELSE FALSE END AS private_tutoring,
        CASE WHEN MAX(cte_last_45_days_brands.buc_sessions) > 0
            OR MAX(cte_future.buc_sessions) > 0 THEN TRUE ELSE FALSE END AS buc,
        CASE WHEN MAX(cte_last_45_days_brands.school_pay_sessions) > 0
            OR MAX(cte_future.school_pay_sessions) > 0 THEN TRUE ELSE FALSE END AS school_pay,
        cte_exams.id AS exam_id,
        cte_exams.created_at AS exam_date,
        cte_exams.exam_type AS subject,
        cte_exams.Exam_Code AS exam_code,
        cte_exams.attempt,
        cte_exams.complete,
        cte_exams.all_sections_scored,
        CASE WHEN cte_exams.exam_type IN ('SAT','Digital SAT','PSAT/NMSQT','Digital PSAT','Digital PSAT/NMSQT','PSAT','PSAT 8/9')
            THEN (ROUND((cte_exams.SATMathLow + cte_exams.SATMathHigh + 1)/2, -1) + ROUND((cte_exams.SATReadingWritingLow + cte_exams.SATReadingWritingHigh + 1)/2, -1))
            ELSE cte_exams.score
        END AS score,
        cte_exams.ACTEnglish AS act_english,
        cte_exams.ACTMath AS act_math,
        cte_exams.ACTReading AS act_reading,
        cte_exams.ACTScience AS act_science,
        ROUND((cte_exams.SATMathLow + cte_exams.SATMathHigh + 1)/2, -1) AS sat_math,
        ROUND((cte_exams.SATReadingWritingLow + cte_exams.SATReadingWritingHigh + 1)/2, -1) AS sat_rw
    FROM cte_past_sessions
    JOIN cte_last_45_days_brands
        ON cte_past_sessions.student_id = cte_last_45_days_brands.student_id
        AND cte_past_sessions.tutor_id = cte_last_45_days_brands.tutor_id
    JOIN cte_last_45_days_sessions lds1
        ON cte_past_sessions.student_id = lds1.student_id
        AND cte_past_sessions.tutor_id = lds1.tutor_id
    JOIN cte_last_45_days_sessions lds2
        ON cte_past_sessions.student_id = lds2.student_id
    LEFT JOIN cte_future
        ON cte_past_sessions.student_id = cte_future.student_id
        AND cte_past_sessions.tutor_id = cte_future.tutor_id
    JOIN students ON cte_past_sessions.student_id = students.id
    JOIN users student_users ON students.user_id = student_users.id
    JOIN employees ON cte_past_sessions.tutor_id = employees.id
    JOIN users tutor_users ON employees.user_id = tutor_users.id
    JOIN team_members ON team_members.member_id = employees.id
    JOIN teams ON teams.id = team_members.team_id
    LEFT JOIN cte_exams ON students.id = cte_exams.student_id
    WHERE employees.end_date IS NULL
      AND team_members.member_type = 'Employee'
      AND tutor_users.title = 'Tutor'
      AND lds1.session_count > 1
      AND (cte_past_sessions.attended_hours >= 4 OR cte_past_sessions.weeks_attended > 3)
    GROUP BY cte_past_sessions.tutor_id, cte_past_sessions.student_id, cte_exams.id
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
        cursor.execute("USE orbit_production")
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




TEAM_MAP = {
    "Team Cross":        "Ela_Cross",
    "Team De Groot":     "Annelies_de_Groot",
    "Team Plamondon":    "Ian_Plamondon",
    "Team St. Marie":    "Geoff_St_Marie",
    "Team Haase-Alvey":  "Kristin_Haase_Alvey",
    "Team Pencak":       "Nikki_Pencak",
    "Team Marino":       "Katherine_Marino",
}

def generate_exam_reports(df):
    from datetime import timedelta
    import pandas as pd
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
        from reportlab.lib.units import inch
        import requests, base64, tempfile, os
    except ImportError as e:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Skipping PDF generation — missing library: {e}")
        return

    df = df.copy()
    df['exam_date'] = pd.to_datetime(df['exam_date'], errors='coerce', utc=True).dt.tz_localize(None)

    today = datetime.today()
    # Use the PREVIOUS complete week (Sun-Sat)
    days_since_sunday = today.weekday() + 1 if today.weekday() != 6 else 0
    this_sunday = (today - timedelta(days=days_since_sunday)).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end   = this_sunday  # previous Saturday midnight = this Sunday 00:00
    week_start = week_end - timedelta(days=7)

    def exam_family(subject):
        s = str(subject).upper()
        if 'ACT' in s: return 'ACT'
        if 'SAT' in s or 'PSAT' in s: return 'SAT'
        return subject

    def is_valid(row):
        try:
            if pd.isna(row['score']): return False
            fam = row['exam_family']
            if fam == 'SAT':
                return pd.notna(row['sat_math']) and float(row['sat_math']) >= 200 and pd.notna(row['sat_rw']) and float(row['sat_rw']) >= 200
            if fam == 'ACT':
                return pd.notna(row['act_english']) and float(row['act_english']) >= 10 and pd.notna(row['act_math']) and float(row['act_math']) >= 10
            return pd.notna(row['score']) and float(row['score']) > 0
        except: return False

    def safe_int(v):
        try:
            if pd.isna(v): return '—'
            return str(int(v))
        except: return '—'

    # Convert numeric columns
    for _col in ['score','sat_math','sat_rw','act_english','act_math','act_reading','act_science']:
        if _col in df.columns:
            df[_col] = pd.to_numeric(df[_col], errors='coerce')
    df['exam_family'] = df['subject'].apply(exam_family)
    df['valid'] = df.apply(is_valid, axis=1)
    this_week = df[(df['exam_date'] >= week_start) & (df['exam_date'] < week_end) & (df['valid'] == True)].copy()

    results = []
    for _, row in this_week.iterrows():
        prior = df[
            (df['student_id'] == row['student_id']) &
            (df['exam_family'] == row['exam_family']) &
            (df['exam_date'] < row['exam_date']) &
            (df['valid'] == True)
        ].sort_values('exam_date', ascending=False)
        prior_score = prior.iloc[0]['score'] if not prior.empty else None
        prior_date  = prior.iloc[0]['exam_date'] if not prior.empty else None
        prior_code  = prior.iloc[0]['exam_code'] if not prior.empty else None
        delta = round(row['score'] - prior_score, 0) if prior_score is not None else None
        results.append({
            'team': row['team_name'], 'tutor': row['tutor_name'], 'student': row['student_name'],
            'exam_family': row['exam_family'],
            'new_exam': str(row['exam_code'])[:22],
            'new_date': row['exam_date'].strftime('%m/%d/%y'),
            'new_score': safe_int(row['score']),
            'prior_exam': str(prior_code)[:22] if prior_code else '—',
            'prior_date': prior_date.strftime('%m/%d/%y') if prior_date is not None else '—',
            'prior_score': safe_int(prior_score),
            'delta': delta,
        })

    out = pd.DataFrame(results) if results else pd.DataFrame()

    def build_pdf(team_df, team_name, output_path):
        doc = SimpleDocTemplate(output_path, pagesize=letter,
                                topMargin=0.5*inch, bottomMargin=0.5*inch,
                                leftMargin=0.6*inch, rightMargin=0.6*inch)
        styles = getSampleStyleSheet()
        story = []
        title_style  = ParagraphStyle('title',  parent=styles['Heading1'], fontSize=16, textColor=colors.HexColor('#1e293b'), spaceAfter=4)
        sub_style    = ParagraphStyle('sub',    parent=styles['Normal'],   fontSize=10, textColor=colors.HexColor('#64748b'), spaceAfter=12)
        tutor_style  = ParagraphStyle('tutor',  parent=styles['Heading2'], fontSize=12, textColor=colors.HexColor('#1e293b'), spaceBefore=14, spaceAfter=6)
        footer_style = ParagraphStyle('footer', parent=styles['Normal'],   fontSize=8,  textColor=colors.HexColor('#94a3b8'))

        story.append(Paragraph(f"Weekly Exam Score Report — {team_name}", title_style))
        story.append(Paragraph(f"Week of {week_start.strftime('%B %d')} – {(week_end - timedelta(days=1)).strftime('%B %d, %Y')}", sub_style))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor('#e2e8f0')))
        story.append(Spacer(1, 12))

        if team_df.empty:
            story.append(Paragraph("No new exam scores this week.", styles['Normal']))
        else:
            for tutor, tdf in team_df.groupby('tutor'):
                story.append(Paragraph(tutor, tutor_style))
                table_data = [['Student', 'Type', 'New Exam', 'Date', 'Score', 'Prior Exam', 'Prior Date', 'Prior', 'Δ']]
                for _, row in tdf.sort_values('student').iterrows():
                    delta_str = ''
                    if row['delta'] is not None and not pd.isna(row['delta']):
                        delta_str = f"+{int(row['delta'])}" if row['delta'] > 0 else str(int(row['delta']))
                    table_data.append([
                        row['student'], row['exam_family'], row['new_exam'], row['new_date'],
                        row['new_score'], row['prior_exam'], row['prior_date'], row['prior_score'], delta_str
                    ])
                col_widths = [1.4*inch, 0.4*inch, 1.4*inch, 0.6*inch, 0.5*inch, 1.4*inch, 0.6*inch, 0.55*inch, 0.45*inch]
                t = Table(table_data, colWidths=col_widths, repeatRows=1)
                ts = TableStyle([
                    ('BACKGROUND',    (0,0), (-1,0), colors.HexColor('#1e293b')),
                    ('TEXTCOLOR',     (0,0), (-1,0), colors.white),
                    ('FONTNAME',      (0,0), (-1,0), 'Helvetica-Bold'),
                    ('FONTSIZE',      (0,0), (-1,-1), 8),
                    ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f8fafc')]),
                    ('GRID',          (0,0), (-1,-1), 0.5, colors.HexColor('#e2e8f0')),
                    ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
                    ('LEFTPADDING',   (0,0), (-1,-1), 4),
                    ('RIGHTPADDING',  (0,0), (-1,-1), 4),
                    ('TOPPADDING',    (0,0), (-1,-1), 4),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                ])
                for i, row in enumerate(table_data[1:], 1):
                    if row[8].startswith('+'):
                        ts.add('TEXTCOLOR', (8,i), (8,i), colors.HexColor('#15803d'))
                        ts.add('FONTNAME',  (8,i), (8,i), 'Helvetica-Bold')
                    elif row[8].startswith('-'):
                        ts.add('TEXTCOLOR', (8,i), (8,i), colors.HexColor('#b91c1c'))
                        ts.add('FONTNAME',  (8,i), (8,i), 'Helvetica-Bold')
                t.setStyle(ts)
                story.append(t)
                story.append(Spacer(1, 8))

        story.append(Spacer(1, 12))
        story.append(Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y at %I:%M %p')} · Revolution Prep", footer_style))
        doc.build(story)

    # Generate and push one PDF per team
    api_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    for team_name, slug in TEAM_MAP.items():
        team_df = out[out['team'] == team_name].copy() if not out.empty else pd.DataFrame()
        tmp = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
        tmp.close()
        try:
            build_pdf(team_df, team_name, tmp.name)
            with open(tmp.name, 'rb') as f:
                pdf_bytes = f.read()
            encoded = base64.b64encode(pdf_bytes).decode()
            gh_path = f"data/exam_reports/{slug}_exam_report.pdf"
            api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{gh_path}"
            r = requests.get(api_url, headers=api_headers, timeout=15)
            payload = {
                "message": f"exam report: {team_name} {week_start.strftime('%Y-%m-%d')}",
                "content": encoded,
            }
            if r.status_code == 200:
                payload["sha"] = r.json().get("sha")
            requests.put(api_url, headers=api_headers, json=payload, timeout=30).raise_for_status()
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Exam report pushed: {gh_path}")
        except Exception as e:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Failed to generate/push report for {team_name}: {e}")
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    def _run():
        df = fetch_exam_data()
        push_to_github(df)
        return df
    df = run_with_retry(_run)
    if df is not None:
        # Only generate exam report on Sundays (when previous week is complete)
        if datetime.today().weekday() == 6:  # 6 = Sunday
            try:
                generate_exam_reports(df)
            except Exception as e:
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] PDF generation failed: {e}")
        else:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Skipping exam report (runs Sundays only)")
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ Done.")


# ─────────────────────────────────────────────────────────────────────────────
# EXAM SCORE REPORT — Weekly PDF generation per team
# ─────────────────────────────────────────────────────────────────────────────

