#!/usr/bin/env python3
"""
build_tutor_metrics_file.py

STEP 2 (part a) of the master metrics pipeline. Takes the raw Redshift
pull (KPI_4Week_Metrics.xlsx, produced by pull_kpi_4week_data.py) and
reshapes it into the flat metrics.csv format used to copy values into
tutor KPI Trackers.

Columns we don't get from the Redshift query yet (parent_update_videos,
progress_update_quality, students_without_grades) are left blank --
these come from a separate source (the FL dashboard history files),
which is the next step to wire in after this one.

The "dates" column is read directly from KPI_4Week_Metrics.xlsx's
period_start/period_end columns -- not passed in separately -- so it's
always exactly the period pull_kpi_4week_data.py actually pulled,
whatever that was (today's default, or a --start-date/--end-date
backfill).
"""

import os

import pandas as pd

from metrics_period import format_period, read_period_from_kpi_file

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_PATH = os.path.join(SCRIPT_DIR, "KPI_4Week_Metrics.xlsx")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "fl_dashboard_history_metrics.csv")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "metrics.csv")

# Final column order/names expected by the tutor tracker copy step.
OUTPUT_COLUMNS = [
    "tutor_name",
    "dates",
    "delivery_percent",
    "availability_percent",
    "sessionsontime",
    "parentupdates",
    "parent_update_videos",
    "progress_update_quality",
    "progressupdates",
    "repurchase_hours",
    "archivable_percent",
    "unscheduled_percent",
    "students_without_grades",
    "total_academics_students",
    "completed_ppw",
    "required_ppw",
    "prep_time_percent",
]

# Direct mapping from the Redshift query's output columns -> our target
# column names. Anything not listed here (and not in OUTPUT_COLUMNS)
# just gets dropped; anything in OUTPUT_COLUMNS with no source here is
# left blank.
SOURCE_TO_TARGET = {
    "tutor_name": "tutor_name",
    "delivery_percent": "delivery_percent",
    "availability_percent": "availability_percent",
    "sessionsontime": "sessionsontime",
    "parentupdates": "parentupdates",
    "progressupdates": "progressupdates",
    "repurchase_hours": "repurchase_hours",
    "archivable_percent": "archivable_percent",
    "unscheduled_percent": "unscheduled_percent",
    "completed_ppw": "completed_ppw",
    "required_ppw": "required_ppw",
}


def main():
    day_start, day_end = read_period_from_kpi_file(INPUT_PATH)

    df = pd.read_excel(INPUT_PATH)

    out = pd.DataFrame()
    for source_col, target_col in SOURCE_TO_TARGET.items():
        out[target_col] = df[source_col] if source_col in df.columns else ""

    out["dates"] = format_period(day_start, day_end)

    # Prep Time % -- same formula sync_ar_data.py already uses for annual
    # review (Total_Prep / Attended_Sessions), just computed on this
    # script's rolling 4-week period instead of the fixed annual-review
    # windows. session_and_email_prep and attended_sessions are both raw
    # hours straight from the Redshift pull (KPI_4Week_Metrics.xlsx) --
    # blank if there were no attended sessions that period (avoid /0).
    if "session_and_email_prep" in df.columns and "attended_sessions" in df.columns:
        prep = df["session_and_email_prep"]
        attended = df["attended_sessions"]
        out["prep_time_percent"] = (prep / attended.replace(0, pd.NA)).where(attended.notna() & (attended != 0), "")
    else:
        out["prep_time_percent"] = ""

    # Fill in the columns we don't have data for yet -- blank for now.
    for col in OUTPUT_COLUMNS:
        if col not in out.columns:
            out[col] = ""

    # Merge in anything pulled from the FL dashboard history files
    # (pull_fl_dashboard_history.py), if it's been run. Only overwrites
    # columns that file actually has values for -- built up one metric
    # at a time, so this fills in more columns over time.
    if os.path.exists(HISTORY_PATH):
        history = pd.read_csv(HISTORY_PATH)
        out = out.merge(history, on="tutor_name", how="left", suffixes=("", "_history"))
        for col in history.columns:
            if col == "tutor_name":
                continue
            hist_col = f"{col}_history" if f"{col}_history" in out.columns else col
            if hist_col in out.columns:
                out[col] = out[hist_col].where(out[hist_col].notna(), out.get(col, ""))
        # drop any leftover _history helper columns
        out = out[[c for c in out.columns if not c.endswith("_history")]]
    else:
        print(f"(no {os.path.basename(HISTORY_PATH)} found yet -- skipping history merge)")

    out = out[OUTPUT_COLUMNS]
    out.to_csv(OUTPUT_PATH, index=False)
    print(f"Saved -> {OUTPUT_PATH} ({len(out)} rows)")


if __name__ == "__main__":
    main()
