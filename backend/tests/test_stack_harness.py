import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from stack_fixtures import (
    JOB_TEXT,
    ChatFixture,
    ProfileAndEmbeddingFixture,
    SearchFixture,
    StackConfig,
)
from stack_harness import BACKEND, Stack, authorize, control

from job_hunter.app import create_app
from job_hunter.application.search_provider import SearchRequest
from job_hunter.infrastructure.ai import AISettings
from job_hunter.infrastructure.embeddings import EmbeddingRecord
from job_hunter.infrastructure.jobs import JobSourceRecord
from job_hunter.infrastructure.searches import SearchRunRecord
from job_hunter.infrastructure.tasks import TaskRecord


def wait_for[T](read: Callable[[], T], ready: Callable[[T], bool]) -> T:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        result = read()
        if ready(result):
            return result
        time.sleep(0.05)
    raise AssertionError("Synthetic stack did not reach the expected state")


def login(client: httpx.Client, email: str = "alice@example.org") -> None:
    client.headers.pop("Cookie", None)
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": "Synthetic-only passphrase 2026!"},
    )
    assert response.status_code == 200, response.text
    client.headers.update(
        {
            "Cookie": "job_hunter_session=" + response.cookies["job_hunter_session"],
            "X-CSRF-Token": response.json()["csrf_token"],
        }
    )


@pytest.mark.integration
def test_real_discovery_scenarios_mail_oidc_and_reset() -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL for the real stack")
    with Stack(mode="test") as stack:
        with httpx.Client(
            base_url=stack.config.api_url,
            headers={"Origin": stack.config.api_url},
            trust_env=False,
        ) as client:
            for scenario in ("success", "duplicate", "transient", "owner-isolation"):
                if scenario != "success":
                    stack.reset(scenario)
                login(client)
                saved = client.post(
                    "/api/v1/searches",
                    json={"name": "Synthetic", "definition": {"groups": [["Python"]]}},
                )
                assert saved.status_code == 201
                run = client.post(
                    f"/api/v1/searches/{saved.json()['id']}/runs",
                    json={"expected_revision": 1, "idempotency_key": str(uuid4())},
                )
                assert run.status_code == 202

                def artifacts() -> int:
                    with stack.db.transaction() as session:
                        return (
                            session.scalar(
                                select(func.count()).select_from(EmbeddingRecord)
                            )
                            or 0
                        )

                wait_for(artifacts, lambda count: count == 1)
                with stack.db.transaction() as session:
                    result = session.scalars(select(SearchRunRecord)).one()
                    assert result.status == "completed"
                    assert result.found_results == (2 if scenario == "duplicate" else 1)
                    assert result.known_urls == (1 if scenario == "duplicate" else 0)
                    job = session.scalars(select(JobSourceRecord)).one()
                    job_id = job.id
                    tasks = session.scalars(
                        select(TaskRecord).where(TaskRecord.kind == "search_discovery")
                    ).all()
                    assert sum(task.attempts for task in tasks) == (
                        2 if scenario == "duplicate" else 1
                    )
                    assert session.scalar(select(func.max(TaskRecord.attempts))) == (
                        2 if scenario == "transient" else 1
                    )
                    for table in (
                        "job_snapshots",
                        "job_cleanings",
                        "job_metadata",
                        "job_requirements",
                        "job_chunks",
                    ):
                        assert (
                            session.scalar(text(f"SELECT count(*) FROM {table}")) == 1
                        )
                assert client.get(f"/api/v1/jobs/{job_id}").status_code == 200
                login(client, "bob@example.org")
                assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404
                assert (
                    client.get(f"/api/v1/searches/runs/{run.json()['id']}").status_code
                    == 404
                )
            client.headers.pop("Cookie")
            values = {
                "email": "new@example.org",
                "password": "Synthetic-only passphrase 2026!",
            }
            assert client.post("/api/auth/register", json=values).status_code == 202
            paths = wait_for(lambda: list((stack.root / "mail").glob("*.json")), bool)
            message = json.loads(paths[0].read_text())
            assert message["recipient"] == values["email"]
            assert paths[0].stat().st_mode & 0o777 == 0o600
            login(client, values["email"])
            token = urlsplit(message["link"]).fragment.removeprefix("token=")
            assert (
                client.post("/api/auth/verify", json={"token": token}).status_code
                == 202
            )
            assert client.get("/api/auth/me").json()["email_verified"]
            client.headers.pop("Cookie")
            flow = client.get("/api/auth/google/start")
            assert flow.status_code == 302
            callback = authorize(stack.root, flow.headers["location"])
            response = client.get(
                callback,
                headers={
                    "Cookie": "job_hunter_oidc=" + flow.cookies["job_hunter_oidc"]
                },
            )
            assert response.headers["location"].endswith("google=signed_in")
            assert all(
                "fixture" not in route for route in create_app().openapi()["paths"]
            )
            root, name = stack.root, stack.name
    assert not root.exists()
    with stack.admin.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                {"name": name},
            )
            == 0
        )
    stack.admin.dispose()


@pytest.mark.integration
def test_isolation_and_cleanup_after_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL for the real stack")
    first, second = Stack(mode="test"), Stack(mode="test")
    with pytest.raises(RuntimeError, match="test failure"):
        with first, second:
            assert first.name != second.name and first.root != second.root
            with first.db.transaction() as session:
                session.execute(
                    text(
                        "UPDATE users SET email='changed@example.org' "
                        "WHERE email='alice@example.org'"
                    )
                )
            with second.db.transaction() as session:
                assert (
                    session.scalar(
                        text(
                            "SELECT count(*) FROM users "
                            "WHERE email='changed@example.org'"
                        )
                    )
                    == 0
                )
            children = first.children + second.children
            raise RuntimeError("test failure")
    assert all(child.poll() is not None for child in children)
    assert not first.root.exists() and not second.root.exists()
    failed = Stack(mode="test")
    monkeypatch.setattr(
        failed, "launch", lambda: (_ for _ in ()).throw(RuntimeError("startup"))
    )
    with pytest.raises(RuntimeError, match="startup"):
        failed.start()
    assert not failed.root.exists() and not failed.created


@pytest.mark.integration
def test_cli_lifecycle(tmp_path: Path) -> None:
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL for the real stack")
    state = tmp_path / "stack.json"
    command = [
        sys.executable,
        "tests/stack_harness.py",
        "start",
        "--mode",
        "test",
        "--state",
        str(state),
    ]
    with (tmp_path / "output").open("wb") as log:
        process = subprocess.Popen(command, cwd=BACKEND, stdout=log, stderr=log)
    try:
        wait_for(state.exists, bool)
        original = json.loads(state.read_text())
        assert state.stat().st_mode & 0o777 == 0o600
        assert control(state, "seed", "success")["users"] == original["users"]
        owner = UUID(original["users"][0]["id"])
        assert "Développeur" in control(state, "resume", "success")["text"]
        assert control(state, "dedup", "success", owner) == {
            "documents": [],
            "parsed": [],
            "extractions": [],
            "proposals": [],
            "facts": [],
            "stored_files": 0,
        }
        for action, identity in (
            ("expire", uuid4()),
            ("dedup", None),
            ("jobs", uuid4()),
            ("unknown", owner),
        ):
            with pytest.raises(RuntimeError, match="control failed"):
                control(state, action, "success", identity)
        with (
            httpx.Client(
                base_url=original["api_url"],
                headers={"Origin": original["api_url"]},
                trust_env=False,
            ) as alice,
            httpx.Client(
                base_url=original["api_url"],
                headers={"Origin": original["api_url"]},
                trust_env=False,
            ) as bob,
        ):
            login(alice)
            login(bob, "bob@example.org")
            assert control(state, "pause-worker", "success") == {"paused": True}
            assert alice.get("/api/health/ready").status_code == 200
            empty = control(state, "jobs", "success", owner)
            assert all(not rows for rows in empty["public"].values())
            assert empty["tasks"] == empty["matches"] == []
            assert control(state, "resume-worker", "success") == {"paused": False}
            assert control(state, "expire", "success", owner) == {"expired": True}
            assert alice.get("/api/auth/me").status_code == 401
            assert bob.get("/api/auth/me").status_code == 200
        assert control(state, "reset", "duplicate")["scenario"] == "duplicate"
        assert control(state, "stop", "success") == {"stopped": True}
        assert process.wait(timeout=10) == 0
        assert not Path(original["root"]).exists()
        assert control(state, "stop", "success") == {"stopped": True}
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)


def test_production_missing_services_and_unexpected_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="explicit test"):
        Stack(mode="production")
    monkeypatch.setenv("JOB_HUNTER_AI_ENVIRONMENT", "production")
    with pytest.raises(ValueError, match="explicit test"):
        Stack(mode="test")
    monkeypatch.delenv("JOB_HUNTER_AI_ENVIRONMENT")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="local PostgreSQL"):
        Stack(mode="test")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from stack_fixtures import deny_outbound; import socket; "
            "deny_outbound('postgresql://localhost/db'); "
            "socket.create_connection(('unexpected.invalid',443))",
        ],
        cwd=BACKEND / "tests",
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "Unexpected harness DNS" in result.stderr
    config = StackConfig(
        mode="test",
        root=tmp_path,
        database_url="unused",
        api_url="http://localhost",
        frontend_url="http://localhost",
        mail_key="unused",
    )
    with pytest.raises(AssertionError, match="synthetic search"):
        SearchFixture(config).fetch(
            SearchRequest(query="unexpected", parameters={}, page=0)
        )
    settings = AISettings(provider="fixture", environment="test")
    with pytest.raises(AssertionError, match="embeddings"):
        asyncio.run(ProfileAndEmbeddingFixture(settings, "{}").embed(("unexpected",)))
    with pytest.raises(AssertionError, match="schema"):
        asyncio.run(
            ChatFixture(settings, "{}").generate(
                "", JOB_TEXT, {"properties": {"unexpected": {}}}
            )
        )
