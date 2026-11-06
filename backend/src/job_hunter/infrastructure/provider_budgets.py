from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from job_hunter.application.ai import Accounting, AIError, Call, Record
from job_hunter.application.search_provider import (
    SearchCapability,
    SearchError,
    SearchPage,
    SearchProvider,
    SearchRequest,
)
from job_hunter.application.worker import (
    Execute,
    PermanentTaskError,
    RetryableTaskError,
)
from job_hunter.domain.tasks import StaleLease, Task
from job_hunter.infrastructure.budgets import BudgetError, PostgresBudgets
from job_hunter.infrastructure.match_events import EVALUATE
from job_hunter.infrastructure.searches import DISCOVERY
from job_hunter.infrastructure.tasks import TaskRecord
from job_hunter.infrastructure.worker import Handler


@dataclass
class ChargeScope:
    ledger: PostgresBudgets
    owner: UUID | None = None
    run: UUID | None = None
    search_key: UUID | None = None
    search_denied: bool = False
    search_actual: int | None = 0


CURRENT: ContextVar[ChargeScope | None] = ContextVar(
    "provider_charge_scope", default=None
)


class BudgetAccounting(Accounting):
    def __init__(self, delegate: Accounting, required: bool) -> None:
        self.delegate, self.required = delegate, required

    def reserve(self, call: Call) -> None:
        scope = CURRENT.get()
        if scope is None and self.required:
            raise AIError("accounting_failure")
        self.delegate.reserve(call)
        if scope is not None:
            try:
                scope.ledger.reserve(
                    call.id,
                    "tokens",
                    call.input_bound + call.output_bound,
                    scope.owner,
                    scope.run,
                )
            except BudgetError as error:
                raise AIError(
                    "quota" if error.code == "quota" else "accounting_failure"
                ) from None

    def record(self, record: Record) -> None:
        self.delegate.record(record)
        scope = CURRENT.get()
        if scope is not None:
            usage = (record.input_tokens, record.output_tokens)
            actual = (
                sum(value for value in usage if value is not None)
                if all(type(value) is int and value >= 0 for value in usage)
                else None
            )
            scope.ledger.settle(record.call.id, actual)


class BudgetedSearch(SearchProvider):
    def __init__(self, delegate: SearchProvider) -> None:
        self.delegate = delegate

    @property
    def capability(self) -> SearchCapability:
        scope = CURRENT.get()
        if scope is not None and scope.search_denied:
            return self.delegate.capability.model_copy(update={"status": "quota"})
        return self.delegate.capability

    def fetch(self, request: SearchRequest) -> SearchPage:
        if self.delegate.capability.status in {"disabled", "unconfigured"}:
            return self.delegate.fetch(request)
        scope = CURRENT.get()
        if scope is None:
            if self.capability.provider == "fixture":
                return self.delegate.fetch(request)
            raise SearchError("unconfigured")
        if scope.search_denied:
            raise SearchError("quota")
        if scope.search_key is None or scope.search_actual != 0:
            raise SearchError("unconfigured")
        scope.search_actual = None
        try:
            result = self.delegate.fetch(request)
        except Exception as error:
            raise SearchError(
                error.code if isinstance(error, SearchError) else "provider_failure",
                error.usage if isinstance(error, SearchError) else None,
            ) from None
        scope.search_actual = 1
        return result


class ProviderBudgets:
    def __init__(
        self,
        ledger: PostgresBudgets,
        profile_kind: str,
        public_kinds: set[str],
        search_enabled: bool = True,
    ) -> None:
        self.ledger, self.profile_kind, self.public_kinds = (
            ledger,
            profile_kind,
            public_kinds,
        )
        self.search_enabled = search_enabled

    def execute(self, execute: Execute, task: Task) -> object:
        try:
            self.ledger.expire()
            with self.ledger.database.transaction() as session:
                if (
                    session.scalar(
                        select(TaskRecord.id).where(
                            TaskRecord.id == task.id,
                            TaskRecord.status == "running",
                            TaskRecord.generation == task.generation,
                            TaskRecord.lease_token == task.lease_token,
                            TaskRecord.lease_expires_at > self.ledger.now(),
                        )
                    )
                    is None
                ):
                    raise StaleLease()
                if task.key.kind in self.public_kinds:
                    owner, run = None, None
                else:
                    queries = {
                        DISCOVERY: "SELECT owner_id, id FROM search_runs WHERE id=:id",
                        self.profile_kind: """SELECT d.owner_id, NULL
                            FROM parsed_documents p
                            JOIN documents d ON d.id=p.document_id WHERE p.id=:id""",
                        EVALUATE: """SELECT m.owner_id, s.id FROM private_matches m
                            LEFT JOIN job_source_merges a ON a.old_id=m.job_id
                            JOIN run_jobs r
                                ON r.source_id=COALESCE(a.source_id,m.job_id)
                            JOIN search_runs s
                                ON s.id=r.run_id AND s.owner_id=m.owner_id
                            JOIN saved_searches q ON q.id=s.search_id
                            WHERE m.id=:id AND s.status!='cancelled'
                                AND q.deleted_at IS NULL
                            ORDER BY s.created_at,s.id LIMIT 1""",
                    }
                    query = queries.get(task.key.kind)
                    row = (
                        session.execute(
                            text(query), {"id": task.payload_ref}
                        ).one_or_none()
                        if query
                        else None
                    )
                    if row is None:
                        raise PermanentTaskError()
                    owner, run = row
        except SQLAlchemyError:
            raise RetryableTaskError() from None
        scope = ChargeScope(self.ledger, owner, run)
        if task.key.kind == DISCOVERY and self.search_enabled:
            scope.search_key = uuid4()
            try:
                self.ledger.reserve(scope.search_key, "search", 1, owner, run)
            except BudgetError as error:
                if error.code != "quota":
                    raise PermanentTaskError() from None
                scope.search_denied = True
            except SQLAlchemyError:
                raise RetryableTaskError() from None
        token = CURRENT.set(scope)
        try:
            return execute(task)
        finally:
            CURRENT.reset(token)
            if scope.search_key is not None and not scope.search_denied:
                try:
                    self.ledger.settle(scope.search_key, scope.search_actual)
                except SQLAlchemyError:
                    raise RetryableTaskError() from None

    def handlers(self, handlers: Mapping[str, Handler]) -> dict[str, Handler]:
        kinds = self.public_kinds | {DISCOVERY, self.profile_kind, EVALUATE}
        return {
            kind: replace(handler, execute=partial(self.execute, handler.execute))
            if kind in kinds
            else handler
            for kind, handler in handlers.items()
        }
