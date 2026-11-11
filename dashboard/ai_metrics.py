"""AI-game growth on legacy platforms: the pure calculations behind the platform page's "AI games" view.

Legacy platforms label AI games differently (or not at all), so there is no single cross-platform figure. What each
one supports (see AI_SOURCES) decides which charts its page shows.
"""

from __future__ import annotations

import pandas as pd

AI_SOURCES: dict[str, dict] = {
    "kongregate": {
        "how": "The developer's own AI declaration in the game description (Kongregate asks new uploads to declare AI use). "
               "Games that say nothing count as undeclared; most older catalogue games are undeclared.",
        "plays": True, "lists": None, "new_games": True, "catalogue": False, "ratings": True,
    },
    "itchio": {
        "how": "The creator's AI disclosure on itch.io (games listed under AI-assisted / AI-generated code, or tagged No AI). "
               "Disclosure is optional for games, so roughly two thirds disclose nothing either way: AI shares are a floor.",
        "plays": False, "lists": ["popular"], "new_games": False, "catalogue": True, "ratings": False,
    },
}
NO_AI_LABEL = {
    "crazygames": "CrazyGames has no AI tag, category or field, so its AI games cannot be told apart.",
    "poki": "Poki allows AI-made games but does not label them, so its AI games cannot be told apart.",
}
# Lists that contain only AI (or only no-AI) games by construction: their AI share says nothing
SINGLE_KIND_LISTS = {"ai_popular", "ai_code", "no_ai"}
LIST_LABELS = {"popular": "Popular", "new_popular": "New & popular", "top_rated": "Top rated", "top_sellers": "Top sellers",
               "newest": "Newest", "hot": "Hot new games", "new": "New & updated"}


def plays_split(df: pd.DataFrame) -> pd.DataFrame:
    """ai_plays_daily rows → wide frame per day: ai, no_ai, undeclared plays, total, ai_share (of all plays) and
    ai_share_declared (of plays on games that declared either way)."""
    if df.empty:
        return pd.DataFrame(columns=["ai", "no_ai", "undeclared", "total", "ai_share", "ai_share_declared"])
    w = df.pivot_table(index="day", columns="ai", values="plays", aggfunc="sum").fillna(0)
    for c in ("ai", "no_ai", "undeclared"):
        if c not in w:
            w[c] = 0.0
    w = w[["ai", "no_ai", "undeclared"]].astype(float).clip(lower=0)
    w["total"] = w.sum(axis=1)
    w["ai_share"] = (w["ai"] / w["total"]).where(w["total"] > 0)
    declared = w["ai"] + w["no_ai"]
    w["ai_share_declared"] = (w["ai"] / declared).where(declared > 0)
    w.index = pd.to_datetime(w.index)
    return w.sort_index()


def list_shares(df: pd.DataFrame, lists: list[str] | None = None) -> pd.DataFrame:
    """ai_list_share rows → long frame (day, list, share) incl. an "All lists" row per day; single-kind lists dropped."""
    if df.empty:
        return pd.DataFrame(columns=["day", "list", "share", "slots"])
    d = df[~df["list_name"].isin(SINGLE_KIND_LISTS)]
    if lists:
        d = d[d["list_name"].isin(lists)]
    if d.empty:
        return pd.DataFrame(columns=["day", "list", "share", "slots"])
    per = d.assign(list=d["list_name"].map(lambda n: LIST_LABELS.get(n, n.replace("_", " ").replace("-", " ").capitalize())))
    per = per.groupby(["day", "list"], as_index=False)[["ai", "slots", "polls"]].sum()
    if per["list"].nunique() > 1:
        allr = per.groupby("day", as_index=False)[["ai", "slots", "polls"]].sum().assign(list="All lists")
        per = pd.concat([allr, per], ignore_index=True)
    per["share"] = per["ai"] / per["slots"]
    per["slots"] = per["slots"] / per["polls"]  # average slots per poll
    per["day"] = pd.to_datetime(per["day"])
    return per[["day", "list", "share", "slots"]].sort_values(["list", "day"])


def catalogue_ai(totals: pd.DataFrame) -> pd.DataFrame:
    """platform_totals rows (one platform) → per day: games, ai_total, no_ai_total, ai_share, ai_new (day-over-day
    change of ai_total over consecutive days only; negative when the platform removes AI games)."""
    if totals.empty:
        return pd.DataFrame(columns=["games", "ai_total", "no_ai_total", "ai_share", "ai_new"])
    t = totals.sort_values("day").copy()
    ex = t["extra"].map(lambda e: e if isinstance(e, dict) else {})
    t["ai_total"] = pd.to_numeric(ex.map(lambda e: e.get("ai_total")), errors="coerce")
    t["no_ai_total"] = pd.to_numeric(ex.map(lambda e: e.get("no_ai_total")), errors="coerce")
    t["games"] = pd.to_numeric(t["games"], errors="coerce")
    t["day"] = pd.to_datetime(t["day"])
    t = t.set_index("day")[["games", "ai_total", "no_ai_total"]]
    t["ai_share"] = (t["ai_total"] / t["games"]).where(t["games"] > 0)
    consecutive = t.index.to_series().diff().dt.days.eq(1)
    t["ai_new"] = t["ai_total"].diff().where(consecutive)
    t["games_new"] = t["games"].diff().where(consecutive)
    return t


def new_ai_share(cat: pd.DataFrame, window: int = 7) -> pd.Series:
    """Share of the catalogue's net new games that are AI-disclosed, over a trailing `window` of days:
    sum(Δ ai_total) / sum(Δ games). Days with removals (negative change) count as they are, so a purge pulls the
    share down for a week; NaN when the catalogue did not grow over the window."""
    if cat.empty or "games_new" not in cat:
        return pd.Series(dtype=float)
    d = cat[["ai_new", "games_new"]].dropna()
    if d.empty:
        return pd.Series(dtype=float)
    d = d.asfreq("D")
    r = d.rolling(window, min_periods=max(1, window // 2)).sum()
    return (r["ai_new"] / r["games_new"]).where(r["games_new"] > 0).dropna()


def ratings_split(df: pd.DataFrame) -> pd.DataFrame:
    """ai_ratings rows → wide frame per day with rating_<kind> and games_<kind> columns."""
    if df.empty:
        return pd.DataFrame()
    w = df.pivot_table(index="day", columns="ai", values=["rating", "games"], aggfunc="first")
    w.columns = [f"{a}_{b}" for a, b in w.columns]
    w.index = pd.to_datetime(w.index)
    return w.sort_index()


def new_games_split(df: pd.DataFrame) -> pd.DataFrame:
    """ai_new_games rows → per week: ai, no_ai, undeclared, total, ai_share (of all new games)."""
    if df.empty:
        return pd.DataFrame(columns=["ai", "no_ai", "undeclared", "total", "ai_share"])
    w = df.pivot_table(index="week", columns="ai", values="games", aggfunc="sum").fillna(0)
    for c in ("ai", "no_ai", "undeclared"):
        if c not in w:
            w[c] = 0
    w = w[["ai", "no_ai", "undeclared"]].astype(int)
    w["total"] = w.sum(axis=1)
    w["ai_share"] = (w["ai"] / w["total"]).where(w["total"] > 0)
    w.index = pd.to_datetime(w.index)
    return w.sort_index()
