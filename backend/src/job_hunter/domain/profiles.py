import hashlib
import json
import unicodedata
from datetime import date, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def calendar_date(value: object) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str) and len(value) == 10 and value[4] == value[7] == "-":
        return date.fromisoformat(value)
    raise ValueError("Use a calendar date or null for unknown.")


Date = Annotated[date, BeforeValidator(calendar_date)]
Label = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(min_length=1, max_length=4000)]


class ProfileValue(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )

    @field_validator("*")
    @classmethod
    def safe_text(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip() or "\x00" in value:
                raise ValueError("Use nonempty text or null for unknown.")
        return value


class Skill(ProfileValue):
    kind: Literal["skill"] = "skill"
    name: Annotated[str, Field(min_length=1, max_length=120)]


class DatedFact(ProfileValue):
    start_date: Date | None = None
    end_date: Date | None = None
    ongoing: bool | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def dates(self) -> Self:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("End date must not precede start date.")
        if self.ongoing is True and self.end_date is not None:
            raise ValueError("An ongoing activity cannot have an end date.")
        return self


class Experience(DatedFact):
    kind: Literal["experience"] = "experience"
    company: Label
    title: Label


class Project(DatedFact):
    kind: Literal["project"] = "project"
    name: Label
    url: Annotated[str, Field(min_length=1, max_length=2048)] | None = None

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str | None) -> str | None:
        if value is not None:
            url = urlsplit(value.strip())
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or any(char.isspace() for char in value.strip())
            ):
                raise ValueError("Use an HTTP or HTTPS URL without credentials.")
            _ = url.port
        return value


FactData = Annotated[Skill | Experience | Project, Field(discriminator="kind")]


def exact_key(fact: FactData) -> str:
    fields = {
        "skill": {"name"},
        "experience": {"company", "title", "start_date", "end_date", "ongoing"},
        "project": {"name", "url", "start_date", "end_date", "ongoing"},
    }[fact.kind]
    values = fact.model_dump(mode="json", include=fields | {"kind"})
    for key, value in values.items():
        if isinstance(value, str):
            values[key] = value.strip() if key == "url" else normalized(value)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def same_content(left: FactData, right: FactData) -> bool:
    return exact_key(left) == exact_key(right) and getattr(
        left, "description", None
    ) == getattr(right, "description", None)


class SourceEvidence(ProfileValue):
    document_id: UUID
    source_version: Annotated[int, Field(ge=1)]
    language: Annotated[str, Field(min_length=1, max_length=80)] | None
    locator: Label
    quote: Description


class ProfileFact(ProfileValue):
    id: UUID
    data: FactData
    created_at: datetime
    updated_at: datetime
    # Manual facts have no document evidence; extraction owns future joins.
    source_associations: tuple[SourceEvidence, ...] = ()
