import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import os

#st.write("Current working directory:", os.getcwd())
# st.write("Files here:", os.listdir())

@st.cache_data(ttl=60)
def load_tutor_concerns():
    file = "Tutor_Concerns.csv"
    if os.path.exists(file):
        try:
            df = pd.read_csv(file)
            return df
        except Exception as e:
            st.warning(f"Could not read {file}: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        try:
            return pd.read_excel(file, sheet_name="AnnualReview")
        except Exception as e:
            st.warning(f"Could not read {file}: {e}")
            return pd.DataFrame()
    else:
        return pd.DataFrame()

@st.cache_data(ttl=60)
def load_grade_summary():
    file = "Jessica_GradesSummary.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file)
    else:
        return pd.DataFrame()  # empty DataFrame if missing

@st.cache_data(ttl=60)
def load_concern_groupings():
    file = "Tutor_Concern_Groupings_Explanations_June2025.csv"
    if os.path.exists(file):
        return pd.read_csv(file)
    else:
        return pd.DataFrame()
    
# Load Monthly Metric sheet for Annual Reviews
@st.cache_data(ttl=60)
def load_monthly_metric_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()

# Load Annual Reviews sheet
@st.cache_data(ttl=60)
def load_annual_reviews():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="AnnualReview")
    else:
        return pd.DataFrame()
    
# Load Repurchase Data
@st.cache_data(ttl=60)
def load_repurchases():
    file = "Repurchase_Summary_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="Sheet 1")
    else:
        return pd.DataFrame()
    
# Filter for Jessica Milner tutors using MasterTutor tab
@st.cache_data(ttl=60)
def load_master_tutor():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        try:
            df = pd.read_excel(file, sheet_name="MasterTutor")
#             st.write(f"Loaded MasterTutor: {df.shape[0]} rows, {df.shape[1]} cols")
#             st.write(df.columns.tolist())  # Show actual columns
            return df
        except Exception as e:
            st.error(f"Error reading MasterTutor sheet: {e}")
            return pd.DataFrame()
    else:
        st.error(f"{file} not found")
        return pd.DataFrame()
    
# Pre-load Subject Additions sheet for later
@st.cache_data(ttl=60)
def load_subject_additions():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="SubjectAddition")
    else:
        return pd.DataFrame()
    
@st.cache_data(ttl=60)
def load_monthly_metric():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()
    
# --- Load data (same as KPI Trends) ---
@st.cache_data(ttl=60)
def load_kpi_data():
    file = "December_Annual_Reviews.xlsx"
    if os.path.exists(file):
        return pd.read_excel(file, sheet_name="MonthlyMetric")
    else:
        return pd.DataFrame()

def render_app(config):

    st.markdown("""
        <style>
            .main-title {
                font-size: 2.5em;
                font-weight: bold;
                color: #004466;
                margin-bottom: 0.3em;
            }
            .block-container {
                padding-top: 2rem;
            }
            section[data-testid="stSidebar"] {
                background-color: #F1F3F5;
            }
            .metric-label {
                font-size: 1.1rem;
                color: #666;
            }
        </style>
    """, unsafe_allow_html=True)


    grade_summary_df = load_grade_summary()
    concern_groupings_df = load_concern_groupings()


    #st.title("Tutor KPI Tracker")
    st.markdown('<div class="main-title">Jessica Tutor Data 📊</div>', unsafe_allow_html=True)


    # Sidebar Navigation
    page = st.sidebar.radio("📂 Navigation", [
        "Concerns",
        "KPI Table",
        "KPI Trends",
        "Grades Summary",
        "Annual Reviews"
    ])

    # Filter tutors by Faculty Leader
    faculty_leader_name = "Jessica Milner"
    master_tutor_df = load_master_tutor()
#     st.write(master_tutor_df.columns.tolist())

    #master_tutor_df = pd.read_csv("Master_Tutor.csv")  # or use cached if already loaded
    annelies_tutors = master_tutor_df[master_tutor_df["Faculty Leader"] == faculty_leader_name]["Full Name"].sort_values().dropna().unique().tolist()


    # ---- Annual Reviews Tab ----
    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📋 Annual Reviews")


    annual_review_df = load_annual_reviews()

    monthly_metric_annual_review_df = load_monthly_metric_annual_reviews()

    repurchase_df = load_repurchases()

    
    annelies_tutors = master_tutor_df[master_tutor_df["Faculty Leader"] == "Jessica Milner"]["Full Name"].dropna().sort_values().tolist()




    # ---- Annual Reviews Tab ----
    if page == "Annual Reviews":
        st.markdown('<div class="main-title">Annual Reviews 📋</div>', unsafe_allow_html=True)
        selected_annual_tutor = st.selectbox("Select a Tutor:", annelies_tutors)

        if selected_annual_tutor:
            tutor_review = annual_review_df[annual_review_df["tutor_name"] == selected_annual_tutor]
            tutor_review_repurchase = repurchase_df[repurchase_df["Tutor Name"] == selected_annual_tutor]
            tutor_review_monthly_metric = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tutor Name"] == selected_annual_tutor]

            if not tutor_review.empty:
                row = tutor_review.iloc[0]
                tutor_tier = row["tier"]

                row_repurchase = tutor_review_repurchase.iloc[0]
                tutor_tier_repurchase = row_repurchase["Current Tier"]
                tutor_deliverytarget = row_repurchase["Delivery Target"]

                row_monthly_metric = tutor_review_monthly_metric
                tutor_tier_monthly_metric = row_monthly_metric["Tier"].iloc[0]

                # Filter comparison data
                team_df = annual_review_df[annual_review_df["fl"] == "Jessica Milner"]
                tier_df = annual_review_df[annual_review_df["tier"] == tutor_tier]

                team_repurchase_df = repurchase_df[repurchase_df["Team Name"] == "Team De Groot"]
                tier_repurchase_df = repurchase_df[repurchase_df["Current Tier"] == tutor_tier]
                tierdelivery_repurchase_df = repurchase_df[
                    (repurchase_df["Current Tier"] == tutor_tier) &
                    (repurchase_df["Delivery Target"] == tutor_deliverytarget)
                ]

                team_monthly_metric_df = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Faculty Leader"] == "Jessica Milner"]
                tier_monthly_metric_df = monthly_metric_annual_review_df[monthly_metric_annual_review_df["Tier"] == tutor_tier_monthly_metric]

                metrics = {
                    "sessions_on_time": "Sessions On Time (%)",
                    "% Parents Updates Done on Time": "Percent of Parent Updates Completed on Time",
                    "prep_time": "Prep Time (%)",
                    "Repurchases Weighted": "Weighted Repurchase",
                    "average_nps": "Average NPS",
                    "% of Active Students with Progress Updates Completed in last 2 months": "Progress Update Average Percentage",
                    "current_sci": "Current SCI",
                    "availability_percent": "Percent to Availability (%)",
                    "delivery_percent":"Percent to Delivery (%)"
                }


                subject_df = load_subject_additions()

                # Loop through metrics
                for col, label in metrics.items():
                    # Insert Subject Additions just before Percent to Availability
                    if col == "availability_percent":
                        st.divider()
                        st.subheader("Subject Additions")
                        if "tutor_name" in subject_df.columns:
                            tutor_subjects = subject_df.loc[
                                subject_df["tutor_name"].str.strip().str.lower() == selected_annual_tutor.strip().lower(),
                                "subject"
                            ].dropna().tolist()
                        else:
                            st.error("Column 'tutor_name' not found in Subject Addition sheet.")
                            tutor_subjects = []

                        if len(tutor_subjects) == 0:
                            st.markdown("<p style='color: gray; font-style: italic; font-size: 1.1rem;'>None</p>", unsafe_allow_html=True)
                        else:
                            for subj in tutor_subjects:
                                st.markdown(
                                    f"""
                                    <div style='
                                        background-color: #f8f9fa;
                                        border-radius: 8px;
                                        padding: 10px 15px;
                                        margin: 6px 0;
                                        font-size: 1.1rem;
                                        font-weight: 500;
                                        color: #333;
                                        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
                                    '>
                                    📘 {subj}
                                    </div>
                                    """,
                                    unsafe_allow_html=True
                                )

                    # Extract metric values
                    if col in ["% Parents Updates Done on Time","% of Active Students with Progress Updates Completed in last 2 months"]:
                        tutor_value_monthly_metric = np.nanmean(row_monthly_metric[col].values)
                    elif col in ["Repurchases Weighted"]:
                        tutor_value_repurchase = row_repurchase[col]
                    else:
                        tutor_value = row[col]

                    # Format percentages
                    if col in ["sessions_on_time", "prep_time", "availability_percent","delivery_percent",
                              "% Parents Updates Done on Time","% of Active Students with Progress Updates Completed in last 2 months"]:
                        if col in ["% Parents Updates Done on Time","% of Active Students with Progress Updates Completed in last 2 months"]:
                            tutor_value_display = f"{tutor_value_monthly_metric * 100:.0f}%"
                            tutor_value_plot = tutor_value_monthly_metric * 100
                            team_avg = team_monthly_metric_df[col].mean() * 100
                            tier_avg = tier_monthly_metric_df[col].mean() * 100
                        else:
                            tutor_value_display = f"{tutor_value * 100:.0f}%"
                            tutor_value_plot = tutor_value * 100
                            team_avg = team_df[col].mean() * 100
                            tier_avg = tier_df[col].mean() * 100
                    else:
                        if col in ["Repurchases Weighted"]:
                            tutor_value_display = f"{tutor_value_repurchase:.1f}"
                            tutor_value_plot = tutor_value_repurchase
                            team_avg = tierdelivery_repurchase_df[col].mean()  # VS Tier/Delivery Target
                            tier_avg = tier_repurchase_df[col].mean()
                        else:
                            tutor_value_display = f"{tutor_value:.1f}"
                            tutor_value_plot = tutor_value
                            team_avg = team_df[col].mean()
                            tier_avg = tier_df[col].mean()

                    st.markdown("<hr>", unsafe_allow_html=True)
                    st.markdown(f"<h3 style='text-align:center'>{label}</h3>", unsafe_allow_html=True)

                    # --- Plots ---
                    if col == "Repurchases Weighted":
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor, "Tier/Delivery Target"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue", "lightgrey"]
                        ))
                        fig_team.update_layout(
                            title=dict(
                                text="VS Tier/Delivery Target",
                                x=0.5,
                                xanchor='center',
                                font=dict(size=16)
                            ),
                            yaxis_title="Value",
                            xaxis_title="",
                            height=300,
                            margin=dict(l=20, r=20, t=40, b=20)
                        )
                    else:
                        fig_team = go.Figure(go.Bar(
                            x=[selected_annual_tutor, "Team Avg"],
                            y=[tutor_value_plot, team_avg],
                            marker_color=["blue", "lightgrey"]
                        ))
                        fig_team.update_layout(
                            title=dict(
                                text="VS Team",
                                x=0.5,
                                xanchor='center',
                                font=dict(size=16)
                            ),
                            yaxis_title="Value",
                            xaxis_title="",
                            height=300,
                            margin=dict(l=20, r=20, t=40, b=20)
                        )

                    fig_tier = go.Figure(go.Bar(
                        x=[selected_annual_tutor, "Tier Avg"],
                        y=[tutor_value_plot, tier_avg],
                        marker_color=["blue", "lightgrey"]
                    ))
                    fig_tier.update_layout(
                        title=dict(
                            text="VS Tier",
                            x=0.5,
                            xanchor='center',
                            font=dict(size=16)
                        ),
                        yaxis_title="Value",
                        xaxis_title="",
                        height=300,
                        margin=dict(l=20, r=20, t=40, b=20)
                    )

                    # Layout: 3 columns
                    col1, col2, col3 = st.columns([1, 1, 1])
                    with col1:
                        st.markdown(
                            f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_annual_tutor}<br>{tutor_value_display}</div>",
                            unsafe_allow_html=True
                        )
                    with col2:
                        st.plotly_chart(fig_team, use_container_width=True)
                    with col3:
                        st.plotly_chart(fig_tier, use_container_width=True)





    # ---- KPI Trends Tab ----
    if page == "KPI Trends":
        st.markdown('<div class="main-title">📈 KPI Trends</div>', unsafe_allow_html=True)


        monthly_df = load_monthly_metric()
        annual_df = load_annual_reviews()
        master_df = load_master_tutor()

        selected_tutor = st.selectbox("Select a Tutor:", annelies_tutors)

        if selected_tutor:
            tutor_df = monthly_df[monthly_df["Tutor Name"] == selected_tutor].copy()
            tutor_tier = annual_df.loc[annual_df["tutor_name"] == selected_tutor, "tier"].values
            tutor_tier = tutor_tier[0] if len(tutor_tier) > 0 else None

            # ---- Parse end date from Date Range ----
            import re
            def extract_end_date(range_str):
                if pd.isna(range_str):
                    return pd.NaT
                # Replace all dash variants with 'to'
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                # Fix cases like "6-14/25" -> "6/14/25"
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT

            tutor_df["Date Parsed"] = tutor_df["Date Range"].apply(extract_end_date)

            annelies_team = master_df[master_df["Faculty Leader"] == "Jessica Milner"]["Full Name"].dropna()
            team_df = monthly_df[monthly_df["Tutor Name"].isin(annelies_team)].copy()
            team_df["Date Parsed"] = team_df["Date Range"].apply(extract_end_date)

            if tutor_tier:
                tier_tutors = annual_df[annual_df["tier"] == tutor_tier]["tutor_name"]
                tier_df = monthly_df[monthly_df["Tutor Name"].isin(tier_tutors)].copy()
                tier_df["Date Parsed"] = tier_df["Date Range"].apply(extract_end_date)
            else:
                tier_df = pd.DataFrame()

            metrics = {
                "% to Delivery Target": "% to Delivery Target",
                "% to Availability Target": "% to Availability Target",
                "% Sessions on Time": "% Sessions on Time",
                "% Parents Updates Done on Time": "% to Parent Updates Completed",
                "% of Active Students with Progress Updates Completed in last 2 months": "% Progress Updates Completed",
                "Weighted Repurchases": "Weighted Repurchases",
                "Ratio of PPW Events with Attached PPWs": "PPW Attachment Ratio"
            }

            percent_metrics = [
                "% to Delivery Target",
                "% to Availability Target",
                "% Sessions on Time",
                "% Parents Updates Done on Time",
                "% of Active Students with Progress Updates Completed in last 2 months"
            ]

            for metric, label in metrics.items():
                st.markdown("<hr>", unsafe_allow_html=True)
                st.markdown(f"<h3 style='text-align:center'>{label}</h3>", unsafe_allow_html=True)

                # Convert percentages
                if metric in percent_metrics:
                    tutor_df[metric] = tutor_df[metric] * 100
                    team_df[metric] = team_df[metric] * 100
                    if not tier_df.empty:
                        tier_df[metric] = tier_df[metric] * 100

                # ---- Keep last 6 periods sorted by Date Parsed ----
                tutor_plot_df = tutor_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed").tail(6)
                team_plot_df = team_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed")
                tier_plot_df = tier_df.dropna(subset=["Date Parsed"]).sort_values("Date Parsed") if not tier_df.empty else pd.DataFrame()

                # Latest value for display
                latest_value = tutor_plot_df[metric].iloc[-1] if not tutor_plot_df.empty else None
                latest_display = f"{latest_value:.0f}%" if metric in percent_metrics else f"{latest_value:.2f}"

                # ---- Tutor vs Team ----
                if not team_plot_df.empty:
                    team_grouped = team_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    team_grouped = team_grouped.sort_values("Date Parsed").tail(6)

                    # Map original Date Range labels
                    team_grouped = team_grouped.merge(
                        team_plot_df[["Date Parsed", "Date Range"]],
                        on="Date Parsed",
                        how="left"
                    ).drop_duplicates(subset=["Date Parsed"])

                    fig_team = px.line(
                        team_grouped,
                        x="Date Range",
                        y=metric,
                        title="VS Team",
                        markers=True
                    )
                    fig_team.add_scatter(
                        x=tutor_plot_df["Date Range"],
                        y=tutor_plot_df[metric],
                        mode="lines+markers",
                        name=selected_tutor,
                        line=dict(width=3)
                    )
                    fig_team.update_layout(
                        title=dict(x=0.5, xanchor='center', font=dict(size=16)),
                        xaxis=dict(tickangle=30),
                        yaxis_title=None,
                        xaxis_title=None,
                        height=350,
                        margin=dict(l=20, r=20, t=50, b=40)
                    )

                # ---- Tutor vs Tier ----
                if not tier_plot_df.empty:
                    tier_grouped = tier_plot_df.groupby("Date Parsed")[metric].mean().reset_index()
                    tier_grouped = tier_grouped.sort_values("Date Parsed").tail(6)

                    # Map original Date Range labels
                    tier_grouped = tier_grouped.merge(
                        tier_plot_df[["Date Parsed", "Date Range"]],
                        on="Date Parsed",
                        how="left"
                    ).drop_duplicates(subset=["Date Parsed"])

                    fig_tier = px.line(
                        tier_grouped,
                        x="Date Range",
                        y=metric,
                        title="VS Tier",
                        markers=True
                    )
                    fig_tier.add_scatter(
                        x=tutor_plot_df["Date Range"],
                        y=tutor_plot_df[metric],
                        mode="lines+markers",
                        name=selected_tutor,
                        line=dict(width=3)
                    )
                    fig_tier.update_layout(
                        title=dict(x=0.5, xanchor='center', font=dict(size=16)),
                        xaxis=dict(tickangle=30),
                        yaxis_title=None,
                        xaxis_title=None,
                        height=350,
                        margin=dict(l=20, r=20, t=50, b=40)
                    )

                # ---- Layout: two rows ----
                row1_col1, row1_col2 = st.columns([1, 3])
                with row1_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True
                    )
                with row1_col2:
                    if not team_plot_df.empty:
                        st.plotly_chart(fig_team, use_container_width=True)

                row2_col1, row2_col2 = st.columns([1, 3])
                with row2_col1:
                    st.markdown(
                        f"<div style='font-size:24px; font-weight:bold; text-align:center;'>{selected_tutor}<br>{latest_display}</div>",
                        unsafe_allow_html=True
                    )
                with row2_col2:
                    if not tier_plot_df.empty:
                        st.plotly_chart(fig_tier, use_container_width=True)


    # ---- Concerns Tab ----
    if page == "Concerns":
        st.markdown('<div class="main-title">Tutor Concerns 📌</div>', unsafe_allow_html=True)

        concerns_df = load_tutor_concerns()

        # Filter for this Faculty Leader
        fl_df = concerns_df[concerns_df["Faculty Leader Name"] == faculty_leader_name]

        if fl_df.empty:
            st.info("No concern data available for your team.")
        else:
                        
            # ---- Parse end date from Date Range ----
            import re
            def extract_end_date(range_str):
                if pd.isna(range_str):
                    return pd.NaT
                # Replace all dash variants with 'to'
                clean_str = range_str.replace("-", "to").replace("–", "to").replace("—", "to")
                parts = clean_str.split("to")
                if len(parts) < 2:
                    return pd.NaT
                end_str = parts[-1].strip()
                # Fix cases like "6-14/25" -> "6/14/25"
                end_str = re.sub(r"(\d+)-(\d+/\d+)", r"\1/\2", end_str)
                try:
                    return pd.to_datetime(end_str, errors="coerce", dayfirst=False)
                except:
                    return pd.NaT
            
            # --- Team Overview (latest date only) ---
            
            fl_df["Date"] = fl_df["Date"].apply(extract_end_date)

            latest_date = fl_df["Date"].max()
            latest_df = fl_df[fl_df["Date"] == latest_date]

            st.subheader(f"Team Overview (Latest Date: {latest_date.date()})")

            # Breakdown of # of tutors in each Concern Group
            concern_counts = latest_df.groupby("Concern Group")["Tutor Name"].nunique().sort_index(ascending=False)
            st.markdown("**Number of Tutors in Each Concern Group**")
            st.bar_chart(concern_counts)

            # List of tutors by Concern Group (5 first)
            for group in sorted(latest_df["Concern Group"].unique(), reverse=True):
                st.markdown(f"### Concern Group {group}")
                tutors_in_group = latest_df[latest_df["Concern Group"] == group]["Tutor Name"].tolist()
                st.write(", ".join(tutors_in_group))

            # Download button for latest team concerns
            st.download_button(
                label="Download Latest Tutor Concerns",
                data=latest_df.to_csv(index=False),
                file_name=f"Tutor_Concerns_{faculty_leader_name}_{latest_date.date()}.csv",
                mime="text/csv"
            )

            st.markdown("---")

            # --- Individual Tutor Selector ---
            tutor_names = fl_df["Tutor Name"].dropna().unique().tolist()
            selected_tutor = st.selectbox("Select a Tutor", tutor_names)

            if selected_tutor:
                tutor_df = fl_df[fl_df["Tutor Name"] == selected_tutor].sort_values("Date")

                # Plot concern score over time
                fig = px.line(
                    tutor_df,
                    x="Date",
                    y="Concern Group",
                    markers=True,
                    title=f"{selected_tutor} Concern Score Over Time"
                )

                # Force y-axis from 1 to 5 and reverse it
                fig.update_yaxes(
                    range=[1, 5],  # 5 at top, 1 at bottom
                    dtick=1,
                    title="Concern Group",
                    autorange=False  # ensure range is respected
                )

                st.plotly_chart(fig, use_container_width=True)

                # Table of all data for the tutor
                st.subheader(f"{selected_tutor} Details")
                st.dataframe(tutor_df[["Date", "Concern Group", "Reasons"]])

                # Download button for individual tutor
                st.download_button(
                    label=f"Download {selected_tutor} Concerns",
                    data=tutor_df.to_csv(index=False),
                    file_name=f"{selected_tutor}_Concerns.csv",
                    mime="text/csv"
                )


    # ---------------------- KPI TABLE TAB ----------------------
    # ---------------------- KPI TABLE TAB ----------------------
    if page == "KPI Table":


        df = load_kpi_data()

        # --- Filter for latest date range and selected faculty leader ---
        # --- Parse start date of each range for proper chronological sorting ---
        df["Date Range Parsed"] = pd.to_datetime(df["Date Range"].str.split(" - ").str[0], errors="coerce")

        # --- Get the latest date based on the parsed start date ---
        latest_range_parsed = df["Date Range Parsed"].max()
        latest_range = df.loc[df["Date Range Parsed"] == latest_range_parsed, "Date Range"].iloc[0]
        leader_name = "Jessica Milner"  # can later make this a dropdown if desired
        team_df = df[(df["Date Range"] == latest_range) & (df["Faculty Leader"] == leader_name)].copy()

        # --- Define KPI metrics ---
        metrics = [
            "% to Delivery Target",
            "% to Availability Target",
            "% Sessions on Time",
            "% Parents Updates Done on Time",
            "% of Active Students with Progress Updates Completed in last 2 months"
        ]

        # --- Convert decimals to percentages ---
        for m in metrics:
            team_df[m] = team_df[m] * 100

        st.title("Team KPI Overview")
        st.caption(f"Faculty Leader: {leader_name} | Latest Date Range: {latest_range}")

        st.divider()
        st.divider()

        # --- Top KPI summary cards (2 rows if needed) ---
        st.subheader("Team Summary KPIs")
        n_metrics = len(metrics)
        n_cols = 3  # 3 cards per row
        rows_needed = (n_metrics + n_cols - 1) // n_cols
        for r in range(rows_needed):
            cols = st.columns(n_cols)
            for i, col in enumerate(cols):
                idx = r * n_cols + i
                if idx < n_metrics:
                    metric = metrics[idx]
                    avg = team_df[metric].mean(skipna=True)
                    if avg >= 90:
                        color = "🟢"
                    elif avg >= 75:
                        color = "🟡"
                    else:
                        color = "🔴"
                    col.metric(label=f"{color} {metric}", value=f"{avg:.1f}%")



        st.divider()
        st.divider()
        st.subheader("📊 Team KPI Changes from Previous Period")

        # --- Parse start date of each range for proper chronological sorting ---
        df["Date Range Parsed"] = pd.to_datetime(df["Date Range"].str.split(" - ").str[0], errors="coerce")

        # --- Sort date ranges chronologically ---
        date_ranges_sorted = df.sort_values("Date Range Parsed")["Date Range"].dropna().unique().tolist()

        if len(date_ranges_sorted) < 2:
            st.info("Not enough time periods available to calculate changes.")
        else:
            latest_range = date_ranges_sorted[-1]
            prev_range = date_ranges_sorted[-2]


            # Filter for Jessica' team in both time periods
            latest_team = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == latest_range)]
            prev_team = df[(df["Faculty Leader"] == leader_name) & (df["Date Range"] == prev_range)]

            # Compute team averages for both periods
            latest_avg = latest_team[metrics].mean()
            prev_avg = prev_team[metrics].mean()

            # Compute change (percentage point difference)
            change_df = pd.DataFrame({
                "Metric": metrics,
                f"{prev_range} Avg": prev_avg.values,
                f"{latest_range} Avg": latest_avg.values,
                "Change (pp)": (latest_avg - prev_avg).values
            })

            # Convert to percent format for readability
            for c in [f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]:
                change_df[c] = change_df[c] * 100

            # Format change direction and color
            def format_change(val):
                if pd.isna(val):
                    return ""
                arrow = "⬆️" if val > 0 else ("⬇️" if val < 0 else "➡️")
                return f"{arrow} {val:+.1f} pp"

            change_df["Change Display"] = change_df["Change (pp)"].apply(format_change)

    #         # Display results as a nice table
    #         st.dataframe(
    #             change_df[["Metric", f"{prev_range} Avg", f"{latest_range} Avg", "Change Display"]]
    #             .rename(columns={
    #                 f"{prev_range} Avg": f"{prev_range}",
    #                 f"{latest_range} Avg": f"{latest_range}",
    #                 "Change Display": "Change"
    #             }),
    #             hide_index=True,
    #             use_container_width=True
    #         )

            # Style the change column
            def style_change(val):
                color = "lightgreen" if val > 0 else ("lightcoral" if val < 0 else "white")
                return f"background-color: {color}; font-weight: bold; text-align: center"

            # Apply styling
            styled_df = change_df[["Metric", f"{prev_range} Avg", f"{latest_range} Avg", "Change (pp)"]].copy()
            styled_df_display = styled_df.style.format({
                f"{prev_range} Avg": "{:.1f}%",
                f"{latest_range} Avg": "{:.1f}%",
                "Change (pp)": "{:+.1f} pp"
            }).applymap(style_change, subset=["Change (pp)"])

            st.write(styled_df_display)




            # Determine symmetric range
            max_abs_change = max(abs(change_df["Change (pp)"].max()), abs(change_df["Change (pp)"].min()))

            # Put the plot in a wide central column
            col1, col2, col3 = st.columns([1, 12, 1])
            with col2:
                fig_change = px.bar(
                    change_df,
                    x="Metric",
                    y="Change (pp)",
                    color="Change (pp)",
                    color_continuous_scale=["red", "white", "green"],
                    text=change_df["Change (pp)"].apply(lambda x: f"{x:+.1f} pp"),
                    title=f"Change in Team Averages: {prev_range} → {latest_range}",
                    height=600
                )

                # Set symmetric color axis
                fig_change.update_layout(
                    title_x=0.20,
                    xaxis_title="",
                    yaxis_title="Change (percentage points)",
                    margin=dict(l=20, r=20, t=60, b=40),
                    coloraxis_colorbar=dict(title="Change"),
                )
                fig_change.update_traces(marker=dict(
                    coloraxis="coloraxis"
                ))
                fig_change.update_coloraxes(cmin=-max_abs_change, cmax=max_abs_change)

                st.plotly_chart(fig_change, use_container_width=True)          



        st.divider()
        # --- Team Comparison vs Other Teams ---
        st.divider()
        st.subheader("Team Metrics Comparison vs Other Teams")

        # Tier selection
        tier_options = ["All"] + sorted(df["Tier"].dropna().unique())
        selected_tier = st.selectbox("Filter by Tier (optional):", tier_options, index=0)

        # Filter by tier if selected
        if selected_tier != "All":
            df_filtered = df[(df["Tier"] == selected_tier) & (df["Date Range"] == latest_range)]
            title_prefix = f"{selected_tier} Tier Team Comparison"
        else:
            df_filtered = df[df["Date Range"] == latest_range]
            title_prefix = "Team Comparison"

        # Compute team averages by Faculty Leader
        leader_group = df_filtered.groupby("Faculty Leader")[metrics].mean()

        for metric in metrics:
            st.markdown(f"### {metric}")

            plot_df = leader_group.reset_index()
            plot_df = plot_df.sort_values(by=metric, ascending=False)

            # Multiply by 100 for plotting
            plot_df[metric + "_pct"] = plot_df[metric] * 100

            # Create a color map so the current Faculty Leader is blue, others gray
            color_map = {fl: ("blue" if fl == leader_name else "lightgray") for fl in plot_df["Faculty Leader"]}

            fig = px.bar(
                plot_df,
                x="Faculty Leader",
                y=metric + "_pct",
                color="Faculty Leader",
                color_discrete_map=color_map,
                text=plot_df[metric + "_pct"].apply(lambda x: f"{x:.1f}%"),
                labels={metric + "_pct": "Percent"},
                height=400
            )

            # Set y-axis max
            if metric == "% to Availability Target":
                y_max = 130
            else:
                y_max = 100

            fig.update_layout(
                title=dict(text=f"{title_prefix}: {metric}", x=0.5, xanchor="center"),
                showlegend=False,
                margin=dict(l=20, r=20, t=50, b=40),
                yaxis=dict(range=[0, y_max], tickformat=".0f%")
            )

            # Center the chart
            col1, col2, col3 = st.columns([1, 4, 1])
            with col2:
                st.plotly_chart(fig, use_container_width=True)



        st.divider()
        st.divider()


        # --- KPI Distributions Across Team ---
        st.subheader("Team KPI Leaderboard")

        # Ensure metrics are numeric
        for m in metrics:
            team_df[m] = pd.to_numeric(team_df[m], errors="coerce")

        team_df["Overall KPI Score"] = team_df[metrics].mean(axis=1)
        display_cols = ["Tutor Name"] + metrics + ["Overall KPI Score"]
        leaderboard_df = (
            team_df[display_cols]
            .sort_values(by="Overall KPI Score", ascending=False)
            .reset_index(drop=True)
        )

        # Format table values for display
        display_table_values = []
        for c in display_cols:
            if c == "Tutor Name":
                display_table_values.append(leaderboard_df[c].astype(str).tolist())
            else:
                display_table_values.append([
                    f"{v:.1f}%" if pd.notna(v) else "" for v in leaderboard_df[c]
                ])

        # --- Build color grid ---
        n_rows = len(leaderboard_df)
        n_cols = len(display_cols)
        colors = [["lightgrey"] * n_cols]  # header row

        # Initialize all white
        cell_colors = [["white"] * n_cols for _ in range(n_rows)]

        # Highlight best (green) and worst (red) per metric
        for j, c in enumerate(display_cols):
            if c in metrics + ["Overall KPI Score"]:
                col_vals = leaderboard_df[c]
                max_val = col_vals.max()
                min_val = col_vals.min()

                for i, val in enumerate(col_vals):
                    if pd.isna(val):
                        continue
                    if val == max_val:
                        cell_colors[i][j] = "lightgreen"
                    elif val == min_val:
                        cell_colors[i][j] = "lightcoral"

        # Combine header and body
        colors += cell_colors

        # --- Plotly Table ---
        fig_table = go.Figure(
            data=[
                go.Table(
                    header=dict(
                        values=[f"<b>{c}</b>" for c in display_cols],
                        fill_color="lightgrey",
                        align="center",
                    ),
                    cells=dict(
                        values=display_table_values,
                        fill_color=colors,
                        align="center",
                    ),
                )
            ]
        )

        fig_table.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=500)
        st.plotly_chart(fig_table, use_container_width=True)

        # --- Download button ---
        st.download_button(
            label="Download Team KPI Data",
            data=leaderboard_df.to_csv(index=False),
            file_name=f"{leader_name.replace(' ', '_')}_KPI_Data.csv",
            mime="text/csv",
        )
    
    
    
    
   

