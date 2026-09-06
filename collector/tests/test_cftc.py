from datetime import date
from pathlib import Path

from collector.fetchers.cftc import fetch_net_noncommercial, parse_reports

FIXTURE = (Path(__file__).parent / "fixtures" / "cftc_vix.json").read_text()


def test_parse_reports_net_position_and_bad_row_skip():
    assert parse_reports(FIXTURE) == [
        (date(2026, 8, 11), -50766.0),  # 61234 - 112000
        (date(2026, 8, 18), 10000.0),
        # malformed third row skipped
    ]


async def test_fetch_net_noncommercial_filters_by_code():
    seen = {}

    async def fake_get(url, params=None):
        seen["url"] = url
        seen["params"] = params
        return FIXTURE

    pts = await fetch_net_noncommercial("1170E1", fake_get)
    assert seen["url"] == "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    assert seen["params"]["cftc_contract_market_code"] == "1170E1"
    assert seen["params"]["$order"] == "report_date_as_yyyy_mm_dd"
    assert seen["params"]["$limit"] == "5000"
    assert len(pts) == 2
