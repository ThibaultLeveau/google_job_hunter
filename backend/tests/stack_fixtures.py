import json
import os
import random
import socket
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import UUID

import httpx2 as httpx
import uvicorn
from http_fixture import FixtureHTTP, http_response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import make_url
from test_discovery import Provider, page
from test_extraction import TEXT, golden
from test_oidc import OidcTransport

from job_hunter import worker
from job_hunter.app import create_app
from job_hunter.application.ai import Reply
from job_hunter.application.search_provider import (
    SearchPage,
    SearchRequest,
)
from job_hunter.application.worker import Worker
from job_hunter.domain.searches import SearchDefinition, compile_search
from job_hunter.infrastructure import ai, match_scheduling, pipeline
from job_hunter.infrastructure.fetching import HttpFetcher
from job_hunter.infrastructure.oidc import (
    ENDPOINTS,
    GoogleProvider,
    load_google_settings,
)
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.provider_budgets import BudgetedSearch

Scenario = Literal["success", "duplicate", "transient", "owner-isolation"]
JOB_URL = "https://jobs.example.org/fixture-job"
JOB_TEXT = "Python engineer\n\nSynthetic Company\n\nRemote\n\nPython required"
PASSWORD = "Synthetic-only passphrase 2026!"
EMAILS = ("alice@example.org", "bob@example.org")
SEARCH = SearchDefinition(
    groups=[["Python", "Go"], ["remote"]],
    country="fr",
    language="en",
    sites=["jobs.example.org"],
)


class StackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["test"]
    root: Path
    database_url: str = Field(repr=False)
    api_url: str
    frontend_url: str
    scenario: Scenario = "success"
    mail_key: str = Field(repr=False)

    def environment(self) -> dict[str, str]:
        values = {
            "DATABASE_URL": self.database_url,
            "DOCUMENT_ROOT": str(self.root / "documents"),
            "ALLOWED_ORIGINS": json.dumps([self.api_url, self.frontend_url]),
            "TRUSTED_HOSTS": '["localhost", "127.0.0.1"]',
            "SEARCH_ALLOWED_DOMAINS": '["jobs.example.org"]',
            "AI_PROVIDER": "fixture",
            "AI_ENVIRONMENT": "test",
            "SEARCH_PROVIDER": "fixture",
            "SEARCH_ENVIRONMENT": "test",
            "MAIL_KEY": self.mail_key,
            "MAIL_SENDER": "accounts@example.org",
            "MAIL_PUBLIC_URL": self.frontend_url,
            "MAIL_SMTP_HOST": "fixture.invalid",
            "GOOGLE_CLIENT_ID": "fixture-client",
            "GOOGLE_CLIENT_SECRET": "fixture-secret",
            "GOOGLE_REDIRECT_URI": self.api_url + "/api/auth/google/callback",
        }
        return {
            **{k: v for k, v in os.environ.items() if not k.startswith("JOB_HUNTER_")},
            **{f"JOB_HUNTER_{k}": v for k, v in values.items()},
        }


def write_private(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with os.fdopen(
        os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
    ) as file:
        json.dump(value, file)
    temporary.replace(path)


def deny_outbound(database_url: str) -> None:
    database = make_url(database_url)
    allowed = (database.host, database.port or 5432)

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.connect" and args[1] != allowed:
            raise RuntimeError("Unexpected harness outbound connection")
        if event == "socket.getaddrinfo" and (args[0], args[1]) != allowed:
            raise RuntimeError("Unexpected harness DNS request")

    sys.addaudithook(audit)


class SearchFixture(Provider):
    def __init__(self, config: StackConfig) -> None:
        duplicate = config.scenario == "duplicate"
        super().__init__(
            (
                page((JOB_URL,), following=1 if duplicate else None),
                page((JOB_URL + "?utm_source=duplicate",), index=1),
            )
        )

    def fetch(self, request: SearchRequest) -> SearchPage:
        configured = (
            compile_search(value, ("jobs.example.org",))
            for value in (SearchDefinition(groups=[["Python"]]), SEARCH)
        )
        if not any(
            request.query == value.query
            and request.parameters == value.provider_parameters
            for value in configured
        ) or request.page not in (0, 1):
            raise AssertionError("Unconfigured synthetic search")
        return super().fetch(request)


class TransientResponses(dict[str, list[bytes]]):
    failed = False

    def __getitem__(self, url: str) -> list[bytes]:
        if url == JOB_URL and not self.failed:
            self.failed = True
            return http_response(status=503)
        return super().__getitem__(url)


class MailFixture:
    def __init__(self, config: StackConfig) -> None:
        self.config = config

    def send(self, recipient: str, link: str, message_id: UUID) -> None:
        if not recipient.endswith("@example.org") or not link.startswith(
            self.config.frontend_url + "/"
        ):
            raise AssertionError("Unconfigured synthetic mail")
        write_private(
            self.config.root / "mail" / f"{message_id}.json",
            {"recipient": recipient, "link": link},
        )


class GoogleFixture(OidcTransport):
    def __init__(self, config: StackConfig) -> None:
        super().__init__(datetime.now(UTC))
        self.config = config

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == ENDPOINTS["token_endpoint"]:
            code = parse_qs(request.content.decode())["code"][0]
            identity = UUID(code)
            recorded = json.loads(
                (self.config.root / "oidc" / f"{identity}.json").read_text()
            )
            _, generated = self.authorize(recorded["url"])
            self.flows[code] = self.flows.pop(generated)
            self.now = datetime.now(UTC)
            self.claims = {"auth_time": int(self.now.timestamp())}
        return super().handle(request)


def check_source(source: str, expected: str) -> None:
    parts = json.loads(json.loads(source)["untrusted_source"])
    if isinstance(parts, dict):
        parts = parts["source"]
    if not parts or any(part["text"] not in expected for part in parts):
        raise AssertionError("Unconfigured synthetic AI source")


class ChatFixture(pipeline.JobFixture):
    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        if set(schema["properties"]) not in (
            {"ordered", "boilerplate"},
            {"selection"},
            {"items"},
        ):
            raise AssertionError("Unconfigured synthetic job schema")
        check_source(source, JOB_TEXT)
        parts = json.loads(json.loads(source)["untrusted_source"])
        if isinstance(parts, dict):
            parts = parts["source"]

        def citation(quote: str) -> dict[str, object]:
            part = next(part for part in parts if quote in part["text"])
            start = part["start"] + part["text"].index(quote)
            return {
                "paragraph_id": part["id"],
                "start": start,
                "end": start + len(quote),
                "quote": quote,
            }

        if "selection" in schema["properties"]:
            return Reply(
                {
                    "selection": {
                        "kind": "job",
                        "evidence": [citation("Python engineer")],
                        "salaries": [],
                        "facts": [
                            {
                                "field": field,
                                "value": value,
                                "uncertain": False,
                                "evidence": citation(value),
                            }
                            for field, value in (
                                ("title", "Python engineer"),
                                ("employer", "Synthetic Company"),
                                ("work_arrangement", "Remote"),
                            )
                        ],
                    }
                },
                0,
                0,
            )
        if "items" in schema["properties"]:
            return Reply(
                {
                    "items": [
                        {
                            "kind": "job_requirement",
                            "level": "required",
                            "text": "Python required",
                            "evidence": [citation("Python required")],
                        }
                    ]
                },
                0,
                0,
            )
        return await super().generate(prompt, source, schema)


class MatchFixture(match_scheduling.UnknownMatch):
    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        data = json.loads(json.loads(source)["untrusted_source"])
        facts = data["profile_facts"]
        if any(
            fact.get("kind") != "skill" or fact.get("skill") not in ("Python", "Go")
            for fact in facts
        ):
            raise AssertionError("Unconfigured synthetic profile match")
        supported = [fact["id"] for fact in facts if fact["skill"] == "Python"]
        result = await super().generate(prompt, source, schema)
        assert isinstance(result.data, dict)
        for row, requirement in zip(
            result.data["items"], data["requirements"], strict=True
        ):
            if requirement["text"] != "Python required":
                raise AssertionError("Unconfigured synthetic requirement")
            row.update(
                status="matched" if supported else "missing",
                profile_evidence=supported,
                rationale="An accepted Python skill supports the explicit requirement."
                if supported
                else "No accepted Python skill is present; this is an evidence gap.",
            )
        return result


class ProfileAndEmbeddingFixture(ai.FixtureProvider):
    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        if set(schema["properties"]) != {"candidates"}:
            raise AssertionError("Unconfigured synthetic profile schema")
        check_source(source, TEXT)
        return await super().generate(prompt, source, schema)

    async def embed(self, texts: tuple[str, ...]) -> Reply:
        if texts != (JOB_TEXT,):
            try:
                facts = json.loads(texts[0]) if len(texts) == 1 else None
                if (
                    not isinstance(facts, list)
                    or not facts
                    or len(facts) > 2
                    or any(
                        not isinstance(fact, dict)
                        or fact.get("kind") != "skill"
                        or fact.get("skill") not in ("Python", "Go")
                        for fact in facts
                    )
                ):
                    raise ValueError()
            except ValueError, IndexError:
                raise AssertionError("Unconfigured synthetic embeddings") from None
        return await super().embed(texts)


def serve(config: StackConfig, role: str, descriptor: int | None = None) -> None:
    if (
        os.environ.get("JOB_HUNTER_AI_ENVIRONMENT") != "test"
        or os.environ.get("JOB_HUNTER_SEARCH_ENVIRONMENT") != "test"
    ):
        raise ValueError("Harness composition requires explicit test settings")
    deny_outbound(config.database_url)
    search = BudgetedSearch(SearchFixture(config))
    if role == "api":
        app = create_app()
        app.state.search_provider = search
        google = load_google_settings()
        assert google is not None and descriptor is not None
        app.state.google_sign_in.provider = GoogleProvider(
            google, GoogleFixture(config).transport
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app, access_log=False, proxy_headers=False, log_level="warning"
            )
        )
        server.run(sockets=[socket.socket(fileno=descriptor)])
        return
    if role != "worker":
        raise ValueError("Unknown harness role")
    fixture = FixtureHTTP(
        {
            JOB_URL: http_response(
                JOB_TEXT.encode(), headers=((b"Content-Type", b"text/plain"),)
            ),
            "https://jobs.example.org/robots.txt": http_response(
                b"User-agent: *\nAllow: /"
            ),
        }
    )
    if config.scenario == "transient":
        fixture.responses = TransientResponses(fixture.responses)

    async def resolve(host: str, timeout: float) -> tuple[str, ...]:
        if host != "jobs.example.org":
            raise AssertionError("Unconfigured synthetic crawl host")
        return await fixture.resolve(host, timeout)

    class ReadyWorker(Worker):
        def run(self) -> None:
            write_private(config.root / "worker-ready.json", {"ready": True})
            super().run()

    response = golden(
        parse(BytesIO(TEXT.encode()), "text/plain", ParseLimits())
    ).model_dump_json()
    with (
        patch.object(ai, "FixtureProvider", ProfileAndEmbeddingFixture),
        patch.object(pipeline, "JobFixture", ChatFixture),
        patch.object(match_scheduling, "UnknownMatch", MatchFixture),
        patch.object(worker, "create_search_provider", lambda _: search),
        patch.object(
            worker,
            "HttpFetcher",
            lambda settings, now: HttpFetcher(settings, resolve, fixture, now),
        ),
        patch.object(worker, "SmtpMail", lambda *_: MailFixture(config)),
        patch.object(worker, "Worker", ReadyWorker),
        patch.object(random, "random", lambda: 0.0),
        patch.object(
            sys,
            "argv",
            ["fixture-worker", "--concurrency", "1", "--profile-fixture", response],
        ),
    ):
        worker.main()


if __name__ == "__main__":
    configuration = StackConfig.model_validate_json(Path(sys.argv[1]).read_text())
    serve(configuration, sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else None)
