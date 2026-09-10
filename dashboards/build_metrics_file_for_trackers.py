#!/usr/bin/env python3
"""
build_metrics_file_for_trackers.py

Converts metrics.csv into Metrics_File.xlsx -- the exact 15-column
format the KPITrackers.ipynb notebook (on the Desktop) reads to copy
each tutor's row into their individual KPI Tracker file. That notebook
copies purely by COLUMN POSITION (not header name), so this script's
column order must exactly match the tutor trackers' "KPIs" sheet
header, confirmed against a real current tracker
("Ayman Smoot KPI Tracker_NewVersion.xlsx"):

  1.  Tutor Name                                  <- tutor_name
  2.  Date Range                                   <- dates
  3.  % to Delivery Target                         <- delivery_percent
  4.  % to Availability Target                     <- availability_percent
  5.  % Sessions on Time                           <- sessionsontime
  6.  % Parents Updates Done on Time                <- parentupdates
  7.  % Parent Updates with Videos                  <- parent_update_videos
  8.  Average Progress Updates Quality              <- progress_update_quality
  9.  % Completed Progress Updates ( 8 weeks)       <- progressupdates
  10. Repurchase Hours (12 weeks)                   <- repurchase_hours
  11. % Archivable Students                         <- archivable_percent
  12. % Unscheduled Hours                           <- unscheduled_percent
  13. % Students without Grades                     <- students_without_grades / total_academics_students
  14. # of PPWs Attached On Time                    <- completed_ppw
  15. # of PPWs Required                             <- required_ppw

Column 13 is the one real transformation: metrics.csv has this as two
separate raw-count columns (students_without_grades, total_academics_students),
but the tracker has a single "%" column, so this computes the percentage
here specifically (blank if total_academics_students is 0/missing).

Output is written directly to the Desktop as Metrics_File.xlsx (matching
the filename pattern KPITrackers.ipynb's loader looks for:
`"Metrics_File" in filename and filename.endswith('.xlsx')`), overwriting
whatever's there. Run this AFTER build_tutor_metrics_file.py, then open/
run KPITrackers.ipynb from the Desktop to actually copy this data into
every tutor's tracker.

This is intentionally a separate, manual step from
run_full_metrics_pipeline.py -- the tutor tracker update and the
MonthlyMetric/dashboard push are being done in two distinct stages this
round (trackers first, dashboard push later), so this script is not
wired into that automatic chain.
"""

import os

import openpyxl
import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METRICS_CSV_PATH = os.path.join(SCRIPT_DIR, "metrics.csv")

DESKTOP_PATH = os.path.expanduser("~/Desktop")
OUTPUT_PATH = os.path.join(DESKTOP_PATH, "Metrics_File.xlsx")

HEADER = [
    "Tutor Name",
    "Date Range",
    "% to Delivery Target",
    "% to Availability Target",
    "% Sessions on Time",
    "% Parents Updates Done on Time",
    "% Parent Updates with Videos",
    "Average Progress Updates Quality",
    "% Completed Progress Updates ( 8 weeks)",
    "Repurchase Hours (12 weeks)",
    "% Archivable Students",
    "% Unscheduled Hours",
    "% Students without Grades",
    "# of PPWs Attached On Time",
    "# of PPWs Required",
]


def pct_without_grades(row):
    total = row["total_academics_students"]
    missing = row["students_without_grades"]
    if pd.isna(total) or total == 0:
        return ""
    return missing / total


def main():
    df = pd.read_csv(METRICS_CSV_PATH)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet2"  # matches the old Metrics_File.xlsx's sheet name

    ws.append(HEADER)

    for _, row in df.iterrows():
        ws.append([
            row["tutor_name"],
            row["dates"],
            row["delivery_percent"],
            row["availability_percent"],
            row["sessionsontime"],
            row["parentupdates"],
            row["parent_update_videos"],
            row["progress_update_quality"],
            row["progressupdates"],
            row["repurchase_hours"],
            row["archivable_percent"],
            row["unscheduled_percent"],
            pct_without_grades(row),
            row["completed_ppw"],
            row["required_ppw"],
        ])

    wb.save(OUTPUT_PATH)
    print(f"Saved -> {OUTPUT_PATH} ({len(df)} tutor rows)")


if __name__ == "__main__":
    main()
