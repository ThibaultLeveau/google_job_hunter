from contextlib import nullcontext
from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import CheckConstraint, Float, ForeignKey, Select, String, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.embeddings import EmbedJobs
from job_hunter.domain.chunks import Chunk, ChunkSet
from job_hunter.domain.embeddings import DIMENSIONS, EmbeddingSpace, Evidence, vector
from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.ai import AISettings, create_ai
from job_hunter.infrastructure.chunks import ChunkRecord, read
from job_hunter.infrastructure.cleaning import CleaningRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.jobs import JobSourceRecord, RunJobRecord
from job_hunter.infrastructure.preparation import PreparationRecord
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.snapshots import SnapshotRecord


class EmbeddingSpaceRecord(Base):
    __tablename__ = "job_embedding_spaces"
    __table_args__ = (
        CheckConstraint("dimensions = 1536", name="ck_embedding_dimensions"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(100))
    revision: Mapped[str] = mapped_column(String(80))
    dimensions: Mapped[int]


class EmbeddingRecord(Base):
    __tablename__ = "job_embeddings"
    __table_args__ = (
        CheckConstraint("vector_norm(embedding) > 0", name="ck_embedding_nonzero"),
    )
    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_chunks.id"), primary_key=True
    )
    space_id: Mapped[UUID] = mapped_column(
        ForeignKey("job_embedding_spaces.id"), primary_key=True
    )
    embedding: Mapped[list[float]] = mapped_column(VECTOR(DIMENSIONS))


class EmbeddingCallRecord(Base):
    __tablename__ = "job_embedding_calls"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    space_id: Mapped[UUID] = mapped_column(ForeignKey("job_embedding_spaces.id"))
    chunk_ids: Mapped[list[str]] = mapped_column(JSONB)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


def create_embeddings(
    database: Database, settings: AISettings, revision: str = "embedding-v1"
) -> EmbedJobs:
    if settings.limits.dimensions != DIMENSIONS:
        raise ValueError("Job embeddings require 1536 dimensions.")
    return EmbedJobs(
        PostgresEmbeddings(database),
        EmbeddingSpace(
            provider=settings.provider,
            model=settings.embedding_model or "fixture-embedding-v1",
            revision=revision,
        ),
        lambda accounting: create_ai(settings, accounting, fixture_response="{}"),
    )


def eligible(owner: UUID, job: UUID, content: UUID) -> Select[UUID]:
    owned = (
        select(RunJobRecord.source_id)
        .join(SearchRunRecord)
        .where(RunJobRecord.source_id == job, SearchRunRecord.owner_id == owner)
        .exists()
    )
    return (
        select(CleaningRecord.id)
        .join(PreparationRecord)
        .join(SnapshotRecord)
        .join(JobSourceRecord, JobSourceRecord.id == SnapshotRecord.source_id)
        .where(
            CleaningRecord.id == content,
            JobSourceRecord.id == job,
            JobSourceRecord.current_snapshot_id == SnapshotRecord.id,
            owned,
        )
    )


class PostgresEmbeddings:
    def __init__(self, database: Database) -> None:
        self.database = database

    def load(self, content_id: UUID, policy_version: str) -> ChunkSet:
        with self.database.transaction() as session:
            batch = read(session, content_id, policy_version)
            if batch is None:
                raise DomainError("not_found")
            return batch

    def pending(self, batch: ChunkSet, space: EmbeddingSpace) -> tuple[Chunk, ...]:
        with self.database.transaction() as session:
            completed = set(
                session.scalars(
                    select(EmbeddingRecord.chunk_id)
                    .join(ChunkRecord)
                    .where(
                        ChunkRecord.content_id == batch.content_id,
                        ChunkRecord.policy_version == batch.policy.version,
                        EmbeddingRecord.space_id == space.id,
                    )
                )
            )
        return tuple(chunk for chunk in batch.chunks if chunk.id not in completed)

    def publish(
        self,
        space: EmbeddingSpace,
        chunks: tuple[Chunk, ...],
        vectors: tuple[tuple[float, ...], ...],
        records: list[dict[str, object]],
        transaction: Session | None = None,
    ) -> None:
        if (
            not 1 <= len(chunks) <= 16
            or len(records) > 1
            or len(vectors) not in (0, len(chunks))
        ):
            raise ValueError("Invalid embedding batch.")
        values = tuple(vector(value) for value in vectors)
        if not values and not records:
            return
        with (
            self.database.transaction()
            if transaction is None
            else nullcontext(transaction)
        ) as session:
            session.execute(
                insert(EmbeddingSpaceRecord)
                .values(id=space.id, **space.model_dump())
                .on_conflict_do_nothing()
            )
            for chunk in chunks:
                row = session.get(ChunkRecord, chunk.id, with_for_update={"read": True})
                if row is None or Chunk.model_validate(row.payload) != chunk:
                    raise DomainError("conflict")
            for record in records:
                call = record.get("call")
                if (
                    not isinstance(call, dict)
                    or call.get("model") != space.model
                    or call.get("kind") != "embedding"
                ):
                    raise ValueError("Invalid embedding call identity.")
                session.execute(
                    insert(EmbeddingCallRecord)
                    .values(
                        id=UUID(str(call["id"])),
                        space_id=space.id,
                        chunk_ids=[str(chunk.id) for chunk in chunks],
                        payload=record,
                    )
                    .on_conflict_do_nothing()
                )
            for chunk, value in zip(chunks, values, strict=False):
                session.execute(
                    insert(EmbeddingRecord)
                    .values(
                        chunk_id=chunk.id,
                        space_id=space.id,
                        embedding=list(value),
                    )
                    .on_conflict_do_nothing()
                )

    def visible(self, owner: UUID, job: UUID, content: UUID, policy: str) -> ChunkSet:
        with self.database.transaction() as session:
            if session.scalar(eligible(owner, job, content)) is None:
                raise DomainError("not_found")
            batch = read(session, content, policy)
            if batch is None:
                raise DomainError("conflict")
            return batch

    def nearest(
        self,
        owner: UUID,
        job: UUID,
        batch: ChunkSet,
        space: EmbeddingSpace,
        query: tuple[float, ...],
        limit: int,
    ) -> tuple[Evidence, ...]:
        if not 1 <= limit <= 20:
            raise ValueError("Invalid retrieval limit.")
        distance = EmbeddingRecord.embedding.op("<=>", return_type=Float)(
            list(vector(query))
        )
        scope = (
            ChunkRecord.content_id == batch.content_id,
            ChunkRecord.policy_version == batch.policy.version,
            EmbeddingRecord.space_id == space.id,
        )
        count = (
            select(func.count())
            .select_from(EmbeddingRecord)
            .join(ChunkRecord)
            .where(*scope)
            .correlate(None)
            .scalar_subquery()
        )
        with self.database.transaction() as session:
            rows = session.execute(
                select(ChunkRecord.payload, distance)
                .join(EmbeddingRecord)
                .where(
                    *scope,
                    ChunkRecord.content_id.in_(eligible(owner, job, batch.content_id)),
                    count == len(batch.chunks),
                )
                .order_by(distance, ChunkRecord.index, ChunkRecord.id)
                .limit(limit)
            ).all()
        if not rows:
            self.visible(owner, job, batch.content_id, batch.policy.version)
            raise DomainError("conflict")
        return tuple(
            Evidence(Chunk.model_validate(payload), float(score))
            for payload, score in rows
        )
