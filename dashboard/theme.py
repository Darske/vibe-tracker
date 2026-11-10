"""Chart + page theme: dark, in the style of rotrends.com. Palette values are the dataviz reference
palette's dark steps (validated: CVD ΔE ≥ 8.4 adjacent, normal-vision ≥ 19, contrast ≥ 3:1 on #15181e).

Categorical hues are assigned to platforms in a FIXED order (sources.added_at, name) and never
re-cycled when a filter hides a platform: a platform keeps its colour on every page.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

CATEGORICAL = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER = "#898781"
# Per-game series (several games of ONE platform, or games across platforms): the lead game in the accent
# and the rest in neutral inks, so game lines never read as platform colours.
GAME_TONES = ["#3987e5", "#c9ced8", "#9aa1ae", "#6f7684", "#4b515d"]
ACCENT = "#3987e5"            # hero numbers + links, like rotrends' blue
STATUS = {"good": "#22c55e", "warning": "#eab308", "serious": "#f97316", "critical": "#ef4444"}
INK = {"primary": "#e8eaf0", "secondary": "#aab0bd", "muted": "#7c8494", "grid": "#262a33", "axis": "#3a3f4b"}
SURFACE = "#171a21"
PAGE = "#0f1115"
BORDER = "#262a33"

_template = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, -apple-system, Segoe UI, Roboto, sans-serif", size=12, color=INK["secondary"]),
        margin=dict(l=4, r=8, t=8, b=4),
        colorway=CATEGORICAL,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1f232c", bordercolor=BORDER, font=dict(color=INK["primary"], size=12)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None,
                    font=dict(size=12, color=INK["secondary"]), itemsizing="constant"),
        xaxis=dict(showgrid=False, linecolor=INK["axis"], tickcolor=INK["axis"], ticks="", zeroline=False,
                   tickfont=dict(color=INK["muted"])),
        yaxis=dict(gridcolor=INK["grid"], gridwidth=1, showline=False, zeroline=False, tickformat="~s",
                   side="right", tickfont=dict(color=INK["muted"])),
    )
)
pio.templates["vibe"] = _template
pio.templates.default = "vibe"


def color_map(platforms: list[str]) -> dict[str, str]:
    """Fixed platform → colour. `platforms` must be the FULL ordered list from the sources table."""
    return {p: (CATEGORICAL[i] if i < len(CATEGORICAL) else OTHER) for i, p in enumerate(platforms)}


def abbrev(n) -> str:
    """12.4k, 3.5M — numbers abbreviated the way the brief asks."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    if n != n:  # NaN
        return "—"
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            v = n / div
            return f"{sign}{v:.1f}{unit}" if v < 100 else f"{sign}{v:.0f}{unit}"
    return f"{sign}{n:.0f}"


def full(n) -> str:
    """15,892,963 — the hero-number style."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    return "—" if n != n else f"{n:,.0f}"


def pct(x) -> str:
    if x is None:
        return "—"
    x = float(x)
    return "—" if x != x else f"{x * 100:+.1f}%"


def likes_per_k(likes, plays, min_plays: int = 1000) -> float | None:
    """Likes per 1,000 lifetime plays; None below min_plays (too noisy to compare)."""
    try:
        likes = float(likes); plays = float(plays)
    except (TypeError, ValueError):
        return None
    if likes != likes or plays != plays or plays < min_plays:
        return None
    return likes * 1000.0 / plays


def ratio_fmt(v) -> str:
    return "—" if v is None or v != v else f"{v:.1f}"


def time_axis(days: int) -> dict:
    """x-axis settings for an intraday series: one tick per day (hours for the 1-day window), no duplicate labels."""
    if days <= 1:
        return dict(tickformat="%H:%M", dtick=3 * 3600 * 1000)
    if days <= 14:
        return dict(tickformat="%b %-d", dtick=86400000 * (1 if days <= 7 else 2))
    return dict(tickformat="%b %-d", nticks=8)


def line_mode(n_points: int) -> str:
    """Lines only when there are enough readings to draw one; otherwise show the points too."""
    return "lines" if n_points > 24 else "lines+markers"


def add_daily(fig, days, values, color: str, name: str = "", width: float = 2.5, unit: str = "plays", marker: int = 5,
              partial_from=None, x_fmt: str = "%b %-d"):
    """Add a daily series to `fig` the way release charts show an unfinished month: closed UTC days as a solid
    line, today's still-running day as a dotted segment with a hollow marker, so a half-day total never reads
    as a crash. Hover on the dotted point says it is partial."""
    from datetime import datetime, timezone

    import pandas as pd

    today = partial_from or datetime.now(timezone.utc).date()  # first day of the unfinished period
    s = pd.DataFrame({"day": pd.to_datetime(pd.Series(list(days))).dt.date, "v": list(values)}).dropna(subset=["v"]).sort_values("day")
    closed, part = s[s["day"] < today], s[s["day"] >= today]
    fig.add_trace(go.Scatter(x=closed["day"], y=closed["v"], mode="lines+markers", name=name, legendgroup=name or None, showlegend=bool(name),
                             line=dict(width=width, color=color), marker=dict(size=marker),
                             hovertemplate="%{x|" + x_fmt + "}: %{y:,.0f} " + unit + ("<extra>" + name + "</extra>" if name else "<extra></extra>")))
    if not part.empty:
        bridge = pd.concat([closed.tail(1), part])
        fig.add_trace(go.Scatter(x=bridge["day"], y=bridge["v"], mode="lines+markers", name=(name + " (today, partial)") if name else "today, partial",
                                 legendgroup=name or None, showlegend=False, line=dict(width=max(1.5, width - 0.5), color=color, dash="dot"),
                                 marker=dict(size=marker + 1, color="rgba(0,0,0,0)", line=dict(color=color, width=1.5)),
                                 hovertemplate="%{x|" + x_fmt + "}: %{y:,.0f} " + unit + " so far (not finished)" + ("<extra>" + name + "</extra>" if name else "<extra></extra>")))
    return fig


def closed_days(df, day_col: str = "day"):
    """Rows for finished UTC days only (drop today's partial day) — for averages and 'latest day' figures."""
    from datetime import datetime, timezone

    import pandas as pd

    if df is None or df.empty:
        return df
    return df[pd.to_datetime(df[day_col]).dt.date < datetime.now(timezone.utc).date()]
