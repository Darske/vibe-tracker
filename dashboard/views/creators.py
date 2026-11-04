"""Creators: rotrends-style ranked list of creators by portfolio plays in window, with a Rising view
(acceleration above a threshold, ≥2 games) and the full sortable table underneath."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import queries as q, ui
from dashboard.components import fmt_dt, phone_table, selected_row
from dashboard.filters import go as nav, range_bar, sidebar
from dashboard.theme import abbrev, pct

f = sidebar()
ui.topnav("creators")
ui.page_header("Creators", "Who is breaking out. Portfolio plays are summed over a creator's tracked games on that platform.")
f = range_bar(f)

cr = q.creators_window(f.window_start, f.selected)
if cr.empty:
    st.info("No creators for this selection yet.")
    st.stop()

c1, c2 = st.columns([2, 3])
with c1:
    view = ui.pills("View", ["Top", "Rising", "Lifetime"], "c_view", "Top")
with c2:
    search = st.text_input("Search creator", "", placeholder="Search creator", label_visibility="collapsed")
with st.expander("Filters", expanded=False):
    a, b = st.columns(2)
    threshold = a.number_input("Rising: acceleration ≥", value=0.25, step=0.05, format="%.2f")
    status_f = b.multiselect("Outreach status", ["new", "watching", "contacted", "replied", "passed", "funded"], default=[])

g = cr.copy()
g["outreach_status"] = g["outreach_status"].fillna("new")
if view == "Rising":
    g = g[(g["acceleration"].fillna(-9) >= threshold) & (g["games_live"].fillna(0) >= 2)]
if status_f:
    g = g[g["outreach_status"].isin(status_f)]
if search:
    g = g[g["name"].fillna("").str.lower().str.contains(search.lower())]
sort_cols = {"Top": ["plays_window", "plays_7d"], "Rising": ["acceleration", "plays_window"], "Lifetime": ["plays_lifetime", "plays_window"]}[view]
g = g.sort_values(sort_cols, ascending=False, na_position="last").head(300).reset_index(drop=True)

# ---------------------------------------------------------------- ranked list
with st.container(border=True):
    if view == "Rising":
        ui.eyebrow("Trending today", "🔥"); ui.title("Rising creators")
        ui.ranked_list(ui.creator_rows(g.head(25), "acceleration"), "value", "pct", rank_key=None)
        sub = f"value = acceleration (this week's plays vs last week's), creators with ≥2 games · threshold {threshold:.2f}"
    elif view == "Lifetime":
        ui.eyebrow("Leaderboard", "🏆"); ui.title("Top creators by lifetime plays")
        ui.ranked_list(ui.creator_rows(g.head(25), "plays_lifetime"), "value", "plain", rank_key=None)
        sub = "each platform's own cumulative counters, summed over the creator's tracked games"
    else:
        ui.eyebrow("Leaderboard", "🏆"); ui.title(f"Top creators by plays, {f.window}")
        ui.ranked_list(ui.creator_rows(g.head(25), "plays_window"), "value", "plain", rank_key=None)
        sub = f"plays over the last {f.window}, summed over the creator's tracked games on that platform"
    st.markdown(f"<div class='vt-sub'>{sub}</div>", unsafe_allow_html=True)


def links_txt(links) -> str:
    return " ".join(k for k in (links or {}) if links.get(k)) or "—"


with st.expander(f"Full table ({len(g)} creators)"):
    disp = pd.DataFrame({
        "#": range(1, len(g) + 1),
        "Creator": g["name"].fillna(g["external_id"]),
        "Platform": g["display_name"],
        "Games": g["games_live"].map(lambda v: "—" if pd.isna(v) else int(v)),
        "Lifetime plays": g["plays_lifetime"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        f"Plays {f.window}": g["plays_window"].map(abbrev),
        "Plays / day": (g["plays_window"] / f.window_days).map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "Share of platform": g["share_of_source"].map(lambda v: "—" if pd.isna(v) else f"{float(v) * 100:.1f}%"),
        "Growth 7d": g["growth_7d"].map(pct),
        "Accel.": g["acceleration"].map(lambda v: "—" if pd.isna(v) else f"{float(v):+.2f}"),
        "Hit games": g["hit_games"].map(lambda v: "—" if pd.isna(v) else int(v)),
        "Followers": g["followers"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "First seen": g["first_seen"].map(fmt_dt),
        "Status": g["outreach_status"],
        "Links": g["links"].map(links_txt),
    })
    ev = phone_table(disp, ["#", "Creator", "Platform", "Lifetime plays", f"Plays {f.window}", "Plays / day"], key=f"creators_{view}")
    sel = selected_row(ev, g)
    if sel is not None:
        nav("creator", creator=int(sel["id"]))
    st.caption("Tap a creator to open their page with notes and cross-platform merge. "
               "Hit games = games at or above the platform's 90th percentile of 7-day plays.")
