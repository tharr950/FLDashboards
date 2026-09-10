#!/usr/bin/env python3
"""
append_to_monthly_metric.py

Appends this run's metrics.csv data into the MonthlyMetric sheet of
December_Annual_Reviews.xlsx (root of the FLDashboards code repo,
GITHUB_CODE_REPO) and commits it back via the GitHub Contents API.

This is the file the live FL Dashboards app reads locally
(load_kpi_data()) to power KPI trends/history on tutor profiles and the
watchlist -- so this step writes directly into production data. Every
sheet other than MonthlyMetric, and every existing MonthlyMetric row,
is left untouched EXCEPT rows that exactly match this run's
(Tutor Name, Date Range) -- those get replaced (handles re-running the
pipeline for the same period after a data issue). Normal runs are pure
appends since the Date Range is new each time.

Column mapping (metrics.csv / KPI_4Week_Metrics.xlsx -> MonthlyMetric):
  tutor_name                -> Tutor Name
  dates                     -> Date Range
  delivery_percent          -> % to Delivery Target
  availability_percent      -> % to Availability Target
  sessionsontime            -> % Sessions on Time
  parentupdates             -> % Parents Updates Done on Time
  progressupdates           -> % of Active Students with Progress Updates
                                Completed in last 2 months
  repurchase_hours          -> Weighted Repurchases
                                (NOTE: this is our new pipeline's hours-based
                                number, not whatever weighting the old manual
                                process used -- history rows keep their old
                                values as-is, no attempt to reconcile/backfill)
  completed_ppw/required_ppw-> Ratio of PPW Events with Attached PPWs
                                (blank if required_ppw is 0/missing)
  tier (from KPI xlsx)      -> Tier
  fl (from KPI xlsx)        -> Faculty Leader

  OLD PPW               -> left blank (legacy column, not populated for new rows)
  Adjunct/Professional   -> left blank (no source wired up yet, per instruction)
  parent_update_videos      -> % Parent Updates with Videos (NEW -- added 2026-08-23,
                                pulled from the FL dashboard history pipeline;
                                did not exist in MonthlyMetric before this, so
                                every row prior to this run is blank for it)
  progress_update_quality   -> Progress Update Quality Score (NEW -- added
                                2026-08-23, 0-10 scale, NOT a percentage;
                                same history as above, blank before this run)
  prep_time_percent         -> Prep Time % (NEW -- added 2026-08-23,
                                session_and_email_prep / attended_sessions
                                from the 4-week Redshift pull, same formula
                                sync_ar_data.py uses for annual review, just
                                on the rolling period; feeds the Tutor
                                Concerns scoring script, which runs after
                                this one)

If either of the two NEW columns above isn't already a column in the
live MonthlyMetric sheet, this script adds it to the header
automatically (see main()) rather than failing -- this is an
intentional, one-time schema widening. Any other unexpected missing
column still raises an error, since that likely means a real mapping
mistake rather than an intentional new metric.

Run this after build_tutor_metrics_file.py.
"""

import base64
import io
import os
import sys

import openpyxl
import pandas as pd
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_CODE_REPO = os.environ["GITHUB_CODE_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

FILE_PATH = "December_Annual_Reviews.xlsx"
SHEET_NAME = "MonthlyMetric"

METRICS_CSV_PATH = os.path.join(SCRIPT_DIR, "metrics.csv")
KPI_XLSX_PATH = os.path.join(SCRIPT_DIR, "KPI_4Week_Metrics.xlsx")

# Columns it's OK to auto-add to the MonthlyMetric header if they aren't
# there yet -- intentional new metrics, not typos. Anything else missing
# from the header still raises an error (fail loud on real mistakes).
NEW_COLUMNS_OK_TO_ADD = {
    "% Parent Updates with Videos",
    "Progress Update Quality Score",
    "Prep Time %",
}


def log(msg):
    print(msg, flush=True)


def build_new_rows():
    metrics = pd.read_csv(METRICS_CSV_PATH)
    kpi = pd.read_excel(KPI_XLSX_PATH)

    lookup = kpi.set_index("tutor_name")[["tier", "fl"]]
    merged = metrics.join(lookup, on="tutor_name")

    def ppw_ratio(row):
        req = row["required_ppw"]
        comp = row["completed_ppw"]
        if pd.isna(req) or req == 0:
            return ""
        return comp / req

    rows = pd.DataFrame({
        "Tutor Name": merged["tutor_name"],
        "Date Range": merged["dates"],
        "% to Delivery Target": merged["delivery_percent"],
        "% to Availability Target": merged["availability_percent"],
        "% Sessions on Time": merged["sessionsontime"],
        "% Parents Updates Done on Time": merged["parentupdates"],
        "% of Active Students with Progress Updates Completed in last 2 months": merged["progressupdates"],
        "Weighted Repurchases": merged["repurchase_hours"],
        "OLD PPW": "",
        "Ratio of PPW Events with Attached PPWs": merged.apply(ppw_ratio, axis=1),
        "Tier": merged["tier"],
        "Adjunct/Professional": "",
        "Faculty Leader": merged["fl"],
        # NaN (no data that period, e.g. no updates required/sent) -> blank
        # cell rather than a literal "nan" written into the spreadsheet.
        "% Parent Updates with Videos": merged["parent_update_videos"].where(merged["parent_update_videos"].notna(), ""),
        "Progress Update Quality Score": merged["progress_update_quality"].where(merged["progress_update_quality"].notna(), ""),
        "Prep Time %": merged["prep_time_percent"].where(merged["prep_time_percent"].notna(), "") if "prep_time_percent" in merged.columns else "",
    })
    return rows


def fetch_workbook():
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{FILE_PATH}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    payload = r.json()
    content = base64.b64decode(payload["content"])
    sha = payload["sha"]
    return openpyxl.load_workbook(io.BytesIO(content)), sha


def push_workbook(wb, sha):
    buf = io.BytesIO()
    wb.save(buf)
    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{FILE_PATH}"
    body = {
        "message": f"data: append MonthlyMetric rows ({pd.Timestamp.utcnow():%Y-%m-%d})",
        "content": encoded,
        "sha": sha,
    }
    r = requests.put(url, json=body, headers=HEADERS, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to push {FILE_PATH}: {r.status_code} {r.text[:300]}")


def main():
    dry_run = "--dry-run" in sys.argv

    new_rows = build_new_rows()
    log(f"Built {len(new_rows)} new MonthlyMetric rows.")

    wb, sha = fetch_workbook()
    ws = wb[SHEET_NAME]

    header = [cell.value for cell in ws[1]]
    missing = [c for c in new_rows.columns if c not in header]
    unexpected_missing = [c for c in missing if c not in NEW_COLUMNS_OK_TO_ADD]
    if unexpected_missing:
        raise RuntimeError(
            f"MonthlyMetric sheet is missing expected column(s): {unexpected_missing}. "
            f"Actual header: {header}"
        )
    for col in missing:
        new_col_idx = len(header) + 1
        ws.cell(row=1, column=new_col_idx, value=col)
        header.append(col)
        log(f"Added new column '{col}' to MonthlyMetric header (column {new_col_idx}). "
            f"All existing rows will be blank for it until they're re-processed.")

    tutor_col = header.index("Tutor Name")
    daterange_col = header.index("Date Range")

    new_periods = set(zip(new_rows["Tutor Name"], new_rows["Date Range"]))

    # Find existing rows that exactly match a (tutor, period) we're about
    # to add, so a re-run overwrites instead of duplicating. Collect row
    # indices (1-based, header is row 1) and delete bottom-up so indices
    # don't shift out from under us.
    rows_to_delete = []
    for row_idx in range(2, ws.max_row + 1):
        tutor = ws.cell(row=row_idx, column=tutor_col + 1).value
        date_range = ws.cell(row=row_idx, column=daterange_col + 1).value
        if (tutor, date_range) in new_periods:
            rows_to_delete.append(row_idx)

    if rows_to_delete:
        log(f"Found {len(rows_to_delete)} existing row(s) matching this run's period(s) -- replacing them.")
        for row_idx in sorted(rows_to_delete, reverse=True):
            ws.delete_rows(row_idx, 1)
    else:
        log("No existing rows match this run's period(s) -- pure append.")

    for _, row in new_rows.iterrows():
        ws.append([row.get(col, "") for col in header])

    if dry_run:
        preview_path = os.path.join(SCRIPT_DIR, "December_Annual_Reviews_PREVIEW.xlsx")
        wb.save(preview_path)
        log(f"[DRY RUN] Nothing pushed to GitHub. Saved a local preview -> {preview_path}")
        log(f"[DRY RUN] Would have deleted {len(rows_to_delete)} existing row(s) and appended {len(new_rows)} new row(s).")
    else:
        push_workbook(wb, sha)
        log(f"Pushed updated {FILE_PATH} -> {GITHUB_CODE_REPO} ({len(new_rows)} rows appended to {SHEET_NAME}).")


if __name__ == "__main__":
    main()
