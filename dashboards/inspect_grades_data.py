#!/usr/bin/env python3
"""
inspect_grades_data.py

One-off: the students_without_grades metric was wrongly filtering on
the 'academics' column. The real eligibility rule (per the dashboard)
is based on session attendance/scheduling, grade level, etc. -- not
that flag. Need to see the actual columns in grades_data.csv (all of
them, including whatever the TRUE/FALSE columns are) to fix this
correctly.

Not part of the regular pipeline.
"""

import io
import os

import pandas as pd
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ["GITHUB_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}


def main():
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/data/cache/grades_data.csv"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))

    print("Columns:", df.columns.tolist())
    print("dtypes:")
    print(df.dtypes)
    print()
    print(f"Total rows: {len(df)}")
    print()
    print("All rows for tutor_name == 'Valdenis Iancu' (with headers):")
    mask = df["tutor_name"].astype(str).str.contains("Valdenis", case=False, na=False)
    print(df[mask].to_string())


if __name__ == "__main__":
    main()
