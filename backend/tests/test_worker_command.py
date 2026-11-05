import os
import selectors
import signal
import subprocess
import sys

import pytest
from sqlalchemy import select
from test_worker import enqueue

from job_hunter import worker as command
from job_hunter.domain.tasks import Task
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import Handler

pytestmark = pytest.mark.integration


def test_command_runs_handler_and_drains_on_signal(
    isolated_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = enqueue(isolated_database)
    calls = []

    def execute(owned: Task) -> object:
        calls.append(owned.id)
        os.kill(os.getpid(), signal.SIGTERM)
        return "result"

    monkeypatch.setattr(
        command, "HANDLERS", {"stage": Handler(execute, lambda *args: None)}
    )
    monkeypatch.setattr(sys, "argv", ["worker", "--concurrency", "1"])
    previous = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        command.main()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    assert calls == [task.id]
    with isolated_database.transaction() as session:
        assert session.scalar(select(TaskRecord.status)) == "succeeded"


def test_module_command_and_empty_registry_shutdown(
    isolated_database: Database,
) -> None:
    enqueue(isolated_database, "not_deployed")
    with subprocess.Popen(
        [sys.executable, "-m", "job_hunter.worker", "--concurrency", "2"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert process.stderr is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stderr, selectors.EVENT_READ)
                assert selector.select(timeout=10), "Worker did not start"
            assert "Worker started." in process.stderr.readline()
            process.send_signal(signal.SIGTERM)
            _, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    with isolated_database.transaction() as session:
        assert session.scalar(select(TaskRecord.status)) == "pending"
