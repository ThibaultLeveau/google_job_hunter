import os
import subprocess
from pathlib import Path
from typing import BinaryIO
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text
from stack_harness import Stack
from test_extraction import TEXT
from test_stack_harness import login, wait_for

from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
IMAGE = next(
    line.split()[1]
    for line in (ROOT / "compose.runtime.yaml").read_text().splitlines()
    if line.strip().startswith("image: pgvector/pgvector:")
)


@pytest.mark.skipif(
    os.environ.get("RUN_RECOVERY_TESTS") != "1", reason="Explicit Docker/GPG rehearsal"
)
def test_encrypted_recovery_preserves_evidence_and_pending_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    container = "job-hunter-recovery-test-" + uuid4().hex[:12]
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(keys)}

    def run(
        args: list[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int = subprocess.PIPE,
    ) -> bytes:
        result = subprocess.run(
            args,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            env=env,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result.stdout or b""

    def files(root: Path, *, source: bool, stream: BinaryIO) -> None:
        args = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "-i",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--entrypoint",
            "tar",
            "--mount",
            f"type=bind,src={root},dst=/documents" + (",readonly" if source else ""),
            IMAGE,
            "-C",
            "/documents",
        ]
        if source:
            run([*args, "-cf", "-", "."], stdout=stream)
        else:
            run([*args, "--no-same-owner", "-xpf", "-"], stdin=stream)

    original: Stack | None = None
    restored: Stack | None = None
    try:
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--memory",
                "1g",
                "-e",
                "POSTGRES_USER=job_hunter",
                "-e",
                "POSTGRES_DB=job_hunter",
                "-e",
                "POSTGRES_PASSWORD=synthetic-recovery-only",
                "-p",
                "127.0.0.1::5432",
                "--health-cmd",
                "pg_isready -h 127.0.0.1 -U job_hunter",
                "--health-interval",
                "1s",
                "--health-timeout",
                "3s",
                "--health-retries",
                "30",
                IMAGE,
            ]
        )
        wait_for(
            lambda: run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container]
            ),
            lambda value: value.strip() == b"healthy",
        )
        address = run(["docker", "port", container, "5432"]).decode().strip()
        monkeypatch.setenv(
            "TEST_DATABASE_URL",
            f"postgresql+psycopg://job_hunter:synthetic-recovery-only@{address}/job_hunter",
        )
        original = Stack(mode="test").start()
        owner = uuid5(NAMESPACE_URL, "alice@example.org")
        with httpx.Client(
            base_url=original.config.api_url,
            trust_env=False,
            headers={"Origin": original.config.api_url},
        ) as client:
            login(client)
            uploaded = client.post(
                "/api/v1/documents",
                content=TEXT.encode(),
                headers={"Content-Type": "text/plain", "X-Document-Name": "resume.txt"},
            )
            assert uploaded.status_code == 201
            document_id = uploaded.json()["id"]
            proposals = wait_for(
                lambda: client.get(f"/api/v1/documents/{document_id}/proposals").json(),
                lambda value: value["status"] == "ready",
            )
            accepted = client.post(
                "/api/v1/profile/review",
                json={
                    "expected_revision": 0,
                    "idempotency_key": str(uuid4()),
                    "items": [
                        {"proposal_id": item["id"], "action": "accept"}
                        for item in proposals["items"]
                        if item["candidate"]["kind"] == "skill"
                    ],
                },
            )
            assert accepted.status_code == 200
            profile = client.get("/api/v1/profile").json()
            old_session = client.headers["Cookie"]
            assert profile["revision"] > 0
            search = client.post(
                "/api/v1/searches",
                json={
                    "name": "Recovery fixture",
                    "definition": {"groups": [["Python"]]},
                },
            )
            assert search.status_code == 201
            endpoint = f"/api/v1/searches/{search.json()['id']}/runs"
            assert (
                client.post(
                    endpoint,
                    json={"expected_revision": 1, "idempotency_key": str(uuid4())},
                ).status_code
                == 202
            )
            before = wait_for(
                lambda: original.job_artifacts(owner),
                lambda value: bool(value["matches"]),
            )
            original.browser_control("pause-worker", None)
            pending = client.post(
                endpoint, json={"expected_revision": 1, "idempotency_key": str(uuid4())}
            )
            assert pending.status_code == 202
            run_id = UUID(pending.json()["id"])
        original.halt()
        with original.db.transaction() as session:
            task = session.scalars(
                select(TaskRecord).where(TaskRecord.subject_id == run_id)
            ).one()
            assert task.status == "pending" and task.attempts == 0
            task_id = task.id
        working = tmp_path / "working"
        working.mkdir(mode=0o700)
        with (working / "database.dump").open("xb") as output:
            run(
                [
                    "docker",
                    "exec",
                    container,
                    "pg_dump",
                    "-U",
                    "job_hunter",
                    "-d",
                    original.name,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                ],
                stdout=output,
            )
        with (working / "documents.tar").open("xb") as output:
            files(original.root / "documents", source=True, stream=output)
        (working / "metadata.txt").write_text(
            "Synthetic recovery set; no production data.\n"
        )
        run(
            [
                "tar",
                "-cf",
                str(working / "set.tar"),
                "-C",
                str(working),
                "database.dump",
                "documents.tar",
                "metadata.txt",
            ]
        )
        run(
            [
                "gpg",
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-generate-key",
                "Synthetic recovery <recovery@example.org>",
                "rsa2048",
                "encrypt",
                "1d",
            ]
        )
        fingerprint = next(
            line.split(":")[9]
            for line in run(["gpg", "--with-colons", "--list-keys"])
            .decode()
            .splitlines()
            if line.startswith("fpr:")
        )
        archive = tmp_path / "backup.gpg"
        run(
            [
                "gpg",
                "--batch",
                "--trust-model",
                "always",
                "--recipient",
                fingerprint,
                "--output",
                str(archive),
                "--encrypt",
                str(working / "set.tar"),
            ]
        )
        archive.chmod(0o600)
        assert TEXT.encode() not in archive.read_bytes()
        original.launch()
        opened = tmp_path / "opened"
        opened.mkdir(mode=0o700)
        run(
            [
                "gpg",
                "--batch",
                "--output",
                str(opened / "set.tar"),
                "--decrypt",
                str(archive),
            ]
        )
        run(
            [
                "tar",
                "-xf",
                str(opened / "set.tar"),
                "-C",
                str(opened),
                "database.dump",
                "documents.tar",
                "metadata.txt",
            ]
        )
        assert (opened / "database.dump").read_bytes() == (
            working / "database.dump"
        ).read_bytes()
        restored = Stack(mode="test")
        restored.config = restored.config.model_copy(
            update={"mail_key": original.config.mail_key}
        )
        with restored.admin.connect() as connection:
            connection.execute(
                text(f'CREATE DATABASE "{restored.name}" TEMPLATE template0')
            )
        restored.created = True
        restored.database = Database(
            Settings(database_url=SecretStr(restored.config.database_url))
        )
        with (opened / "database.dump").open("rb") as source:
            run(
                [
                    "docker",
                    "exec",
                    "-i",
                    container,
                    "pg_restore",
                    "-U",
                    "job_hunter",
                    "-d",
                    restored.name,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    "--single-transaction",
                ],
                stdin=source,
            )
        with (opened / "documents.tar").open("rb") as source:
            files(restored.root / "documents", source=False, stream=source)
        assert restored.job_artifacts(owner) == before
        with restored.db.transaction() as session:
            recovered = session.get(TaskRecord, task_id)
            assert (
                recovered and recovered.status == "pending" and recovered.attempts == 0
            )
        run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "job_hunter",
                "-d",
                restored.name,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "BEGIN; DELETE FROM sessions; DELETE FROM oidc_flows; "
                "DELETE FROM email_verifications; "
                "DELETE FROM password_recoveries; COMMIT;",
            ]
        )
        restored.launch()
        with httpx.Client(
            base_url=restored.config.api_url,
            trust_env=False,
            headers={"Origin": restored.config.api_url},
        ) as client:
            assert (
                client.get("/api/auth/me", headers={"Cookie": old_session}).status_code
                == 401
            )
            login(client)
            assert client.get("/api/v1/profile").json() == profile
            assert (
                client.get(f"/api/v1/documents/{document_id}/download").content
                == TEXT.encode()
            )
            wait_for(
                lambda: client.get(f"/api/v1/searches/runs/{run_id}").json(),
                lambda value: value["status"] == "completed",
            )
            assert restored.job_artifacts(owner) == before
            login(client, "bob@example.org")
            assert (
                client.get(f"/api/v1/documents/{document_id}/download").status_code
                == 404
            )
            assert client.get(f"/api/v1/searches/runs/{run_id}").status_code == 404
        with restored.db.transaction() as session:
            recovered = session.get(TaskRecord, task_id)
            assert (
                recovered
                and recovered.status == "succeeded"
                and recovered.attempts == 1
            )
        assert all(
            path.stat().st_mode & 0o777 == 0o600
            for path in (restored.root / "documents").iterdir()
        )
        corrupted = tmp_path / "corrupted.gpg"
        corrupted.write_bytes(archive.read_bytes()[:-32])
        rejected = subprocess.run(
            ["gpg", "--batch", "--decrypt", str(corrupted)],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert rejected.returncode != 0
    finally:
        for stack in (original, restored):
            if stack is not None:
                stack.stop()
        subprocess.run(
            ["gpgconf", "--homedir", str(keys), "--kill", "gpg-agent"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        subprocess.run(
            ["docker", "rm", "-f", "-v", container],
            capture_output=True,
            timeout=30,
            check=False,
        )
