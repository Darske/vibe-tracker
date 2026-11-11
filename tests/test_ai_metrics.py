from datetime import date

import pandas as pd

from dashboard.ai_metrics import catalogue_ai, list_shares, new_games_split, plays_split


def test_plays_split_shares_and_negative_deltas_clipped():
    df = pd.DataFrame([
        {"day": date(2026, 11, 1), "ai": "ai", "plays": 30}, {"day": date(2026, 11, 1), "ai": "no_ai", "plays": 10},
        {"day": date(2026, 11, 1), "ai": "undeclared", "plays": 60},
        {"day": date(2026, 11, 2), "ai": "ai", "plays": -5}, {"day": date(2026, 11, 2), "ai": "undeclared", "plays": 50},
    ])
    w = plays_split(df)
    assert w.loc["2026-11-01", "ai_share"] == 0.3 and w.loc["2026-11-01", "ai_share_declared"] == 0.75
    assert w.loc["2026-11-02", "ai"] == 0 and w.loc["2026-11-02", "ai_share"] == 0
    assert pd.isna(w.loc["2026-11-02", "ai_share_declared"])


def test_list_shares_drop_single_kind_lists_and_add_all():
    df = pd.DataFrame([
        {"list_name": "hot", "day": date(2026, 11, 1), "ai": 10, "no_ai": 0, "slots": 100, "polls": 2},
        {"list_name": "new", "day": date(2026, 11, 1), "ai": 30, "no_ai": 0, "slots": 100, "polls": 2},
        {"list_name": "ai_popular", "day": date(2026, 11, 1), "ai": 72, "no_ai": 0, "slots": 72, "polls": 2},
    ])
    s = list_shares(df).set_index("list")
    assert set(s.index) == {"All lists", "Hot new games", "New & updated"}
    assert s.loc["All lists", "share"] == 0.2 and s.loc["Hot new games", "slots"] == 50


def test_list_shares_respects_allowed_lists():
    df = pd.DataFrame([
        {"list_name": "popular", "day": date(2026, 11, 1), "ai": 8, "no_ai": 0, "slots": 200, "polls": 2},
        {"list_name": "top_rated", "day": date(2026, 11, 1), "ai": 1, "no_ai": 0, "slots": 200, "polls": 2},
    ])
    s = list_shares(df, ["popular"])
    assert list(s["list"]) == ["Popular"] and s["share"].iloc[0] == 0.04


def test_catalogue_ai_new_only_over_consecutive_days():
    t = pd.DataFrame([
        {"day": date(2026, 11, 1), "games": 1000, "extra": {"ai_total": 100, "no_ai_total": 300}},
        {"day": date(2026, 11, 2), "games": 1010, "extra": {"ai_total": 90}},
        {"day": date(2026, 11, 4), "games": 1020, "extra": {"ai_total": 120}},
    ])
    c = catalogue_ai(t)
    assert c.loc["2026-11-02", "ai_new"] == -10 and pd.isna(c.loc["2026-11-04", "ai_new"])
    assert c.loc["2026-11-01", "ai_share"] == 0.1


def test_new_games_split():
    df = pd.DataFrame([{"week": date(2026, 11, 2), "ai": "ai", "games": 3}, {"week": date(2026, 11, 2), "ai": "undeclared", "games": 9}])
    w = new_games_split(df)
    assert w["total"].iloc[0] == 12 and w["ai_share"].iloc[0] == 0.25 and w["no_ai"].iloc[0] == 0


def test_new_ai_share_rolling_ratio():
    from dashboard.ai_metrics import new_ai_share
    days = pd.date_range("2026-11-01", periods=5).date
    t = pd.DataFrame({"day": days, "games": [1000, 1100, 1200, 1300, 1400],
                      "extra": [{"ai_total": a} for a in (100, 110, 130, 150, 170)]})
    s = new_ai_share(catalogue_ai(t), window=2)
    assert round(s.iloc[-1], 3) == 0.2 and round(s.iloc[0], 3) == 0.1


def test_ratings_split():
    from dashboard.ai_metrics import ratings_split
    df = pd.DataFrame([{"day": date(2026, 11, 1), "ai": "ai", "games": 5, "rating": 3.5},
                       {"day": date(2026, 11, 1), "ai": "undeclared", "games": 50, "rating": 4.1}])
    w = ratings_split(df)
    assert w["rating_ai"].iloc[0] == 3.5 and w["games_undeclared"].iloc[0] == 50
