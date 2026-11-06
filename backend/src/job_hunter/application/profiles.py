from typing import Literal, Protocol
from uuid import UUID

from job_hunter.domain.profiles import FactData, ProfileFact, ProfileValue
from job_hunter.domain.review import ReviewBatch, ReviewResult


class ProfileSnapshot(ProfileValue):
    id: UUID
    revision: int
    facts: tuple[ProfileFact, ...] = ()


class FactResult(ProfileValue):
    outcome: Literal["created", "existing", "updated", "deleted"]
    revision: int
    fact: ProfileFact | None


class Profiles(Protocol):
    def review(self, owner: UUID, batch: ReviewBatch) -> ReviewResult: ...

    def read(self, owner: UUID) -> ProfileSnapshot: ...
    def get(self, owner: UUID, fact_id: UUID) -> ProfileFact: ...
    def put(
        self,
        owner: UUID,
        expected_revision: int,
        data: FactData,
        fact_id: UUID | None = None,
    ) -> FactResult: ...
    def delete(
        self, owner: UUID, expected_revision: int, fact_id: UUID
    ) -> FactResult: ...
