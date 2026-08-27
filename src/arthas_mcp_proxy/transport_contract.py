def supported_transports() -> tuple[str, str, str]:
    """Return transports currently implemented by the server."""
    return ("stdio", "sse", "streamable-http")
