import pytest

from arthas_mcp_proxy.server import build_streamable_http_app


@pytest.mark.e2e
@pytest.mark.contract
def test_streamable_http_app_exposes_asgi_routes() -> None:
    app = build_streamable_http_app()
    routes = {getattr(route, "path", None) for route in app.routes}
    assert routes
    assert any(path in routes for path in {"/mcp", "/"})
