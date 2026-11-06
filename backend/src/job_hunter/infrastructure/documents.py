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
    String,
    UniqueConstraint,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.application.documents import Documents, PrivateStorage
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.documents import PARSER_VERSION, Document, ParseResult
from job_hunter.domain.errors import DomainError
from job_hunter.domain.tasks import NewTask, Task, TaskError, TaskKey
from job_hunter.infrastructure.accounts import UserRecord
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
    ) -> None:
        self.database, self.storage, self.parser, self.now = (
            database,
            storage,
            parser,
            now,
        )

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
            if self.artifact(session, task) is None:
                session.add(
                    ParsedDocumentRecord(
                        id=uuid4(),
                        document_id=row.id,
                        document_version=row.version,
                        parser_version=PARSER_VERSION,
                        source_hash=row.content_hash,
                        content_hash=result.content_hash,
                        data=result.model_dump(mode="json"),
                        created_at=now,
                    )
                )

    def handlers(self) -> dict[str, Handler]:
        def execute(task: Task) -> object:
            try:
                return self.execute(task)
            except SQLAlchemyError:
                raise RetryableTaskError() from None

        return {"document_parse": Handler(execute, self.publish)}


def snapshot(row: DocumentRecord) -> Document:
    return Document.model_validate(row, from_attributes=True)


class PostgresDocuments(Documents):
    def __init__(
        self, database: Database, settings: StorageSettings, now: Callable[[], datetime]
    ) -> None:
        self.database, self.settings, self.now = database, settings, now

    def add(
        self, owner: UUID, key: str, digest: str, name: str, media_type: str, size: int
    ) -> tuple[Document, bool]:
        with self.database.transaction() as session:
            session.scalars(
                select(UserRecord).where(UserRecord.id == owner).with_for_update()
            ).one()
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

    def referenced(self, key: str) -> bool:
        with self.database.transaction() as session:
            return (
                session.scalar(
                    select(DocumentRecord.id).where(DocumentRecord.storage_key == key)
                )
                is not None
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
