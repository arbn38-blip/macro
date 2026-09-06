from collector.runner import run_fetcher
from collector.store import Store


async def test_success_records_active_source(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fn() -> str:
        return "yahoo"

    await run_fetcher("equity", store, fn)
    st = store.statuses()[0]
    assert st["name"] == "equity"
    assert st["active_source"] == "yahoo"
    assert st["last_success"] is not None


async def test_error_recorded_and_swallowed(tmp_path):
    store = Store(tmp_path / "t.db")

    async def fn() -> str:
        raise RuntimeError("upstream down")

    await run_fetcher("news", store, fn)  # must NOT raise
    st = store.statuses()[0]
    assert st["last_error"] == "RuntimeError: upstream down"
    assert st["last_success"] is None
