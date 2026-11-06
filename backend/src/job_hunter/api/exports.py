from typing import Literal, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.responses import JSONResponse

from job_hunter.api.errors import ErrorEnvelope
from job_hunter.api.reauth import ReauthenticationError, require_recent
from job_hunter.api.sessions import COOKIE
from job_hunter.application.exports import (
    ExportTooLarge,
    PersonalExport,
    PersonalExports,
)
from job_hunter.application.sessions import token_hash


class ExportLimitError(ErrorEnvelope):
    code: Literal["export_too_large"] = "export_too_large"
    message: str = (
        "The export exceeds its size limit. Contact your workspace administrator."
    )


router = APIRouter(prefix="/api/v1/account")


@router.post(
    "/export",
    response_model=PersonalExport,
    responses={
        403: {"model": ReauthenticationError | ErrorEnvelope},
        413: {"model": ExportLimitError},
    },
)
def export(request: Request, body: dict[str, object] | None = None) -> Response:
    owner = require_recent(request, "export_data")
    store = cast(PersonalExports | None, request.app.state.exports)
    if store is None:
        raise HTTPException(503)
    if request.query_params or body:
        raise HTTPException(422)
    try:
        content = store.read(owner.user_id, token_hash(request.cookies[COOKIE]))
    except ExportTooLarge:
        return JSONResponse(
            ExportLimitError(request_id=UUID(request.state.request_id)).model_dump(
                mode="json"
            ),
            status_code=413,
        )
    return Response(
        content,
        media_type="application/json",
        headers={
            "Content-Disposition": (
                'attachment; filename="google-job-hunter-export-v1.json"'
            )
        },
    )
