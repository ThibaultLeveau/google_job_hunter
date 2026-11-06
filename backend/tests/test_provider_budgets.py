import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from test_ai import PROMPT, Answer, Ledger, settings
from test_discovery import Provider, searches, start
from test_extraction import TEXT, golden, stage
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_matches import setup
from test_pipeline import build
from test_profiles import ORIGIN, TOKEN
from test_worker import FakeClock, immediate

from job_hunter.app import create_app
from job_hunter.application.ai import AI, Accounting, AIError, Reply
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.search_provider import SearchError, SearchRequest
from job_hunter.application.worker import Worker
from job_hunter.domain.tasks import StaleLease, Task
from job_hunter.infrastructure.ai import AISettings, FixtureProvider, create_ai
from job_hunter.infrastructure.budgets import (
    BudgetReservationRecord,
    BudgetSettings,
    Ceilings,
    PostgresBudgets,
)
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.discovery import PostgresDiscovery
from job_hunter.infrastructure.documents import (
    DocumentParsing,
    LocalStorage,
    PostgresDocuments,
    PostgresExtraction,
    StorageSettings,
)
from job_hunter.infrastructure.match_scheduling import MatchSchedule
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.provider_budgets import (
    CURRENT,
    BudgetedSearch,
    ChargeScope,
    ProviderBudgets,
)
from job_hunter.infrastructure.search_provider import (
    SearchSettings,
    SerpApiSearchProvider,
    create_search_provider,
)
from job_hunter.infrastructure.searches import DISCOVERY
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord, snapshot
from job_hunter.infrastructure.worker import Handler, PostgresWorkerStore
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
LARGE = Ceilings(global_daily=1_000_000, owner_daily=1_000_000, run_daily=1_000_000)


def budgets(
    db: Database, clock: FakeClock, search: Ceilings = LARGE
) -> PostgresBudgets:
    return PostgresBudgets(db, BudgetSettings(tokens=LARGE, search=search), clock.now)


def rows(db: Database) -> list[BudgetReservationRecord]:
    with db.transaction() as session:
        return list(session.scalars(select(BudgetReservationRecord)))


def drain(db: Database, clock: FakeClock, handlers: dict[str, Handler]) -> None:
    worker = Worker(
        PostgresWorkerStore(db, handlers),
        {k: h.execute for k, h in handlers.items()},
        clock,
        immediate,
        Event(),
        lambda: 1,
        concurrency=1,
    )
    for _ in range(100):
        worker.tick()
        with db.transaction() as session:
            remaining = session.scalar(
                select(func.count())
                .select_from(TaskRecord)
                .where(
                    TaskRecord.kind.in_(handlers),
                    TaskRecord.status.in_(("pending", "running")),
                )
            )
        if not remaining and not worker.running:
            assert CURRENT.get() is None
            return
        clock.sleep(1)
    raise AssertionError("Budgeted work did not finish")


@pytest.mark.parametrize("global_limit", [1, 2])
def test_search_admission_usage_api_owner_scope_and_redaction(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    global_limit: int,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, clock = isolated_database, FakeClock()
    ledger = budgets(
        db, clock, Ceilings(global_daily=global_limit, owner_daily=1, run_daily=1)
    )
    caplog.set_level(logging.DEBUG)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "search_metadata": {"status": "Success"},
                "organic_results": [
                    {
                        "position": 1,
                        "title": "Public job",
                        "link": "https://jobs.example.org/1",
                    }
                ],
                "serpapi_pagination": {
                    "next": "https://serpapi.com/search.json?start=10"
                },
            },
        )

    provider = SerpApiSearchProvider(
        SearchSettings(provider="serpapi", api_key=SecretStr("fixture-key-canary")),
        transport=httpx.MockTransport(respond),
    )
    wrapped = BudgetedSearch(provider)
    with pytest.raises(SearchError, match="unconfigured"):
        wrapped.fetch(SearchRequest(query="private-canary"))
    service = PostgresDiscovery(db, wrapped, clock.now)
    runner = ProviderBudgets(ledger, "unused", set())
    handlers = runner.handlers(service.handlers())
    first = start(db, owners[0])
    drain(db, clock, handlers)
    result = searches(db).run(owners[0], first.id)
    assert result.status == "partial" and result.discovery.error_code == "quota"
    assert result.discovery.provider_requests == 1 and len(calls) == 1
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = clock.now
    app.state.provider_budgets = ledger
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/searches/provider").status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.get(f"/api/v1/searches/provider?owner_id={owners[1]}")
        assert (
            response.status_code == 200
            and response.json()["quota"]["search_remaining"] == 0
        )
        assert "private-canary" not in response.text + caplog.text
        assert all(str(owner) not in response.text for owner in owners)
        app.state.provider_budgets = None
        assert client.get("/api/v1/searches/provider").status_code == 503
        app.state.provider_budgets = ledger
        with monkeypatch.context() as patch:

            def unavailable(*args: Any) -> None:
                raise SQLAlchemyError("private-canary")

            patch.setattr(ledger, "availability", unavailable)
            response = client.get("/api/v1/searches/provider")
            assert response.status_code == 503 and "private-canary" not in response.text
    second = start(db, owners[1])
    drain(db, clock, handlers)
    result = searches(db).run(owners[1], second.id)
    assert result.discovery.error_code == "quota"
    assert result.discovery.provider_requests == global_limit - 1
    assert len(calls) == global_limit
    assert all(row.ceiling == row.charged == 1 for row in rows(db))
    metrics = [
        record.search_usage
        for record in caplog.records
        if hasattr(record, "search_usage")
    ]
    assert len(metrics) == global_limit and all(
        metric["requests"] == 1 for metric in metrics
    )
    assert all(str(owner) not in json.dumps(metrics) for owner in owners)
    for private in ("secret query", "fixture-key-canary", "private-canary"):
        assert private not in caplog.text + json.dumps(metrics)


def test_ai_repairs_embedding_unknown_usage_and_no_unscoped_paid_call(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db, clock, trace = isolated_database, FakeClock(), Ledger()
    ledger = budgets(db, clock)
    ai = create_ai(settings(), trace)
    calls = 0

    async def chat(*args: Any) -> Reply:
        nonlocal calls
        assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        calls += 1
        return Reply({"value": 42 if calls == 1 else "ok"}, 100, 10)

    assert ai.chat is not None and ai.embeddings is not None
    monkeypatch.setattr(ai.chat, "generate", chat)
    with pytest.raises(AIError, match="accounting_failure"):
        ai.extract(PROMPT, "private-canary", Answer, "v1")
    assert calls == 0 and not rows(db)
    token = CURRENT.set(ChargeScope(ledger, owners[0], runs[0]))
    try:
        assert ai.extract(PROMPT, "private-canary", Answer, "v1").value.value == "ok"
        assert len(rows(db)) == 2 and sum(row.charged for row in rows(db)) == 220

        async def embed(texts: tuple[str, ...]) -> Reply:
            return Reply([[1.0, 0.0, 0.0]], 20, 0)

        monkeypatch.setattr(ai.embeddings, "embed", embed)
        ai.embed(("private query text " * 3,))
        assert sum(row.charged for row in rows(db)) == 240

        async def timeout(*args: Any) -> Reply:
            raise AIError("timeout")

        monkeypatch.setattr(ai.chat, "generate", timeout)
        with pytest.raises(AIError, match="timeout"):
            ai.extract(PROMPT, "private-canary", Answer, "v1")
        unknown = [row for row in rows(db) if row.state == "unknown"]
        assert len(unknown) == 1 and unknown[0].charged == unknown[0].ceiling
        ledger.settings = BudgetSettings(
            tokens=Ceilings(global_daily=0, owner_daily=0, run_daily=0)
        )
        with pytest.raises(AIError, match="quota"):
            ai.extract(PROMPT, "private-canary", Answer, "v1")
        assert len(rows(db)) == 4
        disabled = create_ai(AISettings(), Ledger())
        with pytest.raises(AIError, match="disabled"):
            disabled.extract(PROMPT, "private-canary", Answer, "v1")
    finally:
        CURRENT.reset(token)
    assert all(row.owner_id == owners[0] and row.run_id == runs[0] for row in rows(db))
    assert (
        "private-canary" not in caplog.text and "fixture-key-canary" not in caplog.text
    )


def test_public_pipeline_retry_and_reuse_charge_only_global_budget(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = isolated_database
    pipeline, clock, _ = build(db, owners[0], runs[0])
    runner = ProviderBudgets(budgets(db, clock), "unused", set(pipeline.kinds.values()))
    handlers = runner.handlers(pipeline.handlers())
    original, count = FixtureProvider.embed, 0

    async def embed(provider: FixtureProvider, texts: tuple[str, ...]) -> Reply:
        nonlocal count
        assert db.engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        count += 1
        if count == 1:
            raise AIError("timeout")
        return await original(provider, texts)

    monkeypatch.setattr(FixtureProvider, "embed", embed)
    drain(db, clock, handlers)
    with db.transaction() as session:
        failed = snapshot(
            session.scalars(
                select(TaskRecord).where(
                    TaskRecord.kind == pipeline.kinds["embed"],
                    TaskRecord.status == "failed",
                )
            ).one()
        )
        PostgresTaskQueue(session).retry(failed.key, failed.generation, clock.now())
    with pytest.raises(StaleLease):
        runner.execute(handlers[failed.key.kind].execute, failed)
    drain(db, clock, handlers)
    charges = rows(db)
    assert count >= 4 and len(charges) >= 6
    assert all(row.owner_id is None and row.run_id is None for row in charges)
    assert sum(row.state == "unknown" for row in charges) == 1
    pipeline.backfill(None, clock.now())
    drain(db, clock, handlers)
    assert len(rows(db)) == len(charges)


def test_private_matching_and_profile_extraction_use_owned_scopes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    tmp_path: Path,
) -> None:
    db, clock = isolated_database, FakeClock()
    repository, job, provider = setup(db, owners[0], runs[0])
    config = AISettings(
        provider="fixture",
        environment="test",
        chat_model="fixture-chat",
        embedding_model="fixture-embedding-v1",
    )

    def factory(ledger: Accounting) -> AI:
        ai = create_ai(config, ledger, fixture_response="{}")
        ai.chat = ai.embeddings = provider
        return ai

    repository.evaluator.factory = factory
    matching = MatchSchedule(repository, clock.now)
    ledger = budgets(db, clock)
    drain(
        db,
        clock,
        ProviderBudgets(ledger, "unused", set()).handlers(matching.handlers()),
    )
    assert matching.status(owners[0], job)["state"] == "current"
    assert [(row.owner_id, row.run_id) for row in rows(db)] == [(owners[0], runs[0])]
    parsed = parse(BytesIO(TEXT.encode()), "text/plain", ParseLimits())
    extraction = PostgresExtraction(
        db, stage(golden(parsed).model_dump_json()), clock.now
    )
    storage = LocalStorage(tmp_path / "private")
    uploads = DocumentUploads(
        PostgresDocuments(db, StorageSettings(), clock.now), storage, 10000
    )
    with uploads.receive("resume", "text/plain") as pending:
        pending.write(TEXT.encode())
        document, _ = uploads.finish(owners[1], pending)
    parsing = DocumentParsing(
        db, storage, lambda *_: parsed, clock.now, extraction.enqueue
    )
    handlers = ProviderBudgets(ledger, extraction.stage.kind, set()).handlers(
        {**parsing.handlers(), **extraction.handlers()}
    )
    drain(db, clock, handlers)
    assert extraction.read(owners[1], document.id, 0).status == "ready"
    assert any(row.owner_id == owners[1] and row.run_id is None for row in rows(db))


def test_concurrent_workers_keep_owner_context_and_refund_unused_admission(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
) -> None:
    db, clock, barrier = (
        Database(Settings(database_pool_size=4)),
        FakeClock(),
        Barrier(2),
    )
    ledger = budgets(db, clock)
    runner = ProviderBudgets(ledger, "unused", set())
    try:
        with db.transaction() as session:
            tasks = [
                PostgresTaskQueue(session).claim(
                    (DISCOVERY,), clock.now(), timedelta(seconds=60)
                )
                for _ in owners
            ]
        assert all(task is not None for task in tasks)

        def execute(task: Task) -> None:
            index = runs.index(task.payload_ref)
            barrier.wait(timeout=5)
            ai = create_ai(
                AISettings(provider="fixture", environment="test"),
                Ledger(),
                fixture_response='{"value":"ok"}',
            )
            result = ai.extract(PROMPT, "private-canary", Answer, "v1")
            receipt = next(
                row for row in rows(db) if row.id == result.records[0].call.id
            )
            assert (receipt.owner_id, receipt.run_id) == (owners[index], runs[index])

        def invoke(task: Task | None) -> None:
            assert task is not None and CURRENT.get() is None
            runner.execute(execute, task)
            assert CURRENT.get() is None

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(invoke, tasks))
        unused = [row for row in rows(db) if row.unit == "search"]
        assert len(unused) == 2 and all(
            row.state == "settled" and row.charged == 0 for row in unused
        )
    finally:
        db.close()


@pytest.mark.parametrize("disabled", [False, True])
def test_search_timeout_unknown_spend_and_disabled_provider(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    disabled: bool,
) -> None:
    db, clock = isolated_database, FakeClock()
    provider = (
        create_search_provider(SearchSettings())
        if disabled
        else BudgetedSearch(Provider((SearchError("timeout"),)))
    )
    runner = ProviderBudgets(
        budgets(db, clock), "unused", set(), search_enabled=not disabled
    )
    service = PostgresDiscovery(db, provider, clock.now)
    run = start(db, owners[0])
    drain(db, clock, runner.handlers(service.handlers()))
    result = searches(db).run(owners[0], run.id)
    assert result.discovery.error_code == ("disabled" if disabled else "timeout")
    assert len(rows(db)) == (0 if disabled else 1)
    if not disabled:
        assert rows(db)[0].state == "unknown" and rows(db)[0].charged == 1
