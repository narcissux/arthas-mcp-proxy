from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arthas_mcp_proxy.server import (
    build_auth_middleware,
    validate_transport_security,
)


def test_unconfigured_auth_allows_loopback_only() -> None:
    validate_transport_security("127.0.0.1", None)
    validate_transport_security("localhost", "")
    with pytest.raises(ValueError, match="loopback"):
        validate_transport_security("0" + ".0.0.0", None)  # noqa: S104


def test_token_auth_requires_bearer_header_and_does_not_echo_secret() -> None:
    app = Starlette()

    async def endpoint(_request):
        return PlainTextResponse("ok")

    app.router.routes.append(Route("/mcp", endpoint))
    secured = build_auth_middleware(app, "test-token")
    client = TestClient(secured)

    assert client.get("/mcp").status_code == 401
    assert client.get("/mcp", headers={"Authorization": "Basic test-token"}).status_code == 401
    response = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert "test-token" not in response.text
    assert client.get("/mcp", headers={"Authorization": "Bearer test-token"}).text == "ok"


def test_token_auth_rejects_duplicate_or_extra_bearer_values() -> None:
    app = Starlette()

    async def endpoint(_request):
        return PlainTextResponse("ok")

    app.router.routes.append(Route("/mcp", endpoint))
    client = TestClient(build_auth_middleware(app, "test-token"))
    assert (
        client.get("/mcp", headers={"Authorization": "Bearer test-token extra"}).status_code == 401
    )
    assert (
        client.get(
            "/mcp", headers={"Authorization": "Bearer test-token, Bearer test-token"}
        ).status_code
        == 401
    )
