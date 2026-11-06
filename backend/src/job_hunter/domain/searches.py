import re
import unicodedata
from collections.abc import Collection
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Offline SerpApi gl/lr catalogs, verified 2026-11-06; no provider lookup at validation.
COUNTRIES = frozenset(
    "ad ae af ag ai al am an ao aq ar as at au aw az ba bb bd be bf bg bh bi bj "
    "bm bn bo br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv "
    "cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd "
    "ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il "
    "im in io iq ir is it je jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc "
    "li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms mt mu "
    "mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl "
    "pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc sd se sg sh si sj sk sl sm "
    "sn so sr st sv sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw tz ua ug "
    "uk um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw".split()
)
LANGUAGES = frozenset(
    "ar bg ca cs da de el en es et fa fi fr he hi hr hu hy id is it iw ja ko lt "
    "lv nl no pl pt ro ru sk sl sr sv th tl tr uk vi zh-CN zh-TW".split()
)
MAX_QUERY_BYTES = 2048
HINTS = (
    "Google may rewrite or ignore Boolean, phrase, exclusion and site hints. "
    "These hints are not hard filters on extracted job metadata.",
    "Language describes search results; country and location bias the search origin, "
    "not the job location, remote eligibility or right to work.",
    "Recency concerns Google's document dates, not a verified job posting date. "
    "Unspecified parameters use provider defaults; no metadata filtering is applied.",
)


def literal_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    if any(
        unicodedata.category(char)[0] not in "LNMZ" and char not in "+#.,'/-"
        for char in value
    ) or any(char in value.casefold() for char in ("//", "www.")):
        raise ValueError("Use literal words without quotes, operators or URLs.")
    value = " ".join(value.split())
    if not value or value.startswith("-") or not any(c.isalnum() for c in value):
        raise ValueError("Use nonempty literal words.")
    return value


def domain_name(value: str) -> str:
    value = value.lower()
    if not re.fullmatch(
        r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
        value,
    ):
        raise ValueError("Use an exact ASCII domain without scheme, port or path.")
    return value


Term = Annotated[
    str,
    Field(min_length=1, max_length=80),
    AfterValidator(literal_text),
    Field(max_length=80),
]
Group = Annotated[list[Term], Field(min_length=1, max_length=8)]
Domain = Annotated[
    str, Field(min_length=3, max_length=253), AfterValidator(domain_name)
]
Location = Annotated[
    str,
    Field(min_length=1, max_length=120),
    AfterValidator(literal_text),
    Field(max_length=120),
]


class SearchDefinition(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, hide_input_in_errors=True
    )
    groups: Annotated[list[Group], Field(min_length=1, max_length=6)]
    exclusions: Annotated[list[Term], Field(max_length=8)] = Field(default_factory=list)
    sites: Annotated[list[Domain], Field(max_length=4)] = Field(default_factory=list)
    language: Annotated[str, Field(min_length=2, max_length=5)] | None = None
    country: Annotated[str, Field(min_length=2, max_length=2)] | None = None
    location: Location | None = None
    recency_days: Annotated[int, Field(ge=1, le=365)] | None = None

    @field_validator("language")
    @classmethod
    def supported_language(cls, value: str | None) -> str | None:
        if value is not None and value not in LANGUAGES:
            raise ValueError("Use a supported result-language code.")
        return value

    @field_validator("country")
    @classmethod
    def supported_country(cls, value: str | None) -> str | None:
        if value is not None and value not in COUNTRIES:
            raise ValueError("Use a supported country code.")
        return value

    @model_validator(mode="after")
    def bounded_terms(self) -> Self:
        if sum(len(group) for group in self.groups) > 32:
            raise ValueError("Use at most 32 keyword terms before deduplication.")
        if len(query_text(self).encode("utf-8")) > MAX_QUERY_BYTES:
            raise ValueError("The compiled query must fit within 2048 UTF-8 bytes.")
        return self


class SearchPreview(BaseModel):
    query: str
    provider_parameters: dict[str, str]
    hints: tuple[str, ...] = HINTS


def query_text(definition: SearchDefinition) -> str:
    groups = [
        "(" + " OR ".join(f'"{term}"' for term in dict.fromkeys(group)) + ")"
        for group in definition.groups
    ]
    if definition.sites:
        groups.append(
            "("
            + " OR ".join(f"site:{s}" for s in dict.fromkeys(definition.sites))
            + ")"
        )
    query = " AND ".join(groups)
    for term in dict.fromkeys(definition.exclusions):
        query += f' -"{term}"'
    return query


def compile_search(
    definition: SearchDefinition, allowed_domains: Collection[str]
) -> SearchPreview:
    # Revalidate mutable nested lists, including values loaded from a saved snapshot.
    definition = SearchDefinition.model_validate(definition.model_dump())
    if not set(definition.sites) <= set(allowed_domains):
        raise ValueError("Every site must exactly match the configured allowlist.")
    parameters: dict[str, str] = {}
    if definition.language is not None:
        parameters["lr"] = "lang_" + definition.language
    if definition.country is not None:
        parameters["gl"] = definition.country
    if definition.location is not None:
        parameters["location"] = definition.location
    if definition.recency_days is not None:
        parameters["as_qdr"] = f"d{definition.recency_days}"
    return SearchPreview(query=query_text(definition), provider_parameters=parameters)
