#!/usr/bin/env python3
"""
debug_jessica_videos.py

One-off diagnostic -- NOT part of the regular pipeline. Dumps exactly what
parent_update_videos_history.csv contains for a given tutor, row by row
(every real column), plus the OLD (buggy) and a NEW candidate per-week
aggregation, so a fix can be verified against real data before it's
trusted in the real pipeline.

Real columns (confirmed from an actual KeyError against the live file):
week of, tutor, faculty leader, student, course id, brand,
sessions attended, sessions unattended, parent update sent,
parent update only sent, update required, homework mentioned,
video found, video duration, scrape error, fetched_at.

There is NO "update type" column -- that was a wrong guess. There IS a
column literally called "update required", which is the leading
hypothesis for the correct denominator (untested until this script's
output is checked against real data).

Usage:
    python3 debug_jessica_videos.py "Jessica Peterson"
"""

import io
import os
import sys

import pandas as pd
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}


def fetch_csv(relative_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{relative_path}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def find_column(df, *candidates):
    lookup = {c.strip().lower(): c for c in df.columns}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lookup:
            return lookup[key]
    raise KeyError(f'None of {candidates} found in columns: {list(df.columns)}')


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "Jessica Peterson"

    df = fetch_csv("data/parent_update_videos_history.csv")
    print(f"Full file: {len(df)} rows, columns: {list(df.columns)}")
    print()

    tutor_col = find_column(df, "tutor")
    week_col = find_column(df, "week of")
    sessions_col = find_column(df, "sessions attended")
    update_sent_col = find_column(df, "parent update sent")
    update_only_col = find_column(df, "parent update only sent")
    update_required_col = find_column(df, "update required")
    video_found_col = find_column(df, "video found")

    df[week_col] = pd.to_datetime(df[week_col]).dt.date
    sub = df[df[tutor_col] == target].sort_values([week_col]).copy()

    print(f"=== All {len(sub)} raw rows for tutor == \"{target}\" (every column) ===")
    # Show every column, not a curated subset -- avoid missing something
    # relevant again.
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 250)
    print(sub.to_string())
    print()

    print(f'Unique raw values -- "{update_sent_col}": {sorted(sub[update_sent_col].dropna().unique().tolist(), key=str)}')
    print(f'Unique raw values -- "{update_only_col}": {sorted(sub[update_only_col].dropna().unique().tolist(), key=str)}')
    print(f'Unique raw values -- "{update_required_col}": {sorted(sub[update_required_col].dropna().unique().tolist(), key=str)}')
    print()

    print(f'=== OLD (buggy) per-week: "{update_sent_col}" == True as denominator ===')
    old_weekly = (
        sub.groupby(week_col)
        .agg(
            rows=(tutor_col, "count"),
            updates_sent=(update_sent_col, lambda s: (s == True).sum()),
            videos_found=(video_found_col, lambda s: (s == True).sum()),
        )
        .reset_index()
    )
    old_weekly["rate"] = old_weekly.apply(
        lambda r: (r["videos_found"] / r["updates_sent"]) if r["updates_sent"] > 0 else float("nan"), axis=1
    )
    print(old_weekly.to_string())
    print()

    print(f'=== NEW per-week: "{update_only_col}" == True as denominator (confirmed by account owner) ===')
    new_weekly = (
        sub[sub[update_only_col] == True]
        .groupby(week_col)
        .agg(
            required=(tutor_col, "count"),
            videos_found=(video_found_col, lambda s: (s == True).sum()),
        )
        .reset_index()
    )
    new_weekly["rate"] = new_weekly.apply(
        lambda r: (r["videos_found"] / r["required"]) if r["required"] > 0 else float("nan"), axis=1
    )
    print(new_weekly.to_string())
    print()

    all_weeks = sorted(df[week_col].unique())
    last_4_skip_1 = all_weeks[:-1][-4:]
    print(f"Last 4 weeks, skip-most-recent-1: {[str(w) for w in last_4_skip_1]}")
    print()

    old_w = old_weekly[old_weekly[week_col].isin(last_4_skip_1)]
    print(f'OLD average over that window: {old_w["rate"].mean()} (rates: {old_w["rate"].tolist()})')
    new_w = new_weekly[new_weekly[week_col].isin(last_4_skip_1)]
    print(f'NEW average over that window: {new_w["rate"].mean()} (rates: {new_w["rate"].tolist()})')


if __name__ == "__main__":
    main()
