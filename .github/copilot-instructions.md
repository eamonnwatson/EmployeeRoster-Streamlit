# Repository Guidelines

## Run the app

- Use Python 3.11 and `uv sync` to install the dependency declared in `pyproject.toml`.
- Run locally with `uv run streamlit run streamlit_app.py`.
- `pyproject.toml` does not define test or lint commands.

## Architecture

- `streamlit_app.py` is both the Streamlit UI and the app's data flow: it gets a Snowflake session with `st.connection("snowflake").session()`, creates a Snowpark DataFrame, converts it to pandas, and renders a chart and table. The current data is illustrative and includes a slider-driven value.
- `snowflake.yml` declares `streamlit_app.py` as the Streamlit app's main file and lists the files packaged for deployment. Keep that artifact list aligned with files required by the app.
- Streamlit in Snowflake supplies Streamlit by default. `pyproject.toml` notes that using a different Streamlit version requires an External Access Integration for package downloads.

## Conventions

- Use Streamlit's Snowflake connection and session for Snowflake work. The app can construct Snowpark DataFrames through the session; for raw SQL, use `session.sql(...)`.
- Declare Python dependencies in `pyproject.toml`; the project targets Python `~=3.11.0`.
