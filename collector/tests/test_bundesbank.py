from datetime import date
from pathlib import Path

import pytest

from collector.fetchers.bundesbank import fetch_series, parse_csv

FIXTURE = (Path(__file__).parent / "fixtures" / "bundesbank_10y.csv").read_text()


def test_parse_csv():
    pts = parse_csv(FIXTURE)
    assert pts == [
        (date(2026, 7, 6), 3.07),
        (date(2026, 7, 7), 3.16),
        (date(2026, 7, 8), 3.17),
    ]


def test_parse_skips_metadata_header_and_non_numeric_rows():
    # real responses are BOM-prefixed; the BOM'd first metadata line must be
    # discarded like any other non-date row, not break parsing
    assert FIXTURE.startswith("﻿")
    # metadata lines (BBSIS;..., Title;..., Unit;...) and the non-trading-day
    # row (value field "."; "Kein Wert vorhanden" is a third field) must not appear
    pts = parse_csv(FIXTURE)
    assert len(pts) == 3
    assert date(2026, 7, 5) not in [d for d, _ in pts]


def test_parse_sorts_out_of_order_rows():
    shuffled = (
        "2026-07-08;3,17;\n"
        "2026-07-06;3,07;\n"
        "2026-07-07;3,16;\n"
    )
    pts = parse_csv(shuffled)
    assert [p[0] for p in pts] == [date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8)]


def test_parse_no_rows_raises():
    with pytest.raises(ValueError):
        parse_csv("BBSIS;foo\nTitle;bar\n")


async def test_fetch_series_builds_url_and_params():
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen["url"] = url
        seen["params"] = params
        return FIXTURE

    series = "D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"
    pts = await fetch_series(series, fake_get)
    assert seen["url"] == f"https://api.statistiken.bundesbank.de/rest/data/BBSIS/{series}"
    assert seen["params"] == {"format": "csv", "lastNObservations": "400"}
    assert len(pts) == 3
