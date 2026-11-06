from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, Event
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from test_profiles import HEADERS, NOW, ORIGIN, TOKEN
from test_profiles import owners as profile_owners

from job_hunter.app import create_app
from job_hunter.application.searches import SearchWrite
from job_hunter.application.sessions import Sessions, csrf_token
from job_hunter.domain.errors import DomainError
from job_hunter.domain.searches import SearchDefinition
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskKey
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.searches import (
    DISCOVERY,
    PostgresSearches,
    SearchRunRecord,
)
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.settings import Settings

owners = profile_owners
BASE = "/api/v1/searches"
DATA = {
    "name": " Python jobs ",
    "definition": {"groups": [["Ｐｙｔｈｏｎ"]], "country": "fr"},
}


def test_run_history_is_newest_first_with_stable_ties(
    isolated_database: Database, owners: tuple[UUID, UUID]
) -> None:
    service = PostgresSearches(isolated_database, lambda: NOW, ())
    saved = service.save(owners[0], SearchWrite.model_validate(DATA))
    first = service.start(owners[0], saved.id, 1, uuid4())
    service.now = lambda: NOW + timedelta(seconds=1)
    recent = [service.start(owners[0], saved.id, 1, uuid4()) for _ in range(2)]
    expected = [run.id for run in sorted(recent, key=lambda run: run.id, reverse=True)]
    expected.append(first.id)
    assert [service.runs(owners[0], 1, n).items[0].id for n in range(3)] == expected
    assert service.runs(owners[0], 1, 0).next_offset == 1
    assert service.runs(owners[0], 1, 2).next_offset is None
    assert service.runs(owners[1], 20, 0).items == ()


def test_owned_crud_snapshots_replay_and_verification(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
) -> None:
    app = create_app(Settings(allowed_origins=(ORIGIN,)))
    cast(Sessions, app.state.sessions).now = lambda: NOW
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get(BASE).status_code == 401
        client.cookies.set("job_hunter_session", TOKEN)
        assert client.post(BASE, json=DATA).status_code == 403
        client.headers.update(HEADERS)
        for invalid in (
            {**DATA, "name": " "},
            {**DATA, "name": "x\x00"},
            {**DATA, "owner_id": str(owners[1])},
            {**DATA, "definition": {"groups": []}},
            {**DATA, "definition": {"groups": [["x"]], "sites": ["evil.test"]}},
        ):
            assert client.post(BASE, json=invalid).status_code == 422
        created = client.post(BASE, json=DATA)
        assert created.status_code == 201
        saved = created.json()
        assert saved["revision"] == 1 and saved["name"] == "Python jobs"
        assert saved["definition"]["groups"] == [["Python"]]
        path = BASE + "/" + saved["id"]
        assert client.get(path).json() == saved
        other = client.post(BASE, json={**DATA, "name": "Other"}).json()
        pages = [
            client.get(BASE, params={"limit": 1, "offset": n}).json() for n in (0, 1)
        ]
        assert {p["items"][0]["id"] for p in pages} == {saved["id"], other["id"]}
        assert [p["next_offset"] for p in pages] == [1, None]
        for params in ({"limit": 101}, {"offset": -1}):
            assert client.get(BASE, params=params).status_code == 422
        start = {"expected_revision": 1, "idempotency_key": str(uuid4())}
        response = client.post(path + "/runs", json=start)
        assert response.status_code == 202
        first = response.json()
        run_path = BASE + "/runs/" + first["id"]
        assert first["snapshot"]["query"] == '("Python")'
        assert first["snapshot"]["provider_parameters"] == {"gl": "fr"}
        assert first["status"] == "pending" and set(first["counters"].values()) == {0}
        edit = {
            "name": "Go jobs",
            "definition": {"groups": [["Go"]]},
            "expected_revision": 1,
        }
        assert client.put(path, json=edit).json()["revision"] == 2
        assert client.put(path, json=edit).status_code == 409
        assert client.delete(path, params={"expected_revision": 1}).status_code == 409
        assert client.get(run_path).json() == first
        assert client.post(path + "/runs", json=start).json() == first
        for wrong in (
            {**start, "expected_revision": 2},
            {**start, "idempotency_key": str(uuid4())},
        ):
            assert client.post(path + "/runs", json=wrong).status_code == 409
        assert (
            client.post(BASE + "/" + other["id"] + "/runs", json=start).status_code
            == 409
        )
        newer = client.post(
            path + "/runs",
            json={"expected_revision": 2, "idempotency_key": str(uuid4())},
        ).json()
        assert newer["snapshot"]["query"] == '("Go")'
        assert (
            client.get(BASE + "/runs", params={"limit": 1}).json()["next_offset"] == 1
        )
        assert (
            len(client.get(BASE + "/runs", params={"offset": 1}).json()["items"]) == 1
        )
        client.cookies.set("job_hunter_session", "b" * 43)
        client.headers.update({"X-CSRF-Token": csrf_token("b" * 43)})
        assert (
            client.get(BASE).json()["items"]
            == client.get(BASE + "/runs").json()["items"]
            == []
        )
        for endpoint in (path, run_path):
            assert client.get(endpoint).status_code == 404
        assert client.put(path, json=edit).status_code == 404
        assert client.delete(path, params={"expected_revision": 2}).status_code == 404
        assert client.post(run_path + "/cancel").status_code == 404
        assert client.post(path + "/runs", json=start).status_code == 404
        owned = client.post(BASE, json=DATA).json()
        own_path = BASE + "/" + owned["id"] + "/runs"
        assert client.post(own_path, json=start).json()["id"] != first["id"]
        with isolated_database.transaction() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.id == owners[1])
                .values(email_verified_at=None)
            )
        assert client.post(own_path, json=start).status_code == 403
        client.cookies.set("job_hunter_session", TOKEN)
        client.headers.update(HEADERS)
        assert client.delete(path, params={"expected_revision": 2}).status_code == 204
        assert client.get(path).status_code == 404
        assert client.post(path + "/runs", json=start).json() == first
        assert (
            client.post(
                path + "/runs",
                json={**start, "idempotency_key": str(uuid4())},
            ).status_code
            == 404
        )
        cancelled = client.post(run_path + "/cancel").json()
        assert (
            cancelled["status"] == "cancelled"
            and cancelled["snapshot"] == first["snapshot"]
        )
        assert client.post(run_path + "/cancel").json() == cancelled
        app.state.searches = None
        assert client.get(BASE).status_code == 503


def test_atomic_rollback_constraints_and_immutable_inputs(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PostgresSearches(isolated_database, lambda: NOW, ("jobs.lever.co",))
    data = SearchWrite(
        name="Jobs",
        definition=SearchDefinition(groups=[["x"]], sites=["jobs.lever.co"]),
    )
    saved = store.save(owners[0], data)
    original = PostgresTaskQueue.enqueue

    def fail(queue: PostgresTaskQueue, task: NewTask, now: datetime) -> Task:
        original(queue, task, now)
        raise RuntimeError("rollback fixture")

    with monkeypatch.context() as patch:
        patch.setattr(PostgresTaskQueue, "enqueue", fail)
        with pytest.raises(RuntimeError):
            store.start(owners[0], saved.id, 1, uuid4())
    with isolated_database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(SearchRunRecord)) == 0
        assert session.scalar(select(func.count()).select_from(TaskRecord)) == 0
    run = store.start(owners[0], saved.id, 1, uuid4())
    invalid_updates: tuple[dict[str, object], ...] = (
        {"snapshot": {}},
        {"search_revision": 2},
        {"processing_ready": 1},
        {"new_urls": 1},
        {"found_results": -1},
        {"status": "unknown"},
    )
    for values in invalid_updates:
        with pytest.raises(IntegrityError), isolated_database.transaction() as session:
            session.execute(
                update(SearchRunRecord)
                .where(SearchRunRecord.id == run.id)
                .values(**values)
            )
    with isolated_database.transaction() as session:
        row = session.get(SearchRunRecord, run.id)
        assert row is not None
        values = {
            column.name: getattr(row, column.name)
            for column in SearchRunRecord.__table__.columns
        }
    for owner in owners:
        with pytest.raises(IntegrityError), isolated_database.transaction() as session:
            session.execute(
                insert(SearchRunRecord).values(
                    {**values, "id": uuid4(), "owner_id": owner}
                )
            )
    store.allowed_domains = ()
    assert store.start(owners[0], saved.id, 1, run.idempotency_key) == run
    with pytest.raises(DomainError, match="conflict"):
        store.start(owners[0], saved.id, 1, uuid4())
    with pytest.raises(DomainError, match="not_found"):
        store.save(
            uuid4(), SearchWrite(name="x", definition=SearchDefinition(groups=[["x"]]))
        )


@pytest.mark.parametrize("publication_first", [False, True])
def test_concurrent_start_and_cancel_fence_personal_work(
    isolated_database: Database,
    owners: tuple[UUID, UUID],
    publication_first: bool,
) -> None:
    database = Database(Settings(database_pool_size=2))
    try:
        store = PostgresSearches(database, lambda: NOW, ())
        saved = store.save(
            owners[0],
            SearchWrite(name="Jobs", definition=SearchDefinition(groups=[["x"]])),
        )
        barrier, key = Barrier(2), uuid4()

        def start() -> UUID:
            barrier.wait(timeout=5)
            return store.start(owners[0], saved.id, 1, key).id

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(start) for _ in range(2)]
            ids = [future.result(timeout=5) for future in futures]
        assert ids[0] == ids[1]
        with database.transaction() as session:
            queue = PostgresTaskQueue(session)
            task = queue.claim((DISCOVERY,), NOW, timedelta(seconds=60))
            assert task is not None
            shared = queue.enqueue(
                NewTask(TaskKey("shared_fixture", uuid4(), 1), uuid4(), NOW), NOW
            )
            assert (
                session.scalar(select(func.count()).select_from(SearchRunRecord)) == 1
            )
        locked, release = Event(), Event()

        def checkpoint() -> datetime:
            locked.set()
            assert release.wait(timeout=5)
            return NOW

        def publish() -> str:
            try:
                with database.transaction() as session:
                    PostgresTaskQueue(session).complete(task, NOW)
                    row = session.scalars(
                        select(SearchRunRecord)
                        .where(SearchRunRecord.id == ids[0])
                        .with_for_update()
                    ).one()
                    assert row.status == "pending"
                    row.status, row.found_results, row.skipped_results = (
                        "completed",
                        1,
                        1,
                    )
                    if publication_first:
                        checkpoint()
                return "completed"
            except StaleLease:
                return "cancelled"

        if not publication_first:
            store.now = checkpoint
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(
                publish
                if publication_first
                else lambda: store.cancel(owners[0], ids[0]).status
            )
            assert locked.wait(timeout=5)
            second = (
                pool.submit(lambda: store.cancel(owners[0], ids[0]).status)
                if publication_first
                else pool.submit(publish)
            )
            release.set()
            expected = "completed" if publication_first else "cancelled"
            assert first.result(timeout=5) == second.result(timeout=5) == expected
        run = store.run(owners[0], ids[0])
        assert run.status == expected and run.counters.found_results == int(
            publication_first
        )
        with database.transaction() as session:
            assert (
                session.scalars(
                    select(TaskRecord.status).where(TaskRecord.id == shared.id)
                ).one()
                == "pending"
            )
    finally:
        database.close()
