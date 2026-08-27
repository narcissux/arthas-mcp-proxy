from dataclasses import dataclass

from .models import ErrorCode, ErrorDetail


@dataclass
class DomainError(Exception):
    code: ErrorCode
    message: str
    retryable: bool = False
    phase: str | None = None
    suggestion: str | None = None

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)


class SSHPoolExhaustedError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.SSH_POOL_EXHAUSTED, message, retryable=True, phase="connect")


class SSHTransportLostError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.SSH_TRANSPORT_LOST, message, retryable=True, phase="resolve")


def to_error_detail(error: DomainError) -> ErrorDetail:
    return ErrorDetail(
        code=error.code,
        message=error.message,
        phase=error.phase,
        retryable=error.retryable,
        suggestion=error.suggestion,
    )


def map_exception(error: Exception) -> ErrorDetail:
    if isinstance(error, DomainError):
        return to_error_detail(error)
    if isinstance(error, TimeoutError):
        return ErrorDetail(code=ErrorCode.COMMAND_TIMEOUT, message=str(error), retryable=True)
    if isinstance(error, ValueError):
        return ErrorDetail(code=ErrorCode.INVALID_ARGUMENT, message=str(error))
    if isinstance(error, ConnectionError):
        code = getattr(error, "code", "unreachable")
        mapped = {
            "command_failed": ErrorCode.ARTHAS_COMMAND_FAILED,
            "protocol_error": ErrorCode.ARTHAS_UNREACHABLE,
            "unreachable": ErrorCode.ARTHAS_UNREACHABLE,
        }.get(code, ErrorCode.ARTHAS_UNREACHABLE)
        return ErrorDetail(
            code=mapped,
            message=str(error),
            retryable=mapped is ErrorCode.ARTHAS_UNREACHABLE,
        )
    if isinstance(error, DomainError):
        return to_error_detail(error)
    return ErrorDetail(code=ErrorCode.INTERNAL_ERROR, message=str(error))
