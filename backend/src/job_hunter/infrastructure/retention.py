import argparse
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import ColumnElement, Table, delete, or_, select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.artifact_retention import ArtifactPolicy
from job_hunter.application.retention import (
    PHASES,
    RetentionCheckpoint,
    RetentionPolicy,
)
from job_hunter.domain.tasks import NewTask, TaskKey
from job_hunter.infrastructure.accounts import (
    AuthRateBucket,
    MailDeliveryRecord,
    OidcFlowRecord,
    RecoveryDeliveryRecord,
    RecoveryRecord,
    SessionRecord,
    UserRecord,
    VerificationRecord,
)
from job_hunter.infrastructure.artifact_retention import PostgresArtifactRetention
from job_hunter.infrastructure.crawling import CrawlProgressRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    DocumentDeletionRecord,
    DocumentRecord,
    LocalStorage,
    ParsedDocumentRecord,
    PostgresDocuments,
    load_storage_settings,
)
from job_hunter.infrastructure.job_details import RetryRecord
from job_hunter.infrastructure.match_events import MatchScanRecord
from job_hunter.infrastructure.matches import MatchIdentityRecord
from job_hunter.infrastructure.searches import DISCOVERY, SearchRunRecord
from job_hunter.infrastructure.snapshots import CrawlDeliveryRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.settings import load_settings


def obsolete_tasks() -> ColumnElement[bool]:
    subjects = (
        (
            TaskRecord.kind.in_(("verification_mail", "verification_expiry")),
            VerificationRecord.id,
        ),
        (TaskRecord.kind.in_(("recovery_mail", "recovery_expiry")), RecoveryRecord.id),
        (
            TaskRecord.kind.in_(("document_parse", "document_cleanup")),
            DocumentRecord.id,
        ),
        (
            TaskRecord.kind.startswith("profile_extract_", autoescape=True),
            ParsedDocumentRecord.id,
        ),
        (TaskRecord.kind == "account_cleanup", UserRecord.id),
        (TaskRecord.kind == DISCOVERY, SearchRunRecord.id),
        (TaskRecord.kind == "match_scan", MatchScanRecord.id),
        (TaskRecord.kind == "match_evaluate", MatchIdentityRecord.id),
    )
    return (
        (TaskRecord.subject_id == TaskRecord.payload_ref)
        & or_(
            *(
                kind
                & ~select(identity).where(identity == TaskRecord.payload_ref).exists()
                for kind, identity in subjects
            )
        )
        & ~select(RetryRecord.task_id)
        .where(RetryRecord.task_id == TaskRecord.id)
        .exists()
        & ~select(CrawlProgressRecord.task_id)
        .where(CrawlProgressRecord.task_id == TaskRecord.id)
        .exists()
        & ~select(CrawlDeliveryRecord.task_id)
        .where(CrawlDeliveryRecord.task_id == TaskRecord.id)
        .exists()
    )


def target(cursor: RetentionCheckpoint) -> tuple[Table, ColumnElement[bool]]:
    policy, now = cursor.policy, cursor.started

    def cutoff(hours: int) -> datetime:
        return now - timedelta(hours=hours)

    phase = PHASES[cursor.phase]
    if phase in ("verification_mail", "recovery_mail"):
        model = (
            MailDeliveryRecord
            if phase == "verification_mail"
            else RecoveryDeliveryRecord
        )
        predicate = (
            select(TaskRecord.id)
            .where(
                TaskRecord.kind == phase,
                TaskRecord.payload_ref == model.id,
                TaskRecord.status == "succeeded",
                TaskRecord.updated_at <= cutoff(policy.mail_hours),
            )
            .exists()
        )
        return cast(Table, model.__table__), predicate
    choices = {
        "sessions": (
            SessionRecord,
            or_(
                SessionRecord.expires_at <= cutoff(policy.session_hours),
                SessionRecord.revoked_at <= cutoff(policy.session_hours),
            ),
        ),
        "verification": (
            VerificationRecord,
            VerificationRecord.expires_at <= cutoff(policy.action_hours),
        ),
        "recovery": (
            RecoveryRecord,
            RecoveryRecord.expires_at <= cutoff(policy.action_hours),
        ),
        "oidc": (
            OidcFlowRecord,
            OidcFlowRecord.expires_at <= cutoff(policy.action_hours),
        ),
        "rates": (
            AuthRateBucket,
            AuthRateBucket.expires_at <= cutoff(policy.rate_hours),
        ),
        "tasks": (
            TaskRecord,
            obsolete_tasks()
            & TaskRecord.status.in_(("succeeded", "cancelled", "failed"))
            & TaskRecord.lease_expires_at.is_(None)
            & (TaskRecord.updated_at <= cutoff(policy.task_hours)),
        ),
        "documents": (
            DocumentRecord,
            (DocumentRecord.deleted_at <= cutoff(policy.retry_hours))
            & select(DocumentDeletionRecord.document_id)
            .where(
                DocumentDeletionRecord.document_id == DocumentRecord.id,
                DocumentDeletionRecord.completed_at.is_(None),
            )
            .exists(),
        ),
        "accounts": (UserRecord, UserRecord.deleted_at <= cutoff(policy.retry_hours)),
    }
    selected_model, condition = choices[phase]
    return cast(Table, selected_model.__table__), condition


def recovery(
    session: Session,
    phase: str,
    identity: UUID,
    now: datetime,
    cutoff: datetime,
    dry_run: bool,
) -> bool:
    kind, step, attempts = "account_cleanup", 1, 10
    query = (
        select(TaskRecord)
        .where(TaskRecord.kind == kind, TaskRecord.subject_id == identity)
        .order_by(TaskRecord.subject_version.desc())
    )
    if phase == "documents":
        deletion = session.get(
            DocumentDeletionRecord, identity, with_for_update={"nowait": True}
        )
        if deletion is None or deletion.completed_at is not None:
            return False
        kind, step, attempts = "document_cleanup", deletion.step, 5
        query = select(TaskRecord).where(
            TaskRecord.kind == kind,
            TaskRecord.subject_id == identity,
            TaskRecord.subject_version == step,
        )
    task = session.scalar(query.limit(1).with_for_update(nowait=True))
    if task is not None and (
        task.status != "failed"
        or task.updated_at > cutoff
        or task.payload_ref != identity
    ):
        return False
    if not dry_run:
        queue = PostgresTaskQueue(session)
        if task is None:
            queue.enqueue(
                NewTask(TaskKey(kind, identity, step), identity, now, attempts), now
            )
        else:
            queue.retry(
                TaskKey(kind, identity, task.subject_version), task.generation, now
            )
    return True


class PostgresRetention:
    def __init__(self, database: Database, now: Callable[[], datetime]) -> None:
        self.database, self.now = database, now

    def batch(
        self,
        policy: RetentionPolicy,
        *,
        dry_run: bool = True,
        checkpoint: str | None = None,
    ) -> dict[str, object]:
        now = self.now()
        cursor = RetentionCheckpoint.start(now, policy, dry_run, checkpoint)
        phase = PHASES[cursor.phase]
        counts: dict[str, int] = {}
        scanned = protected = contended = 0
        after = cursor.after
        if phase == "artifacts":
            result = PostgresArtifactRetention(
                self.database, lambda: cursor.started
            ).batch(
                ArtifactPolicy(days=policy.artifact_days, scan_limit=policy.batch_size),
                dry_run=dry_run,
                checkpoint=after,
            )
            counts, scanned, protected, contended = (
                result.counts,
                result.scanned,
                result.protected,
                result.contended,
            )
            counts["oversized_graphs"] = result.oversized
            after = result.checkpoint
        elif phase == "files":
            settings = load_storage_settings()
            if settings.root is None or not settings.root.is_dir():
                raise ValueError("Document storage is required for file retention.")
            counts["orphan_files"], scanned, after = LocalStorage(settings.root).reap(
                PostgresDocuments(self.database, settings, self.now),
                cursor.started,
                limit=policy.batch_size,
                after=after,
                dry_run=dry_run,
                hours=policy.file_hours,
            )
        else:
            table, predicate = target(cursor)
            with self.database.transaction() as session:
                if not session.scalar(
                    text("SELECT pg_try_advisory_xact_lock(817492355)")
                ):
                    return {
                        "phase": phase,
                        "dry_run": dry_run,
                        "counts": {},
                        "scanned": 0,
                        "protected": 0,
                        "contended": 1,
                        "checkpoint": cursor.encode(),
                    }
                query = select(table.c.id).where(predicate)
                if after is not None:
                    query = query.where(table.c.id > UUID(after))
                ids = list(
                    session.scalars(query.order_by(table.c.id).limit(policy.batch_size))
                )
                for identity in ids:
                    try:
                        with session.begin_nested():
                            if (
                                session.scalar(
                                    select(table.c.id)
                                    .where(table.c.id == identity, predicate)
                                    .with_for_update(nowait=True)
                                )
                                is None
                            ):
                                protected += 1
                                continue
                            if phase in ("documents", "accounts"):
                                eligible = recovery(
                                    session,
                                    phase,
                                    identity,
                                    now,
                                    cursor.started
                                    - timedelta(hours=policy.retry_hours),
                                    dry_run,
                                )
                                name = "cleanup_tasks"
                            else:
                                eligible = True
                                if phase in (
                                    "verification",
                                    "recovery",
                                    "verification_mail",
                                    "recovery_mail",
                                ):
                                    tasks = list(
                                        session.scalars(
                                            select(TaskRecord)
                                            .where(TaskRecord.payload_ref == identity)
                                            .limit(101)
                                            .with_for_update(nowait=True)
                                        )
                                    )
                                    eligible = len(tasks) <= 100 and not any(
                                        task.status == "running" for task in tasks
                                    )
                                name = table.name
                                if eligible and not dry_run:
                                    session.execute(
                                        delete(table).where(table.c.id == identity)
                                    )
                            if eligible:
                                counts[name] = counts.get(name, 0) + 1
                            else:
                                protected += 1
                    except DBAPIError as error:
                        if getattr(error.orig, "sqlstate", None) != "55P03":
                            raise
                        contended += 1
                    finally:
                        scanned += 1
                        after = str(identity)
                if len(ids) < policy.batch_size:
                    after = None
        next_cursor = cursor.model_copy(update={"after": after})
        if after is None:
            next_cursor = cursor.model_copy(
                update={"phase": cursor.phase + 1, "after": None}
            )
        return {
            "phase": phase,
            "dry_run": dry_run,
            "counts": counts,
            "scanned": scanned,
            "protected": protected,
            "contended": contended,
            "checkpoint": None
            if next_cursor.phase == len(PHASES)
            else next_cursor.encode(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one bounded retention batch.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--checkpoint")
    options = parser.parse_args()
    database = None
    try:
        policy = RetentionPolicy.model_validate(
            {
                name: int(os.environ[f"JOB_HUNTER_RETENTION_{name.upper()}"])
                for name in RetentionPolicy.model_fields
                if f"JOB_HUNTER_RETENTION_{name.upper()}" in os.environ
            }
        )
        database = Database(load_settings())
        result = PostgresRetention(database, lambda: datetime.now(UTC)).batch(
            policy, dry_run=not options.apply, checkpoint=options.checkpoint
        )
        print(json.dumps(result, sort_keys=True))
    except ValueError:
        parser.error(
            "Invalid retention configuration or checkpoint; "
            "file cleanup requires document storage."
        )
    except OSError, SQLAlchemyError:
        parser.exit(1, "Retention failed; retry the same checkpoint.\n")
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    main()
