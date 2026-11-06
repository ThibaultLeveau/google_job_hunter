import ipaddress
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote_plus, urlsplit
from uuid import UUID

import idna

CANONICAL_VERSION = 1
TRACKERS = frozenset(
    (
        "utm_source utm_medium utm_campaign utm_term utm_content utm_id "
        "gclid dclid fbclid msclkid"
    ).split()
)
CREDENTIALS = frozenset(
    (
        "api_key apikey key token access_token password secret authorization signature"
    ).split()
)


def canonical_url(value: str) -> str:
    try:
        if (
            not 1 <= len(value.encode("utf-8")) <= 2048
            or any(c.isspace() or unicodedata.category(c)[0] == "C" for c in value)
            or any(c in value for c in '\\<>"{}|^`')
            or re.search(r"%(?![0-9a-fA-F]{2})", value)
        ):
            raise ValueError
        url = urlsplit(value)
        host = url.hostname
        if (
            url.scheme not in {"http", "https"}
            or not host
            or url.username is not None
            or url.password is not None
            or "%" in host
            or url.netloc.endswith(":")
        ):
            raise ValueError
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        else:
            host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
        port = url.port
        if port == 0:
            raise ValueError
        if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
            host += f":{port}"
        kept = []
        if any(
            unquote_plus(part.partition("=")[0]).lower() in CREDENTIALS
            for part in url.fragment.split("&")
        ):
            raise ValueError
        for part in url.query.split("&"):
            key = unquote_plus(part.partition("=")[0]).lower()
            if key in CREDENTIALS:
                raise ValueError
            if key not in TRACKERS:
                kept.append(part)
        result = f"{url.scheme}://{host}{url.path or '/'}"
        query = "&".join(kept)
        # Keep query order, repeated keys, encoding and an explicitly empty query.
        if query or (not url.query and "?" in value.split("#", 1)[0]):
            result += "?" + query
        if len(result.encode("utf-8")) > 2048:
            raise ValueError
        return result
    except ValueError, UnicodeError:
        raise ValueError(
            "Use a valid public HTTP(S) URL of at most 2,048 bytes."
        ) from None


@dataclass(frozen=True)
class JobSource:
    id: UUID
    canonical_url: str
    canonical_version: int
    created_at: datetime


@dataclass(frozen=True)
class Discovery:
    source: JobSource
    run_id: UUID
    original_url: str
    rank: int
    discovered_at: datetime


@dataclass(frozen=True)
class Registration:
    source_id: UUID
    new_source: bool
    new_association: bool
