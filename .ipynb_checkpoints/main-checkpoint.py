# import streamlit as st
# import toml
# import importlib
# import os
# import pandas as pd
# from fractions import Fraction
# import numpy as np
# import plotly.express as px
# import plotly.graph_objects as go

# # --- Streamlit page config (first Streamlit command!) ---
# st.set_page_config(
#     page_title="Faculty Leader Dashboards",
#     layout="wide"
# )

# # --- Mapping of FL -> secrets file ---
# FL_CONFIGS = {
#     "Annelies": "configs/annelies_secrets.toml",
#     "Ela": "configs/ela_secrets.toml",
#     "Ian": "configs/ian_secrets.toml",
#     "Geoff": "configs/geoff_secrets.toml",
#     "Kristin": "configs/kristin_secrets.toml",
#     "Jessica": "configs/jessica_secrets.toml",
# }

# # --- Load universal config ---
# CONFIG_FILE = "config.toml"
# if os.path.exists(CONFIG_FILE):
#     config = toml.load(CONFIG_FILE)
# else:
#     config = {}

# # --- Initialize session state ---
# if "authenticated" not in st.session_state:
#     st.session_state["authenticated"] = False
# if "fl_choice" not in st.session_state:
#     st.session_state["fl_choice"] = None

# # --- Logout button ---
# if st.session_state["authenticated"]:
#     if st.button("Logout"):
#         st.session_state["authenticated"] = False
#         st.session_state["fl_choice"] = None
#         st.experimental_rerun()

# # --- Authentication flow ---
# if not st.session_state["authenticated"]:
#     # Show FL selection dropdown
#     fl_choice = st.selectbox("Select Faculty Leader:", list(FL_CONFIGS.keys()))
    
#     # Load password for selected FL
#     secrets_file = FL_CONFIGS[fl_choice]
#     if os.path.exists(secrets_file):
#         secrets_data = toml.load(secrets_file)
#         correct_password = secrets_data.get("auth", {}).get("password", "")
#     else:
#         correct_password = ""
    
#     # Password input
#     password = st.text_input("Enter password:", type="password")
#     if password:
#         if password == correct_password:
#             st.session_state["authenticated"] = True
#             st.session_state["fl_choice"] = fl_choice
#             st.experimental_rerun()
#         else:
#             st.error("Incorrect password")
#     st.stop()  # Stop here if not authenticated

# # --- User is authenticated at this point ---
# st.success(f"Authenticated! Loading {st.session_state['fl_choice']} dashboard...")

# # --- Dynamically import dashboard module ---
# module_name = f"dashboards.{st.session_state['fl_choice']}"
# dashboard_module = importlib.import_module(module_name)

# # --- Render dashboard passing universal config ---
# dashboard_module.render_app(config)

# import streamlit as st
# import toml
# import importlib
# import os

# # --- Streamlit page config ---
# st.set_page_config(page_title="Faculty Leader Dashboards", layout="wide")

# # --- Mapping of FL -> secrets file ---
# FL_CONFIGS = {
#     "Annelies": "configs/annelies_secrets.toml",
#     "Ela": "configs/ela_secrets.toml",
#     "Ian": "configs/ian_secrets.toml",
#     "Geoff": "configs/geoff_secrets.toml",
#     "Kristin": "configs/kristin_secrets.toml",
#     "Jessica": "configs/jessica_secrets.toml",
# }

# # --- Load universal config ---
# CONFIG_FILE = "config.toml"
# config = toml.load(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else {}

# # --- Initialize session state ---
# if "authenticated" not in st.session_state:
#     st.session_state["authenticated"] = False
# if "fl_choice" not in st.session_state:
#     st.session_state["fl_choice"] = None

# # --- Authentication form ---
# if not st.session_state["authenticated"]:
#     with st.form("login_form"):
#         # FL selection with default first item
#         fl_input = st.selectbox(
#             "Select Faculty Leader:",
#             list(FL_CONFIGS.keys()),
#             index=0  # always default to first FL
#         )

#         # Password input
#         password_input = st.text_input(
#             "Enter password:",
#             type="password"
#         )

#         # Submit button
#         submitted = st.form_submit_button("Login")

#     if submitted:
#         secrets_file = FL_CONFIGS[fl_input]
#         correct_password = ""
#         if os.path.exists(secrets_file):
#             secrets_data = toml.load(secrets_file)
#             correct_password = secrets_data.get("auth", {}).get("password", "")

#         if password_input == correct_password:
#             # ✅ Mark authenticated and store FL choice
#             st.session_state["authenticated"] = True
#             st.session_state["fl_choice"] = fl_input
#             st.success(f"Authenticated! Loading {st.session_state['fl_choice']} dashboard...")
#             # No st.stop() here — continue to render dashboard
#         else:
#             st.error("Incorrect password")
#             st.stop()  # stop only if password is wrong

# # --- Logout button ---
# if st.button("Logout"):
#     st.session_state["authenticated"] = False
#     st.session_state["fl_choice"] = None
#     st.experimental_rerun()

# # --- User is authenticated at this point ---
# if st.session_state["fl_choice"]:
#     st.success(f"Authenticated! Loading {st.session_state['fl_choice']} dashboard...")

#     # --- Dynamically import dashboard module ---
#     module_name = f"dashboards.{st.session_state['fl_choice']}"
#     dashboard_module = importlib.import_module(module_name)

#     # --- Render dashboard passing universal config ---
#     dashboard_module.render_app(config)




import streamlit as st
import toml
import importlib
import os

# --- Streamlit page config ---
st.set_page_config(page_title="Faculty Leader Dashboards", layout="wide")

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
config = toml.load(CONFIG_FILE) if os.path.exists(CONFIG_FILE) else {}

# --- Initialize session state ---
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "fl_choice" not in st.session_state:
    st.session_state["fl_choice"] = None

# --- Login form (sidebar) ---
if not st.session_state["authenticated"]:
    with st.sidebar.form("login_form"):
        fl_input = st.selectbox(
            "Select Faculty Leader:",
            list(FL_CONFIGS.keys()),
            index=0
        )
        password_input = st.text_input(
            "Enter password:",
            type="password"
        )
        submitted = st.form_submit_button("Login")

    if submitted:
        secrets_file = FL_CONFIGS[fl_input]
        correct_password = ""
        if os.path.exists(secrets_file):
            secrets_data = toml.load(secrets_file)
            correct_password = secrets_data.get("auth", {}).get("password", "")

        if password_input == correct_password:
            # ✅ One-click login works
            st.session_state["authenticated"] = True
            st.session_state["fl_choice"] = fl_input
        else:
            st.error("Incorrect password")
            st.stop()  # stop only if password is wrong

# --- Logout button (sidebar) ---
if st.session_state["authenticated"]:
    with st.sidebar:
        if st.button("Logout"):
            st.session_state["authenticated"] = False
            st.session_state["fl_choice"] = None
            st.experimental_rerun()

# --- Dashboard content ---
if st.session_state["authenticated"] and st.session_state["fl_choice"]:
    st.success(f"Authenticated! Loading {st.session_state['fl_choice']} dashboard...")

    module_name = f"dashboards.{st.session_state['fl_choice']}"
    dashboard_module = importlib.import_module(module_name)
    dashboard_module.render_app(config)