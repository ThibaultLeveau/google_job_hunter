import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, get_args
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func, or_, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.ai import ErrorCode
from job_hunter.application.crawling import CRAWL
from job_hunter.application.fetching import FetchErrorCode
from job_hunter.application.job_details import (
    EvaluatedProfileFact,
    JobDetail,
    MatchContext,
    RetryWrite,
    StageStatus,
    WorkAccepted,
)
from job_hunter.domain.cleaning import CleanText
from job_hunter.domain.errors import DomainError
from job_hunter.domain.job_metadata import Metadata
from job_hunter.domain.matching import Match, evidence_ids
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.requirements import Requirements
from job_hunter.domain.tasks import TaskError, TaskKey
from job_hunter.infrastructure.database import Base
from job_hunter.infrastructure.jobs import JobSourceRecord
from job_hunter.infrastructure.match_scheduling import MatchSchedule
from job_hunter.infrastructure.matches import access
from job_hunter.infrastructure.pipeline import PostgresPipeline
from job_hunter.infrastructure.requirements import RequirementsRecord
from job_hunter.infrastructure.requirements import source as requirement_source
from job_hunter.infrastructure.snapshots import PostgresSnapshots
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord

TERMINAL = {
    "blocked",
    "removed",
    "unsupported",
    "non_job",
    "empty",
    "unavailable",
    "cancelled",
}

SAFE = (
    set(get_args(ErrorCode))
    | set(get_args(FetchErrorCode))
    | {e.value for e in TaskError}
    | TERMINAL
    | {
        "content_type",
        "limits",
        "malformed",
        "non_job_marker",
        "robots_denied",
        "robots_unavailable",
        "host_capacity",
        "source_unavailable",
        "unverified_evidence",
        "http_denied",
        "http_removed",
        "empty_page",
        "interactive_challenge",
        "javascript_required",
        "processing_failed",
    }
)


def safe(reason: object) -> str | None:
    return (
        reason
        if isinstance(reason, str) and reason in SAFE
        else "processing_failed"
        if reason
        else None
    )


def revision(task: TaskRecord) -> str:
    return f"{task.kind}:{task.subject_id}:{task.subject_version}"


class RetryRecord(Base):
    __tablename__ = "job_retry_requests"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_job_retry_generation"),
    )
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID]
    stage: Mapped[str] = mapped_column(String(16))
    task_id: Mapped[UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    generation: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PostgresJobDetails:
    def __init__(
        self,
        pipeline: PostgresPipeline,
        matching: MatchSchedule,
        now: Callable[[], datetime],
    ) -> None:
        self.pipeline, self.matching, self.now = pipeline, matching, now
        self.database = pipeline.database

    def allowance(self, session: Session, owner: UUID) -> int:
        used = (
            session.scalar(
                select(func.count())
                .select_from(RetryRecord)
                .where(
                    RetryRecord.owner_id == owner,
                    RetryRecord.created_at >= self.now() - timedelta(days=1),
                )
            )
            or 0
        )
        return max(0, 5 - used)

    def tasks(self, session: Session, source: JobSourceRecord) -> dict[str, TaskRecord]:
        rows = session.scalars(
            select(TaskRecord)
            .where(
                or_(
                    (TaskRecord.kind == CRAWL) & (TaskRecord.subject_id == source.id),
                    TaskRecord.kind.in_(self.pipeline.kinds.values())
                    & (TaskRecord.subject_id == source.current_snapshot_id),
                )
            )
            .order_by(TaskRecord.subject_version.desc())
        )
        result: dict[str, TaskRecord] = {}
        for row in rows:
            result.setdefault(row.kind, row)
        return result

    def detail(self, owner: UUID, job: UUID) -> JobDetail:
        with self.database.transaction() as session:
            job = access(session, owner, job)
            source = session.get(JobSourceRecord, job)
            assert source is not None
            snapshot = source.current_snapshot_id
            artifacts = self.pipeline.artifacts(session, snapshot) if snapshot else None
            progress: dict[str, Any] = (
                self.pipeline.status(session, snapshot) if snapshot else {}
            )
            matching = self.matching.status(owner, job, session=session)
            prepared = (
                Preparation.model_validate(artifacts.prepared.payload)
                if artifacts and artifacts.prepared
                else None
            )
            tasks = self.tasks(session, source)
            stages = []
            terminal = source.crawl_state in TERMINAL or (
                prepared is not None and prepared.state != "uncertain"
            )
            for stage, kind in {"fetch": CRAWL, **self.pipeline.kinds}.items():
                task = tasks.get(kind)
                info = progress.get("stages", {}).get(stage, {})
                raw = (
                    source.crawl_state
                    if stage == "fetch"
                    else info.get("state", "waiting")
                )
                if raw not in TERMINAL and task and task.status != "succeeded":
                    raw = task.status
                blocked = terminal or (
                    stage == "requirements"
                    and artifacts
                    and artifacts.metadata
                    and artifacts.metadata.payload["state"] == "non_job"
                )
                if stage != "fetch" and blocked and raw in {"waiting", "pending"}:
                    raw = "unsupported"
                state = (
                    "completed"
                    if raw in {"fetched", "ready", "uncertain", "cleaned", "succeeded"}
                    else "degraded"
                    if raw in {"degraded", "ambiguous", "unknown", "stale"}
                    else "unsupported"
                    if raw in TERMINAL
                    else "failed"
                    if raw == "failed"
                    else "running"
                    if raw == "running"
                    else "pending"
                )
                stages.append(
                    StageStatus.model_validate(
                        {
                            "stage": stage,
                            "revision": revision(task) if task else kind,
                            "generation": task.generation if task else None,
                            "state": state,
                            "reason": None
                            if raw in {"pending", "running"}
                            else safe(
                                source.crawl_error
                                if stage == "fetch"
                                else info.get("error")
                                or (task.error_code if task else None)
                            ),
                            "created_at": task.created_at if task else None,
                            "updated_at": task.updated_at
                            if task
                            else info.get("updated_at") or source.crawl_observed_at,
                            "retryable": bool(
                                task
                                and task.status == "failed"
                                and state == "failed"
                                and (
                                    source.crawl_state == "failed"
                                    if stage == "fetch"
                                    else source.crawl_state == "fetched"
                                    and not terminal
                                )
                            ),
                        }
                    )
                )
            match = (
                Match.model_validate_json(json.dumps(matching["result"]))
                if matching["result"]
                else None
            )
            context = None
            if match:
                row = session.get(RequirementsRecord, match.requirements_id)
                assert row is not None
                requirements = Requirements.model_validate_json(json.dumps(row.payload))
                original, _ = requirement_source(session, row.metadata_id)
                cited = {id for e in match.evaluations for id in e.profile_evidence}
                facts = TypeAdapter(list[EvaluatedProfileFact]).validate_python(
                    matching["profile_facts"]
                )
                context = MatchContext(
                    profile_changed=bool(matching["profile_changed"]),
                    profile_facts=[fact for fact in facts if fact.id in cited],
                    requirements=requirements,
                    source=original,
                    citations={
                        id: span
                        for item in requirements.items
                        for id, span in zip(
                            evidence_ids(item), item.evidence, strict=True
                        )
                    },
                )
            detail = {
                "status": {
                    "id": job,
                    "snapshot_id": snapshot,
                    "freshness": "removed"
                    if source.crawl_state == "removed"
                    else "pending"
                    if snapshot is None
                    else "current"
                    if source.crawl_state == "fetched"
                    else "stale",
                    **{
                        name: bool(progress.get(name))
                        for name in (
                            "content_ready",
                            "analysis_ready",
                            "embeddings_ready",
                        )
                    },
                    "stages": stages,
                    "match_state": matching["state"],
                    "match_current": matching["current"],
                    "match_stale": matching["stale"],
                    "match_pending": matching["pending"],
                    "match_reason": safe(matching["reason"]),
                    "retries_remaining": self.allowance(session, owner),
                    "reevaluations_remaining": matching["reevaluations_remaining"],
                },
                "source_url": source.primary_url,
                "fetched_at": source.last_fetched_at,
                "preparation_id": artifacts.prepared.id
                if artifacts and artifacts.prepared
                else None,
                "content_id": artifacts.cleaned.id
                if artifacts and artifacts.cleaned
                else None,
                "metadata_id": artifacts.metadata.id
                if artifacts and artifacts.metadata
                else None,
                "requirements_id": artifacts.requirements.id
                if artifacts and artifacts.requirements
                else None,
                "source": prepared,
                "clean_text": CleanText.model_validate(
                    artifacts.cleaned.payload["content"]
                )
                if artifacts and artifacts.cleaned
                else None,
                "metadata": Metadata.model_validate_json(
                    json.dumps(artifacts.metadata.payload)
                )
                if artifacts and artifacts.metadata
                else None,
                "requirements": Requirements.model_validate_json(
                    json.dumps(artifacts.requirements.payload)
                )
                if artifacts and artifacts.requirements
                else None,
                "match": match,
                "match_context": context,
            }
            return JobDetail.model_validate(detail)

    def retry(self, owner: UUID, job: UUID, request: RetryWrite) -> WorkAccepted | None:
        with self.database.transaction() as session:
            job = access(session, owner, job)
            receipt = session.get(RetryRecord, (owner, request.idempotency_key))
            if receipt:
                task = session.get(TaskRecord, receipt.task_id)
                if (
                    PostgresSnapshots(session).resolve(receipt.job_id) != job
                    or receipt.stage != request.stage
                    or receipt.generation != request.expected_generation + 1
                    or task is None
                    or revision(task) != request.revision
                ):
                    raise DomainError("conflict")
                return WorkAccepted(generation=receipt.generation)
            source = session.get(JobSourceRecord, job)
            assert source is not None
            kind = {"fetch": CRAWL, **self.pipeline.kinds}[request.stage]
            task = self.tasks(session, source).get(kind)
            if task is None or revision(task) != request.revision:
                raise DomainError("conflict")
            if request.stage == "fetch":
                allowed = (
                    source.crawl_state == "failed"
                    or task.generation == request.expected_generation + 1
                )
            else:
                artifacts = (
                    self.pipeline.artifacts(session, source.current_snapshot_id)
                    if source.current_snapshot_id
                    else None
                )
                allowed = bool(
                    source.crawl_state == "fetched"
                    and artifacts
                    and (
                        artifacts.prepared is None
                        or artifacts.prepared.payload["state"] == "uncertain"
                    )
                )
            if not allowed:
                raise DomainError("conflict")
            if not self.allowance(session, owner):
                return None
            retried = PostgresTaskQueue(session).retry(
                TaskKey(task.kind, task.subject_id, task.subject_version),
                request.expected_generation,
                self.now(),
            )
            session.add(
                RetryRecord(
                    owner_id=owner,
                    key=request.idempotency_key,
                    job_id=job,
                    stage=request.stage,
                    task_id=task.id,
                    generation=retried.generation,
                    created_at=self.now(),
                )
            )
            return WorkAccepted(generation=retried.generation)

    def reevaluate(self, owner: UUID, job: UUID, key: UUID) -> WorkAccepted | None:
        task = self.matching.request(owner, job, key)
        return WorkAccepted(generation=task.key.subject_version) if task else None
