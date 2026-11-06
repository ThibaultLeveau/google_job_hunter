import fcntl
import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Select,
    String,
    UniqueConstraint,
    and_,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, aliased, mapped_column

from job_hunter.application.documents import DeletionState, Documents, PrivateStorage
from job_hunter.application.extraction import Extracted, ExtractProfile
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.documents import PARSER_VERSION, Document, ParseResult
from job_hunter.domain.errors import DomainError
from job_hunter.domain.extraction import Candidate, Proposal, ProposalPage
from job_hunter.domain.tasks import NewTask, Task, TaskError, TaskKey
from job_hunter.infrastructure.accounts import UserRecord, live_user
from job_hunter.infrastructure.database import Base, Database
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler
from job_hunter.settings import ConfigurationError, load_settings


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JOB_HUNTER_DOCUMENT_", frozen=True, hide_input_in_errors=True
    )
    root: Path | None = None
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=50 * 1024 * 1024)
    quota_bytes: int = Field(default=100 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    quota_count: int = Field(default=100, ge=1, le=1000)


def load_storage_settings() -> StorageSettings:
    try:
        settings = StorageSettings()
        if settings.root is not None and not settings.root.is_absolute():
            raise ValueError()
        return settings
    except SettingsError, ValidationError, ValueError:
        raise ConfigurationError("Invalid document storage settings.") from None


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("owner_id", "content_hash", name="uq_document_content"),
        UniqueConstraint("storage_key", name="uq_document_storage"),
        CheckConstraint("size_bytes > 0 AND version > 0", name="ck_document_size"),
        CheckConstraint("content_hash ~ '^[a-f0-9]{64}$'", name="ck_document_hash"),
        CheckConstraint("storage_key ~ '^[a-f0-9]{32}$'", name="ck_document_key"),
        CheckConstraint(
            "status IN ('queued','processing','ready','failed','unsupported')",
            name="ck_document_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(160))
    media_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int]
    version: Mapped[int]
    status: Mapped[str] = mapped_column(String(16))
    parse_error: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentDeletionRecord(Base):
    __tablename__ = "document_deletions"
    __table_args__ = (CheckConstraint("step > 0", name="ck_document_deletion_step"),)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    step: Mapped[int]
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ParsedDocumentRecord(Base):
    __tablename__ = "parsed_documents"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "document_version",
            "parser_version",
            name="uq_parsed_version",
        ),
        CheckConstraint(
            "document_version > 0 AND parser_version > 0", name="ck_parsed_versions"
        ),
        CheckConstraint(
            "source_hash ~ '^[a-f0-9]{64}$' AND content_hash ~ '^[a-f0-9]{64}$'",
            name="ck_parsed_hashes",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE")
    )
    document_version: Mapped[int]
    parser_version: Mapped[int]
    source_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DocumentParsing:
    def __init__(
        self,
        database: Database,
        storage: PrivateStorage,
        parser: Callable[[BinaryIO, str], ParseResult],
        now: Callable[[], datetime],
        after_parse: Callable[[Session, ParsedDocumentRecord, datetime], None]
        | None = None,
    ) -> None:
        self.database, self.storage, self.parser, self.now = (
            database,
            storage,
            parser,
            now,
        )
        self.after_parse = after_parse

    def live(self, session: Session, task: Task) -> DocumentRecord | None:
        return session.scalar(
            select(DocumentRecord)
            .join(UserRecord)
            .where(
                DocumentRecord.id == task.key.subject_id,
                DocumentRecord.id == task.payload_ref,
                DocumentRecord.version == task.key.subject_version,
                DocumentRecord.deleted_at.is_(None),
                UserRecord.deleted_at.is_(None),
            )
            .with_for_update()
        )

    def artifact(self, session: Session, task: Task) -> ParsedDocumentRecord | None:
        return session.scalar(
            select(ParsedDocumentRecord).where(
                ParsedDocumentRecord.document_id == task.key.subject_id,
                ParsedDocumentRecord.document_version == task.key.subject_version,
                ParsedDocumentRecord.parser_version == PARSER_VERSION,
            )
        )

    def execute(self, task: Task) -> ParseResult | None:
        with self.database.transaction() as session:
            current = session.scalar(
                select(TaskRecord)
                .where(
                    TaskRecord.id == task.id,
                    TaskRecord.status == "running",
                    TaskRecord.generation == task.generation,
                    TaskRecord.lease_token == task.lease_token,
                    TaskRecord.lease_expires_at > self.now(),
                )
                .with_for_update()
            )
            if current is None or (row := self.live(session, task)) is None:
                return None
            artifact = self.artifact(session, task)
            if artifact is not None:
                return ParseResult.model_validate(artifact.data)
            row.status, row.parse_error = "processing", None
            key, media_type = row.storage_key, row.media_type
        try:
            with self.storage.open(key) as file:
                return self.parser(file, media_type)
        except OSError:
            return ParseResult(error="storage_unavailable")

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        if (
            not isinstance(result, ParseResult)
            or (row := self.live(session, task)) is None
        ):
            return
        if result.error is None and result.source_hash != row.content_hash:
            result = ParseResult(error="invalid_document")
        row.parse_error = result.error
        if result.error is not None:
            # Preserve the queue's failed-task retry contract for known parse errors.
            session.execute(
                update(TaskRecord)
                .where(TaskRecord.id == task.id)
                .values(
                    status="failed",
                    error_code=TaskError.PERMANENT.value,
                )
            )
            row.status = (
                "unsupported"
                if result.error
                in {"ocr_required", "encrypted_document", "unsupported_format"}
                else "failed"
            )
        else:
            row.status = "ready"
            artifact = self.artifact(session, task)
            if artifact is None:
                artifact = ParsedDocumentRecord(
                    id=uuid4(),
                    document_id=row.id,
                    document_version=row.version,
                    parser_version=PARSER_VERSION,
                    source_hash=row.content_hash,
                    content_hash=result.content_hash,
                    data=result.model_dump(mode="json"),
                    created_at=now,
                )
                session.add(artifact)
                session.flush()
            if self.after_parse is not None:
                self.after_parse(session, artifact, now)

    def handlers(self) -> dict[str, Handler]:
        def execute(task: Task) -> object:
            try:
                return self.execute(task)
            except SQLAlchemyError:
                raise RetryableTaskError() from None

        cleanup = DocumentCleanup(self.database, self.storage, self.now)
        return {
            "document_parse": Handler(execute, self.publish),
            "document_cleanup": Handler(cleanup.execute, cleanup.publish),
        }


def snapshot(row: DocumentRecord) -> Document:
    return Document.model_validate(row, from_attributes=True)


class ExtractionRecord(Base):
    __tablename__ = "profile_extractions"
    __table_args__ = (
        UniqueConstraint("parsed_id", "version_key", name="uq_extraction_version"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    parsed_id: Mapped[UUID] = mapped_column(
        ForeignKey("parsed_documents.id", ondelete="CASCADE")
    )
    version_key: Mapped[str] = mapped_column(String(64))
    accounting: Mapped[list[dict[str, object]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProposalRecord(Base):
    __tablename__ = "profile_proposals"
    __table_args__ = (
        UniqueConstraint("extraction_id", "ordinal", name="uq_proposal_ordinal"),
        CheckConstraint("ordinal >= 0", name="ck_proposal_ordinal"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    extraction_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_extractions.id", ondelete="CASCADE")
    )
    ordinal: Mapped[int]
    data: Mapped[dict[str, object]] = mapped_column(JSONB)


class ProposalSourceRecord(Base):
    __tablename__ = "proposal_sources"
    __table_args__ = (
        CheckConstraint('start >= 0 AND "end" > start', name="ck_proposal_span"),
    )
    proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("profile_proposals.id", ondelete="CASCADE"), primary_key=True
    )
    field: Mapped[str] = mapped_column(String(16), primary_key=True)
    start: Mapped[int] = mapped_column(primary_key=True)
    end: Mapped[int] = mapped_column(primary_key=True)
    locator: Mapped[str] = mapped_column(String(32))
    quote: Mapped[str] = mapped_column(String(4000))


class PostgresExtraction:
    def __init__(
        self,
        database: Database,
        stage: ExtractProfile,
        now: Callable[[], datetime],
        enabled: bool = True,
    ) -> None:
        self.database, self.stage = database, stage
        self.now, self.enabled = now, enabled

    @staticmethod
    def current() -> Select[ParsedDocumentRecord]:
        return (
            select(ParsedDocumentRecord)
            .join(DocumentRecord)
            .join(UserRecord)
            .where(
                ParsedDocumentRecord.parser_version == PARSER_VERSION,
                ParsedDocumentRecord.document_version == DocumentRecord.version,
                ParsedDocumentRecord.source_hash == DocumentRecord.content_hash,
                DocumentRecord.deleted_at.is_(None),
                UserRecord.deleted_at.is_(None),
            )
        )

    def live(self, session: Session, parsed_id: UUID) -> ParsedDocumentRecord | None:
        return session.scalar(
            self.current().where(ParsedDocumentRecord.id == parsed_id).with_for_update()
        )

    def artifact(self, session: Session, parsed_id: UUID) -> ExtractionRecord | None:
        return session.scalar(
            select(ExtractionRecord).where(
                ExtractionRecord.parsed_id == parsed_id,
                ExtractionRecord.version_key == self.stage.version,
            )
        )

    def enqueue(
        self, session: Session, parsed: ParsedDocumentRecord, now: datetime
    ) -> None:
        if (
            self.enabled
            and session.scalar(
                self.current().where(ParsedDocumentRecord.id == parsed.id)
            )
            is not None
        ):
            key = TaskKey(self.stage.kind, parsed.id, 1)
            PostgresTaskQueue(session).enqueue(
                NewTask(key, parsed.id, now, max_attempts=1), now
            )

    def backfill(self, limit: int = 100) -> int:
        if not self.enabled:
            return 0
        with self.database.transaction() as session:
            parent = aliased(TaskRecord)
            rows = list(
                session.scalars(
                    self.current()
                    .join(
                        parent,
                        and_(
                            parent.kind == "document_parse",
                            parent.subject_id == DocumentRecord.id,
                            parent.subject_version == DocumentRecord.version,
                        ),
                    )
                    .where(
                        ~select(TaskRecord.id)
                        .where(
                            TaskRecord.kind == self.stage.kind,
                            TaskRecord.payload_ref == ParsedDocumentRecord.id,
                        )
                        .exists(),
                    )
                    .order_by(ParsedDocumentRecord.created_at, ParsedDocumentRecord.id)
                    .limit(max(1, min(limit, 100)))
                    .with_for_update(skip_locked=True, of=parent)
                )
            )
            for parsed in rows:
                self.enqueue(session, parsed, self.now())
            return len(rows)

    def execute(self, task: Task) -> Extracted | None:
        with self.database.transaction() as session:
            current = session.scalar(
                select(TaskRecord)
                .where(
                    TaskRecord.id == task.id,
                    TaskRecord.status == "running",
                    TaskRecord.generation == task.generation,
                    TaskRecord.lease_token == task.lease_token,
                    TaskRecord.lease_expires_at > self.now(),
                )
                .with_for_update()
            )
            parsed = self.live(session, task.payload_ref)
            if (
                current is None
                or parsed is None
                or self.artifact(session, parsed.id) is not None
            ):
                return None
            source = ParseResult.model_validate(parsed.data)
            if source.content_hash != parsed.content_hash:
                raise ValueError("Invalid source version.")
        return self.stage.execute(source)

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        parsed = self.live(session, task.payload_ref)
        if (
            not isinstance(result, Extracted)
            or parsed is None
            or result.content_hash != parsed.content_hash
            or self.artifact(session, parsed.id) is not None
        ):
            return
        artifact = ExtractionRecord(
            id=uuid4(),
            parsed_id=parsed.id,
            version_key=self.stage.version,
            accounting=result.accounting,
            created_at=now,
        )
        session.add(artifact)
        session.flush()
        for index, candidate in enumerate(result.candidates):
            row = ProposalRecord(
                id=uuid4(),
                extraction_id=artifact.id,
                ordinal=index,
                data=candidate.model_dump(mode="json"),
            )
            session.add(row)
            session.flush()
            spans = {
                (field.name, span.start, span.end): span
                for field in candidate.fields
                for span in field.evidence
            }
            session.add_all(
                ProposalSourceRecord(
                    proposal_id=row.id, field=field, **span.model_dump()
                )
                for (field, _, _), span in spans.items()
            )

    def handlers(self) -> dict[str, Handler]:
        if not self.enabled:
            return {}
        return {self.stage.kind: Handler(self.execute, self.publish)}

    def read(self, owner: UUID, document: UUID, offset: int) -> ProposalPage:
        with self.database.transaction() as session:
            row = session.scalar(
                select(DocumentRecord)
                .join(UserRecord)
                .where(
                    DocumentRecord.id == document,
                    DocumentRecord.owner_id == owner,
                    DocumentRecord.deleted_at.is_(None),
                    UserRecord.deleted_at.is_(None),
                )
                .with_for_update(read=True)
            )
            if row is None:
                raise DomainError("not_found")
            parsed = session.scalar(self.current().where(DocumentRecord.id == row.id))
            if parsed is None:
                return ProposalPage(
                    status="failed"
                    if row.status in {"failed", "unsupported"}
                    else "waiting_for_parse",
                    source_version=row.version,
                )
            artifact = self.artifact(session, parsed.id)
            language = ParseResult.model_validate(parsed.data).language
            if artifact is None:
                state = session.scalar(
                    select(TaskRecord.status).where(
                        TaskRecord.kind == self.stage.kind,
                        TaskRecord.payload_ref == parsed.id,
                    )
                )
                return ProposalPage(
                    status="unavailable"
                    if not self.enabled
                    else "running"
                    if state == "running"
                    else "failed"
                    if state in {"failed", "succeeded"}
                    else "pending",
                    source_version=row.version,
                    language=language,
                )
            count = session.scalar(
                select(func.count())
                .select_from(ProposalRecord)
                .where(ProposalRecord.extraction_id == artifact.id)
            )
            rows = session.scalars(
                select(ProposalRecord)
                .where(ProposalRecord.extraction_id == artifact.id)
                .order_by(ProposalRecord.ordinal)
                .offset(offset)
                .limit(100)
            )
            return ProposalPage(
                status="ready",
                artifact_id=artifact.id,
                source_version=row.version,
                language=language,
                count=count or 0,
                items=[
                    Proposal(
                        id=proposal.id,
                        candidate=Candidate.model_validate(proposal.data),
                    )
                    for proposal in rows
                ],
            )


class PostgresDocuments(Documents):
    def __init__(
        self, database: Database, settings: StorageSettings, now: Callable[[], datetime]
    ) -> None:
        self.database, self.settings, self.now = database, settings, now

    def add(
        self, owner: UUID, key: str, digest: str, name: str, media_type: str, size: int
    ) -> tuple[Document, bool]:
        with self.database.transaction() as session:
            if live_user(session, owner) is None:
                raise DomainError("not_found")
            rows = list(
                session.scalars(
                    select(DocumentRecord).where(DocumentRecord.owner_id == owner)
                )
            )
            for row in rows:
                if row.content_hash == digest:
                    if row.deleted_at is not None:
                        raise DomainError("conflict")
                    return snapshot(row), False
            if (
                len(rows) >= self.settings.quota_count
                or sum(row.size_bytes for row in rows) + size
                > self.settings.quota_bytes
            ):
                raise DomainError("conflict")
            now, document_id = self.now(), uuid4()
            row = DocumentRecord(
                id=document_id,
                owner_id=owner,
                content_hash=digest,
                storage_key=key,
                filename=name,
                media_type=media_type,
                size_bytes=size,
                version=1,
                status="queued",
                created_at=now,
            )
            session.add(row)
            session.flush()
            PostgresTaskQueue(session).enqueue(
                NewTask(TaskKey("document_parse", document_id, 1), document_id, now),
                now,
            )
            return snapshot(row), True

    def list(self, owner: UUID, offset: int) -> list[Document]:
        with self.database.transaction() as session:
            return [
                snapshot(row)
                for row in session.scalars(
                    select(DocumentRecord)
                    .where(
                        DocumentRecord.owner_id == owner,
                        DocumentRecord.deleted_at.is_(None),
                    )
                    .order_by(DocumentRecord.created_at, DocumentRecord.id)
                    .offset(offset)
                    .limit(100)
                )
            ]

    def get(self, owner: UUID, document: UUID) -> tuple[Document, str]:
        with self.database.transaction() as session:
            row = session.scalar(
                select(DocumentRecord).where(
                    DocumentRecord.id == document,
                    DocumentRecord.owner_id == owner,
                    DocumentRecord.deleted_at.is_(None),
                )
            )
            if row is None:
                raise DomainError("not_found")
            return snapshot(row), row.storage_key

    @staticmethod
    def deletion_state(
        session: Session, phase: DocumentDeletionRecord
    ) -> DeletionState:
        failed = session.scalar(
            select(TaskRecord.id).where(
                TaskRecord.kind == "document_cleanup",
                TaskRecord.subject_id == phase.document_id,
                TaskRecord.subject_version == phase.step,
                TaskRecord.status == "failed",
            )
        )
        return DeletionState(
            id=phase.document_id,
            status="deleted" if phase.completed_at else "deleting",
            error="cleanup_failed" if failed and not phase.completed_at else None,
        )

    def status(self, owner: UUID, document: UUID) -> Document | DeletionState:
        with self.database.transaction() as session:
            phase = session.scalar(
                select(DocumentDeletionRecord)
                .join(DocumentRecord)
                .where(DocumentRecord.id == document, DocumentRecord.owner_id == owner)
            )
            if phase is not None:
                return self.deletion_state(session, phase)
        return self.get(owner, document)[0]

    def delete(self, owner: UUID, document: UUID) -> DeletionState:
        # Profiles reference document tables, so import the lifecycle join at call time.
        from job_hunter.infrastructure.profiles import detach_document

        with self.database.transaction() as session:
            if (
                session.scalar(
                    select(DocumentRecord.id).where(
                        DocumentRecord.id == document, DocumentRecord.owner_id == owner
                    )
                )
                is None
            ):
                raise DomainError("not_found")
            # Freeze producers before locking the document, matching worker publication.
            parse_tasks = list(
                session.scalars(
                    select(TaskRecord)
                    .where(
                        TaskRecord.kind == "document_parse",
                        TaskRecord.payload_ref == document,
                    )
                    .order_by(TaskRecord.id)
                    .with_for_update()
                )
            )
            parsed = select(ParsedDocumentRecord.id).where(
                ParsedDocumentRecord.document_id == document
            )
            extraction_tasks = list(
                session.scalars(
                    select(TaskRecord)
                    .where(
                        TaskRecord.kind.startswith("profile_extract_", autoescape=True),
                        TaskRecord.payload_ref.in_(parsed),
                    )
                    .order_by(TaskRecord.id)
                    .with_for_update()
                )
            )
            list(
                session.scalars(
                    select(TaskRecord)
                    .where(
                        TaskRecord.kind == "document_cleanup",
                        TaskRecord.payload_ref == document,
                    )
                    .order_by(TaskRecord.id)
                    .with_for_update()
                )
            )
            row = session.scalars(
                select(DocumentRecord)
                .where(DocumentRecord.id == document, DocumentRecord.owner_id == owner)
                .with_for_update()
            ).one()
            session.scalar(
                select(UserRecord).where(UserRecord.id == owner).with_for_update()
            )
            phase = session.get(DocumentDeletionRecord, document)
            now = self.now()
            if phase is None:
                row.deleted_at = row.deleted_at or now
                detach_document(session, row)
                phase = DocumentDeletionRecord(document_id=document, step=1)
                session.add(phase)
                for task in parse_tasks + extraction_tasks:
                    if task.status != "succeeded":
                        task.status, task.error_code = "cancelled", None
                        task.generation += 1
                        task.lease_token, task.lease_expires_at = uuid4(), None
                        task.updated_at = now
            if phase.completed_at is None:
                queue = PostgresTaskQueue(session)
                cleanup_task = queue.enqueue(
                    NewTask(
                        TaskKey("document_cleanup", document, phase.step),
                        document,
                        now,
                        max_attempts=5,
                    ),
                    now,
                )
                if cleanup_task.status == "failed":
                    queue.retry(cleanup_task.key, cleanup_task.generation, now)
            session.flush()
            return self.deletion_state(session, phase)

    def referenced(self, key: str) -> bool:
        with self.database.transaction() as session:
            return (
                session.scalar(
                    select(DocumentRecord.id).where(DocumentRecord.storage_key == key)
                )
                is not None
            )


class DocumentCleanup:
    def __init__(
        self, database: Database, storage: PrivateStorage, now: Callable[[], datetime]
    ) -> None:
        self.database, self.storage, self.now = database, storage, now

    def execute(self, task: Task) -> bool:
        try:
            with self.database.transaction() as session:
                key = session.scalar(
                    select(DocumentRecord.storage_key)
                    .join(DocumentDeletionRecord)
                    .where(
                        DocumentRecord.id == task.payload_ref,
                        DocumentRecord.id == task.key.subject_id,
                        DocumentRecord.deleted_at.is_not(None),
                        DocumentDeletionRecord.step == task.key.subject_version,
                        DocumentDeletionRecord.completed_at.is_(None),
                        select(TaskRecord.id)
                        .where(
                            TaskRecord.id == task.id,
                            TaskRecord.status == "running",
                            TaskRecord.generation == task.generation,
                            TaskRecord.lease_token == task.lease_token,
                            TaskRecord.lease_expires_at > self.now(),
                        )
                        .exists(),
                    )
                )
            if key is None:
                return False
            self.storage.discard(key)
            return True
        except OSError, SQLAlchemyError:
            raise RetryableTaskError() from None

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        if result is not True:
            return
        phase = session.scalar(
            select(DocumentDeletionRecord)
            .join(DocumentRecord)
            .where(
                DocumentRecord.id == task.payload_ref,
                DocumentRecord.id == task.key.subject_id,
                DocumentRecord.deleted_at.is_not(None),
                DocumentDeletionRecord.step == task.key.subject_version,
                DocumentDeletionRecord.completed_at.is_(None),
            )
            .with_for_update()
        )
        if phase is None:
            return
        parsed = select(ParsedDocumentRecord.id).where(
            ParsedDocumentRecord.document_id == task.payload_ref
        )
        proposals = (
            select(ProposalRecord.id)
            .join(ExtractionRecord)
            .where(ExtractionRecord.parsed_id.in_(parsed))
            .order_by(ProposalRecord.id)
            .limit(100)
        )
        session.execute(delete(ProposalRecord).where(ProposalRecord.id.in_(proposals)))
        if (
            session.scalar(
                select(ProposalRecord.id)
                .join(ExtractionRecord)
                .where(ExtractionRecord.parsed_id.in_(parsed))
                .limit(1)
            )
            is None
        ):
            session.execute(
                delete(ParsedDocumentRecord).where(
                    ParsedDocumentRecord.id.in_(
                        parsed.order_by(ParsedDocumentRecord.id).limit(10)
                    )
                )
            )
        if session.scalar(parsed.limit(1)) is None:
            phase.completed_at = now
        else:
            phase.step += 1
            PostgresTaskQueue(session).enqueue(
                NewTask(
                    TaskKey("document_cleanup", task.payload_ref, phase.step),
                    task.payload_ref,
                    now,
                    max_attempts=5,
                ),
                now,
            )


class LocalStorage(PrivateStorage):
    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            raise ConfigurationError("Document storage must be a private directory.")
        self.root = root

    def path(self, key: str, suffix: str = ".blob") -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("Invalid storage key.")
        return self.root / (key + suffix)

    def stage(self) -> tuple[str, BinaryIO]:
        key = uuid4().hex
        file = os.fdopen(
            os.open(self.path(key, ".part"), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600),
            "w+b",
        )
        try:
            fcntl.flock(file, fcntl.LOCK_EX)
            return key, file
        except OSError:
            file.close()
            self.discard(key)
            raise

    def publish(self, key: str, file: BinaryIO) -> None:
        file.flush()
        os.fsync(file.fileno())
        os.replace(self.path(key, ".part"), self.path(key))
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def discard(self, key: str) -> None:
        for suffix in (".part", ".blob"):
            self.path(key, suffix).unlink(missing_ok=True)
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def open(self, key: str) -> BinaryIO:
        return cast(
            BinaryIO,
            os.fdopen(os.open(self.path(key), os.O_RDONLY | os.O_NOFOLLOW), "rb"),
        )

    def reap(self, records: Documents, now: datetime) -> None:
        # A live publisher holds this inode lock through the database commit.
        # The sweep never removes a committed reference, including tombstones.
        with os.scandir(self.root) as entries:
            for entry in entries:
                if not re.fullmatch(r"[a-f0-9]{32}\.(part|blob)", entry.name):
                    continue
                try:
                    with os.fdopen(
                        os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW), "rb"
                    ) as file:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if now.timestamp() - os.fstat(
                            file.fileno()
                        ).st_mtime >= 3600 and not records.referenced(entry.name[:32]):
                            Path(entry.path).unlink(missing_ok=True)
                except FileNotFoundError, BlockingIOError:
                    continue


def main() -> None:
    settings = load_storage_settings()
    if settings.root is None:
        raise ConfigurationError("Configure document storage before sweeping.")
    database = Database(load_settings())
    try:
        LocalStorage(settings.root).reap(
            PostgresDocuments(database, settings, lambda: datetime.now(UTC)),
            datetime.now(UTC),
        )
    finally:
        database.close()


if __name__ == "__main__":
    main()
