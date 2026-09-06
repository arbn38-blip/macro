import json
from pathlib import Path

import pytest

from collector.config import ChainCfg, DefiCfg, StrategyCfg
from collector.fetchers.morpho import build_query, fetch_morpho, parse_markets, _row
from collector.store import Store

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "morpho_markets.json").read_text())

DEFI = DefiCfg(
    asset="USDC",
    strategies=[StrategyCfg(id="safe", label="Conservative")],
    chains=[
        ChainCfg(id=8453, name="Base", usdc="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
        ChainCfg(id=1, name="Ethereum", usdc="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
        ChainCfg(id=42161, name="Arbitrum", usdc="0xaf88d065e77c8cc2239327c5edb3a432268e5831"),
    ],
    midnight_chains=[8453],
    token_symbols={},
    morpho_graphql="https://blue-api.morpho.org/graphql",
    morpho_first=25,
)
CHAIN_NAMES = {c.id: c.name for c in DEFI.chains}


def test_build_query_contains_listed_true_chain_ids_and_lowercase_addresses():
    q = build_query([8453, 1, 42161],
                     ["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                      "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"], 25)
    assert "listed: true" in q
    assert "chainId_in: [8453, 1, 42161]" in q
    assert '"0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"' in q
    assert '"0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"' in q
    # addresses must be lowercase, never the mixed-case input
    assert "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913" not in q
    assert "first: 25" in q


def test_parse_markets_happy():
    items = parse_markets(FIXTURE)
    assert len(items) == 9  # 5 real + 1 idle + 1 unknown-chain + 1 garbage-apy + 1 dCOMP, all dict-shaped


def test_parse_markets_errors_member_raises():
    with pytest.raises(ValueError):
        parse_markets({"errors": [{"message": "boom"}]})


def test_parse_markets_missing_data_raises():
    with pytest.raises(ValueError):
        parse_markets({"data": {}})
    with pytest.raises(ValueError):
        parse_markets({"data": {"markets": {}}})
    with pytest.raises(ValueError):
        parse_markets({})


def test_row_math_lltv_wad_and_fractions_and_tvl_passthrough():
    item = {
        "marketId": "0xabc",
        "lltv": "860000000000000000",
        "chain": {"id": 8453},
        "loanAsset": {"symbol": "USDC"},
        "collateralAsset": {"symbol": "cbBTC"},
        "state": {
            "supplyApy": 0.0489,
            "borrowApy": 0.0543,
            "utilization": 0.9041,
            "supplyAssetsUsd": 1413119205.7988927,
        },
    }
    row = _row(item, CHAIN_NAMES)
    assert row["lltv_pct"] == pytest.approx(86.0)
    assert row["supply_apy"] == pytest.approx(4.89)
    assert row["borrow_apy"] == pytest.approx(5.43)
    assert row["utilization_pct"] == pytest.approx(90.41)
    assert row["tvl_usd"] == pytest.approx(1413119205.7988927)  # passthrough, no scaling
    assert row["chain"] == "Base" and row["chain_id"] == 8453
    assert row["collateral"] == "cbBTC" and row["market_id"] == "0xabc"


def test_row_idle_market_skipped():
    item = {
        "marketId": "0xidle", "lltv": "860000000000000000", "chain": {"id": 8453},
        "loanAsset": {"symbol": "USDC"}, "collateralAsset": None,
        "state": {"supplyApy": 0, "borrowApy": 0, "utilization": 0, "supplyAssetsUsd": 0},
    }
    assert _row(item, CHAIN_NAMES) is None


def test_row_unknown_chain_skipped():
    item = {
        "marketId": "0xunknown", "lltv": "860000000000000000", "chain": {"id": 999},
        "loanAsset": {"symbol": "USDC"}, "collateralAsset": {"symbol": "WETH"},
        "state": {"supplyApy": 0.05, "borrowApy": 0.06, "utilization": 0.9, "supplyAssetsUsd": 5e6},
    }
    assert _row(item, CHAIN_NAMES) is None


def test_row_malformed_item_returns_none():
    assert _row({"collateralAsset": {"symbol": "X"}}, CHAIN_NAMES) is None  # missing everything else
    bad_lltv = {
        "marketId": "0xabc", "lltv": "not-a-number", "chain": {"id": 8453},
        "collateralAsset": {"symbol": "cbBTC"},
        "state": {"supplyApy": 0.05, "borrowApy": 0.06, "utilization": 0.9, "supplyAssetsUsd": 1.0},
    }
    assert _row(bad_lltv, CHAIN_NAMES) is None


async def test_fetch_morpho_from_fixture_sorted_and_skips(tmp_path):
    seen: list[dict] = []

    async def fake_post(url, json=None, headers=None):
        seen.append({"url": url, "json": json})
        return FIXTURE

    store = Store(tmp_path / "t.db")
    assert await fetch_morpho(DEFI, store, fake_post) == "morpho-blue"
    assert seen[0]["url"] == DEFI.morpho_graphql
    assert "query" in seen[0]["json"]

    rows = store.doc("morpho_markets").payload["rows"]
    # idle + unknown-chain + garbage-apy (msY) all skipped; dCOMP (legit, ~13% borrow) survives
    assert len(rows) == 6
    # tvl-desc: cbBTC Base (~1.4B) first
    assert rows[0]["collateral"] == "cbBTC" and rows[0]["chain"] == "Base"
    assert rows[0]["tvl_usd"] == pytest.approx(1413119205.7988927)
    tvls = [r["tvl_usd"] for r in rows]
    assert tvls == sorted(tvls, reverse=True)


async def test_fetch_morpho_drops_garbage_apy_keeps_legit(tmp_path):
    async def fake_post(url, json=None, headers=None):
        return FIXTURE

    store = Store(tmp_path / "t.db")
    await fetch_morpho(DEFI, store, fake_post)
    rows = store.doc("morpho_markets").payload["rows"]
    assert not any(r["collateral"] == "msY" for r in rows)  # garbage-apy market dropped
    dcomp = next(r for r in rows if r["collateral"] == "dCOMP")  # legit row survives
    assert dcomp["borrow_apy"] == pytest.approx(13.603793838014336)
    assert all(r["supply_apy"] < 200 and r["borrow_apy"] < 200 for r in rows)


async def test_fetch_morpho_empty_keeps_previous_doc(tmp_path):
    store = Store(tmp_path / "t.db")
    store.put_doc("morpho_markets", {"rows": [{"market_id": "old", "collateral": "OLD"}]},
                   source="morpho-blue")

    async def fake_post(url, json=None, headers=None):
        return {"data": {"markets": {"items": []}}}

    assert await fetch_morpho(DEFI, store, fake_post) == "morpho-blue"
    rows = store.doc("morpho_markets").payload["rows"]
    assert rows[0]["market_id"] == "old"


async def test_fetch_morpho_empty_first_run_writes_empty_doc(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fake_post(url, json=None, headers=None):
        return {"data": {"markets": {"items": []}}}

    assert await fetch_morpho(DEFI, store, fake_post) == "morpho-blue"
    assert store.doc("morpho_markets").payload == {"rows": []}


async def test_fetch_morpho_failure_raises_and_previous_doc_survives(tmp_path):
    store = Store(tmp_path / "t.db")
    store.put_doc("morpho_markets", {"rows": [{"market_id": "old", "collateral": "OLD"}]},
                   source="morpho-blue")

    async def fake_post(url, json=None, headers=None):
        raise RuntimeError("HTTP 500")

    with pytest.raises(RuntimeError, match="morpho request/parse failed"):
        await fetch_morpho(DEFI, store, fake_post)
    assert store.doc("morpho_markets").payload["rows"][0]["market_id"] == "old"


async def test_fetch_morpho_graphql_errors_member_raises(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fake_post(url, json=None, headers=None):
        return {"errors": [{"message": "boom"}]}

    with pytest.raises(RuntimeError, match="morpho request/parse failed"):
        await fetch_morpho(DEFI, store, fake_post)


async def test_fetch_morpho_records_daily_series_both_prefixes(tmp_path):
    async def fake_post(url, json=None, headers=None):
        return FIXTURE

    store = Store(tmp_path / "t.db")
    await fetch_morpho(DEFI, store, fake_post)
    rows = store.doc("morpho_markets").payload["rows"]
    top = rows[0]
    supply_pts = store.points(f"mkt-supply:{top['chain_id']}:{top['market_id']}")
    borrow_pts = store.points(f"mkt-borrow:{top['chain_id']}:{top['market_id']}")
    assert list(supply_pts.values()) == [top["supply_apy"]]
    assert list(borrow_pts.values()) == [top["borrow_apy"]]
