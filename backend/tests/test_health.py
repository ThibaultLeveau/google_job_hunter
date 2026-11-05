import os
import subprocess
import sys
import traceback

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from job_hunter.app import create_app
from job_hunter.settings import ConfigurationError, Settings, load_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in os.environ:
        if name.startswith("JOB_HUNTER_"):
            monkeypatch.delenv(name)


@pytest.fixture
def fake_settings() -> Settings:
    return Settings(docs_enabled=False)


def test_liveness_is_fixed_and_credential_free(fake_settings: Settings) -> None:
    with TestClient(create_app(fake_settings)) as client:
        response = client.get("/api/health/live")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_default_startup_requires_no_credentials() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/health/live").json() == {"status": "ok"}
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_app_creation_is_deterministic_and_isolated(fake_settings: Settings) -> None:
    first = create_app(fake_settings)
    second = create_app(fake_settings)

    assert first is not second
    assert first.openapi() == second.openapi()
    assert list(first.openapi()["paths"]) == ["/api/health/live"]
    first.dependency_overrides[load_settings] = lambda: Settings(docs_enabled=True)
    assert second.dependency_overrides == {}


def test_explicit_settings_do_not_read_environment(
    fake_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", "invalid-ambient-value")
    with TestClient(create_app(fake_settings)) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/docs").status_code == 404


@pytest.mark.parametrize("value", ["true", "1", "false", "0"])
def test_environment_controls_docs(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", value)
    with TestClient(create_app()) as client:
        expected_status = 200 if value in {"true", "1"} else 404
        assert client.get("/docs").status_code == expected_status
        assert client.get("/openapi.json").status_code == expected_status
        assert client.get("/api/health/live").json() == {"status": "ok"}


def test_settings_are_loaded_per_app(monkeypatch: pytest.MonkeyPatch) -> None:
    first = create_app()
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", "true")
    second = create_app()

    with TestClient(first) as client:
        assert client.get("/docs").status_code == 404
    with TestClient(second) as client:
        assert client.get("/docs").status_code == 200


def test_settings_are_immutable(fake_settings: Settings) -> None:
    with pytest.raises(ValidationError, match="Instance is frozen"):
        fake_settings.docs_enabled = True


@pytest.mark.parametrize("value", ["", "maybe", "private-credential-canary"])
def test_invalid_settings_fail_with_redacted_errors(
    value: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("JOB_HUNTER_DOCS_ENABLED", value)
    with pytest.raises(
        ConfigurationError, match="Invalid application settings"
    ) as raised:
        create_app()

    rendered = "".join(traceback.format_exception(raised.value))
    assert "docs_enabled" in str(raised.value)
    assert "JOB_HUNTER_" in str(raised.value)
    assert "input_value" not in rendered
    if value:
        assert value not in rendered + caplog.text


def test_server_startup_rejects_bad_settings_without_disclosing_input() -> None:
    canary = "private-startup-credential-canary"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "job_hunter.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        ],
        env={**os.environ, "JOB_HUNTER_DOCS_ENABLED": canary},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "Invalid application settings: docs_enabled" in result.stderr
    assert canary not in result.stdout + result.stderr
