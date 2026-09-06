"""CFTC COT via the Socrata public reporting API (keyless).

Dataset 6dca-aqww = legacy futures-only report; net non-commercial
positioning = long - short, weekly. $limit=5000 covers ~30y of Tuesdays.
"""
from __future__ import annotations

import json
from datetime import datetime, date

from collector.http import GetText

BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"


def parse_reports(text: str) -> list[tuple[date, float]]:
    out = []
    for row in json.loads(text):
        try:
            d = datetime.fromisoformat(row["report_date_as_yyyy_mm_dd"]).date()
            net = float(row["noncomm_positions_long_all"]) - float(row["noncomm_positions_short_all"])
        except (KeyError, TypeError, ValueError):
            continue  # a malformed row must not fail the series
        out.append((d, net))
    if not out:
        raise ValueError("cftc payload contained no usable reports")
    out.sort(key=lambda p: p[0])
    return out


async def fetch_net_noncommercial(code: str, get_text: GetText) -> list[tuple[date, float]]:
    return parse_reports(await get_text(BASE, params={
        "cftc_contract_market_code": code,
        "$select": "report_date_as_yyyy_mm_dd,noncomm_positions_long_all,noncomm_positions_short_all",
        "$order": "report_date_as_yyyy_mm_dd",
        "$limit": "5000",
    }))
