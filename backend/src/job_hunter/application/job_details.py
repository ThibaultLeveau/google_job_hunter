from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from job_hunter.application.job_listing import Value
from job_hunter.domain.cleaning import CleanText
from job_hunter.domain.job_metadata import Citation, Metadata
from job_hunter.domain.matching import Match
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.requirements import Requirements

StageName = Literal[
    "fetch", "text", "clean", "chunks", "embed", "metadata", "requirements"
]


class StageStatus(Value):
    stage: StageName
    revision: str
    generation: int | None
    state: Literal[
        "pending", "running", "completed", "degraded", "unsupported", "failed"
    ]
    reason: str | None
    created_at: datetime | None
    updated_at: datetime | None
    retryable: bool


class JobStatus(Value):
    id: UUID
    snapshot_id: UUID | None
    freshness: Literal["current", "pending", "stale", "removed"]
    content_ready: bool
    analysis_ready: bool
    embeddings_ready: bool
    stages: list[StageStatus]
    match_state: Literal["current", "stale", "pending", "failed"]
    match_current: bool
    match_stale: bool
    match_pending: bool
    match_reason: str | None
    retries_remaining: int
    reevaluations_remaining: int


class EvaluatedProfileFact(Value):
    id: UUID
    kind: Literal["skill", "experience", "project"]
    skill: str | None = None
    title: str | None = None
    description: str | None = None


class MatchContext(Value):
    profile_changed: bool
    profile_facts: list[EvaluatedProfileFact]
    requirements: Requirements
    source: Preparation
    citations: dict[UUID, Citation]


class JobDetail(Value):
    status: JobStatus
    source_url: str
    fetched_at: datetime | None
    preparation_id: UUID | None
    content_id: UUID | None
    metadata_id: UUID | None
    requirements_id: UUID | None
    source: Preparation | None
    clean_text: CleanText | None
    metadata: Metadata | None
    requirements: Requirements | None
    match: Match | None
    match_context: MatchContext | None = None


class RetryWrite(Value):
    stage: StageName
    revision: str = Field(min_length=1, max_length=160)
    expected_generation: int = Field(ge=0, strict=True)
    idempotency_key: UUID


class ReevaluationWrite(Value):
    idempotency_key: UUID


class WorkAccepted(Value):
    generation: int
    status: Literal["accepted"] = "accepted"


class JobDetails(Protocol):
    def detail(self, owner: UUID, job: UUID) -> JobDetail: ...
    def retry(
        self, owner: UUID, job: UUID, request: RetryWrite
    ) -> WorkAccepted | None: ...
    def reevaluate(self, owner: UUID, job: UUID, key: UUID) -> WorkAccepted | None: ...
