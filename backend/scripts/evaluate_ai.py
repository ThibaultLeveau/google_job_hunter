import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from job_hunter.application.ai import AI, Accounting, AIError, Call, Record
from job_hunter.infrastructure.ai import AISettings, FixtureProvider, LangChainProvider
from scripts.evaluation_cases import dataset, fixture_reply
from scripts.evaluation_metrics import evaluate


@dataclass
class Budget:
    max_requests: int
    max_micro_usd: int
    input_rate: Decimal
    output_rate: Decimal
    requests: int = 0
    reserved_micro_usd: int = 0
    records: list[Record] = field(default_factory=list)

    def reserve(self, call: Call) -> None:
        charge = int(
            (
                call.input_bound * self.input_rate
                + call.output_bound * self.output_rate
            ).to_integral_value(rounding=ROUND_CEILING)
        )
        if (
            self.requests >= self.max_requests
            or self.reserved_micro_usd + charge > self.max_micro_usd
        ):
            raise AIError("quota")
        self.requests += 1
        self.reserved_micro_usd += charge

    def record(self, record: Record) -> None:
        self.records.append(record)


@dataclass
class AccountingPair:
    budget: Budget
    stage: Accounting

    def reserve(self, call: Call) -> None:
        self.stage.reserve(call)
        self.budget.reserve(call)

    def record(self, record: Record) -> None:
        self.budget.record(record)
        self.stage.record(record)


def run(settings: AISettings, budget: Budget) -> dict[str, object]:
    data, results = dataset(), []
    for case in data["cases"]:
        provider = (
            LangChainProvider(settings)
            if settings.provider == "openai"
            else FixtureProvider(settings, json.dumps(fixture_reply(case)))
        )

        def factory(
            stage: Accounting,
            provider: LangChainProvider | FixtureProvider = provider,
        ) -> AI:
            return AI(
                provider,
                provider,
                AccountingPair(budget, stage),
                settings.limits,
                settings.chat_model,
                settings.embedding_model,
            )

        results.append(evaluate(case, factory))
    return {
        "dataset_version": data["version"],
        "dataset_sha256": data["sha256"],
        "mode": "live" if settings.provider == "openai" else "fixture",
        "model": settings.chat_model,
        "passed": all(row["passed"] for row in results),
        "results": results,
        "calls": [asdict(record) for record in budget.records],
        "budget": {
            "requests": budget.requests,
            "max_requests": budget.max_requests,
            "reserved_micro_usd": budget.reserved_micro_usd,
            "max_micro_usd": budget.max_micro_usd,
            "input_usd_per_million": str(budget.input_rate),
            "output_usd_per_million": str(budget.output_rate),
        },
    }


def decimal_argument(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Use a decimal amount.") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic evidence regressions; offline by default."
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-cost-usd", type=decimal_argument)
    parser.add_argument("--input-usd-per-million", type=decimal_argument)
    parser.add_argument("--output-usd-per-million", type=decimal_argument)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("Choose a new report path.")
    if args.live:
        rates = (
            args.max_cost_usd,
            args.input_usd_per_million,
            args.output_usd_per_million,
        )
        if (
            not args.allow_paid
            or args.max_requests is None
            or not 1 <= args.max_requests <= 32
            or any(
                value is None or not value.is_finite() or value <= 0 for value in rates
            )
            or args.max_cost_usd > 5
        ):
            parser.error(
                "Live mode needs --allow-paid, 1–32 requests, a positive cost cap "
                "at most USD 5, and both current positive model rates."
            )
        settings = AISettings()
        if settings.provider != "openai":
            parser.error(
                "Live mode requires configured JOB_HUNTER_AI_ OpenAI settings."
            )
        budget = Budget(
            args.max_requests,
            int(args.max_cost_usd * 1000000),
            args.input_usd_per_million,
            args.output_usd_per_million,
        )
    else:
        if args.allow_paid or any(
            value is not None
            for value in (
                args.max_requests,
                args.max_cost_usd,
                args.input_usd_per_million,
                args.output_usd_per_million,
            )
        ):
            parser.error("Paid-call options require --live.")
        settings = AISettings(
            provider="fixture",
            environment="test",
            api_key=None,
            chat_model="synthetic-v1",
            embedding_model="unused",
        )
        budget = Budget(12, 0, Decimal(0), Decimal(0))
    result = run(settings, budget)
    path = (
        args.output
        or Path("reports/evaluations") / f"{result['mode']}-{uuid4().hex}.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as file:
        json.dump(result, file, ensure_ascii=False, indent=2, default=str)
    print(
        json.dumps(
            {"passed": result["passed"], "mode": result["mode"], "report": str(path)}
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
