import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector.config import AaveRefCfg, FundingRefCfg, LlamaChartCfg, PendleRefCfg, RefsCfg
from collector.fetchers.refs import (
    expiry_short,
    fetch_refs,
    parse_aave_reserve_data,
    parse_funding_premium,
    parse_pendle_market,
)
from collector.store import Store

FIX = Path(__file__).parent / "fixtures"
AAVE_RPC = json.loads((FIX / "aave_reserve_data.json").read_text())
PENDLE_MARKET = (FIX / "pendle_market.json").read_text()
BINANCE_PREMIUM = (FIX / "binance_premium_index.json").read_text()

SUPPLY_PCT = 2.735189345758076
BORROW_PCT = 3.8981404251875613
IMPLIED_PCT = 4.475867335355943
UNDERLYING_PCT = 4.346230199002243
FUNDING_PCT = 0.21790500000000002

BASE_USDC = AaveRefCfg(
    chain="BASE", rpc="https://mainnet.base.org",
    pool="0xa238dd80c259a72e81d7e4664a9801593f98d1c5",
    asset="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", symbol="USDC",
)
ETH_USDT = AaveRefCfg(
    chain="ETH", rpc="https://ethereum-rpc.publicnode.com",
    pool="0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
    asset="0xdac17f958d2ee523a2206206994597c13d831ec7", symbol="USDT",
)

REFS = RefsCfg(
    aave=[BASE_USDC],
    llama_chart=[LlamaChartCfg(pool="7e0661bf-8cf3-45e6-9424-31916d4c7b84",
                                series="aave-base-usdc-supply")],
    pendle=[PendleRefCfg(
        chain_id=8453, address="0xa97bb0de338b23c088dba9bf8c948da726e49033",
        implied_id="pendle-pt-cbbtc-usdc", implied_label="PENDLE PT",
        underlying_id="pendle-underlying-cbbtc-usdc", underlying_label="PENDLE UNDERLY",
    )],
    funding=[FundingRefCfg(symbol="BTCUSDT", id="funding-binance-btc", label="BTC FUND ANN")],
)


async def fake_get(url, params=None, headers=None):
    if "pendle.finance" in url and "markets" in url:
        return PENDLE_MARKET
    if "premiumIndex" in url:
        return BINANCE_PREMIUM
    raise RuntimeError(f"unexpected url: {url}")


async def fake_post(url, json=None, headers=None):
    if "mainnet.base.org" in url:
        return AAVE_RPC
    raise RuntimeError(f"unexpected url: {url}")


# ---- pure parsing ----------------------------------------------------------

def test_parse_aave_reserve_data():
    supply, borrow = parse_aave_reserve_data(AAVE_RPC["result"])
    assert supply == pytest.approx(SUPPLY_PCT)
    assert borrow == pytest.approx(BORROW_PCT)


def test_parse_aave_reserve_data_too_few_words_raises():
    with pytest.raises(ValueError):
        parse_aave_reserve_data("0x" + "00" * 32 * 3)  # only 3 words


def test_parse_aave_reserve_data_out_of_sanity_band_raises():
    words = ["00" * 32] * 5
    # word 2 (supply) absurdly large: 2000 ray-percent
    words[2] = format(int(2000 * 1e27 / 100), "064x")
    bad = "0x" + "".join(words)
    with pytest.raises(ValueError):
        parse_aave_reserve_data(bad)


def test_parse_pendle_market():
    parsed = parse_pendle_market(PENDLE_MARKET)
    assert parsed["implied_pct"] == pytest.approx(IMPLIED_PCT)
    assert parsed["underlying_pct"] == pytest.approx(UNDERLYING_PCT)
    assert parsed["expiry"] == "2026-09-17T00:00:00.000Z"


def test_parse_pendle_market_missing_field_raises():
    with pytest.raises(ValueError):
        parse_pendle_market('{"impliedApy": 0.04}')  # no underlyingApy/expiry
    with pytest.raises(ValueError):
        parse_pendle_market("[]")


def test_expiry_short_formats_iso_date():
    assert expiry_short("2026-09-17T00:00:00.000Z") == "SEP17"
    assert expiry_short("2026-01-05T00:00:00.000Z") == "JAN05"


def test_expiry_short_stable_under_non_c_locale():
    # explicit month table (not strftime's locale-dependent %b) -- must not
    # drift under a locale where month abbreviations aren't English
    import locale

    try:
        locale.setlocale(locale.LC_TIME, "fr_FR.UTF-8")
    except locale.Error:
        pytest.skip("fr_FR.UTF-8 locale not available in this environment")
    try:
        assert expiry_short("2026-09-17T00:00:00.000Z") == "SEP17"
    finally:
        locale.setlocale(locale.LC_TIME, "C")


def test_parse_funding_premium():
    parsed = parse_funding_premium(BINANCE_PREMIUM)
    assert parsed["value_pct"] == pytest.approx(FUNDING_PCT)
    assert parsed["rate_8h"] == pytest.approx(0.00000199)


def test_parse_funding_premium_missing_field_raises():
    with pytest.raises(ValueError):
        parse_funding_premium('{"markPrice": "1"}')


# ---- fetch_refs -------------------------------------------------------------

def test_aave_market_derives_ids_and_labels():
    # BASE/USDC must derive the ORIGINAL ids -- they carry accumulated history
    assert BASE_USDC.supply_id == "aave-base-usdc-supply"
    assert BASE_USDC.borrow_id == "aave-base-usdc-borrow"
    assert BASE_USDC.supply_label == "AAVE USDC BASE SUP"
    assert ETH_USDT.borrow_id == "aave-eth-usdt-borrow"
    assert ETH_USDT.borrow_label == "AAVE USDT ETH BOR"


async def test_fetch_refs_multiple_aave_markets_with_per_market_degradation(tmp_path):
    store = Store(tmp_path / "t.db")
    refs = RefsCfg(aave=[BASE_USDC, ETH_USDT], llama_chart=[],
                   pendle=REFS.pendle, funding=REFS.funding)

    async def eth_rpc_fails(url, json=None, headers=None):
        if "mainnet.base.org" in url:
            return AAVE_RPC
        raise RuntimeError("publicnode down")

    await fetch_refs(refs, store, fake_get, eth_rpc_fails)
    rows = store.doc("rate_refs").payload["rows"]
    # surviving market fresh and first; failed market has no previous doc to
    # carry forward, so its rows are simply absent; order stays stable
    assert [r["id"] for r in rows] == [
        "aave-base-usdc-supply", "aave-base-usdc-borrow",
        "pendle-pt-cbbtc-usdc", "pendle-underlying-cbbtc-usdc",
        "funding-binance-btc",
    ]
    assert rows[0]["label"] == "AAVE USDC BASE SUP"
    assert rows[0]["value_pct"] == pytest.approx(SUPPLY_PCT)


async def test_fetch_refs_row_order_and_labels(tmp_path):
    store = Store(tmp_path / "t.db")
    label = await fetch_refs(REFS, store, fake_get, fake_post)
    assert label == "refs"
    rows = store.doc("rate_refs").payload["rows"]
    assert [r["id"] for r in rows] == [
        "aave-base-usdc-supply", "aave-base-usdc-borrow",
        "pendle-pt-cbbtc-usdc", "pendle-underlying-cbbtc-usdc",
        "funding-binance-btc",
    ]
    pt_row = rows[2]
    assert pt_row["label"] == "PENDLE PT SEP17"
    assert pt_row["extra"] == {"expiry": "2026-09-17T00:00:00.000Z"}
    assert rows[3]["extra"] is None
    assert rows[0]["value_pct"] == pytest.approx(SUPPLY_PCT)
    assert rows[1]["value_pct"] == pytest.approx(BORROW_PCT)
    assert rows[4]["extra"] == {"rate_8h": pytest.approx(0.00000199)}
    assert store.doc("rate_refs").source == "refs"


async def test_fetch_refs_records_daily_points_for_fresh_rows(tmp_path):
    store = Store(tmp_path / "t.db")
    today = datetime.now(timezone.utc).date()
    await fetch_refs(REFS, store, fake_get, fake_post)
    assert store.points("ref:aave-base-usdc-supply")[today] == pytest.approx(SUPPLY_PCT)
    assert store.points("ref:aave-base-usdc-borrow")[today] == pytest.approx(BORROW_PCT)
    assert store.points("ref:pendle-pt-cbbtc-usdc")[today] == pytest.approx(IMPLIED_PCT)
    assert store.points("ref:pendle-underlying-cbbtc-usdc")[today] == pytest.approx(UNDERLYING_PCT)
    assert store.points("ref:funding-binance-btc")[today] == pytest.approx(FUNDING_PCT)


async def test_per_source_degradation_carries_forward_from_previous_doc(tmp_path):
    store = Store(tmp_path / "t.db")
    today = datetime.now(timezone.utc).date()
    old_implied = {"id": "pendle-pt-cbbtc-usdc", "label": "PENDLE PT OLD",
                   "value_pct": 1.23, "extra": {"expiry": "2025-01-01T00:00:00.000Z"}}
    old_underlying = {"id": "pendle-underlying-cbbtc-usdc", "label": "PENDLE UNDERLY",
                       "value_pct": 4.0, "extra": None}
    store.put_doc("rate_refs", {"rows": [
        {"id": "aave-base-usdc-supply", "label": "AAVE SUPPLY", "value_pct": 0.0, "extra": None},
        {"id": "aave-base-usdc-borrow", "label": "AAVE BORROW", "value_pct": 0.0, "extra": None},
        old_implied, old_underlying,
        {"id": "funding-binance-btc", "label": "BTC FUND ANN", "value_pct": 0.0, "extra": None},
    ]}, source="refs")
    store.upsert_points("ref:pendle-pt-cbbtc-usdc", [(today, 1.23)])

    async def pendle_fails(url, params=None, headers=None):
        if "pendle.finance" in url:
            raise RuntimeError("pendle down")
        if "premiumIndex" in url:
            return BINANCE_PREMIUM
        raise RuntimeError(f"unexpected url: {url}")

    label = await fetch_refs(REFS, store, pendle_fails, fake_post)
    assert label == "refs"
    rows_by_id = {r["id"]: r for r in store.doc("rate_refs").payload["rows"]}
    # carried forward unchanged -- stale beats gone
    assert rows_by_id["pendle-pt-cbbtc-usdc"] == old_implied
    assert rows_by_id["pendle-underlying-cbbtc-usdc"] == old_underlying
    # fresh sources updated
    assert rows_by_id["aave-base-usdc-supply"]["value_pct"] == pytest.approx(SUPPLY_PCT)
    assert rows_by_id["aave-base-usdc-borrow"]["value_pct"] == pytest.approx(BORROW_PCT)
    assert rows_by_id["funding-binance-btc"]["value_pct"] == pytest.approx(FUNDING_PCT)
    # only fresh rows get a new daily point -- carried-forward series untouched
    assert store.points("ref:pendle-pt-cbbtc-usdc") == {today: 1.23}
    assert store.points("ref:aave-base-usdc-supply")[today] == pytest.approx(SUPPLY_PCT)


async def test_all_refs_sources_failed_raises(tmp_path):
    store = Store(tmp_path / "t.db")

    async def failing_get(url, params=None, headers=None):
        raise RuntimeError("down")

    async def failing_post(url, json=None, headers=None):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="all refs sources failed"):
        await fetch_refs(REFS, store, failing_get, failing_post)
    assert store.doc("rate_refs") is None
