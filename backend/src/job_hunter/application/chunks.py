from typing import Protocol
from uuid import UUID

from job_hunter.domain.chunks import ChunkPolicy, ChunkSet, chunk_text
from job_hunter.domain.cleaning import CleanText


class Chunks(Protocol):
    def source(self, content_id: UUID) -> CleanText: ...
    def get(self, content_id: UUID, policy_version: str) -> ChunkSet | None: ...
    def publish(self, result: ChunkSet) -> ChunkSet: ...


class ChunkJob:
    def __init__(self, repository: Chunks, policy: ChunkPolicy | None = None) -> None:
        self.repository, self.policy = repository, policy or ChunkPolicy()

    def run(self, content_id: UUID) -> ChunkSet:
        existing = self.repository.get(content_id, self.policy.version)
        if existing is not None:
            return existing
        content = self.repository.source(content_id)
        return self.repository.publish(chunk_text(content_id, content, self.policy))
