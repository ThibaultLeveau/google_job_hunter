from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from job_hunter.domain.tasks import NewTask, TaskKey
from job_hunter.infrastructure.database import Base
from job_hunter.infrastructure.tasks import PostgresTaskQueue

SCAN, EVALUATE = "match_scan", "match_evaluate"


class MatchScanRecord(Base):
    __tablename__ = "match_scans"
    __table_args__ = (
        CheckConstraint(
            "owner_id IS NOT NULL OR job_id IS NOT NULL", name="ck_match_scan_scope"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    owner_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    job_id: Mapped[UUID | None]
    cursor: Mapped[UUID | None]
    step: Mapped[int] = mapped_column(default=1)


class MatchRequestRecord(Base):
    __tablename__ = "match_requests"
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[UUID] = mapped_column(primary_key=True)
    match_id: Mapped[UUID] = mapped_column(
        ForeignKey("private_matches.id", ondelete="CASCADE")
    )
    generation: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def enqueue_scan(
    session: Session, owner: UUID | None, job: UUID | None, revision: str, now: datetime
) -> None:
    if owner is not None and not session.scalar(
        text("""
        SELECT EXISTS(SELECT 1 FROM run_jobs r JOIN search_runs s ON s.id=r.run_id
        JOIN saved_searches ss ON ss.id=s.search_id JOIN users u ON u.id=s.owner_id
        JOIN job_sources j ON j.id=r.source_id
        JOIN job_preparations p ON p.snapshot_id=j.current_snapshot_id
        JOIN job_metadata m ON m.preparation_id=p.id
        JOIN job_requirements q ON q.metadata_id=m.id
        WHERE s.owner_id=:owner AND r.source_id=COALESCE(CAST(:job AS UUID),r.source_id)
        AND u.deleted_at IS NULL AND ss.deleted_at IS NULL AND s.status!='cancelled'
        AND q.payload->>'state' IN ('ready','unknown'))
    """),
        {"owner": owner, "job": job},
    ):
        return
    key = uuid5(NAMESPACE_URL, f"match-scan:{owner}:{job}:{revision}")
    if (
        session.scalar(select(MatchScanRecord.id).where(MatchScanRecord.id == key))
        is not None
    ):
        return
    session.execute(
        insert(MatchScanRecord)
        .values(id=key, owner_id=owner, job_id=job, step=1)
        .on_conflict_do_nothing()
    )
    PostgresTaskQueue(session).enqueue(NewTask(TaskKey(SCAN, key, 1), key, now), now)
