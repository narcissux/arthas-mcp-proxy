from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, model_validator


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
    SSH_CONNECT_TIMEOUT = "SSH_CONNECT_TIMEOUT"
    SSH_TRANSPORT_LOST = "SSH_TRANSPORT_LOST"
    SSH_POOL_EXHAUSTED = "SSH_POOL_EXHAUSTED"
    SSH_COMMAND_TIMEOUT = "SSH_COMMAND_TIMEOUT"
    JVM_NOT_FOUND = "JVM_NOT_FOUND"
    JVM_AMBIGUOUS = "JVM_AMBIGUOUS"
    JVM_IDENTITY_CHANGED = "JVM_IDENTITY_CHANGED"
    JVM_EXITED = "JVM_EXITED"
    ARTHAS_NOT_INSTALLED = "ARTHAS_NOT_INSTALLED"
    ARTHAS_INSTALL_FAILED = "ARTHAS_INSTALL_FAILED"
    ARTHAS_ATTACH_FAILED = "ARTHAS_ATTACH_FAILED"
    ARTHAS_UNREACHABLE = "ARTHAS_UNREACHABLE"
    ARTHAS_COMMAND_FAILED = "ARTHAS_COMMAND_FAILED"
    COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"
    OBSERVATION_LIMIT_EXCEEDED = "OBSERVATION_LIMIT_EXCEEDED"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_ALREADY_FINISHED = "JOB_ALREADY_FINISHED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_QUOTA_EXCEEDED = "JOB_QUOTA_EXCEEDED"
    OUTPUT_CURSOR_INVALID = "OUTPUT_CURSOR_INVALID"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ResultMeta(BaseModel):
    request_id: str
    duration_ms: int
    backend: Literal["ssh", "arthas_cli", "arthas_http", "arthas_ws"] | None = None
    degraded: bool = False
    truncated: bool = False
    original_chars: int | None = None
    returned_chars: int | None = None
    next_cursor: str | None = None


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    phase: str | None = None
    retryable: bool = False
    suggestion: str | None = None


class ToolResult(BaseModel):
    status: Literal["success", "running", "error"]
    summary: str
    data: dict[str, Any] | list[Any] | None = None
    error: ErrorDetail | None = None
    meta: ResultMeta

    @model_validator(mode="after")
    def validate_error_status(self) -> "ToolResult":
        if self.status == "error" and self.error is None:
            raise ValueError("error detail is required when status is error")
        return self
