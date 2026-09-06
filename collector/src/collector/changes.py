"""Pure change math. No I/O, no clock access — everything injected."""
from __future__ import annotations

from datetime import date, timedelta

Points = dict[date, float]


def pct_change(last: float, ref: float | None) -> float | None:
    if ref is None or ref == 0:
        return None
    return round((last / ref - 1.0) * 100, 2)


def bp_move(current_pct: float, ref_pct: float | None) -> int | None:
    if ref_pct is None:
        return None
    return round((current_pct - ref_pct) * 100)


def ref_close(closes: Points, asof: date, horizon: str) -> float | None:
    """Reference close for a change horizon.

    Selection rule: latest close ON OR BEFORE the target date, so weekends and
    holidays resolve to the prior trading day. '1d' is strictly before asof.
    """
    if not closes:
        return None
    dates = sorted(closes)

    def latest_on_or_before(target: date) -> float | None:
        prior = [dt for dt in dates if dt <= target]
        return closes[prior[-1]] if prior else None

    if horizon == "1d":
        prior = [dt for dt in dates if dt < asof]
        return closes[prior[-1]] if prior else None
    if horizon == "1w":
        return latest_on_or_before(asof - timedelta(days=7))
    if horizon == "1m":
        return latest_on_or_before(asof - timedelta(days=30))
    if horizon == "1y":
        return latest_on_or_before(asof - timedelta(days=365))
    if horizon == "ytd":
        return latest_on_or_before(date(asof.year - 1, 12, 31))
    raise ValueError(f"unknown horizon: {horizon}")


def to_bands(points: Points) -> list[tuple[date, date]]:
    """Contiguous runs of value==1 (NBER USREC) -> (start, end) band pairs.

    End = first 0-date after the run, or the last observation while still
    inside one (an open recession shades up to the newest data point).
    """
    bands: list[tuple[date, date]] = []
    start: date | None = None
    ordered = sorted(points)
    for dt in ordered:
        if points[dt] == 1 and start is None:
            start = dt
        elif points[dt] != 1 and start is not None:
            bands.append((start, dt))
            start = None
    if start is not None:
        bands.append((start, ordered[-1]))
    return bands


def apply_transform(points: Points, kind: str) -> Points:
    """Transforms for macro series charts.

    none: raw values. diff: change vs previous observation (NFP-style).
    pct_prev: % change vs previous observation. yoy: % change vs the
    observation 12 months earlier (same day-of-month, monthly series).
    """
    if kind == "none":
        return dict(points)
    ordered = sorted(points)
    out: Points = {}
    if kind in ("diff", "pct_prev"):
        for prev_d, cur_d in zip(ordered, ordered[1:]):
            prev, cur = points[prev_d], points[cur_d]
            if kind == "diff":
                out[cur_d] = round(cur - prev, 2)
            elif prev != 0:
                out[cur_d] = round((cur / prev - 1.0) * 100, 2)
        return out
    if kind == "yoy":
        for dt in ordered:
            try:
                prev = points.get(date(dt.year - 1, dt.month, dt.day))
            except ValueError:  # Feb 29
                prev = None
            if prev:
                out[dt] = round((points[dt] / prev - 1.0) * 100, 2)
        return out
    raise ValueError(f"unknown transform: {kind}")
