from datetime import datetime
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import text

from job_hunter.application.cleaning import CleanJob
from job_hunter.application.job_listing import Cursor, JobPage, JobQuery, JobSummary
from job_hunter.application.job_metadata import ExtractMetadata
from job_hunter.application.requirements import ExtractRequirements
from job_hunter.domain.chunks import ChunkPolicy
from job_hunter.domain.errors import DomainError
from job_hunter.domain.preparation import digest
from job_hunter.infrastructure.ai import AISettings, create_ai
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import create_embeddings
from job_hunter.infrastructure.matching import create_matcher
from job_hunter.infrastructure.preparation import PREPARATION_VERSION

BASE = """
WITH owned AS (
    SELECT r.source_id, min(r.discovered_at) AS discovered_at
    FROM run_jobs r JOIN search_runs s ON s.id=r.run_id
    JOIN saved_searches q ON q.id=s.search_id
    JOIN users u ON u.id=s.owner_id
    WHERE s.owner_id=:owner AND u.deleted_at IS NULL AND q.deleted_at IS NULL
      AND s.status!='cancelled'
      AND (CAST(:run AS uuid) IS NULL OR s.id=CAST(:run AS uuid))
    GROUP BY r.source_id
), facts AS (
    SELECT j.id, j.primary_url AS source_url, o.discovered_at,
        j.last_fetched_at AS fetched_at, j.crawl_observed_at AS observed_at,
        j.crawl_state AS fetch_status, m.payload AS metadata,
        CASE WHEN prep.payload->>'state'='non_job' THEN 'non_job'
            WHEN prep.payload->>'state' IN ('empty','unsupported') THEN 'unavailable'
            WHEN req.payload->>'state' IN ('ready','unknown','failed','unavailable')
            THEN req.payload->>'state'
            WHEN m.payload->>'state' IN ('failed','non_job') THEN m.payload->>'state'
            WHEN EXISTS (SELECT 1 FROM tasks t WHERE t.subject_id=j.current_snapshot_id
                AND t.kind=ANY(:analysis_kinds) AND t.status IN ('failed','cancelled'))
                THEN 'failed' ELSE 'pending' END AS analysis_status,
        CASE WHEN mt.status IN ('failed','cancelled') THEN 'failed'
            WHEN a.match_id IS NOT NULL THEN 'current'
            WHEN history.created_at IS NOT NULL THEN 'stale'
            ELSE 'pending' END AS match_status,
        (a.match_id IS NULL AND coalesce(mt.status,'') NOT IN ('failed','cancelled'))
            AS match_pending,
        (a.match_id IS NULL AND history.created_at IS NOT NULL) AS match_stale,
        coalesce(a.payload->'score',history.score) AS score,
        coalesce(a.created_at,history.created_at) AS evaluated_at
    FROM owned o JOIN job_sources j ON j.id=o.source_id
    LEFT JOIN profiles p ON p.owner_id=:owner
    LEFT JOIN job_preparations prep ON prep.snapshot_id=j.current_snapshot_id
        AND prep.version=:preparation
    LEFT JOIN job_metadata m ON m.preparation_id=prep.id AND m.version=:metadata_version
    LEFT JOIN job_cleanings c ON c.preparation_id=prep.id AND c.version=:cleaning
    LEFT JOIN job_requirements req ON req.metadata_id=m.id AND req.version=:requirements
    LEFT JOIN job_chunk_batches b ON b.content_id=c.id AND b.policy_version=:chunks
    LEFT JOIN private_matches i ON i.owner_id=:owner AND i.profile_revision=p.revision
        AND i.requirements_id=req.id AND i.content_id=c.id
        AND i.chunk_version=b.policy_version AND i.evaluator_version=:evaluator
        AND req.payload->>'state' IN ('ready','unknown')
    LEFT JOIN LATERAL (SELECT status, subject_version FROM tasks
        WHERE kind='match_evaluate' AND subject_id=i.id
        ORDER BY subject_version DESC LIMIT 1) mt ON true
    LEFT JOIN private_match_artifacts a ON a.match_id=i.id
        AND a.generation=mt.subject_version
    LEFT JOIN LATERAL (
        SELECT h.payload->'score' AS score, h.created_at
        FROM private_matches hi JOIN private_match_artifacts h ON h.match_id=hi.id
        WHERE hi.owner_id=:owner AND (hi.job_id=j.id OR hi.job_id IN (
            SELECT old_id FROM job_source_merges WHERE source_id=j.id))
        ORDER BY h.created_at DESC, h.generation DESC, h.match_id DESC LIMIT 1
    ) history ON true
), listed AS (
    SELECT *, CASE WHEN metadata#>>'{fields,title,status}'='known'
        THEN lower(metadata#>>'{fields,title,values,0,value}') END AS sort_title,
        CASE WHEN match_status='current' THEN (score->>'percent')::numeric END
            AS sort_score
    FROM facts
)
SELECT * FROM listed WHERE {predicates}
ORDER BY {ordering}, id {direction} LIMIT :limit
"""


class PostgresJobListing:
    def __init__(self, database: Database, versions: dict[str, Any]) -> None:
        self.database, self.versions = database, versions

    def list(self, owner: UUID, query: JobQuery) -> JobPage:
        cursor = Cursor.read(query, owner)
        params: dict[str, Any] = {
            **self.versions,
            "owner": owner,
            "run": query.run_id,
            "limit": query.limit + 1,
        }
        predicates = []
        if query.keyword is not None:
            params["keyword"] = (
                "%"
                + query.keyword.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
                + "%"
            )
            predicates.append("""EXISTS (SELECT 1 FROM jsonb_each(metadata->'fields') f,
                jsonb_array_elements(f.value->'values') v
                WHERE f.key IN ('title','employer','skills','region','location')
                AND f.value->>'status'='known' AND v->>'value' ILIKE :keyword)""")
        if query.region is not None:
            params["region"], params["unknown_region"] = (
                query.region.strip(),
                query.include_unknown_region,
            )
            predicates.append("""((metadata#>>'{fields,region,status}'='known'
                AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(metadata#>'{fields,region,values}') v
                WHERE lower(v->>'value')=lower(:region))) OR (:unknown_region AND
                coalesce(metadata#>>'{fields,region,status}','unknown')!='known'))""")
        if query.remote is not None:
            params["remote"], params["unknown_remote"] = (
                query.remote,
                query.include_unknown_remote,
            )
            predicates.append("""(metadata->>'work_arrangement'=:remote
                OR (:unknown_remote AND coalesce(metadata->>'work_arrangement',
                    'unknown') IN ('unknown','conflicting')))""")
        if query.match_status is not None:
            params["match_status"] = query.match_status
            predicates.append(
                "score->>'percent' IS NULL"
                if query.match_status == "unknown"
                else "match_status=:match_status"
            )
        if query.currency is not None:
            params.update(
                currency=query.currency,
                period=query.period,
                unknown_salary=query.include_unknown_salary,
            )
            salary = "metadata#>'{selection,salaries,0}'"
            predicates.append(f"""({salary}->>'currency' IS NULL
                OR {salary}->>'currency'=:currency) AND ({salary}->>'period' IS NULL
                OR {salary}->>'period'=:period)""")
            bounds = []
            for name, value, operator in (
                ("minimum", query.salary_min, ">="),
                ("maximum", query.salary_max, "<="),
            ):
                if value is not None:
                    params[name] = value
                    bounds.append(
                        f"(({salary}->>'{name}')::numeric {operator} :{name} OR "
                        f"(:unknown_salary AND {salary}->>'{name}' IS NULL))"
                    )
            predicates.append(f"""((metadata->>'salary_status'='known' AND
                {salary}->>'currency'=:currency AND {salary}->>'period'=:period
                AND {" AND ".join(bounds) or "true"}) OR (:unknown_salary AND (
                coalesce(metadata->>'salary_status','unknown')!='known'
                OR {salary}->>'currency' IS NULL OR {salary}->>'period' IS NULL)))""")
        column, direction = {
            "newest": ("discovered_at", "DESC"),
            "oldest": ("discovered_at", "ASC"),
            "title": ("sort_title", "ASC"),
            "match": ("sort_score", "DESC"),
        }[query.sort]
        operator = "<" if direction == "DESC" else ">"
        if cursor is not None:
            params["after_id"] = cursor.id
            if cursor.value is None:
                predicates.append(f"({column} IS NULL AND id {operator} :after_id)")
            else:
                params["after_value"] = (
                    datetime.fromisoformat(cursor.value)
                    if query.sort in {"newest", "oldest"}
                    and isinstance(cursor.value, str)
                    else cursor.value
                )
                predicates.append(
                    f"({column} IS NULL OR ({column},id) "
                    f"{operator} (:after_value,:after_id))"
                )
        sql = (
            BASE.replace("{predicates}", " AND ".join(predicates) or "true")
            .replace("{ordering}", f"{column} {direction} NULLS LAST")
            .replace("{direction}", direction)
        )
        with self.database.transaction() as session:
            if (
                query.run_id is not None
                and session.scalar(
                    text("""SELECT s.id FROM search_runs s
                JOIN saved_searches q ON q.id=s.search_id
                JOIN users u ON u.id=s.owner_id
                WHERE s.id=:run AND s.owner_id=:owner AND s.status!='cancelled'
                    AND q.deleted_at IS NULL AND u.deleted_at IS NULL"""),
                    params,
                )
                is None
            ):
                raise DomainError("not_found")
            rows = list(session.execute(text(sql), params).mappings())
        next_cursor = None
        if len(rows) > query.limit:
            rows = rows[: query.limit]
            cursor_value = rows[-1][column]
            cursor_value = (
                cursor_value.isoformat()
                if isinstance(cursor_value, datetime)
                else float(cursor_value)
                if query.sort == "match" and cursor_value is not None
                else cursor_value
            )
            next_cursor = Cursor(
                binding=query.binding(owner), id=rows[-1]["id"], value=cursor_value
            ).encode()
        items = []
        for row in rows:
            metadata, score = row["metadata"] or {}, row["score"] or {}
            fields = metadata.get("fields", {})
            facts = {
                name: {
                    "status": fields.get(field, {}).get("status", "unknown"),
                    "values": [
                        v["value"] for v in fields.get(field, {}).get("values", [])
                    ],
                }
                for name, field in (
                    ("title", "title"),
                    ("company", "employer"),
                    ("location", "location"),
                    ("region", "region"),
                )
            }
            items.append(
                JobSummary.model_validate(
                    {
                        **{
                            key: row[key]
                            for key in (
                                "id",
                                "source_url",
                                "discovered_at",
                                "fetched_at",
                                "observed_at",
                                "fetch_status",
                                "analysis_status",
                            )
                        },
                        **facts,
                        "remote": metadata.get("work_arrangement", "unknown"),
                        "salary": {
                            "status": metadata.get("salary_status", "unknown"),
                            "values": [
                                {k: v for k, v in s.items() if k != "evidence"}
                                for s in metadata.get("selection", {}).get(
                                    "salaries", []
                                )
                            ],
                        },
                        "match": {
                            "status": row["match_status"],
                            "pending": row["match_pending"],
                            "stale": row["match_stale"],
                            "score": score.get("percent"),
                            "assessed_percent": score.get("assessed_percent"),
                            "evaluated_at": row["evaluated_at"],
                        },
                    }
                )
            )
        return JobPage(items=items, next_cursor=next_cursor)


def create_listing(database: Database, settings: AISettings) -> PostgresJobListing:
    factory = partial(create_ai, settings)
    configuration = settings.model_dump_json(
        exclude={"api_key", "environment", "embedding_model"}
    )
    metadata, requirements = (
        ExtractMetadata(factory, configuration),
        ExtractRequirements(factory, configuration),
    )
    requirement_revision = PREPARATION_VERSION + metadata.version + requirements.version
    return PostgresJobListing(
        database,
        {
            "preparation": PREPARATION_VERSION,
            "cleaning": CleanJob(factory, configuration).version,
            "metadata_version": metadata.version,
            "requirements": requirements.version,
            "chunks": ChunkPolicy().version,
            "evaluator": create_matcher(
                create_embeddings(database, settings), settings
            ).version,
            "analysis_kinds": [
                f"job_metadata_{digest(PREPARATION_VERSION + metadata.version)[:40]}",
                f"job_requirements_{digest(requirement_revision)[:40]}",
            ],
        },
    )
