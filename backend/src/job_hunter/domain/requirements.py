import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field

from job_hunter.domain.job_metadata import Citation, Metadata, Value, citation
from job_hunter.domain.preparation import Preparation

SUMMARY_LIMIT = 600
REQUIRED_CUE = r"\b(must|required|mandatory|essential)\b"
PREFERRED_CUE = r"\b(preferred|optional|desirable|nice[- ]to[- ]have|not required)\b"
NEGATED_CUE = r"\bnot\s+(required|optional|preferred)\b"


class Candidate(Value):
    kind: Literal["job_requirement", "material", "instruction", "deadline"]
    level: Literal["required", "preferred", "unclear"]
    text: str = Field(min_length=1, max_length=3000, repr=False)
    evidence: list[Citation] = Field(min_length=1, max_length=4)


class Requirement(Candidate):
    id: UUID
    deadline_at: datetime | None


class Requirements(Value):
    metadata_id: UUID
    snapshot_id: UUID
    source_hash: str
    version: str
    state: Literal["ready", "unknown", "unavailable", "failed"]
    reason: str | None
    items: list[Requirement]
    summary: str = Field(max_length=SUMMARY_LIMIT)
    more_requirements: int = Field(ge=0)


def deadline(text: str) -> datetime | None:
    matches = re.findall(
        r"\b\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)\b", text
    )
    if len(matches) != 1:
        return None
    try:
        return datetime.fromisoformat(matches[0]).astimezone(UTC)
    except ValueError, OverflowError:
        return None


def assemble(
    source: Preparation,
    metadata_id: UUID,
    metadata: Metadata,
    version: str,
    candidates: list[Candidate],
    reason: str | None = None,
) -> Requirements:
    if (
        metadata.snapshot_id != source.snapshot_id
        or metadata.source_hash != source.content_hash
    ):
        raise ValueError("Requirements need matching source and metadata revisions.")
    if len(candidates) > 128 or reason is not None and candidates:
        raise ValueError("Invalid requirements result.")
    if source.state != "uncertain" or metadata.state in {"non_job", "failed"}:
        if candidates:
            raise ValueError("Unavailable source cannot publish requirements.")
        reason = "source_unavailable"
    unique: dict[UUID, Requirement] = {}
    negations = tuple(re.finditer(NEGATED_CUE, source.text, re.I))
    for candidate in candidates:
        for span in candidate.evidence:
            citation(source, span)
        primary = candidate.evidence[0]
        if candidate.text != primary.quote:
            raise ValueError("Requirement text must copy its primary evidence.")
        before = source.text[max(0, primary.start - 1) : primary.start]
        after = source.text[primary.end : primary.end + 1]
        if re.search(r"[\w+#.-]", before + after):
            raise ValueError("Requirement evidence must contain complete tokens.")
        if any(
            match.start() < primary.end
            and match.end() > primary.start
            and (match.start() < primary.start or match.end() > primary.end)
            for match in negations
        ):
            raise ValueError("Requirement evidence omits part of a negated priority.")
        context = " ".join(span.quote for span in candidate.evidence)
        if not any(
            re.search(cue, candidate.text, re.I)
            for cue in (REQUIRED_CUE, PREFERRED_CUE)
        ):
            context = citation(source, primary) + " " + context
        required = bool(re.search(REQUIRED_CUE, context, re.I))
        preferred = bool(re.search(PREFERRED_CUE, context, re.I))
        if candidate.level != "unclear" and re.search(NEGATED_CUE, context, re.I):
            raise ValueError("Negated priority needs an unclear classification.")
        if (candidate.level == "required" and (not required or preferred)) or (
            candidate.level == "preferred" and (not preferred or required)
        ):
            raise ValueError("Requirement priority is not supported by its evidence.")
        key = uuid5(metadata_id, version + candidate.model_dump_json())
        unique[key] = Requirement(
            **candidate.model_dump(),
            id=key,
            deadline_at=deadline(candidate.text)
            if candidate.kind == "deadline"
            else None,
        )
    items = sorted(
        unique.values(), key=lambda item: (item.evidence[0].start, str(item.id))
    )
    lines: list[str] = []
    for item in sorted(
        items,
        key=lambda item: {"required": 0, "preferred": 1, "unclear": 2}[item.level],
    ):
        line = f"{item.level.capitalize()} ({item.kind.replace('_', ' ')}): {item.text}"
        if len("\n".join([*lines, line])) > SUMMARY_LIMIT:
            break
        lines.append(line)
    state: Literal["ready", "unknown", "unavailable", "failed"] = (
        "ready" if items else "unknown"
    )
    if reason:
        state = "unavailable" if reason == "source_unavailable" else "failed"
    return Requirements(
        metadata_id=metadata_id,
        snapshot_id=source.snapshot_id,
        source_hash=source.content_hash,
        version=version,
        state=state,
        reason=reason,
        items=items,
        summary="\n".join(lines),
        more_requirements=len(items) - len(lines),
    )
