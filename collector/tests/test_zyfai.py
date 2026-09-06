import json
from pathlib import Path

import pytest

from collector.config import ChainCfg, DefiCfg, StrategyCfg
from collector.fetchers.zyfai import parse_opportunities, fetch_defi
from collector.store import Store

FIXTURE = (Path(__file__).parent / "fixtures" / "zyfai_safe_base.json").read_text()


def test_parse_opportunities():
    opps = parse_opportunities(FIXTURE)
    assert len(opps) == 3  # parse keeps everything dict-shaped; row validation is fetch-time
    assert opps[0]["pool_name"] == "Clearstar cbAssets Vault"
    assert opps[0]["combined_apy"] == pytest.approx(7.977915857647811)


def test_parse_no_data_list_raises():
    with pytest.raises(ValueError):
        parse_opportunities('{"status": "error"}')
    with pytest.raises(ValueError):
        parse_opportunities('{"status": "success", "data": "nope"}')
    with pytest.raises(ValueError):
        parse_opportunities("[]")
    with pytest.raises(ValueError):
        parse_opportunities("null")


def test_parse_drops_non_dict_elements():
    opps = parse_opportunities('{"data": [{"pool_name": "A"}, "junk", null]}')
    assert opps == [{"pool_name": "A"}]


DEFI = DefiCfg(
    asset="USDC",
    strategies=[StrategyCfg(id="safe", label="Conservative"),
                StrategyCfg(id="degen", label="Moderate"),
                StrategyCfg(id="async", label="Dynamic")],
    chains=[ChainCfg(id=8453, name="Base", usdc="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")],
    midnight_chains=[8453],
    token_symbols={},
)
BASE_URL = "https://defiapi.zyf.ai/api/v2/opportunities"


def _payload(*opps: dict) -> str:
    return json.dumps({"status": "success", "data": list(opps)})


def _opp(address: str, apy: float, name: str = "Pool") -> dict:
    return {"protocol_name": "Proto", "pool_name": name, "combined_apy": apy,
            "chain_id": 8453, "pool_address": address, "tvlUsd": 1000.0,
            "url": "https://example.com",
            "averageCombinedApy7Days": None, "averageCombinedApy30Days": None}


async def test_fetch_builds_urls_and_params(tmp_path):
    seen: list[tuple[str, dict]] = []

    async def fake_get(url, params=None, headers=None):
        seen.append((url, params))
        return _payload()

    store = Store(tmp_path / "t.db")
    # all endpoints succeeded but returned zero pools: valid empty doc, no raise
    assert await fetch_defi(DEFI, BASE_URL, store, fake_get) == "zyfai"
    assert seen[0] == (f"{BASE_URL}/safe",
                       {"asset": "USDC", "chainId": "8453", "status": "live"})
    assert [u for u, _ in seen] == [f"{BASE_URL}/safe", f"{BASE_URL}/degen", f"{BASE_URL}/async"]
    assert store.doc("defi_pools").payload == {"rows": []}


async def test_fetch_dedupes_to_most_conservative_tier_and_sorts(tmp_path):
    a, b = "0x000000000000000000000000000000000000000a", "0x000000000000000000000000000000000000000b"

    async def fake_get(url, params=None, headers=None):
        if "/safe" in url:
            return _payload(_opp(a, 5.0, "A"))
        return _payload(_opp(a, 5.0, "A"), _opp(b, 9.0, "B"))  # degen & async re-list A

    store = Store(tmp_path / "t.db")
    await fetch_defi(DEFI, BASE_URL, store, fake_get)
    rows = store.doc("defi_pools").payload["rows"]
    assert [(r["pool"], r["tier"]) for r in rows] == [("A", "Conservative"), ("B", "Moderate")]
    # sorted tier-first (config order), APY-desc within tier — B's higher APY
    # does not move it above the more conservative tier section


async def test_fetch_records_daily_apy_series(tmp_path):
    a = "0x000000000000000000000000000000000000000A"  # mixed case in payload

    async def fake_get(url, params=None, headers=None):
        return _payload(_opp(a, 5.0)) if "/safe" in url else _payload()

    store = Store(tmp_path / "t.db")
    await fetch_defi(DEFI, BASE_URL, store, fake_get)
    pts = store.points(f"defi:8453:{a.lower()}")  # series key is lowercased
    assert list(pts.values()) == [5.0]


async def test_fetch_partial_failure_degrades(tmp_path):
    async def fake_get(url, params=None, headers=None):
        if "/safe" in url:
            raise RuntimeError("HTTP 500")
        return _payload(_opp("0x000000000000000000000000000000000000000b", 9.0, "B"))

    store = Store(tmp_path / "t.db")
    assert await fetch_defi(DEFI, BASE_URL, store, fake_get) == "zyfai"
    rows = store.doc("defi_pools").payload["rows"]
    # B comes back from degen AND async; dedupe keeps the first (Moderate) only
    assert [(r["pool"], r["tier"]) for r in rows] == [("B", "Moderate")]


async def test_fetch_total_failure_raises(tmp_path):
    async def fake_get(url, params=None, headers=None):
        raise RuntimeError("HTTP 500")

    store = Store(tmp_path / "t.db")
    store.put_doc("defi_pools", {"rows": [{"pool": "Old"}]}, source="zyfai")
    with pytest.raises(RuntimeError, match="all zyfai endpoints failed"):
        await fetch_defi(DEFI, BASE_URL, store, fake_get)
    assert store.doc("defi_pools").payload["rows"][0]["pool"] == "Old"  # previous doc survives total failure


async def test_fetch_skips_malformed_rows(tmp_path):
    fixture = FIXTURE  # contains 2 good rows + 1 missing combined_apy

    async def fake_get(url, params=None, headers=None):
        return fixture if "/safe" in url else _payload()

    store = Store(tmp_path / "t.db")
    await fetch_defi(DEFI, BASE_URL, store, fake_get)
    rows = store.doc("defi_pools").payload["rows"]
    assert len(rows) == 2
    clearstar = rows[0]
    assert clearstar["pool"] == "Clearstar cbAssets Vault"
    assert clearstar["apy"] == pytest.approx(7.977915857647811)
    assert clearstar["apy_7d"] == pytest.approx(6.542457503857143)
    assert clearstar["tvl_usd"] == pytest.approx(10732971.31491206)
    assert clearstar["tier"] == "Conservative" and clearstar["chain"] == "Base"
    assert rows[1]["apy_7d"] is None  # null averages survive as None


async def test_fetch_sorts_apy_desc_within_tier(tmp_path):
    a, b = "0x000000000000000000000000000000000000000a", "0x000000000000000000000000000000000000000b"

    async def fake_get(url, params=None, headers=None):
        if "/safe" in url:
            return _payload(_opp(a, 3.0, "Low"), _opp(b, 8.0, "High"))
        return _payload()

    store = Store(tmp_path / "t.db")
    await fetch_defi(DEFI, BASE_URL, store, fake_get)
    rows = store.doc("defi_pools").payload["rows"]
    assert [r["pool"] for r in rows] == ["High", "Low"]  # APY desc within one tier


async def test_fetch_all_empty_keeps_previous_doc(tmp_path):
    store = Store(tmp_path / "t.db")
    store.put_doc("defi_pools", {"rows": [{"pool": "Old", "tier": "Conservative"}]}, source="zyfai")

    async def fake_get(url, params=None, headers=None):
        return _payload()  # every endpoint healthy but empty

    assert await fetch_defi(DEFI, BASE_URL, store, fake_get) == "zyfai"
    assert store.doc("defi_pools").payload["rows"][0]["pool"] == "Old"
