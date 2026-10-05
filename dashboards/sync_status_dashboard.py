"""
sync_status_dashboard.py
Builds an HTML health dashboard for every data feed.
Source of truth = the pushed artifact (GitHub commit time + content checks).
Log files are used only to surface error text from the most recent attempt.
"""
import os, io, re, json, base64, requests, pandas as pd
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

TOKEN = os.environ["GITHUB_TOKEN"]
REPO  = os.environ["GITHUB_REPO"]
H     = {"Authorization": f"token {TOKEN}"}
NOW   = datetime.now(timezone.utc)

BASE     = "/Users/tylerharrington/Desktop/Revolution Prep"
SYNC_LOG = f"{BASE}/FL_Dashboards/dashboards/sync_log.txt"
PROG_LOG = f"{BASE}/ProgressUpdates/progress_scorer/analyze_progress_updates.log"

# name, artifact path, cadence(hrs), log file, marker regex for that script's lines
FEEDS = [
    ("BUC Rates",            "data/buc_rates.csv",                       36,  SYNC_LOG, r"buc_rates"),
    ("Brand Permissions",    "data/brand_permissions.csv",               36,  SYNC_LOG, r"brand_permissions"),
    ("Exam Data",            "data/exam_data.csv",                       36,  SYNC_LOG, r"exam_data"),
    ("Exam PDF Reports",     "data/exam_reports/Ela_Cross_exam_report.pdf", 36, SYNC_LOG, r"[Ee]xam report"),
    ("Parent Update Videos", "data/parent_update_videos_history.csv",    204, SYNC_LOG, r"parent_update_videos|\[Worker \d|\[Retry\]|video found"),
    ("Progress Updates",     "data/progress_updates_history.json",       204, PROG_LOG, r"."),
    ("Family Availability",  "data/cache/family_availability.csv",        36, SYNC_LOG, r"family_availability"),
    ("Grades Cache",         "data/cache/grades_data.csv",                36, SYNC_LOG, r"grades_data"),
    ("Archivable Cache",     "data/cache/archivable_unscheduled.csv",     36, SYNC_LOG, r"archivable_unscheduled|archivable"),
    ("Master Tutor Cache",   "data/cache/master_tutor.csv",               36, SYNC_LOG, r"master_tutor"),
]

def commit_time(path):
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/commits",
                         headers=H, params={"path": path, "per_page": 1}, timeout=20)
        if r.status_code == 200 and r.json():
            return datetime.fromisoformat(r.json()[0]["commit"]["committer"]["date"].replace("Z","+00:00"))
    except Exception:
        pass
    return None

def raw(path):
    try:
        r = requests.get(f"https://raw.githubusercontent.com/{REPO}/main/{path}?cb={int(NOW.timestamp())}",
                         headers=H, timeout=30)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None

def quality(name, path):
    """Returns (ok, note). Content-level check that the run actually produced good data."""
    try:
        if name == "Parent Update Videos":
            df = pd.read_csv(io.StringIO(raw(path)))
            wk = sorted(df["week of"].astype(str).unique())[-1]
            pu = df[(df["week of"].astype(str) == wk) &
                    (df["parent update only sent"].astype(str) == "True")].copy()
            pu["L"] = pu["tutor"].astype(str).str[0]
            rate = pu.groupby("L")["video found"].apply(lambda s: (s.astype(str) == "True").mean())
            zero = rate[rate == 0].index.tolist()
            pct  = (pu["video found"].astype(str) == "True").mean() * 100
            if zero:
                return False, f"week {wk} — letters with 0% videos: {', '.join(zero)} (truncated run)"
            return True, f"week {wk} — {pct:.0f}% videos found, no gaps"
        if name == "Progress Updates":
            p = pd.DataFrame(json.loads(raw(path)))
            p["sent_at"] = pd.to_datetime(p["sent_at"])
            wk = p["sent_at"].dt.to_period("W-SAT").apply(lambda x: x.start_time.date()).max()
            return True, f"latest week {wk} — {len(p):,} records"
        if path.endswith(".pdf"):
            return True, "report present"
        txt = raw(path)
        if txt is None:
            return False, "artifact not reachable"
        df = pd.read_csv(io.StringIO(txt))
        stamp = ""
        for col in ("fetched_at", "_cached_at"):
            if col in df.columns and len(df):
                stamp = f" · stamp {df[col].iloc[0]}"
                break
        if df.empty:
            return False, "file is empty"
        return True, f"{len(df):,} rows{stamp}"
    except Exception as e:
        return False, f"check failed: {str(e)[:90]}"

ERR = re.compile(r"error|fail|exception|traceback|could not|unable", re.I)
SKIP = re.compile(r"Retrying in \d+ seconds|DeprecationWarning|UserWarning|MallocStackLogging")
TS   = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")

def log_scan(logfile, marker, days=10):
    """Last attempt date for this script + any error lines on that date."""
    if not os.path.exists(logfile):
        return None, []
    cutoff = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
    pat = re.compile(marker, re.I)
    hits, errs = [], {}
    try:
        with open(logfile, errors="ignore") as fh:
            for line in fh:
                m = TS.search(line)
                if not m or m.group(1) < cutoff:
                    continue
                day = m.group(1)
                if not pat.search(line):
                    continue
                hits.append(day)
                if ERR.search(line) and not SKIP.search(line):
                    errs.setdefault(day, []).append(line.strip()[:200])
    except Exception:
        return None, []
    if not hits:
        return None, []
    last = max(hits)
    return last, errs.get(last, [])[:4]

def dow(dt):
    return dt.strftime("%A %Y-%m-%d") if dt else "—"


# ─────────────────────────── extra sources ───────────────────────────
import glob
RP = "/Users/tylerharrington/Desktop/Revolution Prep"
LOGTS = re.compile(r"\[(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\]")

def _json(path):
    try:
        with open(path) as fh: return json.load(fh)
    except Exception: return None

def _fmt(ts):
    if not ts: return "—"
    try:
        return datetime.fromisoformat(str(ts).replace("Z","")).strftime("%A %Y-%m-%d %H:%M")
    except Exception: return str(ts)

def _age(ts):
    try:
        return (datetime.now() - datetime.fromisoformat(str(ts).replace("Z",""))).total_seconds()/86400
    except Exception: return None

def _lines(path):
    try:
        return open(path, errors="ignore").read().splitlines()
    except Exception: return []

def _last_match(lines, pat):
    """Return (iso_timestamp, line) for the last line matching pat."""
    rx = re.compile(pat)
    for ln in reversed(lines):
        if rx.search(ln):
            m = LOGTS.search(ln)
            return (f"{m.group(1)}T{m.group(2)}" if m else None), ln
    return None, None

COURSE_RX = re.compile(
    r"course (\d+) '([^']*)'\s*\(subject='([^']*)',\s*starts=([0-9T:+\-]+)\)"
    r"(?:,\s*ends ([0-9\-]+))?(?:,\s*end date unknown\s*\(([^)]*)\))?")

def _section(lines, header_pat, start_idx):
    """Collect 'course ...' lines under a header, from start_idx onward."""
    rx, out, inside = re.compile(header_pat), [], False
    for ln in lines[start_idx:]:
        if rx.search(ln):
            inside = True; continue
        if inside:
            m = COURSE_RX.search(ln)
            if m:
                out.append(dict(id=m.group(1), title=m.group(2), subject=m.group(3),
                                starts=m.group(4)[:10], ends=m.group(5),
                                why=m.group(6)))
            elif ln.strip() and not ln.startswith(("  ", "\t")):
                if out: break
    return out

def sgc_cards():
    out = []

    # ── 1. Course creation ────────────────────────────────────────────
    s = _json(f"{RP}/create_small_group_course_output/status.json")
    if s:
        fail = int(s.get("failed_count") or 0)
        out.append(dict(name="Course Creation", ok=(fail == 0),
            when=_fmt(s.get("last_run_at")), age=_age(s.get("last_run_at")),
            detail=(f"mode {s.get('mode','?')} · created {s.get('created_count',0)} · failed {fail}"
                    f" · courses created through <b>{s.get('latest_start_date_created','—')}</b>"
                    + (" · DRY RUN" if s.get("dry_run") else "")),
            errors=[f"{f.get('start_date','?')} {f.get('subject','')}: {f.get('error','')}"[:200]
                    for f in (s.get("failed") or [])][:5]))
    else:
        L = _lines(f"{RP}/create_small_group_course_output/run_log.txt")
        ts, ln = _last_match(L, r"Starting (create_small_group_course run\.|BATCH run from)")
        # cumulative furthest-out start date across per-slot logs + single runs
        dates = []
        for f in glob.glob(f"{RP}/create_small_group_course_output/logs/*.log"):
            dates += re.findall(r"CREATED (\d{4}-\d{2}-\d{2})", open(f, errors="ignore").read())
        dates += re.findall(r"start_date=(\d{4}-\d{2}-\d{2})", "\n".join(L))
        through = max(dates) if dates else "—"
        mode = "batch" if (ln and "BATCH" in ln) else "single"
        out.append(dict(name="Course Creation", ok=(ts is not None),
            when=_fmt(ts), age=_age(ts),
            detail=f"mode {mode} · courses created through <b>{through}</b> · <i>from run_log</i>",
            errors=[]))

    # ── 2. Test Date backfill ─────────────────────────────────────────
    s = _json(f"{RP}/backfill_test_dates_output/status.json")
    if s:
        st  = s.get("stats") or {}
        bad = int(st.get("failed") or 0)
        out.append(dict(name="Test Date Backfill", ok=(bad == 0),
            when=_fmt(s.get("last_run_at")), age=_age(s.get("last_run_at")),
            detail=(f"updated {st.get('updated',0)} · already set {st.get('already_set',0)}"
                    f" · flagged {st.get('flagged',0)} · blank {st.get('left_blank',0)} · failed {bad}"),
            errors=([f"failed ids: {s.get('failed_course_ids')}"] if bad else [])))
    else:
        L = _lines(f"{RP}/backfill_test_dates_output/run_log.txt")
        ts, _ = _last_match(L, r"Starting backfill_test_dates run\.")
        _, summ = _last_match(L, r"SUMMARY:")
        out.append(dict(name="Test Date Backfill", ok=(ts is not None),
            when=_fmt(ts), age=_age(ts),
            detail=((summ.strip()[:160] if summ else "no summary line") + " · <i>from run_log</i>"),
            errors=[]))

    # ── 3 & 4. Daily check: unstaffed + missing test date ─────────────
    s = _json(f"{RP}/daily_course_check_output/status.json")
    if s:
        uns  = [dict(id=c.get("id"), subject=c.get("subject"),
                     starts=str(c.get("starts_at"))[:10], ends=None, why=None)
                for c in (s.get("unstaffed") or [])]
        miss = [dict(id=c.get("id"), subject=c.get("subject"),
                     starts=str(c.get("starts_at"))[:10], ends=c.get("last_day"),
                     why=c.get("last_day_flagged_reason"))
                for c in (s.get("missing_test_date") or [])]
        when, age = _fmt(s.get("last_run_at")), _age(s.get("last_run_at"))
        through   = str(s.get("created_through") or "—")[:10]
        checked   = s.get("total_courses_checked", "?")
        src = ""
    else:
        L = _lines(f"{RP}/daily_course_check_output/run_log.txt")
        starts = [k for k, ln in enumerate(L) if "Starting daily_course_check run." in ln]
        k = starts[-1] if starts else 0
        ts, _ = _last_match(L[:k+1] or L, r"Starting daily_course_check run\.")
        uns   = _section(L, r"UNSTAFFED \(next", k)
        miss  = _section(L, r"MISSING TEST DATE \(next", k)
        when, age = _fmt(ts), _age(ts)
        through, checked, src = "—", "?", " · <i>from run_log</i>"

    rows_ = []
    for c in uns[:6]:
        rows_.append(f"UNSTAFFED · starts {c['starts']} · {c['subject']} · id {c['id']}")
    for c in miss[:6]:
        end = (f" · ends {c['ends']}" if c.get('ends')
               else (f" · end unknown ({c['why']})" if c.get('why') else " · end date pending next run"))
        rows_.append(f"NO TEST DATE · starts {c['starts']} · {c['subject']} · id {c['id']}{end}")

    out.append(dict(name="Daily Course Check", ok=(not uns and not miss),
        when=when, age=age, warn_only=True,
        detail=(f"courses live through <b>{through}</b> · checked {checked} · "
                f"{len(uns)} unstaffed · {len(miss)} missing test date{src}"),
        errors=rows_))
    return out

def payroll_cards():
    out = []

    base = f"{RP}/tutor_promotion_tier_check_output"
    s = _json(f"{base}/state.json")
    L = _lines(f"{base}/run_log.txt")
    starts = [k for k, ln in enumerate(L) if "Starting tutor_promotion_tier_check run." in ln]
    seg = L[starts[-1]:] if starts else L[-200:]
    warns = [l.strip()[:200] for l in seg if "WARNING" in l][:5]
    hard = []
    se = f"{base}/launchd_stderr.log"
    if os.path.exists(se) and os.path.getsize(se) > 0:
        hard = [l[:200] for l in _lines(se)[-6:] if l.strip()]
    lastrun = s.get("last_run") if s else None
    out.append(dict(name="Tutor Promotion Tier Check", ok=(s is not None and not hard),
        when=_fmt(lastrun), age=_age(lastrun),
        detail=("last fully-completed run · Sundays 6am, self-throttles to ~every 13 days"
                + (f" · {len(warns)} warning(s)" if warns else "")),
        errors=(hard + warns)[:6]))

    bp = f"{RP}/FL_Dashboards/break_pay_by_period"
    periods = sorted(d for d in glob.glob(f"{bp}/*_*") if os.path.isdir(d))
    if periods:
        newest = periods[-1]
        det = os.path.join(newest, "break_owed_detail.xlsx")
        when, age = "—", None
        if os.path.exists(det):
            dt = datetime.fromtimestamp(os.path.getmtime(det))
            when, age = dt.strftime("%A %Y-%m-%d %H:%M"), (datetime.now()-dt).total_seconds()/86400
        errs = [l[:200] for l in _lines(f"{bp}/launchd_stderr.log")[-25:]
                if l.strip() and "already recorded" not in l.lower()]
        note = ("detail file written (run completed)" if os.path.exists(det)
                else "no break_owed_detail.xlsx — run did not complete")
        if not os.path.exists(f"{bp}/launchd_stderr.log"):
            note += " · <b>no launchd logs yet — LaunchAgent may not be loaded</b>"
        out.append(dict(name="Break Pay Payroll", ok=(os.path.exists(det) and not errs),
            when=when, age=age,
            detail=f"latest period <b>{os.path.basename(newest)}</b> · {note} · alternate weeks are expected no-ops",
            errors=errs[-5:]))
    else:
        out.append(dict(name="Break Pay Payroll", ok=False, when="—", age=None,
            detail=f"no period folders found under {bp}", errors=[]))
    return out

def render_cards(cards, stale_after):
    if not cards:
        return "<p class='sub'>No status files found yet for this group.</p>"
    h = ["<table><tr><th>Automation</th><th>Status</th><th>Last run</th><th>Age</th><th>Detail</th></tr>"]
    for c in cards:
        if c.get("warn_only"):
            lab, col = ("ATTENTION", "#f59e0b") if c["errors"] else ("OK", "#16a34a")
        elif not c["ok"]:
            lab, col = "PROBLEM", "#dc2626"
        elif c["age"] is not None and c["age"] > stale_after:
            lab, col = "STALE", "#f59e0b"
        else:
            lab, col = "OK", "#16a34a"
        a = f"{c['age']:.1f} d" if c["age"] is not None else "—"
        h.append(f"<tr><td><b>{c['name']}</b></td>"
                 f"<td><span class='pill' style='background:{col}'>{lab}</span></td>"
                 f"<td>{c['when']}</td><td>{a}</td><td>{c['detail']}</td></tr>")
        if c["errors"] and lab != "OK":
            cls = "warn" if c.get("warn_only") else "err"
            h.append("<tr><td colspan='5'>" +
                     "".join(f"<div class='{cls}'>{e}</div>" for e in c["errors"]) + "</td></tr>")
    return "\n".join(h) + "</table>"

SGC  = sgc_cards()
PAY  = payroll_cards()

rows = []
for name, path, hrs, logfile, marker in FEEDS:
    ct = commit_time(path)
    ok, note = quality(name, path)
    age = (NOW - ct).total_seconds()/3600 if ct else 9e9
    fresh = age <= hrs
    last_attempt, errors = log_scan(logfile, marker)
    if ok and fresh:
        status, colour = "OK", "#16a34a"
    elif ok and not fresh:
        status, colour = "STALE", "#f59e0b"
    else:
        status, colour = "PROBLEM", "#dc2626"
    rows.append(dict(name=name, status=status, colour=colour,
                     success=dow(ct.astimezone()) if ct else "—",
                     age=f"{age/24:.1f} d" if ct else "—",
                     attempt=last_attempt or "—", note=note, errors=errors))

bad = [r for r in rows if r["status"] != "OK"]
css = ("body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:1150px;margin:28px auto;"
       "padding:0 20px;color:#1e293b}h1{font-size:23px;margin-bottom:4px}"
       ".sub{color:#64748b;font-size:13px;margin-bottom:22px}"
       "table{border-collapse:collapse;width:100%;font-size:14px}"
       "th{background:#1e293b;color:#fff;text-align:left;padding:9px 12px;font-weight:600}"
       "td{border-bottom:1px solid #e2e8f0;padding:9px 12px;vertical-align:top}"
       ".pill{color:#fff;border-radius:11px;padding:2px 10px;font-size:12px;font-weight:600}"
       ".err{background:#fef2f2;border-left:3px solid #dc2626;padding:8px 11px;margin:4px 0;"
       "font-family:ui-monospace,Menlo,monospace;font-size:11.5px;white-space:pre-wrap;color:#7f1d1d}"
       ".banner{padding:12px 16px;border-radius:8px;margin-bottom:20px;font-size:14px}"
       ".warn{background:#fffbeb;border-left:3px solid #f59e0b;padding:8px 11px;margin:4px 0;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;white-space:pre-wrap;color:#78350f}"
       ".tabs{border-bottom:2px solid #e2e8f0;margin:18px 0 20px}"
       ".tab{display:inline-block;padding:9px 18px;cursor:pointer;font-size:14px;font-weight:600;color:#64748b;border-bottom:2px solid transparent;margin-bottom:-2px}"
       ".tab.on{color:#1e293b;border-bottom-color:#2563eb}.pane{display:none}.pane.on{display:block}")

body = [f"<html><head><meta charset='utf-8'><title>Data Feed Status</title>"
        f"<meta http-equiv='refresh' content='900'><style>{css}</style></head><body>",
        "<h1>Data Feed Status</h1>",
        f"<div class='sub'>Generated {NOW.astimezone():%A %Y-%m-%d %H:%M %Z} · auto-refreshes every 15 min</div>"]

if bad:
    body.append("<div class='banner' style='background:#fef2f2;border-left:4px solid #dc2626'>"
                f"<b>{len(bad)} feed(s) need attention:</b> " + ", ".join(r["name"] for r in bad) + "</div>")
else:
    body.append("<div class='banner' style='background:#f0fdf4;border-left:4px solid #16a34a'>"
                "<b>All feeds healthy.</b></div>")

body.append(
    "<div class='tabs'>"
    "<span class='tab on' onclick=\"pick(0)\">Data Feeds</span>"
    "<span class='tab' onclick=\"pick(1)\">Small Group Courses</span>"
    "<span class='tab' onclick=\"pick(2)\">Payroll</span></div>"
    "<div class='pane on'>")
body.append("<table><tr><th>Feed</th><th>Status</th><th>Last successful update</th><th>Age</th>"
            "<th>Last attempt (log)</th><th>Detail</th></tr>")
for r in rows:
    body.append(f"<tr><td><b>{r['name']}</b></td>"
                f"<td><span class='pill' style='background:{r['colour']}'>{r['status']}</span></td>"
                f"<td>{r['success']}</td><td>{r['age']}</td><td>{r['attempt']}</td><td>{r['note']}</td></tr>")
    if r["errors"] and r["status"] != "OK":
        body.append("<tr><td colspan='6'>" +
                    "".join(f"<div class='err'>{e}</div>" for e in r["errors"]) + "</td></tr>")
body.append("</table>")
body.append("<p class='sub' style='margin-top:18px'>“Last successful update” is the GitHub commit time of that "
            "feed’s artifact, cross-checked against its contents. “Last attempt” comes from the shared log and is "
            "approximate — several scripts write to the same file.</p></div>")

body.append("<div class='pane'><h2 style='font-size:17px'>Small Group Course automations</h2>")
body.append(render_cards(SGC, stale_after=8))
body.append("</div>")

body.append("<div class='pane'><h2 style='font-size:17px'>Payroll automations</h2>")
body.append(render_cards(PAY, stale_after=15))
body.append("</div>")

body.append("<script>function pick(i){document.querySelectorAll('.tab').forEach((t,j)=>"
            "t.classList.toggle('on',i==j));document.querySelectorAll('.pane').forEach((p,j)=>"
            "p.classList.toggle('on',i==j));}</script>")

body.append("<p class='sub' style='display:none'>“Last successful update” is the GitHub commit time of that "
            "feed’s artifact, cross-checked against its contents. “Last attempt” comes from the shared log and is "
            "approximate — several scripts write to the same file.</p></body></html>")

html = "\n".join(body)
out = "/Users/tylerharrington/Desktop/Revolution Prep/FL_Dashboards/sync_status.html"
open(out, "w").write(html)

# push so it's viewable anywhere
try:
    p = "data/sync_status.html"
    r = requests.get(f"https://api.github.com/repos/{REPO}/contents/{p}", headers=H, timeout=20)
    payload = {"message": f"sync status {NOW:%Y-%m-%d %H:%M} UTC",
               "content": base64.b64encode(html.encode()).decode()}
    if r.status_code == 200:
        payload["sha"] = r.json()["sha"]
    requests.put(f"https://api.github.com/repos/{REPO}/contents/{p}", headers=H, json=payload, timeout=30)
except Exception as e:
    print("push failed:", str(e)[:100])

print(f"wrote {out}")
for r in rows:
    print(f"  {r['status']:8} {r['name']:22} {r['success']:26} {r['note'][:70]}")
