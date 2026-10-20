"""Local-only entry point. Never include dev/ in deployment artifacts."""
from pathlib import Path
import sys

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dev.synthetic import SyntheticRepository
from employee_roster.ui import render_app

st.set_page_config(page_title="Employee roster - synthetic preview", layout="wide")
if "_preview_repository" not in st.session_state:
    st.session_state["_preview_repository"] = SyntheticRepository()

actor = st.sidebar.selectbox("Synthetic app role", [
    "PREVIEW.ADMIN", "PREVIEW.EDITOR", "PREVIEW.VIEWER", "PREVIEW.CLIENTADMIN",
], key="_preview_actor")
render_app(st.session_state["_preview_repository"], actor, synthetic=True)
