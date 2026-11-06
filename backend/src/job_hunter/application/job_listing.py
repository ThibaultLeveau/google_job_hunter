import base64
import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunter.domain.job_metadata import Amount, Arrangement, Status


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class JobQuery(Value):
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, max_length=2048)
    sort: Literal["newest", "oldest", "title", "match"] = "newest"
    run_id: UUID | None = None
    keyword: str | None = Field(default=None, min_length=1, max_length=80)
    region: str | None = Field(default=None, min_length=1, max_length=80)
    remote: Literal["remote", "hybrid", "on_site"] | None = None
    match_status: Literal["current", "stale", "pending", "failed", "unknown"] | None = (
        None
    )
    salary_min: Amount | None = None
    salary_max: Amount | None = None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None
    period: Literal["hour", "day", "week", "month", "year"] | None = None
    include_unknown_region: bool = False
    include_unknown_remote: bool = False
    include_unknown_salary: bool = False

    @model_validator(mode="after")
    def valid(self) -> Self:
        salary = any(
            value is not None
            for value in (self.salary_min, self.salary_max, self.currency, self.period)
        )
        if (salary and (self.currency is None or self.period is None)) or (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("Salary bounds require matching currency and period.")
        if any(
            value is not None and (not value.strip() or "\x00" in value)
            for value in (self.keyword, self.region)
        ):
            raise ValueError("Use a nonempty factual filter.")
        return self

    def binding(self, owner: UUID) -> str:
        return hashlib.sha256(
            (str(owner) + self.model_dump_json(exclude={"cursor"})).encode()
        ).hexdigest()


class Cursor(Value):
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")
    id: UUID
    value: str | float | None

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode()).decode()

    @classmethod
    def read(cls, query: JobQuery, owner: UUID) -> Self | None:
        if query.cursor is None:
            return None
        try:
            result = cls.model_validate_json(
                base64.b64decode(query.cursor, altchars=b"-_", validate=True)
            )
            if result.binding != query.binding(owner):
                raise ValueError()
            if query.sort in {"newest", "oldest"}:
                if (
                    not isinstance(result.value, str)
                    or datetime.fromisoformat(result.value).utcoffset() is None
                ):
                    raise ValueError()
            elif query.sort == "title":
                if result.value is not None and (
                    not isinstance(result.value, str) or len(result.value) > 300
                ):
                    raise ValueError()
            elif result.value is not None and (
                type(result.value) is not float or not 0 <= result.value <= 100
            ):
                raise ValueError()
            return result
        except ValueError, TypeError:
            raise ValueError("Invalid pagination cursor.") from None


class JobFact(Value):
    status: Status = "unknown"
    values: list[str] = Field(default_factory=list, max_length=100)


class SalaryValue(Value):
    minimum: Decimal | None
    maximum: Decimal | None
    currency: str | None
    period: str | None
    basis: str | None
    uncertain: bool


class SalarySummary(Value):
    status: Status = "unknown"
    values: list[SalaryValue] = Field(default_factory=list, max_length=8)


class MatchSummary(Value):
    status: Literal["current", "stale", "pending", "failed"]
    pending: bool
    stale: bool
    score: float | None
    assessed_percent: float | None
    evaluated_at: datetime | None


class JobSummary(Value):
    id: UUID
    source_url: str
    discovered_at: datetime
    fetched_at: datetime | None
    observed_at: datetime | None
    fetch_status: str
    analysis_status: Literal[
        "pending", "ready", "unknown", "non_job", "failed", "unavailable"
    ]
    title: JobFact
    company: JobFact
    location: JobFact
    region: JobFact
    remote: Arrangement
    salary: SalarySummary
    match: MatchSummary


class JobPage(Value):
    items: list[JobSummary]
    next_cursor: str | None


class JobListing(Protocol):
    def list(self, owner: UUID, query: JobQuery) -> JobPage: ...
