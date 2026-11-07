import json
import socket
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from job_hunter.application.ai import AI, Accounting, Reply
from job_hunter.application.matching import aggregate
from job_hunter.infrastructure.ai import AISettings, FixtureProvider
from scripts import evaluate_ai as cli
from scripts import evaluation_metrics as metrics
from scripts.evaluation_cases import dataset, fixture_reply

SETTINGS = AISettings(
    provider="fixture",
    environment="test",
    chat_model="synthetic-v1",
    embedding_model="unused",
)
CASES = dataset()["cases"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object) -> None:
        raise AssertionError("Evaluation test attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def evaluate(case: dict[str, Any], response: object) -> dict[str, object]:
    provider = FixtureProvider(SETTINGS, json.dumps(response))

    def factory(accounting: Accounting) -> AI:
        return AI(
            provider, provider, accounting, SETTINGS.limits, "synthetic-v1", "unused"
        )

    return metrics.evaluate(case, factory)


def test_offline_report_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_AI_PROVIDER", "openai")
    monkeypatch.setenv("JOB_HUNTER_AI_API_KEY", "synthetic-secret-never-sent")
    output = tmp_path / "report.json"
    assert cli.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["passed"] and report["mode"] == "fixture"
    assert report["dataset_version"] == "synthetic-v1"
    assert len(report["dataset_sha256"]) == 64
    assert len(report["results"]) == 6 and report["budget"]["requests"] == 6
    assert report["budget"]["reserved_micro_usd"] == 0
    assert all(
        row["passed"] and all(value == 1 for value in row["metrics"].values())
        for row in report["results"]
    )
    for record in report["calls"]:
        assert record["call"]["model"] == "synthetic-v1"
        assert record["call"]["prompt_version"] and record["call"]["schema_version"]
        assert len(record["call"]["schema_hash"]) == 64
    assert "synthetic-secret" not in output.read_text()
    assert all(
        json.dumps(case["source"], ensure_ascii=False) not in output.read_text()
        for case in CASES
    )
    assert output.stat().st_mode & 0o777 == 0o600
    repeated = cli.run(SETTINGS, cli.Budget(12, 0, Decimal(0), Decimal(0)))
    assert repeated["results"] == report["results"]
    with pytest.raises(SystemExit):
        cli.main(["--output", str(output)])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_invalid_schema_fails_every_stage(case: dict[str, Any]) -> None:
    result = evaluate(case, {"tools": ["execute shell"]})
    assert not result["passed"]
    assert result["reason"] == "invalid_output"


def test_fabricated_evidence_missing_required_item_and_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume, requirements, matching = CASES[0], CASES[4], CASES[5]
    fabricated = fixture_reply(resume)
    fabricated["candidates"][0]["fields"][0]["evidence"][0]["quote"] = "Rust"
    assert not evaluate(resume, fabricated)["passed"]
    omitted = fixture_reply(requirements)
    omitted["items"] = [
        item for item in omitted["items"] if item["text"] != "Python is required"
    ]
    result = evaluate(requirements, omitted)
    assert not result["passed"]
    assert result["metrics"] == {
        "schema_validity": 1,
        "supported_evidence": 1,
        "labelled_requirements": 0,
        "required_item_preservation": 0.5,
        "deduplication": 0,
    }
    monkeypatch.setattr(
        metrics,
        "aggregate",
        lambda requirements, items: aggregate(requirements, items).model_copy(
            update={"percent": 100.0}
        ),
    )
    assert not evaluate(matching, fixture_reply(matching))["passed"]


def test_cleanup_and_private_evidence_and_rationale() -> None:
    cleaning, matching = CASES[1], CASES[5]
    omitted = fixture_reply(cleaning)
    omitted["ordered"].pop(1)
    assert not evaluate(cleaning, omitted)["passed"]
    response = fixture_reply(matching)
    response["items"][0]["profile_evidence"] = ["00000000-0000-0000-0000-000000000000"]
    assert not evaluate(matching, response)["passed"]
    response = fixture_reply(matching)
    response["items"][0]["profile_evidence"] = response["items"][1]["profile_evidence"]
    assert not evaluate(matching, response)["passed"]
    response = fixture_reply(matching)
    for row in response["items"]:
        row["rationale"] = "Different wording describing the same supplied evidence."
    assert evaluate(matching, response)["passed"]


def live_args(**changes: str) -> list[str]:
    values = {
        "max_requests": "1",
        "max_cost_usd": "1",
        "input_usd_per_million": "1",
        "output_usd_per_million": "2",
        **changes,
    }
    return [
        "--live",
        "--allow-paid",
        *[
            value
            for key, item in values.items()
            for value in ("--" + key.replace("_", "-"), item)
        ],
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["--live"],
        ["--allow-paid"],
        ["--max-requests", "1"],
        ["--live", "--max-requests", "1"],
        live_args(max_requests="33"),
        live_args(max_cost_usd="NaN"),
        live_args(max_cost_usd="invalid"),
        live_args(max_cost_usd="6"),
        live_args(input_usd_per_million="-1"),
        live_args(),
    ],
)
def test_live_requires_complete_explicit_opt_in(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(settings: AISettings) -> None:
        raise AssertionError("Live adapter constructed before opt-in")

    monkeypatch.setattr(cli, "LangChainProvider", forbidden)
    monkeypatch.setenv("JOB_HUNTER_AI_PROVIDER", "disabled")
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


@pytest.mark.parametrize(("cost", "expected_calls"), [("1", 1), ("0.000001", 0)])
def test_manual_live_caps_with_fake_adapter(
    cost: str, expected_calls: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "PROVIDER": "openai",
        "CHAT_MODEL": "synthetic-chat",
        "EMBEDDING_MODEL": "unused",
        "API_KEY": "synthetic-never-sent",
    }.items():
        monkeypatch.setenv("JOB_HUNTER_AI_" + name, value)
    calls = []

    class CountingFixture(FixtureProvider):
        async def generate(
            self, prompt: str, source: str, schema: dict[str, Any]
        ) -> Reply:
            calls.append(schema)
            return await super().generate(prompt, source, schema)

    provider = CountingFixture(SETTINGS, '{"invalid": true}')
    monkeypatch.setattr(cli, "LangChainProvider", lambda settings: provider)
    output = tmp_path / "live.json"
    assert cli.main(live_args(max_cost_usd=cost) + ["--output", str(output)]) == 1
    report = json.loads(output.read_text())
    assert len(calls) == report["budget"]["requests"] == expected_calls
    assert report["budget"]["reserved_micro_usd"] <= report["budget"]["max_micro_usd"]
    assert not report["passed"] and report["mode"] == "live"
    assert all(row["reason"] == "quota" for row in report["results"])
    if expected_calls:
        assert report["calls"][0]["error"] == "invalid_output"
        assert report["calls"][0]["call"]["attempt"] == 1
