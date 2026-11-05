from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from job_hunter.domain.accounts import Unauthenticated
from job_hunter.domain.errors import DomainError

ERRORS = {
    400: ("bad_request", "The request is invalid."),
    401: ("unauthenticated", "Authentication is required."),
    403: ("forbidden", "The request is not allowed."),
    404: ("not_found", "The requested resource was not found."),
    405: ("method_not_allowed", "This method is not allowed."),
    408: ("request_timeout", "The request took too long."),
    409: ("conflict", "The request conflicts with the current state."),
    413: ("request_too_large", "The request body is too large."),
    415: ("unsupported_media_type", "This content type is not supported."),
    422: ("validation_error", "Check the submitted fields."),
    429: ("rate_limited", "Too many requests. Try again later."),
    500: ("internal_error", "The request could not be completed."),
    503: ("unavailable", "The service is temporarily unavailable."),
}


class FieldError(BaseModel):
    field: str
    message: str = "Invalid value."


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    field_errors: list[FieldError] = Field(default_factory=list)
    request_id: UUID


def error_response(
    status: int, request_id: str, fields: list[str] | None = None
) -> JSONResponse:
    code, message = ERRORS.get(status, ("request_failed", "The request failed."))
    body = ErrorEnvelope(
        code=code,
        message=message,
        request_id=UUID(request_id),
        field_errors=[FieldError(field=field) for field in fields or []],
    )
    return JSONResponse(body.model_dump(mode="json"), status_code=status)


def validation_fields(request: Request, error: RequestValidationError) -> list[str]:
    # Only declared field names are safe; dynamic keys and validator messages are input.
    declared: set[str] = set()
    route = request.scope.get("route")
    if isinstance(route, APIRoute):
        declared.update(
            field.alias
            for field in route.dependant.query_params + route.dependant.path_params
        )
        model = route.body_field.field_info.annotation if route.body_field else None
        if isinstance(model, type) and issubclass(model, BaseModel):
            declared.update(model.model_fields)
    fields = []
    for detail in error.errors()[:20]:
        location = detail["loc"]
        field = (
            str(location[1])
            if len(location) > 1 and location[1] in declared
            else "request"
        )
        if field not in fields:
            fields.append(field)
    return fields


def install_error_handlers(app: FastAPI) -> None:
    async def handle(request: Request, error: Exception) -> JSONResponse:
        fields = None
        if isinstance(error, RequestValidationError):
            status = 422
            fields = validation_fields(request, error)
        elif isinstance(error, Unauthenticated):
            status = 401
        elif isinstance(error, DomainError):
            status = 404 if error.code == "not_found" else 409
        else:
            assert isinstance(error, HTTPException)
            status = error.status_code
        response = error_response(status, request.state.request_id, fields)
        if (
            isinstance(error, HTTPException)
            and error.headers
            and "Allow" in error.headers
        ):
            response.headers["Allow"] = error.headers["Allow"]
        return response

    for exception in (
        RequestValidationError,
        DomainError,
        Unauthenticated,
        HTTPException,
    ):
        app.add_exception_handler(exception, handle)
