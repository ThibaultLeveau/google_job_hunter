from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field

from job_hunter.api.sessions import authenticated_user, verified_user
from job_hunter.application.search_provider import SearchCapability, SearchProvider
from job_hunter.application.searches import (
    RunPage,
    SavedSearch,
    Searches,
    SearchPage,
    SearchRun,
    SearchValue,
    SearchWrite,
)
from job_hunter.domain.accounts import SessionUser
from job_hunter.domain.searches import (
    COUNTRIES,
    LANGUAGES,
    SearchDefinition,
    SearchPreview,
    compile_search,
)

router = APIRouter(prefix="/api/v1/searches", tags=["searches"])
Owner = Annotated[SessionUser, Depends(authenticated_user)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0, le=10000)]


class SearchEdit(SearchWrite):
    expected_revision: int = Field(ge=1)


class RunStart(SearchValue):
    expected_revision: int = Field(ge=1)
    idempotency_key: Annotated[UUID, Field(strict=False)]


class SearchHintOptions(SearchValue):
    allowed_domains: tuple[str, ...]
    language_codes: tuple[str, ...]
    country_codes: tuple[str, ...]


def searches(request: Request) -> Searches:
    service = cast(Searches | None, request.app.state.searches)
    if service is None:
        raise HTTPException(503)
    return service


Store = Annotated[Searches, Depends(searches)]


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


@router.get("/provider")
def provider_status(request: Request, owner: Owner) -> SearchCapability:
    return cast(SearchProvider, request.app.state.search_provider).capability


@router.get("/hints")
def hint_options(request: Request, owner: Owner) -> SearchHintOptions:
    return SearchHintOptions(
        allowed_domains=cast(tuple[str, ...], request.app.state.search_allowed_domains),
        language_codes=tuple(sorted(LANGUAGES)),
        country_codes=tuple(sorted(COUNTRIES)),
    )


@router.get("")
def list_searches(
    owner: Owner, store: Store, limit: Limit = 20, offset: Offset = 0
) -> SearchPage:
    return store.list(owner.user_id, limit, offset)


@router.post("", status_code=201)
def create_search(body: SearchWrite, owner: Owner, store: Store) -> SavedSearch:
    try:
        return store.save(owner.user_id, body)
    except ValueError:
        raise HTTPException(422) from None


@router.get("/runs")
def list_runs(
    owner: Owner, store: Store, limit: Limit = 20, offset: Offset = 0
) -> RunPage:
    return store.runs(owner.user_id, limit, offset)


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, owner: Owner, store: Store) -> SearchRun:
    return store.run(owner.user_id, run_id)


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: UUID, owner: Owner, store: Store) -> SearchRun:
    return store.cancel(owner.user_id, run_id)


@router.get("/{search_id}")
def get_search(search_id: UUID, owner: Owner, store: Store) -> SavedSearch:
    return store.get(owner.user_id, search_id)


@router.put("/{search_id}")
def edit_search(
    search_id: UUID, body: SearchEdit, owner: Owner, store: Store
) -> SavedSearch:
    try:
        return store.save(owner.user_id, body, search_id, body.expected_revision)
    except ValueError:
        raise HTTPException(422) from None


@router.delete("/{search_id}", status_code=204)
def delete_search(
    search_id: UUID,
    owner: Owner,
    store: Store,
    expected_revision: Annotated[int, Query(ge=1)],
) -> Response:
    store.delete(owner.user_id, search_id, expected_revision)
    return Response(status_code=204)


@router.post("/{search_id}/runs", status_code=202)
def start_run(
    search_id: UUID,
    body: RunStart,
    owner: Annotated[SessionUser, Depends(verified_user)],
    store: Store,
) -> SearchRun:
    return store.start(
        owner.user_id, search_id, body.expected_revision, body.idempotency_key
    )
