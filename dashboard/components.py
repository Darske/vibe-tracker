"""The three reusable components the brief asks for, used on every page:

- metric_card(title, series, anchor_date, kind): headline value, "+Δ (+x%) since <anchor>", line chart from the anchor to today.
- share_donut(title, breakdown): donut with a legend showing each slice's share and absolute value.
- game_grid(games): cards with thumbnail, title, platform badge, creator, published date, plays in window, players now.

Plus a few small helpers (platform badges, link buttons, sortable table with the phone column cap).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.theme import CATEGORICAL, INK, OTHER, SURFACE, abbrev, pct


def _eyebrow(text: str) -> None:
    from html import escape
    st.markdown(f"<div class='vt-eyebrow'>{escape(text)}</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------- metric card
def metric_card(title: str, series: pd.DataFrame, anchor: date, kind: str = "cumulative",
                color: str = CATEGORICAL[0], note: str | None = None, second: pd.DataFrame | None = None,
                second_label: str = "average") -> None:
    """series: DataFrame with columns [day, value]; kind 'cumulative' (plays, followers) or 'daily-peak'
    (concurrent players). For cumulative the headline is the latest total and Δ = latest − value at anchor.
    For daily-peak the headline is today's peak and Δ compares to the peak on the anchor day."""
    with st.container(border=True):
        _eyebrow(title)
        if series is None or series.empty or series["value"].dropna().empty:
            st.markdown("<p class='vt-big'>—</p>", unsafe_allow_html=True)
            st.markdown(f"<div class='vt-sub'>{note or 'no data yet'}</div>", unsafe_allow_html=True)
            return
        s = series.dropna(subset=["value"]).sort_values("day")
        s["day"] = pd.to_datetime(s["day"]).dt.date
        latest = float(s["value"].iloc[-1])
        at_anchor = s[s["day"] <= anchor]
        base = float(at_anchor["value"].iloc[-1]) if not at_anchor.empty else None
        delta_txt = ""
        if base is not None and base != latest:
            d = latest - base
            rel = f" ({pct(d / base)})" if base else ""
            delta_txt = f"{'+' if d >= 0 else '−'}{abbrev(abs(d))}{rel} since {anchor.strftime('%b %-d')}"
        elif base is not None:
            delta_txt = f"no change since {anchor.strftime('%b %-d')}"
        else:
            delta_txt = f"first seen {s['day'].iloc[0].strftime('%b %-d')}"
        st.markdown(f"<p class='vt-big'>{abbrev(latest)}</p>", unsafe_allow_html=True)
        st.markdown(f"<div class='vt-sub'>{delta_txt}{(' · ' + note) if note else ''}</div>", unsafe_allow_html=True)

        plot = s[s["day"] >= anchor] if (s["day"] >= anchor).sum() >= 2 else s.tail(14)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=plot["day"], y=plot["value"], mode="lines+markers", name=title,
                                 line=dict(width=2, color=color), marker=dict(size=5),
                                 hovertemplate="%{x|%b %-d}: %{y:,.0f}<extra></extra>"))
        if second is not None and not second.empty:
            s2 = second.dropna(subset=["value"]).copy()
            s2["day"] = pd.to_datetime(s2["day"]).dt.date
            s2 = s2[s2["day"] >= plot["day"].min()]
            fig.add_trace(go.Scatter(x=s2["day"], y=s2["value"], mode="lines", name=second_label,
                                     line=dict(width=2, color=color, dash="dot"),
                                     hovertemplate="%{x|%b %-d}: %{y:,.1f}<extra>" + second_label + "</extra>"))
        span = max(1, (pd.Timestamp(plot["day"].max()) - pd.Timestamp(plot["day"].min())).days)
        fig.update_layout(height=160, margin=dict(l=0, r=0, t=4, b=0), showlegend=second is not None,
                          xaxis=dict(showticklabels=True, tickformat="%b %-d", dtick=86400000 * max(1, round(span / 3))), yaxis=dict(nticks=3))
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


# ---------------------------------------------------------------------------- share donut
def share_donut(title: str, breakdown: dict[str, float], colors: dict[str, str] | None = None, note: str | None = None) -> None:
    """breakdown: {label: value}. Slices beyond the 8th fold into 'Other' (fixed categorical order)."""
    with st.container(border=True):
        _eyebrow(title)
        items = [(k, float(v)) for k, v in breakdown.items() if v is not None and float(v) > 0]
        if not items:
            st.caption(note or "no data yet")
            return
        items.sort(key=lambda kv: -kv[1])
        if len(items) > 8:
            head, tail = items[:7], items[7:]
            items = head + [("Other", sum(v for _, v in tail))]
        total = sum(v for _, v in items)
        labels = [k for k, _ in items]
        values = [v for _, v in items]
        cols = [(colors or {}).get(k, CATEGORICAL[i] if i < 8 else OTHER) if k != "Other" else OTHER for i, k in enumerate(labels)]
        fig = go.Figure(go.Pie(labels=labels, values=values, hole=0.62, sort=False, direction="clockwise",
                               marker=dict(colors=cols, line=dict(color=SURFACE, width=2)), textinfo="none",
                               hovertemplate="%{label}: %{value:,.0f} (%{percent})<extra></extra>"))
        fig.update_layout(height=200, margin=dict(l=0, r=0, t=0, b=0), showlegend=False,
                          annotations=[dict(text=abbrev(total), x=0.5, y=0.5, showarrow=False, font=dict(size=18, color=INK["primary"]))])
        c1, c2 = st.columns([1, 1])
        with c1:
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        with c2:
            rows = "".join(
                f"<div style='display:flex;align-items:center;gap:6px;font-size:0.85rem;margin:2px 0'>"
                f"<span style='width:10px;height:10px;border-radius:2px;background:{c};display:inline-block'></span>"
                f"<span style='flex:1'>{l}</span><span style='color:{INK['secondary']}'>{('<1' if v / total < 0.005 else f'{v / total * 100:.0f}')}% · {abbrev(v)}</span></div>"
                for l, v, c in zip(labels, values, cols)
            )
            st.markdown(rows, unsafe_allow_html=True)
        if note:
            st.caption(note)


# ---------------------------------------------------------------------------- game grid
def game_grid(games: pd.DataFrame, names: dict[str, str], window_label: str, per_row: int = 3, limit: int = 30) -> None:
    """A creator's games as rotrends-style ranked rows: thumb · title / platform · plays in window · lifetime plays."""
    from dashboard import ui
    if games is None or games.empty:
        st.info("No games in this selection yet.")
        return
    g = games.sort_values("plays_window", ascending=False, na_position="last").head(limit)
    rows = []
    for _, r in g.iterrows():
        pn = r.get("players_now")
        sub = names.get(r["source"], r["source"]) + (f" · {abbrev(pn)} playing now" if pn is not None and not pd.isna(pn) else "") + \
              f" · {abbrev(r.get('plays_total'))} lifetime plays"
        rows.append({"title": r.get("title") or r.get("url"), "sub": sub, "thumb": r.get("thumbnail_url"),
                     "href": ui._href("game", "game", r["id"]), "value": r.get("plays_window"), "rank": None})
    with st.container(border=True):
        ui.eyebrow(f"Plays, {window_label}", "🎮")
        ui.ranked_list(rows, "value", "plain", rank_key=None)


# ---------------------------------------------------------------------------- helpers
def badge(text: str, color: str = INK["secondary"]) -> str:
    return (f"<span style='border:1px solid {color};color:{color};border-radius:10px;padding:0 7px;"
            f"font-size:0.75rem;white-space:nowrap'>{text}</span>")


def link_buttons(links: dict[str, str] | None, url: str | None = None, platform_name: str | None = None) -> None:
    items = []
    if url:
        items.append((f"{platform_name or 'profile'} ↗", url))
    for k in ("x", "twitter", "youtube", "tiktok", "discord", "twitch", "instagram", "website"):
        if links and links.get(k):
            items.append((k.replace("twitter", "x"), links[k]))
    if items:
        st.markdown(" ".join(f"[{badge(k)}]({u})" for k, u in items), unsafe_allow_html=True)


def phone_table(df: pd.DataFrame, phone_cols: list[str], key: str, column_config: dict | None = None, height: int | None = None):
    """Shows at most ~6 columns by default with an 'all columns' toggle (phones)."""
    show_all = st.toggle("All columns", value=False, key=f"allcols_{key}")
    cols = list(df.columns) if show_all else [c for c in phone_cols if c in df.columns]
    kwargs = {"height": height} if height is not None else {}
    return st.dataframe(df[cols], hide_index=True, width='stretch', column_config=column_config,
                        on_select="rerun", selection_mode="single-row", key=f"tbl_{key}", **kwargs)


def selected_row(event, df: pd.DataFrame):
    try:
        rows = event.selection.rows  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return None
    return df.iloc[rows[0]] if rows else None


def fmt_dt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
        return "—"
    return pd.to_datetime(v).strftime("%b %-d")
