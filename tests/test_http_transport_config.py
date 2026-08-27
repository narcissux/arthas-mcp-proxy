from __future__ import annotations

from unittest.mock import patch

import pytest

from arthas_mcp_proxy import server


def test_http_tls_requires_both_files() -> None:
    with pytest.raises(ValueError, match="both certificate and key"):
        server.validate_tls_config("cert.pem", None)
    with pytest.raises(ValueError, match="both certificate and key"):
        server.validate_tls_config(None, "key.pem")


def test_http_tls_is_configured_for_sse_too() -> None:
    with patch("arthas_mcp_proxy.server.uvicorn.run") as run:
        server.run_sse(
            host="127.0.0.1",
            port=8443,
            ssl_certfile="cert.pem",
            ssl_keyfile="key.pem",
        )

    assert run.call_args.kwargs["ssl_certfile"] == "cert.pem"
    assert run.call_args.kwargs["ssl_keyfile"] == "key.pem"


def test_streamable_http_app_auth_is_not_bypassed_by_health_or_mcp_routes() -> None:
    app = server.build_streamable_http_app("secret")
    from starlette.testclient import TestClient

    client = TestClient(app)
    assert client.get("/mcp").status_code == 401
