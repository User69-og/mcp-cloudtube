"""Durable record of every upload attempt.

Why uploads are jobs rather than a blocking call
------------------------------------------------
A video upload takes minutes. An MCP tool call that blocks for minutes is a
chat that appears frozen, and a client that may time out and retry -- which,
for an upload, means publishing the same video twice. So
``upload_video_to_youtube`` starts a background job and returns a job ID
immediately, and ``get_upload_status`` reports on it. That is also exactly the
shape the brief's tool list implies by asking for both tools.

Why the record is on disk rather than in memory
-----------------------------------------------
The client owns this process's lifetime. Claude Desktop restarts its MCP
servers freely -- on reconnect, on config change, on its own restart. An
in-memory job table means that after a restart the answer to "did my upload
finish?" is "I have no idea", and the only safe thing the model can do is
upload again. SQLite costs little and makes the answer survive.

It also makes the resume URI durable, so an upload interrupted by a restart
can continue from the last acknowledged byte rather than re-sending the file
and re-spending 1600 quota units.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, ToolError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class JobState(StrEnum):
    """Lifecycle of one upload.

    Split finely on purpose: "it is at 40%" is a different user-facing message
    depending on whether we are pulling bytes out of Drive or pushing them to
    YouTube, and `PROCESSING` (YouTube transcoding, after our bytes are all
    sent) is the state people most often mistake for a stall.
    """

    PENDING = "pending"
    STAGING = "staging"        # downloading from storage to local disk
    UPLOADING = "uploading"    # sending to YouTube
    PROCESSING = "processing"  # YouTube has the bytes and is transcoding
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}

    @property
    def resumable(self) -> bool:
        return self in {JobState.STAGING, JobState.UPLOADING, JobState.PENDING}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_jobs (
    id                 TEXT PRIMARY KEY,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    state              TEXT NOT NULL,
    source_file_id     TEXT NOT NULL,
    source_file_name   TEXT,
    title              TEXT NOT NULL,
    description        TEXT,
    tags               TEXT,
    requested_privacy  TEXT NOT NULL,
    applied_privacy    TEXT,
    category_id        TEXT,
    made_for_kids      INTEGER NOT NULL DEFAULT 0,
    video_id           TEXT,
    video_url          TEXT,
    bytes_total        INTEGER,
    bytes_sent         INTEGER NOT NULL DEFAULT 0,
    resume_uri         TEXT,
    staged_path        TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    error_code         TEXT,
    error_message      TEXT,
    error_remediation  TEXT,
    idempotency_key    TEXT,
    notes              TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_created  ON upload_jobs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_state    ON upload_jobs (state);
CREATE INDEX IF NOT EXISTS idx_jobs_idemp    ON upload_jobs (idempotency_key);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    """SQLite-backed upload history.

    Thread-safe: upload work happens on worker threads while tool calls are
    served on the event loop, so both touch this store. One connection guarded
    by a lock is simpler to reason about than a connection pool, and the
    workload is a handful of small writes per upload.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._path),
                check_same_thread=False,
                timeout=10.0,
                isolation_level=None,  # autocommit; each statement is its own txn
            )
            conn.row_factory = sqlite3.Row
            # WAL so a reader (get_upload_status) is never blocked by the
            # writer (the running upload) -- the whole point of this store is
            # being able to ask "how is it going?" mid-upload.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            self._conn = conn
            logger.info("jobs.connected", path=str(self._path))
        except sqlite3.Error as exc:
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                f"Could not open the upload history database at {self._path}: {exc}",
                remediation=(
                    "The server records uploads there so their status survives a restart. "
                    "Check the directory is writable, or set CLOUDTUBE_STATE_DIR elsewhere."
                ),
            ) from exc

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        self.connect()
        assert self._conn is not None
        with self._lock:
            cursor = self._conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    # -- writes --------------------------------------------------------------

    def create(
        self,
        *,
        source_file_id: str,
        source_file_name: str | None,
        title: str,
        description: str | None,
        tags: list[str],
        requested_privacy: str,
        category_id: str | None,
        made_for_kids: bool,
        bytes_total: int | None,
        idempotency_key: str | None,
    ) -> str:
        job_id = f"job_{uuid.uuid4().hex[:16]}"
        now = _now()
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO upload_jobs (
                    id, created_at, updated_at, state, source_file_id, source_file_name,
                    title, description, tags, requested_privacy, category_id,
                    made_for_kids, bytes_total, idempotency_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id, now, now, JobState.PENDING, source_file_id, source_file_name,
                    title, description, json.dumps(tags), requested_privacy, category_id,
                    int(made_for_kids), bytes_total, idempotency_key,
                ),
            )
        logger.info("jobs.created", job_id=job_id, source_file_id=source_file_id)
        return job_id

    def update(self, job_id: str, **fields: Any) -> None:
        """Patch a job. Unknown columns are rejected loudly rather than ignored."""
        if not fields:
            return
        allowed = {
            "state", "applied_privacy", "video_id", "video_url", "bytes_total",
            "bytes_sent", "resume_uri", "staged_path", "attempts", "error_code",
            "error_message", "error_remediation", "source_file_name", "notes",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Cannot update unknown job fields: {sorted(unknown)}")

        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = [
            str(v) if isinstance(v, JobState) else v for v in fields.values()
        ]
        with self._cursor() as cursor:
            cursor.execute(
                f"UPDATE upload_jobs SET {assignments}, updated_at = ? WHERE id = ?",
                (*values, _now(), job_id),
            )

    def record_failure(self, job_id: str, error: ToolError) -> None:
        """Persist a failure in full, so get_upload_status can explain it later.

        The remediation is stored alongside the code because the model that
        asks for status afterwards may be a different turn, or a different
        session, from the one that started the upload. It should not have to
        reconstruct what to do about ``UPLOAD_FAILED``.
        """
        self.update(
            job_id,
            state=JobState.FAILED,
            error_code=str(error.code),
            error_message=error.message,
            error_remediation=error.remediation,
        )
        logger.warning("jobs.failed", job_id=job_id, code=str(error.code))

    def increment_attempts(self, job_id: str) -> int:
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE upload_jobs SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (_now(), job_id),
            )
            cursor.execute("SELECT attempts FROM upload_jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
        return int(row["attempts"]) if row else 0

    # -- reads ----------------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM upload_jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
        return _row_to_dict(row) if row else None

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM upload_jobs ORDER BY created_at DESC LIMIT ?",
                (min(max(1, limit), 100),),
            )
            rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        """Find a prior job with the same key, to avoid double-publishing.

        Scoped to non-failed jobs: if the previous attempt failed outright,
        the user genuinely does want to try again, and blocking that would be
        the wrong kind of safety.
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM upload_jobs
                WHERE idempotency_key = ? AND state != ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (key, JobState.FAILED),
            )
            row = cursor.fetchone()
        return _row_to_dict(row) if row else None

    def interrupted(self) -> list[dict[str, Any]]:
        """Jobs left mid-flight by a crash or restart.

        Read at startup so the server can mark them honestly instead of
        leaving them to look eternally in-progress.
        """
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM upload_jobs WHERE state IN (?,?,?)",
                (JobState.PENDING, JobState.STAGING, JobState.UPLOADING),
            )
            rows = cursor.fetchall()
        return [_row_to_dict(row) for row in rows]

    def mark_interrupted_jobs(self) -> int:
        """Called at startup. Does not destroy resume information.

        A job whose resume URI is still on record stays recoverable; one
        without is marked failed, because we have no way to continue it and
        pretending otherwise would mislead the user into waiting.
        """
        count = 0
        for job in self.interrupted():
            if job.get("resume_uri") and job["state"] == JobState.UPLOADING:
                self.update(
                    job["id"],
                    notes=(
                        "The server restarted while this upload was in flight. YouTube still "
                        "holds the partial upload; it can be resumed."
                    ),
                )
            else:
                self.record_failure(
                    job["id"],
                    ToolError(
                        ErrorCode.UPLOAD_FAILED,
                        "The server stopped while this upload was in progress, and it had not "
                        "reached a point YouTube can resume from.",
                        remediation=(
                            "Nothing was published. Start the upload again with "
                            "upload_video_to_youtube."
                        ),
                    ),
                )
            count += 1
        if count:
            logger.info("jobs.interrupted_marked", count=count)
        return count


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    raw_tags = data.get("tags")
    if raw_tags:
        try:
            data["tags"] = json.loads(raw_tags)
        except (ValueError, TypeError):
            data["tags"] = []
    else:
        data["tags"] = []
    data["made_for_kids"] = bool(data.get("made_for_kids"))
    return data


__all__ = ["JobState", "JobStore"]
