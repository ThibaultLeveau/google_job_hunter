from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import RedirectResponse

from job_hunter.api.boundary import RequestBoundary
from job_hunter.api.documents import router as documents_router
from job_hunter.api.errors import ERRORS, ErrorEnvelope, install_error_handlers
from job_hunter.api.profiles import router as profiles_router
from job_hunter.api.reauth import FLOW_COOKIE, FLOW_PATH, install_reauthentication
from job_hunter.api.searches import router as searches_router
from job_hunter.api.sessions import (
    COOKIE,
    authenticated_user,
    browser_guard,
    limit_auth,
    require_origin,
)
from job_hunter.api.sessions import router as sessions_router
from job_hunter.application.accounts import RegisterAccount, VerifyEmail
from job_hunter.application.auth_limits import AuthLimits
from job_hunter.application.documents import DocumentUploads
from job_hunter.application.extraction import ExtractProfile
from job_hunter.application.oidc import ConsentCancelled, GoogleSignIn, LinkingRequired
from job_hunter.application.reauth import Reauthentication
from job_hunter.application.recovery import PasswordRecovery
from job_hunter.application.sessions import LIFETIME, Sessions
from job_hunter.domain.accounts import InvalidRegistration, Registration, SessionUser
from job_hunter.infrastructure.accounts import (
    Argon2Passwords,
    PostgresAccounts,
    PostgresAuthCounter,
    PostgresSessions,
)
from job_hunter.infrastructure.ai import create_ai, load_ai_settings
from job_hunter.infrastructure.budgets import PostgresBudgets, load_budget_settings
from job_hunter.infrastructure.database import Database
from job_hunter.infrastructure.documents import (
    LocalStorage,
    PostgresDocuments,
    PostgresExtraction,
    load_storage_settings,
)
from job_hunter.infrastructure.oidc import (
    CALLBACK,
    GoogleProvider,
    PostgresOidc,
    load_google_settings,
)
from job_hunter.infrastructure.profiles import PostgresProfiles
from job_hunter.infrastructure.reauth import PostgresProofs, recency_seconds
from job_hunter.infrastructure.recovery import PostgresRecovery
from job_hunter.infrastructure.search_provider import (
    create_search_provider,
    load_search_settings,
)
from job_hunter.infrastructure.searches import PostgresSearches
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


class VerificationRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    token: SecretStr = Field(min_length=43, max_length=43)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=320)


class ResetPasswordRequest(VerificationRequest):
    password: SecretStr = Field(min_length=15, max_length=128)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    ai_config = load_ai_settings()
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
    app.state.search_provider = create_search_provider(load_search_settings())
    budget_settings = load_budget_settings()
    app.state.provider_budgets = (
        PostgresBudgets(database, budget_settings, lambda: datetime.now(UTC))
        if database is not None
        else None
    )
    app.state.search_allowed_domains = config.search_allowed_domains
    app.state.searches = (
        PostgresSearches(
            database, lambda: datetime.now(UTC), config.search_allowed_domains
        )
        if database is not None
        else None
    )
    app.include_router(searches_router)
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
    app.state.profiles = (
        PostgresProfiles(database, lambda: datetime.now(UTC))
        if database is not None
        else None
    )
    app.state.proposals = (
        PostgresExtraction(
            database,
            ExtractProfile(
                lambda accounting: create_ai(ai_config, accounting),
                ai_config.model_dump_json(
                    exclude={"api_key", "environment", "embedding_model", "limits"}
                ),
            ),
            lambda: datetime.now(UTC),
            ai_config.provider != "disabled",
        )
        if database is not None
        else None
    )
    app.include_router(profiles_router)
    storage = load_storage_settings()
    app.state.documents = (
        DocumentUploads(
            PostgresDocuments(database, storage, lambda: datetime.now(UTC)),
            LocalStorage(storage.root),
            storage.max_bytes,
        )
        if database is not None and storage.root is not None
        else None
    )
    app.include_router(documents_router)
    install_error_handlers(app)
    app.add_middleware(
        RequestBoundary, settings=config, upload_max_bytes=storage.max_bytes
    )

    @app.middleware("http")
    async def discard_callback_query(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.scope["path"].startswith(FLOW_PATH + "/") and request.scope[
            "path"
        ].rstrip("/").endswith("/callback"):
            raw = request.scope["query_string"]
            request.scope["query_string"] = b""
            request.state.google_query = QueryParams(raw if len(raw) <= 4096 else b"")
        return await call_next(request)

    @app.get("/api/health/live")
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/api/health/ready", responses={503: {"model": ReadinessResponse}})
    def readiness(response: Response) -> ReadinessResponse:
        ready = database is not None and database.is_ready()
        response.status_code = 200 if ready else 503
        return ReadinessResponse(status="ok" if ready else "unavailable")

    accounts = PostgresAccounts(database) if database is not None else None
    verification = (
        VerifyEmail(accounts.verification, lambda: datetime.now(UTC))
        if accounts is not None
        else None
    )
    registration = (
        RegisterAccount(accounts, Argon2Passwords(), lambda: datetime.now(UTC))
        if accounts is not None
        else None
    )
    recovery = (
        PasswordRecovery(
            PostgresRecovery(database, accounts.verification.settings),
            Argon2Passwords(),
            lambda: datetime.now(UTC),
        )
        if database is not None and accounts is not None
        else None
    )

    google = load_google_settings()
    app.state.google_sign_in = (
        GoogleSignIn(
            PostgresOidc(
                database, accounts.verification.settings.key.get_secret_value()
            ),
            GoogleProvider(google),
            lambda: datetime.now(UTC),
        )
        if google is not None and database is not None and accounts is not None
        else None
    )
    result_url = (
        accounts.verification.settings.public_url + "/login"
        if accounts is not None
        else "/login"
    )
    app.state.auth_result_url = result_url
    seconds = recency_seconds()
    app.state.reauthentication = (
        Reauthentication(
            PostgresProofs(database),
            app.state.google_sign_in,
            Argon2Passwords().verify,
            lambda: datetime.now(UTC),
            seconds,
        )
        if database is not None
        else None
    )

    @app.get("/api/auth/providers")
    def providers() -> dict[str, bool]:
        return {"google": app.state.google_sign_in is not None}

    @app.get("/api/auth/google/start")
    def google_start(request: Request) -> Response:
        service = cast(GoogleSignIn | None, app.state.google_sign_in)
        if service is None:
            raise HTTPException(503)
        limit_auth(request, "oauth_start")
        url, binding = service.start(request.cookies.get(COOKIE, ""))
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            FLOW_COOKIE,
            binding,
            max_age=300,
            path=FLOW_PATH,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get(CALLBACK)
    def google_callback(request: Request) -> Response:
        token, outcome = "", "failed"
        try:
            service = cast(GoogleSignIn | None, app.state.google_sign_in)
            query = cast(QueryParams, request.state.google_query)
            if service is not None and all(
                len(query.getlist(key)) <= 1 for key in ("state", "code", "error")
            ):
                token = service.finish(
                    query.get("state", ""),
                    request.cookies.get(FLOW_COOKIE, ""),
                    request.cookies.get(COOKIE, ""),
                    query.get("code", ""),
                    query.get("error", ""),
                )
                outcome = "signed_in"
        except ConsentCancelled:
            outcome = "cancelled"
        except LinkingRequired:
            outcome = "linking_required"
        except Exception:
            pass
        response = RedirectResponse(result_url + "#google=" + outcome, status_code=303)
        if token:
            response.set_cookie(
                COOKIE,
                token,
                max_age=int(LIFETIME.total_seconds()),
                path="/api",
                secure=True,
                httponly=True,
                samesite="lax",
            )
        response.delete_cookie(
            FLOW_COOKIE, path=FLOW_PATH, secure=True, httponly=True, samesite="lax"
        )
        return response

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

    @app.post("/api/auth/verify", status_code=202)
    def verify(
        body: VerificationRequest,
        request: Request,
        user: Annotated[SessionUser, Depends(authenticated_user)],
    ) -> RegistrationResult:
        limit_auth(request, "verification", user.email)
        assert verification is not None
        verification.verify(user.user_id, body.token.get_secret_value())
        return RegistrationResult()

    @app.post("/api/auth/resend-verification", status_code=202)
    def resend(
        request: Request,
        user: Annotated[SessionUser, Depends(authenticated_user)],
    ) -> RegistrationResult:
        limit_auth(request, "verification_resend", user.email)
        assert verification is not None
        verification.resend(user.user_id)
        return RegistrationResult()

    @app.post("/api/auth/forgot-password", status_code=202)
    def forgot_password(
        body: ForgotPasswordRequest, request: Request
    ) -> RegistrationResult:
        require_origin(request)
        limit_auth(request, "recovery", body.email)
        assert recovery is not None
        recovery.request(body.email)
        return RegistrationResult()

    @app.post("/api/auth/reset-password", status_code=202)
    def reset_password(
        body: ResetPasswordRequest, request: Request
    ) -> RegistrationResult:
        require_origin(request)
        limit_auth(request, "recovery")
        assert recovery is not None
        try:
            recovery.reset(
                body.token.get_secret_value(), body.password.get_secret_value()
            )
        except InvalidRegistration:
            raise RequestValidationError(
                [
                    {
                        "loc": ("body", "password"),
                        "msg": "Invalid value.",
                        "type": "value_error",
                    }
                ]
            ) from None
        return RegistrationResult()

    app.include_router(sessions_router)
    install_reauthentication(app)
    return app
