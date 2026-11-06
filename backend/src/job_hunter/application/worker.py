import logging
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event
from typing import Protocol

from job_hunter.domain.tasks import StaleLease, Task, TaskError, require_utc

logger = logging.getLogger(__name__)
type Execute = Callable[[Task], object]
type Submit = Callable[[Execute, Task], Future[object]]


class RetryableTaskError(Exception):
    """A bounded adapter call failed transiently; its message is never persisted."""

    def __init__(self, *args: object, retry_at: datetime | None = None) -> None:
        if retry_at is not None:
            require_utc(retry_at)
        self.retry_at = retry_at
        super().__init__(*args)


class DeferredTaskError(Exception):
    """Capacity is unavailable; return the claim without spending a failure attempt."""

    def __init__(self, retry_at: datetime) -> None:
        require_utc(retry_at)
        self.retry_at = retry_at
        super().__init__("capacity_wait")


class PermanentTaskError(Exception):
    """The task cannot succeed without a new subject version or manual intervention."""


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


class WorkerStore(Protocol):
    def claim(
        self, kinds: tuple[str, ...], now: datetime, lease: timedelta
    ) -> Task | None: ...
    def recover(self, kinds: tuple[str, ...], now: datetime) -> None: ...
    def renew(self, task: Task, now: datetime, lease: timedelta) -> None: ...
    def publish(self, task: Task, result: object, now: datetime) -> None: ...
    def postpone(self, task: Task, retry_at: datetime, now: datetime) -> None: ...
    def reject(
        self, task: Task, error: TaskError, retry_at: datetime | None, now: datetime
    ) -> None: ...


@dataclass
class Running:
    task: Task
    future: Future[object]
    heartbeat_at: datetime
    lost: bool = False


class Worker:
    def __init__(
        self,
        store: WorkerStore,
        handlers: Mapping[str, Execute],
        clock: Clock,
        submit: Submit,
        stop: Event,
        jitter: Callable[[], float],
        *,
        concurrency: int = 2,
        lease: timedelta = timedelta(seconds=60),
    ) -> None:
        if not 1 <= concurrency <= 16 or not timedelta(seconds=1) <= lease <= timedelta(
            minutes=5
        ):
            raise ValueError("Concurrency must be 1–16; lease must be 1–300 seconds.")
        self.store, self.handlers, self.clock = store, dict(handlers), clock
        self.submit, self.stop, self.jitter = submit, stop, jitter
        self.concurrency, self.lease = concurrency, lease
        self.running: list[Running] = []

    def _finish(self, job: Running) -> None:
        if job.lost:
            return
        try:
            result = job.future.result()
            self.store.publish(job.task, result, self.clock.now())
        except StaleLease:
            return
        except Exception as error:
            now = self.clock.now()
            retry_at = None
            code = TaskError.PERMANENT
            if isinstance(error, RetryableTaskError):
                code = TaskError.EXHAUSTED
                if job.task.attempts < job.task.max_attempts:
                    code = TaskError.TRANSIENT
                    delay = min(60, 2 ** min(job.task.attempts - 1, 6)) * self.jitter()
                    retry_at = now + timedelta(seconds=delay)
                    if error.retry_at is not None:
                        retry_at = max(
                            retry_at, min(error.retry_at, now + timedelta(hours=1))
                        )
            try:
                if isinstance(error, DeferredTaskError):
                    self.store.postpone(
                        job.task, max(now + timedelta(seconds=1), error.retry_at), now
                    )
                else:
                    self.store.reject(job.task, code, retry_at, now)
            except StaleLease:
                pass
            except Exception:
                logger.warning(
                    "Task outcome unavailable; lease recovery will resolve it."
                )

    def tick(self) -> None:
        for job in self.running[:]:
            if job.future.done():
                self._finish(job)
                self.running.remove(job)
            elif not job.lost and self.clock.now() >= job.heartbeat_at:
                try:
                    self.store.renew(job.task, self.clock.now(), self.lease)
                    job.heartbeat_at = self.clock.now() + self.lease / 3
                except Exception:
                    # Retain the slot until external work stops; discard its result.
                    job.lost = True
                    logger.warning("Task lease unavailable; result will be discarded.")
        if self.stop.is_set():
            return
        kinds = tuple(self.handlers)
        self.store.recover(kinds, self.clock.now())
        while len(self.running) < self.concurrency and not self.stop.is_set():
            task = self.store.claim(kinds, self.clock.now(), self.lease)
            if task is None:
                break
            self.running.append(
                Running(
                    task,
                    self.submit(self.handlers[task.key.kind], task),
                    self.clock.now() + self.lease / 3,
                )
            )

    def run(self) -> None:
        while not self.stop.is_set() or self.running:
            try:
                self.tick()
            except Exception:
                logger.warning("Worker storage unavailable; polling will retry.")
            if not self.stop.is_set() or self.running:
                self.clock.sleep(0.25)
