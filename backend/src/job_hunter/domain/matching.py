from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field

from job_hunter.domain.job_metadata import Value
from job_hunter.domain.requirements import Requirement

EvidenceID = Annotated[UUID, Field(strict=False)]
Status = Literal["matched", "partial", "missing", "unknown"]


def evidence_ids(requirement: Requirement) -> list[UUID]:
    return [
        uuid5(requirement.id, span.model_dump_json()) for span in requirement.evidence
    ]


class Evaluation(Value):
    requirement_id: EvidenceID
    status: Status
    requirement_evidence: list[EvidenceID] = Field(min_length=1, max_length=4)
    profile_evidence: list[EvidenceID] = Field(max_length=128)
    rationale: str = Field(min_length=1, max_length=600, repr=False)


class ScoreComponent(Value):
    requirement_id: UUID
    status: Status
    weight: int
    earned_units: int
    possible_units: int


class Score(Value):
    percent: float | None
    components: list[ScoreComponent]
    unknown_count: int
    requirement_count: int
    unknown_weight: int
    total_weight: int
    assessed_percent: float | None


class Match(Value):
    owner: UUID
    profile_revision: int
    job_id: UUID
    snapshot_id: UUID
    source_hash: str
    requirements_id: UUID
    requirements_version: str
    content_id: UUID
    chunk_version: str
    embedding_space: UUID
    evaluator_version: str
    rubric_version: str
    prompt_version: str
    model: str
    state: Literal["ready", "unknown", "failed", "unavailable"]
    reason: str | None
    retrieval_complete: bool
    evaluations: list[Evaluation]
    score: Score
