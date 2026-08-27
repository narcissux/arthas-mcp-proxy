import pytest
from starlette.testclient import TestClient

from arthas_mcp_proxy.server import build_sse_app


@pytest.mark.contract
def test_sse_app_exposes_healthz() -> None:
    app = build_sse_app()
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ready": True}


@pytest.mark.contract
def test_server_defaults_are_loopback_and_dns_protected() -> None:
    from arthas_mcp_proxy.server import _transport_security, build_parser

    assert build_parser().parse_args([]).host == "127.0.0.1"
    assert _transport_security.enable_dns_rebinding_protection is True


@pytest.mark.contract
def test_dump_params_redacts_credentials(caplog: pytest.LogCaptureFixture) -> None:
    from arthas_mcp_proxy.server import _dump_params

    with caplog.at_level("INFO"):
        _dump_params("connect_ssh", {"password": "super-secret", "host": "localhost"})

    assert "super-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text
