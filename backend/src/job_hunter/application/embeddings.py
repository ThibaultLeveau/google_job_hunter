import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID

from job_hunter.application.ai import AI, Accounting, AIError, Call, Record
from job_hunter.domain.chunks import Chunk, ChunkSet
from job_hunter.domain.embeddings import (
    EmbeddingProgress,
    EmbeddingSpace,
    Evidence,
    Retrieval,
    vector,
)


class EmbeddingStore(Protocol):
    def load(self, content_id: UUID, policy_version: str) -> ChunkSet: ...
    def pending(self, batch: ChunkSet, space: EmbeddingSpace) -> tuple[Chunk, ...]: ...
    def publish(
        self,
        space: EmbeddingSpace,
        chunks: tuple[Chunk, ...],
        vectors: tuple[tuple[float, ...], ...],
        records: list[dict[str, object]],
    ) -> None: ...
    def visible(
        self, owner: UUID, job: UUID, content: UUID, policy: str
    ) -> ChunkSet: ...
    def nearest(
        self,
        owner: UUID,
        job: UUID,
        batch: ChunkSet,
        space: EmbeddingSpace,
        query: tuple[float, ...],
        limit: int,
    ) -> tuple[Evidence, ...]: ...


class EmbeddingTrace(Accounting):
    def __init__(self) -> None:
        self.reserved = False
        self.records: list[Record] = []

    def reserve(self, call: Call) -> None:
        if self.reserved:
            raise ValueError("Embedding batch call budget exhausted.")
        self.reserved = True

    def record(self, record: Record) -> None:
        self.records.append(record)

    def payload(self) -> list[dict[str, object]]:
        return list(
            json.loads(json.dumps([asdict(row) for row in self.records], default=str))
        )


@dataclass(frozen=True)
class EmbeddedBatch:
    chunks: tuple[Chunk, ...]
    vectors: tuple[tuple[float, ...], ...]
    accounting: list[dict[str, object]]
    error: AIError | None = None


class EmbedJobs:
    def __init__(
        self,
        store: EmbeddingStore,
        space: EmbeddingSpace,
        factory: Callable[[Accounting], AI],
    ) -> None:
        self.store, self.space, self.factory = store, space, factory

    def ai(self, trace: EmbeddingTrace) -> AI:
        ai = self.factory(trace)
        if (
            ai.embedding_model != self.space.model
            or ai.limits.dimensions != self.space.dimensions
        ):
            raise ValueError("Configured embedding space does not match the provider.")
        return ai

    def run(
        self, content_id: UUID, policy_version: str, max_batches: int = 8
    ) -> EmbeddingProgress:
        if not 1 <= max_batches <= 128:
            raise ValueError("Use between one and 128 batches per execution.")
        batch, written = self.store.load(content_id, policy_version), 0
        for _ in range(max_batches):
            pending = self.store.pending(batch, self.space)
            if not pending:
                break
            result = self.execute(pending)
            self.store.publish(
                self.space, result.chunks, result.vectors, result.accounting
            )
            if result.error is not None:
                raise result.error
            written += len(result.chunks)
        return EmbeddingProgress(written, len(self.store.pending(batch, self.space)))

    def execute(self, pending: tuple[Chunk, ...]) -> EmbeddedBatch:
        trace = EmbeddingTrace()
        ai, selected, bound = self.ai(trace), [], 0
        for chunk in pending[:16]:
            size = len(chunk.text.encode())
            if bound + size > ai.limits.input_tokens:
                break
            selected.append(chunk)
            bound += size
        if not selected:
            raise AIError("input_limit")
        chunks = tuple(selected)
        try:
            vectors = tuple(
                vector(value) for value in ai.embed(tuple(c.text for c in chunks)).value
            )
        except AIError as error:
            return EmbeddedBatch(chunks, (), trace.payload(), error)
        except ValueError:
            return EmbeddedBatch(chunks, (), trace.payload(), AIError("invalid_output"))
        return EmbeddedBatch(chunks, vectors, trace.payload())

    def retrieve(
        self,
        owner: UUID,
        job: UUID,
        content: UUID,
        policy: str,
        query: str,
        limit: int = 5,
    ) -> Retrieval:
        if not 1 <= limit <= 20:
            raise ValueError("Use a top-k limit between one and 20.")
        batch = self.store.visible(owner, job, content, policy)
        if self.store.pending(batch, self.space):
            return Retrieval((), False)
        if not batch.chunks:
            return Retrieval((), True)
        trace = EmbeddingTrace()
        response = self.ai(trace).embed((query,)).value[0]
        try:
            value = vector(response)
        except ValueError:
            raise AIError("invalid_output") from None
        evidence = self.store.nearest(owner, job, batch, self.space, value, limit)
        return Retrieval(evidence, True, tuple(trace.payload()))
