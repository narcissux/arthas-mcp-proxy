import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from .errors import DomainError
from .models import ErrorCode


@dataclass(frozen=True)
class LimitedOutput:
    text: str
    truncated: bool
    original_chars: int
    returned_chars: int


@dataclass(frozen=True)
class OutputPage:
    text: str
    next_cursor: str | None


_CURSOR_SECRET = os.environ.get("ARTHAS_MCP_CURSOR_SECRET")
_CURSOR_SECRET_BYTES = _CURSOR_SECRET.encode() if _CURSOR_SECRET else secrets.token_bytes(32)
_CURSOR_VERSION = 1
_DEFAULT_CURSOR_TTL_SECONDS = 300


def limit_output(text: str, max_chars: int) -> LimitedOutput:
    if max_chars < 0:
        raise ValueError("max_chars must be non-negative")
    original_chars = len(text)
    limited = text[:max_chars]
    return LimitedOutput(
        text=limited,
        truncated=original_chars > max_chars,
        original_chars=original_chars,
        returned_chars=len(limited),
    )


def _encode_cursor(offset: int, job_id: str, expires_at: int) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "job": job_id, "offset": offset, "exp": expires_at},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=")
    signature = hmac.new(_CURSOR_SECRET_BYTES, encoded, hashlib.sha256).digest()
    signed = base64.urlsafe_b64encode(signature).rstrip(b"=")
    return f"{encoded.decode()}.{signed.decode()}"


def _decode_cursor(cursor: str, job_id: str, now: int) -> int:
    try:
        encoded, encoded_signature = cursor.split(".", 1)
        provided_signature = base64.urlsafe_b64decode(encoded_signature + "===")
        if base64.urlsafe_b64encode(provided_signature).rstrip(b"=").decode() != encoded_signature:
            raise ValueError
        expected_signature = hmac.new(
            _CURSOR_SECRET_BYTES, encoded.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(provided_signature, expected_signature):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded + "===").decode())
        if (
            payload.get("v") != _CURSOR_VERSION
            or payload.get("job") != job_id
            or not isinstance(payload.get("offset"), int)
            or payload.get("offset") < 0
            or not isinstance(payload.get("exp"), int)
            or payload["exp"] <= now
        ):
            raise ValueError
        return int(payload["offset"])
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ):
        raise DomainError(ErrorCode.OUTPUT_CURSOR_INVALID, "Invalid output cursor") from None


def paginate_output(
    text: str,
    cursor: str | None,
    max_chars: int,
    *,
    job_id: str | None = None,
    cursor_ttl_seconds: int = _DEFAULT_CURSOR_TTL_SECONDS,
) -> OutputPage:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if cursor_ttl_seconds < 0:
        raise ValueError("cursor_ttl_seconds must be non-negative")
    if cursor is None:
        offset = 0
    elif job_id is None:
        # Legacy compatibility for non-server callers. Server requests are job-bound.
        try:
            offset = int(cursor)
        except ValueError as exc:
            raise DomainError(ErrorCode.OUTPUT_CURSOR_INVALID, "Invalid output cursor") from exc
    else:
        offset = _decode_cursor(cursor, job_id, int(time.time()))
    if offset < 0 or offset > len(text):
        raise DomainError(ErrorCode.OUTPUT_CURSOR_INVALID, "Invalid output cursor")
    end = min(offset + max_chars, len(text))
    if end >= len(text):
        return OutputPage(text[offset:end], None)
    next_cursor = (
        str(end)
        if job_id is None
        else _encode_cursor(end, job_id, int(time.time()) + cursor_ttl_seconds)
    )
    return OutputPage(text[offset:end], next_cursor)
