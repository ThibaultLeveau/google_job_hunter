from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from job_hunter.application.searches import SearchValue
from job_hunter.domain.searches import SearchDefinition

PARAMETERS = ("lr", "gl", "location", "as_qdr")
SearchErrorCode = Literal[
    "disabled",
    "unconfigured",
    "input_limit",
    "invalid_response",
    "authentication",
    "quota",
    "rate_limited",
    "timeout",
    "provider_failure",
]


class SearchRequest(SearchValue):
    query: str = Field(min_length=1, max_length=2048, repr=False)
    parameters: dict[str, Annotated[str, Field(max_length=128)]] = Field(
        default_factory=dict, repr=False
    )
    page: int = Field(default=0, ge=0, le=9)

    @model_validator(mode="after")
    def supported(self) -> Self:
        if len(self.query.encode()) > 2048 or not self.query.strip():
            raise ValueError("Use a bounded compiled query.")
        if set(self.parameters) - set(PARAMETERS):
            raise ValueError("Unsupported provider parameter.")
        values: dict[str, object] = {"groups": [["validation"]]}
        for key, value in self.parameters.items():
            if key == "lr":
                if not value.startswith("lang_"):
                    raise ValueError("Use a supported result language.")
                values["language"] = value[5:]
            elif key == "as_qdr":
                if (
                    not value.startswith("d")
                    or not value[1:].isascii()
                    or not value[1:].isdigit()
                ):
                    raise ValueError("Use bounded recency in days.")
                values["recency_days"] = int(value[1:])
            else:
                values["country" if key == "gl" else "location"] = value
        SearchDefinition.model_validate(values)
        return self


class SearchResult(SearchValue):
    title: str = Field(min_length=1, max_length=500)
    snippet: str | None = Field(default=None, max_length=3000)
    source_url: str = Field(min_length=1, max_length=2048)
    rank: int = Field(ge=1, le=100)


class SearchUsage(SearchValue):
    request_id: str | None = None
    requests: int = 0
    received: int = 0
    returned: int = 0
    skipped: int = 0
    error: SearchErrorCode | None = None


class SearchPage(SearchValue):
    results: tuple[SearchResult, ...]
    next_page: int | None
    truncated: bool
    usage: SearchUsage


class SearchCapability(SearchValue):
    provider: Literal["disabled", "serpapi", "fixture"]
    status: Literal["disabled", "unconfigured", "configured", "fixture"]
    max_pages: int
    max_results: int
    page_size: Literal[10] = 10
    parameters: tuple[str, ...] = PARAMETERS


class SearchError(Exception):
    def __init__(self, code: SearchErrorCode, usage: SearchUsage | None = None) -> None:
        self.code, self.usage = code, usage
        super().__init__(code)


class SearchProvider(Protocol):
    @property
    def capability(self) -> SearchCapability: ...
    def fetch(self, request: SearchRequest) -> SearchPage: ...
