#!/usr/bin/env python3
"""
pull_fl_dashboard_history.py

Pulls per-tutor values from the FL dashboard history files on GitHub to
fill in the columns metrics.csv can't get from Redshift.

Unlike pull_kpi_4week_data.py, these metrics are NOT anchored to
whatever period the KPI query was run for -- they always reflect the
most recent data actually present in each source file, regardless of
--start-date/--end-date. Re-running the KPI pull for a past period
(e.g. fixing a data issue) does not change what these values are.

The skip-most-recent-week logic (see SKIP_MOST_RECENT constants) is
correct and intentional -- confirmed by the account owner. Don't touch
that again without new evidence.

Metrics computed:

  parent_update_videos
    Source: data/parent_update_videos_history.csv, real columns (from an
    actual KeyError against the live file, not a guess): week of, tutor,
    faculty leader, student, course id, brand, sessions attended,
    sessions unattended, parent update sent, parent update only sent,
    update required, homework mentioned, video found, video duration,
    scrape error, fetched_at.

    CONFIRMED BUG, FIXED: a video is only actually REQUIRED for a row
    when "parent update only sent" == True -- confirmed by the account
    owner, and independently verified against real data for Jessica
    Peterson, week of 8/2: filtering to that column gives 6 rows, all 6
    with Video Found == True (100%), matching the dashboard exactly. Two
    earlier guesses were wrong and are noted here so they aren't retried:
    (1) "Update Sent == True" as the denominator -- also counts
    Progress-Update-Only sends (no video needed), which is what made it
    look like 75% (6/8) instead of 100%. (2) "Sessions Attended != 0 AND
    Update Type != 'Progress Update Only'" -- there is no "Update Type"
    column at all. (3) "Update Required == True" -- exists as a real
    column but is column-for-column identical to "parent update sent",
    i.e. it answers "was some update required," not "was a video
    specifically required."

    Drop the single most recent 'week of' value, then take the 4 before
    that (not tied to any external period). For each tutor, per week:
      weekly rate = count(rows where video required AND video found)
                    / count(rows where video required)
      (that week is NaN/no-info if no rows required a video that week --
      including if the tutor has no rows at all that week)
    Final value = average of whichever weekly rates aren't NaN within
    those 4 weeks. A tutor with data in only 2 of the 4 weeks is
    averaged over just those 2 (weeks are never skipped/shifted to
    "find" real data points).

  progress_update_quality
    Source: data/progress_updates_history.json (one row per progress
    update sent -- NOT pre-aggregated to weekly, unlike the file above).
    Columns used: tutor, sent_at, total (0-10 raw AI-scored quality:
    what_worked_on 0-2 + goals 0-2 + velocity 0-3 + plan_forward 0-3).
    sent_at is bucketed into a "week of" (the Sunday on/before that
    date, matching the convention used in parent_update_videos_history.csv)
    since this file has no pre-existing week column. Once bucketed,
    same idea as parent_update_videos: drop the single most recent
    'week of' value, take the 4 before that, per tutor average `total`
    across that week's updates, then average the non-NaN weekly values.
    Kept on the raw 0-10 scale (not normalized) to match how the
    dashboard displays it.

  students_without_grades / total_academics_students
    Source: data/cache/grades_data.csv (one row per tutor-student-subject,
    refreshed daily -- a current snapshot, no date window at all).
    NOTE: the 'academics' column in this file is NOT used -- it's
    sparsely populated (only ~2% of rows) and doesn't reliably reflect
    which students should be tracked for grades.
    Every student with grade_lvl >= 9 is included (total_academics_students),
    regardless of whether they have a subject on file -- a blank
    subject doesn't exclude the student, it just can't supply a grade.
    Only subject rows that are non-null AND not test-prep (SAT/ACT/
    PSAT/"Test Prep", matched case-insensitively) count toward "has a
    grade"; students_without_grades = students where none of their
    real/non-test-prep subject rows have a score. Both are raw counts,
    not percentages.

Run this after pull_kpi_4week_data.py, then re-run
build_tutor_metrics_file.py to merge these values into metrics.csv.
"""

import io
import os
import re
from datetime import timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}

OUTPUT_PATH = os.path.join(SCRIPT_DIR, "fl_dashboard_history_metrics.csv")

N_WEEKS = 4

# Reverted back to 1 after the 2026-09-12 catch-up run (period
# 8/9/26-9/5/26), which used skip=0 one-off since that run happened
# after a gap and every week in its window had already fully processed.
# Back on normal cadence now: skip=1 holds back the most recent scraped
# week in case its video-review/scoring data hasn't finished processing
# yet.
PARENT_VIDEOS_SKIP_MOST_RECENT = 1

# See note above -- same reasoning.
PROGRESS_UPDATE_SKIP_MOST_RECENT = 1


def last_n_weeks_excluding_most_recent(all_weeks, n_weeks=N_WEEKS, skip=0):
    """all_weeks: sorted or unsorted iterable of distinct week values.
    Drops the `skip` most recent, then returns the `n_weeks` before that."""
    ordered = sorted(all_weeks)
    if skip:
        ordered = ordered[:-skip]
    return ordered[-n_weeks:]


def log(msg):
    print(msg, flush=True)


def fetch_csv(relative_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{relative_path}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def fetch_json(relative_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{relative_path}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_json(io.StringIO(r.text))


def find_column(df, *candidates):
    """Case/whitespace-insensitive column lookup. Raises with the full
    actual column list if none of the candidates match, instead of
    silently computing against the wrong column (guessing wrong here is
    exactly what caused the last real bug -- fail loud, not quiet)."""
    lookup = {c.strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lookup:
            return lookup[key]
    raise KeyError(f'None of {candidates} found in columns: {list(df.columns)}')


def week_of_sunday(dt):
    """Sunday on/before dt's date -- matches the 'week of' convention
    used in parent_update_videos_history.csv."""
    d = dt.date() if hasattr(dt, "date") else dt
    dow_sunday0 = (d.weekday() + 1) % 7  # Mon=0..Sun=6 -> Sun=0..Sat=6
    return d - timedelta(days=dow_sunday0)


def compute_parent_update_videos():
    df = fetch_csv("data/parent_update_videos_history.csv")

    tutor_col = find_column(df, "tutor")
    week_col = find_column(df, "week of")
    update_only_col = find_column(df, "parent update only sent")
    video_found_col = find_column(df, "video found")

    df[week_col] = pd.to_datetime(df[week_col]).dt.date

    last_weeks = last_n_weeks_excluding_most_recent(df[week_col].unique(), skip=PARENT_VIDEOS_SKIP_MOST_RECENT)
    log(f"parent_update_videos: using weeks {[str(w) for w in last_weeks]}")
    df = df[df[week_col].isin(last_weeks)]

    # CONFIRMED by the account owner: a video is only required when
    # "parent update only sent" == True -- that's the column that
    # distinguishes a real parent update (needs a video) from a progress
    # update (doesn't). Earlier guesses (Sessions Attended + a nonexistent
    # "Update Type" column, then "Update Required") were both wrong --
    # "Update Required" turned out to be answering a different question
    # (identical to "parent update sent" column-for-column). This is the
    # one that was actually verified against real data (Jessica Peterson,
    # week of 8/2: 6/6 = 100%, matching the dashboard).
    df = df[df[update_only_col] == True]

    weekly = (
        df.groupby([tutor_col, week_col])
        .agg(
            required=(tutor_col, "count"),
            videos=(video_found_col, lambda s: (s == True).sum()),
        )
        .reset_index()
    )
    weekly["rate"] = weekly.apply(
        lambda r: (r["videos"] / r["required"]) if r["required"] > 0 else float("nan"),
        axis=1,
    )

    result = (
        weekly.groupby(tutor_col)["rate"]
        .mean()  # skips NaN by default
        .reset_index()
        .rename(columns={tutor_col: "tutor_name", "rate": "parent_update_videos"})
    )
    return result


def compute_progress_update_quality():
    df = fetch_json("data/progress_updates_history.json")
    df["sent_at"] = pd.to_datetime(df["sent_at"])
    df["week_of"] = df["sent_at"].apply(week_of_sunday)

    last_weeks = last_n_weeks_excluding_most_recent(df["week_of"].unique())
    log(f"progress_update_quality: using weeks {[str(w) for w in last_weeks]}")
    df = df[df["week_of"].isin(last_weeks)]

    weekly = (
        df.groupby(["tutor", "week_of"])["total"]
        .mean()
        .reset_index()
        .rename(columns={"total": "weekly_avg"})
    )

    result = (
        weekly.groupby("tutor")["weekly_avg"]
        .mean()  # skips NaN by default; also skips weeks with no data since they're just absent rows
        .reset_index()
        .rename(columns={"tutor": "tutor_name", "weekly_avg": "progress_update_quality"})
    )
    return result


TEST_PREP_PATTERN = re.compile(r"\b(?:SAT|ACT|PSAT)\b|test\s*prep", re.IGNORECASE)


def compute_students_without_grades():
    df = fetch_csv("data/cache/grades_data.csv")

    # Every grade_lvl >= 9 student is included, whether or not they
    # have a subject on file at all -- a missing/blank subject is
    # itself a "no grade found" case, same as the dashboard treats it,
    # not a reason to exclude the student entirely (e.g. Alex Hu has
    # subject=NaN and is correctly flagged as missing on the dashboard).
    eligible = df[df["grade_lvl"] >= 9].copy()

    # Only real, non-test-prep subject rows count toward "has a grade" --
    # a null subject or a test-prep-only subject both just mean no
    # actual academic grade was found for that row.
    is_gradeable_subject = (
        eligible["subject"].notna()
        & ~eligible["subject"].str.contains(TEST_PREP_PATTERN, na=False)
    )
    eligible["gradeable_score"] = eligible["score"].where(is_gradeable_subject)

    per_student = (
        eligible.groupby(["tutor_name", "student_id"])["gradeable_score"]
        .apply(lambda s: s.notna().any())
        .reset_index(name="has_grade")
    )

    result = (
        per_student.groupby("tutor_name")["has_grade"]
        .agg(
            total_academics_students="count",
            students_without_grades=lambda s: (~s).sum(),
        )
        .reset_index()
    )
    return result


def main():
    metrics = [
        compute_parent_update_videos(),
        compute_progress_update_quality(),
        compute_students_without_grades(),
    ]

    result = metrics[0]
    for m in metrics[1:]:
        result = result.merge(m, on="tutor_name", how="outer")

    result.to_csv(OUTPUT_PATH, index=False)
    log(f"Saved -> {OUTPUT_PATH} ({len(result)} tutors)")


if __name__ == "__main__":
    main()
