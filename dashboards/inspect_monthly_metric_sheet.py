#!/usr/bin/env python3
"""
inspect_monthly_metric_sheet.py

One-off: pulls December_Annual_Reviews.xlsx from the repo root and
prints the shape of the MonthlyMetric sheet (columns, dtypes, a few
sample rows, and how "period" is represented) so we can build an
append-and-commit script that matches its actual schema.

Not part of the regular pipeline.
"""

import base64
import io
import os

import pandas as pd
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

FILE_PATH = "December_Annual_Reviews.xlsx"


def main():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FILE_PATH}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    content = base64.b64decode(r.json()["content"])

    xls = pd.ExcelFile(io.BytesIO(content))
    print(f"Sheet names in {FILE_PATH}: {xls.sheet_names}")
    print()

    df = pd.read_excel(xls, sheet_name="MonthlyMetric")
    print(f"MonthlyMetric shape: {df.shape}")
    print()
    print("Columns and dtypes:")
    print(df.dtypes)
    print()
    print("First 5 rows:")
    print(df.head(5).to_string())
    print()
    print("Last 5 rows:")
    print(df.tail(5).to_string())

    # try to spot a date/period column
    for col in df.columns:
        if "date" in col.lower() or "period" in col.lower() or "month" in col.lower() or "week" in col.lower():
            print()
            print(f"Unique values in likely period column '{col}' (most recent 10):")
            try:
                print(sorted(df[col].dropna().unique())[-10:])
            except TypeError:
                print(df[col].dropna().unique()[-10:])


if __name__ == "__main__":
    main()
