"""WebSearchTool: endpoint routing + provider-agnostic normalization.

The Sources panel surfaces real web sources only if `web_search` actually
returns results. DuckDuckGo scraping is blocked in some networks (e.g.
mainland CN), so the tool must honor a configured JSON endpoint (SearXNG /
Bing / Tavily / Serper) — these tests pin that contract without touching the
network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.tools.builtin import WebSearchTool


# --------------------------------------------------------------------------- #
# Settings loads the web-search config (previously a dead getattr).
# --------------------------------------------------------------------------- #
def test_settings_loads_web_search_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WEB_SEARCH_ENDPOINT", "https://search.example/api")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "secret-key")
    monkeypatch.setenv("WEB_SEARCH_METHOD", "post")
    s = Settings()
    assert s.WEB_SEARCH_ENDPOINT == "https://search.example/api"
    assert s.WEB_SEARCH_API_KEY == "secret-key"
    assert s.WEB_SEARCH_METHOD == "post"


def test_settings_defaults_are_safe():
    # _env_file=None isolates from the dev .env (which may configure a real
    # provider) so this asserts the PURE defaults: empty endpoint → DDG fallback.
    s = Settings(_env_file=None)
    assert getattr(s, "WEB_SEARCH_ENDPOINT", "") == ""
    assert getattr(s, "WEB_SEARCH_METHOD", "get") == "get"
    assert getattr(s, "WEB_SEARCH_DEPTH", "advanced") == "advanced"


# --------------------------------------------------------------------------- #
# _normalize_search_data covers the major providers.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload, expect_url, expect_snippet",
    [
        # SearXNG / Google CSE-style: items[]
        (
            {"items": [{"title": "A", "link": "https://a.com", "snippet": "snip-a"}]},
            "https://a.com",
            "snip-a",
        ),
        # DuckDuckGo JSON: Results[]
        (
            {"Results": [{"title": "B", "FirstURL": "https://b.com", "Text": "snip-b"}]},
            "https://b.com",
            "snip-b",
        ),
        # Tavily: results[].content
        (
            {"results": [{"title": "C", "url": "https://c.com", "content": "snip-c"}]},
            "https://c.com",
            "snip-c",
        ),
        # Serper: organic[]
        (
            {"organic": [{"title": "D", "link": "https://d.com", "snippet": "snip-d"}]},
            "https://d.com",
            "snip-d",
        ),
        # Bing v7: webPages.value[]
        (
            {"webPages": {"value": [{"name": "E", "url": "https://e.com", "snippet": "snip-e"}]}},
            "https://e.com",
            "snip-e",
        ),
    ],
)
def test_normalize_handles_provider_shapes(payload, expect_url, expect_snippet):
    out = WebSearchTool._normalize_search_data(payload, 5)
    assert out and out[0]["url"] == expect_url
    assert out[0]["snippet"] == expect_snippet


def test_normalize_drops_items_without_title_or_url():
    out = WebSearchTool._normalize_search_data(
        {"results": [{"title": "x"}, {"url": "https://y.com"}, {"title": "", "url": ""}]}, 5
    )
    assert len(out) == 2  # the empty one is dropped


def test_normalize_caps_at_top_k():
    payload = {"results": [{"title": str(i), "url": f"https://x.com/{i}"} for i in range(20)]}
    assert len(WebSearchTool._normalize_search_data(payload, 3)) == 3


# --------------------------------------------------------------------------- #
# Endpoint routing: GET vs POST, key propagation, DDG fallback.
# --------------------------------------------------------------------------- #
class _StreamResp:
    """Fake streaming httpx response: yields fixed body bytes, exposes the
    attributes ``_bounded_request`` reads (status_code, headers, url, aiter_raw)."""

    def __init__(self, body_bytes: bytes, url: str = "https://search.example/api"):
        self._body = body_bytes
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a: object) -> bool:
        return False

    async def aiter_raw(self):
        yield self._body


def _install_fake_httpx(monkeypatch: pytest.MonkeyPatch, payload: Any, *, raise_on_json: bool = False):
    """Replace httpx.AsyncClient with a recorder; return the calls list.

    The tool now reads responses via ``client.stream(...)`` (bounded read), so the
    fake exposes ``stream`` returning a :class:`_StreamResp` async context manager.
    Recorded call tuples keep the historic shape: (method, url, params_or_body, headers).
    """
    calls: list[tuple] = []
    body_bytes = (
        b"<html>not json</html>" if raise_on_json else json.dumps(payload).encode()
    )

    class _Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

        def stream(self, method, url, **kwargs: Any):
            if str(method).lower() == "get":
                calls.append(("get", url, kwargs.get("params"), kwargs.get("headers")))
            else:
                calls.append(("post", url, kwargs.get("json"), kwargs.get("headers")))
            return _StreamResp(body_bytes, url)

    monkeypatch.setattr("app.tools.builtin.httpx.AsyncClient", _Client)
    return calls


def _settings(endpoint="", api_key="", method="get"):
    return SimpleNamespace(
        WEB_SEARCH_ENDPOINT=endpoint,
        WEB_SEARCH_API_KEY=api_key,
        WEB_SEARCH_METHOD=method,
    )


async def test_endpoint_get_sends_query_and_key(monkeypatch: pytest.MonkeyPatch):
    payload = {"results": [{"title": "T", "url": "https://t.com", "snippet": "s"}]}
    calls = _install_fake_httpx(monkeypatch, payload)
    monkeypatch.setattr("app.tools.builtin.get_settings", lambda: _settings(
        endpoint="https://search.example/api", api_key="k-123", method="get"))

    out = await WebSearchTool().run(query="rust async", top_k=5)

    assert out["ok"] is True
    assert out["results"][0]["url"] == "https://t.com"
    method, url, params, headers = calls[0]
    assert method == "get"
    assert url == "https://search.example/api"
    assert params["q"] == "rust async"
    # Key propagated in the common provider-accepted header forms.
    assert headers["Authorization"] == "Bearer k-123"
    assert headers["Ocp-Apim-Subscription-Key"] == "k-123"


async def test_endpoint_post_sends_body_and_serper_key(monkeypatch: pytest.MonkeyPatch):
    payload = {"organic": [{"title": "T", "link": "https://t.com", "snippet": "s"}]}
    calls = _install_fake_httpx(monkeypatch, payload)
    monkeypatch.setattr("app.tools.builtin.get_settings", lambda: _settings(
        endpoint="https://google.serper.dev/search", api_key="serper-key", method="post"))

    out = await WebSearchTool().run(query="glm-5", top_k=3)

    assert out["ok"] is True
    assert out["results"][0]["url"] == "https://t.com"
    method, url, body, headers = calls[0]
    assert method == "post"
    assert body["q"] == "glm-5"
    assert body["api_key"] == "serper-key"        # Tavily body key
    assert headers["X-API-DB-KEY"] == "serper-key"  # Serper header key


async def test_non_json_endpoint_falls_back_to_ddg(monkeypatch: pytest.MonkeyPatch):
    # Endpoint returns HTML -> json() raises -> the tool hands off to DDG scrape.
    _install_fake_httpx(monkeypatch, None, raise_on_json=True)
    monkeypatch.setattr("app.tools.builtin.get_settings", lambda: _settings(
        endpoint="https://search.example/api"))

    ddg_called = {"v": False}

    async def fake_ddg(self, query, top_k):
        ddg_called["v"] = True
        return [{"title": "ddg", "url": "https://ddg.com", "snippet": ""}]

    monkeypatch.setattr(WebSearchTool, "_search_duckduckgo", fake_ddg)

    out = await WebSearchTool().run(query="x", top_k=5)
    assert ddg_called["v"] is True
    assert out["results"][0]["url"] == "https://ddg.com"


async def test_no_endpoint_uses_duckduckgo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.tools.builtin.get_settings", lambda: _settings(endpoint=""))

    ddg_called = {"v": False}

    async def fake_ddg(self, query, top_k):
        ddg_called["v"] = True
        return [{"title": "only", "url": "https://only.com", "snippet": ""}]

    monkeypatch.setattr(WebSearchTool, "_search_duckduckgo", fake_ddg)

    out = await WebSearchTool().run(query="x", top_k=5)
    assert ddg_called["v"] is True
    assert out["results"][0]["url"] == "https://only.com"


async def test_duckduckgo_scrape_does_not_shadow_html_module(monkeypatch: pytest.MonkeyPatch):
    """Regression: _search_duckduckgo imports stdlib `html` for entity decoding
    but previously ALSO bound the response body to a local named ``html``. The
    later ``html.unescape`` then hit a str, so EVERY DuckDuckGo search returned
    ``{'ok': False, 'error': "'str' object has no attribute 'unescape'"}`` and the
    Sources panel stayed empty. This runs the REAL scrape (mocked HTML) to guard it.
    """
    fake_html = (
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ftokio.rs%2F">'
        "Tokio&#x27;s runtime</a>"
        '<a class="result__snippet">async runtime for Rust</a>'
    )

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

        def stream(self, method, url, **kwargs: Any):
            return _StreamResp(fake_html.encode(), url)

    monkeypatch.setattr("app.tools.builtin.httpx.AsyncClient", _Client)
    out = await WebSearchTool().run(query="tokio", top_k=3)
    assert out["ok"] is True, out
    assert out["results"], out
    assert out["results"][0]["url"] == "https://tokio.rs/"
    # The HTML entity was decoded (stdlib html.unescape), not left as &#x27;.
    assert "'" in out["results"][0]["title"]
