"""Streamlit entry point. Pages live in dashboard/pages/ and are registered here with st.navigation
so the sidebar order is ours. Deploy this file on Streamlit Community Cloud (private app)."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root importable on Streamlit Cloud

st.set_page_config(page_title="vibe-tracker", page_icon="🎮", layout="wide", initial_sidebar_state="collapsed")

PAGES = Path(__file__).parent / "pages"
nav = st.navigation(
    [
        st.Page("pages/health.py", title="Health", icon="🩺", default=True),
        # milestone 3: Platforms (home), Platform, Games, Creators, Creator, Game
    ]
)
nav.run()
