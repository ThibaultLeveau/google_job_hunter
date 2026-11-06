import unicodedata
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from job_hunter.domain.searches import SearchDefinition


class SearchValue(BaseModel):
    model_config = SearchDefinition.model_config


class SearchWrite(SearchValue):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    definition: SearchDefinition

    @field_validator("name")
    @classmethod
    def readable_name(cls, value: str) -> str:
        if not value.strip() or any(unicodedata.category(c)[0] == "C" for c in value):
            raise ValueError("Use a nonempty name without control characters.")
        return value.strip()


class SavedSearch(SearchWrite):
    id: UUID
    revision: int
    created_at: datetime
    updated_at: datetime


class RunSnapshot(SearchWrite):
    compiler_version: Literal[1] = 1
    query: str
    provider_parameters: dict[str, str]


class RunCounters(SearchValue):
    found_results: int = Field(default=0, ge=0)
    new_urls: int = Field(default=0, ge=0)
    known_urls: int = Field(default=0, ge=0)
    skipped_results: int = Field(default=0, ge=0)
    processing_total: int = Field(default=0, ge=0)
    processing_ready: int = Field(default=0, ge=0)


RunStatus = Literal["pending", "running", "completed", "partial", "failed", "cancelled"]


class SearchRun(SearchValue):
    id: UUID
    search_id: UUID
    search_revision: int
    idempotency_key: UUID
    snapshot: RunSnapshot
    status: RunStatus
    counters: RunCounters
    created_at: datetime
    updated_at: datetime


class SearchPage(SearchValue):
    items: tuple[SavedSearch, ...]
    next_offset: int | None


class RunPage(SearchValue):
    items: tuple[SearchRun, ...]
    next_offset: int | None


class Searches(Protocol):
    def list(self, owner: UUID, limit: int, offset: int) -> SearchPage: ...
    def get(self, owner: UUID, search_id: UUID) -> SavedSearch: ...
    def save(
        self,
        owner: UUID,
        data: SearchWrite,
        search_id: UUID | None = None,
        expected_revision: int | None = None,
    ) -> SavedSearch: ...
    def delete(self, owner: UUID, search_id: UUID, expected_revision: int) -> None: ...
    def start(
        self,
        owner: UUID,
        search_id: UUID,
        expected_revision: int,
        key: UUID,
    ) -> SearchRun: ...
    def runs(self, owner: UUID, limit: int, offset: int) -> RunPage: ...
    def run(self, owner: UUID, run_id: UUID) -> SearchRun: ...
    def cancel(self, owner: UUID, run_id: UUID) -> SearchRun: ...
