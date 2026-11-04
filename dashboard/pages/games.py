"""Games (cross-platform): top games across all selected platforms by plays in window or by peak concurrent players."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import queries as q
from dashboard.components import fmt_dt, phone_table, selected_row
from dashboard.filters import go as nav, sidebar, today_utc
from dashboard.theme import abbrev, likes_per_k, pct, ratio_fmt

f = sidebar()
st.title("Games")
st.caption("Cross-platform comparisons use rank and growth, never raw plays: each platform counts a 'play' differently (see the Label column).")

games = q.games_window(f.window_start, f.selected)
if games.empty:
    st.info("No games for this selection yet.")
    st.stop()

c1, c2 = st.columns([2, 3])
by = c1.radio("Rank by", [f"Plays in {f.window}", "Lifetime plays", "Peak concurrent players", "Likes per 1k plays (≥1k plays)"], horizontal=True, label_visibility="collapsed")
mode = c2.radio("Show", ["All", "Movers", "New entrants (7 days)"], horizontal=True, label_visibility="collapsed")
search = st.text_input("Search title or creator", "")

g = games.copy()
g["rank_change"] = g["rank_prior"] - g["rank_in_source"]
if search:
    s = search.lower()
    g = g[g["title"].fillna("").str.lower().str.contains(s) | g["creator_name"].fillna("").str.lower().str.contains(s)]
if mode == "Movers":
    g = g.dropna(subset=["rank_change"]).sort_values("rank_change", ascending=False)
elif mode.startswith("New"):
    g = g[pd.to_datetime(g["first_seen"]).dt.date >= today_utc() - pd.Timedelta(days=7)]
g["likes_per_k"] = [likes_per_k(l, p_) for l, p_ in zip(g["likes"], g["plays_total"])]
sort_col = {"Plays": "plays_window", "Lifet": "plays_total", "Likes": "likes_per_k"}.get(by[:5], "ccu_peak_window")
if sort_col == "likes_per_k":
    g = g.dropna(subset=["likes_per_k"])
if mode != "Movers":
    g = g.sort_values(sort_col, ascending=False, na_position="last")
g = g.head(200).reset_index(drop=True)

disp = pd.DataFrame({
    "#": range(1, len(g) + 1),
    "Δ": g["rank_change"].map(lambda v: "—" if pd.isna(v) else f"{int(v):+d}"),
    "Title": g["title"].fillna(g["url"]),
    "Creator": g["creator_name"].fillna("—"),
    "Platform": g["display_name"],
    "Lifetime plays": g["plays_total"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    f"Plays {f.window}": g["plays_window"].map(abbrev),
    "Plays / day": (g["plays_window"] / f.window_days).map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Growth 7d": g["growth_7d"].map(pct),
    "Peak players": g["ccu_peak_window"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Players now": g["players_now"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Likes": g["likes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Likes / 1k plays": g["likes_per_k"].map(ratio_fmt),
    "Remixes": g["remixes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Rank in platform": g["rank_in_source"].map(lambda v: "—" if pd.isna(v) else int(v)),
    "First seen": g["first_seen"].map(fmt_dt),
    "Label": g["metric_map"].map(lambda m: (m or {}).get("plays", "")),
})
if by.startswith("Likes"):
    phone = ["#", "Title", "Platform", "Likes / 1k plays", "Likes", "Lifetime plays"]
elif by.startswith("Peak"):
    phone = ["#", "Title", "Platform", "Peak players", "Players now", "Growth 7d"]
else:
    phone = ["#", "Title", "Platform", "Lifetime plays", f"Plays {f.window}", "Likes / 1k plays"]
ev = phone_table(disp, phone, key=f"games_{by}_{mode}")
sel = selected_row(ev, g)
if sel is not None:
    nav("game", game=int(sel["id"]))
st.caption("Tap a row to open the game. Δ = rank change within its platform vs before the window.")
