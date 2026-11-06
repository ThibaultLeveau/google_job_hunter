from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.exc import DBAPIError
from test_chunks import saved
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_snapshots import register

from job_hunter.application.ai import AI, AIError, Limits, Reply
from job_hunter.application.chunks import ChunkJob
from job_hunter.application.embeddings import EmbedJobs
from job_hunter.domain.chunks import ChunkPolicy, ChunkSet
from job_hunter.domain.embeddings import DIMENSIONS, EmbeddingSpace, vector
from job_hunter.domain.errors import DomainError
from job_hunter.infrastructure.ai import AISettings, FixtureProvider
from job_hunter.infrastructure.chunks import PostgresChunks
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import (
    EmbeddingCallRecord,
    EmbeddingRecord,
    EmbeddingSpaceRecord,
    PostgresEmbeddings,
    create_embeddings,
)
from job_hunter.infrastructure.jobs import RunJobRecord
from job_hunter.settings import Settings

owners, runs = registry_owners, registry_runs
SPACE = EmbeddingSpace(provider="fixture", model="fixture-embedding-v1")
FIXTURE = AISettings(provider="fixture", environment="test")


def axis(index: int = 0) -> list[float]:
    return [float(i == index) for i in range(DIMENSIONS)]


class Provider(FixtureProvider):
    def __init__(self) -> None:
        super().__init__(FIXTURE, "{}")
        self.calls: list[tuple[str, ...]] = []
        self.respond: Callable[[tuple[str, ...]], object] = lambda texts: [
            [
                part * 1e30
                for part in axis(0 if value.startswith(("A", "PRIVATE")) else 1)
            ]
            for value in texts
        ]

    async def embed(self, texts: tuple[str, ...]) -> Reply:
        self.calls.append(texts)
        return Reply(self.respond(texts), 7, 0)


def service(
    database: Database, provider: Provider, space: EmbeddingSpace = SPACE
) -> EmbedJobs:
    return EmbedJobs(
        PostgresEmbeddings(database),
        space,
        lambda accounting: AI(
            provider,
            provider,
            accounting,
            Limits(input_tokens=1024),
            "unused",
            space.model,
        ),
    )


@pytest.fixture
def batch(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> ChunkSet:
    content = saved(
        isolated_database, owners[0], runs[0], "A" * 128 + "B" * 128 + "C" * 128
    )
    return ChunkJob(
        PostgresChunks(isolated_database), ChunkPolicy(characters=128, overlap=0)
    ).run(content)


@pytest.mark.parametrize(
    "values",
    [
        (),
        (1.0,),
        (0.0,) * DIMENSIONS,
        (float("nan"),) * DIMENSIONS,
        (float("inf"),) * DIMENSIONS,
        (1e100,) * DIMENSIONS,
        (1e-100,) * DIMENSIONS,
    ],
)
def test_vectors_reject_invalid_float32_values(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        vector(values)


def test_fixed_configuration_and_space_identity() -> None:
    assert vector(tuple(axis())) == tuple(axis())
    assert vector(tuple(value * 1e-30 for value in axis())) == tuple(axis())
    assert SPACE.id != SPACE.model_copy(update={"revision": "v2"}).id
    with pytest.raises(ValidationError):
        EmbeddingSpace.model_validate({**SPACE.model_dump(), "dimensions": 3})


@pytest.mark.integration
def test_exact_order_visibility_transience_and_invalidation(
    isolated_database: Database,
    batch: ChunkSet,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    provider = Provider()
    worker = service(isolated_database, provider)
    job = register(isolated_database, owners[0], runs[0])
    assert not worker.retrieve(
        owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
    ).complete
    assert not provider.calls
    assert worker.run(batch.content_id, batch.policy.version).written == 3
    assert worker.run(batch.content_id, batch.policy.version).written == 0
    with pytest.raises(DomainError, match="not_found"):
        worker.retrieve(
            owners[1], job, batch.content_id, batch.policy.version, "PRIVATE"
        )
    assert len(provider.calls) == 1
    result = worker.retrieve(
        owners[0], job, batch.content_id, batch.policy.version, "PRIVATE", 3
    )
    assert result.complete and [e.chunk for e in result.evidence] == list(batch.chunks)
    assert [e.distance for e in result.evidence] == [0.0, 1.0, 1.0]
    assert result.accounting[0]["input_tokens"] == 7
    with isolated_database.transaction() as session:
        calls = session.scalars(select(EmbeddingCallRecord)).all()
        assert len(calls) == 1 and "PRIVATE" not in str(calls[0].payload)
        assert session.scalar(select(func.count()).select_from(EmbeddingRecord)) == 3
    changed = service(
        isolated_database, provider, SPACE.model_copy(update={"revision": "v2"})
    )
    assert not changed.retrieve(
        owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
    ).complete
    assert changed.run(batch.content_id, batch.policy.version).written == 3
    policy = ChunkJob(
        PostgresChunks(isolated_database), ChunkPolicy(characters=256, overlap=0)
    ).run(batch.content_id)
    assert worker.run(policy.content_id, policy.policy.version).written == 2
    assert worker.run(batch.content_id, batch.policy.version).written == 0
    saved(isolated_database, owners[0], runs[0], "Changed source")
    with pytest.raises(DomainError, match="not_found"):
        worker.retrieve(
            owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
        )
    assert worker.store.load(batch.content_id, batch.policy.version) == batch


@pytest.mark.integration
def test_access_and_completeness_rechecked_after_private_query(
    isolated_database: Database,
    batch: ChunkSet,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    provider = Provider()
    worker = service(isolated_database, provider)
    job = register(isolated_database, owners[0], runs[0])
    worker.run(batch.content_id, batch.policy.version)
    provider.respond = lambda texts: [[0.0] * DIMENSIONS]
    with pytest.raises(AIError, match="invalid_output"):
        worker.retrieve(
            owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
        )

    def remove_vector(texts: tuple[str, ...]) -> object:
        with isolated_database.transaction() as session:
            session.execute(
                delete(EmbeddingRecord).where(
                    EmbeddingRecord.chunk_id == batch.chunks[0].id
                )
            )
        return [axis()]

    provider.respond = remove_vector
    with pytest.raises(DomainError, match="conflict"):
        worker.retrieve(
            owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
        )
    provider.respond = lambda texts: [axis() for _ in texts]
    assert worker.run(batch.content_id, batch.policy.version).written == 1

    def revoke(texts: tuple[str, ...]) -> object:
        with isolated_database.transaction() as session:
            session.execute(delete(RunJobRecord).where(RunJobRecord.run_id == runs[0]))
        return [axis()]

    provider.respond = revoke
    with pytest.raises(DomainError, match="not_found"):
        worker.retrieve(
            owners[0], job, batch.content_id, batch.policy.version, "PRIVATE"
        )


@pytest.mark.integration
def test_batch_budget_failure_and_resume(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    content = saved(isolated_database, owners[0], runs[0], "A" * 4000)
    batch = ChunkJob(
        PostgresChunks(isolated_database), ChunkPolicy(characters=128, overlap=0)
    ).run(content)
    provider = Provider()
    worker = service(isolated_database, provider)
    first = worker.run(content, batch.policy.version, max_batches=1)
    assert first.written == 8 and first.remaining == 24 and len(provider.calls) == 1
    for response in ([], [axis()], [[1.0]] * 8, [[0.0] * DIMENSIONS] * 8):

        def respond(texts: tuple[str, ...], value: object = response) -> object:
            return value

        provider.respond = respond
        with pytest.raises(AIError, match="invalid_output"):
            worker.run(content, batch.policy.version)
        assert len(worker.store.pending(batch, SPACE)) == 24
    provider.respond = lambda texts: [axis() for _ in texts]
    assert worker.run(content, batch.policy.version).remaining == 0
    assert all(
        1 <= len(call) <= 16 and sum(len(t.encode()) for t in call) <= 1024
        for call in provider.calls
    )
    with isolated_database.transaction() as session:
        assert session.scalar(
            select(func.count()).select_from(EmbeddingCallRecord)
        ) == len(provider.calls)


@pytest.mark.integration
def test_publication_atomicity_concurrency_and_constraints(
    isolated_database: Database, batch: ChunkSet
) -> None:
    provider = Provider()
    worker = service(isolated_database, provider)
    inserts = 0

    def fail(*args: object) -> None:
        nonlocal inserts
        if "INSERT INTO job_embeddings" in str(args[2]):
            inserts += 1
            if inserts == 2:
                raise RuntimeError("interrupted")

    event.listen(isolated_database.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            worker.run(batch.content_id, batch.policy.version)
    finally:
        event.remove(isolated_database.engine, "before_cursor_execute", fail)
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(EmbeddingRecord)) == 0
        assert (
            session.scalar(select(func.count()).select_from(EmbeddingCallRecord)) == 0
        )
    database, barrier = Database(Settings(database_pool_size=2)), Barrier(2)
    worker = service(database, provider)
    original = provider.respond

    def synchronized(texts: tuple[str, ...]) -> object:
        barrier.wait(timeout=5)
        return original(texts)

    provider.respond = synchronized
    try:
        with ThreadPoolExecutor(2) as pool:
            results = [
                pool.submit(worker.run, batch.content_id, batch.policy.version)
                for _ in range(2)
            ]
            assert all(result.result(timeout=10).remaining == 0 for result in results)
        with database.transaction() as session:
            assert (
                session.scalar(select(func.count()).select_from(EmbeddingRecord)) == 3
            )
            assert (
                session.scalar(select(func.count()).select_from(EmbeddingCallRecord))
                == 2
            )
        for model in (EmbeddingRecord, EmbeddingSpaceRecord, EmbeddingCallRecord):
            with pytest.raises(DBAPIError), database.transaction() as session:
                session.execute(
                    update(model).values(
                        {
                            "space_id"
                            if model != EmbeddingSpaceRecord
                            else "id": SPACE.id
                        }
                    )
                )
        for expression in ("'[1,2,3]'::vector", "array_fill(0, ARRAY[1536])::vector"):
            with pytest.raises(DBAPIError), database.transaction() as session:
                session.execute(delete(EmbeddingRecord))
                session.execute(
                    text(
                        "INSERT INTO job_embeddings VALUES "
                        f"(:chunk, :space, {expression})"
                    ),
                    {"chunk": batch.chunks[0].id, "space": SPACE.id},
                )
        config = Config(toml_file="pyproject.toml")
        command.check(config)
        command.downgrade(config, "0024_job_chunks")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        database.close()


@pytest.mark.integration
def test_empty_missing_and_invalid_configuration(
    isolated_database: Database,
    batch: ChunkSet,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    with pytest.raises(ValueError, match="1536"):
        create_embeddings(
            isolated_database,
            FIXTURE.model_copy(update={"limits": Limits(dimensions=3)}),
        )
    provider = Provider()
    worker = service(isolated_database, provider)
    job = register(isolated_database, owners[0], runs[0])
    with pytest.raises(DomainError, match="not_found"):
        worker.run(uuid4(), batch.policy.version)
    with pytest.raises(DomainError, match="conflict"):
        worker.retrieve(owners[0], job, batch.content_id, "missing", "PRIVATE")
    with pytest.raises(ValueError):
        worker.run(batch.content_id, batch.policy.version, 0)
    with pytest.raises(ValueError):
        worker.retrieve(
            owners[0], job, batch.content_id, batch.policy.version, "PRIVATE", 21
        )
    worker.space = SPACE.model_copy(update={"model": "wrong-model"})
    with pytest.raises(ValueError, match="space"):
        worker.run(batch.content_id, batch.policy.version)
    disabled = create_embeddings(isolated_database, AISettings())
    with pytest.raises(AIError, match="disabled"):
        disabled.run(batch.content_id, batch.policy.version)
    fixture = create_embeddings(isolated_database, FIXTURE)
    assert fixture.run(batch.content_id, batch.policy.version).remaining == 0
    large = saved(isolated_database, owners[0], runs[0], "A" * 2000)
    large_batch = ChunkJob(PostgresChunks(isolated_database)).run(large)
    worker.space = SPACE
    with pytest.raises(AIError, match="input_limit"):
        worker.run(large, large_batch.policy.version)
    empty = saved(isolated_database, owners[0], runs[0], "")
    batch = ChunkJob(PostgresChunks(isolated_database)).run(empty)
    worker.space = SPACE
    assert worker.run(empty, batch.policy.version).remaining == 0
    assert worker.retrieve(
        owners[0], job, empty, batch.policy.version, "PRIVATE"
    ).complete
    assert not provider.calls
