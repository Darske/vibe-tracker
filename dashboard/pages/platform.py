"""Platform page: header, stats line, three cards, the platform's top 100 as a sortable table with Movers / New entrants."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import queries as q
from dashboard.components import badge, fmt_dt, metric_card, phone_table, selected_row
from dashboard.filters import go as nav, label_for, sidebar, today_utc
from dashboard.theme import abbrev, color_map, likes_per_k, pct, ratio_fmt

f = sidebar()
src = st.query_params.get("platform")
srcs = f.sources.set_index("name")
if not src or src not in srcs.index:
    st.title("Platform")
    src = st.selectbox("Choose a platform", list(srcs.index), format_func=lambda n: srcs.loc[n, "display_name"])
row = srcs.loc[src]
color = color_map(list(f.sources["name"]))[src]
labels = row["metric_map"] or {}

st.title(row["display_name"])
st.markdown(f"{badge(row['segment'])} &nbsp; [{row['base_url']}]({row['base_url']})", unsafe_allow_html=True)

pdaily = q.platform_daily()
pdaily = pdaily[pdaily["source"] == src].sort_values("day")
now = q.platform_now()
now = now[now["source"] == src]
games = q.games_window(f.window_start, (src,))

# ---------------------------------------------------------------- stats line
plays_win = games["plays_window"].sum(min_count=1)
peak_today = pdaily[pdaily["day"] == today_utc()]["ccu_peak_top100"]
st.markdown(
    f"**{len(games):,}** games tracked · **{games['creator_id'].nunique():,}** creators · "
    f"**{abbrev(plays_win)}** plays in {f.window} · **{abbrev(peak_today.iloc[0]) if not peak_today.empty else '—'}** peak players today"
)
st.caption(" · ".join(labels.values()) or "metric labels: none recorded")

# ---------------------------------------------------------------- cards
lifetime = float(now["plays_lifetime"].iloc[0]) if not now.empty and pd.notna(now["plays_lifetime"].iloc[0]) else None
c0, c1, c2, c3 = st.columns(4)
with c0:
    with st.container(border=True):
        st.markdown("**Lifetime plays (tracked games)**")
        st.markdown(f"<span style='font-size:2rem;font-weight:600'>{abbrev(lifetime)}</span>", unsafe_allow_html=True)
        st.caption("the platform's own cumulative counters, summed over the games we track · " + labels.get("plays", "plays not public"))
with c1:
    metric_card("Daily plays (tracked top 100)", pdaily[["day", "plays_delta_top100"]].rename(columns={"plays_delta_top100": "value"}),
                f.anchor, "daily-peak", color, note="our measured day-over-day increase")
with c2:
    metric_card("Concurrent players (daily peak)", pdaily[["day", "ccu_peak_top100"]].rename(columns={"ccu_peak_top100": "value"}),
                f.anchor, "daily-peak", color, note=labels.get("players_now", "no players-online counter"),
                second=pdaily[["day", "ccu_avg_top100"]].rename(columns={"ccu_avg_top100": "value"}), second_label="average")
with c3:
    metric_card("New games per day", pdaily[["day", "games_new"]].rename(columns={"games_new": "value"}), f.anchor, "daily-peak", color,
                note="first seen in a tracked list")

# ---------------------------------------------------------------- top 100 table
st.subheader(f"Top games on {row['display_name']}")
mode = st.radio("Show", ["Top 100", "Movers", "New entrants (7 days)", "Best liked (≥1k plays)"], horizontal=True, label_visibility="collapsed")
g = games.copy()
g["rank_change"] = g["rank_prior"] - g["rank_in_source"]
if mode == "Movers":
    g = g.dropna(subset=["rank_change"]).sort_values("rank_change", ascending=False)
elif mode.startswith("New"):
    g = g[pd.to_datetime(g["first_seen"]).dt.date >= today_utc() - pd.Timedelta(days=7)].sort_values("plays_window", ascending=False, na_position="last")
elif mode.startswith("Best liked"):
    g = g.assign(_lpk=[likes_per_k(l, p_) for l, p_ in zip(g["likes"], g["plays_total"])]).dropna(subset=["_lpk"]).sort_values("_lpk", ascending=False)
else:
    g = g.sort_values(["rank_in_source", "plays_window"], ascending=[True, False], na_position="last")
g = g.head(100).reset_index(drop=True)

disp = pd.DataFrame({
    "Rank": g["rank_in_source"].map(lambda v: "—" if pd.isna(v) else int(v)),
    "Δ": g["rank_change"].map(lambda v: "—" if pd.isna(v) else f"{int(v):+d}"),
    "Title": g["title"].fillna(g["url"]),
    "Creator": g["creator_name"].fillna("—"),
    "Lifetime plays": g["plays_total"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    f"Plays {f.window}": g["plays_window"].map(abbrev),
    "Plays / day": (g["plays_window"] / f.window_days).map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Growth 7d": g["growth_7d"].map(pct),
    "Players now": g["players_now"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Likes": g["likes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "Likes / 1k plays": [ratio_fmt(likes_per_k(l, p_)) for l, p_ in zip(g["likes"], g["plays_total"])],
    "Remixes": g["remixes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
    "First seen": g["first_seen"].map(fmt_dt),
})
ev = phone_table(disp, ["Rank", "Title", "Lifetime plays", f"Plays {f.window}", "Plays / day", "Likes / 1k plays"], key=f"platform_{src}_{mode}")
sel = selected_row(ev, g)
if sel is not None:
    nav("game", game=int(sel["id"]))
st.caption(f"Δ = rank change vs the last day before the {f.window} window. "
           f"{label_for(f, src, 'plays', 'plays')}. Tap a row to open the game.")
