import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from job_hunter.app import create_app
from job_hunter.application.sessions import Sessions, csrf_token, token_hash
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import (
    Experience,
    Project,
    Skill,
    SourceEvidence,
    exact_key,
)
from job_hunter.infrastructure.accounts import SessionRecord, UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.profiles import FACT, PostgresProfiles, ProfileFactRecord
from job_hunter.settings import Settings

NOW = datetime(2026, 1, 1, tzinfo=UTC)
BASE = "/api/v1/profile"
FACTS = BASE + "/facts"
ORIGIN = "https://testserver"
TOKEN = "a" * 43
HEADERS = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token(TOKEN)}


@pytest.fixture
def owners(isolated_database: Database) -> tuple[UUID, UUID]:
    owners = uuid4(), uuid4()
    with isolated_database.transaction() as session:
        for owner, token in zip(owners, (TOKEN, "b" * 43), strict=True):
            email = f"{owner}@example.org"
            session.add(
                UserRecord(
                    id=owner,
                    email=email,
                    email_normalized=email,
                    created_at=NOW,
                    email_verified_at=NOW,
                )
            )
            session.flush()
            session.add(
                SessionRecord(
                    id=uuid4(),
                    user_id=owner,
                    token_hash=token_hash(token),
                    expires_at=NOW + timedelta(days=1),
                    authenticated_at=NOW,
                    last_seen_at=NOW,
                    auth_method="password",
                )
            )
    return owners


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("  STRAẞE\t Python ", "strasse python"),
        ("Cafe\u0301", "CAFÉ"),
        ("Ｐｙｔｈｏｎ", "python"),
    ],
)
def test_exact_skill_normalization_preserves_display(left: str, right: str) -> None:
    first, second = Skill(name=left), Skill(name=right)
    assert exact_key(first) == exact_key(second)
    assert first.name == left and second.name == right


def test_natural_keys_distinguish_unknowns_and_lookalikes() -> None:
    base = Experience(company=" Acme ", title="Developer", start_date=date(2020, 1, 1))
    equivalent = Experience(
        company="ACME", title="developer", start_date=date(2020, 1, 1)
    )
    assert exact_key(base) == exact_key(equivalent)
    variants = [
        Experience(
            company="Acme", title="Software Developer", start_date=base.start_date
        ),
        Experience(company="Acme", title="Developer"),
        Experience(
            company="Acme", title="Developer", start_date=base.start_date, ongoing=False
        ),
    ]
    assert all(exact_key(base) != exact_key(value) for value in variants)
    assert exact_key(Project(name="Site")) != exact_key(
        Project(name="Site", url="https://example.org")
    )
    assert exact_key(Project(name="Site", url="https://example.org/Path")) != exact_key(
        Project(name="Site", url="https://example.org/path")
    )
    parsed = FACT.validate_json(
        '{"kind":"experience","company":"A","title":"B","start_date":"2020-01-01"}'
    )
    assert isinstance(parsed, Experience) and parsed.start_date == date(2020, 1, 1)


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "skill", "name": ""},
        {"kind": "skill", "name": " \t "},
        {"kind": "skill", "name": "x" * 121},
        {"kind": "skill", "name": "\ud800"},
        {"kind": "skill", "name": "\x00"},
        {"kind": "skill", "name": 123},
        {"kind": "project", "name": "Site", "url": "javascript:alert(1)"},
        {"kind": "project", "name": "Site", "url": "https://user:secret@example.org"},
        {"kind": "project", "name": "Site", "url": "https://example.org:99999"},
        {"kind": "project", "name": "Site", "url": "https://exa mple.org"},
        {"kind": "project", "name": "Site", "url": "http://"},
        {"kind": "project", "name": "Site", "url": "https://@example.org"},
        {"kind": "project", "name": "Site", "start_date": "2026-02-30"},
        {"kind": "project", "name": "Site", "start_date": "2026-W01-1"},
        {"kind": "project", "name": "Site", "start_date": 123},
        {
            "kind": "project",
            "name": "Site",
            "start_date": "2026-02-01",
            "end_date": "2026-01-01",
        },
        {"kind": "project", "name": "Site", "ongoing": True, "end_date": "2026-01-01"},
        {"kind": "project", "name": "Site", "description": "x" * 4001},
        {"kind": "skill", "name": "Python", "source_associations": []},
    ],
)
def test_fact_validation_rejects_invalid_or_untrusted_values(
    data: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        FACT.validate_json(json.dumps(data))


def test_source_contract_preserves_version_language_and_quote() -> None:
    evidence = SourceEvidence(
        document_id=uuid4(),
        source_version=1,
        language="fr",
        locator="paragraph:2",
        quote="Développeur Python",
    )
    assert evidence.quote == "Développeur Python" and evidence.language == "fr"
    with pytest.raises(ValidationError):
        SourceEvidence(**{**evidence.model_dump(), "source_version": 0})
    assert "source_associations" not in Skill.model_fields
    with pytest.raises(ValidationError) as error:
        Skill(name="private-canary\ud800")
    assert "private-canary" not in str(error.value)


@pytest.mark.integration
@pytest.mark.parametrize(
    "data",
    [
        {"kind": "skill", "name": "Python"},
        {
            "kind": "experience",
            "company": "Acme",
            "title": "Developer",
            "start_date": "2020-01-01",
        },
        {"kind": "project", "name": "Site", "url": None},
    ],
)
def test_real_api_crud_ownership_revisions_and_validation(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    caplog: pytest.LogCaptureFixture,
    data: dict[str, object],
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(Sessions, app.state.sessions).now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(BASE).status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        assert client.get(BASE).json() == {
            "id": str(owners[0]),
            "revision": 0,
            "facts": [],
        }
        revision = 0
        body = {"expected_revision": revision, "fact": data}
        assert client.post(FACTS, json=body).status_code == 403
        created = client.post(FACTS, json=body, headers=HEADERS)
        assert created.status_code == 201, created.text
        revision += 1
        item = created.json()["fact"]
        path = FACTS + "/" + item["id"]
        assert (
            created.json()["outcome"] == "created" and item["source_associations"] == []
        )
        assert client.get(path).json() == item
        assert client.post(FACTS, json=body, headers=HEADERS).status_code == 409
        body["expected_revision"] = revision
        assert (
            client.post(FACTS, json=body, headers=HEADERS).json()["outcome"]
            == "existing"
        )
        assert (
            client.put(path, json=body, headers=HEADERS).json()["revision"] == revision
        )
        client.cookies.set("job_hunter_session", "b" * 43)
        assert client.get(BASE).json()["facts"] == []
        assert client.get(path).status_code == 404
        foreign = {"Origin": ORIGIN, "X-CSRF-Token": csrf_token("b" * 43)}
        assert client.put(path, json=body, headers=foreign).status_code == 404
        assert (
            client.delete(
                path, params={"expected_revision": 0}, headers=foreign
            ).status_code
            == 404
        )
        client.cookies.set("job_hunter_session", TOKEN)
        changed = {
            **data,
            **(
                {"name": "Rust"}
                if data["kind"] == "skill"
                else {"description": "Manual private edit"}
            ),
        }
        result = client.put(
            path,
            json={"expected_revision": revision, "fact": changed},
            headers=HEADERS,
        )
        assert result.status_code == 200 and result.json()["outcome"] == "updated"
        revision += 1
        assert result.json()["fact"]["data"].items() >= changed.items()
        assert (
            client.delete(
                path, params={"expected_revision": revision - 1}, headers=HEADERS
            ).status_code
            == 409
        )
        assert (
            client.delete(
                path, params={"expected_revision": revision}, headers=HEADERS
            ).json()["outcome"]
            == "deleted"
        )
        revision += 1
        assert client.get(path).status_code == 404
        assert client.get(BASE).json()["revision"] == revision
        invalid = {
            "expected_revision": revision,
            "fact": {"kind": "skill", "name": "private-canary\x00"},
        }
        response = client.post(FACTS, json=invalid, headers=HEADERS)
        assert (
            response.status_code == 422 and "canary" not in response.text + caplog.text
        )
        assert (
            client.post(
                FACTS,
                json={**body, "owner_id": str(owners[1])},
                headers=HEADERS,
            ).status_code
            == 422
        )
        with isolated_database.transaction() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.id == owners[0])
                .values(email_verified_at=None)
            )
        assert client.get(BASE).status_code == 403
        assert client.post(FACTS, json=invalid, headers=HEADERS).status_code == 403
        app.state.profiles = None
        with isolated_database.transaction() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.id == owners[0])
                .values(email_verified_at=NOW)
            )
        assert client.get(BASE).status_code == 503


@pytest.mark.integration
def test_upserts_preserve_manual_values_and_never_merge_lookalikes(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
) -> None:
    store = PostgresProfiles(isolated_database, lambda: NOW)
    owner, other = owners
    original = Experience(company=" ACME ", title="Engineer", description="Manual text")
    created = store.put(owner, 0, original)
    assert created.fact is not None
    duplicate = store.put(
        owner,
        1,
        Experience(company="acme", title="ENGINEER", description="Manual text"),
    )
    assert (
        duplicate.outcome == "existing"
        and duplicate.fact == created.fact
        and duplicate.revision == 1
    )
    with pytest.raises(DomainError, match="conflict"):
        store.put(
            owner,
            1,
            Experience(company="acme", title="Engineer", description="Different text"),
        )
    lookalike = store.put(
        owner, 1, Experience(company="Acme", title="Software Engineer")
    )
    assert lookalike.fact is not None and lookalike.fact.id != created.fact.id
    with pytest.raises(DomainError, match="conflict"):
        store.put(owner, 2, original, lookalike.fact.id)
    with pytest.raises(DomainError, match="conflict"):
        store.put(owner, 2, Skill(name="Engineer"), created.fact.id)
    assert store.put(other, 0, original).outcome == "created"
    store.now = lambda: NOW + timedelta(minutes=1)
    changed = store.put(
        owner,
        2,
        Experience(company="Acme", title="Engineer", description=None),
        created.fact.id,
    )
    assert (
        changed.fact is not None
        and changed.fact.created_at == NOW
        and changed.fact.updated_at > NOW
    )
    assert changed.revision == 3 and store.read(owner).revision == 3
    assert len(store.read(owner).facts) == 2 and len(store.read(other).facts) == 1


@pytest.mark.integration
def test_simultaneous_first_upserts_are_fenced_then_idempotent_on_retry(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
) -> None:
    database = Database(
        Settings(
            database_url=SecretStr(
                isolated_database.engine.url.render_as_string(hide_password=False)
            ),
            database_pool_size=2,
        )
    )
    store = PostgresProfiles(database, lambda: NOW)
    gate = Barrier(2)

    def write() -> str:
        gate.wait(timeout=5)
        try:
            return store.put(owners[0], 0, Skill(name="Python")).outcome
        except DomainError as error:
            return error.code

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: write(), range(2)))
        assert sorted(results) == ["conflict", "created"]
        assert len(store.read(owners[0]).facts) == 1
        assert store.put(owners[0], 1, Skill(name=" PYTHON ")).outcome == "existing"
        assert store.read(owners[0]).revision == 1
    finally:
        database.close()


@pytest.mark.integration
@pytest.mark.parametrize("operation", ["create", "edit", "delete"])
def test_failure_after_flush_rolls_back_facts_and_revision(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = PostgresProfiles(isolated_database, lambda: NOW)
    initial = store.put(owners[0], 0, Skill(name="Python"))
    assert initial.fact is not None
    before = store.read(owners[0])
    flush = Session.flush

    def fail(session: Session, objects: object = None) -> None:
        changed = bool(session.new or session.dirty or session.deleted)
        flush(session)
        if changed:
            raise RuntimeError("Injected transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(Session, "flush", fail)
        with pytest.raises(RuntimeError, match="Injected"):
            if operation == "delete":
                store.delete(owners[0], 1, initial.fact.id)
            else:
                store.put(
                    owners[0],
                    1,
                    Skill(name="Rust"),
                    initial.fact.id if operation == "edit" else None,
                )
    assert store.read(owners[0]) == before


@pytest.mark.integration
def test_database_enforces_uniqueness_and_foreign_keys(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
) -> None:
    store = PostgresProfiles(isolated_database, lambda: NOW)
    created = store.put(owners[0], 0, Skill(name="Python"))
    assert created.fact is not None
    for owner in (owners[0], uuid4()):
        with pytest.raises(IntegrityError):
            with isolated_database.transaction() as session:
                session.add(
                    ProfileFactRecord(
                        id=uuid4(),
                        owner_id=owner,
                        kind="skill",
                        exact_key=exact_key(Skill(name="Python")),
                        data={"kind": "skill", "name": "Python"},
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
                session.flush()
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ProfileFactRecord)) == 1
    for invalid in [{}, {"kind": None}, {"kind": "project"}]:
        with pytest.raises(IntegrityError) as error:
            with isolated_database.transaction() as session:
                session.execute(update(ProfileFactRecord).values(data=invalid))
        assert "ck_profile_fact_data" in str(error.value)
