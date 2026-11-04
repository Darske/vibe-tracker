"""Platforms (home): ranked table with a metric selector, two indexed line charts, share donut by segment."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q
from dashboard.components import phone_table, selected_row, share_donut
from dashboard.filters import go as nav, label_for, sidebar, today_utc
from dashboard.theme import INK, abbrev, color_map, pct, ratio_fmt

f = sidebar()
st.title("Platforms")
st.caption("Which platform is winning. Every figure covers the **tracked top lists** of each platform (≈top 100 per list), not the whole platform.")

pdaily = q.platform_daily()
now = q.platform_now()
if pdaily.empty:
    st.info("No derived data yet — the first daily figures appear after the poller has run across two days.")
    st.stop()
pdaily = pdaily[pdaily["source"].isin(f.selected)]
colors = color_map(list(f.sources["name"]))
names = f.names

# ---------------------------------------------------------------- ranked table
METRICS = {
    "Daily plays (top 100)": "plays_delta_top100",
    "Peak concurrent players (top 100)": "ccu_peak_top100",
    "Average concurrent players (top 100)": "ccu_avg_top100",
    "New games / day": "games_new",
    "Active creators": "creators_active",
    "7-day growth": "growth_7d",
    "Likes per 1,000 plays (pooled)": "likes_per_k",
    "Likes per 1,000 plays (median game)": "median_likes_per_k",
}
metric = st.selectbox("Rank platforms by", list(METRICS), index=0)
col = METRICS[metric]

wstart = f.window_start
win = pdaily[pdaily["day"] >= wstart]
rows = []
for src, g in pdaily.groupby("source"):
    g = g.sort_values("day")
    gw = win[win["source"] == src]
    last7 = g[g["day"] > today_utc() - pd.Timedelta(days=7)]
    prev7 = g[(g["day"] <= today_utc() - pd.Timedelta(days=7)) & (g["day"] > today_utc() - pd.Timedelta(days=14))]
    p7, pp7 = last7["plays_delta_top100"].sum(min_count=1), prev7["plays_delta_top100"].sum(min_count=1)
    growth = (p7 / pp7 - 1) if (pd.notna(p7) and pd.notna(pp7) and pp7 >= 100) else None
    latest = g.iloc[-1]
    nrow = now[now["source"] == src]
    days_in_win = max(1, gw["plays_delta_top100"].notna().sum())
    lk = float(nrow["likes_lifetime"].iloc[0]) if not nrow.empty and pd.notna(nrow["likes_lifetime"].iloc[0]) else None
    pk = float(nrow["plays_with_likes"].iloc[0]) if not nrow.empty and pd.notna(nrow["plays_with_likes"].iloc[0]) else None
    rows.append({
        "likes_per_k": (lk * 1000.0 / pk) if lk is not None and pk else None,
        "median_likes_per_k": float(nrow["median_likes_per_k"].iloc[0]) if not nrow.empty and pd.notna(nrow["median_likes_per_k"].iloc[0]) else None,
        "games_with_ratio": int(nrow["games_with_ratio"].iloc[0]) if not nrow.empty and pd.notna(nrow["games_with_ratio"].iloc[0]) else 0,
        "plays_lifetime": float(nrow["plays_lifetime"].iloc[0]) if not nrow.empty and pd.notna(nrow["plays_lifetime"].iloc[0]) else None,
        "plays_per_day": (gw["plays_delta_top100"].sum(min_count=1) / days_in_win) if pd.notna(gw["plays_delta_top100"].sum(min_count=1)) else None,
        "source": src, "Platform": names.get(src, src), "Segment": latest["segment"],
        "plays_delta_top100": gw["plays_delta_top100"].sum(min_count=1),
        "ccu_peak_top100": gw["ccu_peak_top100"].max(), "ccu_avg_top100": gw["ccu_avg_top100"].mean(),
        "games_new": gw["games_new"].mean(), "creators_active": latest["creators_active"], "growth_7d": growth,
        "Games tracked": latest["games_tracked"], "Top-10 share": latest["top10_share"],
        "Label": label_for(f, src, "players_now" if "concurrent" in metric else "plays", ""),
    })
table = pd.DataFrame(rows)
if table.empty:
    st.info("No platforms selected.")
    st.stop()
table = table.sort_values(col, ascending=False, na_position="last").reset_index(drop=True)
table.insert(0, "#", range(1, len(table) + 1))
disp = table.rename(columns={
    "plays_lifetime": "Lifetime plays", "plays_per_day": "Plays / day",
    "likes_per_k": "Likes / 1k plays", "median_likes_per_k": "Median likes / 1k", "games_with_ratio": "Games ≥1k plays",
    "plays_delta_top100": f"Plays {f.window}", "ccu_peak_top100": f"Peak players {f.window}", "ccu_avg_top100": "Avg players",
    "games_new": "New games/day", "creators_active": "Active creators", "growth_7d": "Growth 7d",
})
disp[f"Plays {f.window}"] = disp[f"Plays {f.window}"].map(abbrev)
disp["Lifetime plays"] = disp["Lifetime plays"].map(abbrev)
disp["Likes / 1k plays"] = disp["Likes / 1k plays"].map(ratio_fmt)
disp["Median likes / 1k"] = disp["Median likes / 1k"].map(ratio_fmt)
disp["Plays / day"] = disp["Plays / day"].map(abbrev)
disp[f"Peak players {f.window}"] = disp[f"Peak players {f.window}"].map(abbrev)
disp["Avg players"] = disp["Avg players"].map(lambda v: "—" if pd.isna(v) else f"{v:.0f}")
disp["New games/day"] = disp["New games/day"].map(lambda v: "—" if pd.isna(v) else f"{v:.0f}")
disp["Growth 7d"] = disp["Growth 7d"].map(pct)
disp["Top-10 share"] = disp["Top-10 share"].map(lambda v: "—" if pd.isna(v) else f"{float(v) * 100:.0f}%")
_mcol = {"likes_per_k": "Likes / 1k plays", "median_likes_per_k": "Median likes / 1k"}
phone_cols = ["#", "Platform", "Lifetime plays", "Plays / day", "Likes / 1k plays", {"plays_delta_top100": f"Plays {f.window}", "ccu_peak_top100": f"Peak players {f.window}", "ccu_avg_top100": "Avg players", "games_new": "New games/day", "creators_active": "Active creators", "growth_7d": "Growth 7d", **_mcol}[col], "Games tracked", "Label"]
phone_cols = list(dict.fromkeys(phone_cols))
ev = phone_table(disp.drop(columns=["source"]), phone_cols, key="platforms")
sel = selected_row(ev, table)
if sel is not None:
    nav("platform", platform=sel["source"])
st.caption("Lifetime plays = the platform's own cumulative counter summed over the games we track; Plays / day = our measured daily increase "
           "(average over the window). Likes / 1k plays = all likes ÷ all lifetime plays × 1000 over tracked games (pooled, so big games weigh more); "
           "the median version is the typical game with ≥1,000 plays. Each platform's 'like' is its own button (favorites, thumbs-up, hearts, ratings) — "
           "compare trends and ranks, not absolute values. Tap a row to open the platform.")

# ---------------------------------------------------------------- time series
st.subheader("Over time")
indexed = st.toggle("Indexed (=100 at anchor date)", value=False, help="Raw counts are not comparable across platforms; indexing shows relative movement.")


def line_chart(column: str, title: str, y_fmt: str = "~s"):
    fig = go.Figure()
    for src, g in pdaily.groupby("source", sort=False):
        g = g.sort_values("day").dropna(subset=[column])
        if g.empty:
            continue
        y = g[column].astype(float)
        if indexed:
            base_rows = g[g["day"] <= f.anchor]
            base = float(base_rows[column].iloc[-1]) if not base_rows.empty else float(y.iloc[0])
            if not base:
                continue
            y = y / base * 100
        fig.add_trace(go.Scatter(x=g["day"], y=y, mode="lines+markers", name=names.get(src, src),
                                 line=dict(width=2, color=colors.get(src, INK["muted"])), marker=dict(size=5),
                                 hovertemplate="%{x|%b %-d}: %{y:,.0f}<extra>%{fullData.name}</extra>"))
    fig.add_vline(x=f.anchor, line=dict(color=INK["axis"], width=1, dash="dot"))
    fig.update_layout(height=320, yaxis=dict(tickformat=y_fmt if not indexed else ",.0f"))
    st.markdown(f"**{title}**")
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


line_chart("plays_delta_top100", "Daily plays per platform (tracked top 100)")
line_chart("ccu_peak_top100", "Peak concurrent players per platform (tracked top 100)")
st.caption("One line per platform; colours are fixed per platform on every page. Figures cover the tracked top 100 per platform, not the whole platform.")

# ---------------------------------------------------------------- share donut
st.subheader("Share of tracked daily plays")
by_seg = {}
for seg, g in win.groupby("segment"):
    for src, gg in g.groupby("source"):
        v = gg["plays_delta_top100"].sum(min_count=1)
        if pd.notna(v) and v > 0:
            by_seg[f"{names.get(src, src)} ({'A' if seg == 'aggregator' else 'B'})"] = float(v)
share_donut(f"Tracked plays by platform, last {f.window} (A = aggregator, B = creation)",
            by_seg, colors={f"{names.get(s, s)} ({'A' if seg == 'aggregator' else 'B'})": colors[s] for s, seg in zip(f.sources["name"], f.sources["segment"])},
            note="Platforms that publish no play counter (placements/CCU only) are absent by design.")
