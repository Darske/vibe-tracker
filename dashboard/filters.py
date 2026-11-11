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

WINDOWS = {"1d": 1, "7d": 7, "14d": 14, "28d": 28, "56d": 56, "90d": 90}
COHORTS = {"New entrants": ("new_entrant",), "Legacy": ("legacy",), "All": ("new_entrant", "legacy")}


@dataclass
class Filters:
    sources: pd.DataFrame          # full sources table (fixed colour order)
    selected: tuple[str, ...]      # platform slugs after platform + segment filters
    segment: str                   # "both" | "aggregator" | "creation"
    cohort: str                    # "New entrants" | "Legacy" | "All" (COHORTS)
    anchor: date                   # anchor date for metric cards
    window: str                    # one of WINDOWS: "1d" … "90d"

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
    # Plain links (ranked rows) reload the page and start a new Streamlit session, so they carry the
    # range in the URL (?r=28d); apply it once, then the pills rule.
    r = param("r")
    if r in WINDOWS and st.session_state.get("_r_applied") != r:
        st.session_state["f_range__value"] = r
        st.session_state["_r_applied"] = r
    srcs = q.sources()
    if "cohort" not in srcs.columns:  # before the cohort column exists
        srcs = srcs.assign(cohort="new_entrant")
    all_names = list(srcs["name"])
    from dashboard.ui import pills_value
    cohort = pills_value("f_cohort", "New entrants")
    cohort = cohort if cohort in COHORTS else "New entrants"
    with st.sidebar:
        st.markdown("### Filters")
        seg = st.radio("Segment", ["both", "aggregator", "creation"], horizontal=True, key="f_segment")
        pool = srcs[srcs["cohort"].isin(COHORTS[cohort])]
        pool = pool if seg == "both" else pool[pool["segment"] == seg]
        chosen = st.multiselect(
            "Platforms", options=list(pool["name"]), default=list(pool["name"]),
            format_func=lambda n: srcs.set_index("name").loc[n, "display_name"], key=f"f_platforms_{cohort}_{seg}",
        )
        custom = st.toggle("Custom anchor date", value=False, key="f_custom", help="Cards show the change since this date; otherwise the range pills set it.")
        anchor = st.date_input("Anchor date", value=today_utc() - timedelta(days=7), max_value=today_utc(), key="f_anchor") if custom else None
        st.caption("Figures cover the tracked top lists of each platform, not the whole platform.")
    sel = tuple(n for n in all_names if n in chosen)
    window = pills_value("f_range", "7d")
    if window not in WINDOWS:
        window = "7d"
    if anchor is None:
        anchor = today_utc() - timedelta(days=WINDOWS[window])
    return Filters(sources=srcs, selected=sel, segment=seg, cohort=cohort, anchor=anchor, window=window)


def range_bar(f: Filters, key: str = "f_range", show_cohort: bool = True) -> Filters:
    """Cohort pills (New entrants · Legacy · All) and rotrends' 1d · 7d · 14d · 28d · 56d · 90d range pills, both
    shared across pages via session_state. Changing the cohort reruns the page, and sidebar() applies it."""
    from dashboard.ui import pills
    if show_cohort:
        c1, c2 = st.columns([1, 1.3])
        with c1:
            pills("Platforms", list(COHORTS), "f_cohort", "New entrants")
        with c2:
            window = pills("Range", list(WINDOWS), key, "7d")
    else:
        window = pills("Range", list(WINDOWS), key, "7d")
    anchor = f.anchor if st.session_state.get("f_custom") else today_utc() - timedelta(days=WINDOWS[window])
    return Filters(sources=f.sources, selected=f.selected, segment=f.segment, cohort=f.cohort, anchor=anchor, window=window)


def label_for(f: Filters, source: str, key: str, default: str) -> str:
    """The metric_map label a platform gives a figure (e.g. 'plays = sessions started'), or a default."""
    return (f.labels.get(source) or {}).get(key, default)


def go(page: str, **params) -> None:
    """Navigate to a page with parameters (tap-a-row drill-downs, 'Explore list' buttons).
    st.switch_page drops query params, so they travel in session_state and the target page
    reads them with param(); the target then writes them back into the URL so deep links work."""
    st.session_state["_nav"] = {k: str(v) for k, v in params.items()}
    st.session_state["_nav_fresh"] = True  # consumed by the target page (handed_over())
    st.switch_page(f"views/{page}.py")


def handed_over() -> bool:
    """True once, on the first run of a page reached through go()."""
    return bool(st.session_state.pop("_nav_fresh", False))


def param(name: str) -> str | None:
    """A page parameter from the URL (?name=) or from the last go() call, whichever is present.
    Handed-over params are copied into the URL so the page is shareable."""
    handed = st.session_state.get("_nav") or {}
    if name in handed:
        v = handed.pop(name)
        try:
            st.query_params[name] = v
        except Exception:  # noqa: BLE001  (already set during this run)
            pass
        return v
    return st.query_params.get(name)
