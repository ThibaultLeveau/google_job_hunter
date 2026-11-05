import asyncio
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from starlette.types import Message, Receive, Scope, Send

from job_hunter.api.boundary import RequestBoundary
from job_hunter.app import create_app
from job_hunter.domain.errors import DomainError
from job_hunter.settings import ConfigurationError, Settings, load_settings


class Submitted(BaseModel):
    count: int
    metadata: dict[str, int] = Field(default_factory=dict)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(docs_enabled=True, request_max_bytes=128))

    @app.post("/api/v1/check")
    def check(value: Submitted) -> dict[str, int]:
        return {"count": value.count}

    @app.get("/api/v1/fail/{kind}")
    def fail(kind: str) -> None:
        if kind in {"not_found", "conflict"}:
            raise DomainError("not_found" if kind == "not_found" else "conflict")
        if kind == "http":
            raise HTTPException(418, detail="private-canary")
        raise RuntimeError("private-canary")

    with TestClient(app, base_url="https://testserver") as result:
        yield result


def test_validation_is_typed_redacted_and_correlated(client: TestClient) -> None:
    request_id = str(uuid4())
    response = client.post(
        "/api/v1/check",
        headers={"X-Request-ID": request_id},
        json={
            "count": "private-canary",
            "metadata": {"private-key": "private-value"},
        },
    )
    body = response.json()
    assert response.status_code == 422
    assert body["code"] == "validation_error"
    assert {field["field"] for field in body["field_errors"]} == {"count", "metadata"}
    assert body["request_id"] == response.headers["x-request-id"] == request_id
    assert "private" not in response.text
    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/api/v1/check"]["post"]["responses"]["422"]
    assert response_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorEnvelope"
    )


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("conflict", 409, "conflict"),
        ("http", 418, "request_failed"),
        ("unexpected", 500, "internal_error"),
    ],
)
def test_errors_do_not_leak(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
    path: str,
    status: int,
    code: str,
) -> None:
    response = client.get(f"/api/v1/fail/{path}")
    assert response.status_code == status
    assert response.json()["code"] == code
    assert "private-canary" not in response.text + caplog.text
    assert UUID(response.json()["request_id"])
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Host": "evil.test"}, 400),
        ({"Host": "testserver:bad"}, 400),
        ({"Host": "testserver#"}, 400),
        ({"Host": "user@testserver"}, 400),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://evil.test"}, 403),
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "999999999999999999"}, 413),
    ],
)
def test_hostile_headers(
    client: TestClient, headers: dict[str, str], status: int
) -> None:
    response = client.post("/api/v1/check", headers=headers, content=b"{}")
    assert response.status_code == status
    assert "access-control-allow-origin" not in response.headers
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_exact_cors_and_normal_contracts(client: TestClient) -> None:
    headers = {
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "Content-Type, X-Request-ID",
    }
    assert client.options("/api/v1/check", headers=headers).status_code == 204
    response = client.post(
        "/api/v1/check", headers={"Origin": headers["Origin"]}, json={"count": 1}
    )
    assert response.json() == {"count": 1}
    assert response.headers["access-control-allow-origin"] == headers["Origin"]
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "Origin" in response.headers["vary"]
    assert (
        response.headers["content-security-policy"]
        == "default-src 'none'; frame-ancestors 'none'"
    )
    headers["Access-Control-Request-Headers"] = "X-Forbidden"
    assert client.options("/api/v1/check", headers=headers).status_code == 400
    assert client.get("/api/health/live").json() == {"status": "ok"}
    assert client.get("/api/health/ready").json() == {"status": "unavailable"}
    method_error = client.get("/api/v1/check")
    assert method_error.status_code == 405 and method_error.headers["allow"] == "POST"
    assert (
        client.post(
            "/api/v1/check", content=b"{", headers={"Content-Type": "application/json"}
        ).json()["field_errors"][0]["field"]
        == "request"
    )


@pytest.mark.parametrize("value", ["invalid", "x" * 36])
def test_invalid_request_ids_are_replaced(client: TestClient, value: str) -> None:
    response = client.get("/api/health/live", headers={"X-Request-ID": value})
    assert str(UUID(response.headers["x-request-id"])) != value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("TRUSTED_HOSTS", '["*"]'),
        ("TRUSTED_HOSTS", "[]"),
        ("TRUSTED_HOSTS", "private-canary"),
        ("ALLOWED_ORIGINS", '["null"]'),
        ("ALLOWED_ORIGINS", '["https://a.test/path"]'),
        ("ALLOWED_ORIGINS", '["https://user:private-canary@a.test"]'),
        ("ALLOWED_ORIGINS", '["https://*.test"]'),
        ("REQUEST_MAX_BYTES", "0"),
    ],
)
def test_invalid_boundary_settings(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOB_HUNTER_" + name, value)
    with pytest.raises(ConfigurationError) as raised:
        load_settings()
    assert "private-canary" not in str(raised.value)


@pytest.mark.parametrize(
    "case", ["chunked", "disconnect", "timeout", "replay", "stream_failure", "non_http"]
)
def test_asgi_body_boundaries(case: str, caplog: pytest.LogCaptureFixture) -> None:
    async def exercise() -> list[Message]:
        sent: list[Message] = []
        messages: Iterator[Message] = iter(
            [
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"def", "more_body": False},
                {"type": "http.disconnect"},
            ]
        )

        async def receive() -> Message:
            if case == "timeout":
                raise TimeoutError
            if case == "disconnect":
                return {"type": "http.disconnect"}
            return next(messages)

        async def send(message: Message) -> None:
            sent.append(message)

        async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close"})
                return
            assert (await receive())["body"] == b"abcdef"
            assert (await receive())["type"] == "http.disconnect"
            await send({"type": "http.response.start", "status": 200, "headers": []})
            if case == "stream_failure":
                raise RuntimeError("private-canary")
            await send({"type": "http.response.body", "body": b"ok"})

        scope: Scope = {
            "type": "websocket" if case == "non_http" else "http",
            "scheme": "http",
            "path": "/api/v1/check",
            "method": "POST",
            "headers": [(b"host", b"testserver")],
        }
        await RequestBoundary(
            downstream, Settings(request_max_bytes=5 if case == "chunked" else 6)
        )(scope, receive, send)
        return sent

    sent = asyncio.run(exercise())
    if case == "disconnect":
        assert sent == []
    elif case == "non_http":
        assert sent == [{"type": "websocket.close"}]
    else:
        assert sent[0]["status"] == {"chunked": 413, "timeout": 408}.get(case, 200)
    assert "private-canary" not in caplog.text
