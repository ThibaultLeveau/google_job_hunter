import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from threading import Barrier
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.pool import QueuePool
from test_chunks import saved
from test_embeddings import FIXTURE
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_profiles import NOW
from test_requirements import extractor, item, metadata
from test_snapshots import page, publish, register

from job_hunter.application.ai import AI, Limits, Reply
from job_hunter.application.chunks import ChunkJob
from job_hunter.application.matching import EvaluateMatch
from job_hunter.domain.chunks import ChunkPolicy
from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.profiles import Skill
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.ai import FixtureProvider
from job_hunter.infrastructure.chunks import PostgresChunks
from job_hunter.infrastructure.cleaning import CleaningRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import create_embeddings
from job_hunter.infrastructure.job_metadata import PostgresMetadata, create_metadata
from job_hunter.infrastructure.jobs import RunJobRecord
from job_hunter.infrastructure.matches import (
    MatchArtifactRecord,
    MatchCallRecord,
    MatchIdentityRecord,
    PostgresMatches,
)
from job_hunter.infrastructure.preparation import PreparationRecord
from job_hunter.infrastructure.profiles import PostgresProfiles, ProfileRecord
from job_hunter.infrastructure.requirements import PostgresRequirements
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs


class Provider(FixtureProvider):
    def __init__(self, database: Database) -> None:
        super().__init__(FIXTURE, "{}")
        self.database, self.calls, self.check_idle = database, 0, True
        self.before: Callable[[], None] = lambda: None

    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        assert isinstance(self.database.engine.pool, QueuePool)
        if self.check_idle:
            assert self.database.engine.pool.checkedout() == 0
        self.calls += 1
        self.before()
        supplied = json.loads(json.loads(source)["untrusted_source"])
        return Reply(
            {
                "items": [
                    {
                        "requirement_id": row["id"],
                        "status": "matched",
                        "requirement_evidence": [e["id"] for e in row["evidence"]],
                        "profile_evidence": [supplied["profile_facts"][0]["id"]],
                        "rationale": "The accepted profile explicitly lists Python.",
                    }
                    for row in supplied["requirements"]
                ]
            },
            30,
            10,
        )


def setup(
    database: Database, owner: UUID, run: UUID
) -> tuple[PostgresMatches, UUID, Provider]:
    content = saved(database, owner, run, "Python required")
    policy = ChunkPolicy()
    ChunkJob(PostgresChunks(database), policy).run(content)
    with database.transaction() as session:
        clean = session.get(CleaningRecord, content)
        assert clean is not None
        prepared = session.get(PreparationRecord, clean.preparation_id)
        assert prepared is not None
        source = Preparation.model_validate(prepared.payload)
    meta_stage = create_metadata(
        FIXTURE,
        json.dumps({"selection": metadata(source).selection.model_dump(mode="json")}),
    )
    meta = PostgresMetadata(database, meta_stage).run(prepared.id)
    req_stage = extractor([item(source, "Python required", "required")])
    PostgresRequirements(database, req_stage).run(meta.id)
    PostgresProfiles(database, lambda: NOW).put(owner, 0, Skill(name="Python"))
    provider = Provider(database)
    evaluator = EvaluateMatch(
        create_embeddings(database, FIXTURE),
        lambda ledger: AI(
            provider, provider, ledger, Limits(), "fixture-chat", "fixture-embedding-v1"
        ),
        "fixture",
    )
    return (
        PostgresMatches(
            database,
            evaluator,
            cleaning_version=clean.version,
            metadata_version=meta_stage.version,
            requirements_version=req_stage.version,
            policy=policy,
        ),
        register(database, owner, run),
        provider,
    )


def counts(session: Session) -> list[int]:
    return [
        session.scalar(select(func.count()).select_from(model)) or 0
        for model in (MatchIdentityRecord, MatchArtifactRecord, MatchCallRecord)
    ]


def test_private_reuse_attempt_history_and_schema_constraints(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    repository, job, provider = setup(db, owners[0], runs[0])
    first = repository.run(owners[0], job, NOW)
    assert first.payload["state"] == "ready" and first.payload["profile_revision"] == 1
    assert first.payload["owner"] == str(owners[0])
    assert (
        repository.run(owners[0], job, NOW).payload == first.payload
        and provider.calls == 1
    )
    assert (
        repository.get(owners[0], first.match_id, 1).profile_facts
        == first.profile_facts
    )
    for owner, key, generation in [
        (owners[1], first.match_id, 1),
        (owners[0], uuid4(), 1),
        (owners[0], first.match_id, 99),
    ]:
        with pytest.raises(DomainError, match="not_found"):
            repository.get(owner, key, generation)
    second = repository.run(owners[0], job, NOW, 2)
    assert second.match_id == first.match_id and second.generation == 2
    with pytest.raises(DomainError, match="not_found"):
        repository.run(owners[1], job, NOW)
    assert register(db, owners[1], runs[1]) == job
    PostgresProfiles(db, lambda: NOW).put(owners[1], 0, Skill(name="Python"))
    other = repository.run(owners[1], job, NOW)
    assert (
        other.match_id != first.match_id and other.profile_facts != first.profile_facts
    )
    with db.transaction() as session:
        assert counts(session) == [2, 3, 3]
        identity = session.get(MatchIdentityRecord, first.match_id)
        assert identity is not None
        fields = {
            column.name: getattr(identity, column.name)
            for column in identity.__table__.columns
        }
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.execute(insert(MatchIdentityRecord).values(**{**fields, "id": uuid4()}))
    for model in (MatchIdentityRecord, MatchArtifactRecord, MatchCallRecord):
        with pytest.raises(DBAPIError), db.transaction() as session:
            values: dict[str, object] = (
                {"profile_revision": 3}
                if model is MatchIdentityRecord
                else {"payload": {}}
            )
            session.execute(update(model).values(**values))
    config = Config(toml_file="pyproject.toml")
    command.check(config)
    command.downgrade(config, "0027_job_requirements")
    command.upgrade(config, "head")
    command.check(config)


@pytest.mark.parametrize("commit", [False, True])
def test_publication_and_usage_share_the_callers_transaction(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    commit: bool,
) -> None:
    db = isolated_database
    repository, job, _ = setup(db, owners[0], runs[0])
    with db.transaction() as session:
        captured = repository.capture(session, owners[0], job)
    result = repository.evaluator.execute(captured.profile, captured.job)
    observer = Database(Settings(database_pool_size=1))
    try:
        with nullcontext() if commit else pytest.raises(RuntimeError, match="crash"):
            with db.transaction() as session:
                repository.publish(
                    session, owners[0], captured.identity.id, 1, result, NOW
                )
                assert session.in_transaction() and counts(session) == [1, 1, 1]
                with observer.transaction() as other:
                    assert counts(other) == [1, 0, 0]
                if not commit:
                    raise RuntimeError("crash")
        with observer.transaction() as session:
            assert counts(session) == ([1, 1, 1] if commit else [1, 0, 0])
        with db.transaction() as session:
            repository.publish(session, owners[0], captured.identity.id, 1, result, NOW)
            repository.publish(session, owners[0], captured.identity.id, 1, result, NOW)
            assert counts(session) == [1, 1, 1]
    finally:
        observer.close()


@pytest.mark.parametrize(
    "change",
    ["profile", "job", "user", "search", "run", "association", "configuration"],
)
def test_obsolete_or_deleted_inputs_cannot_publish(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    change: str,
) -> None:
    db = isolated_database
    repository, job, _ = setup(db, owners[0], runs[0])
    with db.transaction() as session:
        captured = repository.capture(session, owners[0], job)
    result = repository.evaluator.execute(captured.profile, captured.job)
    if change == "profile":
        PostgresProfiles(db, lambda: NOW).put(owners[0], 1, Skill(name="Go"))
    elif change == "job":
        publish(db, job, page(body=b"<p>Changed job</p>"))
    elif change == "configuration":
        repository.evaluator.version = "changed"
    else:
        with db.transaction() as session:
            if change == "user":
                session.execute(
                    update(UserRecord)
                    .where(UserRecord.id == owners[0])
                    .values(deleted_at=NOW)
                )
            elif change == "search":
                session.execute(
                    update(SavedSearchRecord)
                    .where(SavedSearchRecord.owner_id == owners[0])
                    .values(deleted_at=NOW)
                )
            elif change == "run":
                session.execute(
                    update(SearchRunRecord)
                    .where(SearchRunRecord.id == runs[0])
                    .values(status="cancelled")
                )
            else:
                session.execute(
                    delete(RunJobRecord).where(RunJobRecord.run_id == runs[0])
                )
    with pytest.raises(DomainError), db.transaction() as session:
        if change in {"user", "profile"}:
            # Simulate objects retained from an earlier READ COMMITTED statement.
            model, field, stale = (
                (UserRecord, "deleted_at", None)
                if change == "user"
                else (ProfileRecord, "revision", 1)
            )
            cached = session.get(model, owners[0])
            assert cached is not None
            set_committed_value(cached, field, stale)
        repository.publish(session, owners[0], captured.identity.id, 1, result, NOW)
    with db.transaction() as session:
        assert counts(session) == [1, 0, 0]
    if change == "profile":
        current = repository.run(owners[0], job, NOW)
        assert (
            current.match_id != captured.identity.id
            and current.payload["profile_revision"] == 2
        )


@pytest.mark.parametrize(
    "fault",
    [
        "score",
        "owner",
        "snapshot",
        "evidence",
        "state",
        "reason",
        "usage",
        "generation",
        "foreign_attempt",
    ],
)
def test_forged_provenance_and_call_reuse_are_rejected(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    fault: str,
) -> None:
    db = isolated_database
    repository, job, _ = setup(db, owners[0], runs[0])
    with db.transaction() as session:
        captured = repository.capture(session, owners[0], job)
    result = repository.evaluator.execute(captured.profile, captured.job)
    generation = 1
    content = result.content
    if fault == "score":
        content = content.model_copy(
            update={"score": content.score.model_copy(update={"percent": 1.0})}
        )
    elif fault in {"owner", "snapshot"}:
        content = content.model_copy(
            update={fault if fault == "owner" else "snapshot_id": uuid4()}
        )
    elif fault == "evidence":
        content = content.model_copy(
            update={
                "evaluations": [
                    content.evaluations[0].model_copy(
                        update={"profile_evidence": [uuid4()]}
                    )
                ]
            }
        )
    elif fault == "state":
        content = content.model_copy(update={"state": "unknown"})
    elif fault == "reason":
        content = content.model_copy(update={"reason": "private payload"})
    elif fault == "usage":
        call = result.accounting[0]["call"]
        assert isinstance(call, dict)
        call["schema_hash"] = "private payload"
    elif fault == "generation":
        generation = 0
    else:
        with db.transaction() as session:
            repository.publish(session, owners[0], captured.identity.id, 1, result, NOW)
        generation = 2
    with pytest.raises(ValueError), db.transaction() as session:
        repository.publish(
            session,
            owners[0],
            captured.identity.id,
            generation,
            replace(result, content=content),
            NOW,
        )
    with db.transaction() as session:
        assert counts(session) == (
            [1, 1, 1] if fault == "foreign_attempt" else [1, 0, 0]
        )


def test_duplicate_workers_reuse_one_artifact_and_keep_both_calls(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    repository, job, provider = setup(isolated_database, owners[0], runs[0])
    database, barrier = Database(Settings(database_pool_size=2)), Barrier(2)
    repository.database = database
    provider.check_idle = False

    def wait() -> None:
        barrier.wait(timeout=5)

    provider.before = wait
    try:
        with ThreadPoolExecutor(2) as executor:
            futures = [
                executor.submit(repository.run, owners[0], job, NOW) for _ in range(2)
            ]
            results = [future.result(timeout=15) for future in futures]
        assert results[0].match_id == results[1].match_id
        with database.transaction() as session:
            assert counts(session) == [1, 1, 2]
    finally:
        database.close()


def test_alias_reconciliation_preserves_private_attempts(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    repository, job, _ = setup(db, owners[0], runs[0])
    first = repository.run(owners[0], job, NOW)
    target_url = "https://jobs.example.org/canonical"
    target = register(db, owners[0], runs[0], target_url)
    publish(
        db,
        job,
        replace(
            page(final=target_url, body=b"Python required"), content_type="text/plain"
        ),
    )
    assert repository.get(owners[0], first.match_id, 1).payload == first.payload
    assert repository.run(owners[0], target, NOW).match_id == first.match_id
    refreshed = repository.run(owners[0], target, NOW, 2)
    assert refreshed.match_id == first.match_id and refreshed.payload["job_id"] == str(
        target
    )
    with db.transaction() as session:
        assert counts(session) == [1, 2, 2]


def test_failed_attempt_is_retained_and_only_an_explicit_generation_retries(
    isolated_database: Database, owners: tuple[UUID, UUID], runs: tuple[UUID, UUID]
) -> None:
    repository, job, provider = setup(isolated_database, owners[0], runs[0])

    def fail() -> None:
        raise RuntimeError("private provider failure")

    provider.before = fail
    first = repository.run(owners[0], job, NOW)
    assert (
        first.payload["state"] == "failed"
        and first.payload["reason"] == "provider_failure"
    )
    assert (
        repository.run(owners[0], job, NOW).payload == first.payload
        and provider.calls == 1
    )
    provider.before = lambda: None
    assert repository.run(owners[0], job, NOW, 2).payload["state"] == "ready"
    assert repository.get(owners[0], first.match_id, 1).payload == first.payload
