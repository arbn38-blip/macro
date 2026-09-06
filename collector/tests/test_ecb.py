from datetime import date
from pathlib import Path

import pytest

from collector.fetchers.ecb import fetch_series, parse_csv

FIXTURE = (Path(__file__).parent / "fixtures" / "ecb_3m.csv").read_text()


def test_parse_csv():
    points = parse_csv(FIXTURE)
    # csvdata quotes commas inside TITLE_COMPL; a naive comma-split would
    # misalign columns — the fixture is a real response, so this covers it
    assert points[0] == (date(2026, 7, 20), 2.3299925919)
    assert points[-1] == (date(2026, 7, 22), 2.3337121399)
    assert [d for d, _ in points] == sorted(d for d, _ in points)


def test_parse_csv_rejects_empty_and_headerless():
    with pytest.raises(ValueError):
        parse_csv("")
    with pytest.raises(ValueError):
        parse_csv("KEY,FREQ\nYC.B,1\n")  # no TIME_PERIOD/OBS_VALUE columns
    header = FIXTURE.splitlines()[0]
    with pytest.raises(ValueError):
        parse_csv(header + "\n")  # header only, no usable rows


async def test_fetch_series_splits_flow_from_key():
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen["url"], seen["params"] = url, params
        return FIXTURE

    points = await fetch_series("YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M", fake_get)
    assert seen["url"].endswith("/data/YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M")
    assert seen["params"]["format"] == "csvdata"
    assert len(points) == 3
