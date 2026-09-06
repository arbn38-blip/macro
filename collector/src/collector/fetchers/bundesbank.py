"""Deutsche Bundesbank SDMX REST: daily Bund yields (BBSIS dataset), keyless."""
from __future__ import annotations

from datetime import date

from collector.http import GetText

BASE = "https://api.statistiken.bundesbank.de/rest/data/BBSIS/"


def parse_csv(text: str) -> list[tuple[date, float]]:
    out = []
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 2:
            continue
        try:
            d = date.fromisoformat(parts[0].strip())
            v = float(parts[1].strip().replace(",", "."))
        except ValueError:
            continue  # metadata header rows and non-trading-day markers
        out.append((d, v))
    if not out:
        raise ValueError("bundesbank CSV contained no usable rows")
    out.sort(key=lambda p: p[0])
    return out


async def fetch_series(series: str, get_text: GetText) -> list[tuple[date, float]]:
    url = f"{BASE}{series}"
    return parse_csv(await get_text(url, params={"format": "csv", "lastNObservations": "400"}))
