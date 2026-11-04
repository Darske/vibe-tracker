"""Game page: header, three cards (plays, concurrent players with raw per-poll line, rank over time), placements history."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q
from dashboard.components import badge, fmt_dt, metric_card
from dashboard.filters import go as nav, sidebar
from dashboard.theme import INK, abbrev, color_map

f = sidebar()
gid = st.query_params.get("game")
if not gid:
    st.title("Game")
    st.info("Open a game from the Games or Platform page.")
    st.stop()
g = q.game(int(gid))
if not g:
    st.error("Game not found.")
    st.stop()
color = color_map(list(f.sources["name"]))[g["source"]]
labels = g.get("metric_map") or {}

# ---------------------------------------------------------------- header
st.title(g.get("title") or g["url"])
pub = g.get("published_at") or g.get("first_seen")
st.markdown(
    f"{badge(g['display_name'], color)} &nbsp; by **{g.get('creator_name') or '—'}** &nbsp;·&nbsp; "
    f"published {fmt_dt(pub)} &nbsp;·&nbsp; [play ↗]({g['url']})", unsafe_allow_html=True)
if g.get("creator_id") and st.button("Open creator page"):
    nav("creator", creator=int(g["creator_id"]))
if g.get("ai_tool") or g.get("engine") or g.get("tags"):
    st.caption(" · ".join(x for x in [g.get("ai_tool"), g.get("engine"), ", ".join((g.get("tags") or [])[:6])] if x))

daily = q.game_daily(int(gid))
polls = q.game_polls(int(gid), 7)

# ---------------------------------------------------------------- cards
c1, c1b, c2, c3 = st.columns(4)
with c1:
    metric_card("Lifetime plays", daily[["day", "plays_total_eod"]].rename(columns={"plays_total_eod": "value"}), f.anchor,
                "cumulative", color, note=labels.get("plays", ""))
with c1b:
    metric_card("Daily plays (measured)", daily[["day", "plays_delta"]].rename(columns={"plays_delta": "value"}), f.anchor,
                "daily-peak", color, note="day-over-day increase of the lifetime counter")
with c2:
    metric_card("Concurrent players (daily peak)", daily[["day", "ccu_peak"]].rename(columns={"ccu_peak": "value"}), f.anchor, "daily-peak", color,
                note=labels.get("players_now", "no players-online counter on this platform"),
                second=daily[["day", "ccu_avg"]].rename(columns={"ccu_avg": "value"}), second_label="average")
with c3:
    rk = daily[["day", "rank_in_source"]].rename(columns={"rank_in_source": "value"})
    with st.container(border=True):
        st.markdown(f"**Rank in {g['display_name']}**")
        if rk["value"].dropna().empty:
            st.caption("no rank yet")
        else:
            latest = int(rk.dropna().iloc[-1]["value"])
            st.markdown(f"<span style='font-size:2rem;font-weight:600'>#{latest}</span>", unsafe_allow_html=True)
            fig = go.Figure(go.Scatter(x=rk["day"], y=rk["value"], mode="lines+markers", line=dict(width=2, color=color), marker=dict(size=5),
                                       hovertemplate="%{x|%b %-d}: #%{y}<extra></extra>"))
            fig.update_layout(height=160, margin=dict(l=0, r=0, t=4, b=0), yaxis=dict(autorange="reversed", nticks=3), xaxis=dict(tickformat="%b %-d", nticks=4))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            st.caption("by 7-day plays within the platform (1 = top)")

# ---------------------------------------------------------------- raw per-poll CCU
if not polls.empty and polls["players_now"].notna().any():
    st.subheader("Players online, every poll (last 7 days)")
    fig = go.Figure(go.Scatter(x=polls["captured_at"], y=polls["players_now"], mode="lines+markers", line=dict(width=2, color=color), marker=dict(size=4),
                               hovertemplate="%{x|%b %-d %H:%M}: %{y}<extra></extra>"))
    fig.update_layout(height=220)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

# ---------------------------------------------------------------- placements
st.subheader("List placements")
pl = q.game_placements(int(gid))
if pl.empty:
    st.caption("Not seen in any tracked list yet.")
else:
    st.dataframe(pd.DataFrame({
        "List": pl["list_name"], "Best position": pl["best_position"], "Latest position": pl["latest_position"],
        "First seen": pl["first_seen"].map(fmt_dt), "Last seen": pl["last_seen"].map(fmt_dt), "Polls": pl["polls"],
    }), hide_index=True, use_container_width=True)

# ---------------------------------------------------------------- daily table
with st.expander("Daily figures"):
    d = daily.sort_values("day", ascending=False)
    st.dataframe(pd.DataFrame({
        "Day": d["day"], "Plays": d["plays_delta"].map(abbrev), "Total": d["plays_total_eod"].map(abbrev),
        "Peak players": d["ccu_peak"], "Avg players": d["ccu_avg"], "Rank": d["rank_in_source"], "7d plays": d["plays_7d"].map(abbrev),
        "Growth 7d": d["growth_7d"], "Gap days": d["gap_days"], "Reset": d["reset_flag"], "Polls": d["captures"],
    }), hide_index=True, use_container_width=True)
