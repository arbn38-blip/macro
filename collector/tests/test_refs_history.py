import json
from datetime import date
from pathlib import Path

import pytest

from collector.config import AaveRefCfg, FundingRefCfg, LlamaChartCfg, PendleRefCfg, RefsCfg
from collector.fetchers.refs_history import (
    daily_mean_annualized,
    fetch_refs_history,
    parse_llama_chart,
    parse_pendle_apy_history,
)
from collector.store import Store

FIX = Path(__file__).parent / "fixtures"
LLAMA_CHART = (FIX / "llama_chart.json").read_text()
PENDLE_APY_HISTORY = (FIX / "pendle_apy_history.json").read_text()
FUNDING_PAGE = json.loads((FIX / "binance_funding_page.json").read_text())

AAVE = AaveRefCfg(
    chain="BASE", rpc="https://mainnet.base.org",
    pool="0xa238dd80c259a72e81d7e4664a9801593f98d1c5",
    asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", symbol="USDC",
)
LLAMA = LlamaChartCfg(pool="7e0661bf-8cf3-45e6-9424-31916d4c7b84", series="aave-base-usdc-supply")
PENDLE = PendleRefCfg(
    chain_id=8453, address="0xa97bb0de338b23c088dba9bf8c948da726e49033",
    implied_id="pendle-pt-cbbtc-usdc", implied_label="PENDLE PT",
    underlying_id="pendle-underlying-cbbtc-usdc", underlying_label="PENDLE UNDERLY",
)
FUNDING = FundingRefCfg(symbol="BTCUSDT", id="funding-binance-btc", label="BTC FUND ANN")


def make_refs(llama_chart=None, pendle=None, funding=None) -> RefsCfg:
    return RefsCfg(
        aave=[AAVE],
        llama_chart=llama_chart if llama_chart is not None else [LLAMA],
        pendle=pendle if pendle is not None else [PENDLE],
        funding=funding if funding is not None else [FUNDING],
    )


REFS = make_refs()


# ---- pure parsing ----------------------------------------------------------

def test_parse_llama_chart_skips_null_apy_base():
    points = dict(parse_llama_chart(LLAMA_CHART))
    assert len(points) == 6  # 7 rows in the fixture, one null apyBase skipped
    assert date(2024, 6, 1) not in points
    assert points[date(2024, 3, 10)] == pytest.approx(13.42682)
    assert points[date(2026, 7, 23)] == pytest.approx(2.7346)


def test_parse_llama_chart_no_data_list_raises():
    with pytest.raises(ValueError):
        parse_llama_chart('{"status": "error"}')
    with pytest.raises(ValueError):
        parse_llama_chart("[]")


def test_parse_pendle_apy_history():
    hist = parse_pendle_apy_history(PENDLE_APY_HISTORY)
    implied = dict(hist["implied"])
    underlying = dict(hist["underlying"])
    assert len(implied) == 8 and len(underlying) == 8
    assert implied[date(2026, 7, 16)] == pytest.approx(4.80)
    assert underlying[date(2026, 7, 16)] == pytest.approx(4.32)
    assert implied[date(2026, 7, 23)] == pytest.approx(4.48)
    assert underlying[date(2026, 7, 23)] == pytest.approx(4.35)


def test_parse_pendle_apy_history_missing_results_raises():
    with pytest.raises(ValueError):
        parse_pendle_apy_history('{"total": 0}')


def test_daily_mean_annualized_known_days():
    points = dict(daily_mean_annualized(FUNDING_PAGE))
    assert points[date(2026, 3, 12)] == pytest.approx(-7.5099, abs=1e-3)
    assert points[date(2026, 3, 13)] == pytest.approx(0.3051, abs=1e-3)


def test_daily_mean_annualized_skips_malformed_rows():
    rows = [{"fundingTime": "not-a-number", "fundingRate": "0.0001"}]
    assert daily_mean_annualized(rows) == []


# ---- fetch_refs_history ------------------------------------------------------

async def test_fetch_refs_history_llama_and_pendle(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fake_get(url, params=None, headers=None):
        if "yields.llama.fi" in url:
            return LLAMA_CHART
        if "apy-history" in url:
            return PENDLE_APY_HISTORY
        raise RuntimeError(f"unexpected url: {url}")

    refs = make_refs(funding=[])
    label = await fetch_refs_history(refs, store, fake_get)
    assert label == "refs-history"
    llama_points = store.points("ref:aave-base-usdc-supply")
    assert llama_points[date(2026, 7, 23)] == pytest.approx(2.7346)
    implied_points = store.points("ref:pendle-pt-cbbtc-usdc")
    assert implied_points[date(2026, 7, 16)] == pytest.approx(4.80)
    underlying_points = store.points("ref:pendle-underlying-cbbtc-usdc")
    assert underlying_points[date(2026, 7, 16)] == pytest.approx(4.32)


async def test_fetch_refs_history_idempotent_rerun(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fake_get(url, params=None, headers=None):
        return LLAMA_CHART

    refs = make_refs(pendle=[], funding=[])
    await fetch_refs_history(refs, store, fake_get)
    first = dict(store.points("ref:aave-base-usdc-supply"))
    await fetch_refs_history(refs, store, fake_get)
    second = dict(store.points("ref:aave-base-usdc-supply"))
    assert first == second


async def test_fetch_refs_history_funding_pagination_and_daily_mean(tmp_path):
    store = Store(tmp_path / "t.db")
    full_page = [
        {"symbol": "BTCUSDT", "fundingTime": 1_000_000_000_000 + i * 28_800_000,
         "fundingRate": "0.00010000", "markPrice": "1"}
        for i in range(1000)
    ]
    calls: list[dict] = []

    async def fake_get(url, params=None, headers=None):
        calls.append(dict(params))
        if len(calls) == 1:
            assert params["startTime"] == "0"
            return json.dumps(full_page)
        assert params["startTime"] == str(full_page[-1]["fundingTime"] + 1)
        return json.dumps(FUNDING_PAGE)

    refs = make_refs(llama_chart=[], pendle=[])
    label = await fetch_refs_history(refs, store, fake_get)
    assert label == "refs-history"
    assert len(calls) == 2  # stopped once a short (<1000) page came back
    points = store.points("ref:funding-binance-btc")
    assert points[date(2026, 3, 12)] == pytest.approx(-7.5099, abs=1e-3)
    assert points[date(2026, 3, 13)] == pytest.approx(0.3051, abs=1e-3)


async def test_fetch_refs_history_funding_hard_cap_20_pages(tmp_path):
    store = Store(tmp_path / "t.db")
    calls: list[dict] = []

    async def always_full_page(url, params=None, headers=None):
        calls.append(dict(params))
        base = len(calls) * 10_000_000_000
        page = [
            {"symbol": "BTCUSDT", "fundingTime": base + i * 28_800_000,
             "fundingRate": "0.0001", "markPrice": "1"}
            for i in range(1000)
        ]
        return json.dumps(page)

    refs = make_refs(llama_chart=[], pendle=[])
    await fetch_refs_history(refs, store, always_full_page)
    assert len(calls) == 20  # hard cap, even though every page is "full"


async def test_fetch_refs_history_per_source_degradation(tmp_path):
    store = Store(tmp_path / "t.db")

    async def llama_fails(url, params=None, headers=None):
        if "yields.llama.fi" in url:
            raise RuntimeError("llama down")
        if "apy-history" in url:
            return PENDLE_APY_HISTORY
        raise RuntimeError(f"unexpected url: {url}")

    refs = make_refs(funding=[])
    label = await fetch_refs_history(refs, store, llama_fails)
    assert label == "refs-history"
    assert store.points("ref:aave-base-usdc-supply") == {}  # llama failed, no points
    assert store.points("ref:pendle-pt-cbbtc-usdc")  # pendle still succeeded


async def test_fetch_refs_history_all_sources_failed_raises(tmp_path):
    store = Store(tmp_path / "t.db")

    async def failing_get(url, params=None, headers=None):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="all refs-history sources failed"):
        await fetch_refs_history(REFS, store, failing_get)
