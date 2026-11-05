import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from job_hunter.infrastructure.database import Database

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_migration_heads.py"


@pytest.mark.parametrize("head_count", [0, 1, 2])
def test_head_gate_rejects_empty_or_divergent_history(
    head_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versions = tmp_path / "migrations/versions"
    versions.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.alembic]\nscript_location = "%(here)s/migrations"\n', encoding="utf-8"
    )
    for number in range(head_count):
        (versions / f"{number}.py").write_text(
            f'revision = "r{number}"\ndown_revision = None\n', encoding="utf-8"
        )
    monkeypatch.chdir(tmp_path)
    if head_count == 1:
        runpy.run_path(str(SCRIPT))
        assert "Migration head: r0" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit, match=f"found {head_count}"):
            runpy.run_path(str(SCRIPT))


def test_ci_cannot_silently_skip_missing_database() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--require-database", "--collect-only"],
        env={
            key: value
            for key, value in os.environ.items()
            if key != "TEST_DATABASE_URL"
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 4
    assert "--require-database needs TEST_DATABASE_URL" in result.stderr


@pytest.mark.integration
def test_database_is_dropped_after_a_failing_test(
    isolated_database: Database, tmp_path: Path
) -> None:
    (tmp_path / "conftest.py").write_text(
        Path(__file__).with_name("conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "test_failure.py").write_text(
        "from pathlib import Path\n"
        "from sqlalchemy import text\n"
        "def test_failure(empty_database):\n"
        '    Path("database-name").write_text(empty_database.engine.url.database)\n'
        "    with empty_database.transaction() as session:\n"
        '        session.execute(text("CREATE TABLE probe (id integer PRIMARY KEY)"))\n'
        '    raise AssertionError("intentional cleanup probe")\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--require-database", "-q", "test_failure.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 1
    assert "intentional cleanup probe" in result.stdout
    name = (tmp_path / "database-name").read_text(encoding="utf-8")
    with isolated_database.transaction() as session:
        assert (
            session.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :name"),
                {"name": name},
            )
            == 0
        )
