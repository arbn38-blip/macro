from datetime import date
from pathlib import Path

from collector.fetchers.aaii import URL, fetch_spread, parse_sentiment

FIXTURE = (Path(__file__).parent / "fixtures" / "aaii_sentiment.xls").read_bytes()


def test_parse_sentiment_spread_scaled_to_points():
    assert parse_sentiment(FIXTURE) == [
        (date(2026, 8, 13), 6.0),   # (0.38 - 0.32) * 100
        (date(2026, 8, 20), 11.0),  # (0.41 - 0.30) * 100
    ]  # header + "Average" footer rows skipped


async def test_fetch_spread_hits_aaii_url():
    seen = {}

    async def fake_get_bytes(url, params=None, headers=None):
        seen["url"] = url
        return FIXTURE

    pts = await fetch_spread(fake_get_bytes)
    assert seen["url"] == URL
    assert len(pts) == 2
