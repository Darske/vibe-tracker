"""Platform page, rotrends-style: hero (players right now / all-time peak, if the platform publishes a live
count; otherwise daily plays), stat tiles, metric cards, then the platform's top games as a ranked list + table."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q, ui
from dashboard.components import badge, fmt_dt, metric_card, phone_table, selected_row
from dashboard.filters import go as nav, label_for, param, range_bar, sidebar, today_utc
from dashboard.theme import abbrev, color_map, full, likes_per_k, line_mode, pct, ratio_fmt, time_axis

f = sidebar()
ui.topnav("platforms")
src = param("platform")
srcs = f.sources.set_index("name")
if not src or src not in srcs.index:
    ui.page_header("Platform")
    src = st.selectbox("Choose a platform", list(srcs.index), format_func=lambda n: srcs.loc[n, "display_name"])
row = srcs.loc[src]
color = color_map(list(f.sources["name"]))[src]
labels = row["metric_map"] or {}

ui.page_header(row["display_name"], f"{badge(row['segment'])} &nbsp; <a class='vt-link' href='{row['base_url']}' target='_blank'>{row['base_url']}</a>",
               eyebrow_text="Platform", icon="📊")
f = range_bar(f)

pdaily = q.platform_daily()
pdaily = pdaily[pdaily["source"] == src].sort_values("day")
now = q.platform_now()
now = now[now["source"] == src]
games = q.games_window(f.window_start, (src,))
win = pdaily[pd.to_datetime(pdaily["day"]).dt.date >= f.window_start]

# ---------------------------------------------------------------- hero
has_ccu = pdaily["ccu_peak_top100"].notna().any()
with st.container(border=True):
    h1, h2 = st.columns(2)
    if has_ccu:
        ccu = q.ccu_now((src,))
        with h1:
            ui.eyebrow("Players right now", "🔥")
            ui.hero(full(ccu.get("players_now")), ui.ago(ccu.get("updated_at")))
        with h2:
            ui.eyebrow("All-time peak", "🏆")
            pk_at = pd.Timestamp(ccu["peak_at"]).strftime("%b %-d at %-I:%M%p UTC").replace("AM", "am").replace("PM", "pm") if ccu.get("peak_at") is not None else None
            ui.hero(full(ccu.get("peak")), pk_at, white=True)
        series = q.ccu_platform_series(f.window_days, (src,))
        series = series[series["source"] == src] if not series.empty else series
        if len(series) < 2:
            st.caption("Not enough players-online readings in this range yet (polled every 30 minutes).")
        else:
            fig = go.Figure(go.Scatter(x=series["run_ts"], y=series["players"], mode=line_mode(len(series)), line=dict(width=2.5, color=color), marker=dict(size=4),
                                       hovertemplate="%{x|%b %-d %H:%M}: %{y:,.0f} players<extra></extra>"))
            fig.update_layout(height=260, margin=dict(t=16), xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"), showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.markdown("<div class='vt-sub'>Players online, summed over the tracked games · polled every 30 minutes</div>", unsafe_allow_html=True)
        if labels.get("players_now"):
            st.caption(f"Definition on {row['display_name']}: {labels['players_now']}")
    else:
        with h1:
            ui.eyebrow(f"Plays, {f.window}", "🔥")
            ui.hero(full(games["plays_window"].sum(min_count=1)), f"{abbrev(win['plays_delta_top100'].mean())} per day on average")
        with h2:
            ui.eyebrow("Lifetime plays", "🏆")
            lifetime = now["plays_lifetime"].iloc[0] if not now.empty else None
            ui.hero(full(lifetime), "the platform's own counters, summed over tracked games", white=True)
        if win["plays_delta_top100"].notna().sum() >= 2:
            fig = go.Figure(go.Scatter(x=win["day"], y=win["plays_delta_top100"], mode="lines+markers", line=dict(width=2.5, color=color), marker=dict(size=5),
                                       hovertemplate="%{x|%b %-d}: %{y:,.0f} plays<extra></extra>"))
            fig.update_layout(height=240, margin=dict(t=16), xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"), showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.caption("Not enough days of history in this range yet.")
        st.markdown(f"<div class='vt-sub'>Daily plays = our measured day-over-day increase across the tracked top 100 · {row['display_name']} publishes no live player count.</div>", unsafe_allow_html=True)
        if labels.get("plays"):
            st.caption(f"Definition on {row['display_name']}: {labels['plays']}")

# ---------------------------------------------------------------- tiles
lk = now["likes_lifetime"].iloc[0] if not now.empty else None
pk_ = now["plays_with_likes"].iloc[0] if not now.empty else None
t1, t2, t3, t4 = st.columns(4)
with t1:
    ui.tile("Games tracked", f"{len(games):,}", f"{games['creator_id'].nunique():,} creators")
with t2:
    ui.tile("Plays / day", abbrev(win["plays_delta_top100"].mean()) if not win.empty else "—", f"average over {f.window}")
with t3:
    ui.tile("New games / day", abbrev(win["games_new"].mean()) if not win.empty else "—", "first seen in a tracked list")
with t4:
    ui.tile("Likes per 1k plays", ratio_fmt(lk * 1000 / pk_) if lk is not None and pk_ else "—",
            f"median game {ratio_fmt(now['median_likes_per_k'].iloc[0])}" if not now.empty and pd.notna(now["median_likes_per_k"].iloc[0]) else "pooled over tracked games")

# ---------------------------------------------------------------- cards
c1, c2, c3 = st.columns(3)
with c1:
    metric_card("Daily plays, latest day", pdaily[["day", "plays_delta_top100"]].rename(columns={"plays_delta_top100": "value"}),
                f.anchor, "daily-peak", color, note="day-over-day increase")
with c2:
    metric_card("Concurrent players (daily peak)", pdaily[["day", "ccu_peak_top100"]].rename(columns={"ccu_peak_top100": "value"}),
                f.anchor, "daily-peak", color, note="players online, tracked top 100" if labels.get("players_now") else "no players-online counter",
                second=pdaily[["day", "ccu_avg_top100"]].rename(columns={"ccu_avg_top100": "value"}), second_label="average")
with c3:
    metric_card("New games, latest day", pdaily[["day", "games_new"]].rename(columns={"games_new": "value"}), f.anchor, "daily-peak", color,
                note="first seen in a tracked list")

# ---------------------------------------------------------------- top games
g = games.copy()
g["rank_change"] = g["rank_prior"] - g["rank_in_source"]
g["likes_per_k"] = [likes_per_k(l, p_) for l, p_ in zip(g["likes"], g["plays_total"])]
MODES = ["Top 100", "Movers", "New (7 days)", "Best liked"]
mode = ui.pills("Show", MODES, f"p_mode_{src}", "Top 100")
if mode == "Movers":
    g = g.dropna(subset=["rank_change"]).sort_values("rank_change", ascending=False); vk, kind, sfx = "rank_change", "rank", ""
elif mode.startswith("New"):
    g = g[pd.to_datetime(g["first_seen"]).dt.date >= today_utc() - timedelta(days=7)].sort_values("plays_window", ascending=False, na_position="last")
    vk, kind, sfx = "plays_window", "num", ""
elif mode.startswith("Best"):
    g = g.dropna(subset=["likes_per_k"]).sort_values("likes_per_k", ascending=False); vk, kind, sfx = "likes_per_k", "plain", " /1k"
elif g["plays_window"].notna().any():
    g = g.sort_values(["rank_in_source", "plays_window"], ascending=[True, False], na_position="last"); vk, kind, sfx = "plays_window", "plain", ""
else:  # platform without a play counter (Wavedash): rank by players online
    g = g.sort_values(["players_now", "ccu_peak_window"], ascending=False, na_position="last").dropna(subset=["players_now"]); vk, kind, sfx = "players_now", "plain", " playing"
g = g.head(100).reset_index(drop=True)
g["pos"] = range(1, len(g) + 1)

with st.container(border=True):
    ui.eyebrow("Leaderboard" if mode in ("Top 100", "Best liked") else "Trending today", "🏆" if mode in ("Top 100", "Best liked") else "🔥")
    ui.title({"Top 100": f"Top games on {row['display_name']}", "Movers": "Top moving games", "New (7 days)": "New and rising",
              "Best liked": "Best liked (≥1k plays)"}[mode])
    ui.ranked_list(ui.game_rows(g.head(25), vk, rank_key="pos" if mode == "Top 100" else "rank_in_source"), "value", kind, value_suffix=sfx)
    what = {"Top 100": f"value = plays in {f.window}" if vk == "plays_window" else "value = players online now · #rank = position in this list",
            "Movers": "value = places gained in the platform ranking since before the window",
            "New (7 days)": f"first seen within 7 days · value = plays in {f.window}",
            "Best liked": "value = likes per 1,000 lifetime plays (≥1,000 plays)"}[mode]
    tail = "" if (mode == "Top 100" and vk != "plays_window") else " · #rank = position by 7-day plays within the platform"
    st.markdown(f"<div class='vt-sub'>{what}{tail} · {label_for(f, src, 'plays', 'plays')}</div>", unsafe_allow_html=True)

with st.expander(f"Full table ({len(g)} games)"):
    disp = pd.DataFrame({
        "Rank": g["rank_in_source"].map(lambda v: "—" if pd.isna(v) else str(int(v))),
        "Δ": g["rank_change"].map(lambda v: "—" if pd.isna(v) else f"{int(v):+d}"),
        "Title": g["title"].fillna(g["url"]),
        "Creator": g["creator_name"].fillna("—"),
        "Lifetime plays": g["plays_total"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        f"Plays {f.window}": g["plays_window"].map(abbrev),
        "Plays / day": (g["plays_window"] / f.window_days).map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "Growth 7d": g["growth_7d"].map(pct),
        "Players now": g["players_now"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "Likes": g["likes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "Likes / 1k plays": g["likes_per_k"].map(ratio_fmt),
        "Remixes": g["remixes"].map(lambda v: "—" if pd.isna(v) else abbrev(v)),
        "First seen": g["first_seen"].map(fmt_dt),
    })
    ev = phone_table(disp, ["Rank", "Title", "Lifetime plays", f"Plays {f.window}", "Plays / day", "Likes / 1k plays"], key=f"platform_{src}_{mode}")
    sel = selected_row(ev, g)
    if sel is not None:
        nav("game", game=int(sel["id"]))
    st.caption(f"Δ = rank change vs the last day before the {f.window} window. Tap a row to open the game.")
