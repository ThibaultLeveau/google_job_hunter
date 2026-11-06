import re
from decimal import Decimal
from typing import Annotated, Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from job_hunter.domain.preparation import Preparation

FieldName = Literal[
    "title",
    "employer",
    "skills",
    "region",
    "location",
    "employment_type",
    "seniority",
    "language",
    "work_arrangement",
    "geographic_eligibility",
]
FIELDS: tuple[FieldName, ...] = get_args(FieldName)
MULTIPLE = {"skills", "region", "location", "language", "geographic_eligibility"}
Status = Literal["unknown", "known", "ambiguous", "conflicting"]
Arrangement = Literal["remote", "hybrid", "on_site", "unknown", "conflicting"]
ARRANGEMENTS: dict[str, Arrangement] = {
    "remote": "remote",
    "fully remote": "remote",
    "télétravail": "remote",
    "hybrid": "hybrid",
    "hybride": "hybrid",
    "on-site": "on_site",
    "on site": "on_site",
    "onsite": "on_site",
    "sur site": "on_site",
}
PERIODS = {
    "hour": r"hour|hourly|heure",
    "day": r"day|daily|jour",
    "week": r"week|weekly|semaine",
    "month": r"month|monthly|mois",
    "year": r"year|yearly|annual|annually|annuel",
}


class Value(BaseModel):
    model_config = ConfigDict(
        frozen=True, strict=True, extra="forbid", hide_input_in_errors=True
    )


class Citation(Value):
    paragraph_id: UUID = Field(strict=False)
    start: int = Field(ge=0, le=100000)
    end: int = Field(ge=0, le=100000)
    quote: str = Field(min_length=1, max_length=4000, repr=False)


class Fact(Value):
    field: FieldName
    value: str = Field(min_length=1, max_length=300)
    uncertain: bool
    evidence: Citation


Amount = Annotated[
    Decimal,
    Field(strict=False, ge=0, le=1000000000000, max_digits=16, decimal_places=4),
]


class Salary(Value):
    minimum: Amount | None
    maximum: Amount | None
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None
    period: Literal["hour", "day", "week", "month", "year"] | None
    basis: Literal["gross", "net"] | None
    uncertain: bool
    evidence: Citation


class Selection(Value):
    kind: Literal["job", "non_job", "ambiguous"]
    evidence: list[Citation] = Field(max_length=4)
    facts: list[Fact] = Field(max_length=100)
    salaries: list[Salary] = Field(max_length=8)


class FactSet(Value):
    status: Status
    values: list[Fact]


class Metadata(Value):
    snapshot_id: UUID
    source_hash: str
    version: str
    source_language: str | None
    state: Literal["ready", "ambiguous", "non_job", "failed"]
    reason: str | None
    selection: Selection
    fields: dict[FieldName, FactSet]
    work_arrangement: Arrangement
    salary_status: Status


def citation(source: Preparation, evidence: Citation) -> str:
    part = next((p for p in source.paragraphs if p.id == evidence.paragraph_id), None)
    if (
        part is None
        or not part.start <= evidence.start < evidence.end <= part.end
        or source.text[evidence.start : evidence.end] != evidence.quote
    ):
        raise ValueError("Evidence does not match the preserved source.")
    return part.text


def salary(source: Preparation, claim: Salary) -> None:
    citation(source, claim.evidence)
    quote = claim.evidence.quote
    numbers = {
        Decimal(n.replace(",", ""))
        for n in re.findall(r"(?<![\w.,+-])\d+(?:,\d{3})*(?:\.\d+)?(?![\w.,%])", quote)
    }
    if any(
        value is not None and value not in numbers
        for value in (claim.minimum, claim.maximum)
    ):
        raise ValueError("Salary amounts must occur explicitly in the evidence.")
    if (
        claim.minimum is not None
        and claim.maximum is not None
        and claim.minimum > claim.maximum
    ):
        raise ValueError("Salary range is reversed.")
    if claim.currency and not re.search(rf"\b{claim.currency}\b", quote):
        raise ValueError("Currency must be explicit; symbols alone are ambiguous.")
    if claim.period and not re.search(rf"\b(?:{PERIODS[claim.period]})\b", quote, re.I):
        raise ValueError("Salary period must be explicit.")
    if claim.basis and not re.search(rf"\b{claim.basis}\b", quote, re.I):
        raise ValueError("Salary basis must be explicit.")


def materialize(
    source: Preparation, selection: Selection, version: str, reason: str | None = None
) -> Metadata:
    if reason is not None and (
        selection.kind != "ambiguous"
        or selection.facts
        or selection.salaries
        or selection.evidence
    ):
        raise ValueError("Failed extraction cannot publish listing facts.")
    if reason is None and not selection.evidence:
        raise ValueError("Page classification needs source evidence.")
    for evidence in selection.evidence:
        citation(source, evidence)
    for fact in selection.facts:
        citation(source, fact.evidence)
        before = source.text[max(0, fact.evidence.start - 1) : fact.evidence.start]
        after = source.text[fact.evidence.end : fact.evidence.end + 1]
        if fact.value != fact.evidence.quote or re.search(r"[\w+#.-]", before + after):
            raise ValueError("Facts must copy complete source tokens.")
    for claim in selection.salaries:
        salary(source, claim)
    fields: dict[FieldName, FactSet] = {}
    for name in FIELDS:
        values = [f for f in selection.facts if f.field == name]
        distinct = {f.value.casefold() for f in values}
        status: Status = "unknown"
        if values:
            status = (
                "conflicting" if len(distinct) > 1 and name not in MULTIPLE else "known"
            )
            if status != "conflicting" and any(f.uncertain for f in values):
                status = "ambiguous"
        fields[name] = FactSet(status=status, values=values)
    work = fields["work_arrangement"]
    arrangement: Arrangement = "unknown"
    if work.status == "conflicting":
        arrangement = "conflicting"
    elif work.status == "known":
        if any(
            re.search(
                r"\b(not|no|never|except|pas|sans|kein)\b",
                citation(source, fact.evidence),
                re.I,
            )
            for fact in work.values
        ):
            fields["work_arrangement"] = FactSet(status="ambiguous", values=work.values)
        else:
            arrangement = ARRANGEMENTS.get(work.values[0].value.casefold(), "unknown")
    salary_status: Status = "unknown"
    salaries = selection.salaries
    if salaries:
        identities = {
            s.model_dump_json(exclude={"evidence", "uncertain"}) for s in salaries
        }
        salary_status = "conflicting" if len(identities) > 1 else "known"
        if salary_status != "conflicting" and any(
            s.uncertain or s.minimum is None and s.maximum is None for s in salaries
        ):
            salary_status = "ambiguous"
    state: Literal["ready", "ambiguous", "non_job", "failed"] = "ambiguous"
    if reason:
        state = "non_job" if source.state == "non_job" else "failed"
    elif selection.kind == "non_job":
        if selection.facts or salaries:
            raise ValueError("Non-job content cannot publish a listing.")
        state = "non_job"
    elif selection.kind == "job" and fields["title"].status == "known":
        state = "ready"
    return Metadata(
        snapshot_id=source.snapshot_id,
        source_hash=source.content_hash,
        version=version,
        source_language=source.language,
        state=state,
        reason=reason,
        selection=selection,
        fields=fields,
        work_arrangement=arrangement,
        salary_status=salary_status,
    )
