import os
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

st.set_page_config(page_title="BUC Promotion Model — Real Data", page_icon="📈", layout="wide")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.stApp { background: #0f1117; color: #e8eaf0; }
.metric-card { background:#1a1d27; border:1px solid #2a2d3a; border-radius:10px; padding:18px 20px; text-align:center; height:100%; }
.metric-card .label { font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:#6b7280; margin-bottom:6px; }
.metric-card .value { font-family:'JetBrains Mono',monospace; font-size:28px; font-weight:600; line-height:1.1; }
.metric-card .sub { font-size:12px; color:#6b7280; margin-top:6px; }
.green{color:#22c55e;} .yellow{color:#f59e0b;} .red{color:#ef4444;} .blue{color:#60a5fa;}
.intro-box { background:#161a24; border:1px solid #232735; border-radius:10px; padding:18px 22px; margin:10px 0 18px 0; font-size:14px; line-height:1.65; color:#d1d5db; }
.intro-box b { color:#e8eaf0; }
.section-label { font-size:11px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; color:#4b5563; margin:26px 0 10px 0; padding-bottom:6px; border-bottom:1px solid #1f2230; }
.tutor-table { width:100%; border-collapse:collapse; font-size:12px; }
.tutor-table th { font-size:10px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:#4b5563; padding:6px 10px; border-bottom:1px solid #1f2230; text-align:left; }
.tutor-table td { padding:6px 10px; border-bottom:1px solid #161820; color:#d1d5db; }
.pill { display:inline-block; padding:2px 9px; border-radius:20px; font-size:10.5px; font-weight:600; }
.pill-dist{background:#14532d;color:#4ade80;} .pill-adv{background:#3b1f00;color:#fb923c;}
.scenario-current{background:#1e293b;color:#93c5fd;}
div[data-testid="stSidebar"] { background:#0d0f18; border-right:1px solid #1f2230; }
</style>
""", unsafe_allow_html=True)

CHART_BG, PAPER_BG = '#1a1d27', '#0f1117'
AXIS_STYLE = dict(gridcolor='#1f2230', color='#6b7280', zerolinecolor='#1f2230')
FONT = dict(color='#9ca3af', size=11)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "buc_margin_model_data")

# ── Load real data ────────────────────────────────────────────────────────────
@st.cache_data
def load_data(data_dir):
    roster = pd.read_csv(os.path.join(data_dir, "buc_roster.csv"))
    weekly = pd.read_csv(os.path.join(data_dir, "buc_weekly_hours.csv"))
    sessions = pd.read_csv(os.path.join(data_dir, "buc_sessions.csv"), parse_dates=["booked_at", "delivered_at"])
    sessions["lead_days"] = (sessions["delivered_at"] - sessions["booked_at"]).dt.total_seconds() / 86400
    sessions["hours"] = sessions["duration_minutes"] / 60.0
    delivered = sessions[(sessions["attendances_attended_count"] > 0) & (sessions["lead_days"] >= 0)].copy()
    return roster, weekly, delivered

if not os.path.isdir(DATA_DIR):
    st.error(f"Can't find the data folder at `{DATA_DIR}`. Run `buc_margin_data_pull.py` and "
             f"`buc_margin_data_pull_roster.py` first (they write into `buc_margin_model_data/` next to this script).")
    st.stop()

roster, weekly, delivered = load_data(DATA_DIR)

da_roster = roster[roster["tier"].isin(["Distinguished", "Advanced"])].copy()
eligible = da_roster[da_roster["buc_pay_rate"].notnull()].copy()
eligible = eligible.merge(weekly, on="tutor_id", how="left")

tutor_stats = delivered.groupby("tutor_id").agg(
    n_sessions=("session_id", "count"),
    mean_lead=("lead_days", "mean"),
).reset_index()
eligible = eligible.merge(tutor_stats, on="tutor_id", how="left")

pop_mean_lead = delivered["lead_days"].mean()
pop_weekly_hours = weekly["avg_weekly_buc_hours"].median()
MIN_SESSIONS = 15
eligible["uses_own_history"] = eligible["n_sessions"].fillna(0) >= MIN_SESSIONS
eligible["mean_lead_used"] = np.where(eligible["uses_own_history"], eligible["mean_lead"], pop_mean_lead)
eligible["avg_weekly_buc_hours"] = eligible["avg_weekly_buc_hours"].fillna(pop_weekly_hours)

no_backlog_premium = roster[(roster["tier"] == "Premium") & (roster["buc_pay_rate"].isnull())]

# Real total current BUC hours by tier, across the WHOLE roster (not just the D/A promotion pool) —
# used later for the "overall BUC margin" view, which blends in a target Premium volume share.
roster_hours = roster.merge(weekly, on="tutor_id", how="left")
real_tier_hours = roster_hours.groupby("tier")["avg_weekly_buc_hours"].sum()
H_total = float(roster_hours["avg_weekly_buc_hours"].sum())
H_premium_real = float(real_tier_hours.get("Premium", 0.0))
da_weighted_avg_pay = float(np.average(eligible["buc_pay_rate"], weights=eligible["avg_weekly_buc_hours"]))

pop_lead_arr = delivered["lead_days"].to_numpy()
tutor_lead_arrays = {tid: g["lead_days"].to_numpy() for tid, g in delivered.groupby("tutor_id")}

def tutor_lead_array(tid, uses_own):
    if uses_own and tid in tutor_lead_arrays:
        return tutor_lead_arrays[tid]
    return pop_lead_arr

def frac_leq(lead_arr, D):
    if lead_arr is None or len(lead_arr) == 0:
        return 1.0
    return float((lead_arr <= D).mean())

# ── Scenario mechanisms ──────────────────────────────────────────────────────
# Every scenario only changes WHEN a tutor's current BUC pay rate updates after promotion —
# never which rate applies to a specific already-booked hour. Pay always follows whatever
# the tutor's current rate is at the moment a session is delivered. That rule never bends;
# these are just different ways of phasing the rate change in.

def rate_immediate(t, old_pay, new_pay, **p):
    return np.full(np.shape(t), new_pay, dtype=float)

def rate_delay(t, old_pay, new_pay, delay_days=21, **p):
    t = np.asarray(t, dtype=float)
    return np.where(t < delay_days, old_pay, new_pay)

def rate_step(t, old_pay, new_pay, step1_days=14, step2_days=45, **p):
    t = np.asarray(t, dtype=float)
    mid = old_pay + 0.5 * (new_pay - old_pay)
    return np.where(t < step1_days, old_pay, np.where(t < step2_days, mid, new_pay))

def rate_ramp(t, old_pay, new_pay, ramp_days=45, **p):
    t = np.asarray(t, dtype=float)
    frac = np.clip(t / max(ramp_days, 1e-9), 0, 1)
    return old_pay + frac * (new_pay - old_pay)

SCENARIO_FN = {"immediate": rate_immediate, "delay": rate_delay, "natural": rate_delay,
               "step": rate_step, "ramp": rate_ramp}
SCENARIO_LABELS = {
    "immediate": "Immediate Full Match",
    "delay": "Delayed Effective Date (fixed)",
    "natural": "Natural Backlog Drain (per-tutor)",
    "step": "Two-Part Step Raise",
    "ramp": "Gradual Ramp",
}
SCENARIO_ORDER = ["immediate", "delay", "natural", "step", "ramp"]

def expected_cost_rate(lead_arr, old_pay, new_pay, key, params):
    if lead_arr is None or len(lead_arr) == 0:
        return new_pay
    rates = SCENARIO_FN[key](lead_arr, old_pay, new_pay, **params)
    return float(np.mean(rates))

def backlog_margin(old_pay, new_pay, weekly_hours, lead_arr, mean_lead, key, params, bill):
    """Little's-Law backlog size (weekly pace x mean lead time) x the blended pay rate that
    results from applying this scenario's rate-change schedule across the real lead-time mix."""
    backlog_hours = weekly_hours / 7.0 * mean_lead
    cost_rate = expected_cost_rate(lead_arr, old_pay, new_pay, key, params)
    margin_pct = (bill - cost_rate) / bill * 100
    margin_dollars = backlog_hours * (bill - cost_rate)
    baseline_dollars = backlog_hours * (bill - old_pay)
    transition_cost = baseline_dollars - margin_dollars
    return dict(backlog_hours=backlog_hours, margin_pct=margin_pct, margin_dollars=margin_dollars,
                baseline_dollars=baseline_dollars, transition_cost=transition_cost, cost_rate=cost_rate)

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## Model Inputs")
    with st.expander("🔒 Locked billing rates & policy pay rate", expanded=False):
        dist_adv_bill = st.number_input("Distinguished/Advanced billing rate, locked at scheduling ($/hr)", 20.0, 60.0, 39.0, 0.5)
        premium_bill  = st.number_input("Premium billing rate, locked at scheduling ($/hr)", 40.0, 120.0, 78.0, 0.5)
        premium_base_pay = st.number_input("New Premium BUC pay rate ($/hr) — a policy decision, not in the data", 25.0, 50.0, 35.0, 0.5)

    st.markdown("---")
    st.markdown("### 👥 Who gets promoted")
    target_n = st.slider("How many tutors to promote to Premium?", 1, len(eligible), min(10, len(eligible)), 1,
                          help="We're not promoting all 169 at once — this picks a realistic batch. We auto-select the "
                               "highest-value tutors (by ongoing monthly margin gain), which naturally comes out mostly "
                               "Distinguished with a few Advanced mixed in. Fine-tune the exact list further down.")
    premium_target_share = st.slider("Target: Premium share of total BUC hours, once ramped up (%)", 0, 100, 30, 1,
                                      help="Real data today: Distinguished/Advanced/Premium hours split roughly "
                                           f"{real_tier_hours.get('Distinguished',0)/H_total*100:.0f}% / "
                                           f"{real_tier_hours.get('Advanced',0)/H_total*100:.0f}% / "
                                           f"{real_tier_hours.get('Premium',0)/H_total*100:.0f}% of {H_total:,.0f} total "
                                           "weekly hours. This sets the assumed future target — hours beyond your tracked "
                                           "batch are assumed to already be fully transitioned (flat $35/hr), since they "
                                           "aren't individually tracked.")

    st.markdown("---")
    st.markdown("### 🧭 Scenario")
    scenario_label = st.selectbox("Scenario to model in detail below", [SCENARIO_LABELS[k] for k in SCENARIO_ORDER], index=1)
    scenario_key = {v: k for k, v in SCENARIO_LABELS.items()}[scenario_label]

    st.markdown("---")
    st.markdown("### ⚙️ Scenario parameters")
    st.caption("Only the slider(s) for your selected scenario affect it — all five are shown here so you can compare them below.")
    delay_days = st.slider("Delayed Effective Date: delay length (days)", 0, 120, 21, 1,
                            help="Pay stays at the tutor's old rate for this many days after promotion, then jumps to the full Premium rate.")
    natural_pctile = st.slider("Natural Backlog Drain: % of each tutor's own backlog to protect", 50, 99, 90, 1,
                                help="The per-tutor delay is set to that tutor's own historical lead-time percentile — long enough to cover this share of their typical backlog.")
    step1_days = st.slider("Two-Part Step Raise: Step 1 length (days at old rate)", 0, 120, 14, 1)
    step2_days = st.slider("Two-Part Step Raise: Step 2 length (days at halfway rate, from promotion)", 1, 150, 45, 1)
    if step2_days <= step1_days:
        step2_days = step1_days + 1
    ramp_days = st.slider("Gradual Ramp: days to phase from old rate to full Premium", 1, 120, 45, 1)

    st.markdown("---")
    st.markdown("### 🚀 Promotion Order Optimizer")
    st.caption(f"{len(eligible)} Distinguished/Advanced tutors are currently active in BUC (real BUC pay rate on file) — "
               f"you've chosen to promote {target_n} of them. "
               f"{eligible['uses_own_history'].sum()} of the full 169 have enough of their own session history "
               f"({MIN_SESSIONS}+ sessions) to use their own lead-time pattern; the rest fall back to the "
               f"company-wide average ({pop_mean_lead:.1f} days).")
    promotions_per_month = st.slider("Promotions per month (pace)", 1, 20, 4)
    horizon_months = st.slider("Months to project", 3, 24, 12)
    margin_floor = st.slider("Don't let blended margin (promoted batch) dip below (%)", 0, 55, 28, 1,
                              help="Calibrated to sit a few points under a typical 10-tutor batch's own starting margin "
                                   "(~33%), so 'max safe pace' below reflects a real constraint. If you change how many "
                                   "tutors you're promoting, revisit this — too high a floor (above the batch's own "
                                   "current margin) makes every scenario show 0/mo, since that's not a promotion risk at "
                                   "all, just the floor being stricter than where you're already starting from.")

pop_natural_delay = float(np.percentile(pop_lead_arr, natural_pctile))
eligible["natural_delay_days"] = [
    float(np.percentile(tutor_lead_array(tid, uses_own), natural_pctile))
    for tid, uses_own in zip(eligible["tutor_id"], eligible["uses_own_history"])
]

# The "who gets promoted" pool: highest ongoing-monthly-gain tutors first (a scenario-invariant
# ranking — it's about which tutors are worth promoting at all, not about how you phase in the raise).
eligible["monthly_benefit_est"] = eligible["avg_weekly_buc_hours"] * 4.345 * (
    (premium_bill - premium_base_pay) - (dist_adv_bill - eligible["buc_pay_rate"]))
target_pool = eligible.nlargest(target_n, "monthly_benefit_est").copy()

def scenario_params_for(key, tutor_natural_delay=None):
    if key == "immediate":
        return {}
    if key == "delay":
        return {"delay_days": delay_days}
    if key == "natural":
        return {"delay_days": tutor_natural_delay if tutor_natural_delay is not None else pop_natural_delay}
    if key == "step":
        return {"step1_days": step1_days, "step2_days": step2_days}
    if key == "ramp":
        return {"ramp_days": ramp_days}
    return {}

def scenario_desc(key):
    return {
        "immediate": "Pay jumps to the full Premium rate immediately on promotion — no delay, no steps.",
        "delay": f"Pay stays at the tutor's old rate for a fixed {delay_days}-day window after promotion, then jumps to Premium.",
        "natural": f"Delay is set per tutor — long enough to cover {natural_pctile}% of that tutor's own historical booking-to-delivery lag (population avg ≈{pop_natural_delay:.0f}d).",
        "step": f"Two steps: old rate until day {step1_days}, a halfway rate until day {step2_days}, then full Premium.",
        "ramp": f"Pay rises in a straight line from the old rate to full Premium over {ramp_days} days.",
    }[key]

def scenario_window(key):
    return {"immediate": 0, "delay": delay_days, "natural": pop_natural_delay,
            "step": step2_days, "ramp": ramp_days}[key]

def batch_rev_cost_by_month(order_df, months, key):
    """Per-month (revenue $, cost $) for a promoted cohort — the shared building block behind
    both the batch-only margin view and the whole-business margin view. Each tutor's PAY rate
    follows this scenario's own schedule curve (the same curve drawn in the mechanism chart)
    sampled at the midpoint of each month since their promotion. Family BILLING is not a policy
    choice — it just takes however long the pre-promotion backlog naturally takes to clear (the
    real, population-average lead time) before new Premium-priced bookings dominate what's
    delivered, so it follows that same natural timeline regardless of which pay scenario is
    selected."""
    results = []
    for m in range(months):
        rev = cost = 0.0
        for r in order_df.itertuples():
            hrs = r.weekly_hours * 4.345
            if m < r.promo_month:
                rev += dist_adv_bill * hrs
                cost += r.buc_pay_rate * hrs
            else:
                t = np.array([(m - r.promo_month + 0.5) * 30.44])
                pay_rate = float(SCENARIO_FN[key](t, r.buc_pay_rate, premium_base_pay, **r.schedule_params)[0])
                rev_rate = float(rate_delay(t, dist_adv_bill, premium_bill, delay_days=pop_mean_lead)[0])
                rev += rev_rate * hrs
                cost += pay_rate * hrs
        results.append((rev, cost))
    return results

def simulate_by_scenario(order_df, months, key):
    """Month-by-month blended margin (%) for a promoted cohort, in isolation — just this batch,
    not the whole business. See batch_rev_cost_by_month for how the underlying $ are built."""
    rev_cost = batch_rev_cost_by_month(order_df, months, key)
    return [((rev - cost) / rev * 100 if rev > 0 else 0.0) for rev, cost in rev_cost]

def simulate_overall_by_scenario(order_df, months, key, flat_premium_hours, remaining_DA_hours, da_avg_pay):
    """Month-by-month blended margin (%) for the WHOLE business, not just the tracked batch:
    the batch's own dynamic $ (from batch_rev_cost_by_month) plus two constant monthly blocks
    for volume that isn't individually tracked — hours already assumed fully Premium (flat pay/
    bill, since only the tracked batch's transition timeline is modeled hour-by-hour) and the
    remaining Distinguished/Advanced hours that never change tier at all. Only the tracked
    batch's promotions actually move over time here; everything else is held constant."""
    monthly_flat_premium_rev = flat_premium_hours * 4.345 * premium_bill
    monthly_flat_premium_cost = flat_premium_hours * 4.345 * premium_base_pay
    monthly_da_rev = remaining_DA_hours * 4.345 * dist_adv_bill
    monthly_da_cost = remaining_DA_hours * 4.345 * da_avg_pay
    rev_cost = batch_rev_cost_by_month(order_df, months, key)
    margins = []
    for rev, cost in rev_cost:
        total_rev = rev + monthly_flat_premium_rev + monthly_da_rev
        total_cost = cost + monthly_flat_premium_cost + monthly_da_cost
        margins.append((total_rev - total_cost) / total_rev * 100 if total_rev > 0 else 0.0)
    return margins

# ── Header ─────────────────────────────────────────────────────────────────────
st.markdown("# BUC Promotion Model")
st.markdown(f"<p style='color:#6b7280;font-size:13px;margin-top:-10px;'>Built on real Redshift/MySQL pulls: "
            f"{len(delivered):,} delivered BUC sessions (last 12mo) · {len(roster)} active tutors · "
            f"pulled {pd.Timestamp.now():%b %d, %Y}</p>", unsafe_allow_html=True)

pop_median_lead = float(np.median(pop_lead_arr))
st.markdown(f"""
<div class="intro-box">
<b>The rule, unchanged:</b> billing locks in at scheduling (${dist_adv_bill:.0f}/hr Distinguished/Advanced, ${premium_bill:.0f}/hr
Premium), tutor pay always follows the BUC rate in effect at delivery. <b>{len(no_backlog_premium)} of {len(roster[roster.tier=='Premium'])}
current Premium tutors have never done BUC work</b> (no pay rate on file for it) — when they start, there's no backlog, same as
the "0 BUC families" rule. The real exposure is the <b>{len(eligible)} Distinguished/Advanced tutors already active in BUC</b>: from
their actual session history, the median booking-to-delivery lead time is <b>{pop_median_lead:.0f} days</b> (mean {pop_mean_lead:.0f},
90th percentile {np.percentile(pop_lead_arr,90):.0f}) — this is measured, not assumed. Every scenario below only changes <i>when</i>
a promoted tutor's pay updates — never which rate applies to a specific already-booked hour.
</div>
""", unsafe_allow_html=True)

# ── Real lead-time distribution ──────────────────────────────────────────────────
st.markdown('<div class="section-label">Real lead-time distribution (86k+ delivered BUC sessions, last 12mo)</div>', unsafe_allow_html=True)
c1, c2, c3, c4 = st.columns(4)
for col, label, val in [
    (c1, "Median lead time", f"{pop_median_lead:.0f}d"),
    (c2, "Mean lead time", f"{pop_mean_lead:.0f}d"),
    (c3, "90th percentile", f"{np.percentile(pop_lead_arr,90):.0f}d"),
    (c4, "Longest tail (99th pct)", f"{np.percentile(pop_lead_arr,99):.0f}d"),
]:
    with col:
        st.markdown(f"""<div class="metric-card"><div class="label">{label}</div>
            <div class="value blue">{val}</div></div>""", unsafe_allow_html=True)

fig_hist = go.Figure()
fig_hist.add_trace(go.Histogram(x=pop_lead_arr[pop_lead_arr <= 150], nbinsx=60, marker_color='#60a5fa',
                                 hovertemplate="Lead time: %{x:.0f}d<br>Sessions: %{y}<extra></extra>"))
fig_hist.add_vline(x=delay_days, line_dash="dot", line_color="#f59e0b", line_width=2,
                    annotation_text=f"Fixed Delay setting ({delay_days}d)", annotation_font_color="#f59e0b")
fig_hist.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
    xaxis=dict(title="Booking-to-delivery lead time (days)", **AXIS_STYLE),
    yaxis=dict(title="Delivered sessions", **AXIS_STYLE),
    margin=dict(l=10, r=20, t=20, b=10), height=320, bargap=0.05)
st.plotly_chart(fig_hist, width="stretch")
st.caption(f"{frac_leq(pop_lead_arr, delay_days)*100:.0f}% of historical sessions deliver within {delay_days} days of booking "
           f"(still at the old rate under a Fixed Delay policy of that length) — the rest would already be at the new rate.")

# ── Scenario mechanism chart ──────────────────────────────────────────────────
st.markdown('<div class="section-label">How each scenario phases in the pay raise</div>', unsafe_allow_html=True)
st.caption("Same promotion, five different rate-change schedules — shown for a representative Distinguished/Advanced → Premium "
           "transition. The rule never changes: whatever line a scenario is on at a given day-since-promotion is the rate a "
           "session delivered that many days later actually gets paid.")
pop_old_pay = float((da_roster["buc_pay_rate"]).mean())
days_axis = np.linspace(0, 150, 151)
colors = {"immediate": "#ef4444", "delay": "#22c55e", "natural": "#60a5fa", "step": "#f59e0b", "ramp": "#a78bfa"}
fig_mech = go.Figure()
for key in SCENARIO_ORDER:
    params = scenario_params_for(key, pop_natural_delay if key == "natural" else None)
    rates = SCENARIO_FN[key](days_axis, pop_old_pay, premium_base_pay, **params)
    fig_mech.add_trace(go.Scatter(x=days_axis, y=rates, mode="lines", name=SCENARIO_LABELS[key],
        line=dict(color=colors[key], width=3.5 if key == scenario_key else 1.75)))
fig_mech.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
    xaxis=dict(title="Days since promotion", **AXIS_STYLE),
    yaxis=dict(title="Tutor's BUC pay rate ($/hr)", **AXIS_STYLE),
    margin=dict(l=10, r=20, t=20, b=10), height=360,
    legend=dict(orientation="h", y=-0.2, font=dict(color="#9ca3af", size=11)))
st.plotly_chart(fig_mech, width="stretch")

# ── Who's being promoted ──────────────────────────────────────────────────────
st.markdown('<div class="section-label">Who\'s being promoted</div>', unsafe_allow_html=True)
st.caption(f"We're not promoting all {len(eligible)} eligible Distinguished/Advanced tutors — auto-picked the "
           f"{target_n} highest ongoing-value tutors to promote (set that count in the sidebar). Override the exact "
           f"list here if you'd rather hand-pick — every chart and table below (including \"this batch\" margin) "
           f"updates to match whatever's selected here.")
tutor_options = {f"{int(r.tutor_id)} — {r.tier}, ${r.buc_pay_rate:.2f}/hr, {r.avg_weekly_buc_hours:.1f} hrs/wk": int(r.tutor_id)
                  for r in eligible.itertuples()}
id_to_label = {v: k for k, v in tutor_options.items()}
default_labels = [id_to_label[tid] for tid in target_pool["tutor_id"] if tid in id_to_label]
selected_labels = st.multiselect("Tutors to promote", list(tutor_options.keys()), default=default_labels)
selected_ids = [tutor_options[l] for l in selected_labels]
pool = eligible[eligible["tutor_id"].isin(selected_ids)].copy()

if len(pool) == 0:
    st.warning("No tutors selected — using the auto-picked batch below until you choose at least one.")
    pool = target_pool.copy()

pool_tier_counts = pool["tier"].value_counts().to_dict()
pool_tier_summary = ", ".join(f"{v} {k}" for k, v in pool_tier_counts.items())
is_auto_pool = set(selected_ids) == set(target_pool["tutor_id"])

batch_old_pay = float(pool["buc_pay_rate"].mean())
batch_weekly_hours = float(pool["avg_weekly_buc_hours"].mean())
batch_mean_lead = float(pool["mean_lead_used"].mean())
batch_backlog = batch_weekly_hours / 7.0 * batch_mean_lead
H_batch = float(pool["avg_weekly_buc_hours"].sum())

# The volume-mix assumption for "overall BUC margin": target Premium share of TOTAL real hours,
# minus whatever this tracked batch itself contributes (that portion follows the scenario's own
# transition timeline instead of being assumed instantly at the flat rate).
Premium_target_hours = premium_target_share / 100.0 * H_total
flat_premium_hours = max(Premium_target_hours - H_batch, 0.0)
remaining_DA_hours = max(H_total - Premium_target_hours, 0.0)
batch_exceeds_target = H_batch > Premium_target_hours

# ── Scenario comparison ────────────────────────────────────────────────────────
st.markdown(f'<div class="section-label">Scenario comparison — promoting {len(pool)} tutors ({pool_tier_summary})</div>', unsafe_allow_html=True)
st.caption(f"We're not promoting all {len(eligible)} eligible Distinguished/Advanced tutors — just the {len(pool)} "
           + ("auto-picked as the highest ongoing-value tutors to promote" if is_auto_pool else "you hand-picked above")
           + ". Change the selection in \"Who's being promoted\" above to update everything below.")

st.markdown(f"""
<div class="intro-box">
<b>Before the numbers — what "temporary margin give-up" actually means:</b> nobody writes a check for it; it isn't a
bill. Here's the mechanism, using the average tutor in your {len(pool)}-tutor batch as an example: earning
${batch_old_pay:.2f}/hr today, delivering about {batch_weekly_hours:.1f} hrs/week of BUC families booked roughly
{batch_mean_lead:.0f} days ahead of time on average, with roughly <b>{batch_backlog:.0f} hours</b> already on the books
at any moment — booked by families at the old ${dist_adv_bill:.0f}/hr rate, not yet delivered. The moment this tutor is
promoted, any <i>new</i> family who books them from then on pays the new ${premium_bill:.0f}/hr rate — no issue there.
But depending on the scenario, some or all of that already-booked backlog may get paid at the new ${premium_base_pay:.0f}/hr
pay rate <i>before</i> the family side has caught up to ${premium_bill:.0f}/hr billing. Every hour where that happens
earns less margin than it would have if we'd left this tutor's pay alone a little longer — <b>add that shortfall up
across the whole backlog and that's the dollar figure below.</b> It's temporary and self-resolving: once the backlog is
delivered, every future hour bills ${premium_bill:.0f} and pays ${premium_base_pay:.0f} — a permanently better margin
than today, for good.
</div>
""", unsafe_allow_html=True)

current_margin = (dist_adv_bill - batch_old_pay) / dist_adv_bill * 100
steady_margin = (premium_bill - premium_base_pay) / premium_bill * 100
c1, c2 = st.columns(2)
with c1:
    st.markdown(f"""<div class="metric-card"><div class="label">Today's margin (this batch, avg)</div>
        <div class="value blue">{current_margin:.1f}%</div>
        <div class="sub">${batch_old_pay:.2f}/hr avg pay vs. ${dist_adv_bill:.0f}/hr billing</div></div>""", unsafe_allow_html=True)
with c2:
    st.markdown(f"""<div class="metric-card"><div class="label">Target margin once fully Premium</div>
        <div class="value green">{steady_margin:.1f}%</div>
        <div class="sub">${premium_base_pay:.0f}/hr pay vs. ${premium_bill:.0f}/hr billing — same for every scenario</div></div>""", unsafe_allow_html=True)
st.markdown("<br>", unsafe_allow_html=True)
if margin_floor > current_margin:
    st.warning(f"Your floor ({margin_floor}%) is set *above* this batch's own starting margin ({current_margin:.1f}%) — "
               f"every scenario below will show \"0/mo\" for max safe pace, but that's not a promotion risk, it's just "
               f"the floor being stricter than where these tutors already sit today. Lower the floor below {current_margin:.0f}% "
               f"to see how the scenarios actually compare.")
st.caption(f"The question below is how each scenario gets your {len(pool)}-tutor batch from {current_margin:.1f}% "
           f"to {steady_margin:.1f}% — how much it temporarily costs, and how many tutors a month you can safely promote "
           f"while getting there.")

def rank_for_scenario(key, pool_df, pace):
    rows = []
    for r in pool_df.itertuples():
        lead_arr = tutor_lead_array(r.tutor_id, r.uses_own_history)
        params = scenario_params_for(key, getattr(r, "natural_delay_days", None))
        bm = backlog_margin(r.buc_pay_rate, premium_base_pay, r.avg_weekly_buc_hours, lead_arr, r.mean_lead_used, key, params, dist_adv_bill)
        monthly_benefit = r.avg_weekly_buc_hours * 4.345 * ((premium_bill - premium_base_pay) - (dist_adv_bill - r.buc_pay_rate))
        payback = bm["transition_cost"] / monthly_benefit if monthly_benefit > 0 else np.inf
        rows.append({
            "tutor_id": r.tutor_id, "tier": r.tier, "buc_pay_rate": r.buc_pay_rate,
            "weekly_hours": r.avg_weekly_buc_hours, "mean_lead_days": r.mean_lead_used,
            "own_history": r.uses_own_history, "transition_cost": bm["transition_cost"],
            "monthly_benefit": monthly_benefit, "payback_months": payback,
            "backlog_hours": bm["backlog_hours"], "margin_dollars": bm["margin_dollars"],
            "schedule_key": key, "schedule_params": params,
        })
    ranked = pd.DataFrame(rows).sort_values("payback_months").reset_index(drop=True)
    ranked["promo_month"] = (ranked.index // pace).astype(int)
    return ranked

scenario_rankings = {key: rank_for_scenario(key, pool, promotions_per_month) for key in SCENARIO_ORDER}

MAX_PACE_SEARCH, HORIZON_CAP = 20, 24

def max_safe_pace(key, pool_df, floor):
    """The fastest pace (tutors/month) that never lets the pool's blended margin dip below
    your floor. Answers 'how many can we promote, how fast' directly, instead of making you
    guess-and-check with the pace slider."""
    if len(pool_df) == 0:
        return 0
    safe_paces = []
    for pace in range(1, MAX_PACE_SEARCH + 1):
        rk = rank_for_scenario(key, pool_df, pace)
        months = max(1, min(int(np.ceil(len(rk) / pace)), HORIZON_CAP))
        m = simulate_by_scenario(rk, months, key)
        if m and min(m) >= floor:
            safe_paces.append(pace)
    return max(safe_paces) if safe_paces else 0

with st.spinner("Finding the safe promotion pace for each scenario…"):
    comparison_rows = []
    for key in SCENARIO_ORDER:
        rk = scenario_rankings[key]
        total_transition = rk["transition_cost"].sum()
        wave1 = rk.head(promotions_per_month)
        best_pace = max_safe_pace(key, pool, margin_floor)
        months_to_finish = int(np.ceil(len(pool) / best_pace)) if best_pace > 0 else None
        comparison_rows.append(dict(
            key=key, label=SCENARIO_LABELS[key], desc=scenario_desc(key), window=scenario_window(key),
            total_transition=total_transition, best_pace=best_pace, months_to_finish=months_to_finish,
            wave1_ids=wave1["tutor_id"].astype(int).tolist(),
        ))
comparison_df = pd.DataFrame(comparison_rows)
precomputed_max_pace = {row["key"]: row["best_pace"] for row in comparison_rows}

fig_cmp = go.Figure()
fig_cmp.add_trace(go.Bar(x=[SCENARIO_LABELS[k] for k in SCENARIO_ORDER],
                          y=[comparison_df.set_index("key").loc[k, "total_transition"] for k in SCENARIO_ORDER],
                          marker_color=[colors[k] for k in SCENARIO_ORDER],
                          hovertemplate="%{x}<br>Temporary margin give-up: $%{y:,.0f}<extra></extra>"))
fig_cmp.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
    yaxis=dict(title="Temporary margin give-up, total ($)", **AXIS_STYLE),
    xaxis=dict(**AXIS_STYLE), margin=dict(l=10, r=20, t=20, b=10), height=340)
st.plotly_chart(fig_cmp, width="stretch")

rows_html = ""
for row in comparison_rows:
    is_current = row["key"] == scenario_key
    wave_preview = ", ".join(str(t) for t in row["wave1_ids"][:8]) + (" …" if len(row["wave1_ids"]) > 8 else "")
    label_html = f'<span class="pill scenario-current">{row["label"]}</span>' if is_current else row["label"]
    pace_txt = f"{row['best_pace']}/mo" + ("+" if row["best_pace"] >= MAX_PACE_SEARCH else "")
    finish_txt = f"{row['months_to_finish']} mo" if row["months_to_finish"] else "not at any tested pace"
    rows_html += f"""<tr><td>{label_html}</td><td style="max-width:230px;">{row['desc']}</td>
        <td>{row['window']:.0f}d</td><td>${row['total_transition']:,.0f}</td>
        <td>{pace_txt}</td><td>{finish_txt}</td><td style="max-width:170px;">{wave_preview}</td></tr>"""
st.markdown(f"""<table class="tutor-table">
  <thead><tr><th>Scenario</th><th>How it works</th><th>Protection window</th>
    <th>Temp. margin give-up (total)</th><th>Max safe pace<br>(floor: {margin_floor}%)</th>
    <th>Time to promote all {len(pool)}</th><th>Wave 1 at your pace ({promotions_per_month}/mo)</th></tr></thead>
  <tbody>{rows_html}</tbody></table>""", unsafe_allow_html=True)
st.caption(f"\"Temp. margin give-up (total)\" doesn't change with pace — it's just the sum of every tutor's own backlog "
           f"exposure. \"Max safe pace\" is the real pace question: how many tutors a month you can promote at once "
           f"without the whole pool's blended margin dipping below your {margin_floor}% floor at any point. A faster "
           f"pace means more tutors are mid-transition simultaneously, which is what actually drags the blended margin "
           f"down — not the total dollar figure. \"Wave 1\" shows who's recommended first at your current pace-slider "
           f"setting ({promotions_per_month}/mo), for comparison across scenarios.")

with st.expander("📐 See the exact formulas", expanded=False):
    example_backlog = batch_backlog
    example_params = scenario_params_for(scenario_key, pop_natural_delay if scenario_key == "natural" else None)
    example_cost_rate = expected_cost_rate(pop_lead_arr, batch_old_pay, premium_base_pay, scenario_key, example_params)
    example_transition = example_backlog * (example_cost_rate - batch_old_pay)
    example_benefit = batch_weekly_hours * 4.345 * ((premium_bill - premium_base_pay) - (dist_adv_bill - batch_old_pay))
    example_payback = example_transition / example_benefit if example_benefit > 0 else float("inf")
    st.markdown(f"""
`backlog hours = (weekly BUC hours ÷ 7) × mean lead time in days` — Little's Law, sized from your real data.

`temporary margin give-up = backlog hours × (blended pay rate under this scenario − old pay rate)` — the blended pay
rate is the real mix of old-rate and new-rate dollars across the backlog, from your actual lead-time distribution.

`ongoing monthly gain once fully transitioned = monthly hours × [(${premium_bill:.0f} − ${premium_base_pay:.0f}) −
(${dist_adv_bill:.0f} − old pay rate)]` — same number regardless of scenario, since it's about the business after the
transition is over, not how you phased it in.

`break-even months = temporary margin give-up ÷ ongoing monthly gain` — used to rank tutors cheapest-first in the
Promotion Order Optimizer below.

**Worked example** — the average tutor in your {len(pool)}-tutor batch (${batch_old_pay:.2f}/hr today,
{batch_weekly_hours:.1f} hrs/wk, {batch_mean_lead:.0f}-day average lead time) under **{SCENARIO_LABELS[scenario_key]}**:
- Backlog ≈ {example_backlog:.1f} hours
- Give-up ≈ {example_backlog:.1f} hrs × (${example_cost_rate:.2f} − ${batch_old_pay:.2f}) ≈ **${example_transition:,.0f}**
- Ongoing monthly gain ≈ **${example_benefit:,.0f}/mo**
- Break-even ≈ ${example_transition:,.0f} ÷ ${example_benefit:,.0f}/mo ≈ **{example_payback:.1f} months**
""")

# ── This batch's own margin over time, by scenario ──────────────────────────────
st.markdown('<div class="section-label">This batch\'s own margin over time, by scenario</div>', unsafe_allow_html=True)
st.caption(f"Just your {len(pool)}-tutor batch ({pool_tier_summary}) in isolation, promoted at your pace "
           f"({promotions_per_month}/month) starting this month — the only thing that changes across these five lines is "
           f"the pay-rate-change schedule. Family billing catches up to the new Premium rate on its own natural timeline "
           f"(~{pop_mean_lead:.0f} days, from your real lead-time data) no matter which scenario you pick — that part isn't "
           f"a policy choice, so it's held constant across all five lines. This is the batch's own margin, not the whole "
           f"business — see \"Overall BUC margin (whole business)\" below for that.")
fig_scen_time = go.Figure()
for key in SCENARIO_ORDER:
    m_line = simulate_by_scenario(scenario_rankings[key], horizon_months, key)
    fig_scen_time.add_trace(go.Scatter(x=list(range(1, horizon_months + 1)), y=m_line, mode="lines+markers",
        name=SCENARIO_LABELS[key], line=dict(color=colors[key], width=3.5 if key == scenario_key else 1.75)))
fig_scen_time.add_hline(y=margin_floor, line_dash="dash", line_color="#f59e0b", line_width=1.5,
    annotation_text=f"Your floor ({margin_floor}%)", annotation_font_color="#f59e0b")
fig_scen_time.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
    xaxis=dict(title="Month", **AXIS_STYLE),
    yaxis=dict(title="Blended margin, this batch only (%)", **AXIS_STYLE),
    margin=dict(l=10, r=20, t=20, b=10), height=380,
    legend=dict(orientation="h", y=-0.2, font=dict(color="#9ca3af", size=11)))
st.plotly_chart(fig_scen_time, width="stretch")
st.caption("Each tutor's line only starts moving in the month THEY get promoted — with the pool promoted a few at a time, "
           "the blended line keeps drifting upward for a while even after any one tutor's own transition has settled. A dip "
           "right after promotion is real: it means pay has already moved (or started moving) before family billing has "
           "caught up — Natural Backlog Drain can even show a temporary overshoot above the eventual target for the "
           "opposite reason (billing catches up before pay does).")

# ── Overall BUC margin (whole business) ──────────────────────────────────────────
st.markdown('<div class="section-label">Overall BUC margin (whole business), by scenario</div>', unsafe_allow_html=True)
if batch_exceeds_target:
    st.warning(f"Your {len(pool)}-tutor batch already represents {H_batch:,.0f} hrs/week of BUC volume — more than your "
               f"{premium_target_share}% target ({Premium_target_hours:,.0f} hrs/week of {H_total:,.0f} total). There's no "
               f"additional \"already-Premium\" volume assumed on top of your tracked batch in that case — raise the target "
               f"share above, or reduce the batch size, if you want headroom between them.")
st.caption(f"Zooms out to the WHOLE business, not just your {len(pool)}-tutor batch. Real total BUC volume today is "
           f"{H_total:,.0f} hrs/week ({real_tier_hours.get('Distinguished',0)/H_total*100:.0f}% Distinguished, "
           f"{real_tier_hours.get('Advanced',0)/H_total*100:.0f}% Advanced, {real_tier_hours.get('Premium',0)/H_total*100:.0f}% "
           f"Premium already). You've set a target of {premium_target_share}% Premium ({Premium_target_hours:,.0f} hrs/week) — "
           f"of that, {H_batch:,.0f} hrs/week is your tracked batch above, individually timed through the scenario's own "
           f"transition schedule, and the remaining {flat_premium_hours:,.0f} hrs/week is assumed to already be fully "
           f"transitioned (flat ${premium_base_pay:.0f}/hr pay, ${premium_bill:.0f}/hr billing — not individually tracked, "
           f"so there's no ramp for it). The other {remaining_DA_hours:,.0f} hrs/week stays Distinguished/Advanced "
           f"throughout, paid at the real hours-weighted average rate across those tutors (${da_weighted_avg_pay:.2f}/hr) "
           f"and billed at ${dist_adv_bill:.0f}/hr. Only your batch's promotions actually move over time on this chart — "
           f"everything else is held constant, which is why the five lines sit much closer together here than in the "
           f"batch-only chart above.")
fig_overall = go.Figure()
for key in SCENARIO_ORDER:
    m_line = simulate_overall_by_scenario(scenario_rankings[key], horizon_months, key,
                                           flat_premium_hours, remaining_DA_hours, da_weighted_avg_pay)
    fig_overall.add_trace(go.Scatter(x=list(range(1, horizon_months + 1)), y=m_line, mode="lines+markers",
        name=SCENARIO_LABELS[key], line=dict(color=colors[key], width=3.5 if key == scenario_key else 1.75)))
fig_overall.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
    xaxis=dict(title="Month", **AXIS_STYLE),
    yaxis=dict(title="Blended margin, whole business (%)", **AXIS_STYLE),
    margin=dict(l=10, r=20, t=20, b=10), height=380,
    legend=dict(orientation="h", y=-0.2, font=dict(color="#9ca3af", size=11)))
st.plotly_chart(fig_overall, width="stretch")
st.caption("This line barely moves compared to the batch-only chart above — expected, since the tracked batch is a small "
           "slice of total BUC volume, so even a scenario with a big temporary dip in its own margin barely nudges the "
           "company-wide number. The chart above answers \"is this promotion wave itself healthy\"; this one answers "
           "\"does it move the needle for the whole business.\"")

# ── Promotion Order Optimizer (detail on the selected scenario) ─────────────────
st.markdown(f'<div class="section-label">Promotion Order Optimizer — detail: {SCENARIO_LABELS[scenario_key]}</div>', unsafe_allow_html=True)
st.caption(f"Detail on your {len(pool)}-tutor batch from \"Who's being promoted\" above. The engine ranks whoever's "
           f"selected by break-even period — temporary margin give-up (real backlog, real lead-time mix, under the "
           f"scenario selected in the sidebar) divided by the ongoing monthly gain once fully transitioned — so the "
           f"cheapest-to-promote, highest-upside tutors go first. That order maximizes cumulative margin for any "
           f"promotion pace; the pace slider (left) then controls how fast you move down the list, and the floor "
           f"below flags if a pace pushes the promoted batch's margin too low.")

if len(pool) == 0:
    st.warning("No tutors selected — pick at least one in \"Who's being promoted\" above.")
else:
    ranked = rank_for_scenario(scenario_key, pool, promotions_per_month)

    total_transition_cost = ranked["transition_cost"].sum()
    total_monthly_benefit_full = ranked["monthly_benefit"].sum()
    pool_max_pace = precomputed_max_pace[scenario_key]
    months_at_pool_max_pace = int(np.ceil(len(ranked) / pool_max_pace)) if pool_max_pace > 0 else None

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"""<div class="metric-card"><div class="label">Temp. margin give-up (total)</div>
            <div class="value yellow">${total_transition_cost:,.0f}</div>
            <div class="sub">across all {len(ranked)} selected tutors, under {SCENARIO_LABELS[scenario_key]} — not a bill, just reduced margin while their backlog drains</div></div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""<div class="metric-card"><div class="label">Ongoing gain once fully switched over</div>
            <div class="value green">${total_monthly_benefit_full:,.0f}/mo</div>
            <div class="sub">permanent margin gain vs. leaving them at Distinguished/Advanced</div></div>""", unsafe_allow_html=True)
    with c3:
        pace_disp = f"{pool_max_pace}/mo" + ("+" if pool_max_pace >= MAX_PACE_SEARCH else "")
        st.markdown(f"""<div class="metric-card"><div class="label">Max safe pace (floor: {margin_floor}%)</div>
            <div class="value blue">{pace_disp}</div>
            <div class="sub">fastest pace that never dips below your floor</div></div>""", unsafe_allow_html=True)
    with c4:
        finish_disp = f"{months_at_pool_max_pace} mo" if months_at_pool_max_pace else "n/a"
        st.markdown(f"""<div class="metric-card"><div class="label">Time to promote all {len(ranked)}, at that pace</div>
            <div class="value blue">{finish_disp}</div>
            <div class="sub">your pace slider is currently set to {promotions_per_month}/mo</div></div>""", unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-label">Does promotion order matter?</div>', unsafe_allow_html=True)
    st.caption(f"A different question from the chart above: holding the scenario fixed at {SCENARIO_LABELS[scenario_key]}, "
               f"does it matter WHICH tutors (of your {len(pool)} selected) go first? This compares the optimizer's "
               f"cheapest-first order against promoting the same tutors in the opposite (worst-first) order.")

    optimizer_order = ranked.copy()
    naive_order = ranked.sort_values("payback_months", ascending=False).reset_index(drop=True)
    naive_order["promo_month"] = (naive_order.index // promotions_per_month).astype(int)

    opt_margins = simulate_by_scenario(optimizer_order, horizon_months, scenario_key)
    naive_margins = simulate_by_scenario(naive_order, horizon_months, scenario_key)

    fig_opt = go.Figure()
    fig_opt.add_trace(go.Scatter(x=list(range(1, horizon_months + 1)), y=opt_margins, mode="lines+markers",
        name="Optimizer order (cheapest-first)", line=dict(color="#22c55e", width=2.5)))
    fig_opt.add_trace(go.Scatter(x=list(range(1, horizon_months + 1)), y=naive_margins, mode="lines+markers",
        name="Reverse order (worst-first, for comparison)", line=dict(color="#ef4444", width=2.5, dash="dot")))
    fig_opt.add_hline(y=margin_floor, line_dash="dash", line_color="#f59e0b", line_width=1.5,
        annotation_text=f"Your floor ({margin_floor}%)", annotation_font_color="#f59e0b")
    fig_opt.update_layout(plot_bgcolor=CHART_BG, paper_bgcolor=PAPER_BG, font=FONT,
        xaxis=dict(title="Month", **AXIS_STYLE),
        yaxis=dict(title="Blended margin, selected pool (%)", **AXIS_STYLE),
        margin=dict(l=10, r=20, t=20, b=10), height=380,
        legend=dict(orientation="h", y=-0.2, font=dict(color="#9ca3af", size=11)))
    st.plotly_chart(fig_opt, width="stretch")

    min_opt, min_naive = min(opt_margins), min(naive_margins)
    if min_opt < margin_floor:
        st.warning(f"At this pace, even the optimizer order dips to {min_opt:.1f}% — below your {margin_floor}% floor. "
                   f"Slow the pace down, choose a scenario with a longer protection window, or raise the floor.")
    else:
        st.success(f"Optimizer order stays above your {margin_floor}% floor throughout (lowest point: {min_opt:.1f}%). "
                   f"The naive reverse order would have dipped to {min_naive:.1f}%.")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-label">Recommended promotion order</div>', unsafe_allow_html=True)
    pill = {"Distinguished": '<span class="pill pill-dist">Distinguished</span>', "Advanced": '<span class="pill pill-adv">Advanced</span>'}
    rows_html = ""
    for _, r in ranked.iterrows():
        pb = "∞" if not np.isfinite(r.payback_months) else f"{r.payback_months:.1f} mo"
        hist_flag = "own history" if r.own_history else "company avg"
        rows_html += f"""<tr><td>{int(r.tutor_id)}</td><td>{pill.get(r.tier, r.tier)}</td>
            <td>${r.buc_pay_rate:.2f}</td><td>{r.weekly_hours:.1f} hrs/wk</td>
            <td>{r.mean_lead_days:.0f}d ({hist_flag})</td>
            <td>${r.transition_cost:,.0f}</td><td>${r.monthly_benefit:,.0f}/mo</td>
            <td>{pb}</td><td>Month {int(r.promo_month)+1}</td></tr>"""
    st.markdown(f"""<table class="tutor-table">
      <thead><tr><th>Tutor ID</th><th>Tier</th><th>Current BUC pay</th><th>Weekly hours</th>
        <th>Mean lead time</th><th>Temp. margin give-up</th><th>Ongoing monthly gain</th><th>Break-even</th><th>Promote in</th></tr></thead>
      <tbody>{rows_html}</tbody></table>""", unsafe_allow_html=True)
    st.caption("Break-even = temp. margin give-up ÷ ongoing monthly gain — tutors with the shortest break-even are "
               "recommended first, since they're the cheapest to promote relative to their long-term upside.")
