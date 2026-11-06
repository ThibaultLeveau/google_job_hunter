from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.domain.preparation import (
    MAX_CHARACTERS,
    MAX_PARAGRAPHS,
    Preparation,
    digest,
)

BOILERPLATE = frozenset({"home", "privacy policy", "cookie policy", "accept cookies"})


class SourceMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paragraph_id: UUID
    source_start: int
    source_end: int
    clean_start: int
    clean_end: int


class CleanText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    snapshot_id: UUID
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    state: Literal["cleaned", "degraded"]
    reason: str | None = Field(default=None, max_length=40)
    text: str = Field(max_length=MAX_CHARACTERS, repr=False)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    mappings: tuple[SourceMapping, ...] = Field(max_length=MAX_PARAGRAPHS)
    removed: tuple[UUID, ...] = Field(max_length=MAX_PARAGRAPHS)


def materialize(
    source: Preparation,
    order: tuple[UUID, ...],
    removed: tuple[UUID, ...],
    version: str,
    reason: str | None = None,
) -> CleanText:
    paragraphs = {paragraph.id: paragraph for paragraph in source.paragraphs}
    excluded = set(removed)
    if reason is not None:
        if order != tuple(paragraphs) or removed:
            raise ValueError("Degraded cleaning must preserve the complete source.")
    elif (
        not order
        or len(set(order + removed)) != len(order + removed)
        or set(order + removed) != set(paragraphs)
        # Moving a requirement across a heading or caveat can change its meaning.
        or order != tuple(key for key in paragraphs if key not in excluded)
        or any(paragraphs[key].text.casefold() not in BOILERPLATE for key in removed)
    ):
        raise ValueError("Unverified paragraph selection.")
    mappings, texts, offset = [], [], 0
    for key in order:
        paragraph = paragraphs[key]
        texts.append(paragraph.text)
        mappings.append(
            SourceMapping(
                paragraph_id=key,
                source_start=paragraph.start,
                source_end=paragraph.end,
                clean_start=offset,
                clean_end=offset + len(paragraph.text),
            )
        )
        offset += len(paragraph.text) + 2
    text = "\n\n".join(texts)
    return CleanText(
        snapshot_id=source.snapshot_id,
        source_hash=source.content_hash,
        version=version,
        state="degraded" if reason else "cleaned",
        reason=reason,
        text=text,
        content_hash=digest(text),
        mappings=tuple(mappings),
        removed=removed,
    )


def verify(result: CleanText, source: Preparation) -> None:
    expected = materialize(
        source,
        tuple(item.paragraph_id for item in result.mappings),
        result.removed,
        result.version,
        result.reason,
    )
    if result != expected:
        raise ValueError("Cleaning does not match preserved source spans.")
