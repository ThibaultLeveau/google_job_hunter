import json
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.ai import AI, Accounting, Reply
from job_hunter.application.matching import EvaluatedMatch
from job_hunter.application.worker import PermanentTaskError, RetryableTaskError
from job_hunter.domain.errors import DomainError
from job_hunter.domain.matching import Match
from job_hunter.domain.tasks import NewTask, StaleLease, Task, TaskError, TaskKey
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.ai import AISettings, FixtureProvider, create_ai
from job_hunter.infrastructure.jobs import RunJobRecord
from job_hunter.infrastructure.match_events import (
    EVALUATE,
    SCAN,
    MatchRequestRecord,
    MatchScanRecord,
    enqueue_scan,
)
from job_hunter.infrastructure.matches import (
    MatchArtifactRecord,
    MatchIdentityRecord,
    PostgresMatches,
    access,
)
from job_hunter.infrastructure.matching import create_matcher
from job_hunter.infrastructure.pipeline import PostgresPipeline
from job_hunter.infrastructure.profiles import ProfileRecord
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.infrastructure.snapshots import PostgresSnapshots, SourceMergeRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord, snapshot
from job_hunter.infrastructure.worker import Handler

PAGE = 25


class UnknownMatch(FixtureProvider):
    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        requirements = json.loads(json.loads(source)["untrusted_source"])[
            "requirements"
        ]
        return Reply(
            {
                "items": [
                    {
                        "requirement_id": row["id"],
                        "status": "unknown",
                        "requirement_evidence": [
                            span["id"] for span in row["evidence"]
                        ],
                        "profile_evidence": [],
                        "rationale": "The fixture does not infer profile fit.",
                    }
                    for row in requirements
                ]
            },
            0,
            0,
        )


def create_schedule(
    pipeline: PostgresPipeline, settings: AISettings, now: Callable[[], datetime]
) -> MatchSchedule:
    matcher = create_matcher(pipeline.embeddings, settings, "{}")
    if settings.provider == "fixture":

        def factory(ledger: Accounting) -> AI:
            ai = create_ai(settings, ledger, fixture_response="{}")
            ai.chat = UnknownMatch(settings, "{}")
            return ai

        matcher.factory = factory
    return MatchSchedule(
        PostgresMatches(
            pipeline.database,
            matcher,
            cleaning_version=pipeline.cleaner.version,
            metadata_version=pipeline.metadata.version,
            requirements_version=pipeline.requirements.version,
            policy=pipeline.policy,
        ),
        now,
    )


class MatchSchedule:
    def __init__(
        self, repository: PostgresMatches, now: Callable[[], datetime]
    ) -> None:
        self.repository, self.database, self.now = repository, repository.database, now

    def latest(self, session: Session, identity: UUID) -> TaskRecord | None:
        return session.scalar(
            select(TaskRecord)
            .where(TaskRecord.kind == EVALUATE, TaskRecord.subject_id == identity)
            .order_by(TaskRecord.subject_version.desc())
            .limit(1)
        )

    def schedule(self, session: Session, owner: UUID, job: UUID, now: datetime) -> None:
        try:
            captured = self.repository.capture(session, owner, job)
        except DomainError:
            return
        if self.latest(session, captured.identity.id) is None:
            generation = (
                session.scalar(
                    select(func.max(MatchArtifactRecord.generation)).where(
                        MatchArtifactRecord.match_id == captured.identity.id
                    )
                )
                or 1
            )
            PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey(EVALUATE, captured.identity.id, generation),
                    captured.identity.id,
                    now,
                ),
                now,
            )

    def scan(self, session: Session, task: Task, result: object, now: datetime) -> None:
        row = session.get(
            MatchScanRecord,
            task.payload_ref,
            with_for_update=True,
            populate_existing=True,
        )
        if row is None or row.step != task.key.subject_version:
            raise PermanentTaskError()
        job = PostgresSnapshots(session).resolve(row.job_id) if row.job_id else None
        column = RunJobRecord.source_id if row.owner_id else SearchRunRecord.owner_id
        query = (
            select(column)
            .select_from(RunJobRecord)
            .join(SearchRunRecord, RunJobRecord.run_id == SearchRunRecord.id)
            .join(SavedSearchRecord)
            .join(UserRecord, SearchRunRecord.owner_id == UserRecord.id)
            .where(
                UserRecord.deleted_at.is_(None),
                SavedSearchRecord.deleted_at.is_(None),
                SearchRunRecord.status != "cancelled",
            )
        )
        if row.owner_id:
            query = query.where(SearchRunRecord.owner_id == row.owner_id)
        if job:
            query = query.where(RunJobRecord.source_id == job)
        if row.cursor:
            query = query.where(column > row.cursor)
        values = list(session.scalars(query.distinct().order_by(column).limit(PAGE)))
        for value in values:
            target = value if row.owner_id else job
            assert target is not None
            self.schedule(session, row.owner_id or value, target, now)
        if values:
            row.cursor = values[-1]
        if len(values) == PAGE:
            row.step += 1
            PostgresTaskQueue(session).enqueue(
                NewTask(TaskKey(SCAN, row.id, row.step), row.id, now), now
            )

    def backfill(self, after: UUID | None = None) -> UUID | None:
        with self.database.transaction() as session:
            query = select(UserRecord.id).where(UserRecord.deleted_at.is_(None))
            if after:
                query = query.where(UserRecord.id > after)
            owners = list(session.scalars(query.order_by(UserRecord.id).limit(100)))
            for owner in owners:
                enqueue_scan(
                    session, owner, None, self.repository.evaluator.version, self.now()
                )
            return owners[-1] if owners else None

    def execute(self, task: Task) -> EvaluatedMatch:
        try:
            with self.database.transaction() as session:
                live = session.scalar(
                    select(TaskRecord.id).where(
                        TaskRecord.id == task.id,
                        TaskRecord.status == "running",
                        TaskRecord.generation == task.generation,
                        TaskRecord.lease_token == task.lease_token,
                        TaskRecord.lease_expires_at > self.now(),
                    )
                )
                if live is None:
                    raise StaleLease()
                identity = session.get(MatchIdentityRecord, task.payload_ref)
                if identity is None:
                    raise PermanentTaskError()
                captured = self.repository.load(session, identity.owner_id, identity.id)
                latest = self.latest(session, identity.id)
                if latest is None or latest.subject_version != task.key.subject_version:
                    raise PermanentTaskError()
                existing = session.get(
                    MatchArtifactRecord, (identity.id, task.key.subject_version)
                )
                if existing:
                    return EvaluatedMatch(
                        Match.model_validate_json(json.dumps(existing.payload)), []
                    )
            return self.repository.evaluator.execute(captured.profile, captured.job)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        identity = session.get(MatchIdentityRecord, task.payload_ref)
        if identity is None or not isinstance(result, EvaluatedMatch):
            raise PermanentTaskError()
        self.repository.publish(
            session,
            identity.owner_id,
            identity.id,
            task.key.subject_version,
            result,
            now,
        )
        latest = self.latest(session, identity.id)
        if latest is None or latest.subject_version != task.key.subject_version:
            raise PermanentTaskError()

    def allowance(self, session: Session, owner: UUID) -> int:
        used = (
            session.scalar(
                select(func.count())
                .select_from(MatchRequestRecord)
                .where(
                    MatchRequestRecord.owner_id == owner,
                    MatchRequestRecord.created_at >= self.now() - timedelta(days=1),
                )
            )
            or 0
        )
        return max(0, 3 - used)

    def request(self, owner: UUID, job: UUID, key: UUID) -> Task | None:
        with self.database.transaction() as session:
            job = access(session, owner, job)
            prior = session.get(MatchRequestRecord, (owner, key))
            if prior:
                identity = session.get(MatchIdentityRecord, prior.match_id)
                assert identity is not None
                if PostgresSnapshots(session).resolve(identity.job_id) != job:
                    raise DomainError("conflict")
                task = session.scalars(
                    select(TaskRecord).where(
                        TaskRecord.kind == EVALUATE,
                        TaskRecord.subject_id == prior.match_id,
                        TaskRecord.subject_version == prior.generation,
                    )
                ).one()
                return snapshot(task)
            if not self.allowance(session, owner):
                return None
            captured = self.repository.capture(session, owner, job)
            latest = self.latest(session, captured.identity.id)
            stored = session.scalar(
                select(func.max(MatchArtifactRecord.generation)).where(
                    MatchArtifactRecord.match_id == captured.identity.id
                )
            )
            generation = max(latest.subject_version if latest else 0, stored or 0) + 1
            session.add(
                MatchRequestRecord(
                    owner_id=owner,
                    key=key,
                    match_id=captured.identity.id,
                    generation=generation,
                    created_at=self.now(),
                )
            )
            return PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey(EVALUATE, captured.identity.id, generation),
                    captured.identity.id,
                    self.now(),
                ),
                self.now(),
            )

    def status(
        self, owner: UUID, job: UUID, *, session: Session | None = None
    ) -> dict[str, object]:
        with (
            nullcontext(session) if session is not None else self.database.transaction()
        ) as session:
            job = access(session, owner, job)
            aliases = select(SourceMergeRecord.old_id).where(
                SourceMergeRecord.source_id == job
            )
            previous = session.scalar(
                select(MatchArtifactRecord)
                .join(MatchIdentityRecord)
                .where(
                    MatchIdentityRecord.owner_id == owner,
                    or_(
                        MatchIdentityRecord.job_id == job,
                        MatchIdentityRecord.job_id.in_(aliases),
                    ),
                )
                .order_by(
                    MatchArtifactRecord.created_at.desc(),
                    MatchArtifactRecord.generation.desc(),
                )
                .limit(1)
            )
            identity = None
            profile_record = session.get(ProfileRecord, owner)
            try:
                if profile_record is None:
                    raise DomainError("conflict")
                profile, inputs = self.repository.inputs(session, owner, job)
                identity = session.scalar(
                    select(MatchIdentityRecord).filter_by(
                        **self.repository.key(profile, inputs)
                    )
                )
            except DomainError as error:
                if error.code != "conflict":
                    raise
            task = self.latest(session, identity.id) if identity else None
            current = (
                session.get(MatchArtifactRecord, (identity.id, task.subject_version))
                if identity and task
                else None
            )
            failed = bool(task and task.status in {"failed", "cancelled"})
            pending = not current and not failed
            artifact = current or previous
            state = (
                "failed"
                if failed
                else "current"
                if current
                else "stale"
                if previous
                else "pending"
            )
            return {
                "state": state,
                "pending": pending,
                "current": current is not None and not failed,
                "stale": current is None and previous is not None,
                "result": artifact.payload if artifact else None,
                "profile_facts": artifact.profile_facts if artifact else [],
                "profile_changed": bool(
                    artifact
                    and profile_record
                    and artifact.payload["profile_revision"] != profile_record.revision
                ),
                "reason": task.error_code if failed and task else None,
                "reevaluations_remaining": self.allowance(session, owner),
            }

    def handlers(self) -> dict[str, Handler]:
        return {
            SCAN: Handler(lambda task: None, self.scan),
            EVALUATE: Handler(
                self.execute,
                self.publish,
                lambda result: (
                    TaskError.PERMANENT
                    if isinstance(result, EvaluatedMatch)
                    and result.content.state in {"failed", "unavailable"}
                    else None
                ),
            ),
        }
