from datetime import date

from collector.changes import apply_transform, bp_move, pct_change, ref_close, to_bands


def d(s: str) -> date:
    return date.fromisoformat(s)


CLOSES = {
    # Mon 2026-06-29 .. Fri 2026-07-03, then Mon 2026-07-06 .. Wed 2026-07-08
    d("2026-06-29"): 100.0,
    d("2026-06-30"): 101.0,
    d("2026-07-01"): 102.0,
    d("2026-07-02"): 103.0,
    d("2026-07-03"): 104.0,
    d("2026-07-06"): 105.0,
    d("2026-07-07"): 106.0,
    d("2026-07-08"): 107.0,
}


def test_pct_change_basic():
    assert pct_change(110.0, 100.0) == 10.0
    assert pct_change(95.0, 100.0) == -5.0


def test_pct_change_no_ref():
    assert pct_change(110.0, None) is None
    assert pct_change(110.0, 0.0) is None


def test_bp_move():
    assert bp_move(4.12, 4.15) == -3
    assert bp_move(4.12, None) is None


def test_ref_close_1d_skips_same_day():
    # asof Wed 8th -> previous close is Tue 7th
    assert ref_close(CLOSES, d("2026-07-08"), "1d") == 106.0


def test_ref_close_1d_over_weekend():
    # asof Mon 6th -> previous close is Fri 3rd
    assert ref_close(CLOSES, d("2026-07-06"), "1d") == 104.0


def test_ref_close_1w_lands_on_weekend_takes_prior_trading_day():
    # asof Wed 8th minus 7d = Wed 1st -> exact match
    assert ref_close(CLOSES, d("2026-07-08"), "1w") == 102.0
    # asof Mon 6th minus 7d = Mon Jun 29 -> exact match
    assert ref_close(CLOSES, d("2026-07-06"), "1w") == 100.0


def test_ref_close_ytd_uses_last_close_of_prior_year():
    closes = dict(CLOSES)
    closes[d("2025-12-30")] = 90.0  # Dec 31 2025 not a trading day here
    assert ref_close(closes, d("2026-07-08"), "ytd") == 90.0


def test_ref_close_missing_history_returns_none():
    assert ref_close(CLOSES, d("2026-07-08"), "1y") is None
    assert ref_close({}, d("2026-07-08"), "1d") is None


def test_transform_none_and_diff_and_pct_prev():
    pts = {d("2026-01-01"): 100.0, d("2026-02-01"): 102.0, d("2026-03-01"): 104.04}
    assert apply_transform(pts, "none") == pts
    assert apply_transform(pts, "diff") == {d("2026-02-01"): 2.0, d("2026-03-01"): 2.04}
    assert apply_transform(pts, "pct_prev") == {d("2026-02-01"): 2.0, d("2026-03-01"): 2.0}


def test_transform_yoy():
    pts = {
        d("2025-06-01"): 100.0,
        d("2025-07-01"): 100.5,
        d("2026-06-01"): 103.0,
        d("2026-07-01"): 103.5,
    }
    out = apply_transform(pts, "yoy")
    assert out == {d("2026-06-01"): 3.0, d("2026-07-01"): 2.99}


def test_ref_close_1m_uses_latest_on_or_before_30d():
    closes = {date(2026, 7, 20): 10.0, date(2026, 7, 24): 11.0, date(2026, 8, 20): 12.0}
    assert ref_close(closes, date(2026, 8, 24), "1m") == 11.0  # 8/24-30d = 7/25 -> 7/24


def test_to_bands_pairs_runs_of_ones():
    pts = {date(2026, 1, 1): 0.0, date(2026, 2, 1): 1.0, date(2026, 3, 1): 1.0,
           date(2026, 4, 1): 0.0, date(2026, 5, 1): 1.0}
    assert to_bands(pts) == [
        (date(2026, 2, 1), date(2026, 4, 1)),
        (date(2026, 5, 1), date(2026, 5, 1)),  # still-open run ends at last obs
    ]


def test_to_bands_empty_and_all_zero():
    assert to_bands({}) == []
    assert to_bands({date(2026, 1, 1): 0.0}) == []
