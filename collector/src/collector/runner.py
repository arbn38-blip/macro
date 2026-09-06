"""Wraps every fetcher run: status recording + total error isolation."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from collector.store import Store

log = logging.getLogger(__name__)

FetchFn = Callable[[], Awaitable[str]]  # returns active source label on success


async def run_fetcher(name: str, store: Store, fn: FetchFn) -> None:
    try:
        active_source = await fn()
        store.record_success(name, active_source)
    except Exception as exc:  # noqa: BLE001 — isolation is the contract
        msg = f"{type(exc).__name__}: {exc}"
        log.warning("fetcher %s failed: %s", name, msg, exc_info=exc)
        store.record_error(name, msg)
