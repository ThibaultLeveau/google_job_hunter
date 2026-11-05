import hmac
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from job_hunter.application.sessions import LIFETIME, Sessions, csrf_token
from job_hunter.domain.accounts import SessionUser, Unauthenticated

COOKIE = "job_hunter_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
router = APIRouter(prefix="/api/auth")


class LoginRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    email: str = Field(max_length=320)
    password: SecretStr = Field(max_length=128)


class CurrentUser(BaseModel):
    id: UUID
    email: str
    email_verified: bool
    csrf_token: str


def sessions(request: Request) -> Sessions:
    service = cast(Sessions | None, request.app.state.sessions)
    if service is None:
        raise HTTPException(503)
    return service


def require_origin(request: Request) -> None:
    if request.headers.get("origin") not in request.app.state.allowed_origins:
        raise HTTPException(403)


def require_csrf(request: Request, token: str) -> None:
    require_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    if len(supplied) != 64 or not hmac.compare_digest(
        supplied.encode(), csrf_token(token).encode()
    ):
        raise HTTPException(403)


def authenticated_user(request: Request) -> SessionUser:
    """Scope every private query by user_id; absent or foreign records return 404."""
    user = cast(SessionUser | None, getattr(request.state, "user", None))
    token = request.cookies.get(COOKIE, "")
    if user is None:
        user = sessions(request).current(token)
        request.state.user = user
    if request.method not in SAFE_METHODS:
        require_csrf(request, token)
    return user


def browser_guard(request: Request) -> None:
    if (
        request.method not in SAFE_METHODS
        and COOKIE in request.cookies
        and request.url.path != "/api/auth/login"
    ):
        authenticated_user(request)


def response_user(user: SessionUser, token: str) -> CurrentUser:
    return CurrentUser(
        id=user.user_id,
        email=user.email,
        email_verified=user.email_verified,
        csrf_token=csrf_token(token),
    )


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> CurrentUser:
    require_origin(request)
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415)
    service = sessions(request)
    previous = request.cookies.get(COOKIE, "")
    if previous:
        try:
            service.current(previous)
        except Unauthenticated:
            pass
        else:
            require_csrf(request, previous)
    token = service.login(body.email, body.password.get_secret_value(), previous)
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(LIFETIME.total_seconds()),
        path="/api",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response_user(service.current(token), token)


@router.get("/me")
def current_user(
    request: Request, user: Annotated[SessionUser, Depends(authenticated_user)]
) -> CurrentUser:
    return response_user(user, request.cookies[COOKIE])


@router.post("/logout", status_code=204)
def logout(
    request: Request, user: Annotated[SessionUser, Depends(authenticated_user)]
) -> Response:
    sessions(request).logout(request.cookies[COOKIE])
    response = Response(status_code=204)
    response.delete_cookie(
        COOKIE, path="/api", secure=True, httponly=True, samesite="lax"
    )
    return response
