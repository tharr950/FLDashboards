import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import os
import io
import psycopg2

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
# SNAPSHOT HELPERS
# ─────────────────────────────────────────────

SNAPSHOT_FILE        = "geoff_archive_snapshots.csv"
GRADES_SNAPSHOT_FILE = "geoff_grades_snapshots.csv"


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
      - stale_grade_students  : students whose most recent grade update (any subject) is >90 days old
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
        total_students    = tdf["student_id"].nunique()
        no_grade_students = tdf[tdf["score"].isna()]["student_id"].nunique()

        # Stale = most recent updated_at across ALL subjects for a student is >90 days ago.
        # Only consider students who have at least one grade entered.
        has_any_grade      = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
        graded_student_ids = has_any_grade[has_any_grade].index
        graded_df          = tdf[tdf["student_id"].isin(graded_student_ids)].copy()
        if not graded_df.empty and "updated_at" in graded_df.columns:
            graded_df["updated_at"] = pd.to_datetime(graded_df["updated_at"], errors="coerce", utc=True)
            graded_df["days_since"] = (now.tz_localize("UTC") - graded_df["updated_at"]).dt.days
            latest_per_student = graded_df.groupby("student_id")["days_since"].min()
            stale_count        = int((latest_per_student > 90).sum())
            avg_days           = round(latest_per_student.mean(), 1)
        else:
            stale_count = 0
            avg_days    = None

        per_student = tdf.groupby("student_id").apply(
            lambda g: g["score"].notna().sum() / len(g) * 100 if len(g) > 0 else 0
        )
        pct_graded = round(per_student.mean(), 1)

        rows.append({
            "tutor_name":            tutor,
            "total_students":        total_students,
            "students_no_grades":    no_grade_students,
            "pct_subjects_graded":   pct_graded,
            "stale_grade_students":  stale_count,
            "avg_days_since_update": avg_days,
            "week_key":              week_key,
            "week_date":             week_date,
        })

    summary = pd.DataFrame(rows)
    updated  = pd.concat([existing, summary], ignore_index=True) if not existing.empty else summary
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


# ─────────────────────────────────────────────
# FILE-BASED DATA LOADERS
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
    file = "Geoff_GradesSummary.xlsx"
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

    st.markdown('<div class="main-title">Geoff Tutor Data 📊</div>', unsafe_allow_html=True)

    # Sidebar Navigation
    page = st.sidebar.radio("📂 Navigation", [
        "Concerns",
        "KPI Table",
        "KPI Trends",
        "Grades Summary",
        #"Annual Reviews",
        "Archivable Students & Unscheduled Hours"
    ])

    faculty_leader_name = "Geoff St. Marie"
    master_tutor_df     = load_master_tutor()
    annelies_tutors     = master_tutor_df[master_tutor_df["Faculty Leader"] == faculty_leader_name]["Full Name"].sort_values().dropna().unique().tolist()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📋 Annual Reviews")

    annual_review_df                = load_annual_reviews()
    monthly_metric_annual_review_df = load_monthly_metric_annual_reviews()
    repurchase_df                   = load_repurchases()
    annelies_tutors                 = master_tutor_df[master_tutor_df["Faculty Leader"] == "Geoff St. Marie"]["Full Name"].dropna().sort_values().tolist()


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
            "Students who have never had a grade entered are also surfaced.\n\n"
            "**Once a week, this data is captured and stored. You will see trend data in the 'Tutor Breakdown' tab once at least 2 weeks of data are captured.**",
            icon=None
        )

        st.info(
            "ℹ️ **Stale Grades**\n\n"
            "Stale Grades are grades that have not been updated for >90 days. "
            "For the student count: **If a student has ANY subject updated within the past 90 days, "
            "that student is not considered stale.**",
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

        # Filter to Team St. Marie
        team_grades_df = raw_grades_df[raw_grades_df["team_name"] == "Team St. Marie"].copy()

        if team_grades_df.empty:
            st.warning("No grades records found for Team St. Marie.")
            st.stop()

        # ── Normalise date columns ────────────────────
        now = pd.Timestamp.now(tz="UTC")
        team_grades_df["updated_at"]        = pd.to_datetime(team_grades_df["updated_at"],        errors="coerce", utc=True)
        team_grades_df["first_session_day"] = pd.to_datetime(team_grades_df["first_session_day"], errors="coerce", utc=True)
        team_grades_df["last_session_day"]  = pd.to_datetime(team_grades_df["last_session_day"],  errors="coerce", utc=True)
        team_grades_df["days_since_update"] = (now - team_grades_df["updated_at"]).dt.days

        # ── Save weekly snapshot ──────────────────────
        grades_snap_df = save_grades_weekly_snapshot(team_grades_df)

        # ─────────────────────────────────────────────
        # TOP CONCERN FLAGS
        # ─────────────────────────────────────────────
        st.markdown("### 🚨 Top Tutors to Address")

        def build_tutor_summary(df):
            rows = []
            for tutor, tdf in df.groupby("tutor_name"):
                total_students    = tdf["student_id"].nunique()

                no_grade_ids      = tdf.groupby("student_id")["score"].apply(lambda s: s.isna().all())
                no_grade_students = int(no_grade_ids.sum())

                pct_graded = (
                    tdf["score"].notna().sum() / len(tdf) * 100
                    if len(tdf) > 0 else 0
                )

                # Stale = most recent updated_at across ALL subjects for a student is >90 days.
                # Only applies to students who have at least one grade entered.
                has_any_grade = tdf.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_ids    = has_any_grade[has_any_grade].index
                graded        = tdf[tdf["student_id"].isin(graded_ids)]
                if not graded.empty:
                    latest_per_student = graded.groupby("student_id")["days_since_update"].min()
                    stale_students     = int((latest_per_student > 90).sum())
                    avg_days           = round(latest_per_student.mean(), 1)
                else:
                    stale_students = 0
                    avg_days       = None

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
                .sort_values("students_no_grades", ascending=False)
                .head(5)
            )
            if top_no_grades.empty:
                st.success("✅ All students have at least one grade entered.")
            else:
                for rank, (_, row) in enumerate(top_no_grades.iterrows()):
                    st.markdown(
                        f"{medals[rank]} **{row['tutor_name']}** — "
                        f"<span style='color:#cc0000; font-weight:bold'>{int(row['students_no_grades'])} students</span>",
                        unsafe_allow_html=True
                    )

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
                        f"<span style='color:#b35c00; font-weight:bold'>{int(row['stale_grade_students'])} students</span>",
                        unsafe_allow_html=True
                    )

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
                        f"<span style='color:#555; font-weight:bold'>{row['pct_subjects_graded']:.1f}%</span>",
                        unsafe_allow_html=True
                    )

        st.divider()

        # ─────────────────────────────────────────────
        # TEAM-LEVEL SUMMARY METRICS
        # ─────────────────────────────────────────────
        st.markdown("### 📊 Team Overview")

        total_students_team = team_grades_df["student_id"].nunique()
        no_grades_team      = int(
            team_grades_df.groupby("student_id")["score"]
            .apply(lambda s: s.isna().all()).sum()
        )
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
        m2.metric("Students — No Grades",     no_grades_team,
                  delta=f"{no_grades_team/total_students_team*100:.0f}% of roster" if total_students_team else None,
                  delta_color="inverse")
        m3.metric("% Subject Rows Graded",    f"{pct_graded_team:.1f}%")
        m4.metric("Students w/ Stale Grades", stale_team,
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
            missing_ids    = (
                view_grades_df.groupby("student_id")["score"]
                .apply(lambda s: s.isna().all())
            )
            missing_ids    = missing_ids[missing_ids].index
            view_grades_df = view_grades_df[view_grades_df["student_id"].isin(missing_ids)]
        elif sel_grade_filter == "Stale Grades Only (>90 days)":
            has_any_grade_view = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
            graded_ids_view    = has_any_grade_view[has_any_grade_view].index
            graded_view        = view_grades_df[view_grades_df["student_id"].isin(graded_ids_view)]
            if not graded_view.empty:
                latest_per     = graded_view.groupby("student_id")["days_since_update"].min()
                stale_ids      = latest_per[latest_per > 90].index
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

                no_grades_chart = (
                    tutor_summary[tutor_summary["students_no_grades"] > 0]
                    .sort_values("students_no_grades", ascending=True)
                )
                if not no_grades_chart.empty:
                    n    = len(no_grades_chart)
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

                pct_chart = tutor_summary.sort_values("pct_subjects_graded", ascending=True)
                n         = len(pct_chart)
                fig2      = px.bar(
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

                stale_chart = (
                    tutor_summary[tutor_summary["stale_grade_students"] > 0]
                    .sort_values("stale_grade_students", ascending=True)
                )
                if not stale_chart.empty:
                    n    = len(stale_chart)
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
                sel_summary = tutor_summary[tutor_summary["tutor_name"] == sel_tutor_g]
                if not sel_summary.empty:
                    row = sel_summary.iloc[0]
                    sc1, sc2, sc3, sc4 = st.columns(4)
                    sc1.metric("Total Students",       int(row["total_students"]))
                    sc2.metric("No Grades",            int(row["students_no_grades"]), delta_color="inverse")
                    sc3.metric("% Subjects Graded",    f"{row['pct_subjects_graded']:.1f}%")
                    sc4.metric("Stale Grade Students", int(row["stale_grade_students"]), delta_color="inverse")

                has_grade        = view_grades_df.groupby("student_id")["score"].apply(lambda s: s.notna().any())
                graded_ids_tutor = has_grade[has_grade].index
                graded_tutor     = view_grades_df[view_grades_df["student_id"].isin(graded_ids_tutor)].copy()
                if not graded_tutor.empty:
                    per_student_days = (
                        graded_tutor.groupby(["student_id","student_name"])["days_since_update"]
                        .min().reset_index()
                        .sort_values("days_since_update", ascending=True)
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

            tutors_to_show = (
                [sel_tutor_g] if single_tutor_grades
                else sorted(team_grades_df["tutor_name"].dropna().unique().tolist())
            )

            trend_metric = st.selectbox(
                "Trend metric",
                ["students_no_grades", "stale_grade_students", "pct_subjects_graded", "avg_days_since_update"],
                format_func=lambda x: {
                    "students_no_grades":    "Students With No Grades",
                    "stale_grade_students":  "Students With Stale Grades (>90d)",
                    "pct_subjects_graded":   "% Subjects Graded",
                    "avg_days_since_update": "Avg Days Since Last Update"
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
                detail_cols    = [
                    "tutor_name", "student_name", "subject",
                    "score", "updated_at", "days_since_update",
                    "first_session_day", "last_session_day"
                ]
                detail_cols    = [c for c in detail_cols if c in view_grades_df.columns]
                detail_display = view_grades_df[detail_cols].copy()

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
                    days  = row.get("Days Since Update")
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

                output_g = io.BytesIO()
                detail_display.to_excel(output_g, index=False)
                output_g.seek(0)
                st.download_button(
                    label="⬇️ Download Grades Detail",
                    data=output_g,
                    file_name="Grades_Detail_TeamStMarie.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

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
            "These students should be reviewed and archived so tutors' dashboards reflect only truly active students.\n\n"
            "**Once a week, this data is captured and stored. You will see trend data per tutor once at least 2 weeks of data are captured.**",
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

        full_team_df = raw_df[raw_df["team_name"] == "Team St. Marie"].copy()

        if full_team_df.empty:
            st.warning("No records found for Team St. Marie.")
            st.stop()

        snapshots_df = save_weekly_snapshot(full_team_df)

        def horizontal_bar(df, x_col, y_col, color_scale, title, x_label, height=None):
            n   = len(df)
            h   = height or max(350, n * 28)
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
                    rank  = top_archive.index.get_loc(i) + 1
                    medal = ["🥇","🥈","🥉","4️⃣","5️⃣"][rank - 1]
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
                    file_name="Archivable_Students_TeamStMarie.xlsx",
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
                    file_name="Unscheduled_Hours_TeamStMarie.xlsx",
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
            tutor_review                = annual_review_df[annual_review_df["tutor_name"] == selected_annual_tutor]
            tutor_review_repurchase     = repurchase_df[repurchase_df["Tutor Name"] == selected_annual_tutor]
            tutor_review_monthly_metric = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tutor Name"] == selected_annual_tutor]

            if not tutor_review.empty:
                row       = tutor_review.iloc[0]
                tutor_tier = row["tier"]

                row_repurchase        = tutor_review_repurchase.iloc[0]
                tutor_tier_repurchase = row_repurchase["Current Tier"]
                tutor_deliverytarget  = row_repurchase["Delivery Target"]

                row_monthly_metric        = tutor_review_monthly_metric
                tutor_tier_monthly_metric = row_monthly_metric["Tier"].iloc[0]

                team_df = annual_review_df[annual_review_df["fl"] == "Geoff St. Marie"]
                tier_df = annual_review_df[annual_review_df["tier"] == tutor_tier]

                team_repurchase_df         = repurchase_df[repurchase_df["Team Name"] == "Team St. Marie"]
                tier_repurchase_df         = repurchase_df[repurchase_df["Current Tier"] == tutor_tier]
                tierdelivery_repurchase_df = repurchase_df[
                    (repurchase_df["Current Tier"] == tutor_tier) &
                    (repurchase_df["Delivery Target"] == tutor_deliverytarget)
                ]

                team_monthly_metric_df = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Faculty Leader"] == "Geoff St. Marie"]
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
                            tutor_value_plot    = tutor_value_monthly_metric * 100
                            team_avg            = team_monthly_metric_df[col].mean() * 100
                            tier_avg            = tier_monthly_metric_df[col].mean() * 100
                        else:
                            tutor_value_display = f"{tutor_value * 100:.0f}%"
                            tutor_value_plot    = tutor_value * 100
                            team_avg            = team_df[col].mean() * 100
                            tier_avg            = tier_df[col].mean() * 100
                    else:
                        if col in ["Repurchases Weighted"]:
                            tutor_value_display = f"{tutor_value_repurchase:.1f}"
                            tutor_value_plot    = tutor_value_repurchase
                            team_avg            = tierdelivery_repurchase_df[col].mean()
                            tier_avg            = tier_repurchase_df[col].mean()
                        else:
                            tutor_value_display = f"{tutor_value:.1f}"
                            tutor_value_plot    = tutor_value
                            team_avg            = team_df[col].mean()
                            tier_avg            = tier_df[col].mean()

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

        selected_tutor = st.selectbox("Select a Tutor:", annelies_tutors)

        if selected_tutor:
            tutor_df   = monthly_df[monthly_df["Tutor Name"] == selected_tutor].copy()
            tutor_tier = annual_df.loc[annual_df["tutor_name"] == selected_tutor, "tier"].values
            tutor_tier = tutor_tier[0] if len(tutor_tier) > 0 else None

            import re
            def extract_end_date(range_str):
                if pd.isna(range_str):
                    return pd.NaT
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts     = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            tutor_df["Date Parsed"] = tutor_df["Date Range"].apply(extract_end_date)

            geoff_team = master_df[master_df["Faculty Leader"] == "Geoff St. Marie"]["Full Name"].dropna()
            team_df    = monthly_df[monthly_df["Tutor Name"].isin(geoff_team)].copy()
            team_df["Date Parsed"] = team_df["Date Range"].apply(extract_end_date)

            if tutor_tier:
                tier_tutors = annual_df[annual_df["tier"] == tutor_tier]["tutor_name"]
                tier_df     = monthly_df[monthly_df["Tutor Name"].isin(tier_tutors)].copy()
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
                if pd.isna(range_str):
                    return pd.NaT
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts     = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            fl_df["Date"] = fl_df["Date"].apply(extract_end_date)
            latest_date   = fl_df["Date"].max()
            latest_df     = fl_df[fl_df["Date"] == latest_date]

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
        latest_range_parsed     = df["Date Range Parsed"].max()
        latest_range            = df.loc[df["Date Range Parsed"] == latest_range_parsed, "Date Range"].iloc[0]
        leader_name             = "Geoff St. Marie"
        team_df                 = df[(df["Date Range"] == latest_range) & (df["Faculty Leader"] == leader_name)].copy()

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
        n_metrics   = len(metrics)
        n_cols      = 3
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

        st.divider()
        st.divider()
        st.subheader("📊 Team KPI Changes from Previous Period")

        df["Date Range Parsed"] = pd.to_datetime(df["Date Range"].str.split(" - ").str[0], errors="coerce")
        date_ranges_sorted      = df.sort_values("Date Range Parsed")["Date Range"].dropna().unique().tolist()

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
                "Metric":              metrics,
                f"{prev_range} Avg":   prev_avg.values,
                f"{latest_range} Avg": latest_avg.values,
                "Change (pp)":         (latest_avg - prev_avg).values
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

            styled_df         = change_df[["Metric", f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]].copy()
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
            plot_df              = leader_group.reset_index().sort_values(by=metric, ascending=False)
            plot_df[metric + "_pct"] = plot_df[metric] * 100
            color_map            = {fl: ("blue" if fl == leader_name else "lightgray") for fl in plot_df["Faculty Leader"]}

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

                fmt_center    = workbook.add_format({'align': 'center'})
                fmt_percentage = workbook.add_format({'num_format': '0.00%', 'align': 'center'})
                fmt_decimal   = workbook.add_format({'num_format': '0.00',  'align': 'center'})
                fmt_text_wrap = workbook.add_format({'text_wrap': True,     'align': 'center'})

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
                        low, high   = kpi_thresholds[col_name]
                        col_letter  = chr(65 + col_num)
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
                "Ela Cross":           "Team Cross",
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