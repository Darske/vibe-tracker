"""Home — rotrends-style: hero (players right now + all-time peak + CCU chart), "top right now" cards,
stat tiles, trending panels, then the ranked platform table, indexed charts and the share donut."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q
from dashboard import ui
from dashboard.indexing import composite, platform_indexes, rebase, smooth
from dashboard.components import phone_table, selected_row, share_donut
from dashboard.filters import go as nav, label_for, range_bar, sidebar, today_utc
from dashboard.theme import ACCENT, GAME_TONES, INK, abbrev, add_daily, closed_days, color_map, full, pct, ratio_fmt, time_axis

f = sidebar()
ui.topnav("platforms")
ui.page_header("Vibe-coded games", "Daily plays, live players and who is breaking out on AI-native game platforms (new entrants) "
               "and on established web game portals (legacy).")
f = range_bar(f)
days = f.window_days
colors = color_map(list(f.sources["name"]))
names = f.names

pdaily = q.platform_daily()
now = q.platform_now()
pdaily = pdaily[pdaily["source"].isin(f.selected)]
nowf = now[now["source"].isin(f.selected)]

# ================================================================== metric toggle: daily plays (default) | players online
mcol, _ = st.columns([2, 3])
with mcol:
    metric = ui.pills("Metric", ["Daily plays", "Players online"], "home_metric", "Daily plays")
daily_mode = metric == "Daily plays"
win = pdaily[pdaily["day"] >= f.window_start]
latest_day = q.latest_play_day(f.selected)
if latest_day is not None and latest_day >= today_utc():  # today is partial: prefer the last closed day when it exists
    closed = pdaily[(pdaily["day"] < today_utc()) & pdaily["plays_delta_top100"].notna()]["day"]
    if not closed.empty:
        latest_day = closed.max()


def plat_link(src: str) -> str:
    return f"<a class='vt-link' href='platform?platform={src}' target='_self'>{names.get(src, src)}</a>"


# Which selected platforms publish a live player count (and so appear in the players-online figures)
_ccu_srcs = set(pdaily.dropna(subset=["ccu_peak_top100"])["source"])
_with_ccu = [names.get(x, x) for x in f.sources["name"] if x in f.selected and x in _ccu_srcs]
_without_ccu = [names.get(x, x) for x in f.sources["name"] if x in f.selected and x not in _ccu_srcs]
ccu_note = ((f"Included: {', '.join(_with_ccu)}" + (" (Wavedash counts only its ~12 trending games)" if "Wavedash" in _with_ccu else "") + ". ")
            if _with_ccu else "") + (f"Not included, because they publish no live player count: {', '.join(_without_ccu)}." if _without_ccu else "")

# ================================================================== hero
if daily_mode:
    per_day = win.groupby("day")["plays_delta_top100"].sum(min_count=1).dropna().sort_index()
    all_days = pdaily.groupby("day")["plays_delta_top100"].sum(min_count=1).dropna()
    with st.container(border=True):
        c1, c2 = st.columns(2)
        with c1:
            ui.eyebrow("Daily plays", "🔥")
            if latest_day is not None and latest_day in per_day.index:
                ui.hero(full(per_day.loc[latest_day]), f"on {pd.Timestamp(latest_day):%b %-d} (UTC) · tracked top lists of the selected platforms")
            else:
                ui.hero("—", "no measured day yet")
        with c2:
            ui.eyebrow("Best day", "🏆")
            if not all_days.empty:
                ui.hero(full(all_days.max()), f"{pd.Timestamp(all_days.idxmax()):%b %-d}", white=True)
            else:
                ui.hero("—", None, white=True)
        if len(per_day) < 2:
            st.caption("Not enough days of history in this range yet.")
        else:
            fig = add_daily(go.Figure(), per_day.index, per_day.values, ACCENT)
            fig.update_layout(height=300, margin=dict(t=16), xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("Daily plays = the day-over-day increase of each platform's own play counter, summed over the tracked top 100 per platform. "
                   "Headline figures use the last finished UTC day; the dotted end of each line is today so far.")

    # ---------------------------------------------------------- activity index (comparable across platforms)
    hist = closed_days(pdaily.dropna(subset=["plays_delta_top100"]))
    hist = hist.rename(columns={"plays_delta_top100": "plays"})[["day", "source", "plays"]]
    sm = smooth(hist)
    idx_start = min(f.window_start, today_utc() - timedelta(days=28))
    with st.container(border=True):
        h1, h2 = st.columns([3, 2])
        with h1:
            ui.eyebrow("Activity index (=100 at start)", "📈")
        with h2:
            per_plat = st.toggle("Show each platform", value=False, key="idx_per_platform")
        comp = rebase(composite(sm)[["volume", "momentum"]], idx_start) if not sm.empty else pd.DataFrame()
        if comp.empty or len(comp.dropna(how="all")) < 2:
            st.caption("Needs about a week of finished days before the index can be drawn.")
        else:
            fig = go.Figure()
            last_v, last_m = comp["volume"].dropna(), comp["momentum"].dropna()
            fig.add_scatter(x=comp.index, y=comp["volume"], name="Volume", mode="lines", line=dict(color=ACCENT, width=3),
                            hovertemplate="%{x|%b %-d}: %{y:.0f}<extra>Volume</extra>")
            fig.add_scatter(x=comp.index, y=comp["momentum"], name="Momentum", mode="lines", line=dict(color=INK["muted"], width=3, dash="dot"),
                            hovertemplate="%{x|%b %-d}: %{y:.0f}<extra>Momentum</extra>")
            if per_plat:
                pi = rebase(platform_indexes(sm), idx_start)
                for src in [s_ for s_ in f.sources["name"] if s_ in pi.columns]:
                    fig.add_scatter(x=pi.index, y=pi[src], name=names.get(src, src), mode="lines",
                                    line=dict(color=colors.get(src), width=1.2), opacity=0.7,
                                    hovertemplate="%{x|%b %-d}: %{y:.0f}<extra>" + names.get(src, src) + "</extra>")
            fig.add_hline(y=100, line=dict(color=INK["muted"], width=1, dash="dash"))
            fig.update_layout(height=300, margin=dict(t=36), legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"), yaxis=dict(tickformat=",.0f"))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key="activity_index")
            if len(last_v) and len(last_m):
                st.markdown(f"<div class='vt-sub'>Latest: volume <b>{last_v.iloc[-1]:.0f}</b> · momentum <b>{last_m.iloc[-1]:.0f}</b> "
                            f"(since {pd.Timestamp(comp.index[0]):%b %-d})</div>", unsafe_allow_html=True)
        _in = [names.get(x, x) for x in f.sources["name"] if x in f.selected and x in set(hist["source"])]
        _out = [names.get(x, x) for x in f.sources["name"] if x in f.selected and x not in set(hist["source"])]
        st.caption("Each platform is compared only with itself: its daily plays (7-day average) are set to 100 at the start, so platforms that "
                   "count plays differently can be combined. Volume = total plays across platforms (big platforms weigh more). "
                   "Momentum = the typical platform's change (every platform weighs the same). Platforms joining later are linked in without a jump. "
                   + (f"Included: {', '.join(_in)}. " if _in else "")
                   + (f"Not included, because they publish no play counter: {', '.join(_out)}." if _out else ""))
else:
    ccu = q.ccu_now(f.selected)
    peaks_all = pdaily.dropna(subset=["ccu_peak_top100"])
    peak_by_day = peaks_all.groupby("day")["ccu_peak_top100"].sum().sort_index()
    peak_win = peak_by_day[peak_by_day.index >= f.window_start]
    with st.container(border=True):
        c1, c2 = st.columns(2)
        with c1:
            ui.eyebrow("Players right now", "🔥")
            ui.hero(full(ccu.get("players_now")), ui.ago(ccu.get("updated_at")))
        with c2:
            ui.eyebrow("Best day (peak players)", "🏆")
            if len(peak_by_day):
                ui.hero(full(peak_by_day.max()), f"{pd.Timestamp(peak_by_day.idxmax()):%b %-d}", white=True)
            else:
                ui.hero("—", None, white=True)
        if len(peak_win) < 2:
            st.caption("Not enough days in this range yet.")
        else:
            fig = add_daily(go.Figure(), peak_win.index, peak_win.values, ACCENT, unit="players (daily peak)")
            fig.update_layout(height=300, margin=dict(t=16), xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        st.caption("One point per day: the peak number of players online at once that day, added up across platforms "
                   "(each platform's own peak, over its tracked top 100). The dotted end is today so far. " + ccu_note)

# ================================================================== top game / top platform
c1, c2 = st.columns(2)
with c1, st.container(border=True):
    palette = GAME_TONES
    if daily_mode:
        ui.eyebrow("Top game by daily plays", "🏆")
        top_games = q.top_games_day(f.selected, latest_day, 5) if latest_day is not None else pd.DataFrame()
        if top_games.empty:
            st.caption("No measured day yet.")
        else:
            g0 = top_games.iloc[0]
            ui.hero(str(g0["title"] or g0["url"]), f"{full(g0['plays_delta'])} plays on {pd.Timestamp(latest_day):%b %-d} · {g0['display_name']}")
            ids = tuple(int(i) for i in top_games["id"])
            gs = q.game_daily_series(ids, f.window_start)
            ui.legend_dots([(str(t)[:22], palette[i]) for i, t in enumerate(top_games["title"])])
            if gs["day"].nunique() < 2:
                st.caption("Not enough days in this range to draw a line yet.")
            else:
                fig = go.Figure()
                for i, (_, row) in enumerate(top_games.iterrows()):
                    s_ = gs[gs["game_id"] == row["id"]].dropna(subset=["plays_delta"])
                    add_daily(fig, s_["day"], s_["plays_delta"], palette[i], str(row["title"])[:30], width=2.4 if i == 0 else 1.6, marker=4)
                fig.update_layout(height=240, showlegend=False, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            if st.button("Top games →", key="to_games", type="tertiary"):
                nav("games", by="Plays")
    else:
        ui.eyebrow("Top game right now", "🏆")
        top_games = q.top_games_now(f.selected, 5)
        if top_games.empty:
            st.caption("No live counts yet.")
        else:
            g0 = top_games.iloc[0]
            ui.hero(str(g0["title"] or g0["url"]), f"{full(g0['players_now'])} players ({abbrev(g0['peak'])} peak) · {g0['display_name']}")
            ids = tuple(int(i) for i in top_games["id"])
            gs = q.game_daily_peaks(ids, f.window_start)
            ui.legend_dots([(str(t)[:22], palette[i]) for i, t in enumerate(top_games["title"])])
            if gs["day"].nunique() < 2:
                st.caption("Not enough days in this range to draw a line yet.")
            else:
                fig = go.Figure()
                for i, (_, row) in enumerate(top_games.iterrows()):
                    s_ = gs[gs["game_id"] == row["id"]]
                    add_daily(fig, s_["day"], s_["ccu_peak"], palette[i], str(row["title"])[:30], width=2.4 if i == 0 else 1.6,
                              unit="players (daily peak)", marker=4)
                fig.update_layout(height=240, showlegend=False, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            st.caption("Lines: each game's daily peak of players online.")
            if st.button("Top games →", key="to_games", type="tertiary"):
                nav("games", by="Players")

with c2, st.container(border=True):
    if daily_mode:
        ui.eyebrow("Top platform by daily plays", "🏆")
        today_rows = pdaily[pdaily["day"] == latest_day].dropna(subset=["plays_delta_top100"]).sort_values("plays_delta_top100", ascending=False) if latest_day is not None else pd.DataFrame()
        if today_rows.empty:
            st.caption("No measured day yet.")
        else:
            p0 = today_rows.iloc[0]
            st.markdown(f"<p class='vt-hero'>{plat_link(p0['source'])}</p><div class='vt-sub'>{full(p0['plays_delta_top100'])} plays on {pd.Timestamp(latest_day):%b %-d} across its tracked top 100</div>", unsafe_allow_html=True)
            order = [s_ for s_ in f.sources["name"] if s_ in set(win.dropna(subset=["plays_delta_top100"])["source"])]
            ui.legend_dots([(names.get(s_, s_), colors[s_]) for s_ in order])
            if win["day"].nunique() < 2:
                st.caption("Not enough days in this range to draw a line yet.")
            else:
                fig = go.Figure()
                for s_ in order:
                    d = win[win["source"] == s_].dropna(subset=["plays_delta_top100"]).sort_values("day")
                    add_daily(fig, d["day"], d["plays_delta_top100"], colors[s_], names.get(s_, s_), width=2, marker=4)
                fig.update_layout(height=240, showlegend=False, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            ui.ranked_list([{"title": names.get(r["source"], r["source"]), "href": f"platform?platform={r['source']}", "value": r["plays_delta_top100"],
                             "sub": f"{int(r['games_tracked']) if pd.notna(r['games_tracked']) else 0} games tracked", "rank": i + 1} for i, (_, r) in enumerate(today_rows.iterrows())],
                            "value", "plain", show_thumb=False)
            st.caption("Tap a platform to see its top games by total plays. Playgama and Wavedash publish no play counter.")
    else:
        ui.eyebrow("Top platform right now", "🏆")
        latest = q.ccu_latest_by_platform(f.selected).sort_values("players", ascending=False)
        if latest.empty:
            st.caption("No live counts yet.")
        else:
            p0 = latest.iloc[0]
            st.markdown(f"<p class='vt-hero'>{plat_link(p0['source'])}</p><div class='vt-sub'>{full(p0['players'])} players online across its tracked games</div>", unsafe_allow_html=True)
            pw = win.dropna(subset=["ccu_peak_top100"])
            order = [s_ for s_ in f.sources["name"] if s_ in set(pw["source"])]
            ui.legend_dots([(names.get(s_, s_), colors[s_]) for s_ in order])
            if pw["day"].nunique() >= 2:
                fig = go.Figure()
                for s_ in order:
                    d = pw[pw["source"] == s_].sort_values("day")
                    add_daily(fig, d["day"], d["ccu_peak_top100"], colors[s_], names.get(s_, s_), width=2, unit="players (daily peak)", marker=4)
                fig.update_layout(height=240, showlegend=False, xaxis=time_axis(f.window_days), yaxis=dict(rangemode="tozero"))
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
            else:
                st.caption("Not enough days in this range to draw a line yet.")
            ui.ranked_list([{"title": names.get(r["source"], r["source"]), "href": f"platform?platform={r['source']}", "value": r["players"],
                             "sub": "players online now", "rank": i + 1} for i, (_, r) in enumerate(latest.iterrows())], "value", "plain", show_thumb=False)
            st.caption("Lines: each platform's daily peak. Tap a platform to see its top games by total plays. " + ccu_note)

# ================================================================== stat tiles
games = q.games_window(f.window_start, f.selected)
lk = nowf["likes_lifetime"].sum(min_count=1); pk = nowf["plays_with_likes"].sum(min_count=1)
t1, t2, t3, t4 = st.columns(4)
with t1:
    ui.tile("Games tracked", full(nowf["games_tracked"].sum()) if not nowf.empty else "—", f"{int(nowf['creators'].sum()) if not nowf.empty else 0:,} creators")
with t2:
    per_day = closed_days(win).groupby("day")["plays_delta_top100"].sum(min_count=1).mean()
    ui.tile("Plays / day (tracked top 100)", abbrev(per_day), f"average over {f.window}")
with t3:
    ui.tile("New games / day", abbrev(closed_days(win).groupby("day")["games_new"].sum(min_count=1).mean()), "first seen in a tracked list, finished days")
with t4:
    ui.tile("Likes per 1k plays", ratio_fmt(lk * 1000 / pk) if pk else "—", "pooled over tracked games")

# ================================================================== trending panels
games["rank_change"] = games["rank_prior"] - games["rank_in_source"]
games["likes_per_k"] = [(l * 1000 / p_) if (pd.notna(l) and pd.notna(p_) and p_ >= 1000) else None for l, p_ in zip(games["likes"], games["plays_total"])]


def rows_for(df: pd.DataFrame, value_key: str) -> list[dict]:
    return ui.game_rows(df, value_key)


p1, p2, p3 = st.columns(3)
with p1, st.container(border=True):
    ui.eyebrow("Trending today", "🔥"); ui.title("Top moving games")
    mv = games.dropna(subset=["rank_change"]).sort_values("rank_change", ascending=False).head(5)
    ui.ranked_list(rows_for(mv, "rank_change"), "value", "rank")
    if st.button("Explore list", key="ex_movers", width='stretch'):
        nav("games", mode="Movers")
with p2, st.container(border=True):
    ui.eyebrow("Trending today", "🔥"); ui.title("New and rising")
    nr = games[pd.to_datetime(games["first_seen"]).dt.date >= today_utc() - timedelta(days=7)].sort_values("plays_window", ascending=False, na_position="last").head(5)
    ui.ranked_list(rows_for(nr, "plays_window"), "value", "num")
    if st.button("Explore list", key="ex_new", width='stretch'):
        nav("games", mode="New")
with p3, st.container(border=True):
    ui.eyebrow("Leaderboard", "🏆"); ui.title(f"Top by plays, {f.window}")
    tp = games.sort_values("plays_window", ascending=False, na_position="last").head(5)
    ui.ranked_list(rows_for(tp, "plays_window"), "value", "plain")
    if st.button("Explore list", key="ex_top", width='stretch'):
        nav("games")

p4, p5 = st.columns(2)
with p4, st.container(border=True):
    ui.eyebrow("Leaderboard", "🏆"); ui.title("Best liked (≥1k plays)")
    bl = games.dropna(subset=["likes_per_k"]).sort_values("likes_per_k", ascending=False).head(5)
    ui.ranked_list(rows_for(bl, "likes_per_k"), "value", "plain", value_suffix=" /1k")
    if st.button("Explore list", key="ex_liked", width='stretch'):
        nav("games", by="Likes")
with p5, st.container(border=True):
    ui.eyebrow("Leaderboard", "🏆"); ui.title("Top creators by plays")
    cr = q.creators_window(f.window_start, f.selected).sort_values("plays_window", ascending=False, na_position="last").head(5)
    ui.ranked_list(ui.creator_rows(cr, "plays_window"), "value", "plain", rank_key=None)
    if st.button("Explore list", key="ex_creators", width='stretch'):
        nav("creators")

# ================================================================== platform table
ui.title("Platforms")
METRICS = {
    "Daily plays (top 100)": "plays_delta_top100", "Peak concurrent players (top 100)": "ccu_peak_top100",
    "Average concurrent players (top 100)": "ccu_avg_top100", "New games / day": "games_new",
    "Active creators": "creators_active", "7-day growth": "growth_7d",
    "Likes per 1,000 plays (pooled)": "likes_per_k", "Likes per 1,000 plays (median game)": "median_likes_per_k",
}
metric = st.selectbox("Rank platforms by", list(METRICS), index=0)
col = METRICS[metric]
rows = []
cohort_of = dict(zip(f.sources["name"], f.sources["cohort"]))
aic = q.ai_disclosure_counts().set_index("source")
for src, g in pdaily.groupby("source"):
    g = g.sort_values("day"); gw = win[win["source"] == src]; gw_closed = closed_days(gw)
    last7 = g[g["day"] > today_utc() - timedelta(days=7)]
    prev7 = g[(g["day"] <= today_utc() - timedelta(days=7)) & (g["day"] > today_utc() - timedelta(days=14))]
    p7, pp7 = last7["plays_delta_top100"].sum(min_count=1), prev7["plays_delta_top100"].sum(min_count=1)
    growth = (p7 / pp7 - 1) if (pd.notna(p7) and pd.notna(pp7) and pp7 >= 100) else None
    latest = g.iloc[-1]; nrow = now[now["source"] == src]
    lk_ = float(nrow["likes_lifetime"].iloc[0]) if not nrow.empty and pd.notna(nrow["likes_lifetime"].iloc[0]) else None
    pk_ = float(nrow["plays_with_likes"].iloc[0]) if not nrow.empty and pd.notna(nrow["plays_with_likes"].iloc[0]) else None
    days_in_win = max(1, gw_closed["plays_delta_top100"].notna().sum())
    rows.append({
        "source": src, "Platform": names.get(src, src), "Segment": latest["segment"],
        "plays_lifetime": float(nrow["plays_lifetime"].iloc[0]) if not nrow.empty and pd.notna(nrow["plays_lifetime"].iloc[0]) else None,
        "plays_per_day": (gw_closed["plays_delta_top100"].sum(min_count=1) / days_in_win) if pd.notna(gw_closed["plays_delta_top100"].sum(min_count=1)) else None,
        "plays_delta_top100": gw["plays_delta_top100"].sum(min_count=1),
        "ccu_peak_top100": gw["ccu_peak_top100"].max(), "ccu_avg_top100": gw["ccu_avg_top100"].mean(),
        "games_new": gw_closed["games_new"].mean(), "creators_active": latest["creators_active"], "growth_7d": growth,
        "likes_per_k": (lk_ * 1000.0 / pk_) if lk_ is not None and pk_ else None,
        "median_likes_per_k": float(nrow["median_likes_per_k"].iloc[0]) if not nrow.empty and pd.notna(nrow["median_likes_per_k"].iloc[0]) else None,
        "Games tracked": latest["games_tracked"], "Top-10 share": latest["top10_share"],
        "Cohort": {"legacy": "Legacy"}.get(cohort_of.get(src), "New entrant"),
        "AI-declared games": (f"{int(aic.loc[src, 'ai_games']):,} of {int(aic.loc[src, 'games']):,}"
                              if cohort_of.get(src) == "legacy" and src in aic.index else "—"),
        "Label": label_for(f, src, "players_now" if "concurrent" in metric else "plays", ""),
    })
table = pd.DataFrame(rows)
if table.empty:
    st.info("No platforms selected.")
    st.stop()
table = table.sort_values(col, ascending=False, na_position="last").reset_index(drop=True)
table.insert(0, "#", range(1, len(table) + 1))
LABELS = {"plays_lifetime": "Lifetime plays", "plays_per_day": "Plays / day", "plays_delta_top100": f"Plays {f.window}",
          "ccu_peak_top100": f"Peak players {f.window}", "ccu_avg_top100": "Avg players", "games_new": "New games/day",
          "creators_active": "Active creators", "growth_7d": "Growth 7d", "likes_per_k": "Likes / 1k plays", "median_likes_per_k": "Median likes / 1k"}
disp = table.rename(columns=LABELS)
for c_ in ("Lifetime plays", "Plays / day", f"Plays {f.window}", f"Peak players {f.window}"):
    disp[c_] = disp[c_].map(abbrev)
disp["Avg players"] = disp["Avg players"].map(lambda v: "—" if pd.isna(v) else f"{v:.0f}")
disp["New games/day"] = disp["New games/day"].map(lambda v: "—" if pd.isna(v) else f"{v:.0f}")
disp["Growth 7d"] = disp["Growth 7d"].map(pct)
disp["Likes / 1k plays"] = disp["Likes / 1k plays"].map(ratio_fmt)
disp["Median likes / 1k"] = disp["Median likes / 1k"].map(ratio_fmt)
disp["Top-10 share"] = disp["Top-10 share"].map(lambda v: "—" if pd.isna(v) else f"{float(v) * 100:.0f}%")
phone_cols = list(dict.fromkeys(["#", "Platform", LABELS[col], "Lifetime plays", "Plays / day", "Likes / 1k plays", "Games tracked"]
                                + (["AI-declared games"] if f.cohort != "New entrants" else [])))
ev = phone_table(disp.drop(columns=["source"]), phone_cols, key="platforms")
sel = selected_row(ev, table)
if sel is not None:
    nav("platform", platform=sel["source"])
st.caption("Lifetime plays = each platform's own cumulative counter summed over tracked games; Plays / day = our measured day-over-day increase. "
           "Likes / 1k plays is pooled (big games weigh more); 'median' is the typical game with ≥1,000 plays. Each platform's like is its own button, "
           "so compare ranks and trends, not absolute values. Tap a row to open the platform.")

# ================================================================== whole-platform totals
_pt = q.platform_totals(tuple(f.selected))
if not _pt.empty:
    ui.title("Whole platform")
    trows = []
    for src in [s_ for s_ in f.sources["name"] if s_ in set(_pt["source"])]:
        g = _pt[_pt["source"] == src].sort_values("day")
        last = g.iloc[-1]
        done = closed_days(g)
        pdays = done[(done["gap_days"] == 1) & done["plays_day"].notna() & (done["plays_day"] >= 0)]
        extra = last["extra"] if isinstance(last["extra"], dict) else {}
        agents = extra.get("agent_share_pct") or {}
        note = ", ".join(f"{k} {v:.0f}%" for k, v in sorted(agents.items(), key=lambda kv: -kv[1])[:3]) if agents else ""
        if extra.get("newGamesThisWeek") is not None:
            note = f"{extra['newGamesThisWeek']} new games this week"
        trows.append({
            "Platform": names.get(src, src),
            "All-time plays": abbrev(last["plays_total"]),
            "Plays, last full day": abbrev(pdays.iloc[-1]["plays_day"]) if len(pdays) else "—",
            "Games": abbrev(last["games"]), "Creators": abbrev(last["creators"]),
            "Players now": abbrev(last["players_now"]) if pd.notna(last["players_now"]) else (
                abbrev(last["people_recent"]) + " recently" if pd.notna(last["people_recent"]) else "—"),
            "Peak today": abbrev(last["players_now_peak"] if pd.notna(last["players_now_peak"]) else last["people_recent_peak"]),
            "Notes": ("AI tools: " + note) if agents else note,
            "Updated": ui.ago(last["captured_at"]).replace("Updated ", ""),
        })
    st.dataframe(pd.DataFrame(trows), hide_index=True, width="stretch")
    st.caption("Figures the platforms publish for their entire catalogue, not just the tracked top lists. Bitmagic: its own stats; "
               "Playgama AI: its AI game index (launches, and which AI tool made the games); Spawn: people on the site recently; "
               "Tesana and CrazyGames: sums over the whole catalogue we read each day. Plays, last full day = change in the all-time "
               "counter between two consecutive days (blank until two days are recorded). Other platforms publish no whole-platform figures.")

# ================================================================== over time
ui.title("Over time")
indexed = st.toggle("Indexed (=100 at anchor date)", value=False, help="Raw counts are not comparable across platforms; indexing shows relative movement.")


def line_chart(column: str, title: str):
    fig = go.Figure()
    for src, g in pdaily.groupby("source", sort=False):
        g = g.sort_values("day").dropna(subset=[column])
        g = g[g["day"] >= f.window_start - timedelta(days=1)] if not indexed else g
        if g.empty:
            continue
        y = g[column].astype(float)
        if indexed:
            base_rows = g[g["day"] <= f.anchor]
            base = float(base_rows[column].iloc[-1]) if not base_rows.empty else float(y.iloc[0])
            if not base:
                continue
            y = y / base * 100
        add_daily(fig, g["day"], y, colors.get(src, INK["muted"]), names.get(src, src), width=2, unit="")
    ui.eyebrow(title)
    if not fig.data:
        st.caption("No data for the selected platforms (not every platform publishes this figure).")
        return
    fig.update_layout(height=320, margin=dict(t=28), yaxis=dict(tickformat="~s" if not indexed else ",.0f", rangemode="tozero"))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"over_time_{column}")


line_chart("plays_delta_top100", "Daily plays per platform (tracked top 100)")
line_chart("ccu_peak_top100", "Peak concurrent players per platform (tracked top 100)")
st.caption("Players chart: " + ccu_note)
st.caption("Colours are fixed per platform on every page. Figures cover the tracked top 100 per platform, not the whole platform.")

ui.title("Share of tracked daily plays")
by_seg = {}
for seg, g in win.groupby("segment"):
    for src, gg in g.groupby("source"):
        v = gg["plays_delta_top100"].sum(min_count=1)
        if pd.notna(v) and v > 0:
            by_seg[f"{names.get(src, src)} ({'A' if seg == 'aggregator' else 'B'})"] = float(v)
share_donut(f"Tracked plays by platform, last {f.window} (A = aggregator, B = creation)", by_seg,
            colors={f"{names.get(s, s)} ({'A' if seg == 'aggregator' else 'B'})": colors[s] for s, seg in zip(f.sources["name"], f.sources["segment"])},
            note="Platforms that publish no play counter (placements/CCU only) are absent by design.")
