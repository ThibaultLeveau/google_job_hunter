import json
from collections.abc import Callable

from job_hunter.application.ai import AI, Accounting, AIError
from job_hunter.application.cleaning import CleanJob
from job_hunter.application.extraction import ExtractProfile, Trace
from job_hunter.application.job_metadata import ExtractMetadata
from job_hunter.application.matching import (
    PROMPT,
    Evaluations,
    aggregate,
    professional_facts,
    validate,
)
from job_hunter.application.requirements import ExtractRequirements
from job_hunter.domain.cleaning import verify
from job_hunter.domain.matching import Score, evidence_ids
from scripts.evaluation_cases import (
    Case,
    identity,
    match_inputs,
    metadata,
    parsed_resume,
    source,
)


def fraction(expected: list[str], found: list[str]) -> float:
    return len(set(expected) & set(found)) / len(set(expected)) if expected else 1.0


def rubric(case: Case, score: Score) -> bool:
    expected = case["expected"]
    return all(
        getattr(score, name) == expected[name]
        for name in (
            "percent",
            "assessed_percent",
            "unknown_count",
            "total_weight",
            "unknown_weight",
            "requirement_count",
        )
    ) and all(
        [getattr(part, attribute) for part in score.components] == expected[label]
        for attribute, label in (
            ("weight", "weights"),
            ("earned_units", "earned"),
            ("possible_units", "possible"),
            ("status", "statuses"),
        )
    )


def evaluate(case: Case, factory: Callable[[Accounting], AI]) -> dict[str, object]:
    checks = {"schema_validity": 1.0, "supported_evidence": 1.0}
    expected = case["expected"]
    reason = None
    try:
        if case["stage"] == "resume":
            result = ExtractProfile(factory, "evaluation-v1").execute(
                parsed_resume(case)
            )
            rows = result.candidates
            fields = [field for row in rows for field in row.fields]
            skills = [str(row.fields[0].value) for row in rows if row.kind == "skill"]
            projects = [
                str(field.value)
                for row in rows
                if row.kind == "project"
                for field in row.fields
                if field.name == "name"
            ]
            checks["labelled_facts"] = float(
                sorted(skills) == sorted(expected["skills"])
                and sorted(projects) == sorted(expected["projects"])
                and all(
                    any(
                        field.name == name and field.value == expected[name]
                        for field in fields
                    )
                    for name in ("company", "title")
                )
                and all(
                    field.value is None
                    for field in fields
                    if field.name in expected["unknown_fields"]
                )
            )
            checks["deduplication"] = float(
                len(rows) == expected["count"]
                and len(skills) == len(set(skills))
                and all(
                    len(row.fields[0].evidence) == expected["skill_evidence"]
                    for row in rows
                    if row.kind == "skill"
                )
            )
            checks["preserved_ambiguity"] = float(
                any(
                    field.name == "start_date"
                    and field.uncertainty == "uncertain"
                    and any(
                        span.quote == expected["uncertain_date"]
                        for span in field.evidence
                    )
                    for field in fields
                )
            )
        elif case["stage"] == "cleaning":
            prepared = source(case)
            clean = CleanJob(factory, "evaluation-v1").execute(prepared).content
            verify(clean, prepared)
            reason = clean.reason
            kept = [
                prepared.text[part.source_start : part.source_end]
                for part in clean.mappings
            ]
            removed = [p.text for p in prepared.paragraphs if p.id in clean.removed]
            checks["labelled_content"] = float(
                kept == expected["preserved"] and removed == expected["removed"]
            )
            checks["required_item_preservation"] = fraction(expected["mandatory"], kept)
        elif case["stage"] == "metadata":
            value = (
                ExtractMetadata(factory, "evaluation-v1").execute(source(case)).content
            )
            reason = value.reason
            checks["labelled_facts"] = float(
                value.fields["title"].status == expected["title_status"]
                and value.work_arrangement == expected["work_arrangement"]
                and [
                    fact.value for fact in value.fields["geographic_eligibility"].values
                ]
                == expected["eligibility"]
                and value.salary_status == expected["salary_status"]
                and len(value.selection.salaries) == expected["salary_count"]
            )
        elif case["stage"] == "requirements":
            prepared = source(case)
            requirements = (
                ExtractRequirements(factory, "evaluation-v1")
                .execute(prepared, identity("metadata"), metadata(prepared))
                .content
            )
            reason = requirements.reason
            actual = [[item.kind, item.level, item.text] for item in requirements.items]
            checks["labelled_requirements"] = float(actual == expected["items"])
            checks["required_item_preservation"] = fraction(
                expected["mandatory"],
                [item.text for item in requirements.items if item.level == "required"],
            )
            checks["deduplication"] = float(
                len(actual) == expected["count"]
                and len({tuple(row) for row in actual}) == len(actual)
            )
        else:
            profile, job = match_inputs(case)
            response = (
                factory(Trace())
                .extract(
                    PROMPT,
                    json.dumps(
                        {
                            "profile_facts": professional_facts(profile),
                            "requirements": [
                                {
                                    "id": str(item.id),
                                    "kind": item.kind,
                                    "level": item.level,
                                    "text": item.text,
                                    "evidence": [
                                        {"id": str(key), **span.model_dump(mode="json")}
                                        for key, span in zip(
                                            evidence_ids(item),
                                            item.evidence,
                                            strict=True,
                                        )
                                    ],
                                }
                                for item in job.requirements.items
                            ],
                            "supplementary_passages": [],
                        },
                        ensure_ascii=False,
                    ),
                    Evaluations,
                    "match-evaluations-v1",
                )
                .value
            )
            validate(response.items, job, profile)
            ordered = {item.requirement_id: item for item in response.items}
            checks["labelled_assessments"] = float(
                [ordered[item.id].status for item in job.requirements.items]
                == expected["statuses"]
                and [
                    sorted(str(key) for key in ordered[item.id].profile_evidence)
                    for item in job.requirements.items
                ]
                == [
                    sorted(str(identity(skill)) for skill in support)
                    for support in expected["support"]
                ]
            )
            checks["rubric_aggregation"] = float(
                rubric(case, aggregate(job.requirements, response.items))
            )
        if reason:
            checks["supported_evidence"] = 0.0
            checks["schema_validity"] = float(reason != "invalid_output")
    except AIError as error:
        checks["schema_validity"] = 0.0
        reason = error.code
    except ValueError:
        checks["supported_evidence"] = 0.0
        reason = "unverified_evidence"
    return {
        "id": case["id"],
        "stage": case["stage"],
        "metrics": checks,
        "passed": all(value == 1.0 for value in checks.values()),
        "reason": reason,
    }
