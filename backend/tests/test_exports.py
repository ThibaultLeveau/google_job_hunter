from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from test_job_registry import owners as registry_owners
from test_job_registry import runs as registry_runs
from test_matches import setup as known_job
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_review import batch, seed
from test_snapshots import register

from job_hunter.app import create_app
from job_hunter.application.exports import ExportLimits, ExportTooLarge, PersonalExport
from job_hunter.application.reauth import ReauthenticationRequired
from job_hunter.application.sessions import csrf_token, token_hash
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.profiles import Skill
from job_hunter.domain.review import ReviewItem
from job_hunter.infrastructure.accounts import SessionRecord, UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    PostgresDocuments,
    StorageSettings,
)
from job_hunter.infrastructure.exports import PostgresExports
from job_hunter.infrastructure.matches import MatchCallRecord
from job_hunter.infrastructure.profiles import (
    PostgresProfiles,
    ProfileRecord,
)
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.settings import Settings

pytestmark = pytest.mark.integration
owners, runs = registry_owners, registry_runs
URL = "/api/v1/account/export"


def application(database: Database, limits: ExportLimits | None = None) -> FastAPI:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    app.state.sessions.now = app.state.reauthentication.now = lambda: NOW
    app.state.exports = PostgresExports(database, lambda: NOW, 300, limits)
    return app


@pytest.fixture
def client(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> Iterator[TestClient]:
    with TestClient(application(isolated_database), base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        yield client


def test_empty_export_schema_headers_and_no_profile_creation(
    client: TestClient, isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    response = client.post(URL, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="google-job-hunter-export-v1.json"'
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.json() == {
        "format": "google-job-hunter",
        "version": 1,
        "exported_at": "2026-01-01T00:00:00Z",
        "owner_id": str(owners[0]),
        "profile": {"id": str(owners[0]), "revision": 0, "facts": []},
        "documents": [],
        "saved_searches": [],
        "runs": [],
        "discoveries": [],
        "matches": [],
    }
    assert client.post(URL, headers=HEADERS).content == response.content
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ProfileRecord)) == 0


def test_populated_export_isolates_retained_history_and_excludes_internal_records(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    runs: tuple[UUID, UUID],
    tmp_path: Path,
) -> None:
    db = isolated_database
    repository, job, _ = known_job(db, owners[0], runs[0])
    first = repository.run(owners[0], job, NOW)
    repository.run(owners[0], job, NOW, 2)
    register(db, owners[1], runs[1])
    profiles = PostgresProfiles(db, lambda: NOW)
    profiles.put(owners[1], 0, Skill(name="other-owner-canary"))
    repository.run(owners[1], job, NOW)
    document, ids = seed(db, owners[0], tmp_path)
    profiles.review(
        owners[0], batch(1, ReviewItem(proposal_id=ids["Python"], action="accept"))
    )
    other_document, _ = seed(db, owners[1], tmp_path)
    with db.transaction() as session:
        run = session.get(SearchRunRecord, runs[0])
        assert run
        run.discovery = {"error_code": "provider-private-canary"}
        session.add(
            MatchCallRecord(
                id=uuid4(),
                match_id=first.match_id,
                generation=1,
                payload={"raw": "provider-call-canary"},
            )
        )
        search = session.get(SavedSearchRecord, run.search_id)
        assert search
        search.deleted_at = NOW
        keys = [row.storage_key for row in session.scalars(select(DocumentRecord))]
    with TestClient(application(db), base_url=ORIGIN) as client:
        client.cookies.set("job_hunter_session", TOKEN)
        response = client.post(URL, headers=HEADERS)
        assert response.status_code == 200, response.text
        exported = PersonalExport.model_validate_json(response.content)
        assert exported.owner_id == owners[0] and exported.profile.revision == 2
        assert exported.profile.facts == profiles.read(owners[0]).facts
        evidence = exported.profile.facts[0].source_associations
        assert evidence and evidence[0].document_id == document
        assert evidence[0].quote == "Python" and evidence[0].source_version == 1
        assert [doc.id for doc in exported.documents] == [document]
        assert exported.saved_searches[0].deleted_at == NOW
        assert [run.id for run in exported.runs] == [runs[0]]
        assert exported.runs[0].definition.name == "Private query"
        assert [(item.run_id, item.job_id) for item in exported.discoveries] == [
            (runs[0], job)
        ]
        assert [item.generation for item in exported.matches] == [1, 2]
        assert all(
            item.id == first.match_id and item.result.owner == owners[0]
            for item in exported.matches
        )
        assert exported.matches[0].result.evaluations[0].rationale
        assert exported.matches[0].profile_facts[0].skill == "Python"
        for secret in [
            str(owners[1]),
            str(runs[1]),
            str(other_document),
            "other-owner-canary",
            "provider-private-canary",
            "provider-call-canary",
            TOKEN,
            *keys,
        ]:
            assert secret not in response.text
        for field in [
            "password_hash",
            "token_hash",
            "idempotency_key",
            "storage_key",
            "provider_parameters",
            "discovery",
            "accounting",
        ]:
            assert f'"{field}":' not in response.text
        # Count nested evidence and historical professional facts as records too.
        total = 9 + len(evidence)
        assert (
            PostgresExports(db, lambda: NOW, 300, ExportLimits(records=total)).read(
                owners[0], token_hash(TOKEN)
            )
            == response.content
        )
        for limit in [1, total - 1]:
            with pytest.raises(ExportTooLarge):
                PostgresExports(db, lambda: NOW, 300, ExportLimits(records=limit)).read(
                    owners[0], token_hash(TOKEN)
                )
        PostgresDocuments(db, StorageSettings(), lambda: NOW).delete(
            owners[0], document
        )
        deleted = PersonalExport.model_validate_json(
            client.post(URL, headers=HEADERS).content
        )
        assert deleted.documents[0].deleted_at == NOW
        assert deleted.profile.facts[0].unavailable_sources == (document,)
        assert deleted.profile.facts[0].source_associations == ()
        assert deleted.matches == exported.matches
        client.cookies.set("job_hunter_session", "b" * 43)
        headers = {**HEADERS, "X-CSRF-Token": csrf_token("b" * 43)}
        with db.transaction() as session:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == owners[1])
                .values(authenticated_at=NOW - timedelta(seconds=299))
            )
        other = client.post(URL, headers=headers)
        assert (
            other.json()["owner_id"] == str(owners[1])
            and str(document) not in other.text
        )


@pytest.mark.parametrize(
    "case",
    [
        "anonymous",
        "csrf",
        "origin",
        "old",
        "future",
        "revoked",
        "expired",
        "owner_body",
        "owner_query",
    ],
)
def test_api_rejects_untrusted_initiation(
    client: TestClient,
    owners: tuple[UUID, UUID],
    isolated_database: Database,
    case: str,
) -> None:
    headers, body, url, expected = dict(HEADERS), None, URL, 403
    if case == "anonymous":
        client.cookies.clear()
        expected = 401
    elif case == "csrf":
        headers.pop("X-CSRF-Token")
    elif case == "origin":
        headers["Origin"] = "https://evil.example.org"
    elif case.startswith("owner_"):
        expected = 422
        if case == "owner_body":
            body = {"owner_id": str(owners[1])}
        else:
            url += "?owner_id=" + str(owners[1])
    else:
        changes = {
            "old": {"authenticated_at": NOW - timedelta(seconds=300)},
            "future": {"authenticated_at": NOW + timedelta(seconds=1)},
            "revoked": {"revoked_at": NOW},
            "expired": {
                "expires_at": NOW,
                "authenticated_at": NOW - timedelta(seconds=1),
            },
        }
        with isolated_database.transaction() as session:
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == owners[0])
                .values(**changes[case])
            )
        if case in {"revoked", "expired"}:
            expected = 401
    response = client.post(url, headers=headers, json=body)
    assert response.status_code == expected
    assert "content-disposition" not in response.headers
    if case in {"old", "future"}:
        assert response.json()["action"] == "export_data"
        assert response.json()["code"] == "reauthentication_required"


@pytest.mark.parametrize("case", ["foreign_session", "deleted", "revoked", "stale"])
def test_snapshot_rechecks_session_and_recent_proof(
    isolated_database: Database, owners: tuple[UUID, UUID], case: str
) -> None:
    digest = token_hash("b" * 43 if case == "foreign_session" else TOKEN)
    with isolated_database.transaction() as session:
        if case == "deleted":
            session.execute(
                update(UserRecord)
                .where(UserRecord.id == owners[0])
                .values(deleted_at=NOW)
            )
        elif case in {"revoked", "stale"}:
            changes = (
                {"revoked_at": NOW}
                if case == "revoked"
                else {"authenticated_at": NOW - timedelta(seconds=300)}
            )
            session.execute(
                update(SessionRecord)
                .where(SessionRecord.user_id == owners[0])
                .values(**changes)
            )
    with pytest.raises(
        ReauthenticationRequired if case == "stale" else Unauthenticated
    ):
        PostgresExports(isolated_database, lambda: NOW, 300).read(owners[0], digest)


def test_limits_fail_before_any_attachment_and_count_utf8_bytes(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    profiles = PostgresProfiles(db, lambda: NOW)
    profiles.put(owners[0], 0, Skill(name="Compétence 日本語"))
    normal = PostgresExports(db, lambda: NOW, 300).read(owners[0], token_hash(TOKEN))
    assert len(normal) > len(normal.decode())
    assert (
        PostgresExports(db, lambda: NOW, 300, ExportLimits(bytes=len(normal))).read(
            owners[0], token_hash(TOKEN)
        )
        == normal
    )
    for limits in [
        ExportLimits(records=0),
        ExportLimits(bytes=1),
        ExportLimits(bytes=len(normal) - 1),
    ]:
        with TestClient(application(db, limits), base_url=ORIGIN) as client:
            client.cookies.set("job_hunter_session", TOKEN)
            response = client.post(URL, headers=HEADERS)
            assert response.status_code == 413
            assert response.json()["code"] == "export_too_large"
            assert "content-disposition" not in response.headers
            assert "Compétence" not in response.text


def test_export_snapshot_does_not_mix_concurrent_profile_edits(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    db = isolated_database
    fact = (
        PostgresProfiles(db, lambda: NOW).put(owners[0], 0, Skill(name="Before")).fact
    )
    assert fact
    other = Database(Settings())
    changed = False

    def edit_after_revision(*arguments: object) -> None:
        nonlocal changed
        if not changed and str(arguments[2]).startswith("SELECT profiles.revision"):
            changed = True
            PostgresProfiles(other, lambda: NOW).put(
                owners[0], 1, Skill(name="After"), fact.id
            )

    event.listen(db.engine, "after_cursor_execute", edit_after_revision)
    try:
        exported = PersonalExport.model_validate_json(
            PostgresExports(db, lambda: NOW, 300).read(owners[0], token_hash(TOKEN))
        )
        assert changed and exported.profile.revision == 1
        assert exported.profile.facts[0].data == Skill(name="Before")
        assert PostgresProfiles(other, lambda: NOW).read(owners[0]).revision == 2
    finally:
        event.remove(db.engine, "after_cursor_execute", edit_after_revision)
        other.close()


def test_unavailable_service_and_export_openapi(client: TestClient) -> None:
    app = cast(FastAPI, client.app)
    app.state.exports = None
    assert client.post(URL, headers=HEADERS).status_code == 503
    operation = app.openapi()["paths"][URL]["post"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/PersonalExport")
