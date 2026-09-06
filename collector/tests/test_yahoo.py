from datetime import date
from pathlib import Path

import pytest

from collector.fetchers import yahoo

YAHOO_SPX = (Path(__file__).parent / "fixtures" / "yahoo_spx.json").read_text()


def test_parse_chart_happy_path():
    quote = yahoo.parse_chart(YAHOO_SPX)
    assert len(quote.closes) == 2  # null close skipped
    assert quote.closes == sorted(quote.closes, key=lambda p: p[0])
    assert quote.closes[-1] == (date(2026, 7, 13), 6234.50)
    assert quote.last == 6240.10
    assert quote.last_ts.endswith("Z")


def test_parse_chart_raises_on_empty_result():
    with pytest.raises(ValueError):
        yahoo.parse_chart('{"chart":{"result":[{}]}}')


def test_parse_chart_null_result_raises_clear_error():
    text = (
        '{"chart": {"result": null, "error": {"code": "Not Found",'
        ' "description": "No data found, symbol may be delisted"}}}'
    )
    with pytest.raises(ValueError, match="symbol may be delisted"):
        yahoo.parse_chart(text)


async def test_fetch_chart_sends_range_param_and_default_ua():
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen["url"] = url
        seen["params"] = params
        seen["headers"] = headers
        return YAHOO_SPX

    quote = await yahoo.fetch_chart("^GSPC", fake_get)
    assert quote.last == 6240.10
    assert seen["params"]["range"] == "1y"
    # no per-fetcher header override: http.get_text applies the honest default
    assert seen["headers"] is None
    assert "^GSPC" in seen["url"] or "%5EGSPC" in seen["url"]


async def test_fetch_chart_range_param():
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen["params"] = params
        return YAHOO_SPX

    await yahoo.fetch_chart("^GSPC", fake_get, range_="10y")
    assert seen["params"]["range"] == "10y"


def test_ratio_points_aligns_common_dates():
    a = [(date(2026, 8, 20), 10.0), (date(2026, 8, 21), 12.0), (date(2026, 8, 24), 14.0)]
    b = [(date(2026, 8, 21), 4.0), (date(2026, 8, 24), 7.0), (date(2026, 8, 25), 8.0)]
    assert yahoo.ratio_points(a, b) == [(date(2026, 8, 21), 3.0), (date(2026, 8, 24), 2.0)]


def test_ratio_points_empty_intersection_or_zero_denominator():
    a = [(date(2026, 8, 20), 10.0)]
    assert yahoo.ratio_points(a, [(date(2026, 8, 21), 4.0)]) == []
    assert yahoo.ratio_points(a, [(date(2026, 8, 20), 0.0)]) == []
