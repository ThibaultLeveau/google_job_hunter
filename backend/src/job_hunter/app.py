from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from job_hunter.api.boundary import RequestBoundary
from job_hunter.api.errors import ERRORS, ErrorEnvelope, install_error_handlers
from job_hunter.infrastructure.database import Database
from job_hunter.settings import Settings, load_settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]


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
        responses={status: {"model": ErrorEnvelope} for status in ERRORS},
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

    return app
