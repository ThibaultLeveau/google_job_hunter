import re
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from job_hunter.domain.documents import TextSegment
from job_hunter.domain.profiles import ProfileValue, Project, calendar_date, normalized

DATED = {"start_date", "end_date", "ongoing", "description"}
FIELDS = {
    "skill": {"name"},
    "experience": DATED | {"company", "title"},
    "project": DATED | {"name", "url"},
}


class Span(ProfileValue):
    locator: str = Field(pattern=r"^(page|paragraph):[1-9][0-9]*$", max_length=32)
    start: int = Field(ge=0, le=1000000)
    end: int = Field(ge=0, le=1000000)
    quote: str = Field(min_length=1, max_length=4000)


class ProposedField(ProfileValue):
    name: Literal[
        "name",
        "company",
        "title",
        "start_date",
        "end_date",
        "ongoing",
        "description",
        "url",
    ]
    value: Annotated[str, Field(max_length=4000)] | bool | None
    uncertainty: Literal["explicit", "uncertain", "missing"]
    evidence: list[Span] = Field(max_length=128)


class Candidate(ProfileValue):
    kind: Literal["skill", "experience", "project"]
    fields: list[ProposedField] = Field(min_length=1, max_length=7)


def complete_token(text: str, start: int, end: int) -> bool:
    before, after = text[max(0, start - 1) : start], text[end : end + 1]
    if after == "." and (end + 1 == len(text) or text[end + 1].isspace()):
        after = ""
    return re.search(r"[\w+#.-]", before + after) is None


def locate_evidence(candidate: Candidate, window: list[TextSegment]) -> Candidate:
    fields = []
    for field in candidate.fields:
        evidence = []
        for span in field.evidence:
            parts = [part for part in window if part.locator == span.locator]
            if any(
                part.start <= span.start < span.end <= part.end
                and part.text[span.start - part.start : span.end - part.start]
                == span.quote
                for part in parts
            ):
                evidence.append(span)
                continue
            positions: set[int] = set()
            for part in parts:
                offset = part.text.find(span.quote)
                while offset != -1:
                    end = offset + len(span.quote)
                    if field.name == "description" or complete_token(
                        part.text, offset, end
                    ):
                        positions.add(part.start + offset)
                    offset = part.text.find(span.quote, offset + 1)
            if not positions or (len(positions) != 1 and candidate.kind != "skill"):
                raise ValueError("Evidence has no unique match in the supplied window.")
            evidence.extend(
                span.model_copy(update={"start": start, "end": start + len(span.quote)})
                for start in sorted(positions)
            )
        resolved = ProposedField.model_validate(
            {**field.model_dump(), "evidence": evidence}
        )
        if (
            field.name in {"start_date", "end_date"}
            and field.value is not None
            and evidence
        ):
            try:
                calendar_date(field.value)
                if any(
                    normalized(str(field.value)) != normalized(span.quote)
                    for span in evidence
                ):
                    raise ValueError("Unsupported date precision.")
            except ValueError:
                resolved = resolved.model_copy(
                    update={"value": None, "uncertainty": "uncertain"}
                )
        fields.append(resolved)
    return candidate.model_copy(update={"fields": fields})


def validate_evidence(candidate: Candidate, segments: list[TextSegment]) -> None:
    if not any(field.value is not None for field in candidate.fields):
        raise ValueError("A proposal needs a supported value.")
    if sorted(field.name for field in candidate.fields) != sorted(
        FIELDS[candidate.kind]
    ):
        raise ValueError("Invalid proposal fields.")
    locations = {part.locator: part for part in segments}
    for field in candidate.fields:
        if field.value is None:
            if field.uncertainty == "explicit" or (
                field.uncertainty == "missing" and field.evidence
            ):
                raise ValueError("Invalid unknown field.")
        elif not field.evidence or field.uncertainty == "missing":
            raise ValueError("A proposed value needs evidence.")
        if field.value is not None and (type(field.value) is bool) != (
            field.name == "ongoing"
        ):
            raise ValueError("Invalid field type.")
        if field.value is not None and field.name in {"start_date", "end_date"}:
            calendar_date(field.value)
        if field.name == "url" and isinstance(field.value, str):
            Project(name="validation", url=field.value)
        for span in field.evidence:
            segment = locations.get(span.locator)
            if (
                segment is None
                or not segment.start <= span.start < span.end <= segment.end
                or segment.text[span.start - segment.start : span.end - segment.start]
                != span.quote
            ):
                raise ValueError("Evidence does not match the supplied source.")
            if field.value is not None:
                if not normalized(span.quote) or normalized(
                    str(field.value)
                ) != normalized(span.quote):
                    raise ValueError("Proposed values must be extractive.")
                start, end = span.start - segment.start, span.end - segment.start
                if field.name != "description" and not complete_token(
                    segment.text, start, end
                ):
                    raise ValueError("A partial token is not a supported fact.")


def combine(candidates: list[Candidate]) -> list[Candidate]:
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        candidate = candidate.model_copy(
            update={"fields": sorted(candidate.fields, key=lambda field: field.name)}
        )
        key = candidate.model_dump_json()
        if candidate.kind == "skill":
            key = "skill:" + normalized(str(candidate.fields[0].value))
            if key in unique:
                field, old = candidate.fields[0], unique[key].fields[0]
                evidence = {
                    span.model_dump_json(): span
                    for span in old.evidence + field.evidence
                }
                field = ProposedField(
                    name="name",
                    value=old.value,
                    evidence=[evidence[key] for key in sorted(evidence)],
                    uncertainty="uncertain"
                    if "uncertain" in {old.uncertainty, field.uncertainty}
                    else "explicit",
                )
                candidate = Candidate(kind="skill", fields=[field])
        unique[key] = candidate
    return [unique[key] for key in sorted(unique)]


class Proposal(ProfileValue):
    id: UUID
    candidate: Candidate


class ProposalPage(ProfileValue):
    status: Literal[
        "waiting_for_parse", "unavailable", "pending", "running", "failed", "ready"
    ]
    artifact_id: UUID | None = None
    source_version: int
    language: str | None = None
    count: int = 0
    items: list[Proposal] = []
