from dataclasses import dataclass, field
from typing import Literal, Protocol

FetchErrorCode = Literal[
    "unsafe_url",
    "unsafe_address",
    "dns",
    "timeout",
    "network",
    "tls",
    "http_status",
    "too_large",
    "encoding",
    "invalid_response",
    "redirect_limit",
    "redirect_loop",
    "busy",
]


class FetchError(Exception):
    def __init__(
        self,
        code: FetchErrorCode,
        transient: bool = False,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.code, self.transient = code, transient
        self.status, self.retry_after = status, retry_after
        super().__init__(code)


@dataclass(frozen=True)
class FetchRequest:
    url: str
    visited: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status: int
    content_type: str | None
    body: bytes = field(repr=False)
    wire_bytes: int
    redirect: FetchRequest | None = None


class PublicFetcher(Protocol):
    def fetch(self, request: FetchRequest) -> FetchResponse: ...
