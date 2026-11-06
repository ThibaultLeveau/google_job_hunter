from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict

from job_hunter.api.errors import ErrorEnvelope
from job_hunter.api.reauth import (
    FLOW_COOKIE,
    FLOW_PATH,
    ReauthenticationError,
    require_recent,
)
from job_hunter.api.sessions import COOKIE
from job_hunter.application.account_deletion import AccountDeletion
from job_hunter.application.sessions import token_hash


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    intent: Literal["delete_account"]


class DeletionAccepted(BaseModel):
    status: Literal["deleting"] = "deleting"


router = APIRouter(prefix="/api/v1/account")


@router.post(
    "/delete",
    status_code=202,
    responses={403: {"model": ReauthenticationError | ErrorEnvelope}},
)
def delete_account(
    request: Request, body: DeleteAccountRequest, response: Response
) -> DeletionAccepted:
    owner = require_recent(request, "delete_account")
    store = cast(AccountDeletion | None, request.app.state.account_deletion)
    if store is None:
        raise HTTPException(503)
    if request.query_params:
        raise HTTPException(422)
    store.delete(owner.user_id, token_hash(request.cookies[COOKIE]))
    for name, path in ((COOKIE, "/api"), (FLOW_COOKIE, FLOW_PATH)):
        response.delete_cookie(
            name, path=path, secure=True, httponly=True, samesite="lax"
        )
    return DeletionAccepted()
