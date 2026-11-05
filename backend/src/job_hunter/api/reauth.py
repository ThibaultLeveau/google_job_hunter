from datetime import datetime
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.datastructures import QueryParams
from starlette.responses import JSONResponse, RedirectResponse

from job_hunter.api.errors import ErrorEnvelope
from job_hunter.api.sessions import COOKIE, authenticated_user, limit_auth
from job_hunter.application.oidc import UNKNOWN_AUTH_TIME, ConsentCancelled
from job_hunter.application.reauth import (
    Action,
    Method,
    Reauthentication,
    ReauthenticationRequired,
    is_recent,
)
from job_hunter.domain.accounts import SessionUser
from job_hunter.domain.errors import DomainError

FLOW_COOKIE, FLOW_PATH = "job_hunter_oidc", "/api/auth/google"


class ReauthenticationError(ErrorEnvelope):
    code: Literal["reauthentication_required"] = "reauthentication_required"
    message: str = "Recent authentication is required."
    action: Action


router = APIRouter(
    prefix="/api/auth",
    responses={403: {"model": ReauthenticationError | ErrorEnvelope}},
)
User = Annotated[SessionUser, Depends(authenticated_user)]


class ActionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    action: Action


class PasswordProof(ActionRequest):
    password: SecretStr = Field(max_length=128)


class ActionReady(BaseModel):
    status: Literal["ready"] = "ready"
    action: Action


class MethodStatus(BaseModel):
    methods: list[Method]
    authenticated_at: datetime | None
    auth_method: Method
    recent: bool
    recency_seconds: int


class Authorization(BaseModel):
    authorization_url: str


def service(request: Request) -> Reauthentication:
    value = cast(Reauthentication | None, request.app.state.reauthentication)
    if value is None:
        raise HTTPException(503)
    return value


def require_recent(request: Request, action: Action) -> SessionUser:
    user = authenticated_user(request)
    service(request).require(user, action)
    return user


@router.get("/methods")
def methods(request: Request, user: User) -> MethodStatus:
    proof = service(request)
    return MethodStatus(
        methods=proof.store.methods(user.user_id),
        auth_method=cast(Method, user.auth_method),
        authenticated_at=None
        if user.authenticated_at == UNKNOWN_AUTH_TIME
        else user.authenticated_at,
        recent=is_recent(user.authenticated_at, proof.now(), proof.seconds),
        recency_seconds=proof.seconds,
    )


@router.get("/actions/{action}")
def action_status(request: Request, action: Action) -> ActionReady:
    require_recent(request, action)
    return ActionReady(action=action)


@router.post("/reauthenticate/password")
def password(body: PasswordProof, request: Request, user: User) -> ActionReady:
    limit_auth(request, "login", user.email)
    service(request).password(
        user, request.cookies[COOKIE], body.password.get_secret_value()
    )
    return ActionReady(action=body.action)


@router.post("/google/{intent}")
def google_start(
    body: ActionRequest,
    request: Request,
    response: Response,
    intent: Literal["link", "reauthenticate"],
    user: User,
) -> Authorization:
    proof = service(request)
    if proof.google is None:
        raise HTTPException(503)
    if intent == "link" and body.action != "link_google":
        raise HTTPException(422)
    limit_auth(request, "oauth_start")
    url, binding = proof.start(user, request.cookies[COOKIE], intent, body.action)
    response.set_cookie(
        FLOW_COOKIE,
        binding,
        path=FLOW_PATH,
        max_age=300,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return Authorization(authorization_url=url)


@router.get("/google/{intent}/callback")
def google_callback(
    request: Request, intent: Literal["link", "reauthenticate"]
) -> Response:
    outcome, action = "failed", ""
    try:
        user = authenticated_user(request)
        query = cast(QueryParams, request.state.google_query)
        if all(len(query.getlist(key)) <= 1 for key in ("state", "code", "error")):
            action = service(request).finish(
                user,
                request.cookies[COOKIE],
                intent,
                query.get("state", ""),
                request.cookies.get(FLOW_COOKIE, ""),
                query.get("code", ""),
                query.get("error", ""),
            )
            outcome = "linked" if intent == "link" else "reauthenticated"
    except ReauthenticationRequired:
        outcome = "reauthentication_required"
    except ConsentCancelled:
        outcome = "cancelled"
    except DomainError:
        outcome = "conflict"
    except Exception:
        pass
    response = RedirectResponse(
        request.app.state.auth_result_url
        + f"#google_{intent}={outcome}&action={action}",
        status_code=303,
    )
    response.delete_cookie(
        FLOW_COOKIE, path=FLOW_PATH, secure=True, httponly=True, samesite="lax"
    )
    return response


def install_reauthentication(app: FastAPI) -> None:
    async def required(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, ReauthenticationRequired)
        body = ReauthenticationError(
            action=error.action, request_id=UUID(request.state.request_id)
        )
        return JSONResponse(body.model_dump(mode="json"), status_code=403)

    app.add_exception_handler(ReauthenticationRequired, required)
    app.include_router(router)
