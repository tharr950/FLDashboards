#!/usr/bin/env python3
"""
inspect_academics_flag.py

One-off: figure out whether the 'academics' column in grades_data.csv
is actually a per-subject classification (this subject is an academic
course, e.g. Algebra II, vs. something else) or actually correlates
1:1 with the brand flags (private_tutoring/buc/school_pay) despite the
name suggesting otherwise. Checks:

1. Does the same subject name show up with both academics=True and
   academics=False in different rows? (proves subject-level if yes)
2. For each tutor, is 'academics' constant across all their rows, and
   does it match whichever of private_tutoring/buc/school_pay is set?
   (proves brand-level if yes)

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

    print("=== Check 1: does the same subject appear with both academics=True and academics=False? ===")
    subj_variance = df.dropna(subset=["subject"]).groupby("subject")["academics"].nunique()
    mixed_subjects = subj_variance[subj_variance > 1]
    print(f"Subjects with mixed academics values: {len(mixed_subjects)} out of {df['subject'].nunique()} distinct subjects")
    print(mixed_subjects.head(20))
    print()

    print("=== Check 2: for known clearly-academic subject names, what's the academics True/False split? ===")
    sample_subjects = ["Algebra II", "Chemistry", "Geometry", "Spanish", "AP Statistics", "Biology", "Calculus"]
    for s in sample_subjects:
        sub = df[df["subject"] == s]
        if len(sub):
            print(f"{s}: {sub['academics'].value_counts(dropna=False).to_dict()} (n={len(sub)})")
    print()

    print("=== Check 3: per-tutor, is academics constant, and does it match brand flags? ===")
    per_tutor = df.groupby("tutor_name").agg(
        academics_nunique=("academics", "nunique"),
        academics_any_true=("academics", "any"),
        private_tutoring_any=("private_tutoring", "any"),
        buc_any=("buc", "any"),
        school_pay_any=("school_pay", "any"),
    )
    print(f"Tutors where academics varies row-to-row: {(per_tutor['academics_nunique'] > 1).sum()} out of {len(per_tutor)}")
    print()
    print("Sample of 10 tutors:")
    print(per_tutor.head(10).to_string())
    print()

    print("=== Check 4: overall correlation -- rows where academics=True, what are the brand flags? ===")
    print(df[df["academics"] == True][["private_tutoring", "buc", "school_pay"]].value_counts())
    print()
    print("=== rows where academics=False, what are the brand flags? ===")
    print(df[df["academics"] == False][["private_tutoring", "buc", "school_pay"]].value_counts())


if __name__ == "__main__":
    main()
