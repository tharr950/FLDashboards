#!/usr/bin/env python3
"""
check_history_weeks.py -- READ-ONLY. Shows the most recent weeks present in
the parent-update-videos and progress-update history files, and which 4 weeks
each skip setting (0 or 1) would use. Changes nothing.

Target period (10/4/26 - 10/31/26) needs weeks of 10/4, 10/11, 10/18, 10/25.
If both files already contain a 'week of' 11/1 (the week after the period),
skip=1 works as normal. If the latest week is 10/25, skip=0 is needed to
include it (only do that if that week is fully processed).
"""
import pull_fl_dashboard_history as h


def main():
    pv = h.fetch_csv("data/parent_update_videos_history.csv")
    wk = h.find_column(pv, "week of")
    pv[wk] = h.pd.to_datetime(pv[wk]).dt.date
    pv_weeks = sorted(pv[h.find_column(pv, "week of")].unique())

    pu = h.fetch_json("data/progress_updates_history.json")
    pu["sent_at"] = h.pd.to_datetime(pu["sent_at"])
    pu_weeks = sorted(pu["sent_at"].apply(h.week_of_sunday).unique())

    for name, weeks in [("parent_update_videos", pv_weeks), ("progress_update_quality", pu_weeks)]:
        print(f"\n{name}: latest 6 weeks present: {[str(w) for w in weeks[-6:]]}")
        for skip in (0, 1):
            used = h.last_n_weeks_excluding_most_recent(weeks, skip=skip)
            print(f"  skip={skip} would use: {[str(w) for w in used]}")


if __name__ == "__main__":
    main()
