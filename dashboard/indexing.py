"""Activity index: one number across platforms that count plays differently.

Raw plays are not comparable across platforms (a Tesana "play" and a CrazyGames "play" are counted by different rules,
and catalogue sizes differ by 1000x). So each platform is compared only with itself:

  1. daily plays per platform (finished days only), smoothed with a trailing 7-day mean (removes weekday swings);
  2. each platform's smoothed series is divided by its own value on the base day and multiplied by 100;
  3. Momentum = chained equal-weighted mean of each platform's day-over-day change (is the typical platform growing?);
     Volume   = index of the summed smoothed plays (is total activity growing? big platforms weigh more).

A platform joins the composite only once it has a base value; later joiners are chained in at 100 relative to the
composite on their first day, so a platform starting mid-range does not create a jump.
"""

from __future__ import annotations

import pandas as pd

WINDOW = 7


def smooth(daily: pd.DataFrame, window: int = WINDOW) -> pd.DataFrame:
    """daily: columns day, source, plays (finished days). Returns wide frame (index day, column per source) of the
    trailing `window`-day mean; a value needs at least `window` - 2 observed days in its window."""
    if daily.empty:
        return pd.DataFrame()
    wide = daily.pivot_table(index="day", columns="source", values="plays", aggfunc="sum").sort_index()
    wide.index = pd.to_datetime(wide.index)
    wide = wide.asfreq("D")
    return wide.rolling(window, min_periods=max(1, window - 2)).mean()


def platform_indexes(sm: pd.DataFrame) -> pd.DataFrame:
    """Each column divided by its first positive value ×100."""
    out = {}
    for col in sm.columns:
        s = sm[col]
        base = s[s > 0]
        if base.empty:
            continue
        out[col] = s / base.iloc[0] * 100
    return pd.DataFrame(out, index=sm.index)


def composite(sm: pd.DataFrame) -> pd.DataFrame:
    """Return frame (index day) with volume and momentum, both chained so platforms joining later add no jump."""
    idx = platform_indexes(sm)
    if idx.empty:
        return pd.DataFrame(columns=["volume", "momentum", "platforms"])
    vol, mom, n = [], [], []
    v_level = m_level = 100.0
    prev = None
    for day in idx.index:
        if prev is not None:
            both = [c for c in idx.columns if pd.notna(sm.at[prev, c]) and pd.notna(sm.at[day, c])
                    and sm.at[prev, c] > 0]
            if both:  # chain on the platforms present on both days
                v_level *= sm.loc[day, both].sum() / sm.loc[prev, both].sum()
                m_level *= float((sm.loc[day, both] / sm.loc[prev, both]).mean())
        live = int(sm.loc[day].gt(0).sum())
        vol.append(v_level if live else None)
        mom.append(m_level if live else None)
        n.append(live)
        if live:
            prev = day
    return pd.DataFrame({"volume": vol, "momentum": mom, "platforms": n}, index=idx.index)


def rebase(df: pd.DataFrame, start) -> pd.DataFrame:
    """Rescale every column so the first non-null value on/after `start` is 100."""
    part = df[df.index >= pd.Timestamp(start)]
    out = {}
    for col in part.columns:
        s = part[col].dropna()
        if s.empty or not s.iloc[0]:
            continue
        out[col] = part[col] / s.iloc[0] * 100
    return pd.DataFrame(out, index=part.index)
