"""ECB Data Portal SDMX REST: daily euro-area yield-curve rates, keyless.

Config stores the full SDMX key including the dataflow prefix
("YC.B.U2.EUR..."); the URL wants them split (/data/YC/B.U2.EUR...).
csvdata rows quote commas inside titles, so this parses with csv.reader,
not a naive split.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from collector.http import GetText

BASE = "https://data-api.ecb.europa.eu/service/data"


def parse_csv(text: str) -> list[tuple[date, float]]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("ecb CSV was empty")
    header = rows[0]
    try:
        t_col, v_col = header.index("TIME_PERIOD"), header.index("OBS_VALUE")
    except ValueError as exc:
        raise ValueError("ecb CSV missing TIME_PERIOD/OBS_VALUE columns") from exc
    out = []
    for row in rows[1:]:
        try:
            out.append((date.fromisoformat(row[t_col]), float(row[v_col])))
        except (ValueError, IndexError):
            continue  # blank observations and stray short rows
    if not out:
        raise ValueError("ecb CSV contained no usable rows")
    out.sort(key=lambda p: p[0])
    return out


async def fetch_series(series: str, get_text: GetText) -> list[tuple[date, float]]:
    flow, key = series.split(".", 1)
    return parse_csv(await get_text(
        f"{BASE}/{flow}/{key}",
        params={"format": "csvdata", "lastNObservations": "400"},
    ))
