"""Creator page — the vibez.club layout: name, handle buttons, stats line, three cards, game grid,
then outreach status + note (writes `notes`) and a merge control (writes `creator_merges`)."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from dashboard import queries as q, ui
from dashboard.components import game_grid, link_buttons, metric_card, share_donut
from dashboard.filters import param, range_bar, sidebar
from dashboard.theme import GAME_TONES, abbrev, color_map, full

f = sidebar()
ui.topnav("creators")
cid = param("creator")
if not cid:
    ui.page_header("Creator")
    st.info("Open a creator from the Creators page.")
    st.stop()
c = q.creator(int(cid))
if not c:
    st.error("Creator not found.")
    st.stop()

family = q.creator_family(c["canonical_key"]) if c.get("canonical_key") else pd.DataFrame()
ids = tuple(int(i) for i in family["id"]) if not family.empty else (int(cid),)
colors = color_map(list(f.sources["name"]))
names = f.names

# ---------------------------------------------------------------- header
links = dict(c.get("links") or {})
if not family.empty:
    for _, r in family.iterrows():
        for k, v in (r["links"] or {}).items():
            links.setdefault(k, v)
games = q.creator_games(ids, f.window_start)
cdaily = q.creator_daily(ids)
followers = family["followers"].sum(min_count=1) if not family.empty else c.get("followers")
total_plays = games["plays_total"].sum(min_count=1)
plays_win = games["plays_window"].sum(min_count=1)

hc1, hc2 = st.columns([1, 5])
with hc1:
    if c.get("avatar_url"):
        st.markdown(f"<img class='vt-art round' src='{c['avatar_url']}'/>", unsafe_allow_html=True)
    else:
        st.markdown(f"<div class='vt-thumb ph vt-art round' style='height:auto;font-size:2rem'>{(c.get('name') or '?')[:1].upper()}</div>", unsafe_allow_html=True)
with hc2:
    ui.page_header(c.get("name") or c["external_id"], None, eyebrow_text=names.get(c["source"], c["source"]), icon="👤")
    link_buttons(links, c.get("url"), names.get(c["source"], c["source"]))
    if c.get("bio"):
        st.caption(c["bio"][:300])
    ui.stats_line([(f"{len(games)}", "games"), (abbrev(total_plays), "lifetime plays"),
                   (abbrev(followers) if followers is not None and not pd.isna(followers) else "—", "followers")])
f = range_bar(f, show_cohort=False)

with st.container(border=True):
    a_, b_ = st.columns(2)
    with a_:
        ui.eyebrow(f"Plays, {f.window}", "🔥"); ui.hero(full(plays_win), "summed over the creator's tracked games")
    with b_:
        ui.eyebrow("Lifetime plays", "🏆"); ui.hero(full(total_plays), "the platform's own counters", white=True)

# ---------------------------------------------------------------- cards
daily = cdaily.groupby("day", as_index=False)["plays_delta"].sum(min_count=1).rename(columns={"plays_delta": "value"})
cum = daily.copy()
cum["value"] = cum["value"].fillna(0).cumsum()
c1, c2, c3 = st.columns(3)
with c1:
    metric_card("Daily plays (measured)", daily, f.anchor, "daily-peak", colors.get(c["source"]),
                note="day-over-day increase, summed across the creator's games")
with c2:
    if followers is not None and not pd.isna(followers):
        metric_card("Followers", pd.DataFrame({"day": [pd.Timestamp.utcnow().date()], "value": [float(followers)]}), f.anchor, "cumulative",
                    colors.get(c["source"]), note="latest profile value (history starts now)")
    else:
        ccu = cdaily.groupby("day", as_index=False)["ccu_peak"].max().rename(columns={"ccu_peak": "value"})
        metric_card("Concurrent players (daily peak)", ccu, f.anchor, "daily-peak", colors.get(c["source"]),
                    note="this platform exposes no follower count")
with c3:
    if not family.empty and family["source"].nunique() > 1:
        by_plat = games.groupby("source")["plays_window"].sum(min_count=1).dropna()
        share_donut(f"Plays by platform, last {f.window}", {names.get(k, k): v for k, v in by_plat.items()},
                    colors={names.get(k, k): colors.get(k) for k in by_plat.index})
    else:
        top = games.dropna(subset=["plays_window"]).sort_values("plays_window", ascending=False)
        labels_ = [str(r["title"] or r["url"])[:30] for _, r in top.head(8).iterrows()]
        share_donut(f"Top game share, last {f.window}", {l: r["plays_window"] for l, (_, r) in zip(labels_, top.head(8).iterrows())},
                    colors={l: (GAME_TONES + ["#3a3f4b", "#2f343e", "#262a33"])[i] for i, l in enumerate(labels_)})

# ---------------------------------------------------------------- games
game_grid(games, names, f.window)

# ---------------------------------------------------------------- notes + merge
ui.title("Outreach")
key = c.get("canonical_key") or c["external_id"]
statuses = ["new", "watching", "contacted", "replied", "passed", "funded"]
with st.form("note_form"):
    a, b = st.columns([1, 3])
    status = a.selectbox("Status", statuses, index=statuses.index(c.get("outreach_status") or "new"))
    note = b.text_area("Note", value=c.get("note") or "", height=80, placeholder="contact, context, what they're building…")
    if st.form_submit_button("Save"):
        q.upsert_note(key, status, note)
        st.success("Saved.")
        st.rerun()

with st.expander("Merge with the same creator on another platform"):
    st.caption(f"Current identity key: `{key}`. Give two creators the same key to show them as one person (plays by platform donut, combined stats).")
    with st.form("merge_form"):
        new_key = st.text_input("Canonical key", value=key)
        mnote = st.text_input("Why (optional)", value="")
        if st.form_submit_button("Merge"):
            q.merge_creator(int(cid), new_key.strip().lower(), mnote or None)
            st.success("Merged. Reloading…")
            st.rerun()
    if not family.empty and len(family) > 1:
        st.dataframe(family[["display_name", "name", "url", "followers"]].rename(columns={"display_name": "Platform"}), hide_index=True, width='stretch')
