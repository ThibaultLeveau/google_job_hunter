from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from test_extraction import TEXT, golden, stage
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import Experience, Skill
from job_hunter.domain.review import (
    ReviewBatch,
    ReviewItem,
    ReviewResult,
    reviewed,
    supported,
)
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentParsing,
    DocumentRecord,
    LocalStorage,
    PostgresDocuments,
    PostgresExtraction,
    StorageSettings,
)
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.profiles import (
    FactSourceRecord,
    PostgresProfiles,
    ProfileFactRecord,
    ReviewDecisionRecord,
    ReviewRequestRecord,
)
from job_hunter.infrastructure.worker import PostgresWorkerStore
from job_hunter.settings import Settings

owners = profile_owners


def seed(
    database: Database, owner: UUID, tmp_path: Path
) -> tuple[UUID, dict[str, UUID]]:
    parsed = parse(
        BytesIO((TEXT + "\n" + str(uuid4())).encode()), "text/plain", ParseLimits()
    )
    extraction = PostgresExtraction(
        database, stage(golden(parsed).model_dump_json()), lambda: NOW
    )
    document, _ = PostgresDocuments(database, StorageSettings(), lambda: NOW).add(
        owner,
        uuid4().hex,
        parsed.source_hash or "",
        "resume.txt",
        "text/plain",
        len(parsed.text.encode()),
    )
    parser = DocumentParsing(
        database,
        LocalStorage(tmp_path / "private"),
        lambda _file, _type: parsed,
        lambda: NOW,
        extraction.enqueue,
    )
    store = PostgresWorkerStore(
        database, {**parser.handlers(), **extraction.handlers()}
    )
    task = store.claim(("document_parse",), NOW, timedelta(seconds=60))
    assert task is not None
    store.publish(task, parsed, NOW)
    task = store.claim((extraction.stage.kind,), NOW, timedelta(seconds=60))
    assert task is not None
    store.publish(task, extraction.execute(task), NOW)
    return document.id, {
        str(field.value): proposal.id
        for proposal in extraction.read(owner, document.id, 0).items
        for field in proposal.candidate.fields
        if field.name in {"name", "company"}
    }


def batch(revision: int, *items: ReviewItem) -> ReviewBatch:
    return ReviewBatch(
        expected_revision=revision, idempotency_key=uuid4(), items=list(items)
    )


@pytest.mark.integration
def test_mixed_decisions_edits_replay_and_provenance(
    isolated_database: Database, owners: tuple[UUID, UUID], tmp_path: Path
) -> None:
    document, ids = seed(isolated_database, owners[0], tmp_path)
    store = PostgresProfiles(isolated_database, lambda: NOW)
    rejected = store.review(
        owners[0], batch(0, ReviewItem(proposal_id=ids["Atlas"], action="reject"))
    )
    assert rejected.revision == 0 and not store.read(owners[0]).facts
    request = batch(
        0,
        ReviewItem(proposal_id=ids["Python"], action="accept"),
        ReviewItem(
            proposal_id=ids["Acme"],
            action="edit",
            fact=Experience(company="Acme", title="Staff Engineer"),
        ),
        ReviewItem(proposal_id=ids["Atlas"], action="reject"),
        ReviewItem(proposal_id=ids["Atlas Plus"], action="accept"),
    )
    result = store.review(owners[0], request)
    assert [item.outcome for item in result.items] == [
        "created",
        "created",
        "rejected",
        "created",
    ]
    assert result.revision == 1 and store.review(owners[0], request) == result
    snapshot = store.read(owners[0])
    experience = next(fact for fact in snapshot.facts if fact.data.kind == "experience")
    assert experience.user_fields == ("title",)
    assert [source.field for source in experience.source_associations] == ["company"]
    assert experience.source_associations[0].document_id == document
    assert all(
        source.start is not None and source.end is not None
        for fact in snapshot.facts
        for source in fact.source_associations
    )
    repeated = store.review(owners[0], batch(1, *request.items))
    assert repeated == result
    with pytest.raises(DomainError, match="conflict"):
        store.review(owners[0], request.model_copy(update={"expected_revision": 1}))
    denied = store.review(
        owners[0], batch(1, ReviewItem(proposal_id=ids["Atlas"], action="accept"))
    )
    assert denied.items[0].outcome == "conflict" and denied.revision == 1
    with isolated_database.transaction() as session:
        assert (
            session.scalar(select(func.count()).select_from(ReviewDecisionRecord)) == 4
        )
        span = session.scalars(select(FactSourceRecord)).first()
        assert span is not None
        values = {
            column.name: getattr(span, column.name)
            for column in FactSourceRecord.__table__.columns
        }
    for dangling in (False, True):
        with pytest.raises(IntegrityError), isolated_database.transaction() as session:
            session.execute(
                insert(FactSourceRecord),
                values | {"proposal_id": uuid4() if dangling else span.proposal_id},
            )
    with isolated_database.transaction() as session:
        session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.id == document)
            .values(deleted_at=NOW)
        )
    assert store.review(owners[0], request) == result
    assert all(not fact.source_associations for fact in store.read(owners[0]).facts)


@pytest.mark.integration
def test_exact_cross_document_links_manual_edits_and_distinct_projects(
    isolated_database: Database, owners: tuple[UUID, UUID], tmp_path: Path
) -> None:
    _, first = seed(isolated_database, owners[0], tmp_path)
    _, second = seed(isolated_database, owners[0], tmp_path)
    store = PostgresProfiles(isolated_database, lambda: NOW)
    manual = Experience(
        company=" Acme ", title="Développeur", description="Manual text"
    )
    created = store.put(owners[0], 0, manual)
    assert created.fact is not None
    conflict = store.review(
        owners[0], batch(1, ReviewItem(proposal_id=first["Acme"], action="accept"))
    )
    assert conflict.items[0].outcome == "conflict" and conflict.revision == 1
    corrected = store.review(
        owners[0],
        batch(1, ReviewItem(proposal_id=first["Acme"], action="edit", fact=manual)),
    )
    assert (
        corrected.items[0].outcome == "linked"
        and corrected.items[0].fact_id == created.fact.id
    )
    assert store.get(owners[0], created.fact.id).data == manual
    assert {
        source.field
        for source in store.get(owners[0], created.fact.id).source_associations
    } == {"company", "title"}
    accepted = store.review(
        owners[0],
        batch(
            2,
            *(
                ReviewItem(proposal_id=first[name], action="accept")
                for name in ("Python", "Atlas", "Atlas Plus")
            ),
        ),
    )
    linked = store.review(
        owners[0], batch(3, ReviewItem(proposal_id=second["Python"], action="accept"))
    )
    assert (
        linked.items[0].outcome == "linked"
        and linked.items[0].fact_id == accepted.items[0].fact_id
    )
    snapshot = store.read(owners[0])
    assert snapshot.revision == 4 and len(snapshot.facts) == 4
    skill = next(fact for fact in snapshot.facts if fact.data.kind == "skill")
    assert len({source.document_id for source in skill.source_associations}) == 2
    changed = store.put(
        owners[0],
        4,
        Experience(company="Acme", title="Lead", description="Manual text"),
        created.fact.id,
    )
    assert changed.fact is not None and isinstance(changed.fact.data, Experience)
    assert changed.fact.data.title == "Lead"
    assert [source.field for source in changed.fact.source_associations] == ["company"]
    assert "title" in changed.fact.user_fields
    store.review(
        owners[0],
        batch(5, ReviewItem(proposal_id=first["Acme"], action="edit", fact=manual)),
    )
    assert store.get(owners[0], created.fact.id).data == changed.fact.data


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["same_key", "different_keys", "manual"])
def test_concurrent_acceptance(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    mode: str,
) -> None:
    _, ids = seed(isolated_database, owners[0], tmp_path)
    database = Database(
        Settings(
            database_url=SecretStr(
                isolated_database.engine.url.render_as_string(hide_password=False)
            ),
            database_pool_size=2,
        )
    )
    store, gate = PostgresProfiles(database, lambda: NOW), Barrier(2)
    request = batch(0, ReviewItem(proposal_id=ids["Python"], action="accept"))

    def apply(index: int) -> ReviewResult | str:
        gate.wait(timeout=5)
        try:
            if mode == "manual" and index == 1:
                return store.put(owners[0], 0, Skill(name="Rust")).outcome
            return store.review(
                owners[0],
                request
                if mode == "same_key" or index == 0
                else request.model_copy(update={"idempotency_key": uuid4()}),
            )
        except DomainError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, range(2)))
        assert (
            results[0] == results[1]
            if mode == "same_key"
            else results.count("conflict") == 1
        )
        assert (
            len(store.read(owners[0]).facts) == 1
            and store.read(owners[0]).revision == 1
        )
    finally:
        database.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "failure", ["stale", "foreign", "deleted", "owner", "rollback"]
)
def test_failures_are_atomic(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    doc, ids = seed(
        isolated_database, owners[1] if failure == "foreign" else owners[0], tmp_path
    )
    store = PostgresProfiles(isolated_database, lambda: NOW)
    request = batch(
        1 if failure == "stale" else 0,
        ReviewItem(proposal_id=ids["Python"], action="accept"),
        ReviewItem(proposal_id=ids["Atlas"], action="reject"),
    )
    if failure in {"deleted", "owner"}:
        with isolated_database.transaction() as session:
            if failure == "deleted":
                session.execute(
                    update(DocumentRecord)
                    .where(DocumentRecord.id == doc)
                    .values(deleted_at=NOW)
                )
            else:
                session.execute(
                    update(UserRecord)
                    .where(UserRecord.id == owners[0])
                    .values(deleted_at=NOW)
                )
    flush = Session.flush

    def fail(session: Session, objects: object = None) -> None:
        publication = any(isinstance(row, ReviewRequestRecord) for row in session.new)
        flush(session)
        if publication:
            raise RuntimeError("review transaction fixture")

    with monkeypatch.context() as patch:
        if failure == "rollback":
            patch.setattr(Session, "flush", fail)
        with pytest.raises(RuntimeError if failure == "rollback" else DomainError):
            store.review(owners[0], request)
    with isolated_database.transaction() as session:
        for table in (
            ProfileFactRecord,
            FactSourceRecord,
            ReviewDecisionRecord,
            ReviewRequestRecord,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0
    assert store.read(owners[0]).revision == 0


@pytest.mark.integration
def test_review_api_authentication_validation_and_conflict(
    isolated_database: Database, owners: tuple[UUID, UUID], tmp_path: Path
) -> None:
    _, ids = seed(isolated_database, owners[0], tmp_path)
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = lambda: NOW
    body = batch(0, ReviewItem(proposal_id=ids["Python"], action="accept")).model_dump(
        mode="json"
    )
    with TestClient(app, base_url=ORIGIN) as client:
        url = "/api/v1/profile/review"
        assert client.post(url, json=body, headers=HEADERS).status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        assert client.post(url, json=body).status_code == 403
        assert (
            client.post(
                url, json=body | {"owner_id": str(owners[1])}, headers=HEADERS
            ).status_code
            == 422
        )
        wrong = batch(
            0,
            ReviewItem(
                proposal_id=ids["Python"],
                action="edit",
                fact=Experience(company="A", title="B"),
            ),
        )
        conflict = client.post(url, json=wrong.model_dump(mode="json"), headers=HEADERS)
        assert (
            conflict.json()["items"][0]["outcome"] == "conflict"
            and conflict.json()["revision"] == 0
        )
        response = client.post(url, json=body, headers=HEADERS)
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert client.post(url, json=body, headers=HEADERS).json() == response.json()
        assert (
            client.post(
                url, json=body | {"idempotency_key": str(uuid4())}, headers=HEADERS
            ).status_code
            == 409
        )
        assert (
            client.get("/api/v1/profile").json()["facts"][0]["source_associations"][0][
                "quote"
            ]
            == "Python"
        )


@pytest.mark.parametrize("change", ["duplicate", "empty", "revision", "edit", "accept"])
def test_review_request_validation(change: str) -> None:
    request = batch(0, ReviewItem(proposal_id=uuid4(), action="accept")).model_dump(
        mode="json"
    )
    if change == "duplicate":
        request["items"] *= 2
    if change == "empty":
        request["items"] = []
    if change == "revision":
        request["expected_revision"] = True
    if change == "edit":
        request["items"][0]["action"] = "edit"
    if change == "accept":
        request["items"][0]["fact"] = Skill(name="Python").model_dump()
    with pytest.raises(ValidationError):
        ReviewBatch.model_validate(request)


def test_incomplete_candidate_requires_correction() -> None:
    parsed = parse(BytesIO(TEXT.encode()), "text/plain", ParseLimits())
    candidate = next(
        row for row in golden(parsed).candidates if row.kind == "experience"
    )
    missing = candidate.model_copy(
        update={
            "fields": [
                field.model_copy(
                    update={"value": None, "evidence": [], "uncertainty": "missing"}
                )
                if field.name == "company"
                else field
                for field in candidate.fields
            ]
        }
    )
    with pytest.raises(ValidationError):
        reviewed(missing, ReviewItem(proposal_id=uuid4(), action="accept"))
    data, fields = reviewed(
        missing,
        ReviewItem(
            proposal_id=uuid4(),
            action="edit",
            fact=Experience(company="Acme", title="Développeur"),
        ),
    )
    assert isinstance(data, Experience) and fields == ["company"]


@pytest.mark.integration
def test_existing_manual_fields_survive_migration(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    store = PostgresProfiles(isolated_database, lambda: NOW)
    result = store.put(owners[0], 0, Skill(name="Python"))
    assert result.fact is not None
    config = Config(toml_file=Path(__file__).resolve().parents[1] / "pyproject.toml")
    command.downgrade(config, "0013_profile_proposals")
    command.upgrade(config, "head")
    assert store.get(owners[0], result.fact.id).user_fields == ("name",)


def test_changed_url_path_loses_evidence_but_label_case_keeps_it() -> None:
    assert not supported("url", "https://example.org/path", "https://example.org/Path")
    assert supported("url", " https://example.org/Path ", "https://example.org/Path")
    assert supported("name", " PYTHON ", "Python")
    assert not supported("start_date", None, "2020–2022")
