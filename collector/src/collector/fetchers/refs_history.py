"""Full-history backfill for the RATE REFS panel (daily job, cadence 86400s).

Mirrors the macro / macro_history split: this is the 'refs_history' half of
the live 'refs' fetcher (collector.fetchers.refs). Upserts into the same
'ref:{id}' series so the existing chart overlay and the panel's bp-change
math work unchanged for both live and backfilled points. Idempotent
(upsert_points); each source degrades independently; total failure raises.

Aave borrow has no free backfill source (DefiLlama's borrow-APY endpoints
started returning HTTP 402 on 2026-07-22) -- it simply accumulates from the
daily point fetch_refs records each run.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from collector.config import FundingRefCfg, LlamaChartCfg, PendleRefCfg, RefsCfg
from collector.http import GetText
from collector.store import Store

log = logging.getLogger(__name__)

LLAMA_CHART_BASE = "https://yields.llama.fi/chart"
PENDLE_APY_HISTORY_BASE = "https://api-v2.pendle.finance/core/v2"
FUNDING_RATE_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
FUNDING_PAGE_LIMIT = 1000
FUNDING_MAX_PAGES = 20


def parse_llama_chart(text: str) -> list[tuple[date, float]]:
    """Parse a DefiLlama `/chart/{pool}` response into daily (date, apyBase) points.

    Entries with a null apyBase (no data that day) are skipped; a malformed
    individual point is skipped rather than failing the whole series.
    """
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("llama chart payload is not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise ValueError("llama chart payload has no data list")
    out: list[tuple[date, float]] = []
    for pt in data:
        try:
            apy_base = pt["apyBase"]
            if apy_base is None:
                continue
            d = datetime.fromisoformat(pt["timestamp"].replace("Z", "+00:00")).date()
            out.append((d, float(apy_base)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def _fetch_llama_points(entry: LlamaChartCfg, get_text: GetText) -> list[tuple[date, float]]:
    return parse_llama_chart(await get_text(f"{LLAMA_CHART_BASE}/{entry.pool}"))


def parse_pendle_apy_history(text: str) -> dict[str, list[tuple[date, float]]]:
    """Parse a Pendle `apy-history` response: `results` is a CSV STRING with
    header `timestamp,underlyingApy,impliedApy`. Values are fractions (0.0447
    -> 4.47%); a malformed row is skipped rather than failing the whole series.
    """
    body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("pendle apy-history payload is not a JSON object")
    results = body.get("results")
    if not isinstance(results, str):
        raise ValueError("pendle apy-history payload missing results CSV")
    implied: list[tuple[date, float]] = []
    underlying: list[tuple[date, float]] = []
    for row in csv.DictReader(io.StringIO(results)):
        try:
            d = datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc).date()
            implied.append((d, float(row["impliedApy"]) * 100))
            underlying.append((d, float(row["underlyingApy"]) * 100))
        except (KeyError, TypeError, ValueError):
            continue
    return {"implied": implied, "underlying": underlying}


async def _fetch_pendle_history(
    entry: PendleRefCfg, get_text: GetText
) -> dict[str, list[tuple[date, float]]]:
    url = f"{PENDLE_APY_HISTORY_BASE}/{entry.chain_id}/markets/{entry.address}/apy-history"
    return parse_pendle_apy_history(await get_text(url, params={"time_frame": "day"}))


def daily_mean_annualized(rows: list[dict]) -> list[tuple[date, float]]:
    """Group raw 8h Binance funding rates by UTC date and annualize the daily
    mean: Binance settles 3x/day, so `mean(rate) * 3 * 365 * 100`. A malformed
    row is skipped rather than failing the whole batch.
    """
    by_day: dict[date, list[float]] = defaultdict(list)
    for row in rows:
        try:
            d = datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=timezone.utc).date()
            rate = float(row["fundingRate"])
        except (KeyError, TypeError, ValueError):
            continue
        by_day[d].append(rate)
    return [(d, (sum(rates) / len(rates)) * 3 * 365 * 100) for d, rates in by_day.items()]


async def _fetch_funding_history(entry: FundingRefCfg, get_text: GetText) -> list[tuple[date, float]]:
    """Paginate `fundingRate` by `startTime` until a short/empty page ends the
    series, hard-capped at FUNDING_MAX_PAGES pages.
    """
    all_rows: list[dict] = []
    start_time = 0
    for _ in range(FUNDING_MAX_PAGES):
        body = json.loads(await get_text(FUNDING_RATE_URL, params={
            "symbol": entry.symbol, "limit": str(FUNDING_PAGE_LIMIT), "startTime": str(start_time),
        }))
        if not isinstance(body, list):
            raise ValueError("binance fundingRate payload is not a JSON array")
        if not body:
            break
        all_rows.extend(body)
        if len(body) < FUNDING_PAGE_LIMIT:
            break
        start_time = int(body[-1]["fundingTime"]) + 1
    return daily_mean_annualized(all_rows)


async def fetch_refs_history(refs: RefsCfg, store: Store, get_text: GetText) -> str:
    ok = 0
    errors: list[str] = []

    for entry in refs.llama_chart:
        try:
            store.upsert_points(f"ref:{entry.series}", await _fetch_llama_points(entry, get_text))
            ok += 1
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs_history llama %s failed: %s", entry.pool, exc)
            errors.append(f"llama {entry.pool}: {exc}")

    for entry in refs.pendle:
        try:
            history = await _fetch_pendle_history(entry, get_text)
            store.upsert_points(f"ref:{entry.implied_id}", history["implied"])
            store.upsert_points(f"ref:{entry.underlying_id}", history["underlying"])
            ok += 1
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs_history pendle %s failed: %s", entry.address, exc)
            errors.append(f"pendle {entry.address}: {exc}")

    for entry in refs.funding:
        try:
            store.upsert_points(f"ref:{entry.id}", await _fetch_funding_history(entry, get_text))
            ok += 1
        except Exception as exc:  # noqa: BLE001 — per-source isolation
            log.warning("refs_history funding %s failed: %s", entry.symbol, exc)
            errors.append(f"funding {entry.symbol}: {exc}")

    if ok == 0:
        raise RuntimeError(f"all refs-history sources failed: {'; '.join(errors)}")
    return "refs-history"
