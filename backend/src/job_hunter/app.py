from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from job_hunter.settings import Settings, load_settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else load_settings()
    app = FastAPI(
        title="Google Job Hunter",
        version="0.1.0",
        docs_url="/docs" if config.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.docs_enabled else None,
    )

    @app.get("/api/health/live")
    def health() -> HealthResponse:
        return HealthResponse()

    return app
