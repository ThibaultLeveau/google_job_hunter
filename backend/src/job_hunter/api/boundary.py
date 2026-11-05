import asyncio
import logging
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from job_hunter.api.errors import error_response
from job_hunter.settings import Settings

logger = logging.getLogger(__name__)
METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
HEADERS = {"content-type", "x-request-id"}


class RequestBoundary:
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app, self.settings = app, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        supplied = headers.get("x-request-id", "")
        try:
            request_id = str(UUID(supplied)) if len(supplied) == 36 else str(uuid4())
        except ValueError:
            request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        origin = headers.get("origin")
        started = False

        async def safe_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                outgoing = MutableHeaders(scope=message)
                outgoing["X-Request-ID"] = request_id
                outgoing["X-Content-Type-Options"] = "nosniff"
                outgoing["X-Frame-Options"] = "DENY"
                outgoing["Referrer-Policy"] = "no-referrer"
                outgoing["Cache-Control"] = "no-store"
                if scope["path"].startswith("/api/"):
                    outgoing["Content-Security-Policy"] = (
                        "default-src 'none'; frame-ancestors 'none'"
                    )
                if scope["scheme"] == "https":
                    outgoing["Strict-Transport-Security"] = "max-age=31536000"
                if origin is not None and origin in self.settings.allowed_origins:
                    outgoing["Access-Control-Allow-Origin"] = origin
                    outgoing["Access-Control-Allow-Credentials"] = "true"
                    outgoing["Access-Control-Expose-Headers"] = "X-Request-ID"
                    outgoing.add_vary_header("Origin")
            await send(message)

        async def reject(status: int) -> None:
            await error_response(status, request_id)(scope, receive, safe_send)

        try:
            host = urlsplit("//" + headers.get("host", ""))
            _ = host.port
            valid_host = (
                host.hostname in self.settings.trusted_hosts
                and headers["host"] == host.netloc
                and not host.username
                and not host.path
                and not host.query
                and not host.fragment
            )
        except ValueError:
            valid_host = False
        if not valid_host or len(headers.getlist("host")) != 1:
            await reject(400)
            return
        if len(headers.getlist("origin")) > 1 or (
            origin is not None and origin not in self.settings.allowed_origins
        ):
            await reject(403)
            return
        if scope["method"] == "OPTIONS" and "access-control-request-method" in headers:
            requested = {
                value.strip().lower()
                for value in headers.get("access-control-request-headers", "").split(
                    ","
                )
                if value.strip()
            }
            if (
                origin is None
                or headers["access-control-request-method"] not in METHODS
                or not requested <= HEADERS
            ):
                await reject(400)
                return
            await Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Methods": ", ".join(sorted(METHODS)),
                    "Access-Control-Allow-Headers": ", ".join(sorted(HEADERS)),
                    "Access-Control-Max-Age": "600",
                },
            )(scope, receive, safe_send)
            return
        lengths = headers.getlist("content-length")
        if len(lengths) > 1 or (lengths and not lengths[0].isdecimal()):
            await reject(400)
            return
        if lengths and (
            len(lengths[0]) > 16 or int(lengths[0]) > self.settings.request_max_bytes
        ):
            await reject(413)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.settings.request_max_bytes:
                        await reject(413)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await reject(408)
            return

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        try:
            await self.app(scope, replay, safe_send)
        except Exception:
            # Avoid server traceback logging of private exception values or payloads.
            logger.error("request_failed request_id=%s", request_id)
            if not started:
                await reject(500)
