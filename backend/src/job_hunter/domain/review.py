from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from job_hunter.domain.extraction import Candidate
from job_hunter.domain.profiles import FactData, ProfileValue, normalized

Identifier = Annotated[UUID, Field(strict=False)]
FACT = TypeAdapter(FactData)


class ReviewItem(ProfileValue):
    proposal_id: Identifier
    action: Literal["accept", "reject", "edit"]
    fact: FactData | None = None

    @model_validator(mode="after")
    def edit_only(self) -> Self:
        if (self.action == "edit") != (self.fact is not None):
            raise ValueError("Only edit decisions require a corrected fact.")
        return self


class ReviewBatch(ProfileValue):
    expected_revision: int = Field(ge=0)
    idempotency_key: Identifier
    items: list[ReviewItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({item.proposal_id for item in self.items}) != len(self.items):
            raise ValueError("Select each proposal once.")
        return self


class ReviewOutcome(ProfileValue):
    proposal_id: UUID
    outcome: Literal["created", "linked", "rejected", "conflict"]
    fact_id: UUID | None = None


class ReviewResult(ProfileValue):
    revision: int
    items: list[ReviewOutcome]


def supported(field: str, value: object, quote: str) -> bool:
    if field == "url":
        return isinstance(value, str) and value.strip() == quote.strip()
    return value is not None and normalized(str(value)) == normalized(quote)


def reviewed(candidate: Candidate, item: ReviewItem) -> tuple[FactData, list[str]]:
    original: dict[str, object] = {
        field.name: field.value for field in candidate.fields
    }
    fact = item.fact or FACT.validate_python({"kind": candidate.kind, **original})
    if fact.kind != candidate.kind:
        raise ValueError("Review cannot change the kind of a proposal.")
    corrections = [
        name
        for name, value in fact.model_dump(mode="json").items()
        if name != "kind" and original.get(name) != value
    ]
    return fact, corrections
