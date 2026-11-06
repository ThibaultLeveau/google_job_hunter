import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from test_profiles import NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.application.search_provider import SearchError, SearchRequest
from job_hunter.infrastructure.search_provider import (
    FixtureSearchProvider,
    SearchSettings,
    SerpApiSearchProvider,
    create_search_provider,
    load_search_settings,
)
from job_hunter.settings import ConfigurationError, Settings

owners = profile_owners
RAW = Path(__file__).with_name("fixtures").joinpath("search_page.json").read_bytes()
SECRET = "synthetic-provider-credential"
REQUEST = SearchRequest(
    query='("private query")',
    parameters={
        "lr": "lang_fr",
        "gl": "fr",
        "as_qdr": "d7",
        "location": "Paris, France",
    },
)


def test_contract_pagination_and_private_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert SECRET.encode() not in RAW and b"api_key" not in RAW
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for name in ("httpx", "httpcore.connection", "httpcore.http11"):
            logging.getLogger(name).warning("Private request %s", request.url)
        body = json.loads(RAW)
        body["search_parameters"]["api_key"] = SECRET
        if request.url.params["start"] == "10":
            body["organic_results"] *= 5
            body["serpapi_pagination"]["next"] = (
                "https://serpapi.com/search.json?start=20"
            )
        body["serpapi_pagination"]["next"] += "&api_key=" + SECRET
        return httpx.Response(200, json=body)

    provider = SerpApiSearchProvider(
        SearchSettings(provider="serpapi", api_key=SecretStr(SECRET), max_results=15),
        httpx.MockTransport(respond),
    )
    caplog.set_level(logging.DEBUG)
    first = provider.fetch(REQUEST)
    second = provider.fetch(REQUEST.model_copy(update={"page": 1}))
    logging.getLogger("httpx").warning("Unrelated diagnostic")
    assert [result.rank for result in first.results] == [1, 2]
    assert first.results[0].title == "Développeur Python"
    assert first.results[1].snippet is None and first.next_page == 1
    assert len(second.results) == 5 and second.truncated and second.next_page is None
    assert second.results[0].rank == 11
    assert first.usage.request_id == "fixture-search-1" and first.usage.requests == 1
    assert first.usage.received == first.usage.returned == 2
    serialized = first.model_dump_json() + second.model_dump_json()
    assert SECRET not in serialized + caplog.text and REQUEST.query not in caplog.text
    assert "json_endpoint" not in serialized and "Unrelated diagnostic" in caplog.text
    assert any("search_usage" in record.__dict__ for record in caplog.records)
    assert len(calls) == 2
    assert dict(calls[0].url.params) == {
        **REQUEST.parameters,
        "engine": "google",
        "q": REQUEST.query,
        "start": "0",
        "output": "json",
        "api_key": SECRET,
    }
    with pytest.raises(SearchError, match="input_limit"):
        provider.fetch(REQUEST.model_copy(update={"page": 2}))
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:pass@example.org/job",
        "https://example.org/?api_key=",
        "https://example.org/#access_token=x",
        "https://example.org:wrong/job",
        "https://example.org/with space",
        f"https://example.org/{SECRET}",
        "https://%65xample.org/job",
        "https://example.org/\\job",
        "https://[invalid/job",
    ],
)
def test_malformed_entries_and_reflected_credentials_are_discarded(url: str) -> None:
    body = json.loads(RAW)
    body["organic_results"][0]["link"] = url
    body["organic_results"] += [
        None,
        {"position": True},
        {"position": 0},
        {
            "position": 3,
            "title": SECRET,
            "link": "https://example.org/job",
        },
    ]
    body["search_metadata"]["id"] = SECRET
    provider = SerpApiSearchProvider(
        SearchSettings(provider="serpapi", api_key=SecretStr(SECRET)),
        httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )
    page = provider.fetch(REQUEST)
    assert len(page.results) == 1 and page.results[0].rank == 2
    assert page.usage.skipped == 5 and page.usage.request_id is None
    assert SECRET not in page.model_dump_json()


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {}, "authentication"),
        (403, {}, "authentication"),
        (429, {"error": "Your account has run out of searches."}, "quota"),
        (429, {"error": "Hourly rate exceeded"}, "rate_limited"),
        (503, {}, "provider_failure"),
        (302, {}, "provider_failure"),
        (200, [], "invalid_response"),
        (200, {"search_metadata": None}, "invalid_response"),
        (200, {"search_metadata": {"status": "Error"}}, "invalid_response"),
        (
            200,
            {"search_metadata": {"status": "Success"}, "organic_results": {}},
            "invalid_response",
        ),
    ],
)
def test_error_classification(status: int, body: Any, code: str) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status, json=body, headers={"location": "https://elsewhere.invalid"}
        )

    provider = SerpApiSearchProvider(
        SearchSettings(provider="serpapi", api_key=SecretStr(SECRET)),
        httpx.MockTransport(respond),
    )
    with pytest.raises(SearchError) as error:
        provider.fetch(REQUEST)
    assert error.value.code == code and len(calls) == 1
    assert error.value.usage is not None and error.value.usage.requests == 1
    assert SECRET not in str(error.value) + repr(error.value.usage)


@pytest.mark.parametrize(
    "kind",
    "timeout deadline network bad_json large compressed pagination missing".split(),
)
def test_bounded_transport_and_empty_success(kind: str) -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if kind == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if kind == "deadline":
            await asyncio.Event().wait()
        if kind == "network":
            raise httpx.ConnectError(SECRET, request=request)
        if kind in {"bad_json", "large", "compressed"}:
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"x" * (1025 if kind == "large" else 1)),
                headers={
                    "content-encoding": "br" if kind == "compressed" else "identity"
                },
            )
        body = {
            "search_metadata": {"status": "Success"},
            "error": "Google hasn't returned any results for this query.",
        }
        if kind == "pagination":
            body["serpapi_pagination"] = {"next": "https://elsewhere.invalid/?start=10"}
        return httpx.Response(200, json=body)

    provider = SerpApiSearchProvider(
        SearchSettings(
            provider="serpapi",
            api_key=SecretStr(SECRET),
            max_response_bytes=1024,
            timeout_seconds=0.01 if kind == "deadline" else 1,
        ),
        httpx.MockTransport(respond),
    )
    if kind == "missing":
        page = provider.fetch(REQUEST)
        assert (
            page.results == () and page.next_page is None and page.usage.received == 0
        )
    else:
        with pytest.raises(SearchError) as error:
            provider.fetch(REQUEST)
        assert error.value.code == {
            "timeout": "timeout",
            "deadline": "timeout",
            "network": "provider_failure",
        }.get(kind, "invalid_response")
        assert SECRET not in str(error.value)
    assert calls == 1


def test_fixtures_disabled_state_and_input_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = create_search_provider(
        SearchSettings(provider="fixture", environment="test", max_pages=1), (RAW,)
    )
    page = fixture.fetch(REQUEST)
    assert page.usage.requests == 0 and page.next_page is None and page.truncated
    with pytest.raises(SearchError, match="input_limit"):
        fixture.fetch(REQUEST.model_copy(update={"page": 1}))
    empty = create_search_provider(
        SearchSettings(provider="fixture", environment="test")
    )
    assert empty.fetch(REQUEST).results == ()
    for provider, status in (("disabled", "disabled"), ("serpapi", "unconfigured")):
        monkeypatch.setenv("JOB_HUNTER_SEARCH_PROVIDER", provider)
        service = create_search_provider(load_search_settings())
        assert service.capability.status == status
        with pytest.raises(SearchError, match=status):
            service.fetch(REQUEST)
    with pytest.raises(ConfigurationError):
        FixtureSearchProvider(SearchSettings(), ())
    with pytest.raises(ValidationError):
        SearchSettings(provider="fixture")
    for value in (
        {"parameters": {"api_key": SECRET}},
        {"parameters": {"lr": "fr"}},
        {"parameters": {"gl": "xx"}},
        {"parameters": {"as_qdr": "w7"}},
        {"parameters": {"as_qdr": "d366"}},
        {"page": 10},
        {"query": "中" * 1000},
    ):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate({"query": "query", **value})
    monkeypatch.setenv(
        "JOB_HUNTER_SEARCH_BASE_URL",
        "https://user:" + SECRET + "@example.org/search.json",
    )
    with pytest.raises(ConfigurationError) as error:
        load_search_settings()
    assert SECRET not in str(error.value)


def test_capability_requires_session_and_exposes_no_credentials(
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JOB_HUNTER_SEARCH_PROVIDER", "serpapi")
    monkeypatch.setenv("JOB_HUNTER_SEARCH_API_KEY", SECRET)
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/searches/provider").status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.get("/api/v1/searches/provider")
        assert response.status_code == 200 and response.json()["status"] == "configured"
        assert SECRET not in response.text and "base_url" not in response.text
