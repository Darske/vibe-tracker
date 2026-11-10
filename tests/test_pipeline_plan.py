"""Run planning: stalest platform first, so a time budget that runs out never starves the same platforms."""

from datetime import datetime, timedelta, timezone

from tracker.pipeline import order_by_staleness

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def test_stalest_first_and_never_run_before_all():
    names = ["tesana", "rosebud", "astrocade", "remix", "playgama"]
    last = {"tesana": NOW - timedelta(hours=1), "rosebud": NOW - timedelta(hours=2), "astrocade": NOW - timedelta(hours=1),
            "remix": NOW - timedelta(days=5), "playgama": None}
    assert order_by_staleness(names, last) == ["playgama", "remix", "rosebud", "tesana", "astrocade"]


def test_ties_keep_registry_order():
    names = ["a", "b", "c"]
    assert order_by_staleness(names, {}) == ["a", "b", "c"]


def test_platforms_skipped_by_budget_move_to_the_front_next_run():
    # Run 1 covers the first three; the last two were cut by the budget and keep their old timestamps.
    names = ["tesana", "rosebud", "astrocade", "wavedash", "remix"]
    old = NOW - timedelta(days=5)
    after_run1 = {"tesana": NOW, "rosebud": NOW, "astrocade": NOW, "wavedash": old, "remix": old}
    assert order_by_staleness(names, after_run1)[:2] == ["wavedash", "remix"]
