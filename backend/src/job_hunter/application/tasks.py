from datetime import datetime, timedelta
from typing import Protocol

from job_hunter.domain.tasks import NewTask, Task, TaskError, TaskKey


class TaskQueue(Protocol):
    """Transaction-bound queue. Subject use cases authorize access."""

    def enqueue(self, task: NewTask, now: datetime) -> Task: ...

    def claim(
        self, kinds: tuple[str, ...], now: datetime, lease: timedelta
    ) -> Task | None: ...

    def complete(self, task: Task, now: datetime) -> Task: ...

    def fail(self, task: Task, error: TaskError, now: datetime) -> Task: ...

    def retry(self, key: TaskKey, expected_generation: int, now: datetime) -> Task: ...
