import json
from collections.abc import Callable, Iterator
from datetime import datetime
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session

from job_hunter.application.exports import (
    ExportBudget,
    ExportDiscovery,
    ExportLimits,
    ExportMatch,
    ExportRun,
    ExportSearch,
    PersonalExport,
)
from job_hunter.application.job_details import EvaluatedProfileFact
from job_hunter.application.profiles import ProfileSnapshot
from job_hunter.application.reauth import ReauthenticationRequired, is_recent
from job_hunter.application.searches import RunCounters, RunSnapshot, SearchWrite
from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.matching import Match
from job_hunter.infrastructure.accounts import SessionRecord, UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import DocumentRecord, snapshot
from job_hunter.infrastructure.jobs import RunJobRecord
from job_hunter.infrastructure.matches import MatchArtifactRecord, MatchIdentityRecord
from job_hunter.infrastructure.profiles import (
    FactSourceRecord,
    ProfileFactRecord,
    ProfileRecord,
    UnavailableSourceRecord,
    view,
)
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord


def rows[T](session: Session, query: Select[T], budget: ExportBudget) -> Iterator[T]:
    for row in session.scalars(
        query.limit(budget.limits.records - budget.records + 1)
    ).yield_per(1):
        budget.count()
        yield row


class PostgresExports:
    def __init__(
        self,
        database: Database,
        now: Callable[[], datetime],
        seconds: int,
        limits: ExportLimits | None = None,
    ) -> None:
        self.database, self.now, self.seconds, self.limits = (
            database,
            now,
            seconds,
            limits or ExportLimits(),
        )

    def read(self, owner: UUID, session_digest: str) -> bytes:
        budget = ExportBudget(self.limits)
        with self.database.transaction() as session:
            session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            now = self.now()
            proof = session.scalar(
                select(SessionRecord)
                .join(UserRecord)
                .where(
                    SessionRecord.user_id == owner,
                    SessionRecord.token_hash == session_digest,
                    SessionRecord.revoked_at.is_(None),
                    SessionRecord.expires_at > now,
                    UserRecord.deleted_at.is_(None),
                )
            )
            if proof is None:
                raise Unauthenticated()
            if not is_recent(proof.authenticated_at, now, self.seconds):
                raise ReauthenticationRequired("export_data")
            # Bound nested provenance before the existing fact projection loads it.
            for source in (FactSourceRecord, UnavailableSourceRecord):
                for _ in rows(
                    session,
                    select(source.fact_id)
                    .join(ProfileFactRecord)
                    .where(ProfileFactRecord.owner_id == owner),
                    budget,
                ):
                    pass
            revision = session.scalar(
                select(ProfileRecord.revision).where(ProfileRecord.owner_id == owner)
            )
            facts = tuple(
                budget.keep(view(row, session))
                for row in rows(
                    session,
                    select(ProfileFactRecord)
                    .where(ProfileFactRecord.owner_id == owner)
                    .order_by(ProfileFactRecord.created_at, ProfileFactRecord.id),
                    budget,
                )
            )
            documents = [
                budget.keep(snapshot(row))
                for row in rows(
                    session,
                    select(DocumentRecord)
                    .where(DocumentRecord.owner_id == owner)
                    .order_by(DocumentRecord.created_at, DocumentRecord.id),
                    budget,
                )
            ]
            searches = [
                budget.keep(ExportSearch.model_validate(row, from_attributes=True))
                for row in rows(
                    session,
                    select(SavedSearchRecord)
                    .where(SavedSearchRecord.owner_id == owner)
                    .order_by(SavedSearchRecord.created_at, SavedSearchRecord.id),
                    budget,
                )
            ]
            runs = []
            for row in rows(
                session,
                select(SearchRunRecord)
                .where(SearchRunRecord.owner_id == owner)
                .order_by(SearchRunRecord.created_at, SearchRunRecord.id),
                budget,
            ):
                frozen = RunSnapshot.model_validate(row.snapshot)
                runs.append(
                    budget.keep(
                        ExportRun(
                            id=row.id,
                            search_id=row.search_id,
                            search_revision=row.search_revision,
                            definition=SearchWrite(
                                name=frozen.name, definition=frozen.definition
                            ),
                            query=frozen.query,
                            status=row.status,
                            counters=RunCounters.model_validate(
                                row, from_attributes=True
                            ),
                            created_at=row.created_at,
                            updated_at=row.updated_at,
                        )
                    )
                )
            discoveries = [
                budget.keep(
                    ExportDiscovery(
                        run_id=row.run_id,
                        job_id=row.source_id,
                        original_url=row.original_url,
                        rank=row.rank,
                        discovered_at=row.discovered_at,
                    )
                )
                for row in rows(
                    session,
                    select(RunJobRecord)
                    .join(SearchRunRecord)
                    .where(SearchRunRecord.owner_id == owner)
                    .order_by(
                        RunJobRecord.run_id, RunJobRecord.rank, RunJobRecord.source_id
                    ),
                    budget,
                )
            ]
            artifacts = (
                select(MatchArtifactRecord)
                .join(MatchIdentityRecord)
                .where(MatchIdentityRecord.owner_id == owner)
                .order_by(MatchArtifactRecord.match_id, MatchArtifactRecord.generation)
            )
            # Historical professional projections can grow with the user's profile.
            for count in session.scalars(
                artifacts.with_only_columns(
                    func.jsonb_array_length(MatchArtifactRecord.profile_facts)
                ).limit(self.limits.records + 1)
            ).yield_per(1):
                budget.count(count)
            matches = []
            for artifact in rows(session, artifacts, budget):
                result = Match.model_validate_json(json.dumps(artifact.payload))
                if result.owner != owner:
                    raise ValueError("Invalid export provenance.")
                matches.append(
                    budget.keep(
                        ExportMatch(
                            id=artifact.match_id,
                            generation=artifact.generation,
                            created_at=artifact.created_at,
                            result=result,
                            profile_facts=TypeAdapter(
                                list[EvaluatedProfileFact]
                            ).validate_python(artifact.profile_facts),
                        )
                    )
                )
            return budget.finish(
                PersonalExport(
                    exported_at=now,
                    owner_id=owner,
                    profile=ProfileSnapshot(
                        id=owner, revision=revision or 0, facts=facts
                    ),
                    documents=documents,
                    saved_searches=searches,
                    runs=runs,
                    discoveries=discoveries,
                    matches=matches,
                )
            )
