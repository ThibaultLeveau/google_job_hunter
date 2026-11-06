from collections.abc import Iterator
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from job_hunter.api.sessions import authenticated_user, verified_user
from job_hunter.application.job_details import (
    JobDetail,
    JobDetails,
    JobStatus,
    ReevaluationWrite,
    RetryWrite,
    WorkAccepted,
)
from job_hunter.application.job_listing import JobListing, JobPage, JobQuery
from job_hunter.domain.accounts import SessionUser

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


class PrivateJobQueries:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/jobs"):
            # Uvicorn retains the transport scope for access logs; parsing uses a copy.
            private_scope = dict(scope)
            scope["query_string"] = b""
            await self.app(private_scope, receive, send)
        else:
            await self.app(scope, receive, send)


@router.get("")
def list_jobs(
    request: Request,
    owner: Annotated[SessionUser, Depends(authenticated_user)],
    query: Annotated[JobQuery, Query()],
) -> JobPage:
    listing = cast(JobListing | None, request.app.state.job_listing)
    if listing is None:
        raise HTTPException(503)
    try:
        return listing.list(owner.user_id, query)
    except ValueError:
        raise HTTPException(422) from None
    except SQLAlchemyError:
        raise HTTPException(503) from None


def details(request: Request) -> Iterator[JobDetails]:
    service = cast(JobDetails | None, request.app.state.job_details)
    if service is None:
        raise HTTPException(503)
    try:
        yield service
    except SQLAlchemyError:
        raise HTTPException(503) from None


DetailService = Annotated[JobDetails, Depends(details)]
Owner = Annotated[SessionUser, Depends(authenticated_user)]
VerifiedOwner = Annotated[SessionUser, Depends(verified_user)]


@router.get("/{job_id}")
def job_detail(job_id: UUID, owner: Owner, service: DetailService) -> JobDetail:
    return service.detail(owner.user_id, job_id)


@router.get("/{job_id}/status")
def job_status(job_id: UUID, owner: Owner, service: DetailService) -> JobStatus:
    return service.detail(owner.user_id, job_id).status


@router.post("/{job_id}/retry", status_code=202)
def retry_job(
    job_id: UUID, body: RetryWrite, owner: VerifiedOwner, service: DetailService
) -> WorkAccepted:
    result = service.retry(owner.user_id, job_id, body)
    if result is None:
        raise HTTPException(429)
    return result


@router.post("/{job_id}/reevaluate", status_code=202)
def reevaluate_job(
    job_id: UUID, body: ReevaluationWrite, owner: VerifiedOwner, service: DetailService
) -> WorkAccepted:
    result = service.reevaluate(owner.user_id, job_id, body.idempotency_key)
    if result is None:
        raise HTTPException(429)
    return result
