#!/usr/bin/env python3
"""
fix_stripped_formula_values.py

Repairs the damage confirmed by compare_pre_post_push.py: our
2026-08-23 append_to_monthly_metric.py push (commit 04e5933b55) went
through an openpyxl load+save round trip, and openpyxl -- which has no
formula engine -- dropped the cached calculated VALUES for every
VLOOKUP formula cell in Tier / Faculty Leader / Adjunct-Professional
across MonthlyMetric, for every period that still used the old
formula-based approach (~2387 rows across 11 periods). Confirmed via
direct pre-push vs post-push comparison (data_only=True): e.g. Aaron
Schneberger's 6/14/26-7/11/26 row went from
Tier=Distinguished/FL=Ian Plamondon/Adjunct=Professional (pre-push,
commit 62350fec87) to all-None (post-push).

Repair strategy, per broken row (Tier cell value starts with "="):
  1. Best source: the commit immediately before our push
     (PRE_PUSH_SHA), which still has the real cached values, looked up
     by the EXACT (Tutor Name, Date Range) key -- this preserves
     historically-accurate Tier/FL/Adjunct for that specific period
     (a tutor's tier or team may have changed since then, so this is
     more correct than using current org data for an old period).
  2. Fallback: the embedded MasterTutor sheet in the CURRENT workbook
     (current org assignment, by tutor name only) -- used only when a
     row has no match in the pre-push snapshot at all. Logged
     separately since it's current-org-as-of-today, not
     period-accurate.
  3. Anything matching neither is left untouched and reported for
     manual review.

Every repaired cell is written as a plain VALUE (not a formula), so it
can never be silently stripped by a future openpyxl round-trip again.
This is a permanent fix, not just a one-time patch.

Usage:
    python3 fix_stripped_formula_values.py            # dry run (default) -- writes a local preview file, pushes nothing
    python3 fix_stripped_formula_values.py --push      # writes the fix back to GitHub for real
"""

import base64
import io
import os
import sys
from collections import defaultdict

import openpyxl
import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_CODE_REPO = os.environ["GITHUB_CODE_REPO"]
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

FILE_PATH = "December_Annual_Reviews.xlsx"
SHEET_NAME = "MonthlyMetric"

PRE_PUSH_SHA = "62350fec87"  # commit immediately before our 2026-08-23 push, confirmed via compare_pre_post_push.py


def log(msg):
    print(msg, flush=True)


def is_formula(v):
    return isinstance(v, str) and v.startswith("=")


def fetch_workbook_at_ref(ref, data_only):
    url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{FILE_PATH}"
    params = {"ref": ref} if ref else {}
    r = requests.get(url, headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    content = base64.b64decode(payload["content"])
    return openpyxl.load_workbook(io.BytesIO(content), data_only=data_only), payload.get("sha")


def build_historical_lookup(wb_pre):
    ws = wb_pre[SHEET_NAME]
    header = [c.value for c in ws[1]]
    tutor_col = header.index("Tutor Name") + 1
    date_col = header.index("Date Range") + 1
    tier_col = header.index("Tier") + 1
    fl_col = header.index("Faculty Leader") + 1
    adj_col = header.index("Adjunct/Professional") + 1

    lookup = {}
    for row in range(2, ws.max_row + 1):
        tutor = ws.cell(row=row, column=tutor_col).value
        date_range = ws.cell(row=row, column=date_col).value
        tier = ws.cell(row=row, column=tier_col).value
        fl = ws.cell(row=row, column=fl_col).value
        adj = ws.cell(row=row, column=adj_col).value
        # Only keep entries that actually have real (non-formula, non-blank) values.
        if not is_formula(tier) and tier not in (None, ""):
            lookup[(tutor, date_range)] = {"Tier": tier, "Faculty Leader": fl, "Adjunct/Professional": adj}
    return lookup


def build_current_org_lookup(wb_current):
    if "MasterTutor" not in wb_current.sheetnames:
        return {}
    mt = wb_current["MasterTutor"]
    header = [c.value for c in mt[1]]
    name_col = header.index("Full Name") + 1
    fl_col = header.index("Faculty Leader") + 1
    adj_col = header.index("Professional/Adjunct") + 1
    tier_col = header.index("Tier") + 1
    lookup = {}
    for row in range(2, mt.max_row + 1):
        name = mt.cell(row=row, column=name_col).value
        if name:
            lookup[name] = {
                "Tier": mt.cell(row=row, column=tier_col).value,
                "Faculty Leader": mt.cell(row=row, column=fl_col).value,
                "Adjunct/Professional": mt.cell(row=row, column=adj_col).value,
            }
    return lookup


def main():
    push_for_real = "--push" in sys.argv

    log("Fetching pre-push snapshot (for historically-accurate values)...")
    wb_pre, _ = fetch_workbook_at_ref(PRE_PUSH_SHA, data_only=True)
    historical_lookup = build_historical_lookup(wb_pre)
    log(f"  Built historical lookup with {len(historical_lookup)} (tutor, period) entries.")

    log("Fetching current live workbook (formulas intact, for editing)...")
    wb_current, sha = fetch_workbook_at_ref(None, data_only=False)
    current_org_lookup = build_current_org_lookup(wb_current)
    log(f"  Built current-org fallback lookup with {len(current_org_lookup)} tutors.")

    ws = wb_current[SHEET_NAME]
    header = [c.value for c in ws[1]]
    tutor_col = header.index("Tutor Name") + 1
    date_col = header.index("Date Range") + 1
    tier_col = header.index("Tier") + 1
    fl_col = header.index("Faculty Leader") + 1
    adj_col = header.index("Adjunct/Professional") + 1

    fixed_via_history = 0
    fixed_via_fallback = 0
    unresolved = []
    fallback_examples = []

    for row in range(2, ws.max_row + 1):
        tier_val = ws.cell(row=row, column=tier_col).value
        if not is_formula(tier_val):
            continue

        tutor = ws.cell(row=row, column=tutor_col).value
        date_range = ws.cell(row=row, column=date_col).value
        key = (tutor, date_range)

        if key in historical_lookup:
            vals = historical_lookup[key]
            ws.cell(row=row, column=tier_col, value=vals["Tier"])
            ws.cell(row=row, column=fl_col, value=vals["Faculty Leader"])
            ws.cell(row=row, column=adj_col, value=vals["Adjunct/Professional"])
            fixed_via_history += 1
        elif tutor in current_org_lookup:
            vals = current_org_lookup[tutor]
            ws.cell(row=row, column=tier_col, value=vals["Tier"])
            ws.cell(row=row, column=fl_col, value=vals["Faculty Leader"])
            ws.cell(row=row, column=adj_col, value=vals["Adjunct/Professional"])
            fixed_via_fallback += 1
            if len(fallback_examples) < 15:
                fallback_examples.append((tutor, date_range, vals))
        else:
            unresolved.append((row, tutor, date_range))

    log("")
    log(f"Fixed via pre-push historical snapshot: {fixed_via_history}")
    log(f"Fixed via current MasterTutor fallback (current org, not period-accurate): {fixed_via_fallback}")
    log(f"Unresolved (left untouched): {len(unresolved)}")

    if fallback_examples:
        log("")
        log("Sample rows fixed via fallback (current org data, review if these look wrong for that period):")
        for tutor, date_range, vals in fallback_examples:
            log(f"  {tutor!r} / {date_range!r} -> {vals}")

    if unresolved:
        log("")
        log(f"Sample of unresolved rows (up to 20 of {len(unresolved)}):")
        for row, tutor, date_range in unresolved[:20]:
            log(f"  row {row}: tutor={tutor!r} date_range={date_range!r}")

    if not push_for_real:
        preview_path = os.path.join(SCRIPT_DIR, "December_Annual_Reviews_FIX_PREVIEW.xlsx")
        wb_current.save(preview_path)
        log("")
        log(f"[DRY RUN] Nothing pushed to GitHub. Saved a local preview -> {preview_path}")
        log("[DRY RUN] Re-run with --push once you've reviewed this output to write the fix for real.")
    else:
        buf = io.BytesIO()
        wb_current.save(buf)
        encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
        url = f"https://api.github.com/repos/{GITHUB_CODE_REPO}/contents/{FILE_PATH}"
        body = {
            "message": "fix: restore Tier/Faculty Leader/Adjunct values stripped by 2026-08-23 openpyxl push",
            "content": encoded,
            "sha": sha,
        }
        r = requests.put(url, json=body, headers=HEADERS, timeout=60)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Failed to push {FILE_PATH}: {r.status_code} {r.text[:300]}")
        log("")
        log(f"Pushed fix -> {GITHUB_CODE_REPO} ({fixed_via_history + fixed_via_fallback} cells repaired).")


if __name__ == "__main__":
    main()
