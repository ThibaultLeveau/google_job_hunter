import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import get_args
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.ai import ErrorCode, Record
from job_hunter.application.matching import (
    PROMPT,
    RUBRIC,
    EvaluatedMatch,
    EvaluateMatch,
    Evaluations,
    MatchJob,
    aggregate,
    professional_facts,
    validate,
)
from job_hunter.application.profiles import ProfileSnapshot
from job_hunter.application.requirements import RequirementsTrace
from job_hunter.domain.chunks import ChunkPolicy
from job_hunter.domain.errors import DomainError
from job_hunter.domain.job_metadata import Metadata
from job_hunter.domain.preparation import Preparation
from job_hunter.domain.requirements import Requirements
from job_hunter.domain.tasks import require_utc
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.chunks import read as read_chunks
from job_hunter.infrastructure.cleaning import CleaningRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.job_metadata import MetadataRecord
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    RunJobRecord,
    lock_job_namespace,
)
from job_hunter.infrastructure.preparation import PREPARATION_VERSION, PreparationRecord
from job_hunter.infrastructure.profiles import PostgresProfiles, ProfileFactRecord, view
from job_hunter.infrastructure.requirements import RequirementsRecord
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.infrastructure.snapshots import PostgresSnapshots


class MatchIdentityRecord(Base):
    __tablename__ = "private_matches"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "profile_revision",
            "requirements_id",
            "content_id",
            "chunk_version",
            "evaluator_version",
            name="uq_private_match_revision",
        ),
        CheckConstraint("profile_revision >= 0", name="ck_match_profile_revision"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Historical source IDs remain resolvable after public alias reconciliation.
    job_id: Mapped[UUID]
    profile_revision: Mapped[int]
    requirements_id: Mapped[UUID] = mapped_column(ForeignKey("job_requirements.id"))
    content_id: Mapped[UUID] = mapped_column(ForeignKey("job_cleanings.id"))
    chunk_version: Mapped[str] = mapped_column(String(64))
    evaluator_version: Mapped[str] = mapped_column(String(64))


class MatchArtifactRecord(Base):
    __tablename__ = "private_match_artifacts"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_match_generation"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND jsonb_typeof(profile_facts) = 'array'",
            name="ck_match_payload",
        ),
    )
    match_id: Mapped[UUID] = mapped_column(
        ForeignKey("private_matches.id", ondelete="CASCADE"), primary_key=True
    )
    generation: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    profile_facts: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MatchCallRecord(Base):
    __tablename__ = "private_match_calls"
    __table_args__ = (
        CheckConstraint("generation > 0", name="ck_match_call_generation"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    match_id: Mapped[UUID] = mapped_column(
        ForeignKey("private_matches.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)


@dataclass(frozen=True)
class MatchInputs:
    identity: MatchIdentityRecord
    profile: ProfileSnapshot
    job: MatchJob


def access(session: Session, owner: UUID, job: UUID) -> UUID:
    lock_job_namespace(session)
    job = PostgresSnapshots(session).resolve(job)
    user = session.scalar(
        select(UserRecord)
        .where(UserRecord.id == owner)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or user.deleted_at is not None:
        raise DomainError("not_found")
    association = session.scalar(
        select(SearchRunRecord.id)
        .join(RunJobRecord)
        .join(SavedSearchRecord)
        .where(
            RunJobRecord.source_id == job,
            SearchRunRecord.owner_id == owner,
            SearchRunRecord.status != "cancelled",
            SavedSearchRecord.deleted_at.is_(None),
        )
        .order_by(SearchRunRecord.id)
        .limit(1)
        .with_for_update(of=(SearchRunRecord, SavedSearchRecord))
    )
    if association is None:
        raise DomainError("not_found")
    return job


class PostgresMatches:
    def __init__(
        self,
        database: Database,
        evaluator: EvaluateMatch,
        *,
        cleaning_version: str,
        metadata_version: str,
        requirements_version: str,
        policy: ChunkPolicy,
    ) -> None:
        self.database, self.evaluator, self.policy = database, evaluator, policy
        self.cleaning_version, self.metadata_version = (
            cleaning_version,
            metadata_version,
        )
        self.requirements_version = requirements_version
        self.ai = evaluator.factory(RequirementsTrace())

    def inputs(
        self, session: Session, owner: UUID, job_id: UUID
    ) -> tuple[ProfileSnapshot, MatchJob]:
        job_id = access(session, owner, job_id)
        profile = PostgresProfiles.lock_profile(session, owner)
        session.refresh(profile)
        rows = session.scalars(
            select(ProfileFactRecord)
            .where(ProfileFactRecord.owner_id == owner)
            .execution_options(populate_existing=True)
        )
        accepted = ProfileSnapshot(
            id=owner,
            revision=profile.revision,
            facts=tuple(view(row, session) for row in rows),
        )
        row = session.execute(
            select(
                RequirementsRecord, MetadataRecord, PreparationRecord, CleaningRecord
            )
            .join(MetadataRecord, RequirementsRecord.metadata_id == MetadataRecord.id)
            .join(
                PreparationRecord, MetadataRecord.preparation_id == PreparationRecord.id
            )
            .join(CleaningRecord, CleaningRecord.preparation_id == PreparationRecord.id)
            .join(
                JobSourceRecord,
                JobSourceRecord.current_snapshot_id == PreparationRecord.snapshot_id,
            )
            .where(
                JobSourceRecord.id == job_id,
                RequirementsRecord.version == self.requirements_version,
                MetadataRecord.version == self.metadata_version,
                PreparationRecord.version == PREPARATION_VERSION,
                CleaningRecord.version == self.cleaning_version,
            )
        ).one_or_none()
        if row is None:
            raise DomainError("conflict")
        requirements, metadata, source, clean = row
        req = Requirements.model_validate_json(json.dumps(requirements.payload))
        if (
            req.state not in {"ready", "unknown"}
            or read_chunks(session, clean.id, self.policy.version) is None
        ):
            raise DomainError("conflict")
        return accepted, MatchJob(
            job_id,
            requirements.id,
            clean.id,
            self.policy.version,
            Preparation.model_validate(source.payload),
            Metadata.model_validate_json(json.dumps(metadata.payload)),
            req,
        )

    def key(self, profile: ProfileSnapshot, job: MatchJob) -> dict[str, object]:
        return dict(
            owner_id=profile.id,
            profile_revision=profile.revision,
            requirements_id=job.requirements_id,
            content_id=job.content_id,
            chunk_version=job.chunk_version,
            evaluator_version=self.evaluator.version,
        )

    def capture(self, session: Session, owner: UUID, job_id: UUID) -> MatchInputs:
        profile, job = self.inputs(session, owner, job_id)
        values = self.key(profile, job)
        session.execute(
            insert(MatchIdentityRecord)
            .values(id=uuid4(), job_id=job.id, **values)
            .on_conflict_do_nothing(constraint="uq_private_match_revision")
        )
        identity = session.scalars(
            select(MatchIdentityRecord).filter_by(**values)
        ).one()
        return MatchInputs(identity, profile, job)

    def load(self, session: Session, owner: UUID, identity_id: UUID) -> MatchInputs:
        identity = session.scalar(
            select(MatchIdentityRecord).where(
                MatchIdentityRecord.id == identity_id,
                MatchIdentityRecord.owner_id == owner,
            )
        )
        if identity is None:
            raise DomainError("not_found")
        profile, job = self.inputs(session, owner, identity.job_id)
        if any(
            getattr(identity, key) != value
            for key, value in self.key(profile, job).items()
        ):
            raise DomainError("conflict")
        return MatchInputs(identity, profile, job)

    def get(
        self, owner: UUID, identity_id: UUID, generation: int
    ) -> MatchArtifactRecord:
        with self.database.transaction() as session:
            identity = session.scalar(
                select(MatchIdentityRecord).where(
                    MatchIdentityRecord.id == identity_id,
                    MatchIdentityRecord.owner_id == owner,
                )
            )
            if identity is None:
                raise DomainError("not_found")
            access(session, owner, identity.job_id)
            result = session.get(MatchArtifactRecord, (identity_id, generation))
            if result is None:
                raise DomainError("not_found")
            return result

    def run(
        self, owner: UUID, job_id: UUID, now: datetime, generation: int = 1
    ) -> MatchArtifactRecord:
        require_utc(now)
        if type(generation) is not int or not 1 <= generation <= 2**31 - 1:
            raise ValueError("Invalid match attempt generation.")
        with self.database.transaction() as session:
            captured = self.capture(session, owner, job_id)
            existing = session.get(
                MatchArtifactRecord, (captured.identity.id, generation)
            )
            if existing is not None:
                return existing
        result = self.evaluator.execute(captured.profile, captured.job)
        with self.database.transaction() as session:
            return self.publish(
                session, owner, captured.identity.id, generation, result, now
            )

    def publish(
        self,
        session: Session,
        owner: UUID,
        identity_id: UUID,
        generation: int,
        result: EvaluatedMatch,
        now: datetime,
    ) -> MatchArtifactRecord:
        require_utc(now)
        if type(generation) is not int or not 1 <= generation <= 2**31 - 1:
            raise ValueError("Invalid match attempt generation.")
        captured = self.load(session, owner, identity_id)
        profile, job = captured.profile, captured.job
        content = result.content
        validate(content.evaluations, job, profile)
        expected = content.model_copy(
            update=dict(
                owner=owner,
                profile_revision=profile.revision,
                job_id=job.id,
                snapshot_id=job.source.snapshot_id,
                source_hash=job.source.content_hash,
                requirements_id=job.requirements_id,
                requirements_version=job.requirements.version,
                content_id=job.content_id,
                chunk_version=job.chunk_version,
                embedding_space=self.evaluator.embeddings.space.id,
                evaluator_version=self.evaluator.version,
                rubric_version=RUBRIC,
                prompt_version=PROMPT.version,
                model=self.ai.chat_model,
                score=aggregate(job.requirements, content.evaluations),
            )
        )
        reasons = {*get_args(ErrorCode), "unverified_evidence", "source_unavailable"}
        state = (
            "unavailable"
            if content.reason == "source_unavailable"
            else "failed"
            if content.reason
            else "unknown"
            if expected.score.percent is None
            else "ready"
        )
        if (
            expected != content
            or content.state != state
            or content.reason is not None
            and (
                content.reason not in reasons
                or any(item.status != "unknown" for item in content.evaluations)
            )
            or len(result.accounting) > 3
        ):
            raise ValueError("Invalid private match provenance.")
        kinds: list[str] = []
        for value in result.accounting:
            record = TypeAdapter(Record).validate_json(json.dumps(value), strict=True)
            call = record.call
            chat = call.kind == "chat"
            kinds.append(call.kind)
            schema = (
                json.dumps(
                    Evaluations.model_json_schema(), ensure_ascii=False, sort_keys=True
                )
                if chat
                else str(self.evaluator.embeddings.space.dimensions)
            )
            if (
                call.kind not in {"chat", "embedding"}
                or kinds.count(call.kind) > (2 if chat else 1)
                or call.schema_hash != hashlib.sha256(schema.encode()).hexdigest()
                or not 0
                <= call.input_bound
                <= (self.ai.limits.input_tokens if chat else 8000)
                or call.output_bound != (self.ai.limits.output_tokens if chat else 0)
                or call.model
                != (
                    self.ai.chat_model
                    if chat
                    else self.evaluator.embeddings.space.model
                )
                or call.prompt_version != (PROMPT.version if chat else "none")
                or call.schema_version
                != ("match-evaluations-v1" if chat else "embedding-v1")
                or not 1 <= call.attempt <= (2 if chat else 1)
                or not math.isfinite(record.duration_seconds)
                or record.duration_seconds < 0
                or any(
                    n is not None and n < 0
                    for n in (record.input_tokens, record.output_tokens)
                )
            ):
                raise ValueError("Invalid private match accounting.")
            session.execute(
                insert(MatchCallRecord)
                .values(
                    id=call.id,
                    match_id=identity_id,
                    generation=generation,
                    payload=json.loads(json.dumps(asdict(record), default=str)),
                )
                .on_conflict_do_nothing()
            )
            stored = session.get(MatchCallRecord, call.id, populate_existing=True)
            assert stored is not None
            if stored.match_id != identity_id or stored.generation != generation:
                raise ValueError("Call identity belongs to another match attempt.")
        session.execute(
            insert(MatchArtifactRecord)
            .values(
                match_id=identity_id,
                generation=generation,
                payload=content.model_dump(mode="json"),
                profile_facts=professional_facts(profile),
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
        return session.scalars(
            select(MatchArtifactRecord).where(
                MatchArtifactRecord.match_id == identity_id,
                MatchArtifactRecord.generation == generation,
            )
        ).one()
