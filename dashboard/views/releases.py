"""Releases: games published per week or month, per platform and combined (Steam-style release chart).

Counts come from the daily `releases` job, which sweeps each platform's newest feed or sitemap, so they cover the
whole catalogue rather than the tracked top lists. How solid each platform's numbers are is shown under the chart.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q, ui
from dashboard.filters import sidebar, today_utc
from dashboard.theme import ACCENT, STACKED, STATUS, abbrev, add_daily, add_series, color_map, full
from tracker.release_queries import COVERAGE, DATE_LOOKUP

f = sidebar()
ui.topnav("releases")
ui.page_header("Game releases", "New games published per week or month on each platform, counting the whole catalogue, not just the top lists.",
               eyebrow_text="Releases", icon="🚀")

c1, c2, c3, c4 = st.columns([2, 2, 2.4, 2.6])
with c1:
    per = ui.pills("Period", ["Monthly", "Weekly"], "rel_period", "Monthly")
with c2:
    view = ui.pills("View", ["Combined", "By platform"], "rel_view", "Combined")
with c3:
    span = ui.pills("Span", ["6M", "1Y", "2Y", "All"], "rel_span", "1Y")
with c4:
    with_partial = st.toggle("Include partial platforms", value=False, key="rel_partial",
                             help="Astrocade and Playgama counts cover only games that reached a tracked list, so they are left out of the totals unless you switch them on.")

_sc, _ = st.columns([2, 4])
with _sc:
    style = ui.chart_style()

if f.cohort == "Legacy":
    st.info("Release counts cover the new-entrant platforms so far. Legacy platforms (CrazyGames, Poki, itch.io, Kongregate) "
            "are tracked on the other pages; their release counts are a next step.")
    st.stop()

period = "month" if per == "Monthly" else "week"
today = today_utc()
cur_start = today.replace(day=1) if period == "month" else today - timedelta(days=today.weekday())
back = {"6M": 183, "1Y": 366, "2Y": 731}.get(span)
since = date(2020, 1, 1) if back is None else today - timedelta(days=back)
since = since.replace(day=1) if period == "month" else since - timedelta(days=since.weekday())

names, colors = f.names, color_map(list(f.sources["name"]))
partial = {k for k, (kind, _) in COVERAGE.items() if kind == "partial"}
chosen = tuple(s for s in f.selected if with_partial or s not in partial)
raw = q.release_counts(period, since, chosen)
cov = q.release_coverage()
if raw.empty:
    st.info("No release data yet. The releases job runs once a day; the first run lists every platform's catalogue.")
    st.stop()

raw["period"] = pd.to_datetime(raw["period"]).dt.date
step = "MS" if period == "month" else "W-MON"
frames = []
for src, g in raw.groupby("source"):  # zero-fill each platform from its first period, so quiet periods show as 0, not gaps
    idx = pd.date_range(min(g["period"]), cur_start, freq=step).date
    frames.append(g.set_index("period")["releases"].reindex(idx, fill_value=0).rename_axis("period").reset_index().assign(source=src))
df = pd.concat(frames, ignore_index=True)
combined = df.groupby("period", as_index=False)["releases"].sum().sort_values("period")

# ---------------------------------------------------------------- headline numbers
closed = combined[combined["period"] < cur_start]
now_val = combined.loc[combined["period"] == cur_start, "releases"].sum()
last = closed.iloc[-1] if len(closed) else None
prev = closed.iloc[-2] if len(closed) > 1 else None
unit = "month" if period == "month" else "week"
t1, t2, t3 = st.columns(3)
with t1:
    ui.tile(f"This {unit} so far", full(now_val), f"since {cur_start:%b %-d}")
with t2:
    if last is not None:
        chg = (last["releases"] / prev["releases"] - 1) if prev is not None and prev["releases"] else None
        ui.tile(f"Last full {unit}", full(last["releases"]),
                (f"{pd.Timestamp(last['period']):%b %Y}" if period == "month" else f"week of {pd.Timestamp(last['period']):%b %-d}")
                + (f" · {chg * 100:+.0f}% vs the one before" if chg is not None else ""))
    else:
        ui.tile(f"Last full {unit}", "—", "not enough history yet")
with t3:
    lp = df[df["period"] == (last["period"] if last is not None else cur_start)].sort_values("releases", ascending=False)
    if len(lp) and lp.iloc[0]["releases"] > 0:
        ui.tile(f"Most releases, last {unit}", names.get(lp.iloc[0]["source"], lp.iloc[0]["source"]), f"{full(lp.iloc[0]['releases'])} games")
    else:
        ui.tile(f"Most releases, last {unit}", "—")

# ---------------------------------------------------------------- chart
tracking_start = pd.to_datetime(cov["since"]).min() if len(cov) and cov["since"].notna().any() else None
x_fmt = "%b %Y" if period == "month" else "%b %-d"
with st.container(border=True):
    ui.eyebrow(f"Games released per {unit}", "📈")
    _have = set(df["source"])
    _sel = [x for x in f.sources["name"] if x in f.selected]
    _cohort = dict(zip(f.sources["name"], f.sources["cohort"]))
    _why = {}
    for x in _sel:
        if x in _have:
            continue
        if x in partial and not with_partial:
            _why[x] = "partial count, switch on above"
        elif _cohort.get(x) == "legacy":
            _why[x] = "release counts not built yet"
        else:
            _why[x] = "no release data yet"
    ui.sites([x for x in _sel if x in _have], names, _why)
    fig = go.Figure()
    if style == STACKED:  # bars stacked by platform: the bar height is the combined count
        for src in [s for s in f.sources["name"] if s in set(df["source"])]:
            d = df[df["source"] == src]
            add_series(fig, STACKED, d["period"], d["releases"], colors.get(src), names.get(src, src), unit="games",
                       partial_from=cur_start, x_fmt=x_fmt)
        fig.update_layout(barmode="stack", legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"))
    elif view == "Combined":
        add_daily(fig, combined["period"], combined["releases"], ACCENT, "", unit="games", partial_from=cur_start, x_fmt=x_fmt)
        fig.update_layout(showlegend=False)
    else:
        for src in [s for s in f.sources["name"] if s in set(df["source"])]:
            d = df[df["source"] == src]
            add_daily(fig, d["period"], d["releases"], colors.get(src), names.get(src, src), width=2, unit="games",
                      partial_from=cur_start, x_fmt=x_fmt, marker=4)
    if tracking_start is not None and tracking_start.date() >= since:
        fig.add_vline(x=tracking_start.date(), line=dict(color=STATUS["good"], dash="dash", width=1.5))
        fig.add_annotation(x=tracking_start.date(), y=1, yref="paper", text="release tracking starts", showarrow=False,
                           textangle=-90, xanchor="right", yanchor="top", font=dict(size=11, color=STATUS["good"]))
    fig.update_layout(height=380, margin=dict(t=16), yaxis=dict(rangemode="tozero", tickformat="~s"),
                      xaxis=dict(tickformat=x_fmt))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.markdown(
        f"<div class='vt-sub'>The {'hatched last bar' if style == STACKED else 'dotted end of each line'} is the current {unit}, still in progress. "
        "Before release tracking started, only games with a known publish date are counted (complete for Tesana and Bitmagic; "
        "Spawn, Remix and Wavedash fill in day by day; Rosebud and Playgama AI have no history). "
        + ("Astrocade and Playgama are partial: they count only games that reached a tracked list." if with_partial else "")
        + "</div>", unsafe_allow_html=True)

# ---------------------------------------------------------------- coverage
ui.title("How complete each platform's count is")
cv = cov.set_index("source") if len(cov) else pd.DataFrame()
rows = []
for src in f.sources["name"]:
    if src not in f.selected:
        continue
    kind, note = COVERAGE.get(src, ("—", ""))
    r = cv.loc[src] if src in cv.index else None
    rows.append({
        "Platform": names.get(src, src),
        "Count": {"complete": "Complete", "partial": "Partial", "net": "Net change"}.get(kind, kind),
        "Games listed": "—" if r is None else abbrev(r["games"]),
        "With publish date": "—" if r is None or not r["games"] else f"{r['dated'] / r['games'] * 100:.0f}%",
        "Counting new games since": "—" if r is None or pd.isna(r["since"]) else f"{pd.Timestamp(r['since']):%b %-d, %Y}",
        "Dates still to look up": abbrev(r["dates_pending"]) if r is not None and src in DATE_LOOKUP else "—",
        "What is counted": note,
    })
st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
