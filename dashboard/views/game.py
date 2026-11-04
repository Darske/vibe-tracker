"""Game page: header, three cards (plays, concurrent players with raw per-poll line, rank over time), placements history."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q, ui
from dashboard.components import badge, fmt_dt, metric_card
from dashboard.filters import go as nav, param, range_bar, sidebar
from dashboard.theme import abbrev, color_map, full, likes_per_k, line_mode, ratio_fmt, time_axis

f = sidebar()
ui.topnav("games")
gid = param("game")
if not gid:
    ui.page_header("Game")
    st.info("Open a game from the Games or Platform page.")
    st.stop()
g = q.game(int(gid))
if not g:
    st.error("Game not found.")
    st.stop()
color = color_map(list(f.sources["name"]))[g["source"]]
labels = g.get("metric_map") or {}

# ---------------------------------------------------------------- header
pub = g.get("published_at") or g.get("first_seen")
daily = q.game_daily(int(gid))
last = q.game_latest(int(gid)) or {}
lpk = likes_per_k(last.get("likes"), last.get("plays_total"))

hc1, hc2 = st.columns([1, 5])
with hc1:
    if g.get("thumbnail_url"):
        st.markdown(f"<img class='vt-art' src='{g['thumbnail_url']}'/>", unsafe_allow_html=True)
with hc2:
    ui.page_header(g.get("title") or g["url"],
                   f"{badge(g['display_name'], color)} &nbsp; by <b style='color:var(--ink)'>{g.get('creator_name') or '—'}</b> &nbsp;·&nbsp; "
                   f"published {fmt_dt(pub)} &nbsp;·&nbsp; <a class='vt-link' href='{g['url']}' target='_blank'>play ↗</a>",
                   eyebrow_text="Game", icon="🕹️")
    bits = [(abbrev(last.get("likes")), "likes") if last.get("likes") is not None else None,
            (ratio_fmt(lpk), "likes / 1k plays") if lpk is not None else None,
            (abbrev(last.get("remixes")), "remixes") if last.get("remixes") is not None else None,
            (abbrev(last.get("comments")), "comments") if last.get("comments") is not None else None,
            (abbrev(last.get("players_total")), "players") if last.get("players_total") is not None else None]
    if any(bits):
        ui.stats_line([b for b in bits if b])
    if g.get("ai_tool") or g.get("engine") or g.get("tags"):
        st.caption(" · ".join(x for x in [g.get("ai_tool"), g.get("engine"), ", ".join((g.get("tags") or [])[:6])] if x))
    if g.get("creator_id") and st.button("Open creator page →", type="tertiary"):
        nav("creator", creator=int(g["creator_id"]))
f = range_bar(f)
polls = q.game_polls(int(gid), f.window_days)

# ---------------------------------------------------------------- hero
rk = daily[["day", "rank_in_source"]].rename(columns={"rank_in_source": "value"})
has_ccu = daily["ccu_peak"].notna().any() or (not polls.empty and polls["players_now"].notna().any())
with st.container(border=True):
    a, b = st.columns(2)
    with a:
        if has_ccu:
            ui.eyebrow("Players right now", "🔥")
            ui.hero(full(last.get("players_now")), ui.ago(last.get("captured_at")))
        else:
            ui.eyebrow(f"Plays, {f.window}", "🔥")
            w = daily[pd.to_datetime(daily["day"]).dt.date >= f.window_start]
            ui.hero(full(w["plays_delta"].sum(min_count=1)), f"{abbrev(w['plays_delta'].mean())} per day on average")
    with b:
        if has_ccu:
            ui.eyebrow("All-time peak", "🏆")
            pk = daily.dropna(subset=["ccu_peak"]).sort_values("ccu_peak").tail(1)
            ui.hero(full(pk["ccu_peak"].iloc[0]) if not pk.empty else "—", pd.Timestamp(pk["day"].iloc[0]).strftime("%b %-d") + " (daily peak)" if not pk.empty else None, white=True)
        else:
            ui.eyebrow("Lifetime plays", "🏆")
            ui.hero(full(last.get("plays_total")), labels.get("plays", "the platform's own counter"), white=True)
    if has_ccu and not polls.empty and polls["players_now"].notna().any():
        pp = polls.dropna(subset=["players_now"])
        if len(pp) >= 2:
            fig = go.Figure(go.Scatter(x=pp["captured_at"], y=pp["players_now"], mode=line_mode(len(pp)), line=dict(width=2.5, color=color), marker=dict(size=4),
                                       hovertemplate="%{x|%b %-d %H:%M}: %{y}<extra></extra>"))
            fig.update_layout(height=240, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"), showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.caption("Not enough players-online readings in this range yet.")
        st.markdown(f"<div class='vt-sub'>Players online, every poll, last {f.window}</div>", unsafe_allow_html=True)
        if labels.get("players_now"):
            st.caption(f"Definition on {g['display_name']}: {labels['players_now']}")
    elif not has_ccu:
        w = daily[pd.to_datetime(daily["day"]).dt.date >= f.window_start]
        if len(w) >= 2:
            fig = go.Figure(go.Scatter(x=w["day"], y=w["plays_delta"], mode="lines+markers", line=dict(width=2.5, color=color), marker=dict(size=5),
                                       hovertemplate="%{x|%b %-d}: %{y:,.0f} plays<extra></extra>"))
            fig.update_layout(height=220, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"), showlegend=False)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.markdown("<div class='vt-sub'>Daily plays = day-over-day increase of the lifetime counter · this platform publishes no live player count</div>", unsafe_allow_html=True)
        if labels.get("plays"):
            st.caption(f"Definition on {g['display_name']}: {labels['plays']}")

# ---------------------------------------------------------------- cards
c1, c1b, c2, c3 = st.columns(4)
with c1:
    metric_card("Lifetime plays", daily[["day", "plays_total_eod"]].rename(columns={"plays_total_eod": "value"}), f.anchor,
                "cumulative", color, note="the platform's own counter")
with c1b:
    metric_card("Daily plays (measured)", daily[["day", "plays_delta"]].rename(columns={"plays_delta": "value"}), f.anchor,
                "daily-peak", color, note="day-over-day increase of the lifetime counter")
with c2:
    metric_card("Concurrent players (daily peak)", daily[["day", "ccu_peak"]].rename(columns={"ccu_peak": "value"}), f.anchor, "daily-peak", color,
                note="players online" if labels.get("players_now") else "no players-online counter on this platform",
                second=daily[["day", "ccu_avg"]].rename(columns={"ccu_avg": "value"}), second_label="average")
with c3:
    with st.container(border=True):
        ui.eyebrow(f"Rank in {g['display_name']}")
        if rk["value"].dropna().empty:
            st.markdown("<p class='vt-big'>—</p><div class='vt-sub'>no rank yet</div>", unsafe_allow_html=True)
        else:
            latest = int(rk.dropna().iloc[-1]["value"])
            ui.big(f"#{latest}")
            fig = go.Figure(go.Scatter(x=rk["day"], y=rk["value"], mode="lines+markers", line=dict(width=2, color=color), marker=dict(size=5),
                                       hovertemplate="%{x|%b %-d}: #%{y}<extra></extra>"))
            fig.update_layout(height=160, margin=dict(l=0, r=0, t=4, b=0), yaxis=dict(autorange="reversed", dtick=1, tickformat=",d"), xaxis=dict(tickformat="%b %-d", nticks=4))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            st.caption("by 7-day plays within the platform (1 = top)")

# ---------------------------------------------------------------- placements
ui.title("List placements")
pl = q.game_placements(int(gid))
if pl.empty:
    st.caption("Not seen in any tracked list yet.")
else:
    st.dataframe(pd.DataFrame({
        "List": pl["list_name"], "Best position": pl["best_position"], "Latest position": pl["latest_position"],
        "First seen": pl["first_seen"].map(fmt_dt), "Last seen": pl["last_seen"].map(fmt_dt), "Polls": pl["polls"],
    }), hide_index=True, width='stretch')

# ---------------------------------------------------------------- daily table
with st.expander("Daily figures"):
    d = daily.sort_values("day", ascending=False)
    st.dataframe(pd.DataFrame({
        "Day": d["day"], "Plays": d["plays_delta"].map(abbrev), "Total": d["plays_total_eod"].map(abbrev),
        "Peak players": d["ccu_peak"], "Avg players": d["ccu_avg"], "Rank": d["rank_in_source"], "7d plays": d["plays_7d"].map(abbrev),
        "Growth 7d": d["growth_7d"], "Gap days": d["gap_days"], "Reset": d["reset_flag"], "Polls": d["captures"],
    }), hide_index=True, width='stretch')
