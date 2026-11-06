from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import event, text, update
from test_discovery import start
from test_job_metadata import TEXT, cite, extractor, selection
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_matches import setup
from test_profiles import NOW, ORIGIN, TOKEN
from test_snapshots import publish, register

from job_hunter.app import create_app
from job_hunter.application.job_listing import Cursor, JobQuery
from job_hunter.domain.errors import DomainError
from job_hunter.domain.job_metadata import Salary
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.profiles import Skill
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.infrastructure.ai import AISettings
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.job_listing import PostgresJobListing, create_listing
from job_hunter.infrastructure.job_metadata import PostgresMetadata
from job_hunter.infrastructure.jobs import PostgresJobRegistry
from job_hunter.infrastructure.match_scheduling import MatchSchedule
from job_hunter.infrastructure.preparation import PostgresPreparation
from job_hunter.infrastructure.profiles import PostgresProfiles
from job_hunter.infrastructure.requirements import (
    PostgresRequirements,
    create_requirements,
)
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
FIXTURE = AISettings(provider="fixture", environment="test")


def known(
    db: Database,
    owner: UUID,
    run: UUID,
    index: int,
    currency: str = "EUR",
    period: str = "year",
    missing: str | None = None,
    failed: bool = False,
) -> UUID:
    url = f"https://jobs.example.org/list/{index}"
    job = register(db, owner, run, url)
    body = TEXT.replace("EUR", currency).replace("per year", f"per {period}")
    snapshot = publish(
        db, job, FetchedSnapshot(url, url, 200, "text/plain", body.encode(), NOW)
    )
    prepared = PostgresPreparation(db).run(snapshot.id)
    source = Preparation.model_validate(prepared.payload)
    chosen = selection(source).model_copy(
        update={
            "salaries": [
                Salary.model_validate(
                    {
                        "minimum": 50000,
                        "maximum": 70000,
                        "currency": currency,
                        "period": period,
                        "basis": "gross",
                        "uncertain": False,
                        "evidence": cite(
                            source, f"{currency} 50,000 - 70,000 per {period} gross"
                        ),
                        **({missing: None} if missing else {}),
                    }
                )
            ]
        }
    )
    metadata = PostgresMetadata(db, extractor(chosen)).run(prepared.id)
    if failed:
        PostgresRequirements(db, create_requirements(FIXTURE, "{}")).run(metadata.id)
    return job


def test_filters_unknowns_salary_units_and_safe_projection(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    listing = create_listing(db, FIXTURE)
    good = known(db, owners[0], runs[0], 1)
    unknown = register(db, owners[0], runs[0], "https://jobs.example.org/unknown")
    foreign = known(db, owners[1], runs[1], 2)
    result = listing.list(owners[0], JobQuery()).items
    assert {row.id for row in result} == {good, unknown} and foreign not in {
        r.id for r in result
    }
    card = next(row for row in result if row.id == good)
    assert card.title.values == ["Software Engineer"] and card.company.values == [
        "Example Corp"
    ]
    assert card.location.values == ["Paris", "Lyon"] and card.remote == "remote"
    assert card.fetched_at == NOW and card.match.score is None
    assert (
        "evidence" not in card.model_dump_json()
        and "Ignore previous" not in card.model_dump_json()
    )
    query = JobQuery(
        keyword="pYtHoN",
        region="france",
        remote="remote",
        currency="EUR",
        period="year",
        salary_min=Decimal(40000),
        salary_max=Decimal(80000),
        match_status="unknown",
    )
    assert [r.id for r in listing.list(owners[0], query).items] == [good]
    for changes in (
        {"currency": "USD"},
        {"period": "month"},
        {"salary_min": Decimal(60000)},
        {"salary_max": Decimal(60000)},
        {"keyword": "%"},
        {"region": "Germany"},
        {"remote": "on_site"},
    ):
        assert listing.list(owners[0], query.model_copy(update=changes)).items == []
    for field, value in (("region", "France"), ("remote", "remote")):
        filtered = JobQuery.model_validate(
            {field: value, f"include_unknown_{field}": True}
        )
        assert len(listing.list(owners[0], filtered).items) == 2
    usd = known(db, owners[0], runs[0], 3, "USD")
    known(db, owners[0], runs[0], 4, "EUR", "month")
    salaries = JobQuery(currency="EUR", period="year", include_unknown_salary=True)
    assert {r.id for r in listing.list(owners[0], salaries).items} == {good, unknown}
    usd_unknown = known(db, owners[0], runs[0], 5, "USD", missing="period")
    assert {r.id for r in listing.list(owners[0], salaries).items} == {good, unknown}
    assert {
        r.id
        for r in listing.list(
            owners[0], salaries.model_copy(update={"currency": "USD"})
        ).items
    } == {usd, usd_unknown, unknown}
    missing = known(db, owners[0], runs[0], 6, missing="minimum")
    bounded = JobQuery(currency="EUR", period="year", salary_min=Decimal(40000))
    assert [r.id for r in listing.list(owners[0], bounded).items] == [good]
    assert missing in {
        r.id
        for r in listing.list(
            owners[0], bounded.model_copy(update={"include_unknown_salary": True})
        ).items
    }
    failed = known(db, owners[0], runs[0], 7, failed=True)
    assert (
        next(
            row for row in listing.list(owners[0], JobQuery()).items if row.id == failed
        ).analysis_status
        == "failed"
    )
    url = "https://jobs.example.org/empty"
    empty = register(db, owners[0], runs[0], url)
    source = publish(db, empty, FetchedSnapshot(url, url, 200, "text/plain", b" ", NOW))
    PostgresPreparation(db).run(source.id)
    assert (
        next(
            row for row in listing.list(owners[0], JobQuery()).items if row.id == empty
        ).analysis_status
        == "unavailable"
    )


@pytest.mark.parametrize("sort", ["newest", "oldest", "title", "match"])
def test_cursor_ties_nulls_binding_and_bounded_queries(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    sort: str,
) -> None:
    db, listing = isolated_database, create_listing(isolated_database, FIXTURE)
    first = known(db, owners[0], runs[0], 1)
    second = known(db, owners[0], runs[0], 2)
    other_run = start(db, owners[0])
    with db.transaction() as session:
        registry = PostgresJobRegistry(session)
        for index in range(3, 83):
            registry.register(
                owners[0], runs[0], f"https://jobs.example.org/{index}", 1, NOW
            )
        registry.register(owners[0], runs[0], "https://jobs.example.org/list/1", 2, NOW)
        registry.register(
            owners[0], other_run.id, "https://jobs.example.org/list/1", 1, NOW
        )
    statements: list[str] = []

    def count(connection: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        query = JobQuery.model_validate({"limit": 1, "sort": sort})
        page = listing.list(owners[0], query)
        assert len(statements) == 1 and page.next_cursor
        cursor = page.next_cursor
        seen = [page.items[0].id]
        while page.next_cursor:
            page = listing.list(
                owners[0], query.model_copy(update={"cursor": page.next_cursor})
            )
            seen.extend(row.id for row in page.items)
        assert len(seen) == len(set(seen)) == 82
        if sort == "title":
            assert set(seen[:2]) == {first, second}
        for changes in (
            {"sort": "oldest" if sort != "oldest" else "newest"},
            {"region": "France"},
        ):
            with pytest.raises(ValueError, match="cursor"):
                listing.list(
                    owners[0], query.model_copy(update={"cursor": cursor, **changes})
                )
        with pytest.raises(ValueError, match="cursor"):
            listing.list(owners[1], query.model_copy(update={"cursor": cursor}))
        invalid = Cursor(
            binding=query.binding(owners[0]),
            id=first,
            value="invalid" if sort != "title" else 42.0,
        ).encode()
        with pytest.raises(ValueError, match="cursor"):
            listing.list(owners[0], query.model_copy(update={"cursor": invalid}))
    finally:
        event.remove(db.engine, "before_cursor_execute", count)


def test_current_stale_failed_match_summaries_and_read_only_get(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    repository, job, provider = setup(db, owners[0], runs[0])
    matching = MatchSchedule(repository, lambda: NOW)
    task = matching.request(owners[0], job, uuid4())
    assert task is not None
    repository.run(owners[0], job, NOW, task.key.subject_version)
    versions = create_listing(db, FIXTURE).versions | {
        "cleaning": repository.cleaning_version,
        "metadata_version": repository.metadata_version,
        "requirements": repository.requirements_version,
        "chunks": repository.policy.version,
        "evaluator": repository.evaluator.version,
    }
    listing = PostgresJobListing(db, versions)
    current = listing.list(owners[0], JobQuery(match_status="current")).items[0]
    assert current.match.score == 100 and current.match.pending is False
    assert current.match.stale is False
    calls = provider.calls
    assert listing.list(owners[0], JobQuery(sort="match")).items[0] == current
    assert provider.calls == calls
    pending = register(db, owners[0], runs[0], "https://jobs.example.org/pending")
    first_page = listing.list(owners[0], JobQuery(sort="match", limit=1))
    assert first_page.items[0].id == job and first_page.next_cursor
    assert (
        listing.list(
            owners[0], JobQuery(sort="match", limit=1, cursor=first_page.next_cursor)
        )
        .items[0]
        .id
        == pending
    )
    retry = matching.request(owners[0], job, uuid4())
    assert retry is not None
    with db.transaction() as session:
        session.execute(
            update(TaskRecord).where(TaskRecord.id == retry.id).values(status="failed")
        )
    assert (
        listing.list(owners[0], JobQuery(match_status="failed")).items[0].match.status
        == "failed"
    )
    assert listing.list(owners[0], JobQuery(match_status="failed")).items[0].match.stale
    PostgresProfiles(db, lambda: NOW).put(owners[0], 1, Skill(name="Go"))
    stale = listing.list(owners[0], JobQuery(match_status="stale")).items[0]
    assert stale.match.score == 100 and stale.match.pending is True
    assert listing.list(owners[1], JobQuery()).items == []
    with db.transaction() as session:
        session.execute(
            update(SearchRunRecord)
            .where(SearchRunRecord.id == runs[0])
            .values(status="cancelled")
        )
    assert listing.list(owners[0], JobQuery()).items == []
    with pytest.raises(DomainError, match="not_found"):
        listing.list(owners[0], JobQuery(run_id=runs[0]))


def test_api_validation_authorization_and_migration(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    known(db, owners[0], runs[0], 1)
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    app.state.job_listing = create_listing(db, FIXTURE)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/v1/jobs").status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.get("/api/v1/jobs", params={"run_id": str(runs[0])})
        assert response.status_code == 200 and len(response.json()["items"]) == 1
        assert (
            client.get("/api/v1/jobs", params={"run_id": str(runs[1])}).status_code
            == 404
        )
        for params in (
            {"limit": 0},
            {"limit": 51},
            {"sort": "salary"},
            {"cursor": "bad%"},
            {"salary_min": 1},
            {"currency": "EUR"},
            {"keyword": " "},
            {"salary_min": 2, "salary_max": 1, "currency": "EUR", "period": "year"},
            {"owner_id": str(owners[1])},
        ):
            assert (
                client.get(
                    "/api/v1/jobs", params={k: str(v) for k, v in params.items()}
                ).status_code
                == 422
            )
        app.state.job_listing = None
        assert client.get("/api/v1/jobs").status_code == 503
    config = Config(toml_file="pyproject.toml")
    command.downgrade(config, "0030_provider_budgets")
    command.upgrade(config, "head")
    with db.transaction() as session:
        assert (
            len(
                session.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE indexname IN "
                        "('ix_match_owner_job','ix_source_merge_target')"
                    )
                ).all()
            )
            == 2
        )
