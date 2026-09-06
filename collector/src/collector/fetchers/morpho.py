"""Morpho Blue GraphQL markets: the underlying isolated lending markets that
Midnight's fixed-rate books quote against — the cleanest floating comparable
for the term structure (spec §2).

One POST to blue-api.morpho.org/graphql covers every configured chain and the
USDC loan asset in one call. `listed: true` is essential: without it the API
returns exploit-bait / unlisted markets (one seen at ~297,892% APY — not a
real yield) — but it is not sufficient: some Ethereum markets are `listed:
true` and still broken, reporting ~298,000% supply/borrow APY at 100%
utilization (msY, AZND — observed live 2026-07-23). An APY sanity band
catches those too. Values are fractions (×100 for a percent); `lltv` is an
18-decimal WAD ratio. Markets with a null collateralAsset are idle (no
collateral configured yet) and are skipped; markets on a chain id the config
doesn't recognize are skipped with a warning.

Writes doc 'morpho_markets' {"rows": [...]}, source "morpho-blue". Empty
rows with a previous non-empty doc keep the previous rows (zyfai-style
guard); a genuinely empty first run writes the empty doc. Daily history: one
point per row into both 'mkt-supply:{chain_id}:{market_id}' and
'mkt-borrow:{chain_id}:{market_id}' (spec §3).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from collector.config import DefiCfg
from collector.http import PostJson
from collector.store import Store

log = logging.getLogger(__name__)

WAD = 10**18
APY_SANITY_MIN = -100.0  # percent; a rate can't shrink principal by more than 100%
APY_SANITY_MAX = 200.0   # percent; real markets don't clear this — the observed failure
                         # mode is a `listed: true` market that's still broken and reports
                         # ~298,000% APY (msY / AZND on Ethereum, seen live 2026-07-23)


def build_query(chain_ids: list[int], usdc_addresses: list[str], first: int) -> str:
    ids = ", ".join(str(c) for c in chain_ids)
    addrs = ", ".join(f'"{a.lower()}"' for a in usdc_addresses)
    return f"""{{ markets(first: {first}, where: {{ chainId_in: [{ids}],
    loanAssetAddress_in: [{addrs}],
    listed: true }},
    orderBy: SupplyAssetsUsd, orderDirection: Desc) {{
  items {{ marketId lltv chain {{ id }}
          loanAsset {{ symbol }} collateralAsset {{ symbol }}
          state {{ supplyApy borrowApy utilization supplyAssetsUsd }} }} }} }}"""


def parse_markets(body: dict) -> list[dict]:
    if "errors" in body:
        raise ValueError(f"morpho graphql errors: {body['errors']}")
    try:
        items = body["data"]["markets"]["items"]
    except (KeyError, TypeError) as exc:
        raise ValueError("morpho payload missing data.markets.items") from exc
    if not isinstance(items, list):
        raise ValueError("morpho payload data.markets.items is not a list")
    return [i for i in items if isinstance(i, dict)]


def _row(item: dict, chain_names: dict[int, str]) -> dict | None:
    try:
        collateral_asset = item["collateralAsset"]
        if collateral_asset is None:
            return None  # idle market: no collateral configured — nothing to show
        chain_id = int(item["chain"]["id"])
        chain_name = chain_names.get(chain_id)
        if chain_name is None:
            log.warning("skipping morpho market on unknown chain id %s", chain_id)
            return None
        market_id = str(item["marketId"])
        state = item["state"]
        supply_apy = float(state["supplyApy"]) * 100
        borrow_apy = float(state["borrowApy"]) * 100
        if not (APY_SANITY_MIN < supply_apy < APY_SANITY_MAX) or \
                not (APY_SANITY_MIN < borrow_apy < APY_SANITY_MAX):
            log.warning("skipping morpho market %s: apy out of sanity band", market_id)
            return None
        return {
            "chain": chain_name,
            "chain_id": chain_id,
            "market_id": market_id,
            "collateral": str(collateral_asset["symbol"]),
            "lltv_pct": int(item["lltv"]) / WAD * 100,
            "supply_apy": supply_apy,
            "borrow_apy": borrow_apy,
            "utilization_pct": float(state["utilization"]) * 100,
            "tvl_usd": float(state["supplyAssetsUsd"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def fetch_morpho(defi: DefiCfg, store: Store, post_json: PostJson) -> str:
    chain_names = {c.id: c.name for c in defi.chains}
    query = build_query([c.id for c in defi.chains], [c.usdc for c in defi.chains], defi.morpho_first)
    try:
        body = await post_json(defi.morpho_graphql, json={"query": query})
        items = parse_markets(body)
    except Exception as exc:  # noqa: BLE001 — request/parse failure must raise, doc survives
        raise RuntimeError(f"morpho request/parse failed: {exc}") from exc

    rows: list[dict] = []
    for item in items:
        row = _row(item, chain_names)
        if row is None:
            continue
        rows.append(row)
    rows.sort(key=lambda r: -r["tvl_usd"])  # API already sorts TVL-desc; sort anyway

    if not rows:
        prev = store.doc("morpho_markets")
        if prev and prev.payload.get("rows"):
            log.warning("morpho returned zero markets; keeping previous doc")
            return "morpho-blue"

    today = datetime.now(timezone.utc).date()
    for row in rows:
        store.upsert_points(
            f"mkt-supply:{row['chain_id']}:{row['market_id']}", [(today, row["supply_apy"])]
        )
        store.upsert_points(
            f"mkt-borrow:{row['chain_id']}:{row['market_id']}", [(today, row["borrow_apy"])]
        )
    store.put_doc("morpho_markets", {"rows": rows}, source="morpho-blue")
    return "morpho-blue"
