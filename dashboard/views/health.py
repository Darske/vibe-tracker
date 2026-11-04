"""Health: last run per platform, days of history, DB size, Actions minutes, rows per run over time.
A platform with a failed or zero-row run in the last 6 hours is highlighted. This page is how a
broken adapter gets noticed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard import queries as q, ui
from dashboard.theme import INK, STATUS, abbrev, color_map

ui.topnav("health")
ui.page_header("Health", "Last run per platform, history depth, database size, Actions minutes. A broken adapter shows up here first.", eyebrow_text="System", icon="🩺")

try:
    last = q.last_runs()
    stats = q.health_stats()
    srcs = q.sources()
except Exception as e:  # noqa: BLE001
    st.error(f"Cannot read the database: {e}")
    st.stop()

now = datetime.now(timezone.utc)
colors = color_map(list(srcs["name"]))

# ---------------------------------------------------------------- headline tiles
first_day = stats.get("first_day")
days_hist = (now.date() - first_day).days + 1 if first_day else 0
minutes_month = float(stats.get("seconds_this_month") or 0) / 60
avg_run = float(stats.get("avg_run_seconds_7d") or 0)

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    ui.tile("Days of history", f"{days_hist}", f"since {first_day:%b %-d}" if first_day else None)
with c2:
    ui.tile("Games tracked", abbrev(stats.get("games")))
with c3:
    ui.tile("Snapshot rows", abbrev(stats.get("snapshot_rows")))
with c4:
    ui.tile("Database size", f"{float(stats.get('db_bytes') or 0) / 1e6:.0f} MB", "Supabase free tier: 500 MB")
with c5:
    ui.tile("Actions minutes", f"{minutes_month:.0f}", "this month · public repo: unlimited")
if avg_run > 360:
    st.warning(f"Average run time over the last 7 days is {avg_run / 60:.1f} min — above the 6-minute line. "
               "Consider a public repo (unlimited minutes) or the 3-hour cron.", icon="⏱️")
else:
    st.caption(f"7-day average run time: {avg_run / 60:.1f} min per run (warn above 6).")

last_ccu = stats.get("last_ccu_reading")
if last_ccu is not None and not pd.isna(last_ccu):
    age_min = (now - pd.Timestamp(last_ccu).to_pydatetime().astimezone(timezone.utc)).total_seconds() / 60
    st.caption(f"Players-online poll: last reading {age_min:.0f} min ago · {int(stats.get('ccu_polls_24h') or 0)} readings in the last 24 h "
               f"(target 48: the ccu workflow runs every 30 min for Tesana, Spawn, Bitmagic, Wavedash).")

# ---------------------------------------------------------------- last run per platform
ui.title("Last run per platform")
if last.empty:
    st.info("No runs yet. Trigger the scrape workflow once from the Actions tab.")
    st.stop()

drops = q.row_drop_warnings()
drop_set = set(drops["source"]) if not drops.empty else set()


def _state(row) -> tuple[str, str]:
    age = now - row["run_ts"].to_pydatetime().astimezone(timezone.utc)
    if row["status"] in ("failed", "zero_rows") and age < timedelta(hours=6):
        return "critical", f"{row['status']} {int(age.total_seconds() // 60)} min ago"
    if row["source"] in drop_set:
        return "warning", "rows dropped >50% vs 7-day median"
    if age > timedelta(hours=max(6, 2 * int(srcs.set_index('name').loc[row['source'], 'poll_every_hours']))):
        return "warning", f"no run for {age.total_seconds() / 3600:.0f} h"
    if row["status"] == "partial":
        return "warning", "partial (time budget)"
    return "good", "ok"


rows = []
for _, r in last.iterrows():
    state, why = _state(r)
    rows.append({
        "state": state, "Platform": r["display_name"], "Status": why,
        "Last run (UTC)": r["run_ts"].strftime("%Y-%m-%d %H:%M"), "Discovered": r["rows_discovered"],
        "Snapshotted": r["rows_snapshotted"], "Gone": r["rows_gone"], "Seconds": float(r["seconds"] or 0),
        "Requests": r["requests"], "Errors": "; ".join(map(str, r["errors"] or [])),
    })
table = pd.DataFrame(rows)
icons = {"good": "✅", "warning": "⚠️", "critical": "🔴"}
table.insert(0, "", table.pop("state").map(icons))
mobile = st.toggle("All columns", value=False, help="Phone view shows 6 columns by default")
cols = list(table.columns) if mobile else ["", "Platform", "Status", "Last run (UTC)", "Snapshotted", "Seconds"]
st.dataframe(table[cols], hide_index=True, width='stretch')

bad = table[table[""] == "🔴"]
if not bad.empty:
    st.error("Failed or zero-row runs in the last 6 hours: " + ", ".join(bad["Platform"]), icon="🔴")

# ---------------------------------------------------------------- rows per run over time
ui.title("Rows snapshotted per run")
hist = q.runs_history(14)
if not hist.empty:
    fig = go.Figure()
    for src, g in hist.groupby("source", sort=False):
        fig.add_trace(go.Scatter(
            x=g["run_ts"], y=g["rows_snapshotted"], mode="lines+markers", name=g["display_name"].iloc[0], marker=dict(size=5),
            line=dict(width=2, color=colors.get(src, INK["muted"])),
            hovertemplate="%{y} rows<extra>%{fullData.name}</extra>",
        ))
    failed = hist[hist["status"].isin(["failed", "zero_rows"])]
    if not failed.empty:
        fig.add_trace(go.Scatter(
            x=failed["run_ts"], y=failed["rows_snapshotted"], mode="markers", name="failed / zero rows",
            marker=dict(size=9, color=STATUS["critical"], symbol="x"), hovertemplate="%{x}<extra>failed</extra>",
        ))
    fig.update_layout(height=320, yaxis_title=None, xaxis_title=None)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
    st.caption("One line per platform, last 14 days. A dip to zero means the adapter found the lists but no counters; a gap means the run did not happen.")

    ui.title("Seconds per run")
    fig2 = go.Figure()
    for src, g in hist.groupby("source", sort=False):
        fig2.add_trace(go.Scatter(x=g["run_ts"], y=g["seconds"], mode="lines+markers", name=g["display_name"].iloc[0], marker=dict(size=5),
                                  line=dict(width=2, color=colors.get(src, INK["muted"])),
                                  hovertemplate="%{y:.0f} s<extra>%{fullData.name}</extra>"))
    fig2.update_layout(height=260)
    st.plotly_chart(fig2, use_container_width=True, config={"displayModeBar": False})

with st.expander("Metric labels per platform (metric_map)"):
    for _, s in srcs.iterrows():
        mm = s["metric_map"] or {}
        st.markdown(f"**{s['display_name']}** · {s['segment']}" + "".join(f"\n- {v}" for v in mm.values()))
