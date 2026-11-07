import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select, update
from test_chunks import content
from test_embeddings import Provider, service
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_profiles import NOW
from test_worker import make_worker

from job_hunter.application.job_listing import JobQuery
from job_hunter.application.searches import SearchWrite
from job_hunter.domain.chunks import ChunkPolicy, ChunkSet, chunk_text
from job_hunter.domain.preparation import digest
from job_hunter.domain.searches import SearchDefinition
from job_hunter.domain.tasks import NewTask, Task, TaskKey
from job_hunter.infrastructure.ai import AISettings
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    PostgresDocuments,
    StorageSettings,
)
from job_hunter.infrastructure.job_listing import create_listing
from job_hunter.infrastructure.jobs import PostgresJobRegistry
from job_hunter.infrastructure.searches import (
    DISCOVERY,
    PostgresSearches,
    SearchRunRecord,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.settings import Settings

owners, runs = registry_owners, registry_runs
MEASURED: ContextVar[list[int] | None] = ContextVar("measured_queries", default=None)


@pytest.fixture
def counted(isolated_database: Database) -> Iterator[Database]:
    def record(_connection: Any, cursor: Any, *_args: Any) -> None:
        if (counts := MEASURED.get()) is not None:
            counts.append(cursor.rowcount)

    event.listen(isolated_database.engine, "after_cursor_execute", record)
    try:
        yield isolated_database
    finally:
        event.remove(isolated_database.engine, "after_cursor_execute", record)


@contextmanager
def budget(statements: int, rows: int) -> Iterator[None]:
    counts: list[int] = []
    token = MEASURED.set(counts)
    try:
        yield
        assert 1 <= len(counts) <= statements, counts
        assert all(0 <= count <= rows for count in counts), counts
    finally:
        MEASURED.reset(token)


@pytest.mark.integration
def test_paginated_jobs_have_constant_query_and_row_budgets(
    counted: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    expected: dict[UUID, set[UUID]] = {owner: set() for owner in owners}
    with counted.transaction() as session:
        registry = PostgresJobRegistry(session)
        for owner, run, count in zip(owners, runs, (99, 57), strict=True):
            for index in range(count):
                group = "shared" if index < 5 else str(owner)
                url = f"https://jobs.example.org/{group}/{index}"
                expected[owner].add(
                    registry.register(owner, run, url, index + 1, NOW).source_id
                )
    listing = create_listing(
        counted, AISettings(provider="fixture", environment="test")
    )
    for owner, run in zip(owners, runs, strict=True):
        for scope in (None, run):
            cursor: str | None = None
            seen: list[UUID] = []
            while True:
                with budget(1 if scope is None else 2, 51):
                    page = listing.list(
                        owner, JobQuery(limit=50, cursor=cursor, run_id=scope)
                    )
                assert 0 < len(page.items) <= 50
                seen.extend(row.id for row in page.items)
                cursor = page.next_cursor
                if cursor is None:
                    break
            assert len(seen) == len(set(seen)) and set(seen) == expected[owner]
    assert len(expected[owners[0]] & expected[owners[1]]) == 5
    first = listing.list(owners[0], JobQuery(limit=50))
    with pytest.raises(ValueError, match="cursor"):
        listing.list(owners[1], JobQuery(limit=50, cursor=first.next_cursor))


@pytest.mark.integration
def test_private_listing_budgets_preserve_current_worker_failures(
    counted: Database, owners: tuple[UUID, UUID]
) -> None:
    searches = PostgresSearches(counted, lambda: NOW, ())
    documents = PostgresDocuments(counted, StorageSettings(), lambda: NOW)
    expected: dict[UUID, dict[UUID, str]] = {owner: {} for owner in owners}
    expected_searches: dict[UUID, set[UUID]] = {owner: set() for owner in owners}
    expected_documents: dict[UUID, set[UUID]] = {owner: set() for owner in owners}
    for owner in owners:
        for index in range(103):
            saved = searches.save(
                owner,
                SearchWrite(
                    name=f"Private {index}",
                    definition=SearchDefinition(groups=[["Python"]]),
                ),
            )
            expected_searches[owner].add(saved.id)
            document_id = uuid4()
            expected_documents[owner].add(document_id)
            run = searches.start(owner, saved.id, 1, uuid4())
            pages = 1 if index % 4 in (1, 2) else 0
            status = (
                "completed" if index % 4 == 3 else "running" if pages else "pending"
            )
            expected[owner][run.id] = {
                0: "failed",
                1: "partial",
                2: "running",
                3: "completed",
            }[index % 4]
            with counted.transaction() as session:
                session.execute(
                    update(SearchRunRecord)
                    .where(SearchRunRecord.id == run.id)
                    .values(status=status, discovery={"pages_completed": pages})
                )
                PostgresTaskQueue(session).enqueue(
                    NewTask(TaskKey(DISCOVERY, run.id, 2), run.id, NOW), NOW
                )
                session.execute(
                    update(TaskRecord)
                    .where(
                        TaskRecord.subject_id == run.id,
                        TaskRecord.kind == DISCOVERY,
                        TaskRecord.subject_version == (2 if index % 4 == 1 else 1),
                    )
                    .values(status="failed")
                )
                session.add(
                    DocumentRecord(
                        id=document_id,
                        owner_id=owner,
                        content_hash=digest(str(index)),
                        storage_key=uuid4().hex,
                        filename="fixture.txt",
                        media_type="text/plain",
                        size_bytes=1,
                        version=1,
                        status="queued",
                        created_at=NOW,
                    )
                )
    for owner in owners:
        seen_runs: dict[UUID, str] = {}
        seen_searches: set[UUID] = set()
        seen_documents: set[UUID] = set()
        for offset in (0, 50, 100, 150):
            with budget(2, 51):
                page = searches.runs(owner, 50, offset)
            for run in page.items:
                assert run.id not in seen_runs
                seen_runs[run.id] = run.status
                assert run.discovery.error_code == (
                    "worker_failure" if run.status in {"failed", "partial"} else None
                )
            with budget(1, 51):
                definitions = searches.list(owner, 50, offset)
            seen_searches.update(row.id for row in definitions.items)
        for offset in (0, 100, 200):
            with budget(1, 100):
                uploads = documents.list(owner, offset)
            seen_documents.update(row.id for row in uploads)
        assert seen_runs == expected[owner]
        assert seen_searches == expected_searches[owner]
        assert seen_documents == expected_documents[owner]


@pytest.mark.integration
def test_large_input_backlog_has_bounded_fanout_and_materialized_output(
    isolated_database: Database,
) -> None:
    _, cleaned = content("🦀" * 100000)
    assert len(cleaned.text) == 100000
    policy = ChunkPolicy(characters=2000, token_bound=512, overlap=64)
    database = Database(Settings(database_pool_size=4))
    ready, release = Barrier(5), Event()
    published: list[UUID] = []
    with database.transaction() as session:
        queue = PostgresTaskQueue(session)
        for _ in range(1000):
            subject = uuid4()
            queue.enqueue(NewTask(TaskKey("stage", subject, 1), subject, NOW), NOW)

    def execute(task: Task) -> object:
        result = chunk_text(task.key.subject_id, cleaned, policy)
        assert result.chunks[-1].end == 100000
        assert len(result.chunks) <= 2048
        assert sum(len(c.text.encode()) for c in result.chunks) <= 800000
        assert sum(len(c.spans) for c in result.chunks) <= 2048
        ready.wait(timeout=30)
        assert release.wait(timeout=30)
        return result

    def publish(_session: Any, task: Task, result: object, _now: Any) -> None:
        assert isinstance(result, ChunkSet)
        published.append(task.id)

    try:
        with ThreadPoolExecutor(4) as external, ThreadPoolExecutor(2) as claimers:
            workers = [
                make_worker(
                    database,
                    execute,
                    publish=publish,
                    submit=external.submit,
                    concurrency=2,
                )[0]
                for _ in range(2)
            ]
            for future in [claimers.submit(worker.tick) for worker in workers]:
                future.result(timeout=30)
            ready.wait(timeout=30)
            for worker in workers:
                worker.tick()
                worker.stop.set()
            with database.transaction() as session:
                states = dict(
                    session.execute(
                        select(TaskRecord.status, func.count()).group_by(
                            TaskRecord.status
                        )
                    ).all()
                )
                assert states == {"pending": 996, "running": 4}
            release.set()
            for worker in workers:
                for running in worker.running:
                    running.future.result(timeout=30)
                worker.tick()
            assert len(published) == len(set(published)) == 4
            with database.transaction() as session:
                assert (
                    session.scalar(
                        select(func.count())
                        .select_from(TaskRecord)
                        .where(TaskRecord.status == "succeeded")
                    )
                    == 4
                )
        batch = chunk_text(uuid4(), cleaned, policy)
        provider = Provider()
        embedded = service(database, provider).execute(batch.chunks)
        assert embedded.error is None and len(provider.calls) == 1
        assert len(embedded.chunks) == len(embedded.vectors) == 2
        assert sum(len(text.encode()) for text in provider.calls[0]) <= 1024
    finally:
        release.set()
        database.close()


@pytest.mark.parametrize(
    "operation",
    [
        "socket.getaddrinfo('unexpected.invalid', 443)",
        "socket.gethostbyname('unexpected.invalid')",
        "connection.connect(('203.0.113.1', 443))",
        "connection.connect_ex(('203.0.113.1', 443))",
        "connection.sendto(b'fixture', ('203.0.113.1', 443))",
        "connection.sendmsg([b'fixture'], [], 0, ('203.0.113.1', 443))",
    ],
)
def test_caught_external_network_still_fails_pytest(
    tmp_path: Path, operation: str
) -> None:
    probe = tmp_path / "test_probe.py"
    probe.write_text(f"""import socket, sys

def test_probe():
    def last_resort(event, args):
        if event.startswith("socket.") and event != "socket.__new__":
            raise RuntimeError("Fallback blocks real network")
    sys.addaudithook(last_resort)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        try:
            {operation}
        except Exception:
            pass
""")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "conftest",
            "-c",
            "pyproject.toml",
            str(probe),
            "-q",
            "--tb=short",
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parent.resolve())},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Unexpected external test network (even if caught)" in result.stdout
