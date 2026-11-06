import json
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.ai import AI, Accounting, Reply
from job_hunter.application.cleaning import Cleaned, CleanJob
from job_hunter.application.crawling import CRAWL
from job_hunter.application.embeddings import EmbeddedBatch, EmbedJobs
from job_hunter.application.job_metadata import ExtractedMetadata, ExtractMetadata
from job_hunter.application.requirements import (
    ExtractedRequirements,
    ExtractRequirements,
)
from job_hunter.application.worker import PermanentTaskError, RetryableTaskError
from job_hunter.domain.chunks import ChunkPolicy, ChunkSet, chunk_text
from job_hunter.domain.preparation import Preparation, digest
from job_hunter.domain.snapshots import FetchedSnapshot
from job_hunter.domain.tasks import NewTask, Task, TaskError, TaskKey
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.ai import AISettings, FixtureProvider, create_ai
from job_hunter.infrastructure.chunks import PostgresChunks, read, source
from job_hunter.infrastructure.cleaning import CleaningRecord, PostgresCleaning
from job_hunter.infrastructure.crawling import PostgresCrawler
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.embeddings import (
    EmbeddingRecord,
    PostgresEmbeddings,
    create_embeddings,
)
from job_hunter.infrastructure.job_metadata import (
    MetadataCallRecord,
    MetadataRecord,
    PostgresMetadata,
)
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    RunJobRecord,
    lock_job_namespace,
)
from job_hunter.infrastructure.match_events import enqueue_scan
from job_hunter.infrastructure.preparation import (
    PREPARATION_VERSION,
    PostgresPreparation,
    PreparationRecord,
    prepare,
)
from job_hunter.infrastructure.requirements import (
    PostgresRequirements,
    RequirementsCallRecord,
    RequirementsRecord,
)
from job_hunter.infrastructure.requirements import source as requirements_source
from job_hunter.infrastructure.searches import SavedSearchRecord, SearchRunRecord
from job_hunter.infrastructure.snapshots import PostgresSnapshots, SnapshotRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler


class JobFixture(FixtureProvider):
    async def generate(self, prompt: str, source: str, schema: dict[str, Any]) -> Reply:
        if "ordered" in schema["properties"]:
            parts = json.loads(json.loads(source)["untrusted_source"])
            return Reply(
                {"ordered": [part["id"] for part in parts], "boilerplate": []}, 0, 0
            )
        if "selection" in schema["properties"]:
            part = json.loads(json.loads(source)["untrusted_source"])[0]
            citation = {
                "paragraph_id": part["id"],
                "start": part["start"],
                "end": part["start"] + len(part["text"][:200]),
                "quote": part["text"][:200],
            }
            selection = {
                "kind": "ambiguous",
                "evidence": [citation],
                "facts": [],
                "salaries": [],
            }
            return Reply({"selection": selection}, 0, 0)
        return Reply({"items": []}, 0, 0)


def create_pipeline(
    database: Database, crawler: PostgresCrawler, settings: AISettings
) -> PostgresPipeline:
    def factory(accounting: Accounting) -> AI:
        ai = create_ai(settings, accounting, fixture_response="{}")
        if settings.provider == "fixture":
            ai.chat = JobFixture(settings, "{}")
        return ai

    configuration = settings.model_dump_json(
        exclude={"api_key", "environment", "embedding_model"}
    )
    return PostgresPipeline(
        database,
        crawler,
        CleanJob(factory, configuration),
        ExtractMetadata(factory, configuration),
        ExtractRequirements(factory, configuration),
        create_embeddings(database, settings),
    )


@dataclass
class Artifacts:
    prepared: PreparationRecord | None
    cleaned: CleaningRecord | None
    chunks: ChunkSet | None
    metadata: MetadataRecord | None
    requirements: RequirementsRecord | None
    embedded: int


class PostgresPipeline:
    def __init__(
        self,
        database: Database,
        crawler: PostgresCrawler,
        cleaner: CleanJob,
        metadata: ExtractMetadata,
        requirements: ExtractRequirements,
        embeddings: EmbedJobs,
        policy: ChunkPolicy | None = None,
    ) -> None:
        policy = policy or ChunkPolicy()
        self.database, self.crawler, self.policy = database, crawler, policy
        self.cleaner, self.metadata, self.requirements = cleaner, metadata, requirements
        self.embeddings = embeddings
        # Each task kind includes all upstream revisions, but no unrelated branch.
        versions = {"text": PREPARATION_VERSION}
        versions["clean"] = versions["text"] + cleaner.version
        versions["chunks"] = versions["clean"] + policy.version
        versions["embed"] = versions["chunks"] + embeddings.space.model_dump_json()
        versions["metadata"] = versions["text"] + metadata.version
        versions["requirements"] = versions["metadata"] + requirements.version
        self.kinds = {
            stage: f"job_{stage}_{digest(version)[:40]}"
            for stage, version in versions.items()
        }

    def active(
        self, session: Session, snapshot_id: UUID, *, lock: bool = False
    ) -> bool:
        lock_job_namespace(session)
        query = (
            select(RunJobRecord.run_id)
            .join(SearchRunRecord)
            .join(SavedSearchRecord)
            .join(UserRecord, UserRecord.id == SearchRunRecord.owner_id)
            .join(JobSourceRecord, JobSourceRecord.id == RunJobRecord.source_id)
            .where(
                JobSourceRecord.current_snapshot_id == snapshot_id,
                SearchRunRecord.status != "cancelled",
                SavedSearchRecord.deleted_at.is_(None),
                UserRecord.deleted_at.is_(None),
            )
            .limit(1)
        )
        if lock:
            query = query.with_for_update(
                of=(UserRecord, SavedSearchRecord, SearchRunRecord)
            )
        return session.scalar(query) is not None

    def artifacts(self, session: Session, snapshot_id: UUID) -> Artifacts:
        row = session.execute(
            select(
                PreparationRecord, CleaningRecord, MetadataRecord, RequirementsRecord
            )
            .outerjoin(
                CleaningRecord,
                and_(
                    CleaningRecord.preparation_id == PreparationRecord.id,
                    CleaningRecord.version == self.cleaner.version,
                ),
            )
            .outerjoin(
                MetadataRecord,
                and_(
                    MetadataRecord.preparation_id == PreparationRecord.id,
                    MetadataRecord.version == self.metadata.version,
                ),
            )
            .outerjoin(
                RequirementsRecord,
                and_(
                    RequirementsRecord.metadata_id == MetadataRecord.id,
                    RequirementsRecord.version == self.requirements.version,
                ),
            )
            .where(
                PreparationRecord.snapshot_id == snapshot_id,
                PreparationRecord.version == PREPARATION_VERSION,
            )
        ).one_or_none()
        prepared, cleaned, metadata, requirements = (
            row if row is not None else (None, None, None, None)
        )
        chunks = read(session, cleaned.id, self.policy.version) if cleaned else None
        embedded = (
            session.scalar(
                select(func.count())
                .select_from(EmbeddingRecord)
                .where(
                    EmbeddingRecord.chunk_id.in_([chunk.id for chunk in chunks.chunks]),
                    EmbeddingRecord.space_id == self.embeddings.space.id,
                )
            )
            if chunks and chunks.chunks
            else 0
        )
        return Artifacts(
            prepared, cleaned, chunks, metadata, requirements, embedded or 0
        )

    def enqueue(self, session: Session, snapshot_id: UUID, now: datetime) -> None:
        if not self.active(session, snapshot_id):
            return
        artifacts = self.artifacts(session, snapshot_id)
        missing = []
        if artifacts.prepared is None:
            missing.append(("text", 1))
        elif artifacts.prepared.payload["state"] == "uncertain":
            if artifacts.cleaned is None:
                missing.append(("clean", 1))
            elif artifacts.chunks is None:
                missing.append(("chunks", 1))
            elif artifacts.embedded < len(artifacts.chunks.chunks):
                missing.append(("embed", artifacts.embedded + 1))
            if artifacts.metadata is None:
                missing.append(("metadata", 1))
            elif (
                artifacts.metadata.payload["state"] in {"ready", "ambiguous"}
                and artifacts.requirements is None
            ):
                missing.append(("requirements", 1))
        if (
            artifacts.chunks
            and artifacts.requirements
            and artifacts.requirements.payload["state"] in {"ready", "unknown"}
        ):
            source = session.get(SnapshotRecord, snapshot_id)
            assert source is not None
            enqueue_scan(
                session,
                None,
                source.source_id,
                f"ready:{artifacts.requirements.id}:{artifacts.chunks.content_id}:{self.policy.version}",
                now,
            )
        for stage, batch in missing:
            key = TaskKey(self.kinds[stage], snapshot_id, batch)
            existing = session.scalar(
                select(TaskRecord.id).filter_by(
                    kind=key.kind, subject_id=snapshot_id, subject_version=batch
                )
            )
            # Do not wait on another claimed stage while holding reference locks.
            if existing is None:
                PostgresTaskQueue(session).enqueue(
                    NewTask(key, snapshot_id, now, 3), now
                )

    def backfill(self, after: UUID | None, now: datetime) -> UUID | None:
        with self.database.transaction() as session:
            query = select(JobSourceRecord).where(
                JobSourceRecord.current_snapshot_id.is_not(None)
            )
            if after is not None:
                query = query.where(JobSourceRecord.id > after)
            rows = list(session.scalars(query.order_by(JobSourceRecord.id).limit(100)))
            for row in rows:
                assert row.current_snapshot_id is not None
                self.enqueue(session, row.current_snapshot_id, now)
            return rows[-1].id if rows else None

    def execute(self, stage: str, task: Task) -> object:
        try:
            return self.compute(stage, task)
        except SQLAlchemyError:
            raise RetryableTaskError() from None

    def compute(self, stage: str, task: Task) -> object:
        with self.database.transaction() as session:
            self.crawler.live(session, task)
            if not self.active(session, task.payload_ref):
                raise PermanentTaskError()
            artifacts = self.artifacts(session, task.payload_ref)
            snapshot = session.get(SnapshotRecord, task.payload_ref)
            assert snapshot is not None
            fetched = FetchedSnapshot(
                snapshot.requested_url,
                snapshot.final_url,
                snapshot.status,
                snapshot.content_type,
                snapshot.body,
                snapshot.fetched_at,
            )
            prepared = (
                Preparation.model_validate(artifacts.prepared.payload)
                if artifacts.prepared
                else None
            )
            clean = source(session, artifacts.cleaned.id) if artifacts.cleaned else None
            requirement_input = (
                requirements_source(session, artifacts.metadata.id)
                if artifacts.metadata
                else None
            )
        # All parsing and provider calls happen after the transaction has closed.
        if stage == "text":
            return prepare(task.payload_ref, fetched) if prepared is None else None
        if stage == "clean":
            assert prepared is not None
            return self.cleaner.execute(prepared) if artifacts.cleaned is None else None
        if stage == "metadata":
            assert prepared is not None
            return (
                self.metadata.execute(prepared) if artifacts.metadata is None else None
            )
        if stage == "requirements":
            assert artifacts.metadata is not None and requirement_input is not None
            return (
                self.requirements.execute(
                    requirement_input[0], artifacts.metadata.id, requirement_input[1]
                )
                if artifacts.requirements is None
                else None
            )
        if stage == "chunks":
            assert artifacts.cleaned is not None and clean is not None
            return (
                chunk_text(artifacts.cleaned.id, clean, self.policy)
                if artifacts.chunks is None
                else None
            )
        assert stage == "embed" and artifacts.chunks is not None
        pending = self.embeddings.store.pending(artifacts.chunks, self.embeddings.space)
        return self.embeddings.execute(pending) if pending else None

    @staticmethod
    def failure(result: object) -> TaskError | None:
        if (
            isinstance(result, (ExtractedMetadata, ExtractedRequirements))
            and result.content.state == "failed"
        ):
            return TaskError.PERMANENT
        if isinstance(result, EmbeddedBatch) and result.error is not None:
            return TaskError.PERMANENT
        return None

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        if not self.active(session, task.payload_ref, lock=True):
            raise PermanentTaskError()
        artifacts = self.artifacts(session, task.payload_ref)
        if isinstance(result, Preparation):
            PostgresPreparation(self.database).publish(session, result)
        elif isinstance(result, Cleaned):
            assert artifacts.prepared is not None
            PostgresCleaning(self.database, self.cleaner).publish(
                session, artifacts.prepared.id, result
            )
        elif isinstance(result, ExtractedMetadata):
            assert artifacts.prepared is not None
            if result.content.state == "failed":
                for record in result.accounting:
                    call = record["call"]
                    assert isinstance(call, dict)
                    session.add(
                        MetadataCallRecord(
                            id=UUID(str(call["id"])),
                            preparation_id=artifacts.prepared.id,
                            version=self.metadata.version,
                            payload=record,
                        )
                    )
            else:
                PostgresMetadata(self.database, self.metadata).publish(
                    session, artifacts.prepared.id, result
                )
        elif isinstance(result, ExtractedRequirements):
            assert artifacts.metadata is not None
            if result.content.state == "failed":
                for record in result.accounting:
                    call = record["call"]
                    assert isinstance(call, dict)
                    session.add(
                        RequirementsCallRecord(
                            id=UUID(str(call["id"])),
                            metadata_id=artifacts.metadata.id,
                            version=self.requirements.version,
                            payload=record,
                        )
                    )
            else:
                PostgresRequirements(self.database, self.requirements).publish(
                    session, artifacts.metadata.id, result
                )
        elif isinstance(result, ChunkSet):
            PostgresChunks(self.database).publish(result, session)
        elif isinstance(result, EmbeddedBatch):
            PostgresEmbeddings(self.database).publish(
                self.embeddings.space,
                result.chunks,
                result.vectors,
                result.accounting,
                session,
            )
        session.flush()
        self.enqueue(session, task.payload_ref, now)

    def handlers(self) -> dict[str, Handler]:
        fetch = self.crawler.handlers()[CRAWL]

        def fetched(
            session: Session, task: Task, result: object, now: datetime
        ) -> None:
            fetch.publish(session, task, result, now)
            current = PostgresSnapshots(session).resolve(task.key.subject_id)
            source = session.get(JobSourceRecord, current)
            if (
                source is not None
                and source.current_snapshot_id is not None
                and source.crawl_state == "fetched"
            ):
                self.enqueue(session, source.current_snapshot_id, now)

        return {
            CRAWL: Handler(fetch.execute, fetched, fetch.failure, fetch.rejected),
            **{
                kind: Handler(partial(self.execute, stage), self.publish, self.failure)
                for stage, kind in self.kinds.items()
            },
        }

    def status(self, session: Session, snapshot_id: UUID) -> dict[str, object]:
        artifacts = self.artifacts(session, snapshot_id)
        current = self.active(session, snapshot_id)
        records = (
            artifacts.prepared,
            artifacts.cleaned,
            artifacts.chunks,
            None,
            artifacts.metadata,
            artifacts.requirements,
        )
        embedded = artifacts.chunks is not None and artifacts.embedded == len(
            artifacts.chunks.chunks
        )
        stages = {}
        for (stage, kind), artifact in zip(self.kinds.items(), records, strict=True):
            task = session.scalar(
                select(TaskRecord)
                .where(TaskRecord.kind == kind, TaskRecord.subject_id == snapshot_id)
                .order_by(TaskRecord.subject_version.desc())
                .limit(1)
            )
            payload: dict[str, object] = {}
            if isinstance(artifact, CleaningRecord):
                content = artifact.payload["content"]
                assert isinstance(content, dict)
                payload = content
            elif isinstance(
                artifact, (PreparationRecord, MetadataRecord, RequirementsRecord)
            ):
                payload = artifact.payload
            stages[stage] = {
                "revision": kind,
                "snapshot_id": str(snapshot_id),
                "dependency_id": str(artifacts.prepared.id)
                if stage in {"clean", "metadata"} and artifacts.prepared
                else str(artifacts.cleaned.id)
                if stage in {"chunks", "embed"} and artifacts.cleaned
                else str(artifacts.metadata.id)
                if stage == "requirements" and artifacts.metadata
                else str(snapshot_id),
                "state": "stale"
                if not current
                else payload.get("state", "ready")
                if artifact is not None or (stage == "embed" and embedded)
                else task.status
                if task
                else "waiting",
                "error": payload.get("reason") or (task.error_code if task else None),
                "created_at": task.created_at if task else None,
                "updated_at": task.updated_at if task else None,
            }
        content = (
            current
            and artifacts.prepared is not None
            and artifacts.prepared.payload["state"] == "uncertain"
        )
        return {
            "content_ready": content,
            "analysis_ready": content
            and artifacts.requirements is not None
            and artifacts.requirements.payload["state"] in {"ready", "unknown"},
            "embeddings_ready": content
            and artifacts.chunks is not None
            and artifacts.embedded == len(artifacts.chunks.chunks),
            "stages": stages,
        }
