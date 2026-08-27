import json
from typing import Any

from .jobs import DiagnosticJob


def serialize_job(job: DiagnosticJob) -> str:
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "output": job.output,
    }
    if job.error is not None:
        payload["error"] = job.error
    return json.dumps(payload)
