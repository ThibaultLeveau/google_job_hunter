from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from job_hunter.domain.jobs import canonical_url
from job_hunter.domain.tasks import require_utc

SNAPSHOT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FetchedSnapshot:
    requested_url: str
    final_url: str
    status: int
    content_type: str | None
    body: bytes = field(repr=False)
    fetched_at: datetime
    redirects: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        canonical_url(self.requested_url)
        canonical_url(self.final_url)
        require_utc(self.fetched_at)
        if len(self.redirects) > 10:
            raise ValueError("Use at most ten redirect hops.")
        for url in self.redirects:
            canonical_url(url)
        if (
            type(self.status) is not int
            or not 200 <= self.status <= 599
            or len(self.body) > SNAPSHOT_BYTES
            or self.content_type is not None
            and (
                len(self.content_type) > 256
                or any(ord(c) < 32 or ord(c) >= 127 for c in self.content_type)
            )
        ):
            raise ValueError("Use bounded public HTTP snapshot metadata and bytes.")

    @property
    def content_hash(self) -> str:
        return sha256(self.body).hexdigest()
