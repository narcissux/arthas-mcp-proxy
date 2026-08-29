from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class JobStatus(Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class DiagnosticJob:
    job_id: str
    status: JobStatus
    created_at: datetime
    completed_at: datetime | None = None
    output: str = ""
    error: dict[str, Any] | None = None
    jvm_handle: str | None = None
    quota_key: str | None = None

    @property
    def is_finished(self) -> bool:
        return self.status is not JobStatus.RUNNING
