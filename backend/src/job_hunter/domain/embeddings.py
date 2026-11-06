import math
from array import array
from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.domain.chunks import Chunk

DIMENSIONS = 1536


class EmbeddingSpace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1, max_length=16)
    model: str = Field(min_length=1, max_length=100)
    revision: str = Field(default="embedding-v1", min_length=1, max_length=80)
    dimensions: Literal[1536] = 1536

    @property
    def id(self) -> UUID:
        return uuid5(NAMESPACE_URL, "job-hunter-embedding:" + self.model_dump_json())


def vector(values: tuple[float, ...]) -> tuple[float, ...]:
    if len(values) != DIMENSIONS:
        raise ValueError("Embedding dimension mismatch.")
    try:
        rounded = tuple(array("f", values))
    except OverflowError, TypeError:
        raise ValueError("Invalid embedding values.") from None
    if not all(math.isfinite(value) for value in rounded) or not any(rounded):
        raise ValueError("Embeddings must be finite, nonzero float32 vectors.")
    # pgvector's float32 distance accumulation can overflow on unnormalized values.
    norm = math.hypot(*rounded)
    return tuple(array("f", (value / norm for value in rounded)))


@dataclass(frozen=True)
class Evidence:
    chunk: Chunk
    distance: float


@dataclass(frozen=True)
class EmbeddingProgress:
    written: int
    remaining: int


@dataclass(frozen=True)
class Retrieval:
    evidence: tuple[Evidence, ...]
    complete: bool
    accounting: tuple[dict[str, object], ...] = ()
