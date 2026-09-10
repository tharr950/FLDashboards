#!/usr/bin/env python3
"""
diagnose_grades_match.py

One-off diagnostic: why did only ~29/207 tutors get a
students_without_grades value? Checks whether it's a real "most tutors
have no Academics students" situation, or a tutor_name mismatch between
grades_data.csv and the rest of the pipeline (same kind of spelling/
format mismatch we ran into during the SharePoint automation).

Not part of the regular pipeline -- just run this once and share the
output.
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


def fetch_csv(relative_path):
    url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/{relative_path}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def main():
    grades = fetch_csv("data/cache/grades_data.csv")

    print("=== grades_data.csv overview ===")
    print(f"Total rows: {len(grades)}")
    print(f"Distinct tutor_name (all rows): {grades['tutor_name'].nunique()}")
    print()
    print("'academics' column raw value counts (checking it parsed as real booleans):")
    print(grades["academics"].value_counts(dropna=False))
    print(f"dtype: {grades['academics'].dtype}")
    print()

    academics_true = grades[grades["academics"] == True]
    print(f"Distinct tutor_name where academics == True: {academics_true['tutor_name'].nunique()}")
    print()

    grades_tutors = set(grades["tutor_name"].dropna().unique())
    academics_tutors = set(academics_true["tutor_name"].dropna().unique())

    kpi_path = os.path.join(SCRIPT_DIR, "KPI_4Week_Metrics.xlsx")
    kpi = pd.read_excel(kpi_path)
    kpi_tutors = set(kpi["tutor_name"].dropna().unique())

    print(f"Distinct tutors in KPI_4Week_Metrics.xlsx: {len(kpi_tutors)}")
    print()

    only_in_kpi = sorted(kpi_tutors - grades_tutors)
    only_in_grades = sorted(grades_tutors - kpi_tutors)

    print(f"Tutors in KPI list but NOT anywhere in grades_data.csv (by exact name match): {len(only_in_kpi)}")
    print(only_in_kpi[:25])
    print()
    print(f"Tutors in grades_data.csv but NOT in KPI list (by exact name match): {len(only_in_grades)}")
    print(only_in_grades[:25])
    print()

    # Overlap between KPI tutor list and the *academics-only* tutor set --
    # this is the number that actually matters for students_without_grades.
    overlap_academics = kpi_tutors & academics_tutors
    print(f"KPI tutors that DO show up with at least one academics==True row: {len(overlap_academics)}")


if __name__ == "__main__":
    main()
