import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet

from job_hunter.infrastructure.database import Database
from job_hunter.settings import ConfigurationError, Settings, load_settings
from scripts import runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "JOB_HUNTER_DATABASE_URL", "postgresql+psycopg://u:p@localhost/db"
    )
    monkeypatch.setenv("JOB_HUNTER_DOCUMENT_ROOT", str(tmp_path))
    monkeypatch.setenv("JOB_HUNTER_ALLOWED_ORIGINS", '["https://app.example.org"]')
    monkeypatch.setenv("JOB_HUNTER_TRUSTED_HOSTS", '["app.example.org","127.0.0.1"]')


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("DATABASE_URL", None, "DATABASE_URL"),
        ("DOCUMENT_ROOT", None, "DOCUMENT_ROOT"),
        ("MAIL_KEY", None, "MAIL_"),
        ("MAIL_KEY", "private-canary", "MAIL_"),
        ("AI_PROVIDER", "fixture", "AI settings"),
        ("SEARCH_PROVIDER", "fixture", "search settings"),
        ("AI_ENVIRONMENT", "test", "reject development/test"),
        ("SEARCH_ENVIRONMENT", "development", "reject development/test"),
        ("AI_PROVIDER", "openai", "AI settings"),
        ("SEARCH_PROVIDER", "serpapi", "SEARCH_API_KEY"),
        ("ALLOWED_ORIGINS", "[]", "HTTPS origins"),
        ("ALLOWED_ORIGINS", '["http://app.example.org"]', "HTTPS origins"),
        ("TRUSTED_HOSTS", '["app.example.org"]', "trusted hosts"),
    ],
)
def test_invalid_startup_is_redacted(
    production: None,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    value: str | None,
    message: str,
) -> None:
    if value is None:
        monkeypatch.delenv("JOB_HUNTER_" + key, raising=False)
    else:
        monkeypatch.setenv("JOB_HUNTER_" + key, value)
    with pytest.raises(ConfigurationError, match=message) as error:
        runtime.production_settings()
    assert "private-canary" not in str(error.value)


def test_storage_must_be_private(production: None, tmp_path: Path) -> None:
    assert runtime.production_settings().database_url is not None
    tmp_path.chmod(0o755)
    with pytest.raises(ConfigurationError, match="private directory"):
        runtime.production_settings()


@pytest.mark.integration
def test_schema_gate_before_and_after_migration(empty_database: Database) -> None:
    settings = load_settings()
    with pytest.raises(ConfigurationError, match="run migrate once"):
        runtime.schema_ready(settings)
    command.upgrade(Config(toml_file=ROOT / "backend/pyproject.toml"), "head")
    runtime.schema_ready(settings)


@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_commands_exec_for_signal_delivery(
    production: None,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    calls: list[tuple[str, list[str]]] = []
    checked: list[Settings] = []
    monkeypatch.setattr(runtime, "schema_ready", checked.append)
    monkeypatch.setattr(os, "execv", lambda exe, args: calls.append((exe, args)))
    assert runtime.main([role]) == 0
    assert calls == [(sys.executable, [sys.executable, "-m", *runtime.COMMANDS[role]])]
    assert len(checked) == (0 if role == "migrate" else 1)


def test_health_and_safe_failures(
    production: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime, "schema_ready", lambda _: None)
    original = httpx.Client
    status = 200
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(status)),
            **kwargs,
        ),
    )
    assert runtime.main(["api-health"]) == 0
    status = 503
    assert runtime.main(["api-health"]) == 1
    monkeypatch.setattr(
        Path, "read_bytes", lambda _: b"python\0-m\0job_hunter.worker\0"
    )
    assert runtime.main(["worker-health"]) == 0
    monkeypatch.setattr(Path, "read_bytes", lambda _: b"unrelated\0")
    assert runtime.main(["worker-health"]) == 1
    monkeypatch.delenv("JOB_HUNTER_DATABASE_URL")
    assert runtime.main(["migrate"]) == 1
    assert "Runtime configuration" in capsys.readouterr().err


@pytest.mark.skipif(
    os.environ.get("RUN_CONTAINER_TESTS") != "1", reason="Explicit Docker smoke"
)
def test_container_runtime_and_fixture_pipeline(tmp_path: Path) -> None:
    project = "job-hunter-runtime-test-" + uuid4().hex[:12]
    canary = "runtime-private-canary-" + uuid4().hex
    envfile = tmp_path / ".env.runtime"
    envfile.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=" + canary,
                f"JOB_HUNTER_DATABASE_URL=postgresql+psycopg://job_hunter:{canary}@database:5432/job_hunter",
                'JOB_HUNTER_TRUSTED_HOSTS=["app.example.org","127.0.0.1","localhost"]',
                'JOB_HUNTER_ALLOWED_ORIGINS=["https://app.example.org"]',
                "JOB_HUNTER_MAIL_PUBLIC_URL=https://app.example.org",
                "JOB_HUNTER_MAIL_SENDER=accounts@example.org",
                "JOB_HUNTER_MAIL_SMTP_HOST=smtp.invalid",
                "JOB_HUNTER_MAIL_KEY=" + Fernet.generate_key().decode(),
                "JOB_HUNTER_AI_PROVIDER=disabled",
                "JOB_HUNTER_SEARCH_PROVIDER=disabled",
            ]
        )
        + "\n"
    )
    envfile.chmod(0o600)
    env = {**os.environ, "RUNTIME_ENV_FILE": str(envfile), "RUNTIME_PORT": "0"}
    compose = [
        "docker",
        "compose",
        "-p",
        project,
        "--env-file",
        str(envfile),
        "-f",
        "compose.runtime.yaml",
    ]

    def run(args: list[str], *, ok: bool = True, timeout: int = 120) -> str:
        result = subprocess.run(
            args, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        if ok and result.returncode:
            pytest.fail(output.replace(canary, "[redacted]"))
        if not ok:
            assert result.returncode != 0
        return output

    public = ROOT / "frontend/public"
    created_public = not public.exists()
    public.mkdir(exist_ok=True)
    probe = public / (".env.runtime-probe-" + uuid4().hex)
    probe.write_text(canary)
    try:
        run([*compose, "build", "api", "web"], timeout=600)
        run(
            [
                "docker",
                "build",
                "--target",
                "fixture-test",
                "-t",
                project + "-fixture",
                ".",
            ],
            timeout=300,
        )
        for target in ("backend", "web"):
            image = project + "-" + target
            assert canary not in run(["docker", "history", "--no-trunc", image])
            config = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"]
            assert config["User"] not in ("", "root", "0")
            assert not any("JOB_HUNTER_" in item for item in config["Env"])
        output = run(
            ["docker", "run", "--rm", "--network", "none", project + "-backend", "api"],
            ok=False,
        )
        assert "DATABASE_URL is required" in output
        output = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--env-file",
                str(envfile),
                "-e",
                "JOB_HUNTER_AI_PROVIDER=fixture",
                project + "-backend",
                "api",
            ],
            ok=False,
        )
        assert "Invalid AI settings" in output and canary not in output
        run([*compose, "up", "-d", "--wait", "database"])
        output = run([*compose, "run", "--rm", "api"], ok=False)
        assert "run migrate once" in output
        run([*compose, "run", "--rm", "migrate"])
        run(
            [
                *compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "90",
                "api",
                "worker",
                "web",
            ]
        )
        address = run([*compose, "port", "web", "8080"]).strip()
        with httpx.Client(base_url="http://" + address, trust_env=False) as client:
            for path in ("/", "/profile", "/api/health/live", "/api/health/ready"):
                assert client.get(path).status_code == 200
            assert client.get("/api/auth/me").status_code == 401
            assert client.get("/" + probe.name).text != canary
        assets = run(
            [
                *compose,
                "exec",
                "-T",
                "web",
                "sh",
                "-c",
                "find /usr/share/nginx/html -type f -exec cat {} +",
            ]
        )
        assert canary not in assets
        for service in ("api", "worker"):
            run(
                [
                    *compose,
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    "import os; from pathlib import Path; "
                    "p=Path('/private/documents'); "
                    "assert os.getuid()==10001 and p.stat().st_mode & 0o777 == 0o700",
                ]
            )
        run(
            [
                *compose,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; "
                "Path('/private/documents/probe').write_text('persistent')",
            ]
        )
        run([*compose, "restart", "api", "worker"])
        run(
            [
                *compose,
                "exec",
                "-T",
                "worker",
                "python",
                "-c",
                "from pathlib import Path; "
                "assert Path('/private/documents/probe').read_text() == 'persistent'",
            ]
        )
        database_id = run([*compose, "ps", "-q", "database"]).strip()
        run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "container:" + database_id,
                "--name",
                project + "-fixture",
                "-e",
                f"TEST_DATABASE_URL=postgresql+psycopg://job_hunter:{canary}@127.0.0.1:5432/job_hunter",
                project + "-fixture",
            ],
            timeout=360,
        )
        run([*compose, "stop", "api", "worker", "web"])
        for service in ("api", "worker", "web"):
            identity = run([*compose, "ps", "-a", "-q", service]).strip()
            state = json.loads(run(["docker", "inspect", identity]))[0]["State"]
            assert state["ExitCode"] == 0 and not state["OOMKilled"]
    finally:
        subprocess.run(
            ["docker", "rm", "-f", project + "-fixture"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        run([*compose, "down", "--volumes", "--remove-orphans"], timeout=180)
        probe.unlink(missing_ok=True)
        if created_public:
            public.rmdir()
        subprocess.run(
            [
                "docker",
                "image",
                "rm",
                *[project + "-" + name for name in ("backend", "web", "fixture")],
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
