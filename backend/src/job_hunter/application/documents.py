from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import BinaryIO, Literal, Protocol
from uuid import UUID

from job_hunter.domain.documents import (
    Document,
    UploadTooLarge,
    display_name,
    validate_content,
)
from job_hunter.domain.errors import DomainError
from job_hunter.domain.profiles import ProfileValue


class PrivateStorage(Protocol):
    def stage(self) -> tuple[str, BinaryIO]: ...
    def publish(self, key: str, file: BinaryIO) -> None: ...
    def discard(self, key: str) -> None: ...
    def open(self, key: str) -> BinaryIO: ...


class DeletionState(ProfileValue):
    id: UUID
    status: Literal["deleting", "deleted"]
    error: Literal["cleanup_failed"] | None = None


class Documents(Protocol):
    def status(self, owner: UUID, document: UUID) -> Document | DeletionState: ...
    def delete(self, owner: UUID, document: UUID) -> DeletionState: ...

    def add(
        self, owner: UUID, key: str, digest: str, name: str, media_type: str, size: int
    ) -> tuple[Document, bool]: ...
    def list(self, owner: UUID, offset: int) -> list[Document]: ...
    def get(self, owner: UUID, document: UUID) -> tuple[Document, str]: ...
    def referenced(self, key: str) -> bool: ...


@dataclass
class PendingUpload:
    key: str
    file: BinaryIO
    filename: str
    media_type: str
    limit: int
    size: int = 0
    retain: bool = False

    def write(self, chunk: bytes) -> None:
        if self.size + len(chunk) > self.limit:
            raise UploadTooLarge()
        self.file.write(chunk)
        self.size += len(chunk)


class DocumentUploads:
    def __init__(
        self, records: Documents, storage: PrivateStorage, max_bytes: int
    ) -> None:
        self.records, self.storage, self.max_bytes = records, storage, max_bytes

    @contextmanager
    def receive(self, name: str, media_type: str) -> Iterator[PendingUpload]:
        filename = display_name(name, media_type)
        key, file = self.storage.stage()
        upload = PendingUpload(key, file, filename, media_type, self.max_bytes)
        try:
            yield upload
        finally:
            try:
                if not upload.retain:
                    self.storage.discard(key)
            except OSError:
                pass  # The grace-period orphan sweep retries failed cleanup.
            finally:
                file.close()

    def finish(self, owner: UUID, upload: PendingUpload) -> tuple[Document, bool]:
        validate_content(upload.file, upload.media_type, upload.size)
        upload.file.seek(0)
        digest = sha256()
        while chunk := upload.file.read(65536):
            digest.update(chunk)
        self.storage.publish(upload.key, upload.file)
        # A lost commit acknowledgement must not delete a committed blob.
        upload.retain = True
        try:
            document, created = self.records.add(
                owner,
                upload.key,
                digest.hexdigest(),
                upload.filename,
                upload.media_type,
                upload.size,
            )
        except DomainError:
            upload.retain = False
            raise
        upload.retain = created
        return document, created
