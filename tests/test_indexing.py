import pandas as pd

from dashboard.indexing import composite, rebase, smooth


def frame(series: dict[str, list[float]], start="2026-10-01"):
    rows = []
    for src, vals in series.items():
        for i, v in enumerate(vals):
            if v is not None:
                rows.append({"day": pd.Timestamp(start) + pd.Timedelta(days=i), "source": src, "plays": v})
    return pd.DataFrame(rows)


def test_flat_platforms_stay_at_100_regardless_of_size():
    sm = smooth(frame({"big": [1e6] * 20, "small": [10] * 20}))
    c = composite(sm).dropna()
    assert (c["volume"].round(6) == 100).all() and (c["momentum"].round(6) == 100).all()


def test_momentum_equal_weighted_volume_size_weighted():
    big = [1000.0] * 20
    small = [10.0] * 10 + [20.0] * 10  # small platform doubles
    c = composite(smooth(frame({"big": big, "small": small}), window=1))
    last = c.iloc[-1]
    assert round(last["momentum"]) == 150  # (1 + 2) / 2
    assert round(last["volume"], 1) == round(1020 / 1010 * 100, 1)


def test_late_joiner_adds_no_jump():
    a = [100.0] * 20
    b = [None] * 10 + [5000.0] * 10
    c = composite(smooth(frame({"a": a, "b": b}), window=1))
    assert (c["volume"].round(6) == 100).all() and (c["momentum"].round(6) == 100).all()
    assert c["platforms"].iloc[-1] == 2


def test_rebase_sets_start_to_100():
    df = pd.DataFrame({"x": [50.0, 100.0, 200.0]}, index=pd.date_range("2026-10-01", periods=3))
    r = rebase(df, "2026-10-02")
    assert list(r["x"]) == [100.0, 200.0]
