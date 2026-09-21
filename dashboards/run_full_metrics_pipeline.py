#!/usr/bin/env python3
"""
run_full_metrics_pipeline.py

The one command that runs the ENTIRE chain, start to finish:
  0. sync_master_tutor.py         -- refreshes Desktop/Master_Tutor.csv from
                                      Redshift (the same tutor/team query the
                                      dashboard's own cache uses), so the KPI
                                      Tracker automation's roster never goes
                                      stale between runs.
  1. pull_kpi_4week_data.py       -- Redshift 4-week KPI query -> KPI_4Week_Metrics.xlsx
  2. pull_fl_dashboard_history.py -- GitHub history files -> fl_dashboard_history_metrics.csv
  3. build_tutor_metrics_file.py  -- combines both -> metrics.csv
  4. append_to_monthly_metric.py  -- appends metrics.csv into MonthlyMetric and
                                      COMMITS December_Annual_Reviews.xlsx to the
                                      FLDashboards repo
  5. build_tutor_concerns.py      -- scores each tutor's Concern Group/Reasons
                                      (ported from TutorKPISorter.ipynb) and
                                      COMMITS Tutor_Concerns.csv to the
                                      FLDashboards repo. Runs last on purpose --
                                      it reads MonthlyMetric's previous period
                                      (for the "Diff M2M ..." rules), so it
                                      needs step 4's current-period row already
                                      pushed first.

Steps 4 and 5 are the only ones that write anything to GitHub. Steps
1-3 only read (Redshift, GitHub history files) and write local files.

Stops immediately if any step fails (non-zero exit code), so a bad
Redshift connection or a GitHub hiccup doesn't silently produce a
half-populated metrics.csv or a bad commit. Each step's own output
prints straight to the console as it runs.

Flags:
  --start-date YYYY-MM-DD --end-date YYYY-MM-DD
      Run the whole chain for a specific historical period instead of
      the default rolling 4-week window ending today. Only forwarded to
      step 1 -- it stamps the period directly onto KPI_4Week_Metrics.xlsx
      (period_start/period_end columns), and steps 2-4 read the period
      back out of that file instead of needing it passed to them too
      (see metrics_period.py). That's deliberate: the period used to
      only live in CLI flags that had to be manually kept in sync across
      separate scripts, which is exactly the kind of thing that quietly
      drifts.

  --dry-run
      Runs the ENTIRE chain -- Redshift query, GitHub history pulls,
      metrics.csv build -- but steps 4 and 5 stop short of committing.
      Instead of pushing to GitHub, step 4 downloads the real
      December_Annual_Reviews.xlsx, applies the same deletes/appends it
      would for real, and saves the result locally as
      December_Annual_Reviews_PREVIEW.xlsx; step 5 does the same for
      Tutor_Concerns.csv, saving Tutor_Concerns_PREVIEW.csv. This is how
      you test the whole pipeline end-to-end with zero writes to GitHub.

Examples:
  python3 run_full_metrics_pipeline.py                          (today, dry-run off -- pushes for real)
  python3 run_full_metrics_pipeline.py --dry-run                (today, no GitHub push)
  python3 run_full_metrics_pipeline.py --start-date 2026-06-01 --end-date 2026-06-28 --dry-run
"""

import os
import subprocess
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

STEPS = [
    "sync_master_tutor.py",
    "pull_kpi_4week_data.py",
    "pull_fl_dashboard_history.py",
    "build_tutor_metrics_file.py",
    "append_to_monthly_metric.py",
    "build_tutor_concerns.py",
]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def main():
    argv = sys.argv[1:]
    dry_run = "--dry-run" in argv
    period_args = [a for a in argv if a != "--dry-run"]

    label_bits = period_args + (["--dry-run"] if dry_run else [])
    log(f"Starting full metrics pipeline{' (' + ' '.join(label_bits) + ')' if label_bits else ''}")
    if not dry_run:
        log("NOTE: this run WILL commit to the FLDashboards repo in the final step. Pass --dry-run to test without pushing.")

    for step in STEPS:
        path = os.path.join(SCRIPT_DIR, step)
        if step == "pull_kpi_4week_data.py":
            # Only this step takes the period flags -- everything
            # downstream reads the period back out of its output file.
            args = [sys.executable, path] + period_args
        elif step == "append_to_monthly_metric.py":
            # This script pushes to GitHub by default and needs an
            # explicit --dry-run to hold back.
            args = [sys.executable, path] + (["--dry-run"] if dry_run else [])
        elif step == "build_tutor_concerns.py":
            # Opposite default from the step above -- this one is
            # dry-run by default and needs an explicit --push to write
            # to GitHub. Different scripts, different authors/eras;
            # just mapping this orchestrator's single --dry-run flag to
            # whatever each one actually expects.
            args = [sys.executable, path] + ([] if dry_run else ["--push"])
        else:
            args = [sys.executable, path]

        log(f"--- Running {step} ---")
        result = subprocess.run(args, cwd=SCRIPT_DIR)
        if result.returncode != 0:
            log(f"{step} failed (exit code {result.returncode}). Stopping pipeline.")
            sys.exit(result.returncode)

    if dry_run:
        log("Pipeline complete (DRY RUN) -- nothing was pushed to GitHub. "
            "Check December_Annual_Reviews_PREVIEW.xlsx and Tutor_Concerns_PREVIEW.csv.")
    else:
        log("Pipeline complete -- metrics.csv is up to date, and MonthlyMetric + "
            "Tutor_Concerns.csv have been pushed to GitHub.")


if __name__ == "__main__":
    main()
