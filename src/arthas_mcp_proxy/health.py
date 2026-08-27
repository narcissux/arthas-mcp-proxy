from typing import Any


def health_payload() -> dict[str, Any]:
    return {"status": "ok", "ready": True}
