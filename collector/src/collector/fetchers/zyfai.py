"""Zyfai DeFi yield opportunities: USDC pools across risk tiers and chains.

Tiers are nested (safe ⊂ degen ⊂ async): a pool is listed once, under the most
conservative tier that contains it — strategies iterate in config order, first
listing wins. Writes the 'defi_pools' doc and records one daily combined-APY
point per pool to series 'defi:{chain_id}:{pool_address}' (history accumulates
from day one; no chart UI yet — spec §3.1).

Undocumented public API: shape changes surface as parse failures. On total
endpoint failure the run raises and the previous doc survives; a run where every
endpoint answers but returns zero pools also keeps the previous non-empty doc
(its aging timestamp marks the panel stale).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from collector.config import DefiCfg
from collector.http import GetText
from collector.store import Store

log = logging.getLogger(__name__)


def parse_opportunities(text: str) -> list[dict]:
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("zyfai payload is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("zyfai payload has no data list")
    return [o for o in data if isinstance(o, dict)]


def _opt_float(x) -> float | None:
    return None if x is None else float(x)


def _row(opp: dict, tier: str, chain_id: int, chain_name: str) -> dict | None:
    try:
        return {
            "tier": tier,
            "chain": chain_name,
            "chain_id": chain_id,
            "pool_address": str(opp["pool_address"]),
            "protocol": str(opp["protocol_name"]),
            "pool": str(opp["pool_name"]),
            "apy": float(opp["combined_apy"]),
            "apy_7d": _opt_float(opp.get("averageCombinedApy7Days")),
            "apy_30d": _opt_float(opp.get("averageCombinedApy30Days")),
            "tvl_usd": _opt_float(opp.get("tvlUsd")),
            "url": opp.get("url"),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def fetch_defi(defi: DefiCfg, base_url: str, store: Store, get_text: GetText) -> str:
    rows: list[dict] = []
    seen: set[tuple[int, str]] = set()
    ok = 0
    errors: list[str] = []
    for strat in defi.strategies:  # strategy-major: most conservative tier claims the pool
        for chain in defi.chains:
            params = {"asset": defi.asset, "chainId": str(chain.id), "status": "live"}
            try:
                opps = parse_opportunities(await get_text(f"{base_url}/{strat.id}", params=params))
                ok += 1
            except Exception as exc:  # noqa: BLE001 — one dead endpoint degrades, not kills
                errors.append(f"{strat.id}/{chain.name}: {exc}")
                log.warning("zyfai %s/%s failed: %s", strat.id, chain.name, exc)
                continue
            for opp in opps:
                row = _row(opp, strat.label, chain.id, chain.name)
                if row is None:
                    log.warning("skipping malformed zyfai opportunity in %s/%s", strat.id, chain.name)
                    continue
                key = (chain.id, row["pool_address"].lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
    if ok == 0:
        raise RuntimeError(f"all zyfai endpoints failed: {'; '.join(errors)}")
    if not rows:
        prev = store.doc("defi_pools")
        if prev and prev.payload.get("rows"):
            # all endpoints answered but with zero pools — likely an API-side
            # outage; keep the last good rows and let the stale footer show it
            log.warning("zyfai returned zero pools everywhere; keeping previous doc")
            return "zyfai"
    tier_rank = {s.label: i for i, s in enumerate(defi.strategies)}
    rows.sort(key=lambda r: (tier_rank[r["tier"]], -r["apy"]))
    today = datetime.now(timezone.utc).date()
    for row in rows:
        store.upsert_points(
            f"defi:{row['chain_id']}:{row['pool_address'].lower()}", [(today, row["apy"])]
        )
    store.put_doc("defi_pools", {"rows": rows}, source="zyfai")
    return "zyfai"
