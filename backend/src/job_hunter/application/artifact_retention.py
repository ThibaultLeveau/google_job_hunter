import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from job_hunter.domain.tasks import require_utc


@dataclass(frozen=True)
class ArtifactPolicy:
    days: int = 30
    scan_limit: int = 20
    row_limit: int = 10000

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.days, 3650),
            (self.scan_limit, 100),
            (self.row_limit, 10000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid artifact retention bounds.")


class ArtifactCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = 1
    cutoff: datetime
    days: int
    dry_run: bool
    after: UUID | None = None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def start(
        cls, now: datetime, policy: ArtifactPolicy, dry_run: bool, token: str | None
    ) -> ArtifactCheckpoint:
        require_utc(now)
        if type(dry_run) is not bool:
            raise ValueError("Invalid retention mode.")
        cutoff = now - timedelta(days=policy.days)
        try:
            if token is None:
                return cls(cutoff=cutoff, days=policy.days, dry_run=dry_run)
            if len(token) > 2048:
                raise ValueError()
            value = cls.model_validate_json(
                base64.b64decode(token, altchars=b"-_", validate=True)
            )
            require_utc(value.cutoff)
            if (
                value.version != 1
                or value.days != policy.days
                or value.dry_run != dry_run
                or value.cutoff > cutoff
            ):
                raise ValueError()
            return value
        except ValueError:
            raise ValueError("Invalid artifact retention checkpoint.") from None


@dataclass(frozen=True)
class ArtifactBatch:
    dry_run: bool
    counts: dict[str, int]
    scanned: int
    protected: int
    oversized: int
    contended: int
    checkpoint: str | None
