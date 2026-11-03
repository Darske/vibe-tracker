"""Creators: ranked by portfolio plays in window, with a Rising toggle (acceleration above a threshold, ≥2 games)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import queries as q
from dashboard.components import fmt_dt, phone_table, selected_row
from dashboard.filters import go as nav, sidebar
from dashboard.theme import abbrev, pct

f = sidebar()
st.title("Creators")
st.caption("Who is breaking out. Portfolio plays are summed over a creator's tracked games on that platform.")

cr = q.creators_window(f.window_start, f.selected)
if cr.empty:
    st.info("No creators for this selection yet.")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 2])
rising = c1.toggle("Rising only", value=False, help="acceleration above the threshold and at least 2 games")
threshold = c2.number_input("Acceleration ≥", value=0.25, step=0.05, format="%.2f", disabled=not rising)
status_f = c3.multiselect("Outreach status", ["new", "watching", "contacted", "replied", "passed", "funded"], default=[])
search = st.text_input("Search creator", "")

g = cr.copy()
g["outreach_status"] = g["outreach_status"].fillna("new")
if rising:
    g = g[(g["acceleration"].fillna(-9) >= threshold) & (g["games_live"].fillna(0) >= 2)]
if status_f:
    g = g[g["outreach_status"].isin(status_f)]
if search:
    g = g[g["name"].fillna("").str.lower().str.contains(search.lower())]
g = g.sort_values(["plays_window", "plays_7d"], ascending=False, na_position="last").head(300).reset_index(drop=True)


def links_txt(links) -> str:
    return " ".join(k for k in (links or {}) if links.get(k)) or "—"


disp = pd.DataFrame({
    "#": range(1, len(g) + 1),
    "Creator": g["name"].fillna(g["external_id"]),
    "Platform": g["display_name"],
    "Games": g["games_live"].map(lambda v: "—" if pd.isna(v) else int(v)),
    f"Plays {f.window}": g["plays_window"].map(abbrev),
    "Share of platform": g["share_of_source"].map(lambda v: "—" if pd.isna(v) else f"{float(v) * 100:.1f}%"),
    "Growth 7d": g["growth_7d"].map(pct),
    "Accel.": g["acceleration"].map(lambda v: "—" if pd.isna(v) else f"{float(v):+.2f}"),
    "Hit games": g["hit_games"].map(lambda v: "—" if pd.isna(v) else int(v)),
    "Followers": g["followers"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "First seen": g["first_seen"].map(fmt_dt),
    "Status": g["outreach_status"],
    "Links": g["links"].map(links_txt),
})
ev = phone_table(disp, ["#", "Creator", "Platform", f"Plays {f.window}", "Growth 7d", "Status"], key=f"creators_{rising}")
sel = selected_row(ev, g)
if sel is not None:
    nav("creator", creator=int(sel["id"]))
st.caption("Tap a creator to open their page (vibez.club layout) with notes and cross-platform merge. "
           "Hit games = games at or above the platform's 90th percentile of 7-day plays.")
