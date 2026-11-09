import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import TypeAdapter
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.profiles import FactResult, ProfileSnapshot
from job_hunter.domain.errors import DomainError
from job_hunter.domain.extraction import Candidate
from job_hunter.domain.profiles import (
    FactData,
    ProfileFact,
    SourceEvidence,
    exact_key,
    same_content,
)
from job_hunter.domain.review import (
    ReviewBatch,
    ReviewOutcome,
    ReviewResult,
    reviewed,
    supported,
)
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.documents import (
    DocumentRecord,
    ExtractionRecord,
    ParsedDocumentRecord,
    ProposalRecord,
    ProposalSourceRecord,
)
from job_hunter.infrastructure.match_events import enqueue_scan

FACT = TypeAdapter(FactData)


class ProfileRecord(Base):
    __tablename__ = "profiles"
    __table_args__ = (CheckConstraint("revision >= 0", name="ck_profile_revision"),)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(default=0)


class ProfileFactRecord(Base):
    __tablename__ = "profile_facts"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "exact_key", name="uq_profile_fact_exact"),
        CheckConstraint(
            "kind IN ('skill', 'experience', 'project')", name="ck_profile_fact_kind"
        ),
        CheckConstraint(
            "jsonb_typeof(data) = 'object' AND coalesce(data->>'kind', '') = kind",
            name="ck_profile_fact_data",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("profiles.owner_id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16))
    exact_key: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSONB)
    user_fields: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewRequestRecord(Base):
    __tablename__ = "profile_review_requests"
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("profiles.owner_id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[UUID] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewDecisionRecord(Base):
    __tablename__ = "profile_review_decisions"
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_proposals.id", ondelete="CASCADE"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(16))
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FactSourceRecord(Base):
    __tablename__ = "profile_fact_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "field", "start", "end"],
            [
                "proposal_sources.proposal_id",
                "proposal_sources.field",
                "proposal_sources.start",
                "proposal_sources.end",
            ],
            ondelete="CASCADE",
        ),
    )
    fact_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_facts.id", ondelete="CASCADE"), primary_key=True
    )
    proposal_id: Mapped[UUID] = mapped_column(primary_key=True)
    field: Mapped[str] = mapped_column(String(16), primary_key=True)
    start: Mapped[int] = mapped_column(primary_key=True)
    end: Mapped[int] = mapped_column(primary_key=True)


class UnavailableSourceRecord(Base):
    __tablename__ = "unavailable_profile_sources"
    fact_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_facts.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(primary_key=True)


def detach_document(session: Session, document: DocumentRecord) -> None:
    profile = session.scalar(
        select(ProfileRecord)
        .where(ProfileRecord.owner_id == document.owner_id)
        .with_for_update()
    )
    if profile is None:
        return
    facts = (
        select(FactSourceRecord.fact_id, ParsedDocumentRecord.document_id)
        .select_from(FactSourceRecord)
        .join(ProfileFactRecord)
        .join(ProposalSourceRecord)
        .join(ProposalRecord)
        .join(ExtractionRecord)
        .join(ParsedDocumentRecord)
        .where(
            ParsedDocumentRecord.document_id == document.id,
            ProfileFactRecord.owner_id == document.owner_id,
        )
        .distinct()
    )
    changed = session.scalars(
        insert(UnavailableSourceRecord)
        .from_select(["fact_id", "document_id"], facts)
        .on_conflict_do_nothing()
        .returning(UnavailableSourceRecord.fact_id)
    ).first()
    if changed is not None:
        profile.revision += 1
        assert document.deleted_at is not None
        enqueue_scan(
            session,
            document.owner_id,
            None,
            f"profile:{profile.revision}",
            document.deleted_at,
        )


def view(row: ProfileFactRecord, session: Session) -> ProfileFact:
    evidence = session.execute(
        select(
            ProposalSourceRecord,
            ParsedDocumentRecord.document_version,
            ParsedDocumentRecord.data["language"].astext,
            DocumentRecord.id,
        )
        .select_from(FactSourceRecord)
        .join(ProposalSourceRecord)
        .join(ProposalRecord)
        .join(ExtractionRecord)
        .join(ParsedDocumentRecord)
        .join(DocumentRecord)
        .where(
            FactSourceRecord.fact_id == row.id,
            DocumentRecord.owner_id == row.owner_id,
            DocumentRecord.deleted_at.is_(None),
        )
        .order_by(
            DocumentRecord.id, ProposalSourceRecord.field, ProposalSourceRecord.start
        )
    )
    return ProfileFact(
        id=row.id,
        data=FACT.validate_python(row.data),
        created_at=row.created_at,
        updated_at=row.updated_at,
        user_fields=tuple(row.user_fields),
        unavailable_sources=tuple(
            session.scalars(
                select(UnavailableSourceRecord.document_id)
                .where(UnavailableSourceRecord.fact_id == row.id)
                .order_by(UnavailableSourceRecord.document_id)
            )
        ),
        source_associations=tuple(
            SourceEvidence(
                document_id=document,
                source_version=version,
                language=language,
                locator=span.locator,
                quote=span.quote,
                field=span.field,
                proposal_id=span.proposal_id,
                start=span.start,
                end=span.end,
            )
            for span, version, language, document in evidence
            if supported(span.field, row.data.get(span.field), span.quote)
        ),
    )


class PostgresProfiles:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self.database, self.now = database, now

    @staticmethod
    def lock_profile(session: Session, owner: UUID) -> ProfileRecord:
        session.execute(
            insert(ProfileRecord)
            .values(owner_id=owner, revision=0)
            .on_conflict_do_nothing()
        )
        return session.scalars(
            select(ProfileRecord)
            .where(ProfileRecord.owner_id == owner)
            .with_for_update()
        ).one()

    @contextmanager
    def locked(self, owner: UUID) -> Iterator[tuple[Session, ProfileRecord]]:
        with self.database.transaction() as session:
            if (
                session.scalar(
                    select(UserRecord.id)
                    .where(UserRecord.id == owner, UserRecord.deleted_at.is_(None))
                    .with_for_update()
                )
                is None
            ):
                raise DomainError("not_found")
            yield session, self.lock_profile(session, owner)

    def read(self, owner: UUID) -> ProfileSnapshot:
        with self.database.transaction() as session:
            profile = session.scalars(
                select(ProfileRecord)
                .where(ProfileRecord.owner_id == owner)
                .with_for_update(read=True)
            ).one_or_none()
            if profile is None:
                return ProfileSnapshot(id=owner, revision=0)
            rows = session.scalars(
                select(ProfileFactRecord)
                .where(ProfileFactRecord.owner_id == owner)
                .order_by(ProfileFactRecord.created_at, ProfileFactRecord.id)
            )
            return ProfileSnapshot(
                id=owner,
                revision=profile.revision,
                facts=tuple(view(row, session) for row in rows),
            )

    @staticmethod
    def owned(session: Session, owner: UUID, fact_id: UUID) -> ProfileFactRecord:
        row = session.scalars(
            select(ProfileFactRecord).where(
                ProfileFactRecord.owner_id == owner, ProfileFactRecord.id == fact_id
            )
        ).one_or_none()
        if row is None:
            raise DomainError("not_found")
        return row

    def get(self, owner: UUID, fact_id: UUID) -> ProfileFact:
        with self.database.transaction() as session:
            session.scalar(
                select(ProfileRecord)
                .where(ProfileRecord.owner_id == owner)
                .with_for_update(read=True)
            )
            return view(self.owned(session, owner, fact_id), session)

    def put(
        self,
        owner: UUID,
        expected_revision: int,
        data: FactData,
        fact_id: UUID | None = None,
    ) -> FactResult:
        outcome: Literal["created", "updated"]
        with self.locked(owner) as (session, profile):
            row = self.owned(session, owner, fact_id) if fact_id is not None else None
            if profile.revision != expected_revision:
                raise DomainError("conflict")
            duplicate = session.scalars(
                select(ProfileFactRecord).where(
                    ProfileFactRecord.owner_id == owner,
                    ProfileFactRecord.kind == data.kind,
                    ProfileFactRecord.exact_key == exact_key(data),
                )
            ).one_or_none()
            if row is None and duplicate is not None:
                if not same_content(view(duplicate, session).data, data):
                    raise DomainError("conflict")
                return FactResult(
                    outcome="existing",
                    revision=profile.revision,
                    fact=view(duplicate, session),
                )
            if row is not None:
                if row.kind != data.kind or (
                    duplicate is not None and duplicate.id != row.id
                ):
                    raise DomainError("conflict")
                if row.data == data.model_dump(mode="json"):
                    return FactResult(
                        outcome="existing",
                        revision=profile.revision,
                        fact=view(row, session),
                    )
                changed = {
                    name
                    for name, value in data.model_dump(mode="json").items()
                    if row.data.get(name) != value
                }
                row.user_fields = sorted(set(row.user_fields) | changed)
                for link, span in session.execute(
                    select(FactSourceRecord, ProposalSourceRecord)
                    .join(ProposalSourceRecord)
                    .where(FactSourceRecord.fact_id == row.id)
                ):
                    if not supported(
                        span.field,
                        data.model_dump(mode="json").get(span.field),
                        span.quote,
                    ):
                        session.delete(link)
                row.data, row.exact_key, row.updated_at = (
                    data.model_dump(mode="json"),
                    exact_key(data),
                    self.now(),
                )
                outcome = "updated"
            else:
                now = self.now()
                row = ProfileFactRecord(
                    id=uuid4(),
                    owner_id=owner,
                    kind=data.kind,
                    exact_key=exact_key(data),
                    data=data.model_dump(mode="json"),
                    user_fields=sorted(data.model_fields_set - {"kind"}),
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                outcome = "created"
            profile.revision += 1
            enqueue_scan(
                session, owner, None, f"profile:{profile.revision}", self.now()
            )
            session.flush()
            return FactResult(
                outcome=outcome, revision=profile.revision, fact=view(row, session)
            )

    def delete(self, owner: UUID, expected_revision: int, fact_id: UUID) -> FactResult:
        with self.locked(owner) as (session, profile):
            row = self.owned(session, owner, fact_id)
            if profile.revision != expected_revision:
                raise DomainError("conflict")
            session.delete(row)
            profile.revision += 1
            enqueue_scan(
                session, owner, None, f"profile:{profile.revision}", self.now()
            )
            session.flush()
            return FactResult(outcome="deleted", revision=profile.revision, fact=None)

    def review(self, owner: UUID, batch: ReviewBatch) -> ReviewResult:
        digest = hashlib.sha256(batch.model_dump_json().encode()).hexdigest()
        with self.database.transaction() as session:
            if (
                session.scalar(
                    select(UserRecord.id).where(
                        UserRecord.id == owner, UserRecord.deleted_at.is_(None)
                    )
                )
                is None
            ):
                raise DomainError("not_found")
            cached = session.get(ReviewRequestRecord, (owner, batch.idempotency_key))
            if cached is not None:
                if cached.digest != digest:
                    raise DomainError("conflict")
                return ReviewResult.model_validate_json(json.dumps(cached.result))
            # Extraction also locks documents before the owner; keep that lock order.
            rows = session.scalars(
                select(ProposalRecord)
                .join(ExtractionRecord)
                .join(ParsedDocumentRecord)
                .join(DocumentRecord)
                .where(
                    ProposalRecord.id.in_([item.proposal_id for item in batch.items]),
                    DocumentRecord.owner_id == owner,
                    DocumentRecord.deleted_at.is_(None),
                    DocumentRecord.version == ParsedDocumentRecord.document_version,
                    DocumentRecord.content_hash == ParsedDocumentRecord.source_hash,
                )
                .order_by(DocumentRecord.id, ProposalRecord.id)
                .with_for_update(of=DocumentRecord)
            ).all()
            if (
                len(rows) != len(batch.items)
                or session.scalar(
                    select(UserRecord.id)
                    .where(UserRecord.id == owner, UserRecord.deleted_at.is_(None))
                    .with_for_update()
                )
                is None
            ):
                raise DomainError("not_found")
            profile = self.lock_profile(session, owner)
            cached = session.get(ReviewRequestRecord, (owner, batch.idempotency_key))
            if cached is not None:
                if cached.digest != digest:
                    raise DomainError("conflict")
                return ReviewResult.model_validate_json(json.dumps(cached.result))
            if profile.revision != batch.expected_revision:
                raise DomainError("conflict")
            proposals = {row.id: Candidate.model_validate(row.data) for row in rows}
            outcomes, changed = [], False
            for item in batch.items:
                item_digest = hashlib.sha256(
                    item.model_dump_json().encode()
                ).hexdigest()
                outcome = ReviewOutcome(
                    proposal_id=item.proposal_id, outcome="conflict"
                )
                prior = session.get(ReviewDecisionRecord, item.proposal_id)
                if prior is not None:
                    if prior.digest == item_digest:
                        outcome = ReviewOutcome.model_validate_json(
                            json.dumps(prior.result)
                        )
                elif item.action == "reject":
                    outcome = ReviewOutcome(
                        proposal_id=item.proposal_id, outcome="rejected"
                    )
                else:
                    try:
                        data, corrections = reviewed(proposals[item.proposal_id], item)
                    except ValueError:
                        outcomes.append(outcome)
                        continue
                    fact = session.scalar(
                        select(ProfileFactRecord).where(
                            ProfileFactRecord.owner_id == owner,
                            ProfileFactRecord.kind == data.kind,
                            ProfileFactRecord.exact_key == exact_key(data),
                        )
                    )
                    if fact is not None and not same_content(
                        FACT.validate_python(fact.data), data
                    ):
                        outcomes.append(outcome)
                        continue
                    created = fact is None
                    if fact is None:
                        fact = ProfileFactRecord(
                            id=uuid4(),
                            owner_id=owner,
                            kind=data.kind,
                            exact_key=exact_key(data),
                            data=data.model_dump(mode="json"),
                            user_fields=corrections,
                            created_at=self.now(),
                            updated_at=self.now(),
                        )
                        session.add(fact)
                        session.flush()
                        changed = True
                    for span in session.scalars(
                        select(ProposalSourceRecord).where(
                            ProposalSourceRecord.proposal_id == item.proposal_id
                        )
                    ):
                        if supported(span.field, fact.data.get(span.field), span.quote):
                            session.add(
                                FactSourceRecord(
                                    fact_id=fact.id,
                                    proposal_id=span.proposal_id,
                                    field=span.field,
                                    start=span.start,
                                    end=span.end,
                                )
                            )
                            changed = True
                    outcome = ReviewOutcome(
                        proposal_id=item.proposal_id,
                        outcome="created" if created else "linked",
                        fact_id=fact.id,
                    )
                if prior is None and outcome.outcome != "conflict":
                    session.add(
                        ReviewDecisionRecord(
                            proposal_id=item.proposal_id,
                            action=item.action,
                            digest=item_digest,
                            result=outcome.model_dump(mode="json"),
                            created_at=self.now(),
                        )
                    )
                outcomes.append(outcome)
            if changed:
                profile.revision += 1
                enqueue_scan(
                    session, owner, None, f"profile:{profile.revision}", self.now()
                )
            result = ReviewResult(revision=profile.revision, items=outcomes)
            session.add(
                ReviewRequestRecord(
                    owner_id=owner,
                    key=batch.idempotency_key,
                    digest=digest,
                    result=result.model_dump(mode="json"),
                    created_at=self.now(),
                )
            )
            session.flush()
            return result
