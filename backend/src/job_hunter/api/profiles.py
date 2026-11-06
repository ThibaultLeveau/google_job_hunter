from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field

from job_hunter.api.sessions import verified_user
from job_hunter.application.profiles import FactResult, Profiles, ProfileSnapshot
from job_hunter.domain.accounts import SessionUser
from job_hunter.domain.profiles import FactData, ProfileFact, ProfileValue

router = APIRouter(prefix="/api/v1/profile")
Owner = Annotated[SessionUser, Depends(verified_user)]


class FactWrite(ProfileValue):
    expected_revision: Annotated[int, Field(ge=0)]
    fact: FactData


def profiles(request: Request) -> Profiles:
    service = cast(Profiles | None, request.app.state.profiles)
    if service is None:
        raise HTTPException(503)
    return service


Store = Annotated[Profiles, Depends(profiles)]


@router.get("")
def profile(owner: Owner, store: Store) -> ProfileSnapshot:
    return store.read(owner.user_id)


@router.get("/facts/{fact_id}")
def fact(fact_id: UUID, owner: Owner, store: Store) -> ProfileFact:
    return store.get(owner.user_id, fact_id)


@router.post("/facts", responses={201: {"model": FactResult}})
def upsert(
    body: FactWrite, owner: Owner, store: Store, response: Response
) -> FactResult:
    result = store.put(owner.user_id, body.expected_revision, body.fact)
    response.status_code = 201 if result.outcome == "created" else 200
    return result


@router.put("/facts/{fact_id}")
def edit(fact_id: UUID, body: FactWrite, owner: Owner, store: Store) -> FactResult:
    return store.put(owner.user_id, body.expected_revision, body.fact, fact_id)


@router.delete("/facts/{fact_id}")
def delete(
    fact_id: UUID,
    owner: Owner,
    store: Store,
    expected_revision: Annotated[int, Query(ge=0)],
) -> FactResult:
    return store.delete(owner.user_id, expected_revision, fact_id)
