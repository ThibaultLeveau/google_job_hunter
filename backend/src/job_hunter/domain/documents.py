import codecs
import re
from datetime import datetime
from hashlib import sha256
from typing import BinaryIO, Literal, Self
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParseError = Literal[
    "invalid_document",
    "encrypted_document",
    "ocr_required",
    "empty_document",
    "limits_exceeded",
    "timed_out",
    "cancelled",
    "storage_unavailable",
    "unsupported_format",
]
PARSER_VERSION = 1


class TextSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    locator: str = Field(pattern=r"^(page|paragraph):[1-9][0-9]*$", max_length=32)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = Field(max_length=1000000, pattern=r"^[^\x00]*$")


class ParseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    segments: tuple[TextSegment, ...] = Field(default=(), max_length=10000)
    language: str | None = Field(default=None, max_length=80, pattern=r"^[^\x00]*$")
    engine: str | None = Field(default=None, max_length=80)
    source_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    error: ParseError | None = None

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments)

    @property
    def content_hash(self) -> str:
        return sha256(self.text.encode()).hexdigest()

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.error is not None:
            if self.segments:
                raise ValueError("Failed parsing cannot publish partial text.")
            return self
        if (
            not self.source_hash
            or not self.engine
            or not self.text.strip()
            or len(self.text) > 1000000
        ):
            raise ValueError("Invalid parsed document.")
        offset, locations = 0, set()
        for segment in self.segments:
            if (
                segment.start != offset
                or segment.end != offset + len(segment.text)
                or segment.locator in locations
            ):
                raise ValueError("Invalid source locations.")
            offset = segment.end + 1
            locations.add(segment.locator)
        return self


TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
}


class InvalidDocument(ValueError):
    pass


class UploadTooLarge(ValueError):
    pass


class Document(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID
    filename: str
    media_type: str
    size_bytes: int
    version: int
    status: Literal["queued", "processing", "ready", "failed", "unsupported"]
    parse_error: ParseError | None = None
    created_at: datetime
    deleted_at: datetime | None


def display_name(name: str, media_type: str) -> str:
    if media_type not in TYPES:
        raise InvalidDocument()
    name = re.sub(r"[^\w .-]", "_", name.replace("\\", "/").rsplit("/", 1)[-1])
    name = name.strip(" .")[:150] or "document"
    return (
        name if name.lower().endswith(TYPES[media_type]) else name + TYPES[media_type]
    )


def validate_content(file: BinaryIO, media_type: str, size: int) -> None:
    file.seek(0)
    signature = file.read(8)
    valid = False
    try:
        if media_type == "application/pdf":
            file.seek(max(0, size - 1024))
            valid = bool(re.match(rb"%PDF-[12]\.[0-9]", signature)) and (
                file.read().rstrip().endswith(b"%%EOF")
            )
        elif media_type == "text/plain":
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            file.seek(0)
            while chunk := file.read(65536):
                text = decoder.decode(chunk)
                if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                    raise InvalidDocument()
            decoder.decode(b"", final=True)
            valid = size > 0 and not signature.startswith((b"%PDF-", b"PK\x03\x04"))
        elif signature.startswith(b"PK\x03\x04"):
            with ZipFile(file) as archive:
                entries = archive.infolist()
                valid = (
                    len(entries) <= 1000
                    and sum(item.file_size for item in entries) <= 50 * 1024 * 1024
                    and all(not item.flag_bits & 1 for item in entries)
                    and {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
                    <= set(archive.namelist())
                )
    except UnicodeError, BadZipFile:
        raise InvalidDocument() from None
    if not valid:
        raise InvalidDocument()
