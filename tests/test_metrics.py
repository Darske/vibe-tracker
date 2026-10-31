from datetime import date, datetime, timezone

from tracker import metrics
from tracker.metrics import Capture, daily_rows, growth, acceleration, rolling_7d


def ts(day: int, hour: int = 12, month: int = 1) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=timezone.utc)


def test_first_capture_is_null_not_zero():
    rows = daily_rows([Capture(ts(1), plays_total=100)])
    assert rows[date(2026, 1, 1)].plays_delta is None
    assert rows[date(2026, 1, 1)].plays_total_eod == 100
    assert rows[date(2026, 1, 1)].captures == 1


def test_normal_consecutive_days():
    rows = daily_rows([Capture(ts(1), 100), Capture(ts(2), 150), Capture(ts(3), 160)])
    assert rows[date(2026, 1, 2)].plays_delta == 50
    assert rows[date(2026, 1, 3)].plays_delta == 10
    assert rows[date(2026, 1, 3)].gap_days is None
    assert not rows[date(2026, 1, 3)].reset_flag


def test_several_polls_collapse_to_one_daily_delta_and_ccu():
    caps = [
        Capture(ts(1, 22), 100, players_now=5),
        Capture(ts(2, 2), 110, players_now=10),
        Capture(ts(2, 10), 130, players_now=30),
        Capture(ts(2, 22), 170, players_now=20),
    ]
    rows = daily_rows(caps)
    d2 = rows[date(2026, 1, 2)]
    assert d2.plays_delta == 70  # 170 - 100 (last of previous capture day)
    assert d2.captures == 3
    assert d2.ccu_peak == 30
    assert d2.ccu_avg == 20.0
    assert rows[date(2026, 1, 1)].ccu_peak == 5


def test_three_day_gap_split_evenly():
    rows = daily_rows([Capture(ts(1), 100), Capture(ts(4), 190)])
    assert rows[date(2026, 1, 2)].plays_delta == 30
    assert rows[date(2026, 1, 3)].plays_delta == 30
    assert rows[date(2026, 1, 4)].plays_delta == 30
    assert rows[date(2026, 1, 4)].gap_days == 3
    assert rows[date(2026, 1, 2)].gap_days == 3
    assert rows[date(2026, 1, 2)].captures == 0


def test_gap_remainder_lands_on_last_day_and_sums():
    rows = daily_rows([Capture(ts(1), 0), Capture(ts(4), 100)])
    parts = [rows[date(2026, 1, d)].plays_delta for d in (2, 3, 4)]
    assert sum(parts) == 100
    assert parts == [33, 33, 34]


def test_counter_reset_is_null_plus_flag():
    rows = daily_rows([Capture(ts(1), 500), Capture(ts(2), 20)])
    d2 = rows[date(2026, 1, 2)]
    assert d2.plays_delta is None
    assert d2.reset_flag is True
    assert d2.plays_total_eod == 20


def test_missing_counter_gives_null_delta_without_flag():
    rows = daily_rows([Capture(ts(1), None, likes=3), Capture(ts(2), None, likes=7)])
    d2 = rows[date(2026, 1, 2)]
    assert d2.plays_delta is None and d2.reset_flag is False
    assert d2.likes_delta == 4


def test_ccu_null_when_platform_has_no_players_now():
    rows = daily_rows([Capture(ts(1), 1), Capture(ts(2), 2)])
    assert rows[date(2026, 1, 2)].ccu_peak is None and rows[date(2026, 1, 2)].ccu_avg is None


def test_rolling_and_growth_noise_floor():
    deltas = {date(2026, 1, d): 10 for d in range(1, 15)}  # 14 days × 10
    p7, prev7 = rolling_7d(deltas, date(2026, 1, 14))
    assert (p7, prev7) == (70, 70)
    assert growth(p7, prev7) is None  # prev7 < noise floor 100
    assert growth(300, 200, noise_floor=100) == 0.5
    assert growth(300, None) is None
    assert acceleration(0.5, 0.2) == 0.3
    assert acceleration(0.5, None) is None


def test_rolling_window_returns_none_with_no_data():
    assert rolling_7d({}, date(2026, 1, 1)) == (None, None)


def test_noise_floor_is_configurable():
    assert metrics.NOISE_FLOOR == 100  # default from env
