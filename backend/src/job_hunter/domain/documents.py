import codecs
import re
from datetime import datetime
from typing import BinaryIO, Literal
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from pydantic import BaseModel, ConfigDict

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
    status: Literal["queued", "processing", "ready", "failed"]
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
