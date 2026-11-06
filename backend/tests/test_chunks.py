from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from itertools import pairwise
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_snapshots import page, publish, register

from job_hunter.application.chunks import ChunkJob
from job_hunter.domain.chunks import ChunkPolicy, chunk_text
from job_hunter.domain.cleaning import CleanText, materialize
from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import Preparation
from job_hunter.infrastructure.ai import AISettings
from job_hunter.infrastructure.chunks import (
    ChunkBatchRecord,
    ChunkRecord,
    PostgresChunks,
)
from job_hunter.infrastructure.cleaning import PostgresCleaning, create_cleaner
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.preparation import PostgresPreparation, prepare
from job_hunter.settings import Settings

owners, runs = registry_owners, registry_runs


def content(text: str) -> tuple[Preparation, CleanText]:
    source = prepare(
        uuid4(), replace(page(body=text.encode()), content_type="text/plain")
    )
    return source, materialize(
        source,
        tuple(p.id for p in source.paragraphs),
        (),
        "c" * 64,
        None if source.paragraphs else "empty",
    )


@pytest.mark.parametrize(
    "text", ["", "Hello", "日本語 👩🏽‍💻 مرحبا" * 300, "x" * 10000, "First\n" * 1000]
)
@pytest.mark.parametrize("overlap", [0, 16, 32])
def test_bounds_overlap_complete_coverage_and_exact_provenance(
    text: str, overlap: int
) -> None:
    source, cleaned = content(text)
    policy = ChunkPolicy(characters=128, token_bound=512, overlap=overlap)
    result = chunk_text(uuid4(), cleaned, policy)
    assert result == chunk_text(result.content_id, cleaned, policy)
    assert bool(result.chunks) == bool(cleaned.text)
    covered: set[int] = set()
    for index, chunk in enumerate(result.chunks):
        assert chunk.index == index and chunk.text.strip()
        assert chunk.text == cleaned.text[chunk.start : chunk.end]
        assert (
            len(chunk.text) <= 128
            and chunk.token_bound == len(chunk.text.encode()) <= 512
        )
        covered.update(range(chunk.start, chunk.end))
        for span in chunk.spans:
            assert (
                chunk.text[span.chunk_start : span.chunk_end]
                == source.text[span.source_start : span.source_end]
            )
            assert span.paragraph_id in {p.id for p in source.paragraphs}
        if index:
            previous = result.chunks[index - 1]
            assert (
                chunk.start > previous.start and previous.end - chunk.start == overlap
            )
    assert covered == set(range(len(cleaned.text)))
    assert len({c.id for c in result.chunks}) == len(result.chunks)


def test_paragraph_boundaries_policy_versions_and_repeated_text_locations() -> None:
    _, cleaned = content("a" * 70 + "\n" + "b" * 70 + "\n" + "c" * 70)
    policy = ChunkPolicy(characters=128, overlap=16)
    result = chunk_text(uuid4(), cleaned, policy)
    assert [c.end for c in result.chunks] == [70, 142, 214]
    assert (
        chunk_text(result.content_id, cleaned, ChunkPolicy()).policy.version
        != policy.version
    )
    _, repeated = content("x" * 384)
    result = chunk_text(uuid4(), repeated, ChunkPolicy(characters=128, overlap=0))
    assert len(result.chunks) == 3 and len({c.content_hash for c in result.chunks}) == 1
    assert len({c.id for c in result.chunks}) == 3
    with pytest.raises(ValueError, match="hash"):
        chunk_text(
            uuid4(), cleaned.model_copy(update={"content_hash": "0" * 64}), policy
        )


def test_chunk_spans_preserve_source_gaps_after_cleaning() -> None:
    source, _ = content("A" * 80 + "\nHome\n" + "B" * 80)
    first, removed, last = source.paragraphs
    cleaned = materialize(source, (first.id, last.id), (removed.id,), "c" * 64)
    result = chunk_text(uuid4(), cleaned, ChunkPolicy(characters=128, overlap=16))
    span = result.chunks[-1].spans[-1]
    assert span.source_start == last.start > cleaned.mappings[-1].clean_start
    for chunk in result.chunks:
        for mapping in chunk.spans:
            assert (
                chunk.text[mapping.chunk_start : mapping.chunk_end]
                == source.text[mapping.source_start : mapping.source_end]
            )


def test_long_multilingual_paragraphs_keep_chunk_count_bounded() -> None:
    _, cleaned = content(("🦀" * 65 + "\n") * 1400)
    policy = ChunkPolicy(characters=2000, token_bound=512, overlap=64)
    result = chunk_text(uuid4(), cleaned, policy)
    assert len(result.chunks) <= 2048 and result.chunks[-1].end == len(cleaned.text)
    assert all(b.start - a.start >= 50 for a, b in pairwise(result.chunks))


@pytest.mark.parametrize(
    "options",
    [{"overlap": -1}, {"overlap": 400}, {"characters": 10}, {"token_bound": 20}],
)
def test_invalid_policy_is_rejected(options: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ChunkPolicy.model_validate(options)


def saved(
    database: Database, owner: UUID, run: UUID, text: str = "Job prose " * 300
) -> UUID:
    source_id = register(database, owner, run)
    snapshot = publish(
        database,
        source_id,
        replace(page(body=text.encode()), content_type="text/plain"),
    )
    prepared = PostgresPreparation(database).run(snapshot.id)
    return PostgresCleaning(database, create_cleaner(AISettings())).run(prepared.id).id


@pytest.mark.integration
def test_persistence_revision_separation_constraints_and_migrations(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    content_id = saved(isolated_database, owners[0], runs[0])
    repository = PostgresChunks(isolated_database)
    job = ChunkJob(repository)
    first = job.run(content_id)
    assert job.run(content_id) == first
    changed_policy = ChunkJob(repository, ChunkPolicy(characters=512, overlap=32))
    second = changed_policy.run(content_id)
    assert second.policy.version != first.policy.version
    assert not {c.id for c in first.chunks} & {c.id for c in second.chunks}
    later_id = saved(isolated_database, owners[0], runs[0], "Different job content")
    assert job.run(later_id).content_hash != first.content_hash
    assert repository.get(content_id, first.policy.version) == first
    empty_id = saved(isolated_database, owners[0], runs[0], "")
    assert job.run(empty_id).chunks == () and job.run(empty_id).chunks == ()
    with pytest.raises(DomainError, match="not_found"):
        job.run(uuid4())
    with pytest.raises(ValueError):
        repository.publish(first.model_copy(update={"chunks": ()}))
    with pytest.raises(DBAPIError), isolated_database.transaction() as session:
        session.execute(update(ChunkRecord).values(payload={}))
    for invalid in ("", "wrong hash"):
        with pytest.raises(IntegrityError), isolated_database.transaction() as session:
            session.add(
                ChunkRecord(
                    id=uuid4(),
                    content_id=content_id,
                    policy_version=first.policy.version,
                    index=1000,
                    content_hash="0" * 64,
                    payload={"text": invalid},
                )
            )
    with pytest.raises(IntegrityError), isolated_database.transaction() as session:
        chunk = first.chunks[0]
        session.add(
            ChunkRecord(
                id=uuid4(),
                content_id=content_id,
                policy_version=first.policy.version,
                index=chunk.index,
                content_hash=chunk.content_hash,
                payload=chunk.model_dump(mode="json"),
            )
        )
    config = Config(toml_file="pyproject.toml")
    command.check(config)
    command.downgrade(config, "0023_job_cleaning")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.integration
def test_interrupted_publication_leaves_no_partial_batch(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    content_id = saved(isolated_database, owners[0], runs[0])
    job = ChunkJob(PostgresChunks(isolated_database))

    def fail(*args: object) -> None:
        if "INSERT INTO job_chunks" in str(args[2]):
            raise RuntimeError("interrupted publication")

    event.listen(isolated_database.engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            job.run(content_id)
    finally:
        event.remove(isolated_database.engine, "before_cursor_execute", fail)
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ChunkBatchRecord)) == 0
        assert session.scalar(select(func.count()).select_from(ChunkRecord)) == 0
    assert len(job.run(content_id).chunks) > 1


@pytest.mark.integration
def test_concurrent_creation_returns_one_complete_batch(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content_id = saved(isolated_database, owners[0], runs[0])
    database = Database(Settings(database_pool_size=2))
    repository, barrier = PostgresChunks(database), Barrier(2)
    original = repository.source

    def synchronized(content_id: UUID) -> CleanText:
        result = original(content_id)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(repository, "source", synchronized)
    try:
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(ChunkJob(repository).run, content_id) for _ in range(2)
            ]
            results = [future.result(timeout=10) for future in futures]
        assert results[0] == results[1]
        with database.transaction() as session:
            assert (
                session.scalar(select(func.count()).select_from(ChunkBatchRecord)) == 1
            )
            assert session.scalar(select(func.count()).select_from(ChunkRecord)) == len(
                results[0].chunks
            )
    finally:
        database.close()
