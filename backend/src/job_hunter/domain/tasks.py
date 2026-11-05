import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from job_hunter.domain.errors import DomainError

TaskStatus = Literal["pending", "running", "succeeded", "failed"]


class TaskError(StrEnum):
    TRANSIENT = "transient_failure"
    PERMANENT = "permanent_failure"
    EXHAUSTED = "attempts_exhausted"


class StaleLease(DomainError):
    def __init__(self) -> None:
        super().__init__("conflict")


def require_utc(value: datetime) -> None:
    if value.utcoffset() != timedelta(0):
        raise ValueError("Task timestamps must use UTC.")


@dataclass(frozen=True)
class TaskKey:
    kind: str
    subject_id: UUID
    subject_version: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.kind):
            raise ValueError("Task kind must be a bounded code.")
        if not isinstance(self.subject_id, UUID):
            raise ValueError("Task subjects must be UUID references.")
        if type(self.subject_version) is not int or self.subject_version < 1:
            raise ValueError("Task subject version must be positive.")


@dataclass(frozen=True)
class NewTask:
    key: TaskKey
    payload_ref: UUID
    run_at: datetime
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.payload_ref, UUID):
            raise ValueError("Task payloads must be UUID references.")
        require_utc(self.run_at)
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 100:
            raise ValueError("Task attempt limit must be between 1 and 100.")


@dataclass(frozen=True)
class Task:
    id: UUID
    key: TaskKey
    payload_ref: UUID
    status: TaskStatus
    run_at: datetime
    attempts: int
    max_attempts: int
    generation: int
    lease_token: UUID
    lease_expires_at: datetime | None
    error_code: TaskError | None
