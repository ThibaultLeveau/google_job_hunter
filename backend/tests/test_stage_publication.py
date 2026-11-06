from contextlib import nullcontext
from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_chunks import saved
from test_embeddings import SPACE, Provider, axis, service
from test_embeddings import batch as embedding_batch
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs

from job_hunter.application.ai import AI, AIError, Limits
from job_hunter.application.embeddings import EmbedJobs
from job_hunter.domain.chunks import ChunkPolicy, ChunkSet, chunk_text
from job_hunter.domain.embeddings import DIMENSIONS
from job_hunter.infrastructure.chunks import (
    ChunkBatchRecord,
    ChunkRecord,
    PostgresChunks,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import (
    EmbeddingCallRecord,
    EmbeddingRecord,
    EmbeddingSpaceRecord,
    PostgresEmbeddings,
)
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs, batch = registry_owners, registry_runs, embedding_batch


def counts(session: Session) -> list[int]:
    return [
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (
            ChunkBatchRecord,
            ChunkRecord,
            EmbeddingSpaceRecord,
            EmbeddingRecord,
            EmbeddingCallRecord,
        )
    ]


@pytest.mark.parametrize("commit", [False, True])
def test_caller_owns_chunk_vector_and_accounting_transaction(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    commit: bool,
) -> None:
    content_id = saved(isolated_database, owners[0], runs[0], "A" * 384)
    chunks, embeddings = (
        PostgresChunks(isolated_database),
        PostgresEmbeddings(isolated_database),
    )
    draft = chunk_text(
        content_id, chunks.source(content_id), ChunkPolicy(characters=128, overlap=0)
    )
    provider = Provider()

    def respond(texts: tuple[str, ...]) -> object:
        # The only pooled connection is available while the provider executes.
        with isolated_database.transaction() as session:
            assert counts(session) == [0, 0, 0, 0, 0]
        return [axis() for _ in texts]

    provider.respond = respond
    result = service(isolated_database, provider).execute(draft.chunks)
    assert result.error is None and len(result.accounting) == 1
    observer = Database(Settings(database_pool_size=1))
    try:
        with nullcontext() if commit else pytest.raises(RuntimeError, match="rollback"):
            with isolated_database.transaction() as session:
                assert chunks.publish(draft, session) == draft
                embeddings.publish(
                    SPACE, result.chunks, result.vectors, result.accounting, session
                )
                assert session.in_transaction()
                assert counts(session) == [1, 3, 1, 3, 1]
                with observer.transaction() as other:
                    assert counts(other) == [0, 0, 0, 0, 0]
                if not commit:
                    raise RuntimeError("rollback")
        with observer.transaction() as session:
            assert counts(session) == ([1, 3, 1, 3, 1] if commit else [0, 0, 0, 0, 0])
        # Both standalone paths remain usable, with no repeated provider call.
        assert chunks.publish(draft) == draft
        embeddings.publish(SPACE, result.chunks, result.vectors, result.accounting)
        with observer.transaction() as session:
            assert counts(session) == [1, 3, 1, 3, 1]
        assert len(provider.calls) == 1
    finally:
        observer.close()


@pytest.mark.parametrize("invalid_vectors", [False, True])
def test_failed_execution_returns_accounting_without_publishing(
    isolated_database: Database, batch: ChunkSet, invalid_vectors: bool
) -> None:
    provider = Provider()

    def fail(texts: tuple[str, ...]) -> object:
        if invalid_vectors:
            return [[0.0] * DIMENSIONS for _ in texts]
        raise RuntimeError("private provider response must not escape")

    provider.respond = fail
    worker = service(isolated_database, provider)
    result = worker.execute(batch.chunks)
    assert result.error is not None and result.vectors == ()
    assert result.error.code == (
        "invalid_output" if invalid_vectors else "provider_failure"
    )
    assert "private" not in str(result.accounting) and len(result.accounting) == 1
    repository = PostgresEmbeddings(isolated_database)
    with pytest.raises(RuntimeError, match="rollback"):
        with isolated_database.transaction() as session:
            assert counts(session)[2:] == [0, 0, 0]
            repository.publish(
                SPACE, result.chunks, result.vectors, result.accounting, session
            )
            assert counts(session)[2:] == [1, 0, 1]
            raise RuntimeError("rollback")
    with isolated_database.transaction() as session:
        assert counts(session)[2:] == [0, 0, 0]
        repository.publish(
            SPACE, result.chunks, result.vectors, result.accounting, session
        )
    with isolated_database.transaction() as session:
        assert counts(session)[2:] == [1, 0, 1]
    provider.respond = lambda texts: [axis() for _ in texts]
    assert worker.run(batch.content_id, batch.policy.version).remaining == 0
    with isolated_database.transaction() as session:
        assert counts(session)[2:] == [1, len(batch.chunks), 2]


@pytest.mark.parametrize("input_tokens,selected", [(1024, 8), (64000, 16)])
def test_one_batch_is_bounded_and_does_not_persist_during_execution(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    input_tokens: int,
    selected: int,
) -> None:
    content_id = saved(isolated_database, owners[0], runs[0], "A" * 4096)
    chunks = PostgresChunks(isolated_database)
    draft = chunk_text(
        content_id, chunks.source(content_id), ChunkPolicy(characters=128, overlap=0)
    )
    provider = Provider()
    worker = EmbedJobs(
        PostgresEmbeddings(isolated_database),
        SPACE,
        lambda accounting: AI(
            provider,
            provider,
            accounting,
            Limits(input_tokens=input_tokens),
            "unused",
            SPACE.model,
        ),
    )
    result = worker.execute(draft.chunks)
    assert result.error is None and result.chunks == draft.chunks[:selected]
    assert len(result.vectors) == selected and len(provider.calls) == 1
    with isolated_database.transaction() as session:
        assert counts(session) == [0, 0, 0, 0, 0]
    with pytest.raises(AIError, match="input_limit"):
        worker.execute(())
    assert len(provider.calls) == 1
