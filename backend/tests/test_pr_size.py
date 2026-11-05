import runpy
import subprocess
from pathlib import Path

import pytest

from scripts.check_pr_size import Change, branch_changes, parse_numstat, report

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_size.py"


def test_fixture_totals_and_lockfile_scope(capsys: pytest.CaptureFixture[str]) -> None:
    changes = parse_numstat(
        b"5\t0\tadded.py\0"
        b"0\t7\tdeleted.py\0"
        b"2\t1\t\0old.py\0new.py\0"
        b"2000\t3000\tbackend/uv.lock\0"
        b"4000\t5000\tfrontend/package-lock.json\0"
        b"13\t4\tfrontend/src/generated/api.ts\0"
        b"8\t3\tcustom.lock\0"
        b"6\t2\t\0authored.py\0backend/uv.lock\0"
    )
    assert report(changes) == 0
    output = capsys.readouterr().out
    assert "Files: 8 / 10" in output
    assert "Raw: +6034/-8017 = 14051" in output
    assert "Dependency locks: +6000/-8000 = 14000" in output
    assert "Authored: +34/-17 = 51 / 1000" in output
    assert changes[2].paths == ("old.py", "new.py")
    assert not changes[-1].lockfile
    assert parse_numstat(b"") == []


@pytest.mark.parametrize(
    "data",
    [
        b"1\t0\tpath",
        b"bad\0",
        b"-1\t0\tp\0",
        b"1\tx\tp\0",
        b"1\t0\t\0old\0",
        b"1\t0\t\0\0new\0",
    ],
)
def test_invalid_diff_fails_closed(data: bytes) -> None:
    with pytest.raises(ValueError):
        parse_numstat(data)


@pytest.mark.parametrize(
    ("files", "lines", "failures"),
    [
        (10, 1000, []),
        (11, 1000, ["File cap"]),
        (10, 1001, ["Authored-line cap"]),
        (11, 1001, ["File cap", "Authored-line cap"]),
    ],
)
def test_caps(
    files: int, lines: int, failures: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    changes = [Change(lines - 1, 1, ("generated.ts",))]
    changes += [Change(0, 0, (f"old{i}", f"new{i}")) for i in range(files - 1)]
    assert report(changes) == bool(failures)
    output = capsys.readouterr().out
    assert output.count("FAIL:") == len(failures)
    assert all(failure in output for failure in failures)


def test_filenames_are_data(capsys: pytest.CaptureFixture[str]) -> None:
    path = b"--bad\t$(touch pwned)`id`\n::error::forged\xff"
    changes = parse_numstat(b"1\t2\t" + path + b"\0")
    assert report(changes) == 0
    output = capsys.readouterr().out
    assert "\\n::error::forged" in output
    assert "\\udcff" in output
    assert not any(line.startswith("::") for line in output.splitlines())


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    run_git("init", "-b", "main")
    run_git("config", "user.name", "Size Test")
    run_git("config", "user.email", "size@example.invalid")
    (tmp_path / "removed.py").write_text("removed\n", encoding="utf-8")
    (tmp_path / "old.py").write_text("renamed\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Base")
    return tmp_path


def run_git(*arguments: str) -> None:
    subprocess.run(["git", *arguments], check=True, capture_output=True)


def run_cli(base: str, head: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr("sys.argv", [str(SCRIPT), base, head])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    assert isinstance(raised.value.code, int)
    return raised.value.code


def test_real_merge_base_and_malicious_filename(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_git("switch", "-c", "feature")
    run_git("mv", "old.py", "renamed.py")
    run_git("rm", "removed.py")
    name = "$(touch pwned)\t\n::error::forged"
    (repository / name).write_text("new\n", encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Small feature")
    run_git("switch", "main")
    (repository / "main-only.py").write_text("main\n" * 2000, encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Independent main change")
    assert run_cli("main", "feature", monkeypatch) == 0
    output = capsys.readouterr().out
    assert "Files: 3 / 10" in output
    assert "Raw: +1/-1 = 2" in output
    assert "main-only.py" not in output
    assert '"old.py", "renamed.py"' in output
    assert not (repository / "pwned").exists()


@pytest.mark.parametrize(
    ("attributes", "exit_code", "message"),
    [
        ("generated.ts -diff", 2, "Uncountable diff"),
        ("generated.ts diff=generated", 1, "Authored-line cap exceeded: 1001 > 1000"),
    ],
)
def test_future_oversized_branch_fails(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    attributes: str,
    exit_code: int,
    message: str,
) -> None:
    run_git("config", "diff.external", "false")
    run_git("config", "diff.generated.textconv", "false")
    (repository / ".git/info/attributes").write_text(
        attributes + "\n", encoding="utf-8"
    )
    run_git("switch", "-c", "oversized")
    (repository / "generated.ts").write_text("source\n" * 1001, encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "Oversized generated source")
    assert run_cli("main", "HEAD", monkeypatch) == exit_code
    assert message in capsys.readouterr().out


def test_invalid_ref_cannot_become_option(
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        branch_changes("--help", "HEAD")
    assert run_cli("missing-ref", "HEAD", monkeypatch) == 2
    assert "Could not measure diff" in capsys.readouterr().out
