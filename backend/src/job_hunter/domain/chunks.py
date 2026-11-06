from typing import Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from job_hunter.domain.cleaning import CleanText
from job_hunter.domain.preparation import digest


class ChunkPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    revision: Literal[1] = 1
    characters: int = Field(default=1500, ge=128, le=2000)
    token_bound: int = Field(default=6000, ge=512, le=6000)
    overlap: int = Field(default=150, ge=0)

    @model_validator(mode="after")
    def bounded_overlap(self) -> Self:
        if self.overlap > min(self.characters // 4, self.token_bound // 8):
            raise ValueError("Overlap must leave room for forward progress.")
        return self

    @property
    def version(self) -> str:
        return digest(self.model_dump_json())


class ChunkSpan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    paragraph_id: UUID
    source_start: int
    source_end: int
    chunk_start: int
    chunk_end: int


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    id: UUID
    index: int = Field(ge=0, lt=2048)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000, repr=False)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    token_bound: int = Field(ge=1, le=6000)
    spans: tuple[ChunkSpan, ...]


class ChunkSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    content_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy: ChunkPolicy
    chunks: tuple[Chunk, ...] = Field(max_length=2048)


def chunk_text(content_id: UUID, content: CleanText, policy: ChunkPolicy) -> ChunkSet:
    if digest(content.text) != content.content_hash:
        raise ValueError("Invalid cleaned content hash.")
    result: list[Chunk] = []
    start = 0
    while start < len(content.text):
        low, high = start + 1, min(len(content.text), start + policy.characters)
        while low < high:
            middle = (low + high + 1) // 2
            if len(content.text[start:middle].encode()) <= policy.token_bound:
                low = middle
            else:
                high = middle - 1
        end = low
        if end < len(content.text):
            boundaries = [
                part.clean_end
                for part in content.mappings
                # Keep 100,000-character sources within the 2,048-chunk ceiling.
                if start + policy.overlap + 50 <= part.clean_end <= end
            ]
            if boundaries:
                end = boundaries[-1]
        text = content.text[start:end]
        if text.strip():
            spans = []
            for part in content.mappings:
                left, right = max(start, part.clean_start), min(end, part.clean_end)
                if left < right:
                    spans.append(
                        ChunkSpan(
                            paragraph_id=part.paragraph_id,
                            source_start=part.source_start + left - part.clean_start,
                            source_end=part.source_start + right - part.clean_start,
                            chunk_start=left - start,
                            chunk_end=right - start,
                        )
                    )
            index, content_hash = len(result), digest(text)
            result.append(
                Chunk(
                    id=uuid5(content_id, f"{policy.version}:{index}:{content_hash}"),
                    index=index,
                    start=start,
                    end=end,
                    text=text,
                    content_hash=content_hash,
                    # Matches the AI adapter's conservative UTF-8 byte token bound.
                    token_bound=len(text.encode()),
                    spans=tuple(spans),
                )
            )
        if end == len(content.text):
            break
        start = max(start + 1, end - policy.overlap)
    return ChunkSet(
        content_id=content_id,
        content_hash=content.content_hash,
        policy=policy,
        chunks=tuple(result),
    )
