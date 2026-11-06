from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from job_hunter.api.sessions import authenticated_user
from job_hunter.domain.accounts import SessionUser
from job_hunter.domain.searches import SearchDefinition, SearchPreview, compile_search

router = APIRouter(prefix="/api/v1/searches", tags=["searches"])


@router.post("/preview")
def preview_search(
    definition: SearchDefinition,
    request: Request,
    user: Annotated[SessionUser, Depends(authenticated_user)],
) -> SearchPreview:
    allowed = cast(tuple[str, ...], request.app.state.search_allowed_domains)
    try:
        return compile_search(definition, allowed)
    except ValueError:
        raise HTTPException(422) from None
