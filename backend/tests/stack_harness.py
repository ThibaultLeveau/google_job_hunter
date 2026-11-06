import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import TracebackType
from typing import Any, Self
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text, update
from stack_fixtures import EMAILS, PASSWORD, Scenario, StackConfig, write_private
from test_extraction import TEXT

from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    AuthIdentityRecord,
    SessionRecord,
    UserRecord,
)
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    ExtractionRecord,
    ParsedDocumentRecord,
    ProposalRecord,
)
from job_hunter.infrastructure.profiles import ProfileFactRecord
from job_hunter.settings import Settings

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("stack_fixtures.py")


class Stack:
    def __init__(
        self,
        *,
        mode: str,
        scenario: Scenario = "success",
        port: int = 0,
        frontend_url: str = "http://127.0.0.1:5173",
    ) -> None:
        if mode != "test" or any(
            os.environ.get(name, "test") != "test"
            for name in ("JOB_HUNTER_AI_ENVIRONMENT", "JOB_HUNTER_SEARCH_ENVIRONMENT")
        ):
            raise ValueError(
                "Harness requires explicit test mode, never production settings"
            )
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise ValueError(
                "Start local PostgreSQL/pgvector and set TEST_DATABASE_URL"
            )
        Settings(database_url=SecretStr(url))
        self.admin = create_engine(
            url,
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
            connect_args={"connect_timeout": 2},
        )
        if self.admin.url.host not in ("localhost", "127.0.0.1", "::1"):
            self.admin.dispose()
            raise ValueError("Harness PostgreSQL must use a loopback host")
        self.name = "test_stack_" + uuid4().hex
        self.root = Path(tempfile.mkdtemp(prefix="job-hunter-stack-"))
        self.listener = socket.socket()
        self.children: list[subprocess.Popen[bytes]] = []
        self.created = False
        self.database: Database | None = None
        try:
            self.listener.bind(("127.0.0.1", port))
            self.config = StackConfig(
                mode="test",
                root=self.root,
                database_url=self.admin.url.set(database=self.name).render_as_string(
                    hide_password=False
                ),
                api_url=f"http://127.0.0.1:{self.listener.getsockname()[1]}",
                frontend_url=frontend_url,
                scenario=scenario,
                mail_key=Fernet.generate_key().decode(),
            )
            for name in ("documents", "mail", "oidc", "commands", "replies"):
                (self.root / name).mkdir(mode=0o700)
        except BaseException:
            self.stop()
            raise

    @property
    def db(self) -> Database:
        assert self.database is not None
        return self.database

    def start(self) -> Self:
        try:
            with self.admin.connect() as connection:
                connection.execute(text(f'CREATE DATABASE "{self.name}"'))
            self.created = True
            self.database = Database(
                Settings(database_url=SecretStr(self.config.database_url))
            )
            with (self.root / "migration.log").open("wb") as log:
                subprocess.run(
                    [sys.executable, "-m", "alembic", "upgrade", "head"],
                    cwd=BACKEND,
                    env=self.config.environment(),
                    stdout=log,
                    stderr=log,
                    timeout=30,
                    check=True,
                )
            self.seed()
            self.launch()
            return self
        except BaseException:
            self.stop()
            raise

    def seed(self) -> dict[str, object]:
        hashed, now = Argon2Passwords().hash(PASSWORD), datetime.now(UTC)
        with self.db.transaction() as session:
            for email in EMAILS:
                owner = uuid5(NAMESPACE_URL, email)
                if session.get(UserRecord, owner) is not None:
                    continue
                session.add(
                    UserRecord(
                        id=owner,
                        email=email,
                        email_normalized=email,
                        created_at=now,
                        email_verified_at=now,
                    )
                )
                session.flush()
                session.add(
                    AuthIdentityRecord(
                        id=uuid5(owner, "password"),
                        user_id=owner,
                        issuer="password",
                        subject=str(owner),
                        password_hash=hashed,
                        created_at=now,
                    )
                )
        return self.info()

    def info(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "api_url": self.config.api_url,
            "scenario": self.config.scenario,
            "users": [
                {
                    "id": str(uuid5(NAMESPACE_URL, email)),
                    "email": email,
                    "password": PASSWORD,
                }
                for email in EMAILS
            ],
        }

    def launch(self) -> None:
        path = self.root / "config.json"
        write_private(path, self.config.model_dump(mode="json"))
        (self.root / "worker-ready.json").unlink(missing_ok=True)
        for role in ("api", "worker"):
            descriptor = self.listener.fileno()
            args = [sys.executable, str(FIXTURES), str(path), role]
            if role == "api":
                args.append(str(descriptor))
            with (self.root / f"{role}.log").open("ab") as log:
                self.children.append(
                    subprocess.Popen(
                        args,
                        cwd=BACKEND,
                        env=self.config.environment(),
                        stdout=log,
                        stderr=log,
                        pass_fds=(descriptor,) if role == "api" else (),
                    )
                )
        deadline = time.monotonic() + 40
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                if any(child.poll() is not None for child in self.children):
                    raise RuntimeError("Harness API or worker exited during startup")
                try:
                    if (
                        client.get(
                            self.config.api_url + "/api/health/ready"
                        ).status_code
                        == 200
                        and (self.root / "worker-ready.json").exists()
                    ):
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise RuntimeError("Harness health deadline exceeded")

    def browser_control(self, action: str, owner: UUID | None) -> dict[str, object]:
        if action == "resume":
            return {"text": TEXT}
        if owner not in {uuid5(NAMESPACE_URL, email) for email in EMAILS}:
            raise ValueError("Select a seeded synthetic owner")
        with self.db.transaction() as session:
            if action == "expire":
                now = datetime.now(UTC)
                session.execute(
                    update(SessionRecord)
                    .where(SessionRecord.user_id == owner)
                    .values(
                        authenticated_at=now - timedelta(days=2),
                        expires_at=now - timedelta(days=1),
                    )
                )
                return {"expired": True}
            if action != "dedup":
                raise ValueError("Unknown browser control")
            documents = select(DocumentRecord.id).where(
                DocumentRecord.owner_id == owner
            )
            parsed = select(ParsedDocumentRecord.id).where(
                ParsedDocumentRecord.document_id.in_(documents)
            )
            extractions = select(ExtractionRecord.id).where(
                ExtractionRecord.parsed_id.in_(parsed)
            )
            statements = {
                "documents": documents,
                "parsed": parsed,
                "extractions": extractions,
                "proposals": select(ProposalRecord.id).where(
                    ProposalRecord.extraction_id.in_(extractions)
                ),
                "facts": select(ProfileFactRecord.id).where(
                    ProfileFactRecord.owner_id == owner
                ),
            }
            result: dict[str, object] = {
                name: sorted(str(identity) for identity in session.scalars(statement))
                for name, statement in statements.items()
            }
            keys = session.scalars(
                select(DocumentRecord.storage_key).where(
                    DocumentRecord.owner_id == owner
                )
            )
            result["stored_files"] = sum(
                (self.root / "documents" / (key + ".blob")).is_file() for key in keys
            )
            return result

    def halt(self) -> None:
        for child in self.children:
            if child.poll() is None:
                child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        self.children.clear()

    def reset(self, scenario: Scenario) -> dict[str, object]:
        self.halt()
        self.config = StackConfig.model_validate(
            {**self.config.model_dump(), "scenario": scenario}
        )
        with self.db.transaction() as session:
            tables = ", ".join(
                f'"{table.name}"' for table in Base.metadata.sorted_tables
            )
            session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        for name in ("documents", "mail", "oidc"):
            shutil.rmtree(self.root / name)
            (self.root / name).mkdir(mode=0o700)
        result = self.seed()
        self.launch()
        return result

    def stop(self) -> None:
        try:
            self.halt()
            self.listener.close()
            if self.database is not None:
                self.database.close()
            if self.created:
                with self.admin.connect() as connection:
                    connection.execute(
                        text(f'DROP DATABASE "{self.name}" WITH (FORCE)')
                    )
                self.created = False
        finally:
            self.admin.dispose()
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def authorize(root: Path, url: str) -> str:
    parsed = urlsplit(url)
    if (
        len(url) > 8192
        or parsed.scheme != "https"
        or parsed.netloc != "accounts.google.com"
    ):
        raise ValueError("Expected the synthetic Google authorization URL")
    state = parse_qs(parsed.query)["state"][0]
    code = str(uuid4())
    config = StackConfig.model_validate_json((root / "config.json").read_text())
    write_private(root / "oidc" / f"{code}.json", {"url": url})
    return (
        config.api_url
        + "/api/auth/google/callback?"
        + urlencode({"state": state, "code": code})
    )


def control(
    state_file: Path, action: str, scenario: Scenario, owner: UUID | None = None
) -> dict[str, Any]:
    if action == "stop" and not state_file.exists():
        return {"stopped": True}
    root = Path(json.loads(state_file.read_text())["root"])
    identity = uuid4().hex
    write_private(
        root / "commands" / f"{identity}.json",
        {
            "action": action,
            "scenario": scenario,
            "owner": str(owner) if owner else None,
        },
    )
    reply = root / "replies" / f"{identity}.json"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if action == "stop" and not state_file.exists():
            return {"stopped": True}
        if not state_file.exists():
            raise RuntimeError("Harness stopped before completing the command")
        if reply.exists():
            result: dict[str, Any] = json.loads(reply.read_text())
            reply.unlink()
            if "error" in result:
                raise RuntimeError("Harness control failed")
            return result
        time.sleep(0.05)
    raise RuntimeError("Harness control deadline exceeded")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Isolated test stack; no production fixture routes."
    )
    parser.add_argument(
        "action",
        choices=(
            "start",
            "seed",
            "reset",
            "stop",
            "mail",
            "authorize",
            "resume",
            "expire",
            "dedup",
        ),
    )
    parser.add_argument("--mode", choices=("test",), default=None)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=("success", "duplicate", "transient", "owner-isolation"),
        default="success",
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--url")
    parser.add_argument("--owner", type=UUID)
    args = parser.parse_args()
    try:
        if args.action == "start":
            if args.state.exists():
                raise ValueError("Use a new private state-file path")
            stop = Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            try:
                with Stack(
                    mode=args.mode,
                    scenario=args.scenario,
                    port=args.port,
                    frontend_url=args.frontend_url,
                ) as stack:
                    write_private(args.state, stack.info())
                    print(json.dumps(stack.info()), flush=True)
                    while not stop.wait(0.05):
                        if any(child.poll() is not None for child in stack.children):
                            raise RuntimeError("Harness API or worker exited")
                        for request in sorted((stack.root / "commands").glob("*.json")):
                            data = json.loads(request.read_text())
                            request.unlink()
                            if data["action"] == "stop":
                                stop.set()
                                break
                            try:
                                if data["action"] in ("seed", "reset"):
                                    result = (
                                        stack.seed()
                                        if data["action"] == "seed"
                                        else stack.reset(data["scenario"])
                                    )
                                else:
                                    result = stack.browser_control(
                                        data["action"],
                                        UUID(data["owner"])
                                        if data.get("owner")
                                        else None,
                                    )
                            except ValueError:
                                result = {"error": "Invalid fixture control"}
                            write_private(args.state, stack.info())
                            write_private(stack.root / "replies" / request.name, result)
            finally:
                args.state.unlink(missing_ok=True)
        elif args.action in ("seed", "reset", "stop", "resume", "expire", "dedup"):
            print(
                json.dumps(control(args.state, args.action, args.scenario, args.owner))
            )
        else:
            root = Path(json.loads(args.state.read_text())["root"])
            value = (
                authorize(root, args.url)
                if args.action == "authorize"
                else [
                    json.loads(path.read_text())
                    for path in sorted((root / "mail").glob("*.json"))
                ]
            )
            print(json.dumps(value))
    except Exception:
        parser.exit(
            1,
            "Test stack failed; verify test mode, local PostgreSQL "
            "and the private state path.\n",
        )


if __name__ == "__main__":
    main()
