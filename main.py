import streamlit as st
import toml
import importlib
import os
import pandas as pd
from fractions import Fraction
import numpy as np
import plotly.express as px
import plotly.graph_objects as go

# --- Streamlit page config (first Streamlit command!) ---
st.set_page_config(
    page_title="Faculty Leader Dashboards",
    layout="wide"
)

# --- Mapping of FL -> secrets file ---
FL_CONFIGS = {
    "Annelies": "configs/annelies_secrets.toml",
    "Ela": "configs/ela_secrets.toml",
    "Ian": "configs/ian_secrets.toml",
    "Geoff": "configs/geoff_secrets.toml",
    "Kristin": "configs/kristin_secrets.toml",
    "Jessica": "configs/jessica_secrets.toml",
}

# --- Load universal config ---
CONFIG_FILE = "config.toml"
if os.path.exists(CONFIG_FILE):
    config = toml.load(CONFIG_FILE)
else:
    config = {}

# --- Initialize session state ---
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "fl_choice" not in st.session_state:
    st.session_state["fl_choice"] = None

# --- Logout button ---
if st.session_state["authenticated"]:
    if st.button("Logout"):
        st.session_state["authenticated"] = False
        st.session_state["fl_choice"] = None
        st.experimental_rerun()

# --- Authentication flow ---
if not st.session_state["authenticated"]:
    # Show FL selection dropdown
    fl_choice = st.selectbox("Select Faculty Leader:", list(FL_CONFIGS.keys()))
    
    # Load password for selected FL
    secrets_file = FL_CONFIGS[fl_choice]
    if os.path.exists(secrets_file):
        secrets_data = toml.load(secrets_file)
        correct_password = secrets_data.get("auth", {}).get("password", "")
    else:
        correct_password = ""
    
    # Password input
    password = st.text_input("Enter password:", type="password")
    if password:
        if password == correct_password:
            st.session_state["authenticated"] = True
            st.session_state["fl_choice"] = fl_choice
            st.experimental_rerun()
        else:
            st.error("Incorrect password")
    st.stop()  # Stop here if not authenticated

# --- User is authenticated at this point ---
st.success(f"Authenticated! Loading {st.session_state['fl_choice']} dashboard...")

# --- Dynamically import dashboard module ---
module_name = f"dashboards.{st.session_state['fl_choice']}"
dashboard_module = importlib.import_module(module_name)

# --- Render dashboard passing universal config ---
dashboard_module.render_app(config)
