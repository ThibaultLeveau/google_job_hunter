import re
from dataclasses import dataclass
from typing import Literal

from job_hunter.domain.snapshots import FetchedSnapshot

CrawlState = Literal[
    "fetched", "blocked", "removed", "unsupported", "non_job", "failed"
]
CRAWL = "job_crawl"
FAILED_STATES = ("blocked", "removed", "unsupported", "non_job", "failed")


@dataclass(frozen=True)
class CrawlResult:
    state: CrawlState
    error: str | None = None
    snapshot: FetchedSnapshot | None = None


def classify(snapshot: FetchedSnapshot) -> CrawlResult:
    if snapshot.status in {401, 403}:
        return CrawlResult("blocked", "http_denied", snapshot)
    if snapshot.status in {404, 410}:
        return CrawlResult("removed", "http_removed", snapshot)
    if not 200 <= snapshot.status < 300:
        return CrawlResult("failed", "http_status", snapshot)
    mime = (snapshot.content_type or "").split(";", 1)[0].strip().lower()
    if mime not in {"text/html", "application/xhtml+xml", "text/plain"}:
        return CrawlResult("unsupported", "content_type", snapshot)
    if not snapshot.body.strip():
        return CrawlResult("non_job", "empty_page", snapshot)
    if mime != "text/plain":
        text = snapshot.body[:262144].decode("utf-8", errors="replace").lower()
        if re.search(r"<input\b[^>]{0,2048}\btype\s*=\s*['\"]?password\b", text) or any(
            marker in text for marker in ("g-recaptcha", "h-captcha", "cf-chl-")
        ):
            return CrawlResult("blocked", "interactive_challenge", snapshot)
        if (
            re.search(
                r"<noscript\b[^>]{0,2048}>(?=[^<]{0,2048}javascript)"
                r"[^<]{0,2048}(?:enable|requires?)",
                text,
            )
            and re.search(r"<div\s+id=['\"](?:root|app|__next)['\"]\s*>\s*</div>", text)
            and not re.search(r"<(?:main|article|section|h1|p|li)\b", text)
        ):
            return CrawlResult("unsupported", "javascript_required", snapshot)
    return CrawlResult("fetched", snapshot=snapshot)
