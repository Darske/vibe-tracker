"""Page chrome and the rotrends-style building blocks: eyebrow labels, hero numbers, panels, ranked
lists with rank badges and deltas, range pills, stat tiles. Pure presentation — no SQL here."""

from __future__ import annotations

import base64
import functools
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from dashboard.theme import ACCENT, BORDER, INK, PAGE, STATUS, SURFACE, abbrev

CSS = f"""
<style>
:root {{ --accent:{ACCENT}; --surface:{SURFACE}; --page:{PAGE}; --border:{BORDER};
        --ink:{INK['primary']}; --ink2:{INK['secondary']}; --muted:{INK['muted']};
        --good:{STATUS['good']}; --bad:{STATUS['critical']}; }}
.stApp {{ background: var(--page); }}
.block-container {{ padding-top: 4.4rem; padding-bottom: 3rem; max-width: 1180px; }}
h1 {{ font-size: 1.9rem !important; font-weight: 700 !important; letter-spacing: -0.01em; margin-bottom: .4rem !important; }}
h2, h3 {{ font-weight: 700 !important; letter-spacing: -0.01em; }}
/* cards */
div[data-testid="stVerticalBlockBorderWrapper"] {{ background: var(--surface); border: 1px solid var(--border) !important;
  border-radius: 14px !important; padding: 2px 6px; }}
/* rotrends-style bits */
.vt-eyebrow {{ font-size: .74rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--ink2); margin: 2px 0 2px 0; }}
.vt-hero {{ font-size: 2.1rem; font-weight: 700; color: var(--accent); line-height: 1.1; margin: 0; letter-spacing: -0.01em; }}
.vt-hero.white {{ color: var(--ink); }}
.vt-big {{ font-size: 1.6rem; font-weight: 700; color: var(--ink); line-height: 1.15; margin: 0; }}
/* "which sites are in this chart" strip under chart titles */
.vt-sites {{ display:flex; flex-wrap:wrap; align-items:center; gap:6px 6px; margin: 4px 0 10px 0; font-size:.8rem; }}
.vt-sites .k {{ color: var(--ink2); font-weight:600; margin-right:2px; }}
.vt-sites .k.out {{ margin-left:8px; color: var(--muted); }}
.vt-chip {{ display:inline-flex; align-items:center; gap:5px; padding:2px 8px 2px 3px; border-radius:999px;
  background: rgba(255,255,255,.06); border:1px solid var(--border); color: var(--ink); white-space:nowrap; }}
.vt-chip img {{ width:16px; height:16px; border-radius:4px; display:block; }}
.vt-chip.icon {{ padding:2px; }}
.vt-chip.icon img {{ width:18px; height:18px; }}
.vt-chip.out {{ opacity:.45; background: transparent; text-decoration: line-through; text-decoration-color: rgba(255,255,255,.35); }}
.vt-logo {{ width:22px; height:22px; border-radius:5px; vertical-align:-4px; margin-right:6px; }}
.vt-logo-sm {{ width:16px; height:16px; border-radius:4px; vertical-align:-3px; margin-right:5px; }}
.vt-logo-lg {{ width:34px; height:34px; border-radius:8px; vertical-align:-5px; margin-right:10px; }}
.vt-sub {{ color: var(--muted); font-size: .86rem; margin: 2px 0 10px 0; }}
.vt-title {{ font-size: 1.25rem; font-weight: 700; color: var(--ink); margin: 0 0 6px 0; }}
.vt-row {{ display: flex; align-items: center; gap: 10px; padding: 9px 2px; border-bottom: 1px solid var(--border); }}
.vt-row:last-child {{ border-bottom: 0; }}
.vt-thumb {{ width: 38px; height: 38px; border-radius: 8px; object-fit: cover; background: #22262f; flex: none; }}
.vt-thumb.ph {{ display:flex; align-items:center; justify-content:center; color: var(--muted); font-weight:700; font-size:.8rem; }}
.vt-name {{ flex: 1; min-width: 0; }}
.vt-name .t {{ color: var(--ink); font-weight: 600; font-size: .95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.vt-name .c {{ color: var(--muted); font-size: .8rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.vt-val {{ font-variant-numeric: tabular-nums; font-weight: 600; font-size: .92rem; white-space: nowrap; }}
.vt-val.up {{ color: var(--good); }} .vt-val.down {{ color: var(--bad); }} .vt-val.flat {{ color: var(--ink2); }}
.vt-rank {{ color: var(--ink); font-weight: 700; font-variant-numeric: tabular-nums; min-width: 48px; text-align: right; }}
.vt-badge {{ display:inline-block; border:1px solid var(--border); color: var(--ink2); border-radius: 999px; padding: 0 8px; font-size: .72rem; line-height: 1.5; white-space: nowrap; }}
.vt-tile {{ padding: 6px 2px 2px 2px; }}
.vt-tile .l {{ color: var(--ink2); font-size: .85rem; }}
.vt-tile .v {{ color: var(--ink); font-size: 1.55rem; font-weight: 700; line-height: 1.2; }}
.vt-tile .d {{ font-size: .8rem; color: var(--muted); }}
a.vt-link {{ color: var(--accent) !important; text-decoration: none; font-weight: 600; }}
/* pills: segmented control */
div[data-testid="stSegmentedControl"] button {{ border-radius: 8px !important; }}
/* header art (game thumb / creator avatar) */
.vt-art {{ width: 100% !important; max-width: 150px !important; border-radius: 14px; border: 1px solid var(--border); display:block; }}
.vt-art.round {{ border-radius: 50%; aspect-ratio: 1; object-fit: cover; }}
/* top nav page links */
div[data-testid="stPageLink"] a {{ border-radius: 999px; padding: 4px 12px; }}
@media (max-width: 640px) {{ div[data-testid="stPageLink"] a {{ padding: 2px 3px; font-size: .82rem; }} div[data-testid="stPageLink"] a span[data-testid="stIconEmoji"] {{ display: none; }} }}
div[data-testid="stPageLink"] a:hover {{ background: var(--surface); }}
/* tables + buttons */
div[data-testid="stDataFrame"] {{ border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }}
.stButton > button[kind="secondary"] {{ border-radius: 10px; border: 1px solid var(--border); background: transparent; color: var(--ink); }}
.stButton > button[kind="secondary"]:hover {{ border-color: var(--accent); color: var(--accent); }}
.stButton > button[kind="tertiary"] {{ color: var(--accent); padding-left: 0; }}
/* hide streamlit chrome we don't need */
#MainMenu, footer {{ visibility: hidden; }}
@media (max-width: 640px) {{
  .vt-hero {{ font-size: 1.7rem; }}
  .block-container {{ padding-left: .9rem; padding-right: .9rem; }}
}}
</style>
"""


LOGO_DIR = Path(__file__).resolve().parent / "static" / "logos"


@functools.lru_cache(maxsize=64)
def logo_uri(source: str) -> str | None:
    """Platform icon (taken from the platform's own site, 64 px PNG in dashboard/static/logos) as a data URI."""
    f = LOGO_DIR / f"{source}.png"
    return "data:image/png;base64," + base64.b64encode(f.read_bytes()).decode() if f.exists() else None


def logo_html(source: str, cls: str = "vt-logo") -> str:
    uri = logo_uri(source)
    return f"<img class='{cls}' src='{uri}' alt=''/>" if uri else ""


def _chip(source: str, label: str, out: bool = False, why: str | None = None, compact: bool = False) -> str:
    uri = logo_uri(source)
    img = f"<img src='{uri}' alt=''/>" if uri else ""
    tip = escape(label + (f": {why}" if why else ""))
    cls = "vt-chip" + (" out" if out else "") + (" icon" if compact and uri else "")
    text = "" if compact and uri else escape(label)
    return f"<span class='{cls}' title='{tip}'>{img}{text}</span>"


def sites(included, names: dict, excluded: dict | None = None, label: str = "Includes", compact: bool = False) -> None:
    """The row of platform logos right under a chart title: which sites the chart covers and, faded and struck
    through, which selected sites it leaves out (hover a faded chip for the reason). `included` is an iterable of
    source keys, `excluded` maps source key -> reason. compact=True shows the left-out platforms as logos only (name on hover) for narrow cards."""
    inc = list(dict.fromkeys(included))
    exc = {k: v for k, v in (excluded or {}).items() if k not in inc}
    if not inc and not exc:
        return
    parts = [f"<span class='k'>{escape(label)}</span>"] + [_chip(s, names.get(s, s)) for s in inc]
    if not inc:
        parts.append("<span class='vt-chip out' style='text-decoration:none'>none in this range</span>")
    groups: dict[str, list[str]] = {}
    for s_, r in exc.items():  # one labelled group per reason, so it reads without hovering (phones)
        groups.setdefault(r, []).append(s_)
    for i, (r, srcs) in enumerate(groups.items()):
        head = (f"Not included ({r})" if i == 0 else f"({r})")
        parts.append(f"<span class='k out'>{escape(head)}</span>")
        parts += [_chip(s_, names.get(s_, s_), out=True, why=r, compact=compact) for s_ in srcs]
    st.markdown(f"<div class='vt-sites'>{''.join(parts)}</div>", unsafe_allow_html=True)


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def eyebrow(text: str, icon: str = "") -> None:
    st.markdown(f"<div class='vt-eyebrow'>{icon} {escape(text)}</div>", unsafe_allow_html=True)


def hero(value: str, sub: str | None = None, white: bool = False) -> None:
    cls = "vt-hero white" if white else "vt-hero"
    st.markdown(f"<p class='{cls}'>{escape(value)}</p>" + (f"<div class='vt-sub'>{escape(sub)}</div>" if sub else ""), unsafe_allow_html=True)


def big(value: str, sub: str | None = None) -> None:
    st.markdown(f"<p class='vt-big'>{escape(value)}</p>" + (f"<div class='vt-sub'>{escape(sub)}</div>" if sub else ""), unsafe_allow_html=True)


def title(text: str) -> None:
    st.markdown(f"<div class='vt-title'>{escape(text)}</div>", unsafe_allow_html=True)


def tile(label: str, value: str, detail: str | None = None) -> None:
    with st.container(border=True):
        st.markdown(
            f"<div class='vt-tile'><div class='l'>{escape(label)}</div><div class='v'>{escape(value)}</div>"
            + (f"<div class='d'>{escape(detail)}</div>" if detail else "") + "</div>", unsafe_allow_html=True)


def ago(ts) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return "no data yet"
    t = pd.Timestamp(ts).to_pydatetime()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - t).total_seconds() / 60
    if mins < 90:
        return f"Updated {mins:.0f} minutes ago"
    if mins < 60 * 36:
        return f"Updated {mins / 60:.0f} hours ago"
    return f"Updated {mins / 1440:.0f} days ago"


def delta_html(delta, kind: str = "num") -> str:
    """Green ↗ / red ↘ value like rotrends' trending rows. kind: num | pct | rank (rank: positive = moved up)."""
    if delta is None or (isinstance(delta, float) and pd.isna(delta)):
        return "<span class='vt-val flat'>—</span>"
    d = float(delta)
    if kind == "pct":
        txt = f"{d * 100:+.0f}%"
    elif kind == "rank":
        txt = f"{abs(d):.0f}"
    else:
        txt = abbrev(abs(d)) if abs(d) >= 1000 else f"{abs(d):,.0f}"
    if d > 0:
        return f"<span class='vt-val up'>↗ {txt}</span>"
    if d < 0:
        return f"<span class='vt-val down'>↘ {txt}</span>"
    return f"<span class='vt-val flat'>→ {txt}</span>"


def ranked_list(rows: list[dict], value_key: str, value_kind: str = "num", rank_key: str | None = "rank",
                show_thumb: bool = True, value_suffix: str = "") -> None:
    """rows: dicts with title, creator (optional), thumb (optional), <value_key>, <rank_key>.
    Renders rotrends' row: thumb · name/@creator · green/red value · #rank."""
    parts = []
    for r in rows:
        thumb = r.get("thumb")
        if show_thumb:
            t = (f"<img class='vt-thumb' src='{escape(str(thumb))}' loading='lazy'/>" if thumb
                 else f"<div class='vt-thumb ph'>{escape((r.get('title') or '?')[:1].upper())}</div>")
        else:
            t = ""
        creator = f"<div class='c'>@{escape(str(r['creator']).lstrip('@'))}</div>" if r.get("creator") else (f"<div class='c'>{escape(str(r['sub']))}</div>" if r.get("sub") else "")
        href, url = r.get("href"), r.get("url")
        name = escape(str(r.get("title") or "untitled"))
        if href:  # internal drill-down (same tab)
            name_html = f"<a class='vt-link' href='{escape(str(href))}' target='_self' style='color:var(--ink)!important;font-weight:600'>{name}</a>"
        elif url:
            name_html = f"<a class='vt-link' href='{escape(str(url))}' target='_blank' style='color:var(--ink)!important;font-weight:600'>{name}</a>"
        else:
            name_html = name
        val = r.get(value_key)
        if value_kind in ("num", "pct", "rank"):
            v = delta_html(val, value_kind)
        else:  # plain value
            v = f"<span class='vt-val flat'>{escape(abbrev(val))}{value_suffix}</span>"
        rank = r.get(rank_key) if rank_key else None
        rk = f"<div class='vt-rank'>#{int(rank)}</div>" if rank is not None and not (isinstance(rank, float) and pd.isna(rank)) else ""
        parts.append(f"<div class='vt-row'>{t}<div class='vt-name'><div class='t'>{name_html}</div>{creator}</div>{v}{rk}</div>")
    st.markdown("".join(parts) or "<div class='vt-sub'>nothing yet</div>", unsafe_allow_html=True)




def legend_dots(items: list[tuple[str, str]]) -> None:
    """items: [(label, colour)] — the coloured-dot legend rotrends puts above multi-series charts."""
    st.markdown(" &nbsp; ".join(
        f"<span style='color:{c}'>●</span> <span style='color:var(--ink2);font-size:.85rem'>{escape(l)}</span>" for l, c in items
    ), unsafe_allow_html=True)


def page_header(title_text: str, sub: str | None = None, eyebrow_text: str | None = None, icon: str = "",
                logo: str | None = None) -> None:
    """rotrends-style page head: optional eyebrow, big title (with the platform's logo when `logo` is a source key),
    muted one-liner."""
    if eyebrow_text:
        eyebrow(eyebrow_text, icon)
    img = logo_html(logo, "vt-logo vt-logo-lg") if logo else ""
    st.markdown(f"<h1 style='margin-top:0'>{img}{escape(title_text)}</h1>", unsafe_allow_html=True)
    if sub:
        st.markdown(f"<div class='vt-sub' style='margin-top:-4px'>{sub}</div>", unsafe_allow_html=True)


def _href(page: str, key: str, value) -> str:
    """Internal link that also carries the current range (a plain link starts a new session)."""
    r = st.session_state.get("f_range__value")
    return f"{page}?{key}={int(value)}" + (f"&r={r}" if r else "")


def game_rows(df: pd.DataFrame, value_key: str, rank_key: str = "rank_in_source", sub_key: str | None = None) -> list[dict]:
    """Rows for ranked_list from a games_window-shaped frame; names link to our game page."""
    out = []
    for _, r in df.iterrows():
        out.append({"title": r.get("title") or r.get("url"), "creator": r.get("creator_name"), "thumb": r.get("thumbnail_url"),
                    "href": _href("game", "game", r["id"]), "value": r.get(value_key), "rank": r.get(rank_key) if rank_key else None,
                    "sub": r.get(sub_key) if sub_key else None})
    return out


def creator_rows(df: pd.DataFrame, value_key: str) -> list[dict]:
    out = []
    for _, r in df.iterrows():
        n_games = r.get("games_live")
        n_games = int(n_games) if n_games is not None and not pd.isna(n_games) else 0
        out.append({"title": r.get("name") or r.get("external_id"), "sub": f"{r.get('display_name')} · {n_games} game{'s' if n_games != 1 else ''}",
                    "thumb": r.get("avatar_url"), "href": _href("creator", "creator", r["id"]), "value": r.get(value_key), "rank": None})
    return out


def stats_line(parts: list[tuple[str, str]]) -> None:
    """'**2,365** games · **1,440** creators' as a muted line with white numbers."""
    st.markdown(" &nbsp;·&nbsp; ".join(f"<span style='color:var(--ink);font-weight:700'>{escape(v)}</span> "
                                        f"<span style='color:var(--ink2)'>{escape(l)}</span>" for v, l in parts), unsafe_allow_html=True)


def pills(label: str, options: list[str], key: str, default: str | None = None) -> str:
    """st.segmented_control whose value survives page changes and never comes back None.

    Keyed widget state is dropped by Streamlit when the user moves to another page, so the chosen
    value lives in a plain session_state key (`<key>__value`) that an on_change callback keeps in
    sync; the widget itself is recreated from it on every run. Deselecting the active pill (which the
    control allows) leaves the stored value untouched, so the pill simply stays selected."""
    store = f"{key}__value"
    default = default if default in options else options[0]
    if st.session_state.get(store) not in options:
        st.session_state[store] = default

    nonce_key = f"{key}__nonce"
    wkey = f"{key}_{st.session_state.get(nonce_key, 0)}"

    def _sync() -> None:
        v = st.session_state.get(wkey)
        if v in options:
            st.session_state[store] = v
        else:  # deselect: rotate the widget key so a fresh control is drawn showing the stored value
            st.session_state[nonce_key] = st.session_state.get(nonce_key, 0) + 1

    st.segmented_control(label, options, default=st.session_state[store], key=wkey, label_visibility="collapsed", on_change=_sync)
    return st.session_state[store]


def chart_style() -> str:
    """The "Lines | Stacked bars" switch, one setting shared by every page (stored like pills())."""
    from dashboard.theme import CHART_STYLES, LINES
    return pills("Chart style", CHART_STYLES, "chart_style", LINES)


def pills_value(key: str, default: str) -> str:
    """The stored value of a pills() control without rendering it."""
    return st.session_state.get(f"{key}__value") or default


def topnav(active: str) -> None:
    """rotrends-style top navigation (the sidebar only holds filters)."""
    items = [("platforms", "Platforms", "🏁"), ("games", "Games", "🎮"), ("creators", "Creators", "🧑‍🎨"), ("releases", "Releases", "🚀"), ("health", "Health", "🩺")]
    with st.container(horizontal=True, gap="small"):  # stays one row on phones (st.columns would stack)
        for slug, label, icon in items:
            st.page_link(f"views/{slug}.py", label=label, icon=icon, width="content")
    st.markdown("<div style='border-bottom:1px solid var(--border);margin:-6px 0 14px 0'></div>", unsafe_allow_html=True)
