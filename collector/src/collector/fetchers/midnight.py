"""Morpho Midnight fixed-rate books → implied USDC term structure.

Books quote zero-coupon unit prices (18-decimal WAD): pay p now, receive 1 at
maturity. Implied annualized yield over d days is (1/p)^(365/d) - 1 (ACT/365,
compounded). Lenders buy credit units at the ask; borrowers sell debt units at
the bid — so lend APY < borrow APY is the normal spread. Best level is chosen
by price (min ask / max bid), not by API ordering.

Writes the 'midnight_curve' doc. No history series: maturities roll off, so
per-market series would be short-lived orphans (spec §3.2).

Base URL note: /v0/ is the real API (found in @morpho-org/midnight-sdk); the
docs site advertises /v1/ which 404s. Pagination: {cursor, data}, page size
capped at 20 by the server, cursor echoed as a query param.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from collector.config import ChainCfg, DefiCfg
from collector.http import GetText
from collector.store import Store

log = logging.getLogger(__name__)

WAD = 10**18
MAX_PAGES = 10        # cursor-loop hard cap; Base has ~5 books today
PRICE_SANITY_MAX = 1.5  # unit price above this (or <= 0) is garbage, not a yield


def parse_books(text: str) -> tuple[str | None, list[dict]]:
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("midnight payload is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("midnight payload has no data list")
    return body.get("cursor"), [b for b in data if isinstance(b, dict)]


def implied_apy(price_wad: int | str, maturity_ts: int, now: datetime) -> float | None:
    days = (maturity_ts - now.timestamp()) / 86400
    if days <= 0:
        return None
    price = int(price_wad) / WAD
    if not (0 < price <= PRICE_SANITY_MAX):
        return None
    try:
        return ((1 / price) ** (365 / days) - 1) * 100
    except OverflowError:
        # sub-hour horizons annualize to astronomically large figures — meaningless
        return None


def _book_row(book: dict, chain: ChainCfg, symbols: dict[str, str], now: datetime) -> dict | None:
    try:
        maturity_ts = int(book["maturity"])
        days = (maturity_ts - now.timestamp()) / 86400
        if days <= 0:
            return None  # matured book still listed by the API — not part of the curve
        asks = [l for l in (book.get("asks") or []) if isinstance(l, dict)]
        bids = [l for l in (book.get("bids") or []) if isinstance(l, dict)]
        best_ask = min((int(l["price"]) for l in asks), default=None)
        best_bid = max((int(l["price"]) for l in bids), default=None)
        collats = [
            symbols.get(str(c["token"]).lower(), str(c["token"])[:6] + "…")
            for c in (book.get("collaterals") or [])
        ]
        return {
            "chain": chain.name,
            "market_id": str(book["market_id"]),
            "maturity": datetime.fromtimestamp(maturity_ts, tz=timezone.utc).date().isoformat(),
            "days": days,
            "lend_apy": implied_apy(best_ask, maturity_ts, now) if best_ask else None,
            "borrow_apy": implied_apy(best_bid, maturity_ts, now) if best_bid else None,
            "ask_depth_usd": sum(int(l["assets"]) for l in asks) / 1e6,  # USDC: 6 decimals
            "bid_depth_usd": sum(int(l["assets"]) for l in bids) / 1e6,
            "collateral": "+".join(collats) or "—",
        }
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None


async def _fetch_all_books(base_url: str, chain_id: int, get_text: GetText) -> list[dict]:
    books: list[dict] = []
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        params = {"chain_ids": str(chain_id), "limit": "20"}  # server caps limit at 20
        if cursor:
            params["cursor"] = cursor
        cursor, page = parse_books(await get_text(f"{base_url}/books", params=params))
        books.extend(page)
        if not cursor:
            break
    else:
        log.warning("midnight pagination hit MAX_PAGES with cursor still live; books truncated")
    return books


async def fetch_midnight(
    defi: DefiCfg, base_url: str, store: Store, get_text: GetText,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    chains_attempted = 0
    chains_ok = 0
    errors: list[str] = []
    rows: list[dict] = []
    for chain in defi.chains:
        if chain.id not in defi.midnight_chains:
            continue
        chains_attempted += 1
        try:
            books = await _fetch_all_books(base_url, chain.id, get_text)
            chains_ok += 1
        except Exception as exc:  # noqa: BLE001 — one dead chain degrades, not kills
            errors.append(f"{chain.name}: {exc}")
            log.warning("midnight %s failed: %s", chain.name, exc)
            continue
        for book in books:
            if str(book.get("loan_token", "")).lower() != chain.usdc:
                continue  # USDC term structure only (spec §2.2)
            row = _book_row(book, chain, defi.token_symbols, now)
            if row is None:
                continue
            rows.append(row)
    if chains_attempted and chains_ok == 0:
        raise RuntimeError(f"all midnight chains failed: {'; '.join(errors)}")
    rows.sort(key=lambda r: r["maturity"])
    store.put_doc("midnight_curve", {"rows": rows}, source="morpho")
    return "morpho"
