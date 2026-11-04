"""Games (cross-platform): rotrends-style ranked list of the top games across the selected platforms,
ranked by plays in window / lifetime plays / peak concurrent players / likes per 1k plays, with Movers and New views.
The home page's 'Explore list' buttons land here with ?mode= / ?by= query params."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import streamlit as st

from dashboard import queries as q, ui
from dashboard.components import fmt_dt, phone_table, selected_row
from dashboard.filters import go as nav, handed_over, param, range_bar, sidebar, today_utc
from dashboard.theme import abbrev, likes_per_k, pct, ratio_fmt

f = sidebar()
ui.topnav("games")
ui.page_header("Games", "Cross-platform comparisons use rank and growth, never raw plays: each platform counts a 'play' differently.")
f = range_bar(f)

games = q.games_window(f.window_start, f.selected)
if games.empty:
    st.info("No games for this selection yet.")
    st.stop()

BY = {"Plays": "Plays", "Lifetime": "Lifetime plays", "Players": "Peak players", "Likes": "Likes / 1k plays"}
MODES = {"All": "All", "Movers": "Movers", "New": "New (7 days)"}
handed = handed_over()  # arriving from an Explore button / go(): start from that list's defaults
qp_by, qp_mode = param("by"), param("mode")
if handed or st.session_state.get("_g_applied") != (qp_by, qp_mode):
    st.session_state["g_by__value"] = BY.get(qp_by, BY["Plays"])
    st.session_state["g_mode__value"] = MODES.get(qp_mode, "All")
st.session_state["_g_applied"] = (qp_by, qp_mode)  # a deep link applies once, then the pills rule

c1, c2 = st.columns([3, 2])
with c1:
    by_label = ui.pills("Rank by", list(BY.values()), "g_by", BY["Plays"])
with c2:
    mode_label = ui.pills("Show", list(MODES.values()), "g_mode", "All")
by = next(k for k, v in BY.items() if v == by_label)
mode = next(k for k, v in MODES.items() if v == mode_label)
search = st.text_input("Search title or creator", "", placeholder="Search title or creator", label_visibility="collapsed")

g = games.copy()
g["rank_change"] = g["rank_prior"] - g["rank_in_source"]
g["likes_per_k"] = [likes_per_k(l, p_) for l, p_ in zip(g["likes"], g["plays_total"])]
if search:
    s = search.lower()
    g = g[g["title"].fillna("").str.lower().str.contains(s) | g["creator_name"].fillna("").str.lower().str.contains(s)]
if mode == "Movers":
    g = g.dropna(subset=["rank_change"]).sort_values("rank_change", ascending=False)
elif mode == "New":
    g = g[pd.to_datetime(g["first_seen"]).dt.date >= today_utc() - timedelta(days=7)]
sort_col = {"Plays": "plays_window", "Lifetime": "plays_total", "Likes": "likes_per_k", "Players": "ccu_peak_window"}[by]
if sort_col == "likes_per_k":
    g = g.dropna(subset=["likes_per_k"])
if mode != "Movers":
    g = g.sort_values(sort_col, ascending=False, na_position="last")
g = g.head(200).reset_index(drop=True)

# ---------------------------------------------------------------- ranked list (top 25)
head_title = {"All": f"Top games by {by_label.lower()}" + (f", {f.window}" if by in ("Plays", "Players") else ""), "Movers": "Top moving games", "New": "New and rising"}[mode]
value_key, value_kind, suffix = sort_col, "plain", ""
if mode == "Movers":
    value_key, value_kind = "rank_change", "rank"
elif by == "Likes":
    suffix = " /1k"
with st.container(border=True):
    ui.eyebrow("Trending today" if mode != "All" else "Leaderboard", "🔥" if mode != "All" else "🏆")
    ui.title(head_title)
    rows = ui.game_rows(g.head(25), value_key, sub_key=None)
    for r, (_, src) in zip(rows, g.head(25).iterrows()):
        r["creator"] = f"{src['creator_name']} · {src['display_name']}" if src.get("creator_name") else src["display_name"]
    ui.ranked_list(rows, "value", value_kind, value_suffix=suffix)
    cap = {"Movers": "value = places gained within its platform's ranking since before the window",
           "New": f"first seen in a tracked list within 7 days · value = plays in {f.window}",
           "All": {"Plays": f"plays counted by each platform over the last {f.window}", "Lifetime": "each platform's own cumulative counter",
                   "Players": f"highest concurrent-player reading in the last {f.window} (platforms with a live counter)",
                   "Likes": "likes per 1,000 lifetime plays, games with ≥1,000 plays"}[by]}[mode]
    st.markdown(f"<div class='vt-sub'>{cap} · #rank = position within its platform</div>", unsafe_allow_html=True)

# ---------------------------------------------------------------- full table
with st.expander(f"Full table ({len(g)} games)"):
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
        "Rank in platform": g["rank_in_source"].map(lambda v: "—" if pd.isna(v) else str(int(v))),
        "First seen": g["first_seen"].map(fmt_dt),
        "Label": g["metric_map"].map(lambda m: (m or {}).get("plays", "")),
    })
    if by == "Likes":
        phone = ["#", "Title", "Platform", "Likes / 1k plays", "Likes", "Lifetime plays"]
    elif by == "Players":
        phone = ["#", "Title", "Platform", "Peak players", "Players now", "Growth 7d"]
    else:
        phone = ["#", "Title", "Platform", "Lifetime plays", f"Plays {f.window}", "Likes / 1k plays"]
    ev = phone_table(disp, phone, key=f"games_{by}_{mode}")
    sel = selected_row(ev, g)
    if sel is not None:
        nav("game", game=int(sel["id"]))
    st.caption("Tap a row to open the game. Δ = rank change within its platform vs before the window.")
