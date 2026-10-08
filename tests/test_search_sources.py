from pathlib import Path

import httpx
import pytest
from eventfinder.config import SourceDefinition
from eventfinder.sources import (
    RequestLimiter,
    RobotsPolicy,
    SearchEventSource,
    SourceFetchError,
)
from eventfinder.urls import URLSafety


class _SearchSettings:
    def __init__(
        self,
        *,
        serper_api_key: str | None = None,
        brave_search_api_key: str | None = None,
        tavily_api_key: str | None = None,
        exa_api_key: str | None = None,
    ):
        self.serper_api_key = serper_api_key
        self.brave_search_api_key = brave_search_api_key
        self.tavily_api_key = tavily_api_key
        self.exa_api_key = exa_api_key


async def _safe_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _search_source(client: httpx.AsyncClient) -> SearchEventSource:
    safety = URLSafety(_safe_resolver)
    return SearchEventSource(
        SourceDefinition(
            name="search",
            adapter="search",
            query="technical events",
            allowed_domains=["events.example.test"],
            rate_limit_seconds=0,
        ),
        client,
        RobotsPolicy(client, safety),
        safety,
        RequestLimiter(),
    )


def _platform_event_fixture() -> str:
    return Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_empty_ddgs_results_use_fallback_and_hydrate(monkeypatch):
    from eventfinder import sources

    class EmptySearch:
        def text(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(sources, "DDGS", EmptySearch)
    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(serper_api_key="synthetic-serper-key"),
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host, request.url.path))
        if request.url.host == "google.serper.dev":
            assert request.headers["x-api-key"] == "synthetic-serper-key"
            return httpx.Response(
                200,
                json={"organic": [{"link": "https://events.example.test/platform-fixture"}]},
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "events.example.test":
            return httpx.Response(200, text=_platform_event_fixture())
        raise AssertionError(f"unexpected request to {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _search_source(client).fetch()

    assert result.candidates
    assert ("google.serper.dev", "/search") in requests
    assert ("events.example.test", "/platform-fixture") in requests


@pytest.mark.asyncio
async def test_empty_first_fallback_provider_continues_to_next(monkeypatch):
    from eventfinder import sources

    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(
            serper_api_key="synthetic-serper-key",
            brave_search_api_key="synthetic-brave-key",
        ),
    )
    providers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        providers.append(request.url.host or "")
        if request.url.host == "google.serper.dev":
            return httpx.Response(200, json={"organic": []})
        if request.url.host == "api.search.brave.com":
            return httpx.Response(
                200,
                json={"web": {"results": [{"url": "https://events.example.test/next"}]}},
            )
        raise AssertionError(f"unexpected provider {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results("query")

    assert results == [{"href": "https://events.example.test/next"}]
    assert providers == ["google.serper.dev", "api.search.brave.com"]


@pytest.mark.asyncio
async def test_all_fallback_provider_errors_are_aggregated(monkeypatch):
    from eventfinder import sources

    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(
            serper_api_key="synthetic-serper-key",
            brave_search_api_key="synthetic-brave-key",
            tavily_api_key="synthetic-tavily-key",
            exa_api_key="synthetic-exa-key",
        ),
    )
    statuses = {
        "google.serper.dev": 500,
        "api.search.brave.com": 502,
        "api.tavily.com": 432,
        "api.exa.ai": 402,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses[request.url.host or ""])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceFetchError) as caught:
            await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))

    message = str(caught.value)
    assert "ddgs: ddgs down" in message
    assert "serper:" in message
    assert "brave:" in message
    assert "tavily:" in message
    assert "exa:" in message
    assert "432" in message
    assert "402" in message
    assert all(
        secret not in message
        for secret in (
            "synthetic-serper-key",
            "synthetic-brave-key",
            "synthetic-tavily-key",
            "synthetic-exa-key",
        )
    )


@pytest.mark.asyncio
async def test_ddgs_failure_and_successful_empty_provider_return_empty(monkeypatch):
    from eventfinder import sources

    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(serper_api_key="synthetic-serper-key"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"organic": []})
        )
    ) as client:
        results = await _search_source(client)._fallback_results(
            "query", RuntimeError("ddgs down")
        )

    assert results == []


@pytest.mark.asyncio
async def test_provider_failure_then_successful_empty_provider_return_empty(monkeypatch):
    from eventfinder import sources

    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(
            serper_api_key="synthetic-serper-key",
            brave_search_api_key="synthetic-brave-key",
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            return httpx.Response(500)
        if request.url.host == "api.search.brave.com":
            return httpx.Response(200, json={"web": {"results": []}})
        raise AssertionError(f"unexpected provider {request.url.host}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results(
            "query", RuntimeError("ddgs down")
        )

    assert results == []


@pytest.mark.asyncio
async def test_successful_empty_ddgs_result_counts_when_fallback_fails(monkeypatch):
    from eventfinder import sources

    class EmptySearch:
        def text(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(sources, "DDGS", EmptySearch)
    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(serper_api_key="synthetic-serper-key"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as client:
        result = await _search_source(client).fetch()

    assert result.candidates == []


@pytest.mark.asyncio
async def test_empty_search_without_configured_fallback_is_successful_empty(monkeypatch):
    from eventfinder import sources

    class EmptySearch:
        def text(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(sources, "DDGS", EmptySearch)
    monkeypatch.setattr(sources, "get_settings", lambda: _SearchSettings())

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(AssertionError("no provider should run"))
        )
    ) as client:
        result = await _search_source(client).fetch()
        with pytest.raises(SourceFetchError, match="ddgs: ddgs down"):
            await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))

    assert result.candidates == []


@pytest.mark.asyncio
async def test_successful_empty_provider_results_return_empty(monkeypatch):
    from eventfinder import sources

    monkeypatch.setattr(
        sources,
        "get_settings",
        lambda: _SearchSettings(serper_api_key="synthetic-serper-key"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"organic": []})
        )
    ) as client:
        results = await _search_source(client)._fallback_results("query")

    assert results == []


@pytest.mark.asyncio
async def test_nonempty_ddgs_results_short_circuit_fallback_and_keep_domain_boundary(
    monkeypatch,
):
    from eventfinder import sources

    class NonemptySearch:
        def text(self, *_args, **_kwargs):
            return [
                {"href": "https://events.example.test/platform-fixture"},
                {"href": "https://outside.example.test/should-not-fetch"},
            ]

    monkeypatch.setattr(sources, "DDGS", NonemptySearch)

    async def forbidden_fallback(*_args):
        raise AssertionError("nonempty DDGS results should short-circuit fallback")

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host or "", request.url.path))
        if request.url.host == "events.example.test" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.host == "events.example.test":
            return httpx.Response(200, text=_platform_event_fixture())
        raise AssertionError(f"disallowed destination was fetched: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = _search_source(client)
        source._fallback_results = forbidden_fallback
        result = await source.fetch()

    assert result.candidates
    assert ("events.example.test", "/platform-fixture") in requests
    assert all(host == "events.example.test" for host, _path in requests)


@pytest.mark.asyncio
async def test_tavily_uses_bearer_header_without_key_in_body():
    key = "synthetic-tavily-key-for-test"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={"results": [{"url": "https://events.example.test/tavily-result"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._search_tavily("query", key)

    assert captured["authorization"] == f"Bearer {key}"
    assert captured["body"] == '{"query":"query","max_results":12}'
    assert key not in str(captured["body"])
    assert results == [{"href": "https://events.example.test/tavily-result"}]
