"""Platform page → "AI games": how AI-made games are growing on a legacy platform (Kongregate, itch.io).

Each platform labels AI games its own way, so this view shows only what that platform supports (AI_SOURCES) and says
how the label is set. CrazyGames and Poki get a short explanation instead. Every legacy platform also shows "creators crossing over":
people who publish on a new-entrant (AI-native) platform under the same name.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q, ui
from dashboard.ai_metrics import (AI_SOURCES, NO_AI_LABEL, catalogue_ai, list_shares, new_ai_share, new_games_split, plays_split,
                                  ratings_split)
from dashboard.theme import ACCENT, INK, STATUS, abbrev, full, time_axis

KIND_COLORS = {"ai": ACCENT, "no_ai": STATUS["warning"], "undeclared": INK["axis"]}
KIND_NAMES = {"ai": "Declares AI", "no_ai": "Declares no AI", "undeclared": "Says nothing"}
CFG = {"displayModeBar": False}


def _pct(v, digits: int = 0) -> str:
    return "—" if v is None or pd.isna(v) else f"{v * 100:.{digits}f}%"


def _share_axis(fig, height: int = 260):
    fig.update_layout(height=height, margin=dict(t=36), legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"),
                      yaxis=dict(tickformat=".0%", rangemode="tozero"))


def render(src: str, name: str, f, today) -> None:
    cfg = AI_SOURCES.get(src)
    if cfg is None:
        st.info(NO_AI_LABEL.get(src, f"{name} does not label AI-made games, so they cannot be tracked separately here. "
                                     "On the new-entrant platforms every game is AI-made.")
                + " The closest signal is below: creators who also publish on AI-native platforms.")
    else:
        _labelled(src, name, f, today, cfg)
    _crossover(src, name, f)


def _labelled(src: str, name: str, f, today, cfg: dict) -> None:
    start = min(f.window_start, today - timedelta(days=28))
    days = (today - start).days + 1
    st.markdown(f"<div class='vt-sub'><b>How a game counts as AI-made on {name}:</b> {cfg['how']}</div>", unsafe_allow_html=True)

    plays = plays_split(q.ai_plays_daily(src, start)) if cfg["plays"] else pd.DataFrame()
    closed = plays[plays.index.date < today] if len(plays) else plays
    shares = list_shares(q.ai_list_share(src, start), cfg["lists"])
    cat = catalogue_ai(q.platform_totals((src,))) if cfg["catalogue"] else pd.DataFrame()
    newg = new_games_split(q.ai_new_games(src, today - timedelta(weeks=26))) if cfg["new_games"] else pd.DataFrame()

    # ---------------------------------------------------------------- tiles
    tiles = []
    if cfg["plays"]:
        last7 = closed.tail(7)
        tiles.append(("AI share of plays, last 7 days", _pct(last7["ai"].sum() / last7["total"].sum() if last7["total"].sum() else None, 1),
                      f"{abbrev(last7['ai'].sum())} of {abbrev(last7['total'].sum())} plays on tracked games" if len(last7) else "needs two days of readings"))
    if cfg["catalogue"] and len(cat.dropna(subset=["ai_total"])):
        c = cat.dropna(subset=["ai_total"]).iloc[-1]
        tiles.append(("AI-disclosed games", full(c["ai_total"]), f"{_pct(c['ai_share'], 1)} of {abbrev(c['games'])} HTML5 games"))
        if pd.notna(c["no_ai_total"]):
            tiles.append(("Tagged \"No AI\"", full(c["no_ai_total"]), f"{_pct(c['no_ai_total'] / c['games'], 1)} of the catalogue"))
    if len(shares):
        main = "All lists" if "All lists" in set(shares["list"]) else shares["list"].iloc[0]
        s = shares[shares["list"] == main].iloc[-1]
        tiles.append((f"AI share of {main.lower() if main == 'All lists' else main + ' top ' + str(round(s['slots']))}", _pct(s["share"]),
                      f"latest day, {pd.Timestamp(s['day']):%b %-d}"))
    if len(newg):
        recent = newg[newg.index >= pd.Timestamp(today - timedelta(weeks=4))]
        tiles.append(("New games declaring AI, last 4 weeks", f"{int(recent['ai'].sum())} of {int(recent['total'].sum())}",
                      _pct(recent["ai"].sum() / recent["total"].sum() if recent["total"].sum() else None) + " of new uploads"))
    if tiles:
        cols = st.columns(len(tiles))
        for col, t in zip(cols, tiles):
            with col:
                ui.tile(*t)

    # ---------------------------------------------------------------- plays by declaration
    if cfg["plays"]:
        with st.container(border=True):
            ui.eyebrow("Share of daily plays going to AI games", "📈")
            if len(closed) < 2:
                st.caption("Needs a few days of readings (daily plays need two polls a day apart).")
            else:
                roll = closed[["ai", "no_ai", "total"]].rolling(7, min_periods=3).sum()
                fig = go.Figure()
                fig.add_scatter(x=closed.index, y=roll["ai"] / roll["total"], name="Of all plays (7-day)", mode="lines",
                                line=dict(color=ACCENT, width=3), hovertemplate="%{x|%b %-d}: %{y:.1%}<extra></extra>")
                decl = roll["ai"] + roll["no_ai"]
                fig.add_scatter(x=closed.index, y=(roll["ai"] / decl).where(decl > 0), name="Of plays on games that declared (7-day)",
                                mode="lines", line=dict(color=INK["secondary"], width=2, dash="dot"),
                                hovertemplate="%{x|%b %-d}: %{y:.1%}<extra></extra>")
                _share_axis(fig)
                fig.update_layout(xaxis=time_axis(days))
                st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_play_share")
                fig = go.Figure()
                for k in ("ai", "no_ai", "undeclared"):
                    fig.add_bar(x=closed.index, y=closed[k], name=KIND_NAMES[k], marker_color=KIND_COLORS[k],
                                hovertemplate="%{x|%b %-d}: %{y:,.0f}<extra>" + KIND_NAMES[k] + "</extra>")
                fig.update_layout(barmode="stack", height=240, margin=dict(t=36), xaxis=time_axis(days),
                                  legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"), yaxis=dict(tickformat="~s"))
                st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_play_bars")
            st.caption("Daily plays of the games we track on this platform (its top lists and recent uploads), split by what each game declares. "
                       "Finished UTC days only.")

    # ---------------------------------------------------------------- catalogue
    if cfg["catalogue"]:
        with st.container(border=True):
            ui.eyebrow("AI-disclosed games in the catalogue", "🗂️")
            cd = cat.dropna(subset=["ai_total"])
            if len(cd) < 2:
                st.caption("Recorded once per scrape from now on; the line appears after the second day."
                           + (f" Today: {full(cd['ai_total'].iloc[-1])} AI-disclosed games ({_pct(cd['ai_share'].iloc[-1], 1)})." if len(cd) else ""))
            else:
                fig = go.Figure()
                fig.add_scatter(x=cd.index, y=cd["ai_total"], name="AI-disclosed games", mode="lines+markers",
                                line=dict(color=ACCENT, width=3), customdata=cd["ai_share"],
                                hovertemplate="%{x|%b %-d}: %{y:,.0f} games (%{customdata:.1%} of the catalogue)<extra></extra>")
                fig.update_layout(height=260, margin=dict(t=16), showlegend=False, yaxis=dict(tickformat="~s"))
                st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_catalogue")
                nd = cd["ai_new"].dropna()
                if len(nd):
                    fig = go.Figure(go.Bar(x=nd.index, y=nd, marker_color=[ACCENT if v >= 0 else STATUS["critical"] for v in nd],
                                           hovertemplate="%{x|%b %-d}: %{y:+,.0f}<extra></extra>"))
                    fig.update_layout(height=200, margin=dict(t=16), yaxis=dict(tickformat="~s"))
                    ui.eyebrow("Net new AI-disclosed games per day")
                    st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_catalogue_new")
                ns = new_ai_share(cat)
                if len(ns):
                    ui.eyebrow("Share of new games that are AI-disclosed (7-day)")
                    fig = go.Figure()
                    fig.add_scatter(x=ns.index, y=ns, name="Of new games", mode="lines+markers" if len(ns) < 4 else "lines",
                                    line=dict(color=ACCENT, width=3), hovertemplate="%{x|%b %-d}: %{y:.1%}<extra>of new games</extra>")
                    fig.add_scatter(x=cd.index, y=cd["ai_share"], name="Of the whole catalogue", mode="lines",
                                    line=dict(color=INK["muted"], width=2, dash="dot"), hovertemplate="%{x|%b %-d}: %{y:.1%}<extra>of catalogue</extra>")
                    _share_axis(fig, 240)
                    st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_new_share")
                    st.caption("New AI-disclosed games divided by all new HTML5 games over the last 7 days (net of removals). "
                               "Above the dotted line means AI is a bigger part of what is being added now than of the catalogue so far.")
            st.caption("Counts from itch.io's own browse pages (HTML5 games). Net change can be negative when itch.io removes AI games "
                       "(it purged AI spam on Nov 3, 2026) or creators change their disclosure.")

    # ---------------------------------------------------------------- top lists
    if len(shares):
        with st.container(border=True):
            ui.eyebrow("Share of top-list slots held by AI games", "🏆")
            fig = go.Figure()
            groups = list(shares.groupby("list", sort=False))
            groups = [x for x in groups if x[0] != "All lists"] + [x for x in groups if x[0] == "All lists"]  # main line on top
            for i, (lname, g) in enumerate(groups):
                main = lname == "All lists" or shares["list"].nunique() == 1
                fig.add_scatter(x=g["day"], y=g["share"], name=lname, mode="lines+markers" if len(g) < 3 else "lines",
                                line=dict(color=ACCENT if main else [INK["secondary"], INK["muted"], STATUS["warning"], STATUS["good"]][i % 4],
                                          width=3 if main else 1.5),
                                hovertemplate="%{x|%b %-d}: %{y:.0%}<extra>" + lname + "</extra>")
            _share_axis(fig)
            fig.update_layout(xaxis=time_axis(days))
            st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_list_share")
            st.caption("Average over the day's polls of the share of list positions taken by AI-declared games. "
                       + ("Only itch.io's Popular list is shown: every AI game in its top 100 also shows on the AI-assisted list we read, "
                          "so its count is complete; on other lists AI games outside that list would be missed." if src == "itchio" else
                          "A rising share means AI games are winning more of the platform's visibility."))

    # ---------------------------------------------------------------- new uploads
    if cfg["new_games"]:
        with st.container(border=True):
            ui.eyebrow("New games per week, by AI declaration", "🚀")
            if newg.empty:
                st.caption("No new games recorded yet.")
            else:
                fig = go.Figure()
                for k in ("ai", "no_ai", "undeclared"):
                    fig.add_bar(x=newg.index, y=newg[k], name=KIND_NAMES[k], marker_color=KIND_COLORS[k],
                                customdata=newg["ai_share"],
                                hovertemplate="week of %{x|%b %-d}: %{y} (AI share %{customdata:.0%})<extra>" + KIND_NAMES[k] + "</extra>")
                fig.update_layout(barmode="stack", height=280, margin=dict(t=36), legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"))
                st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_new_games")
            st.caption("Games we have seen, dated by their publish date (the starting catalogue on our first day is left out). "
                       "The latest week is still in progress.")

    # ---------------------------------------------------------------- ratings
    if cfg.get("ratings"):
        rt = ratings_split(q.ai_ratings(src, start))
        with st.container(border=True):
            ui.eyebrow("Player ratings: AI games vs the rest", "⭐")
            if rt.empty:
                st.caption("Ratings are stored from Nov 11, 2026; the comparison appears after the next scrapes.")
            else:
                last = rt.iloc[-1]
                cols = st.columns(3)
                for col, k in zip(cols, ("ai", "no_ai", "undeclared")):
                    with col:
                        v, n = last.get(f"rating_{k}"), last.get(f"games_{k}")
                        ui.tile(KIND_NAMES[k], "—" if v is None or pd.isna(v) else f"{v:.2f} ★",
                                None if n is None or pd.isna(n) else f"average over {int(n)} games")
                if len(rt) >= 2:
                    fig = go.Figure()
                    for k in ("ai", "no_ai", "undeclared"):
                        if f"rating_{k}" in rt:
                            fig.add_scatter(x=rt.index, y=rt[f"rating_{k}"], name=KIND_NAMES[k], mode="lines",
                                            line=dict(color=KIND_COLORS[k] if k != "undeclared" else INK["secondary"], width=3 if k == "ai" else 2),
                                            hovertemplate="%{x|%b %-d}: %{y:.2f} ★<extra>" + KIND_NAMES[k] + "</extra>")
                    fig.update_layout(height=240, margin=dict(t=36), xaxis=time_axis(days),
                                      legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"), yaxis=dict(tickformat=".1f"))
                    st.plotly_chart(fig, use_container_width=True, config=CFG, key="ai_ratings")
            st.caption("Average star rating of tracked games with at least 10 ratings (each game counts once). "
                       "Games still \"under judgement\" have no public average and are left out. A closing gap means AI games are catching up on quality.")

    # ---------------------------------------------------------------- top AI games
    with st.container(border=True):
        ui.eyebrow(f"Top AI games on {name}", "🔥")
        if cfg["plays"]:
            g = q.games_window(f.window_start, (src,))
            g = g[g["ai_disclosure"] == "ai"].sort_values("plays_window", ascending=False, na_position="last").head(10)
            rows = [{"title": r["title"], "href": f"game?game={r['id']}", "thumb": r["thumbnail_url"], "value": r["plays_window"],
                     "creator": r["creator_name"] if pd.notna(r["creator_name"]) else None, "rank": i + 1} for i, (_, r) in enumerate(g.iterrows())]
            if rows:
                ui.ranked_list(rows, "value", "plain")
                st.caption(f"By plays, {f.window}.")
        else:
            g = q.ai_top_listed(src, (cfg["lists"] or ["popular"])[0])
            rows = [{"title": r["title"], "href": f"game?game={r['id']}", "thumb": r["thumbnail_url"], "value": f"Popular #{r['position']}",
                     "rank": None} for i, (_, r) in enumerate(g.iterrows())]
            if rows:
                ui.ranked_list(rows, "value", "plain")
                st.caption("Position on the Popular list at the latest poll.")
        if not rows:
            st.caption("No AI-declared games recorded yet.")


def _crossover(src: str, name: str, f) -> None:
    """Creators on this legacy platform who also publish on a new-entrant (AI-native) platform under the same name."""
    cx = q.creator_crossover(src, f.window_start)
    with st.container(border=True):
        ui.eyebrow("Creators crossing over from AI-native platforms", "🔀")
        if cx.empty:
            st.caption(f"No {name} creator matches a creator on the AI-native platforms yet.")
        else:
            plays = cx["plays_window"].fillna(0).sum()
            all_plays = q.games_window(f.window_start, (src,))["plays_window"].fillna(0).sum()
            cols = st.columns(3)
            with cols[0]:
                ui.tile("Crossover creators", full(len(cx)), f"with {full(cx['games'].sum())} games on {name}")
            with cols[1]:
                ui.tile(f"Their plays, {f.window}", abbrev(plays) if plays else "—",
                        (f"{plays / all_plays * 100:.1f}% of tracked plays" if all_plays and plays else "no play counter" if not all_plays else None))
            with cols[2]:
                ui.tile("Also on", ", ".join(sorted({p for x in cx["platforms"] for p in str(x).split(", ")})) or "—")
            tbl = pd.DataFrame({
                "Creator": cx["name"], "Also publishes on": cx["platforms"], "Games there": cx["ne_games"],
                f"Games on {name}": cx["games"], f"Plays {f.window}": cx["plays_window"].map(abbrev),
                "Top game here": cx["top_game"], "First seen here": pd.to_datetime(cx["first_seen"]).dt.strftime("%b %-d, %Y"),
            })
            st.dataframe(tbl.head(50), hide_index=True, width="stretch")
        st.caption("Matched by creator name, so a namesake can slip in; check the list. This works even where the platform has no AI label "
                   "(CrazyGames, Poki): it shows AI-native creators bringing their games to established portals.")
