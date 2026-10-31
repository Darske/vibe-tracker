"""Chart theme shared by every page. Palette values from the dataviz reference instance.

Categorical hues are assigned to platforms in a FIXED order (by sources.added_at, then name) and
never re-cycled when a filter hides a platform: a platform keeps its colour everywhere.
"""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
OTHER = "#898781"
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
INK = {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"}
SURFACE = "#fcfcfb"

_template = go.layout.Template(
    layout=go.Layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, -apple-system, Segoe UI, sans-serif", size=12, color=INK["secondary"]),
        margin=dict(l=8, r=8, t=28, b=8),
        colorway=CATEGORICAL,
        hovermode="x unified",
        hoverlabel=dict(bgcolor="white", bordercolor=INK["grid"], font=dict(color=INK["primary"])),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title=None, font=dict(size=11)),
        xaxis=dict(showgrid=False, linecolor=INK["axis"], tickcolor=INK["axis"], ticks="outside", zeroline=False),
        yaxis=dict(gridcolor=INK["grid"], gridwidth=1, showline=False, zeroline=False, tickformat="~s"),
    )
)
pio.templates["vibe"] = _template
pio.templates.default = "vibe"


def color_map(platforms: list[str]) -> dict[str, str]:
    """Fixed platform → colour assignment. `platforms` must be the FULL ordered list from the
    sources table, not the filtered one, so colours never shift."""
    out = {}
    for i, p in enumerate(platforms):
        out[p] = CATEGORICAL[i] if i < len(CATEGORICAL) else OTHER
    return out


def abbrev(n) -> str:
    """12.4k, 3.5M — numbers abbreviated the way the brief asks."""
    if n is None:
        return "—"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if n >= div:
            v = n / div
            return f"{sign}{v:.1f}{unit}" if v < 100 else f"{sign}{v:.0f}{unit}"
    return f"{sign}{n:.0f}"


def pct(x) -> str:
    return "—" if x is None else f"{float(x) * 100:+.1f}%"
