"""Streamlit entry point. Pages live in dashboard/pages/ and are registered here with st.navigation.
Deploy this file on Streamlit Community Cloud (private app, DATABASE_URL in secrets)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root importable on Streamlit Cloud

st.set_page_config(page_title="vibe-tracker", page_icon="🎮", layout="wide", initial_sidebar_state="auto")

nav = st.navigation(
    {
        "Explore": [
            st.Page("pages/platforms.py", title="Platforms", icon="🏁", default=True),
            st.Page("pages/games.py", title="Games", icon="🎮"),
            st.Page("pages/creators.py", title="Creators", icon="🧑‍🎨"),
        ],
        "Detail": [
            st.Page("pages/platform.py", title="Platform", icon="📊", url_path="platform"),
            st.Page("pages/creator.py", title="Creator", icon="👤", url_path="creator"),
            st.Page("pages/game.py", title="Game", icon="🕹️", url_path="game"),
        ],
        "System": [st.Page("pages/health.py", title="Health", icon="🩺")],
    }
)
nav.run()
