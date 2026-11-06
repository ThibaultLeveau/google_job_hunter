from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from job_hunter.application.crawling import CRAWL as CRAWL
from job_hunter.application.crawling import FAILED_STATES
from job_hunter.application.search_provider import (
    SearchError,
    SearchPage,
    SearchProvider,
    SearchRequest,
    SearchUsage,
)
from job_hunter.application.searches import DiscoveryProgress, RunSnapshot
from job_hunter.application.worker import RetryableTaskError
from job_hunter.domain.jobs import canonical_url
from job_hunter.domain.tasks import NewTask, Task, TaskKey
from job_hunter.infrastructure.accounts import UserRecord
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.jobs import (
    JobSourceRecord,
    PostgresJobRegistry,
    lock_job_namespace,
)
from job_hunter.infrastructure.searches import DISCOVERY, SearchRunRecord
from job_hunter.infrastructure.tasks import PostgresTaskQueue, TaskRecord
from job_hunter.infrastructure.worker import Handler


@dataclass(frozen=True)
class DiscoveredPage:
    page: SearchPage | None = None
    error: str | None = None
    usage: SearchUsage = SearchUsage()


class PostgresDiscovery:
    def __init__(
        self, database: Database, provider: SearchProvider, now: Callable[[], datetime]
    ) -> None:
        self.database, self.provider, self.now = database, provider, now

    @staticmethod
    def live(session: Session, task: Task, now: datetime) -> SearchRunRecord | None:
        row = session.scalar(
            select(SearchRunRecord)
            .where(
                SearchRunRecord.id == task.key.subject_id,
                SearchRunRecord.id == task.payload_ref,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.status not in {"pending", "running"}:
            return None
        owner = session.scalar(
            select(UserRecord).where(UserRecord.id == row.owner_id).with_for_update()
        )
        if owner is None or owner.deleted_at is not None:
            row.status, row.updated_at = "cancelled", now
            return None
        if (
            DiscoveryProgress.model_validate(row.discovery).pages_completed
            != task.key.subject_version - 1
        ):
            return None
        return row

    def execute(self, task: Task) -> DiscoveredPage | None:
        try:
            with self.database.transaction() as session:
                if (
                    session.scalar(
                        select(TaskRecord.id)
                        .where(
                            TaskRecord.id == task.id,
                            TaskRecord.status == "running",
                            TaskRecord.generation == task.generation,
                            TaskRecord.lease_token == task.lease_token,
                            TaskRecord.lease_expires_at > self.now(),
                        )
                        .with_for_update()
                    )
                    is None
                ):
                    return None
                row = self.live(session, task, self.now())
                if row is None:
                    return None
                progress = DiscoveryProgress.model_validate(row.discovery)
                cap = self.provider.capability
                if progress.max_pages is None:
                    progress = DiscoveryProgress.model_validate(
                        {
                            **progress.model_dump(),
                            "max_pages": min(10, cap.max_pages),
                            "max_results": min(
                                100, cap.max_results, cap.max_pages * 10
                            ),
                        }
                    )
                snapshot = RunSnapshot.model_validate(row.snapshot)
                request = SearchRequest(
                    query=snapshot.query,
                    parameters=snapshot.provider_parameters,
                    page=progress.pages_completed,
                )
                row.status, row.updated_at = "running", self.now()
                progress = progress.model_copy(
                    update={
                        "provider_requests": progress.provider_requests
                        + int(cap.provider == "serpapi" and cap.status == "configured")
                    }
                )
                row.discovery = progress.model_dump(mode="json")
        except SQLAlchemyError:
            raise RetryableTaskError() from None
        try:
            page = self.provider.fetch(request)
            assert progress.max_results is not None
            bound = min(10, progress.max_results - request.page * 10)
            if not (
                page.usage.error is None
                and 0 <= page.usage.received <= bound
                and page.usage.returned == len(page.results) <= page.usage.received
                and page.usage.skipped == page.usage.received - len(page.results)
                and page.next_page in (None, request.page + 1)
                and all(
                    request.page * 10 < result.rank <= (request.page + 1) * 10
                    for result in page.results
                )
            ):
                return DiscoveredPage(error="invalid_response")
            return DiscoveredPage(page=page, usage=page.usage)
        except SearchError as error:
            return DiscoveredPage(error=error.code, usage=error.usage or SearchUsage())
        except Exception:
            return DiscoveredPage(error="provider_failure")

    def publish(
        self, session: Session, task: Task, result: object, now: datetime
    ) -> None:
        lock_job_namespace(session)
        if (
            not isinstance(result, DiscoveredPage)
            or (row := self.live(session, task, now)) is None
        ):
            return
        progress = DiscoveryProgress.model_validate(row.discovery)
        changes: dict[str, object] = {"last_request_id": result.usage.request_id}
        if result.error is not None:
            row.status = "partial" if progress.pages_completed else "failed"
            changes["error_code"] = result.error
        else:
            assert (
                result.page is not None
                and progress.max_pages is not None
                and progress.max_results is not None
            )
            page = result.page
            row.found_results += page.usage.received
            row.skipped_results += page.usage.skipped
            valid = []
            for entry in page.results:
                try:
                    valid.append((canonical_url(entry.source_url), entry))
                except ValueError:
                    row.skipped_results += 1
            # Stable URL order avoids inverted unique-key waits across pages.
            for _, entry in sorted(valid, key=lambda pair: pair[0]):
                found = PostgresJobRegistry(session).register(
                    row.owner_id, row.id, entry.source_url, entry.rank, now
                )
                if found.new_source:
                    row.new_urls += 1
                    PostgresTaskQueue(session).enqueue(
                        NewTask(
                            TaskKey(CRAWL, found.source_id, 1), found.source_id, now
                        ),
                        now,
                    )
                else:
                    row.known_urls += 1
                if found.new_association:
                    row.processing_total += 1
                    row.processing_failed += int(
                        session.scalar(
                            select(JobSourceRecord.crawl_state).where(
                                JobSourceRecord.id == found.source_id
                            )
                        )
                        in FAILED_STATES
                        or session.scalar(
                            select(TaskRecord.status).where(
                                TaskRecord.kind == CRAWL,
                                TaskRecord.subject_id == found.source_id,
                                TaskRecord.subject_version == 1,
                            )
                        )
                        == "failed"
                    )
            completed = progress.pages_completed + 1
            capped = (
                completed >= progress.max_pages
                or completed * 10 >= progress.max_results
            )
            changes.update(
                pages_completed=completed,
                limited=page.truncated or (capped and page.next_page is not None),
            )
            row.status = "completed" if page.next_page is None or capped else "running"
            if row.status == "running":
                PostgresTaskQueue(session).enqueue(
                    NewTask(TaskKey(DISCOVERY, row.id, completed + 1), row.id, now), now
                )
        row.discovery = DiscoveryProgress.model_validate(
            {**progress.model_dump(), **changes}
        ).model_dump(mode="json")
        row.updated_at = now

    def handlers(self) -> dict[str, Handler]:
        return {DISCOVERY: Handler(self.execute, self.publish)}
