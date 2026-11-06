from hashlib import sha256
from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_CHARACTERS = 100000
MAX_PARAGRAPHS = 2000


def digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def paragraph_id(snapshot: UUID, version: str, index: int, text: str) -> UUID:
    return uuid5(snapshot, f"{version}:{index}:{digest(text)}")


class Paragraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    start: int = Field(ge=0, le=MAX_CHARACTERS)
    end: int = Field(ge=0, le=MAX_CHARACTERS)
    text: str = Field(min_length=1, max_length=MAX_CHARACTERS, repr=False)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Preparation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    snapshot_id: UUID
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str = Field(min_length=1, max_length=120)
    state: Literal["uncertain", "empty", "unsupported", "non_job"]
    reason: (
        Literal[
            "http_status",
            "content_type",
            "limits",
            "encoding",
            "malformed",
            "non_job_marker",
        ]
        | None
    ) = None
    language: str | None = Field(
        default=None, max_length=80, pattern=r"^[^\x00-\x1f]*$"
    )
    paragraphs: tuple[Paragraph, ...] = Field(default=(), max_length=MAX_PARAGRAPHS)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def text(self) -> str:
        return "\n\n".join(paragraph.text for paragraph in self.paragraphs)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (
            len(self.text) > MAX_CHARACTERS
            or "\x00" in self.text
            or digest(self.text) != self.content_hash
            or bool(self.paragraphs) != (self.state in {"uncertain", "non_job"})
        ):
            raise ValueError("Invalid preparation content.")
        offset = 0
        for index, paragraph in enumerate(self.paragraphs):
            if (
                paragraph.start != offset
                or paragraph.end != offset + len(paragraph.text)
                or paragraph.content_hash != digest(paragraph.text)
                or paragraph.id
                != paragraph_id(self.snapshot_id, self.version, index, paragraph.text)
            ):
                raise ValueError("Invalid preparation provenance.")
            offset = paragraph.end + 2
        return self
