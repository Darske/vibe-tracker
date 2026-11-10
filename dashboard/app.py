"""Streamlit entry point. Pages live in dashboard/views/ (not "pages/", which would trigger Streamlit's legacy auto-navigation on deep links) and are registered here with st.navigation.
Deploy this file on Streamlit Community Cloud (private app, DATABASE_URL in secrets)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root importable on Streamlit Cloud

st.set_page_config(page_title="vibe-tracker", page_icon="🎮", layout="wide", initial_sidebar_state="collapsed")
from dashboard.ui import inject_css  # noqa: E402

inject_css()

nav = st.navigation(
    {
        "Explore": [
            st.Page("views/platforms.py", title="Platforms", icon="🏁", default=True),
            st.Page("views/games.py", title="Games", icon="🎮"),
            st.Page("views/creators.py", title="Creators", icon="🧑‍🎨"),
            st.Page("views/releases.py", title="Releases", icon="🚀"),
        ],
        "Detail": [
            st.Page("views/platform.py", title="Platform", icon="📊", url_path="platform"),
            st.Page("views/creator.py", title="Creator", icon="👤", url_path="creator"),
            st.Page("views/game.py", title="Game", icon="🕹️", url_path="game"),
        ],
        "System": [st.Page("views/health.py", title="Health", icon="🩺")],
    },
    position="hidden",  # rotrends-style top nav (ui.topnav) instead of the sidebar list; the sidebar only holds filters
)
nav.run()
