"""Global sidebar filters shared by every page: platforms, segment, anchor date, window.

State lives in st.session_state so it survives page changes. Drill-down pages (platform / creator /
game) read their target id from st.query_params.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from dashboard import queries as q

WINDOWS = {"24h": 1, "7d": 7, "30d": 30}


@dataclass
class Filters:
    sources: pd.DataFrame          # full sources table (fixed colour order)
    selected: tuple[str, ...]      # platform slugs after platform + segment filters
    segment: str                   # "both" | "aggregator" | "creation"
    anchor: date                   # anchor date for metric cards
    window: str                    # "24h" | "7d" | "30d"

    @property
    def window_days(self) -> int:
        return WINDOWS[self.window]

    @property
    def window_start(self) -> date:
        return today_utc() - timedelta(days=self.window_days - 1)

    @property
    def names(self) -> dict[str, str]:
        return dict(zip(self.sources["name"], self.sources["display_name"]))

    @property
    def labels(self) -> dict[str, dict]:
        return {r["name"]: (r["metric_map"] or {}) for _, r in self.sources.iterrows()}


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def sidebar() -> Filters:
    srcs = q.sources()
    all_names = list(srcs["name"])
    with st.sidebar:
        st.markdown("### Filters")
        seg = st.radio("Segment", ["both", "aggregator", "creation"], horizontal=True, key="f_segment")
        pool = srcs if seg == "both" else srcs[srcs["segment"] == seg]
        chosen = st.multiselect(
            "Platforms", options=list(pool["name"]), default=list(pool["name"]),
            format_func=lambda n: srcs.set_index("name").loc[n, "display_name"], key=f"f_platforms_{seg}",
        )
        anchor = st.date_input("Anchor date (cards show change since)", value=today_utc() - timedelta(days=7),
                               max_value=today_utc(), key="f_anchor")
        window = st.radio("Window for tables", list(WINDOWS), index=1, horizontal=True, key="f_window")
        st.caption("Figures cover the tracked top lists of each platform, not the whole platform.")
    sel = tuple(n for n in all_names if n in chosen)
    return Filters(sources=srcs, selected=sel, segment=seg, anchor=anchor, window=window)


def label_for(f: Filters, source: str, key: str, default: str) -> str:
    """The metric_map label a platform gives a figure (e.g. 'plays = sessions started'), or a default."""
    return (f.labels.get(source) or {}).get(key, default)


def go(page: str, **params) -> None:
    """Navigate to a page with query params (used for tap-a-row drill-downs)."""
    st.query_params.clear()
    for k, v in params.items():
        st.query_params[k] = str(v)
    st.switch_page(f"pages/{page}.py")
