import json
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from job_hunter.application.matching import MatchJob
from job_hunter.application.profiles import ProfileSnapshot
from job_hunter.domain.documents import ParseResult
from job_hunter.domain.extraction import FIELDS
from job_hunter.domain.job_metadata import Citation, Metadata, Selection, materialize
from job_hunter.domain.matching import evidence_ids
from job_hunter.domain.preparation import Preparation, digest
from job_hunter.domain.profiles import ProfileFact, Skill
from job_hunter.domain.requirements import Candidate, assemble
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.infrastructure.parsing import ParseLimits, parse
from job_hunter.infrastructure.preparation import prepare

DATASET = Path(__file__).resolve().parents[1] / "evaluations/synthetic-v1.json"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
Case = dict[str, Any]


def identity(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "job-hunter-evaluation-v1:" + name)


def dataset() -> dict[str, Any]:
    raw = DATASET.read_bytes()
    data: dict[str, Any] = json.loads(raw)
    data["sha256"] = sha256(raw).hexdigest()
    return data


def parsed_resume(case: Case) -> ParseResult:
    return parse(BytesIO(case["source"].encode()), "text/plain", ParseLimits())


def source(case: Case) -> Preparation:
    url = "https://jobs.example.org/evaluation"
    return prepare(
        identity(case["id"]),
        FetchedSnapshot(url, url, 200, "text/plain", case["source"].encode(), NOW),
    )


def cite(prepared: Preparation, quote: str) -> Citation:
    part = next(p for p in prepared.paragraphs if quote in p.text)
    start = part.start + part.text.index(quote)
    return Citation(
        paragraph_id=part.id, start=start, end=start + len(quote), quote=quote
    )


def metadata(prepared: Preparation) -> Metadata:
    return materialize(
        prepared,
        Selection(
            kind="ambiguous",
            evidence=[cite(prepared, prepared.paragraphs[0].text)],
            facts=[],
            salaries=[],
        ),
        digest("evaluation-metadata-v1"),
    )


def requirement_candidates(prepared: Preparation, rows: list[Any]) -> list[Candidate]:
    return [
        Candidate.model_validate(
            {
                "kind": kind,
                "level": level,
                "text": text,
                "evidence": [cite(prepared, text)],
            }
        )
        for kind, level, text in rows
    ]


def match_inputs(case: Case) -> tuple[ProfileSnapshot, MatchJob]:
    prepared = source(case)
    meta = metadata(prepared)
    req = assemble(
        prepared,
        identity("metadata"),
        meta,
        "evaluation-requirements-v1",
        requirement_candidates(prepared, case["requirements"]),
    )
    profile = ProfileSnapshot(
        id=identity("owner"),
        revision=1,
        facts=tuple(
            ProfileFact(
                id=identity(skill),
                data=Skill(name=skill),
                created_at=NOW,
                updated_at=NOW,
            )
            for skill in case["profile"]
        ),
    )
    return profile, MatchJob(
        identity("job"),
        identity("requirements"),
        identity("content"),
        "evaluation-chunks-v1",
        prepared,
        meta,
        req,
    )


def resume_reply(case: Case) -> dict[str, object]:
    parsed = parsed_resume(case)
    fixture, candidates = case["fixture"], []
    offsets: dict[str, int] = {}
    rows = [("skill", {"name": skill}) for skill in fixture["skills"]]
    experience = fixture["experience"]
    rows += [
        ("experience", {"company": experience["company"], "title": experience["title"]})
    ]
    rows += [("project", {"name": name}) for name in fixture["projects"]]
    for kind, values in rows:
        fields = []
        for name in sorted(FIELDS[kind]):
            value = values.get(name)
            quote = (
                experience["uncertain_date"]
                if kind == "experience" and name == "start_date"
                else value
            )
            spans = []
            if quote:
                offset = offsets.get(quote, 0) if kind == "skill" else 0
                start = parsed.text.index(quote, offset)
                offsets[quote] = start + len(quote)
                part = next(p for p in parsed.segments if p.start <= start < p.end)
                spans = [
                    {
                        "locator": part.locator,
                        "start": start,
                        "end": start + len(quote),
                        "quote": quote,
                    }
                ]
            fields.append(
                {
                    "name": name,
                    "value": value,
                    "evidence": spans,
                    "uncertainty": "explicit"
                    if value is not None
                    else "uncertain"
                    if quote
                    else "missing",
                }
            )
        candidates.append({"kind": kind, "fields": fields})
    return {"candidates": candidates}


def fixture_reply(case: Case) -> dict[str, Any]:
    if case["stage"] == "resume":
        return resume_reply(case)
    prepared, fixture = source(case), case["fixture"]
    if case["stage"] == "cleaning":
        return {
            key: [
                str(p.id)
                for p in prepared.paragraphs
                if (p.text in fixture["remove"]) == removed
            ]
            for key, removed in (("ordered", False), ("boilerplate", True))
        }
    if case["stage"] == "metadata":
        return {
            "selection": {
                "kind": "job",
                "evidence": [
                    cite(prepared, prepared.paragraphs[0].text).model_dump(mode="json")
                ],
                "salaries": [],
                "facts": [
                    {
                        "field": field,
                        "value": value,
                        "uncertain": uncertain,
                        "evidence": cite(prepared, value).model_dump(mode="json"),
                    }
                    for field, value, uncertain in fixture["facts"]
                ],
            }
        }
    if case["stage"] == "requirements":
        return {
            "items": [
                row.model_dump(mode="json")
                for row in requirement_candidates(prepared, fixture["items"])
            ]
        }
    _, job = match_inputs(case)
    return {
        "items": [
            {
                "requirement_id": str(req.id),
                "status": status,
                "requirement_evidence": [str(value) for value in evidence_ids(req)],
                "profile_evidence": [str(identity(skill)) for skill in support],
                "rationale": "Synthetic assessment; unknowns remain unassessed.",
            }
            for req, status, support in zip(
                job.requirements.items,
                fixture["statuses"],
                fixture["support"],
                strict=True,
            )
        ]
    }
