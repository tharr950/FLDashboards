#!/usr/bin/env python3
"""
metrics_period.py

Single source of truth for the reporting period (start date / end date)
used across the whole metrics pipeline (pull_kpi_4week_data.py,
pull_fl_dashboard_history.py, build_tutor_metrics_file.py).

Every script accepts --start-date YYYY-MM-DD --end-date YYYY-MM-DD to
pull KPIs for that exact period. If neither is given, it defaults to
the standard rolling 4-week window ending on the most recently
completed Saturday (as of today) -- the normal weekly-run case.

Previously each script computed its own default window independently in
Python (and the SQL query computed its own version via CURRENT_DATE),
which only worked because everything happened to run same-day and
nobody needed a custom range. Now the period is resolved once here and
passed as plain start/end dates everywhere, including straight into the
SQL query as bind parameters -- no more date math duplicated between
Python and SQL.
"""

import argparse
from datetime import date, timedelta

import pandas as pd


def default_period(today=None):
    """
    The standard rolling window: the most recent completed Saturday
    on/before today, and the 27 days before that (a 28-day / 4-week
    span). Used only when no explicit --start-date/--end-date is given.
    """
    today = today or date.today()
    dow = (today.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> Sun=0..Sat=6 (matches Redshift EXTRACT(dow))
    offset = ((dow + 1) % 7) + (7 if dow == 6 else 0)
    day_end = today - timedelta(days=offset)
    day_start = day_end - timedelta(days=27)
    return day_start, day_end


def format_period(day_start, day_end):
    fmt = lambda d: f"{d.month}/{d.day}/{d.strftime('%y')}"
    return f"{fmt(day_start)} - {fmt(day_end)}"


def parse_period_args(argv=None):
    """
    Shared --start-date/--end-date CLI arguments.
    Returns (day_start, day_end) as date objects -- explicit values if
    both are given, otherwise the default rolling 4-week window.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--start-date", dest="start_date", default=None,
                         help="Period start date (YYYY-MM-DD). Must be given together with --end-date.")
    parser.add_argument("--end-date", dest="end_date", default=None,
                         help="Period end date (YYYY-MM-DD). Must be given together with --start-date.")
    args, _ = parser.parse_known_args(argv)

    if args.start_date and args.end_date:
        return date.fromisoformat(args.start_date), date.fromisoformat(args.end_date)
    if args.start_date or args.end_date:
        raise SystemExit("Both --start-date and --end-date must be given together.")

    return default_period()


def read_period_from_kpi_file(kpi_xlsx_path):
    """
    pull_kpi_4week_data.py is the one place you actually choose the
    period (via --start-date/--end-date, or the default). It stamps
    that period onto every row it pulls (period_start/period_end
    columns) so every downstream step -- pull_fl_dashboard_history.py,
    build_tutor_metrics_file.py -- reads the period FROM THE DATA
    instead of needing the same flags passed to them again. That
    duplication is exactly what let the "dates" label and the actual
    pulled data drift apart before; this is the single source of truth.
    """
    df = pd.read_excel(kpi_xlsx_path, usecols=["period_start", "period_end"])
    if df.empty:
        raise RuntimeError(f"{kpi_xlsx_path} has no rows -- can't determine the period.")
    day_start = pd.to_datetime(df["period_start"].iloc[0]).date()
    day_end = pd.to_datetime(df["period_end"].iloc[0]).date()
    return day_start, day_end
