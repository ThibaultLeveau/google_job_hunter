from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from job_hunter.api.sessions import authenticated_user
from job_hunter.application.job_listing import JobListing, JobPage, JobQuery
from job_hunter.domain.accounts import SessionUser

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


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
