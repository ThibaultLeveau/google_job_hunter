from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from job_hunter.application.job_details import EvaluatedProfileFact
from job_hunter.application.profiles import ProfileSnapshot
from job_hunter.application.searches import (
    RunCounters,
    RunStatus,
    SavedSearch,
    SearchWrite,
)
from job_hunter.domain.documents import Document
from job_hunter.domain.matching import Match
from job_hunter.domain.profiles import ProfileValue


class ExportSearch(SavedSearch):
    deleted_at: datetime | None


class ExportRun(ProfileValue):
    id: UUID
    search_id: UUID
    search_revision: int
    definition: SearchWrite
    query: str
    status: RunStatus
    counters: RunCounters
    created_at: datetime
    updated_at: datetime


class ExportDiscovery(ProfileValue):
    run_id: UUID
    job_id: UUID
    original_url: str
    rank: int
    discovered_at: datetime


class ExportMatch(ProfileValue):
    id: UUID
    generation: int
    created_at: datetime
    result: Match
    profile_facts: list[EvaluatedProfileFact]


class PersonalExport(ProfileValue):
    format: Literal["google-job-hunter"] = "google-job-hunter"
    version: Literal[1] = 1
    exported_at: datetime
    owner_id: UUID
    profile: ProfileSnapshot
    documents: list[Document]
    saved_searches: list[ExportSearch]
    runs: list[ExportRun]
    discoveries: list[ExportDiscovery]
    matches: list[ExportMatch]


@dataclass(frozen=True)
class ExportLimits:
    records: int = 2000
    bytes: int = 8 * 1024 * 1024


class ExportTooLarge(Exception):
    pass


class ExportBudget:
    def __init__(self, limits: ExportLimits) -> None:
        self.limits, self.records, self.bytes = limits, 0, 0

    def count(self, amount: int = 1) -> None:
        self.records += amount
        if self.records > self.limits.records:
            raise ExportTooLarge()

    def keep[T: BaseModel](self, value: T) -> T:
        self.bytes += len(value.model_dump_json().encode("utf-8"))
        if self.bytes > self.limits.bytes:
            raise ExportTooLarge()
        return value

    def finish(self, value: PersonalExport) -> bytes:
        content = value.model_dump_json().encode("utf-8")
        if len(content) > self.limits.bytes:
            raise ExportTooLarge()
        return content


class PersonalExports(Protocol):
    def read(self, owner: UUID, session_digest: str) -> bytes: ...
