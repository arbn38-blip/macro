import httpx

from collector import http


async def test_get_text_strips_query_from_http_errors(monkeypatch):
    async def fake_get(self, url, params=None):
        req = httpx.Request("GET", f"{url}?api_key=SECRET123")
        return httpx.Response(429, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    try:
        await http.get_text("https://api.stlouisfed.org/x", params={"api_key": "SECRET123"})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "SECRET123" not in str(exc)
        assert "429" in str(exc)
        assert exc.__suppress_context__  # chained cause would re-leak the URL


def _patch_client(monkeypatch, seen):
    def fake_init(self, *args, headers=None, **kwargs):
        seen["headers"] = headers

    async def fake_aenter(self):
        return self

    async def fake_aexit(self, *args):
        return None

    async def fake_get(self, url, params=None):
        req = httpx.Request("GET", url)
        return httpx.Response(200, request=req, text="ok")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    monkeypatch.setattr(httpx.AsyncClient, "__aenter__", fake_aenter)
    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", fake_aexit)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_get_text_uses_custom_headers_when_given(monkeypatch):
    seen = {}
    _patch_client(monkeypatch, seen)

    custom = {"User-Agent": "Mozilla/5.0 fake"}
    result = await http.get_text("https://example.com/x", headers=custom)
    assert result == "ok"
    assert seen["headers"] == custom


async def test_get_text_defaults_headers_when_not_given(monkeypatch):
    seen = {}
    _patch_client(monkeypatch, seen)

    await http.get_text("https://example.com/x")
    assert seen["headers"] == {"User-Agent": http.USER_AGENT}
    assert "Mozilla" not in http.USER_AGENT  # never impersonate a browser


async def test_get_bytes_returns_content(monkeypatch):
    async def fake_get(self, url, params=None):
        req = httpx.Request("GET", url)
        return httpx.Response(200, request=req, content=b"\x00\x01binary")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await http.get_bytes("https://example.com/f.xls") == b"\x00\x01binary"


async def test_get_bytes_strips_query_from_http_errors(monkeypatch):
    async def fake_get(self, url, params=None):
        req = httpx.Request("GET", f"{url}?api_key=SECRET123")
        return httpx.Response(403, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    try:
        await http.get_bytes("https://example.com/f.xls", params={"api_key": "SECRET123"})
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "SECRET123" not in str(exc)
        assert "403" in str(exc)
        assert exc.__suppress_context__
