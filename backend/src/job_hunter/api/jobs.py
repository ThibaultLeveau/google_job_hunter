from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from job_hunter.api.sessions import authenticated_user
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
