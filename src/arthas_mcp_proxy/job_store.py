from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

from .errors import DomainError
from .jobs import DiagnosticJob, JobStatus
from .models import ErrorCode

JOB_MAX_ACTIVE_PER_JVM = 3


class JobStore:
    def __init__(
        self,
        *,
        max_jobs: int = 100,
        ttl_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_jobs < 1 or ttl_seconds < 1:
            raise ValueError("max_jobs and ttl_seconds must be positive")
        self._jobs: dict[str, DiagnosticJob] = {}
        self._lock = threading.Lock()
        self._max_jobs, self._ttl = max_jobs, timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _quota_bucket(job: DiagnosticJob) -> str | None:
        return job.jvm_handle or job.quota_key

    def create(
        self, *, jvm_handle: str | None = None, quota_key: str | None = None
    ) -> DiagnosticJob:
        job = DiagnosticJob(
            f"job-{uuid.uuid4().hex}",
            JobStatus.RUNNING,
            self._clock(),
            jvm_handle=jvm_handle,
            quota_key=quota_key,
        )
        bucket = jvm_handle or quota_key
        with self._lock:
            self._expire_stale_locked()
            if sum(not item.is_finished for item in self._jobs.values()) >= self._max_jobs:
                raise DomainError(
                    ErrorCode.JOB_QUOTA_EXCEEDED,
                    "Maximum number of running diagnostic jobs reached",
                )
            if bucket:
                running_for_jvm = sum(
                    1
                    for item in self._jobs.values()
                    if not item.is_finished and self._quota_bucket(item) == bucket
                )
                if running_for_jvm >= JOB_MAX_ACTIVE_PER_JVM:
                    raise DomainError(
                        ErrorCode.JOB_QUOTA_EXCEEDED,
                        "Maximum number of running diagnostic jobs per JVM "
                        f"is {JOB_MAX_ACTIVE_PER_JVM}",
                    )
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> DiagnosticJob:
        with self._lock:
            self._expire_stale_locked()
            job = self._jobs.get(job_id)
        if job is None:
            raise DomainError(ErrorCode.JOB_NOT_FOUND, f"Job not found: {job_id}")
        return job

    def list(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        jvm_handle: str | None = None,
    ) -> list[DiagnosticJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            self._expire_stale_locked()
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [
                job
                for job in jobs
                if (status is None or job.status is status)
                and (jvm_handle is None or job.jvm_handle == jvm_handle)
            ][:limit]

    def _expire_stale_locked(self) -> None:
        now = self._clock()
        for job in self._jobs.values():
            if not job.is_finished and now - job.created_at >= self._ttl:
                job.status, job.completed_at = JobStatus.EXPIRED, now

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        output: str = "",
        error: dict[str, Any] | None = None,
    ) -> DiagnosticJob:
        with self._lock:
            self._expire_stale_locked()
            job = self._jobs.get(job_id)
            if job is None:
                raise DomainError(ErrorCode.JOB_NOT_FOUND, f"Job not found: {job_id}")
            if job.is_finished:
                raise DomainError(ErrorCode.JOB_ALREADY_FINISHED, f"Job already finished: {job_id}")
            job.status, job.output, job.error = status, output, error
            if status is not JobStatus.RUNNING:
                job.completed_at = self._clock()
            return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def cancel(self, job_id: str) -> DiagnosticJob:
        return self.update(job_id, status=JobStatus.CANCELLED)


class SQLiteJobStore(JobStore):
    """Single-instance SQLite MVP; deliberately not distributed observability."""

    _RESTART_ERROR = {
        "code": "JOB_RESTARTED",
        "message": "Job was running when proxy restarted",
        "phase": "startup",
        "retryable": False,
        "suggestion": "Start a new diagnostic job",
    }

    def __init__(
        self,
        database: str | Path,
        *,
        max_jobs: int = 100,
        ttl_seconds: int = 3600,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_jobs < 1 or ttl_seconds < 1:
            raise ValueError("max_jobs and ttl_seconds must be positive")
        self._database = str(database)
        self._lock = threading.Lock()
        self._max_jobs, self._ttl = max_jobs, timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, "
                "completed_at TEXT, output TEXT NOT NULL, error TEXT, jvm_handle TEXT, "
                "quota_key TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "jvm_handle" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN jvm_handle TEXT")
            if "quota_key" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN quota_key TEXT")
            db.execute(
                "UPDATE jobs SET status=?, completed_at=?, error=? WHERE status=?",
                (
                    JobStatus.FAILED.value,
                    self._clock().isoformat(),
                    json.dumps(self._RESTART_ERROR),
                    JobStatus.RUNNING.value,
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _from_row(row: sqlite3.Row) -> DiagnosticJob:
        keys = set(row.keys())
        return DiagnosticJob(
            row["job_id"],
            JobStatus(row["status"]),
            datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            row["output"],
            json.loads(row["error"]) if row["error"] else None,
            jvm_handle=row["jvm_handle"] if "jvm_handle" in keys else None,
            quota_key=row["quota_key"] if "quota_key" in keys else None,
        )

    def _expire_locked(self, db: sqlite3.Connection) -> None:
        now = self._clock()
        db.execute(
            "UPDATE jobs SET status=?, completed_at=? WHERE status=? AND created_at<=?",
            (
                JobStatus.EXPIRED.value,
                now.isoformat(),
                JobStatus.RUNNING.value,
                (now - self._ttl).isoformat(),
            ),
        )

    def _count_running_for_quota(self, db: sqlite3.Connection, bucket: str) -> int:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=? AND "
                "(jvm_handle=? OR (jvm_handle IS NULL AND quota_key=?))",
                (JobStatus.RUNNING.value, bucket, bucket),
            ).fetchone()[0]
        )

    def create(
        self, *, jvm_handle: str | None = None, quota_key: str | None = None
    ) -> DiagnosticJob:
        job = DiagnosticJob(
            f"job-{uuid.uuid4().hex}",
            JobStatus.RUNNING,
            self._clock(),
            jvm_handle=jvm_handle,
            quota_key=quota_key,
        )
        bucket = jvm_handle or quota_key
        with self._lock, self._connect() as db:
            self._expire_locked(db)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status=?", (JobStatus.RUNNING.value,)
                ).fetchone()[0]
                >= self._max_jobs
            ):
                raise DomainError(
                    ErrorCode.JOB_QUOTA_EXCEEDED,
                    "Maximum number of running diagnostic jobs reached",
                )
            running_for_jvm = self._count_running_for_quota(db, bucket) if bucket else 0
            if bucket and running_for_jvm >= JOB_MAX_ACTIVE_PER_JVM:
                raise DomainError(
                    ErrorCode.JOB_QUOTA_EXCEEDED,
                    "Maximum number of running diagnostic jobs per JVM "
                    f"is {JOB_MAX_ACTIVE_PER_JVM}",
                )
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job.job_id,
                    job.status.value,
                    job.created_at.isoformat(),
                    None,
                    job.output,
                    None,
                    job.jvm_handle,
                    job.quota_key,
                ),
            )
        return job

    def get(self, job_id: str) -> DiagnosticJob:
        with self._lock, self._connect() as db:
            self._expire_locked(db)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise DomainError(ErrorCode.JOB_NOT_FOUND, f"Job not found: {job_id}")
        return self._from_row(row)

    def list(
        self,
        *,
        status: JobStatus | None = None,
        limit: int = 50,
        jvm_handle: str | None = None,
    ) -> list[DiagnosticJob]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock, self._connect() as db:
            self._expire_locked(db)
            clauses: list[str] = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status=?")
                params.append(status.value)
            if jvm_handle is not None:
                clauses.append("jvm_handle=?")
                params.append(jvm_handle)
            query = "SELECT * FROM jobs"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            rows = db.execute(
                query + " ORDER BY created_at DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus,
        output: str = "",
        error: dict[str, Any] | None = None,
    ) -> DiagnosticJob:
        with self._lock, self._connect() as db:
            self._expire_locked(db)
            row = db.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise DomainError(ErrorCode.JOB_NOT_FOUND, f"Job not found: {job_id}")
            if JobStatus(row["status"]) is not JobStatus.RUNNING:
                raise DomainError(ErrorCode.JOB_ALREADY_FINISHED, f"Job already finished: {job_id}")
            db.execute(
                "UPDATE jobs SET status=?, output=?, error=?, completed_at=? WHERE job_id=?",
                (
                    status.value,
                    output,
                    json.dumps(error) if error is not None else None,
                    self._clock().isoformat() if status is not JobStatus.RUNNING else None,
                    job_id,
                ),
            )
        return self.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("DELETE FROM jobs WHERE job_id=?", (job_id,)).rowcount > 0

    def cancel(self, job_id: str) -> DiagnosticJob:
        return self.update(job_id, status=JobStatus.CANCELLED)


_INSTALLED_STORE: JobStore | None = None


def install_job_store(store: JobStore) -> None:
    global _INSTALLED_STORE
    _INSTALLED_STORE = store


def get_job_store() -> JobStore | None:
    return _INSTALLED_STORE
