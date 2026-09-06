import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector.config import ChainCfg, DefiCfg, StrategyCfg
from collector.fetchers.midnight import fetch_midnight, implied_apy, parse_books
from collector.store import Store

FIXTURE = (Path(__file__).parent / "fixtures" / "midnight_books.json").read_text()
# All expected numbers below assume this frozen "now" (epoch 1784678400):
NOW = datetime(2026, 7, 22, tzinfo=timezone.utc)

DEFI = DefiCfg(
    asset="USDC",
    strategies=[StrategyCfg(id="safe", label="Conservative")],
    chains=[ChainCfg(id=8453, name="Base", usdc="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")],
    midnight_chains=[8453],
    token_symbols={"0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf": "cbBTC"},
)
BASE_URL = "https://api.morpho.org/v0/midnight"


def test_parse_books():
    cursor, books = parse_books(FIXTURE)
    assert cursor is None
    assert len(books) == 3
    assert books[1]["maturity"] == 1787929200


def test_parse_no_data_list_raises():
    with pytest.raises(ValueError):
        parse_books('{"cursor": null}')


def test_implied_apy_clean_numbers():
    # price 0.99 WAD, exactly 73 days out -> (1/0.99)^(365/73) - 1 = (100/99)^5 - 1
    maturity = int(NOW.timestamp()) + 73 * 86400
    y = implied_apy("990000000000000000", maturity, NOW)
    assert y == pytest.approx(5.153571281335045)


def test_implied_apy_real_capture():
    # best ask of the Aug-28 book: p=0.9958739, 37.625 days -> 4.0925%
    y = implied_apy("995873900000000000", 1787929200, NOW)
    assert y == pytest.approx(4.0925361830999485)


def test_implied_apy_matured_returns_none():
    assert implied_apy("990000000000000000", int(NOW.timestamp()) - 86400, NOW) is None


def test_implied_apy_garbage_price_returns_none():
    maturity = int(NOW.timestamp()) + 73 * 86400
    assert implied_apy("0", maturity, NOW) is None
    assert implied_apy("2000000000000000000", maturity, NOW) is None  # p=2.0 > sanity band


def test_implied_apy_near_maturity_does_not_overflow():
    assert implied_apy("500000000000000000", int(NOW.timestamp()) + 60, NOW) is None


async def test_fetch_midnight_from_fixture(tmp_path):
    seen: list[dict] = []

    async def fake_get(url, params=None, headers=None):
        seen.append({"url": url, "params": params})
        return FIXTURE

    store = Store(tmp_path / "t.db")
    assert await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW) == "morpho"
    assert seen[0]["url"] == f"{BASE_URL}/books"
    assert seen[0]["params"] == {"chain_ids": "8453", "limit": "20"}

    rows = store.doc("midnight_curve").payload["rows"]
    assert len(rows) == 2                      # WETH book filtered out
    aug, sep = rows                            # sorted by maturity ascending
    assert aug["maturity"] == "2026-08-28" and sep["maturity"] == "2026-09-25"
    assert aug["days"] == pytest.approx(37.625)
    # best ask selected by min price even though fixture lists worst-first
    assert aug["lend_apy"] == pytest.approx(4.0925361830999485)
    assert aug["borrow_apy"] == pytest.approx(4.53047561440989)
    assert aug["ask_depth_usd"] == pytest.approx(100699.339381)   # 6-dec USDC sum
    assert aug["bid_depth_usd"] == pytest.approx(318.754794)
    assert aug["collateral"] == "cbBTC"
    assert aug["chain"] == "Base"
    # one-sided book: no asks -> lend unavailable, borrow still quoted
    assert sep["lend_apy"] is None and sep["borrow_apy"] is not None
    assert sep["ask_depth_usd"] == 0 and sep["bid_depth_usd"] == pytest.approx(250.0)


async def test_fetch_midnight_follows_cursor(tmp_path):
    page1 = json.dumps({"cursor": "abc", "data": [json.loads(FIXTURE)["data"][1]]})
    page2 = json.dumps({"cursor": None, "data": [json.loads(FIXTURE)["data"][0]]})
    calls: list[dict] = []

    async def fake_get(url, params=None, headers=None):
        calls.append(params)
        return page2 if params.get("cursor") else page1

    store = Store(tmp_path / "t.db")
    await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW)
    assert calls[1]["cursor"] == "abc"
    assert len(store.doc("midnight_curve").payload["rows"]) == 2


async def test_fetch_midnight_skips_matured_book(tmp_path):
    matured = dict(json.loads(FIXTURE)["data"][1], maturity=int(NOW.timestamp()) - 86400)

    async def fake_get(url, params=None, headers=None):
        return json.dumps({"cursor": None, "data": [matured]})

    store = Store(tmp_path / "t.db")
    await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW)
    assert store.doc("midnight_curve").payload["rows"] == []


async def test_fetch_midnight_unknown_collateral_truncated(tmp_path):
    book = dict(json.loads(FIXTURE)["data"][1],
                collaterals=[{"token": "0xdeadbeef00000000000000000000000000000000"}])

    async def fake_get(url, params=None, headers=None):
        return json.dumps({"cursor": None, "data": [book]})

    store = Store(tmp_path / "t.db")
    await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW)
    assert store.doc("midnight_curve").payload["rows"][0]["collateral"] == "0xdead…"


async def test_fetch_midnight_all_chains_failed_raises(tmp_path):
    async def fake_get(url, params=None, headers=None):
        raise RuntimeError("HTTP 502")

    store = Store(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="all midnight chains failed"):
        await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW)


async def test_fetch_midnight_empty_result_overwrites_previous_doc(tmp_path):
    # Deliberate divergence from zyfai: zero live USDC books is a VALID state
    # (post-maturity gap) and must replace old rows, not preserve them.
    store = Store(tmp_path / "t.db")
    store.put_doc("midnight_curve", {"rows": [{"maturity": "2026-01-01"}]}, source="morpho")

    async def fake_get(url, params=None, headers=None):
        return json.dumps({"cursor": None, "data": []})

    await fetch_midnight(DEFI, BASE_URL, store, fake_get, now=NOW)
    assert store.doc("midnight_curve").payload == {"rows": []}
