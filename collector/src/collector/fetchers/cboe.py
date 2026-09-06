"""CBOE daily market statistics — put/call ratios (keyless CDN JSON).

The endpoint is per-day, so history accumulates: each run walks the last
`days` weekdays and upserts what it finds. Holidays/missing days raise on
the CDN (403/404) and are skipped silently — only a fully-empty walk raises.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from collector.http import GetText

BASE = "https://cdn.cboe.com/data/us/options/market_statistics/daily"
DEFAULT_DAYS = 30


def parse_daily(text: str, ratio_name: str) -> float:
    for ratio in json.loads(text).get("ratios") or []:
        if ratio.get("name") == ratio_name:
            return float(ratio["value"])
    raise ValueError(f"cboe daily stats missing ratio {ratio_name!r}")


async def fetch_ratio_history(
    ratio_name: str, get_text: GetText, days: int = DEFAULT_DAYS, today: date | None = None
) -> list[tuple[date, float]]:
    today = today or date.today()
    out: list[tuple[date, float]] = []
    for back in range(days):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:  # no stats published on weekends
            continue
        try:
            out.append((d, parse_daily(await get_text(f"{BASE}/{d.isoformat()}_daily_options"), ratio_name)))
        except Exception:  # noqa: BLE001 — holiday or not-yet-published day
            continue
    if not out:
        raise ValueError(f"cboe returned no usable days for {ratio_name!r}")
    out.sort(key=lambda p: p[0])
    return out
