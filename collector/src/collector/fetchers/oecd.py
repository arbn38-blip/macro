"""OECD SDMX data API (keyless) — CLI / confidence indicators for cycle series.

A series ref is "{flow}/{key}" appended to the rest/data base, e.g.
"OECD.SDD.STES,DSD_STES@DF_CLI,4.1/USA.M.LI...AA...H". The csvfilewithlabels
format carries values in TIME_PERIOD / OBS_VALUE columns.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from collector.http import GetText

BASE = "https://sdmx.oecd.org/public/rest/data"


def parse_csv(text: str) -> list[tuple[date, float]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        period, value = row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if not period or not value:
            continue
        try:
            parts = [int(p) for p in period.split("-")]
            d = date(parts[0], parts[1] if len(parts) > 1 else 1, 1)
            out.append((d, float(value)))
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda p: p[0])
    return out


async def fetch_series(oecd_ref: str, get_text: GetText) -> list[tuple[date, float]]:
    text = await get_text(
        f"{BASE}/{oecd_ref}",
        params={"startPeriod": "1990-01", "format": "csvfilewithlabels"},
    )
    pts = parse_csv(text)
    if not pts:
        raise ValueError(f"oecd series {oecd_ref} contained no usable points")
    return pts
