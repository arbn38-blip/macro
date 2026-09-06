"""Live reference-rate values for the DEFI tab RATE REFS panel (cadence 900s).

Mirrors the macro / macro_history split: this is the 'refs' half (live
values + a daily point per row); 'refs_history' backfills the full series
into the same 'ref:{id}' keys. Live rows: aave supply/borrow per configured
market (one eth_call each, decoded ourselves -- see
parse_aave_reserve_data), pendle implied/underlying (one GET per configured
market), and funding annualized (one GET per configured symbol).

Writes doc 'rate_refs' {"rows": [{id, label, value_pct, extra}]} (source
"refs"). Per-ref degradation: a failed source logs a warning and its rows
are carried forward from the previous doc, keyed by id (stale beats gone);
total failure raises. Only rows fetched fresh this run get a new point in
their 'ref:{id}' series -- carried-forward rows keep whatever history they
already have.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

from collector.config import AaveRefCfg, FundingRefCfg, PendleRefCfg, RefsCfg
from collector.http import GetText, PostJson
from collector.store import Store

log = logging.getLogger(__name__)

AAVE_SELECTOR = "0x35ea6a75"  # getReserveData(address)
AAVE_RATE_SANITY_MAX = 1000.0  # percent; anything at/above this is garbage, not a yield

PENDLE_MARKET_BASE = "https://api-v2.pendle.finance/core/v1"
FUNDING_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def _aave_call_data(asset: str) -> str:
    """Build the `eth_call` calldata: selector + asset left-padded to a 32-byte word."""
    hex_asset = asset.lower()
    if hex_asset.startswith("0x"):
        hex_asset = hex_asset[2:]
    return AAVE_SELECTOR + hex_asset.rjust(64, "0")


def parse_aave_reserve_data(result_hex: str) -> tuple[float, float]:
    """Decode `getReserveData`'s ABI-encoded return blob into (supply_pct, borrow_pct).

    Word 2 is the current liquidity (supply) rate, word 4 the current
    variable borrow rate, both RAY (1e27) scaled. Raises ValueError if the
    blob has fewer than 5 words or either decoded rate falls outside the
    [0, 1000) percent sanity band.
    """
    hexstr = result_hex[2:] if result_hex.startswith("0x") else result_hex
    words = [hexstr[i : i + 64] for i in range(0, len(hexstr), 64)]
    if len(words) < 5:
        raise ValueError(f"aave reserve data has {len(words)} words, need >= 5")
    supply_pct = int(words[2], 16) / 1e27 * 100
    borrow_pct = int(words[4], 16) / 1e27 * 100
    if not (0 <= supply_pct < AAVE_RATE_SANITY_MAX):
        raise ValueError(f"aave supply rate out of sanity band: {supply_pct}")
    if not (0 <= borrow_pct < AAVE_RATE_SANITY_MAX):
        raise ValueError(f"aave borrow rate out of sanity band: {borrow_pct}")
    return supply_pct, borrow_pct


async def _fetch_aave_rows(cfg: AaveRefCfg, post_json: PostJson) -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": cfg.pool, "data": _aave_call_data(cfg.asset)}, "latest"],
    }
    body = await post_json(cfg.rpc, json=payload)
    error = body.get("error")
    if error is not None:
        raise ValueError(f"aave RPC error: {error}")
    result = body.get("result")
    if not isinstance(result, str):
        raise ValueError("aave RPC response missing result")
    supply_pct, borrow_pct = parse_aave_reserve_data(result)
    return [
        {"id": cfg.supply_id, "label": cfg.supply_label, "value_pct": supply_pct, "extra": None},
        {"id": cfg.borrow_id, "label": cfg.borrow_label, "value_pct": borrow_pct, "extra": None},
    ]


_MONTHS_INV = {i: m for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1
)}


def expiry_short(expiry_iso: str) -> str:
    """'2026-09-17T00:00:00.000Z' -> 'SEP17' (label suffix for a Pendle PT row).

    Explicit table: %b is locale-dependent.
    """
    d = date.fromisoformat(expiry_iso[:10])
    return f"{_MONTHS_INV[d.month]}{d.day:02d}"


def parse_pendle_market(text: str) -> dict:
    """Parse one `markets/{address}` response into implied/underlying pct + expiry.

    Raises ValueError if the payload isn't a JSON object or is missing
    impliedApy, underlyingApy, or expiry.
    """
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("pendle payload is not a JSON object")
    implied = body.get("impliedApy")
    underlying = body.get("underlyingApy")
    expiry = body.get("expiry")
    if not isinstance(implied, (int, float)):
        raise ValueError("pendle payload missing impliedApy")
    if not isinstance(underlying, (int, float)):
        raise ValueError("pendle payload missing underlyingApy")
    if not isinstance(expiry, str):
        raise ValueError("pendle payload missing expiry")
    return {"implied_pct": implied * 100, "underlying_pct": underlying * 100, "expiry": expiry}


async def _fetch_pendle_rows(entry: PendleRefCfg, get_text: GetText) -> list[dict]:
    url = f"{PENDLE_MARKET_BASE}/{entry.chain_id}/markets/{entry.address}"
    parsed = parse_pendle_market(await get_text(url))
    return [
        {
            "id": entry.implied_id,
            "label": f"{entry.implied_label} {expiry_short(parsed['expiry'])}",
            "value_pct": parsed["implied_pct"],
            "extra": {"expiry": parsed["expiry"]},
        },
        {
            "id": entry.underlying_id,
            "label": entry.underlying_label,
            "value_pct": parsed["underlying_pct"],
            "extra": None,
        },
    ]


def parse_funding_premium(text: str) -> dict:
    """Parse one `premiumIndex` response into annualized pct + the raw 8h rate.

    Raises ValueError if the payload isn't a JSON object or has no
    (numeric) lastFundingRate.
    """
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("binance funding payload is not a JSON object")
    raw = body.get("lastFundingRate")
    if raw is None:
        raise ValueError("binance funding payload missing lastFundingRate")
    try:
        rate = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("binance funding lastFundingRate is not numeric") from exc
    return {"value_pct": rate * 3 * 365 * 100, "rate_8h": rate}


async def _fetch_funding_rows(entry: FundingRefCfg, get_text: GetText) -> list[dict]:
    parsed = parse_funding_premium(await get_text(FUNDING_PREMIUM_URL, params={"symbol": entry.symbol}))
    return [{
        "id": entry.id, "label": entry.label, "value_pct": parsed["value_pct"],
        "extra": {"rate_8h": parsed["rate_8h"]},
    }]


async def fetch_refs(refs: RefsCfg, store: Store, get_text: GetText, post_json: PostJson) -> str:
    prev = store.doc("rate_refs")
    prev_by_id = {r["id"]: r for r in (prev.payload.get("rows", []) if prev else [])}

    fresh: dict[str, dict] = {}
    errors: list[str] = []

    for market in refs.aave:
        try:
            for row in await _fetch_aave_rows(market, post_json):
                fresh[row["id"]] = row
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs aave %s %s failed: %s", market.chain, market.symbol, exc)
            errors.append(f"aave {market.chain} {market.symbol}: {exc}")

    for entry in refs.pendle:
        try:
            for row in await _fetch_pendle_rows(entry, get_text):
                fresh[row["id"]] = row
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs pendle %s failed: %s", entry.address, exc)
            errors.append(f"pendle {entry.address}: {exc}")

    for entry in refs.funding:
        try:
            for row in await _fetch_funding_rows(entry, get_text):
                fresh[row["id"]] = row
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs funding %s failed: %s", entry.symbol, exc)
            errors.append(f"funding {entry.symbol}: {exc}")

    if not fresh:
        raise RuntimeError(f"all refs sources failed: {'; '.join(errors)}")

    # stable row order: aave markets (config order, supply then borrow),
    # pendle rows (config order), funding rows
    order = []
    for market in refs.aave:
        order += [market.supply_id, market.borrow_id]
    for entry in refs.pendle:
        order += [entry.implied_id, entry.underlying_id]
    order += [entry.id for entry in refs.funding]

    rows = []
    for row_id in order:
        if row_id in fresh:
            rows.append(fresh[row_id])
        elif row_id in prev_by_id:
            rows.append(prev_by_id[row_id])  # stale beats gone

    today = datetime.now(timezone.utc).date()
    for row in rows:
        if row["id"] in fresh:  # only fresh rows get a new daily point
            store.upsert_points(f"ref:{row['id']}", [(today, row["value_pct"])])

    store.put_doc("rate_refs", {"rows": rows}, source="refs")
    return "refs"
