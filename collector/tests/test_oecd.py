from datetime import date
from pathlib import Path

from collector.fetchers.oecd import fetch_series, parse_csv

FIXTURE = (Path(__file__).parent / "fixtures" / "oecd_cli.csv").read_text()


def test_parse_csv_monthly_dates_and_blank_skip():
    assert parse_csv(FIXTURE) == [(date(2024, 7, 1), 99.39526)]  # blank 2024-06 skipped


def test_parse_csv_no_data_rows():
    header = FIXTURE.splitlines()[0]
    assert parse_csv(header + "\n") == []


async def test_fetch_series_builds_url():
    seen = {}

    async def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return FIXTURE

    ref = "OECD.SDD.STES,DSD_STES@DF_CLI,4.1/USA.M.LI...AA...H"
    pts = await fetch_series(ref, fake_get)
    assert seen["url"] == f"https://sdmx.oecd.org/public/rest/data/{ref}"
    assert seen["params"] == {"startPeriod": "1990-01", "format": "csvfilewithlabels"}
    assert pts == [(date(2024, 7, 1), 99.39526)]
