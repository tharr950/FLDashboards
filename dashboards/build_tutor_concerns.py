#!/usr/bin/env python3
"""
build_tutor_concerns.py

Automates the "Tutor Concerns" ranking that used to be run by hand in a
Jupyter notebook (TutorKPISorter.ipynb) against a manually-exported
TutorMetricReport5.csv. This is the LAST step of the pipeline -- run it
after append_to_monthly_metric.py, since it needs that step's freshly
pushed current-period row (for Tier/Faculty Leader) and the previous
period's row (for the "Diff M2M ..." rules) already in MonthlyMetric.

Pipeline order:
    pull_kpi_4week_data.py
    build_tutor_metrics_file.py
    pull_fl_dashboard_history.py   (parent_update_videos / progress_update_quality)
    append_to_monthly_metric.py
    build_tutor_concerns.py        <-- this script

Data sources:
  - metrics.csv (this period's tutor metrics -- same file
    append_to_monthly_metric.py just pushed from)
  - KPI_4Week_Metrics.xlsx (for Tier / Faculty Leader, same as
    append_to_monthly_metric.py)
  - MonthlyMetric sheet of December_Annual_Reviews.xlsx, fetched fresh
    from GitHub (GITHUB_CODE_REPO) -- used ONLY to look up each tutor's
    PREVIOUS period values for the "Diff M2M ..." rules. Assumes
    append_to_monthly_metric.py has already pushed this run's current
    period, but that's fine -- the current period's own row isn't
    needed here since we already have it in metrics.csv/KPI_4Week.

Rules ported from TutorKPISorter.ipynb (see that notebook for the
original manual version). Each rule can push a concern score and a
human-readable reason; a tutor's final "Concern Group" is the MAX score
across all triggered rules (1 = no concerns, 5 = highest).

Known v1 gap: the original notebook also had two NPS-based rules
("Avg. NPS Score < 8" and "Diff M2M Avg. NPS Score < -1.5"). NPS is
pulled by sync_ar_data.py, but only for two fixed annual-review date
windows -- not on this pipeline's rolling 4-week cadence -- so there's
no current-period NPS data to score against yet. Those two rules are
skipped for now (documented per Tyler's decision on 2026-08-23); revisit
if/when NPS gets added to the rolling pull.

Also skipped for the same reason (no prior-period value to diff
against): any "Diff M2M ..." rule where either period is missing simply
never triggers for that tutor -- expected, not a bug, especially in the
first few cycles after a metric (like Prep Time %) is newly added.

Output: Tutor Name, Faculty Leader Name, Concern Group, Reasons, Date
pushed to Tutor_Concerns.csv in GITHUB_CODE_REPO (same repo as
December_Annual_Reviews.xlsx -- both are read the same way by the live
app via a plain local path, so they're assumed to deploy together; flag
this if that assumption is wrong). Matches on (Tutor Name, Date) so
re-running for the same period replaces instead of duplicating.

Usage:
    python3 build_tutor_concerns.py              # dry run (default) -- writes a local preview CSV, pushes nothing
    python3 build_tutor_concerns.py --push        # writes Tutor_Concerns.csv to GitHub for real
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

MONTHLY_METRIC_FILE = "December_Annual_Reviews.xlsx"
MONTHLY_METRIC_SHEET = "MonthlyMetric"
CONCERNS_FILE = "Tutor_Concerns.csv"

METRICS_CSV_PATH = os.path.join(SCRIPT_DIR, "metrics.csv")
KPI_XLSX_PATH = os.path.join(SCRIPT_DIR, "KPI_4Week_Metrics.xlsx")


def log(msg):
    print(msg, flush=True)


def fetch_monthly_metric_df():
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{MONTHLY_METRIC_FILE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"])
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb[MONTHLY_METRIC_SHEET]
    header = [c.value for c in ws[1]]
    rows = []
    for row in range(2, ws.max_row + 1):
        rows.append([ws.cell(row=row, column=i + 1).value for i in range(len(header))])
    return pd.DataFrame(rows, columns=header)


def fetch_concerns_file():
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{CONCERNS_FILE}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return pd.DataFrame(columns=["Tutor Name", "Faculty Leader Name", "Concern Group", "Reasons", "Date"]), None
    r.raise_for_status()
    payload = r.json()
    content = base64.b64decode(payload["content"])
    return pd.read_csv(io.BytesIO(content)), payload["sha"]


def push_concerns_file(df, sha):
    csv_content = df.to_csv(index=False)
    encoded = base64.b64encode(csv_content.encode()).decode("utf-8")
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{CONCERNS_FILE}"
    body = {
        "message": f"data: update Tutor_Concerns ({pd.Timestamp.utcnow():%Y-%m-%d})",
        "content": encoded,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, json=body, headers=HEADERS, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to push {CONCERNS_FILE}: {r.status_code} {r.text[:300]}")


def build_current_period_df():
    """This period's per-tutor metrics + Tier/Faculty Leader, same sources
    append_to_monthly_metric.py uses."""
    metrics = pd.read_csv(METRICS_CSV_PATH)
    kpi = pd.read_excel(KPI_XLSX_PATH)
    lookup = kpi.set_index("tutor_name")[["tier", "fl"]]
    merged = metrics.join(lookup, on="tutor_name")

    def ppw_ratio(row):
        req = row.get("required_ppw")
        comp = row.get("completed_ppw")
        if pd.isna(req) or req == 0:
            return float("nan")
        return comp / req

    merged["ppw_ratio"] = merged.apply(ppw_ratio, axis=1)
    return merged


def get_previous_period_lookup(monthly_df, current_period_label):
    """Returns a dict: tutor_name -> {delivery, availability, prep_time, progress_updates}
    for the period immediately before current_period_label, or {} if there
    isn't one yet (first cycle -- Diff M2M rules just won't trigger)."""
    monthly_df = monthly_df.copy()
    monthly_df["Date Range Parsed"] = pd.to_datetime(
        monthly_df["Date Range"].astype(str).str.split(" - ").str[0], errors="coerce")
    periods_sorted = (
        monthly_df[["Date Range", "Date Range Parsed"]]
        .dropna()
        .drop_duplicates()
        .sort_values("Date Range Parsed")["Date Range"]
        .tolist()
    )
    if current_period_label not in periods_sorted:
        # Current period hasn't been pushed to MonthlyMetric yet (e.g. this
        # script was run before append_to_monthly_metric.py) -- treat the
        # most recent period in the sheet as "previous" for diffing.
        prev_label = periods_sorted[-1] if periods_sorted else None
    else:
        idx = periods_sorted.index(current_period_label)
        prev_label = periods_sorted[idx - 1] if idx > 0 else None

    if prev_label is None:
        return {}, None

    prev_rows = monthly_df[monthly_df["Date Range"] == prev_label]
    lookup = {}
    for _, r in prev_rows.iterrows():
        name = r.get("Tutor Name")
        if pd.isna(name):
            continue
        lookup[name] = {
            "delivery": r.get("% to Delivery Target"),
            "availability": r.get("% to Availability Target"),
            "prep_time": r.get("Prep Time %") if "Prep Time %" in prev_rows.columns else None,
            "progress_updates": r.get("% of Active Students with Progress Updates Completed in last 2 months"),
        }
    return lookup, prev_label


def to_float(v):
    try:
        if v in (None, "", "#DIV/0!"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def score_tutor(row, prev, tier_delivery_p10):
    """Returns (concern_group, reasons_str) for one tutor row."""
    scores = [1]
    reasons = []

    def flag(score, reason):
        scores.append(score)
        reasons.append(reason)

    availability = to_float(row.get("availability_percent"))
    delivery = to_float(row.get("delivery_percent"))
    sessions_on_time = to_float(row.get("sessionsontime"))
    parent_updates = to_float(row.get("parentupdates"))
    ppw_ratio = to_float(row.get("ppw_ratio"))
    progress_updates = to_float(row.get("progressupdates"))
    repurchase_hours = to_float(row.get("repurchase_hours"))
    prep_time = to_float(row.get("prep_time_percent"))
    tier = row.get("tier")

    # Rule 1: % to Availability Target
    if availability is not None and availability < 0.90:
        flag(3, "% to Availability Target < 90%")

    # Rule 2: % Parents Updates Done on Time
    if parent_updates is not None:
        if parent_updates < 0.85:
            flag(5, "% Parent Updates < 85%")
        elif parent_updates < 0.90:
            flag(4, "% Parent Updates < 90%")

    # Rule 3: % Sessions on Time
    if sessions_on_time is not None:
        if sessions_on_time < 0.85:
            flag(5, "% Sessions on Time < 85%")
        elif sessions_on_time < 0.90:
            flag(4, "% Sessions on Time < 90%")

    # Rule 4: Ratio of PPW Events with Attached PPWs
    if ppw_ratio is not None and ppw_ratio < 0.5:
        flag(2, "Ratio of PPW Events with Attached PPW < 1/2")

    # Rule 5: % of Active Students with Progress Updates Completed in last 2 months
    if progress_updates is not None:
        if progress_updates < 0.50:
            flag(5, "% of Active Students with Progress Updates Completed in last 2 months < 50%")
        elif progress_updates < 0.60:
            flag(4, "% of Active Students with Progress Updates Completed in last 2 months < 60%")
        elif progress_updates < 0.70:
            flag(3, "% of Active Students with Progress Updates Completed in last 2 months < 70%")

    # Rule 5b: Diff M2M % of Active Students with Progress Updates Completed in last 2 months
    if progress_updates is not None and prev and prev.get("progress_updates") is not None:
        prev_progress = to_float(prev["progress_updates"])
        if prev_progress is not None:
            diff = progress_updates - prev_progress
            if diff < -0.20:
                flag(2, "Diff M2M % of Active Students with Progress Updates Completed in last 2 months < -20%")

    # Rule 6: Weighted Repurchases (repurchase_hours)
    if repurchase_hours is not None and repurchase_hours == 0:
        flag(2, "Weighted Repurchases = 0")

    # Rule 7: Prep Time %
    if prep_time is not None and prep_time > 0.15:
        flag(2, "Prep Time % > 15%")

    # Rule 8: Diff M2M Prep Time %
    if prep_time is not None and prev and prev.get("prep_time") is not None:
        prev_prep = to_float(prev["prep_time"])
        if prev_prep is not None:
            diff = prep_time - prev_prep
            if diff > 0.10:
                flag(2, "Diff M2M Prep Time % > 10%")

    # NOTE: NPS-based rules from the original notebook ("Avg. NPS Score < 8"
    # and "Diff M2M Avg. NPS Score < -1.5") are intentionally skipped -- NPS
    # isn't pulled on this pipeline's rolling 4-week cadence yet (see
    # sync_ar_data.py, which only covers fixed annual-review windows).

    # Rule 10: Diff M2M % to Delivery Target (only matters if already low)
    if delivery is not None and delivery < 0.85 and prev and prev.get("delivery") is not None:
        prev_delivery = to_float(prev["delivery"])
        if prev_delivery is not None:
            diff = delivery - prev_delivery
            if diff < -0.20:
                flag(2, "Diff M2M % to Delivery Target < -20% and % to Delivery Target < 85%")

    # Rule 11: Diff M2M % to Availability Target (only matters if already low)
    if availability is not None and availability < 0.95 and prev and prev.get("availability") is not None:
        prev_availability = to_float(prev["availability"])
        if prev_availability is not None:
            diff = availability - prev_availability
            if diff < -0.10:
                flag(2, "Diff M2M % to Availability Target < -10% and % to Availability Target < 95%")

    # Rule 12: Bottom 10% in % to Delivery Target for this tutor's tier
    if delivery is not None and tier in tier_delivery_p10 and tier_delivery_p10[tier] is not None:
        if delivery <= tier_delivery_p10[tier]:
            flag(3, "Bottom 10% in % to Delivery Target for tier")

    return max(scores), ("; ".join(reasons) if reasons else "None")


def main():
    push_for_real = "--push" in sys.argv

    log("Building current period tutor metrics...")
    current_df = build_current_period_df()
    current_period_label = current_df["dates"].dropna().iloc[0] if "dates" in current_df.columns else None
    log(f"  {len(current_df)} tutors, period: {current_period_label}")

    log("Fetching MonthlyMetric for previous-period comparisons...")
    monthly_df = fetch_monthly_metric_df()
    prev_lookup, prev_period_label = get_previous_period_lookup(monthly_df, current_period_label)
    log(f"  Previous period used for Diff M2M rules: {prev_period_label} "
        f"({len(prev_lookup)} tutors)" if prev_period_label else "  No previous period found -- Diff M2M rules won't trigger this run.")

    # Bottom-10th-percentile-per-tier threshold, computed from THIS period's
    # tutor pool only (matches the notebook's per-run quantile calc).
    current_df["delivery_percent_f"] = current_df["delivery_percent"].apply(to_float)
    tier_delivery_p10 = (
        current_df.dropna(subset=["delivery_percent_f", "tier"])
        .groupby("tier")["delivery_percent_f"]
        .quantile(0.10)
        .to_dict()
    )

    output_rows = []
    for _, row in current_df.iterrows():
        tutor_name = row.get("tutor_name")
        if pd.isna(tutor_name):
            continue
        prev = prev_lookup.get(tutor_name)
        concern_group, reasons = score_tutor(row, prev, tier_delivery_p10)
        output_rows.append({
            "Tutor Name": tutor_name,
            "Faculty Leader Name": row.get("fl"),
            "Concern Group": concern_group,
            "Reasons": reasons,
            "Date": current_period_label,
        })

    new_df = pd.DataFrame(output_rows)
    log(f"Scored {len(new_df)} tutors. Concern Group distribution:")
    log(new_df["Concern Group"].value_counts().sort_index().to_string())

    log("Fetching existing Tutor_Concerns.csv...")
    existing_df, sha = fetch_concerns_file()

    if not existing_df.empty and current_period_label in existing_df.get("Date", pd.Series(dtype=object)).values:
        before = len(existing_df)
        existing_df = existing_df[existing_df["Date"] != current_period_label]
        log(f"Found {before - len(existing_df)} existing row(s) for this period -- replacing them.")
    else:
        log("No existing rows for this period -- pure append.")

    combined_df = pd.concat([existing_df, new_df], ignore_index=True)

    if not push_for_real:
        preview_path = os.path.join(SCRIPT_DIR, "Tutor_Concerns_PREVIEW.csv")
        combined_df.to_csv(preview_path, index=False)
        log(f"[DRY RUN] Nothing pushed to GitHub. Saved a local preview -> {preview_path}")
        log(f"[DRY RUN] Would have written {len(combined_df)} total rows "
            f"({len(new_df)} for this period) to {CONCERNS_FILE} in {GITHUB_CODE_REPO}.")
    else:
        push_concerns_file(combined_df, sha)
        log(f"Pushed updated {CONCERNS_FILE} -> {GITHUB_CODE_REPO} "
            f"({len(new_df)} rows for {current_period_label}, {len(combined_df)} rows total).")


if __name__ == "__main__":
    main()
