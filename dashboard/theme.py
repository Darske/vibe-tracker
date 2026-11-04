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
