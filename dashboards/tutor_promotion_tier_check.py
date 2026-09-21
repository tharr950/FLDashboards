#!/usr/bin/env python3
"""
tutor_promotion_tier_check.py
------------------------------
Standalone, cron-driven script (no Claude involvement at runtime).

Every ~2 weeks, for every tutor currently marked "1st Raise Completed" or
"BUC Raise Completed" on the monday.com "Tutor Promotion Raises" board:

  1. Pull their current active student list (sessions scheduled in the
     future) from Redshift.
  2. Work out what fraction of those students count as being at the
     tutor's NEW tier, per the two rules Tyler defined:
       a. any family whose first session on the current booking is after
          the tutor's Promotion Date counts as new-tier, even if the
          purchased tier itself is lower, and
       b. a family only gets ONE purchase at the old (lower) tier after
          the promotion date -- every purchase after that first one counts
          as new-tier regardless of what tier was actually purchased.
  3. If >=50% of a tutor's active students count as new-tier: write them
     to a local CSV report AND flag it on the monday board. It does NOT
     mark the task fully "done" or touch Orbit -- that stays a manual,
     human checkpoint (per the original process doc), on purpose.

IMPORTANT / please read before relying on this:
  - I (Claude) could not test this against real data. Neither this cloud
    sandbox nor the linked-device shell can reach Redshift or api.monday.com
    (both are network-blocked in those sandboxes), so this has only been
    reviewed by reading, not run. RUN IT MANUALLY FIRST and sanity-check
    the output against 1-2 tutors whose status you already know before
    trusting the cron schedule.
  - The "2nd purchase after promotion date" rule needs a family's FULL
    booking history (including bookings that are no longer active) to
    count purchase order correctly -- so this pulls a separate, unfiltered
    history query per tutor for that count, and uses Tyler's original
    query (with the hardcoded date swapped for "today") for the active
    roster / denominator.
  - monday column titles are auto-detected by fuzzy title match (see
    COL_TITLE_CANDIDATES below) rather than hardcoded IDs, since I
    couldn't inspect the board's real column list myself. First run will
    print exactly what it matched -- check that block before trusting
    later runs. If a required column can't be matched confidently, the
    script logs it and skips *writing* for that tutor rather than
    guessing.

Setup (see bottom of file / README section):
  1. Add MONDAY_API_TOKEN=... to this folder's .env (same file as
     REDSHIFT_*/RP_*).
  2. pip install requests psycopg2-binary python-dotenv pandas  (you
     already have these for the other sync_*.py scripts.)
  3. Run manually once: python3 tutor_promotion_tier_check.py
  4. Once you trust the output, schedule it (see CRON SETUP at bottom).
"""

import json
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import psycopg2
import requests
from dotenv import load_dotenv

# ── Paths / config ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

OUT_DIR = os.path.join(SCRIPT_DIR, "tutor_promotion_tier_check_output")
os.makedirs(OUT_DIR, exist_ok=True)
LOG_PATH = os.path.join(OUT_DIR, "run_log.txt")
STATE_PATH = os.path.join(OUT_DIR, "state.json")

MONDAY_BOARD_ID = 18429541208
MONDAY_API_URL = "https://api.monday.com/v2"

ACTIVE_STATUSES = {"1st Raise Completed", "BUC Raise Completed"}
THRESHOLD = 0.50

# Cron/launchd can't easily express "every 14 days" natively, so this
# self-throttles: whatever cadence the OS scheduler actually fires at
# (daily, weekly, whatever), the script itself no-ops unless at least
# this many days have passed since the last real run. Safe against a
# missed fire (laptop asleep, etc.) -- it just runs a bit late instead
# of not at all.
MIN_DAYS_BETWEEN_RUNS = 13

# Fuzzy title candidates for each monday column this script needs.
# EDIT THESE if the first run's "Detected columns" log block shows a
# wrong/missing match for your board.
COL_TITLE_CANDIDATES = {
    "status": ["status", "task status", "raise status"],
    "promotion_date": ["promotion date", "1st raise date", "promo date"],
    "flag": ["ready for final raise", "final raise flag", "flag", "50% flag"],
}


# ── Logging ──────────────────────────────────────────────────────────────
def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


# ── Self-throttle state ────────────────────────────────────────────────
def should_run_now():
    if not os.path.exists(STATE_PATH):
        return True
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        last = datetime.fromisoformat(state["last_run"])
    except Exception:
        return True
    return (datetime.now() - last) >= timedelta(days=MIN_DAYS_BETWEEN_RUNS)


def record_run():
    with open(STATE_PATH, "w") as f:
        json.dump({"last_run": datetime.now().isoformat()}, f)


# ── Redshift ────────────────────────────────────────────────────────────
def get_redshift_conn():
    return psycopg2.connect(
        host=os.environ["REDSHIFT_HOST"],
        port=int(os.environ.get("REDSHIFT_PORT", 5439)),
        dbname=os.environ["REDSHIFT_DB"],
        user=os.environ["REDSHIFT_USER"],
        password=os.environ["REDSHIFT_PASSWORD"],
        connect_timeout=30,
    )


# Same tutor/employee resolution pattern as sync_master_tutor.py --
# reused deliberately so this stays consistent with the dashboard's own
# notion of "active tutor".
ACTIVE_TUTORS_QUERY = """
SELECT DISTINCT
    e.id AS tutor_id,
    tu.first_name || ' ' || tu.last_name AS tutor_name
FROM dw.employees e
JOIN dw.users tu ON e.user_id = tu.id
WHERE e.type = 'Tutor'
  AND e.end_date IS NULL
  AND e.tier_id IS NOT NULL
  AND tu.title = 'Tutor'
"""

# Tyler's query, with the hardcoded HAVING date made dynamic (today,
# instead of a fixed test date) so it reflects "sessions scheduled in
# the future" at whatever moment the script runs.
ACTIVE_ROSTER_QUERY = """
SELECT DISTINCT
tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
student_users.first_name||' '||student_users.last_name AS student_name,
dw.students.id AS student_id,
advisorusers.first_name||' '||advisorusers.last_name AS advisor_name,
dw.courses.id AS course_id,
MIN(dw.sessions.starts_at) AS first_session,
MAX(dw.sessions.starts_at) AS last_scheduled_session,
dw.bookings.booked_at,
dw.bookings.brand_name,
dw.bookings.segment_name,
dw.bookings.duration/60.0 AS booked_hours
FROM dw.sessions
JOIN dw.courses
ON dw.sessions.course_id = dw.courses.id
JOIN dw.enrollments
ON dw.enrollments.course_id = dw.courses.id
JOIN dw.students
ON dw.enrollments.enrollee_id = dw.students.id
JOIN dw.users student_users
ON dw.students.user_id = student_users.id
JOIN dw.employees
ON dw.sessions.supervisor_id = dw.employees.id
JOIN dw.users tutor_users
ON dw.employees.user_id = tutor_users.id
LEFT JOIN dw.line_items
ON dw.enrollments.id = dw.line_items.enrollment_id
JOIN dw.bookings
ON dw.line_items.order_id = dw.bookings.order_id
JOIN dw.parents
ON dw.students.parent_id= dw.parents.id
LEFT JOIN dw.employees advisors
ON dw.parents.advisor_id = advisors.id
LEFT JOIN dw.users advisorusers
ON advisors.user_id = advisorusers.id
WHERE 1=1
AND dw.sessions.supervisor_id IN %(tutor_ids)s
AND dw.courses.brand_id IN (41,42,43,36,2)
AND dw.bookings.duration >0
GROUP BY tutor_name,
student_name,
dw.students.id,
advisor_name,
dw.courses.id,
dw.bookings.booked_at,
dw.bookings.brand_name,
dw.bookings.segment_name,
booked_hours
HAVING last_scheduled_session >= %(today)s
"""

# Same join, but WITHOUT the "active/future" HAVING filter and without
# the session-level columns -- just booking history, needed to correctly
# count "which purchase number is this" per family per rule (b). Rule (b)
# needs bookings that may no longer be active.
BOOKING_HISTORY_QUERY = """
SELECT DISTINCT
tutor_users.first_name||' '||tutor_users.last_name AS tutor_name,
dw.students.id AS student_id,
dw.bookings.booked_at
FROM dw.bookings
JOIN dw.line_items
ON dw.line_items.order_id = dw.bookings.order_id
JOIN dw.enrollments
ON dw.enrollments.id = dw.line_items.enrollment_id
JOIN dw.students
ON dw.enrollments.enrollee_id = dw.students.id
JOIN dw.courses
ON dw.enrollments.course_id = dw.courses.id
JOIN dw.employees
ON dw.courses.id IN (
    SELECT DISTINCT course_id FROM dw.sessions WHERE supervisor_id = dw.employees.id
)
JOIN dw.users tutor_users
ON dw.employees.user_id = tutor_users.id
WHERE dw.employees.id IN %(tutor_ids)s
AND dw.courses.brand_id IN (41,42,43,36,2)
AND dw.bookings.duration > 0
"""
# NOTE: the subquery-based tutor/course link above is a best-effort
# reconstruction (Tyler's original query links tutor->course via
# dw.sessions.supervisor_id, but sessions are exactly what this history
# query intentionally excludes so cancelled/past bookings aren't
# dropped). VERIFY this returns sane row counts before trusting it --
# if it looks wrong, the simplest fix is to pull full session-linked
# booking history the same way as ACTIVE_ROSTER_QUERY but drop the
# HAVING clause entirely, accepting that it'll only see bookings that
# have at least one session recorded.


def fetch_active_tutor_ids(conn, wanted_names):
    """Resolve monday tutor names -> Redshift employee ids."""
    df = pd.read_sql(ACTIVE_TUTORS_QUERY, conn)
    name_to_id = dict(zip(df["tutor_name"], df["tutor_id"]))
    resolved, unmatched = {}, []
    for name in wanted_names:
        if name in name_to_id:
            resolved[name] = name_to_id[name]
        else:
            unmatched.append(name)
    return resolved, unmatched


def fetch_roster(conn, tutor_ids):
    if not tutor_ids:
        return pd.DataFrame()
    return pd.read_sql(
        ACTIVE_ROSTER_QUERY,
        conn,
        params={"tutor_ids": tuple(tutor_ids), "today": date.today().isoformat()},
    )


def fetch_booking_history(conn, tutor_ids):
    if not tutor_ids:
        return pd.DataFrame()
    try:
        return pd.read_sql(BOOKING_HISTORY_QUERY, conn, params={"tutor_ids": tuple(tutor_ids)})
    except Exception as e:
        log(f"WARNING: booking-history query failed ({e}); rule (b) [2nd purchase after "
            f"promotion date] will be skipped this run, only rule (a) [first session after "
            f"promotion date] will be applied. Fix BOOKING_HISTORY_QUERY before trusting "
            f"results near the 50% line.")
        return pd.DataFrame()


# ── monday.com ──────────────────────────────────────────────────────────
def monday_request(query, variables=None):
    token = os.environ["MONDAY_API_TOKEN"]
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": token, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"monday API error: {data['errors']}")
    return data["data"]


def detect_columns(columns):
    """Fuzzy-match this board's real columns against COL_TITLE_CANDIDATES.
    Returns {key: column_id_or_None}. Logs exactly what it matched."""
    detected = {}
    log("Detected monday columns:")
    for key, candidates in COL_TITLE_CANDIDATES.items():
        match = None
        for col in columns:
            title = col["title"].strip().lower()
            if any(c in title or title in c for c in candidates):
                match = col
                break
        detected[key] = match["id"] if match else None
        log(f"  {key!r} -> {match['title'] + ' (id=' + match['id'] + ')' if match else 'NOT FOUND'}")
    return detected


def fetch_board():
    query = """
    query ($boardId: [ID!]) {
      boards(ids: $boardId) {
        columns { id title type }
        items_page(limit: 100) {
          items {
            id
            name
            column_values { id text value column { title } }
          }
        }
      }
    }
    """
    data = monday_request(query, {"boardId": [MONDAY_BOARD_ID]})
    return data["boards"][0]


def get_active_tutors_from_board(board, col_ids):
    status_col = col_ids.get("status")
    promo_col = col_ids.get("promotion_date")
    active = []
    for item in board["items_page"]["items"]:
        values = {cv["id"]: cv["text"] for cv in item["column_values"]}
        status_text = values.get(status_col, "") if status_col else ""
        if status_text not in ACTIVE_STATUSES:
            continue
        promo_date_text = values.get(promo_col) if promo_col else None
        if not promo_date_text:
            log(f"  SKIP {item['name']!r}: status is {status_text!r} but no Promotion Date value found "
                f"(column id {promo_col}) -- can't apply the 50% rule without it.")
            continue
        try:
            promo_date = datetime.strptime(promo_date_text, "%Y-%m-%d").date()
        except ValueError:
            log(f"  SKIP {item['name']!r}: couldn't parse Promotion Date {promo_date_text!r} as YYYY-MM-DD.")
            continue
        active.append({"item_id": item["id"], "name": item["name"], "promotion_date": promo_date})
    return active


def flag_item_on_board(item_id, col_ids, message):
    flag_col = col_ids.get("flag")
    if not flag_col:
        log(f"  NOTE: no 'flag' column detected on the board -- not writing back for item {item_id}; "
            f"relying on the CSV report only. Add a status/text column titled one of "
            f"{COL_TITLE_CANDIDATES['flag']} to enable the board flag.")
        return
    mutation = """
    mutation ($boardId: ID!, $itemId: ID!, $columnId: String!, $value: JSON!) {
      change_column_value(board_id: $boardId, item_id: $itemId, column_id: $columnId, value: $value) {
        id
      }
    }
    """
    # Plain-text style value; if the detected column is a real "status"
    # type column this may need to be the label's index instead of raw
    # text -- check the first run's log for API errors here and adjust.
    value = json.dumps(message)
    try:
        monday_request(
            mutation,
            {"boardId": str(MONDAY_BOARD_ID), "itemId": str(item_id), "columnId": flag_col, "value": value},
        )
        log(f"  Flagged item {item_id} on monday.")
    except Exception as e:
        log(f"  WARNING: failed to write monday flag for item {item_id}: {e}")


# ── Business logic ──────────────────────────────────────────────────────
def compute_new_tier_fraction(roster_df, history_df, promotion_date):
    """Per Tyler's rules:
      (a) a booking counts as new-tier if its first_session is after promotion_date.
      (b) among a family's bookings placed after promotion_date, the 1st is exempt
          (still allowed at the old tier) but the 2nd and every one after that counts
          as new-tier regardless of what was actually purchased.
    Returns (fraction_new_tier, per_student_detail_df).
    """
    if roster_df.empty:
        return None, pd.DataFrame()

    roster_df = roster_df.copy()
    roster_df["first_session"] = pd.to_datetime(roster_df["first_session"])
    roster_df["booked_at"] = pd.to_datetime(roster_df["booked_at"])

    new_tier_students = set()
    all_students = set(roster_df["student_id"].unique())

    # Rule (a): first_session after promotion date, evaluated on the
    # currently-active roster rows directly.
    rule_a_hits = roster_df[roster_df["first_session"].dt.date > promotion_date]
    new_tier_students.update(rule_a_hits["student_id"].unique())

    # Rule (b): needs full booking history (not just active rows).
    if not history_df.empty:
        hist = history_df.copy()
        hist["booked_at"] = pd.to_datetime(hist["booked_at"])
        for student_id, group in hist.groupby("student_id"):
            after = group[group["booked_at"].dt.date > promotion_date].sort_values("booked_at")
            if len(after) >= 2:
                new_tier_students.add(student_id)

    fraction = len(new_tier_students & all_students) / len(all_students) if all_students else 0.0

    detail = (
        roster_df[["student_id", "student_name", "first_session"]]
        .drop_duplicates("student_id")
        .assign(counts_as_new_tier=lambda d: d["student_id"].isin(new_tier_students))
    )
    return fraction, detail


# ── Main ────────────────────────────────────────────────────────────────
def main():
    if "--force" not in sys.argv and not should_run_now():
        log("Last real run was too recent (< %d days) -- skipping (use --force to override)." % MIN_DAYS_BETWEEN_RUNS)
        return

    log("=" * 70)
    log("Starting tutor_promotion_tier_check run.")

    log("Fetching monday board...")
    board = fetch_board()
    col_ids = detect_columns(board["columns"])
    if not col_ids.get("status") or not col_ids.get("promotion_date"):
        log("ABORT: couldn't confidently detect the Status and/or Promotion Date columns "
            "(see 'Detected monday columns' above). Fix COL_TITLE_CANDIDATES at the top of "
            "this file to match your board's real column titles, then re-run with --force.")
        return

    active_tutors = get_active_tutors_from_board(board, col_ids)
    log(f"{len(active_tutors)} tutor(s) currently in an active raise status: "
        f"{[t['name'] for t in active_tutors]}")
    if not active_tutors:
        record_run()
        return

    log("Connecting to Redshift...")
    conn = get_redshift_conn()
    try:
        name_to_id, unmatched = fetch_active_tutor_ids(conn, [t["name"] for t in active_tutors])
        if unmatched:
            log(f"WARNING: couldn't resolve these monday tutor names to a Redshift employee id "
                f"(name mismatch? check spelling/suffixes): {unmatched}")

        tutor_ids = list(name_to_id.values())
        roster_all = fetch_roster(conn, tutor_ids)
        history_all = fetch_booking_history(conn, tutor_ids)
    finally:
        conn.close()

    flagged_rows = []
    for tutor in active_tutors:
        emp_id = name_to_id.get(tutor["name"])
        if emp_id is None:
            continue
        roster = roster_all[roster_all["tutor_name"] == tutor["name"]] if not roster_all.empty else roster_all
        history = history_all[history_all["tutor_name"] == tutor["name"]] if not history_all.empty else history_all

        if roster.empty:
            log(f"{tutor['name']}: no active (future-scheduled) students found -- skipping.")
            continue

        fraction, detail = compute_new_tier_fraction(roster, history, tutor["promotion_date"])
        n_total = detail["student_id"].nunique()
        n_new = int(detail["counts_as_new_tier"].sum())
        log(f"{tutor['name']}: {n_new}/{n_total} active students at new tier "
            f"({fraction:.0%}) -- promotion date {tutor['promotion_date']}")

        if fraction is not None and fraction >= THRESHOLD:
            log(f"  >= {THRESHOLD:.0%} threshold met -- flagging for final raise review.")
            flagged_rows.append({
                "tutor_name": tutor["name"],
                "promotion_date": tutor["promotion_date"],
                "students_total": n_total,
                "students_new_tier": n_new,
                "fraction_new_tier": fraction,
                "run_date": date.today().isoformat(),
            })
            flag_item_on_board(
                tutor["item_id"], col_ids,
                f"Ready for final raise as of {date.today().isoformat()} "
                f"({n_new}/{n_total} = {fraction:.0%} at new tier)",
            )

    if flagged_rows:
        report_path = os.path.join(OUT_DIR, f"flagged_{date.today().isoformat()}.csv")
        pd.DataFrame(flagged_rows).to_csv(report_path, index=False)
        log(f"Wrote report -> {report_path}")
    else:
        log("No tutors crossed the 50% threshold this run.")

    record_run()
    log("Run complete.")


if __name__ == "__main__":
    main()

# ── CRON SETUP ────────────────────────────────────────────────────────
# This has to be scheduled from your actual Mac Terminal -- there's no
# way for me to reach your real crontab/launchd from here.
#
# Quick option (crontab), e.g. every Monday at 8am (script self-throttles
# to ~14 days regardless, so a weekly fire is fine and more resilient to
# a missed/asleep run than trying to hit exactly-14-days in cron syntax):
#
#   crontab -e
#   0 8 * * 1 cd "/Users/tylerharrington/Desktop/Revolution Prep/FL_Dashboards/dashboards" && /usr/bin/python3 tutor_promotion_tier_check.py >> tutor_promotion_tier_check_output/cron.log 2>&1
#
# More precise option (launchd, fires exactly every 14 days regardless
# of day-of-week): create
# ~/Library/LaunchAgents/com.tylerharrington.tutorpromotioncheck.plist
# with a <key>StartInterval</key><integer>1209600</integer> (14*86400)
# pointing at this script, then `launchctl load` it. Ask if you want the
# plist written out -- happy to do that once the cron version is verified.
