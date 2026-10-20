"""Production entry point: Snowflake viewer identity, owner connection, no preview."""
from pathlib import Path
import sys
import uuid

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
st.set_page_config(page_title="Employee roster", layout="wide")

# Identity must be established before importing or constructing the repository.
try:
    actor = st.user.user_name
except Exception:
    actor = None
if not isinstance(actor, str) or not actor.strip():
    st.error("Your Snowflake viewer identity is unavailable. Open this app from Snowflake with an authorized user.")
    st.text(f"Correlation ID: {uuid.uuid4().hex}")
    st.stop()

try:
    from employee_roster.persistence import SnowflakeRepository
    from employee_roster.ui import render_app

    session = st.connection("snowflake").session()
    repository = SnowflakeRepository(session)
except Exception:
    st.error("The roster connection is unavailable. Please retry or contact support.")
    st.text(f"Correlation ID: {uuid.uuid4().hex}")
    st.stop()

try:
    render_app(repository, actor)
except Exception:
    st.error("The roster could not be displayed. Please retry or contact support.")
    st.text(f"Correlation ID: {uuid.uuid4().hex}")
    st.stop()
