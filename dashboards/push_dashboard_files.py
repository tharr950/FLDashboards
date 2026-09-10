#!/usr/bin/env python3
"""
push_dashboard_files.py

One-off utility to push the 7 FL dashboard .py files (the actual
Streamlit app source, deployed from GITHUB_CODE_REPO) to GitHub after a
local edit. There's no git clone of that repo on this machine -- every
other write to it this session (December_Annual_Reviews.xlsx,
Tutor_Concerns.csv) went through the GitHub Contents API directly, so
this does the same thing for these .py files.

Assumes each file lives at the repo root with the same filename it has
locally (matching where December_Annual_Reviews.xlsx sits) -- if the
live app actually has these nested in a subfolder, update FILES below
with the right path (e.g. "pages/Ian.py") and re-run.

Usage:
    python3 push_dashboard_files.py             # dry run (default) -- shows what would change, pushes nothing
    python3 push_dashboard_files.py --push       # pushes for real
"""

import base64
import os
import sys

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_CODE_REPO = os.environ["GITHUB_CODE_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

FILES = [
    "Ian.py",
    "Annelies.py",
    "Ela.py",
    "Kristin.py",
    "Geoff.py",
    "Katherine.py",
    "Nikki.py",
]


def log(msg):
    print(msg, flush=True)


def get_remote_sha(repo_path):
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{repo_path}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()["sha"]


def push_file(repo_path, local_path, sha):
    with open(local_path, "rb") as f:
        content = f.read()
    encoded = base64.b64encode(content).decode("utf-8")
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{repo_path}"
    body = {
        "message": f"app: update {repo_path} (Team Trends Over Time section)",
        "content": encoded,
    }
    if sha:
        body["sha"] = sha
    r = requests.put(url, json=body, headers=HEADERS, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to push {repo_path}: {r.status_code} {r.text[:300]}")


def main():
    push_for_real = "--push" in sys.argv

    for fname in FILES:
        local_path = os.path.join(SCRIPT_DIR, fname)
        if not os.path.exists(local_path):
            log(f"SKIP {fname}: not found locally at {local_path}")
            continue

        sha = get_remote_sha(fname)
        local_size = os.path.getsize(local_path)
        status = "will UPDATE existing file" if sha else "will CREATE new file (no existing file at that path in the repo)"

        if not push_for_real:
            log(f"[DRY RUN] {fname}: {status} ({local_size} bytes)")
        else:
            push_file(fname, local_path, sha)
            log(f"Pushed {fname} -> {GITHUB_CODE_REPO} ({local_size} bytes)")

    if not push_for_real:
        log("")
        log("[DRY RUN] Nothing pushed. If any file above says CREATE instead of UPDATE, "
            "the path is probably wrong (e.g. nested in a subfolder on the real repo) -- "
            "fix FILES in this script before running with --push.")
    else:
        log("")
        log("Done. Give the Streamlit app a minute to pick up the change (it usually redeploys automatically on a push).")


if __name__ == "__main__":
    main()
