import pytest

from arthas_mcp_proxy.server import build_parser, build_streamable_http_app


@pytest.mark.contract
def test_streamable_http_app_is_available() -> None:
    app = build_streamable_http_app()
    assert app is not None


@pytest.mark.contract
def test_cli_transport_choices_include_streamable_http() -> None:
    parser = build_parser()
    assert parser.parse_args(["--transport", "streamable-http"]).transport == "streamable-http"


@pytest.mark.contract
def test_cli_transport_choices_accept_all_supported() -> None:
    parser = build_parser()
    for transport in ("stdio", "sse", "streamable-http"):
        assert parser.parse_args(["--transport", transport]).transport == transport


@pytest.mark.contract
def test_cli_transport_default_remains_sse() -> None:
    parser = build_parser()
    assert parser.parse_args([]).transport == "sse"
