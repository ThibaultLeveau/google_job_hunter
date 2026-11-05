import json
import runpy
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "check_coverage.py"


@pytest.mark.parametrize(
    ("lines", "line_total", "branches", "branch_total", "expected"),
    [
        (90, 100, 85, 100, 0),
        (89, 100, 100, 100, 1),
        (100, 100, 84, 100, 1),
        (8999, 10000, 100, 100, 1),
        (100, 100, 8499, 10000, 1),
        (100, 100, 0, 0, 0),
        (0, 100, 0, 100, 1),
        (0, 0, 0, 0, "Invalid line coverage counts."),
        (101, 100, 100, 100, "Invalid line coverage counts."),
        (100, 100, -1, 100, "Invalid branch coverage counts."),
        (True, 100, 100, 100, "Invalid line coverage counts."),
        (100, "100", 100, 100, "Invalid line coverage counts."),
    ],
)
def test_independent_coverage_floors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lines: int,
    line_total: int | str,
    branches: int,
    branch_total: int,
    expected: int | str,
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "meta": {"branch_coverage": True},
                "totals": {
                    "covered_lines": lines,
                    "num_statements": line_total,
                    "covered_branches": branches,
                    "num_branches": branch_total,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", [str(GATE), str(report)])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(GATE))
    assert raised.value.code == expected


def test_unmeasured_branches_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text('{"meta": {"branch_coverage": false}}', encoding="utf-8")
    monkeypatch.setattr("sys.argv", [str(GATE), str(report)])
    with pytest.raises(SystemExit, match="Branch coverage must be enabled"):
        runpy.run_path(str(GATE))
