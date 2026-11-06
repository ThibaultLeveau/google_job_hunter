import asyncio
import json
import logging
import re
from contextvars import ContextVar
from typing import Literal, Self
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from job_hunter.application.search_provider import (
    SearchCapability,
    SearchError,
    SearchErrorCode,
    SearchPage,
    SearchProvider,
    SearchRequest,
    SearchResult,
    SearchUsage,
)
from job_hunter.settings import ConfigurationError

PRIVATE_HTTP = ContextVar("private_search_http", default=False)
LOGGER = logging.getLogger(__name__)
CREDENTIAL_FIELDS = {
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "password",
    "secret",
    "authorization",
    "signature",
}


class PrivateHttpLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not PRIVATE_HTTP.get()


# HTTPX logs request URLs; httpcore DEBUG diagnostics can include response headers.
for name in (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
):
    logging.getLogger(name).addFilter(PrivateHttpLogs())


class SearchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_SEARCH_", frozen=True, hide_input_in_errors=True
    )
    provider: Literal["disabled", "serpapi", "fixture"] = "disabled"
    environment: Literal["production", "development", "test"] = "production"
    api_key: SecretStr | None = Field(default=None, max_length=256)
    base_url: str = Field(default="https://serpapi.com/search.json", max_length=2048)
    timeout_seconds: float = Field(default=10, ge=0.01, le=30)
    max_pages: int = Field(default=5, ge=1, le=10)
    max_results: int = Field(default=50, ge=1, le=100)
    max_response_bytes: int = Field(default=1048576, ge=1024, le=2097152)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path != "/search.json"
            or any(c.isspace() or c in "\\*" for c in value)
        ):
            raise ValueError("Use a trusted HTTPS search.json endpoint.")
        _ = url.port
        return value

    @model_validator(mode="after")
    def fixture_mode(self) -> Self:
        if self.provider == "fixture" and self.environment == "production":
            raise ValueError("Fixtures require development/test mode.")
        return self


def load_search_settings() -> SearchSettings:
    try:
        return SearchSettings()
    except ValidationError, SettingsError:
        raise ConfigurationError(
            "Invalid search settings. Check JOB_HUNTER_SEARCH_ variables."
        ) from None


def source_url(value: str, secret: str) -> str:
    url = urlsplit(value)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or "\\" in value
        or "%" in url.hostname
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or (secret and secret in unquote(value))
        or set(key.lower() for key in parse_qs(url.query, keep_blank_values=True))
        & CREDENTIAL_FIELDS
        or set(key.lower() for key in parse_qs(url.fragment, keep_blank_values=True))
        & CREDENTIAL_FIELDS
    ):
        raise ValueError("Use a public source URL without credentials.")
    _ = url.port
    value.encode("utf-8")
    return value


class SerpApiSearchProvider:
    def __init__(
        self,
        settings: SearchSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings, self.transport = settings, transport

    @property
    def capability(self) -> SearchCapability:
        settings = self.settings
        status: Literal["disabled", "unconfigured", "configured", "fixture"] = (
            settings.provider
            if settings.provider != "serpapi"
            else "configured"
            if settings.api_key and settings.api_key.get_secret_value().strip()
            else "unconfigured"
        )
        return SearchCapability(
            provider=settings.provider,
            status=status,
            max_pages=settings.max_pages,
            max_results=min(settings.max_results, settings.max_pages * 10),
        )

    async def response(self, request: SearchRequest) -> tuple[int, bytes]:
        config = self.settings
        assert config.api_key is not None
        async with (
            asyncio.timeout(config.timeout_seconds),
            httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=config.timeout_seconds,
            ) as client,
        ):
            parameters = {
                **request.parameters,
                "engine": "google",
                "q": request.query,
                "start": str(request.page * 10),
                "output": "json",
                "api_key": config.api_key.get_secret_value(),
            }
            async with client.stream(
                "GET",
                config.base_url,
                params=parameters,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            ) as response:
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise SearchError("invalid_response")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > config.max_response_bytes:
                        raise SearchError("invalid_response")
                    data.extend(chunk)
                return response.status_code, bytes(data)

    def fetch(self, request: SearchRequest) -> SearchPage:
        request = SearchRequest.model_validate(request.model_dump())
        capability = self.capability
        if capability.status in ("disabled", "unconfigured"):
            raise SearchError(capability.status)
        if (
            request.page >= capability.max_pages
            or request.page * 10 >= capability.max_results
        ):
            raise SearchError("input_limit")
        token = PRIVATE_HTTP.set(True)
        usage = SearchUsage(requests=int(capability.provider == "serpapi"))
        try:
            status, raw = asyncio.run(self.response(request))
            if len(raw) > self.settings.max_response_bytes:
                raise SearchError("invalid_response")
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            metadata = body.get("search_metadata", {}) if isinstance(body, dict) else {}
            secret = (
                self.settings.api_key.get_secret_value()
                if self.settings.api_key
                else ""
            )
            request_id = metadata.get("id") if isinstance(metadata, dict) else None
            if (
                isinstance(request_id, str)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id)
                and not (secret and secret in request_id)
            ):
                usage = usage.model_copy(update={"request_id": request_id})
            if status in (401, 403):
                raise SearchError("authentication")
            if status == 429:
                error = str(body.get("error", "")) if isinstance(body, dict) else ""
                raise SearchError(
                    "quota"
                    if "run out of searches" in error.lower()
                    or "quota" in error.lower()
                    else "rate_limited"
                )
            if status != 200:
                raise SearchError("provider_failure")
            if (
                not isinstance(body, dict)
                or not isinstance(metadata, dict)
                or metadata.get("status") != "Success"
            ):
                raise SearchError("invalid_response")
            rows = body.get("organic_results", [])
            if not isinstance(rows, list):
                raise SearchError("invalid_response")
            bound = min(10, capability.max_results - request.page * 10)
            results: list[SearchResult] = []
            for row in rows[:bound]:
                try:
                    if (
                        not isinstance(row, dict)
                        or type(row.get("position")) is not int
                        or not 1 <= row["position"] <= 10
                    ):
                        raise ValueError("Invalid organic entry.")
                    result = SearchResult.model_validate(
                        {
                            "title": row.get("title"),
                            "snippet": row.get("snippet"),
                            "source_url": row.get("link"),
                            "rank": request.page * 10 + row["position"],
                        }
                    )
                    source_url(result.source_url, secret)
                    text = result.title + (result.snippet or "")
                    text.encode("utf-8")
                    if not result.title.strip() or "\x00" in text:
                        raise ValueError("Invalid result text.")
                    if secret and secret in (result.title + (result.snippet or "")):
                        raise ValueError("Credential reflected in result.")
                    results.append(result)
                except ValueError:
                    continue
            pagination = body.get("serpapi_pagination", {})
            if not isinstance(pagination, dict):
                raise SearchError("invalid_response")
            next_link = pagination.get("next", pagination.get("next_link"))
            next_page = None
            if next_link is not None:
                if not isinstance(next_link, str) or len(next_link) > 8192:
                    raise SearchError("invalid_response")
                url, base = urlsplit(next_link), urlsplit(self.settings.base_url)
                if (url.scheme, url.netloc, url.path) != (
                    base.scheme,
                    base.netloc,
                    base.path,
                ) or parse_qs(url.query).get("start") != [str((request.page + 1) * 10)]:
                    raise SearchError("invalid_response")
                if (
                    request.page + 1 < capability.max_pages
                    and (request.page + 1) * 10 < capability.max_results
                ):
                    next_page = request.page + 1
            usage = usage.model_copy(
                update={
                    "received": min(len(rows), bound),
                    "returned": len(results),
                    "skipped": min(len(rows), bound) - len(results),
                }
            )
            return SearchPage(
                results=tuple(results),
                next_page=next_page,
                truncated=len(rows) > bound
                or (next_link is not None and next_page is None),
                usage=usage,
            )
        except httpx.TimeoutException, TimeoutError:
            usage = usage.model_copy(update={"error": "timeout"})
            raise SearchError("timeout", usage) from None
        except ValueError, TypeError:
            usage = usage.model_copy(update={"error": "invalid_response"})
            raise SearchError("invalid_response", usage) from None
        except Exception as error:
            code: SearchErrorCode = (
                error.code if isinstance(error, SearchError) else "provider_failure"
            )
            usage = usage.model_copy(update={"error": code})
            raise SearchError(code, usage) from None
        finally:
            PRIVATE_HTTP.reset(token)
            LOGGER.info(
                "Search provider call", extra={"search_usage": usage.model_dump()}
            )


class FixtureSearchProvider(SerpApiSearchProvider):
    def __init__(self, settings: SearchSettings, pages: tuple[bytes, ...]) -> None:
        if settings.provider != "fixture" or settings.environment == "production":
            raise ConfigurationError("Fixture search requires development/test mode.")
        super().__init__(settings)
        self.pages = pages

    async def response(self, request: SearchRequest) -> tuple[int, bytes]:
        if request.page >= len(self.pages):
            return 200, b'{"search_metadata":{"status":"Success"}}'
        return 200, self.pages[request.page]


def create_search_provider(
    settings: SearchSettings, pages: tuple[bytes, ...] = ()
) -> SearchProvider:
    if settings.provider == "fixture":
        return FixtureSearchProvider(settings, pages)
    return SerpApiSearchProvider(settings)
