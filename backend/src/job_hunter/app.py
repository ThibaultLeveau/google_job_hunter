from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool

from job_hunter.api.boundary import RequestBoundary
from job_hunter.api.errors import ERRORS, ErrorEnvelope, install_error_handlers
from job_hunter.api.sessions import browser_guard, limit_auth
from job_hunter.api.sessions import router as sessions_router
from job_hunter.application.accounts import RegisterAccount
from job_hunter.application.auth_limits import AuthLimits
from job_hunter.application.sessions import Sessions
from job_hunter.domain.accounts import InvalidRegistration, Registration
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    PostgresAccounts,
    PostgresAuthCounter,
    PostgresSessions,
)
from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings, load_settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=320)
    password: SecretStr = Field(min_length=15, max_length=128)


class RegistrationResult(BaseModel):
    status: Literal["accepted"] = "accepted"


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    database = Database(config) if config.database_url is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if database is not None:
                await run_in_threadpool(database.close)

    app = FastAPI(
        title="Google Job Hunter",
        version="0.1.0",
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
        lifespan=lifespan,
        dependencies=[Depends(browser_guard)],
        responses={status: {"model": ErrorEnvelope} for status in ERRORS},
    )
    app.state.allowed_origins = config.allowed_origins
    app.state.sessions = (
        Sessions(
            PostgresSessions(database),
            Argon2Passwords().verify,
            lambda: datetime.now(UTC),
        )
        if database is not None
        else None
    )
    app.state.auth_limits = (
        AuthLimits(PostgresAuthCounter(database), lambda: datetime.now(UTC))
        if database is not None
        else None
    )
    install_error_handlers(app)
    app.add_middleware(RequestBoundary, settings=config)

    @app.get("/api/health/live")
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/health/ready", responses={503: {"model": ReadinessResponse}})
    def readiness(response: Response) -> ReadinessResponse:
        ready = database is not None and database.is_ready()
        response.status_code = 200 if ready else 503
        return ReadinessResponse(status="ok" if ready else "unavailable")

    registration = (
        RegisterAccount(
            PostgresAccounts(database), Argon2Passwords(), lambda: datetime.now(UTC)
        )
        if database is not None
        else None
    )

    @app.post("/api/auth/register", status_code=202)
    def register(body: RegistrationRequest, request: Request) -> RegistrationResult:
        if registration is not None:
            limit_auth(request, "registration", body.email)
        try:
            values = Registration(body.email, body.password.get_secret_value())
        except InvalidRegistration as error:
            raise RequestValidationError(
                [
                    {
                        "loc": ("body", error.field_name),
                        "msg": "Invalid value.",
                        "type": "value_error",
                    }
                ]
            ) from None
        if registration is None:
            raise HTTPException(503)
        registration.execute(values)
        return RegistrationResult()

    app.include_router(sessions_router)
    return app
