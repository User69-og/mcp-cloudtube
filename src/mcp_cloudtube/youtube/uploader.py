"""Orchestrates one upload: validate, stage, send, verify, clean up.

The pipeline is deliberately ordered cheapest-check-first, so that the
failures a user is most likely to hit cost the least:

1.  **Validate metadata.** Pure local work. A bad title fails here, before a
    byte moves.
2.  **Check for a duplicate request.** A client that times out and retries
    must not publish the same video twice.
3.  **Confirm the channel exists.** One cheap API call. An account with no
    YouTube channel fails here rather than after staging two gigabytes.
4.  **Stage from storage to local disk.** Now we spend bandwidth.
5.  **Upload to YouTube.** Now we spend quota.
6.  **Read the video back.** Confirms what YouTube actually applied, rather
    than assuming the request was honoured.
7.  **Clean up**, always, including on failure and cancellation.

Each step writes its outcome to the job record before moving on, so a crash
at any point leaves a job whose state is true rather than one stuck at
"uploading" forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import ErrorCode, ToolError
from ..logging_setup import get_logger
from ..storage.base import StorageProvider
from .client import YouTubeTransport, describe_video_status
from .jobs import JobState, JobStore
from .metadata import ValidatedMetadata, privacy_mismatch_error, validate

logger = get_logger(__name__)


class UploadCoordinator:
    """Starts uploads, tracks them, and answers questions about them."""

    def __init__(
        self,
        *,
        storage: StorageProvider,
        youtube: YouTubeTransport,
        jobs: JobStore,
        settings: Settings,
    ) -> None:
        self._storage = storage
        self._youtube = youtube
        self._jobs = jobs
        self._settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()

    # -- starting ------------------------------------------------------------

    async def start(
        self,
        *,
        file_id: str,
        title: str,
        description: str | None,
        tags: list[str] | None,
        privacy_status: str,
        category_id: str | None,
        made_for_kids: bool,
    ) -> dict[str, Any]:
        """Validate, de-duplicate and launch. Returns immediately."""

        # 1. Local validation. Cheapest possible failure.
        metadata = validate(
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            category_id=category_id,
            made_for_kids=made_for_kids,
        )

        # 2. Duplicate suppression.
        key = _idempotency_key(file_id, metadata)
        if existing := self._jobs.find_by_idempotency_key(key):
            logger.info("upload.duplicate_suppressed", job_id=existing["id"])
            return {
                "job_id": existing["id"],
                "state": existing["state"],
                "duplicate_of": existing["id"],
                "video_url": existing.get("video_url"),
                "message": (
                    "An upload of this same file with the same title and privacy was already "
                    f"started (job {existing['id']}, currently {existing['state']}). Returning "
                    "that job instead of publishing the video a second time. Use "
                    "get_upload_status to follow it, or change the title to force a separate "
                    "upload."
                ),
            }

        # 3. Confirm there is a channel to upload to.
        channel = await self._youtube.check_access()

        # 4. Describe the source before committing, so the job record is
        #    meaningful even if staging fails immediately.
        source = await self._storage.get_metadata(file_id)
        if not source.is_video:
            raise ToolError(
                ErrorCode.INVALID_INPUT,
                f"{source.summary()} is not a video file, so it cannot be uploaded to YouTube.",
                remediation=(
                    "Use search_files with videos_only=true to find an actual video, then "
                    "pass that file's ID."
                ),
                details={"file_id": file_id, "mime_type": source.mime_type},
            )

        job_id = self._jobs.create(
            source_file_id=file_id,
            source_file_name=source.name,
            title=metadata.title,
            description=metadata.description,
            tags=metadata.tags,
            requested_privacy=metadata.privacy_status,
            category_id=metadata.category_id,
            made_for_kids=metadata.made_for_kids,
            bytes_total=source.size_bytes,
            idempotency_key=key,
        )

        task = asyncio.create_task(
            self._run(job_id, file_id, metadata), name=f"upload-{job_id}"
        )
        self._tasks[job_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(job_id, None))

        return {
            "job_id": job_id,
            "state": JobState.PENDING,
            "channel_title": channel.get("channel_title"),
            "source_file": source.name,
            "size_bytes": source.size_bytes,
            "requested_privacy": metadata.privacy_status,
            "message": (
                f"Upload started as job {job_id}. This runs in the background; call "
                "get_upload_status with this job ID to follow it. Do not start another "
                "upload of the same file while this one is running."
            ),
        }

    # -- the pipeline ----------------------------------------------------------

    async def _run(self, job_id: str, file_id: str, metadata: ValidatedMetadata) -> None:
        """Background worker. Must never raise: a dead task is an invisible failure."""
        staged: Path | None = None
        try:
            self._jobs.increment_attempts(job_id)

            # --- stage -------------------------------------------------------
            self._jobs.update(job_id, state=JobState.STAGING, bytes_sent=0)
            staged = _temp_path(self._settings, job_id)
            self._jobs.update(job_id, staged_path=str(staged))

            def staging_progress(done: int, total: int | None) -> None:
                self._jobs.update(job_id, bytes_sent=done, bytes_total=total)

            source = await self._storage.download_to_path(
                file_id, staged, progress=staging_progress
            )
            self._jobs.update(
                job_id, bytes_total=staged.stat().st_size, bytes_sent=0,
                source_file_name=source.name,
            )

            if self._is_cancelled(job_id):
                raise ToolError(
                    ErrorCode.CANCELLED,
                    "The upload was cancelled while the file was being staged.",
                    remediation="Nothing was uploaded to YouTube.",
                )

            # --- upload -------------------------------------------------------
            self._jobs.update(job_id, state=JobState.UPLOADING)

            def upload_progress(sent: int, total: int | None) -> None:
                self._jobs.update(job_id, bytes_sent=sent, bytes_total=total)

            def remember_resume_uri(uri: str) -> None:
                self._jobs.update(job_id, resume_uri=uri)

            outcome = await self._youtube.upload(
                staged,
                metadata,
                on_progress=upload_progress,
                on_resume_uri=remember_resume_uri,
                should_cancel=lambda: self._is_cancelled(job_id),
            )

            self._jobs.update(
                job_id,
                state=JobState.PROCESSING,
                video_id=outcome.video_id,
                video_url=outcome.video_url,
                applied_privacy=outcome.applied_privacy,
            )

            # --- YouTube's own verdict ------------------------------------------
            if outcome.rejection_reason:
                raise ToolError(
                    ErrorCode.VIDEO_REJECTED,
                    f"YouTube rejected the video: {outcome.rejection_reason}",
                    remediation=(
                        "This is a content or policy decision and will not change on retry. "
                        "Relay the reason to the user."
                    ),
                    details={"video_id": outcome.video_id, "video_url": outcome.video_url},
                )
            if outcome.failure_reason:
                raise ToolError(
                    ErrorCode.UPLOAD_FAILED,
                    f"YouTube could not process the video: {outcome.failure_reason}",
                    remediation=(
                        "The bytes arrived but the video is unusable. Check the source file "
                        "plays correctly, then try again."
                    ),
                    details={"video_id": outcome.video_id},
                )

            # --- did the requested privacy actually stick? -----------------------
            applied = outcome.applied_privacy
            if applied and applied != metadata.privacy_status:
                error = privacy_mismatch_error(
                    metadata.privacy_status, applied, outcome.video_id, outcome.video_url
                )
                # Note, not failure: the video *is* published. Recording this as
                # FAILED would tell the user nothing was uploaded, which is
                # false and would invite a duplicate upload.
                self._jobs.update(
                    job_id,
                    state=JobState.COMPLETED,
                    applied_privacy=applied,
                    error_code=str(error.code),
                    error_message=error.message,
                    error_remediation=error.remediation,
                )
                logger.warning(
                    "upload.privacy_not_applied",
                    job_id=job_id,
                    requested=metadata.privacy_status,
                    applied=applied,
                )
                return

            self._jobs.update(job_id, state=JobState.COMPLETED)
            logger.info("upload.completed", job_id=job_id, video_id=outcome.video_id)

        except ToolError as error:
            self._jobs.record_failure(job_id, error)
        except asyncio.CancelledError:
            self._jobs.record_failure(
                job_id,
                ToolError(
                    ErrorCode.CANCELLED,
                    "The upload was cancelled, most likely because the server shut down.",
                    remediation=(
                        "Nothing was published. Start the upload again when the server is "
                        "back."
                    ),
                ),
            )
            raise
        except BaseException as exc:
            logger.exception("upload.unexpected_failure", job_id=job_id)
            self._jobs.record_failure(
                job_id,
                ToolError(
                    ErrorCode.INTERNAL,
                    f"The upload failed unexpectedly: {type(exc).__name__}: {exc}",
                    details={"job_id": job_id},
                    cause=exc,
                ),
            )
        finally:
            self._cancelled.discard(job_id)
            if staged is not None:
                _cleanup(staged)

    # -- status ------------------------------------------------------------------

    async def status(self, job_id: str | None, *, limit: int = 5) -> dict[str, Any]:
        """Report on one job, or list recent ones when no ID is given."""
        if job_id is None:
            recent = self._jobs.recent(limit)
            return {
                "jobs": [_public_job(job) for job in recent],
                "count": len(recent),
                "message": (
                    "No recent uploads." if not recent else
                    "Most recent uploads first. Pass a job_id for full detail on one."
                ),
            }

        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(
                ErrorCode.UPLOAD_NOT_FOUND,
                f"There is no upload job with ID {job_id!r}.",
                details={"job_id": job_id},
            )

        view = _public_job(job)

        # While YouTube is still processing, ask it directly -- our record only
        # knows the bytes arrived, not what became of them.
        if job["state"] == JobState.PROCESSING and job.get("video_id"):
            try:
                payload = await self._youtube.get_video(job["video_id"])
            except ToolError as exc:
                view["processing_check_failed"] = exc.message
            else:
                if payload is not None:
                    remote = describe_video_status(payload)
                    view["youtube"] = remote
                    if remote["is_terminal_failure"]:
                        self._jobs.record_failure(
                            job_id,
                            ToolError(
                                ErrorCode.VIDEO_REJECTED,
                                "YouTube finished processing and rejected the video: "
                                f"{remote.get('rejection_reason') or remote.get('failure_reason')}",
                            ),
                        )
                        view["state"] = JobState.FAILED
                    elif remote["processing_status"] == "succeeded":
                        self._jobs.update(
                            job_id,
                            state=JobState.COMPLETED,
                            applied_privacy=remote.get("privacy_status"),
                        )
                        view["state"] = JobState.COMPLETED

        return view

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            raise ToolError(
                ErrorCode.UPLOAD_NOT_FOUND,
                f"There is no upload job with ID {job_id!r}.",
            )
        state = JobState(job["state"])
        if state.terminal:
            return {
                "job_id": job_id,
                "cancelled": False,
                "state": str(state),
                "message": f"That job already finished with state '{state}'; nothing to cancel.",
            }

        self._cancelled.add(job_id)
        if task := self._tasks.get(job_id):
            task.cancel()

        self._jobs.update(
            job_id,
            state=JobState.CANCELLED,
            notes="Cancelled by the user from chat.",
        )
        return {
            "job_id": job_id,
            "cancelled": True,
            "state": str(JobState.CANCELLED),
            "message": (
                "The upload was cancelled. If bytes had already reached YouTube, the partial "
                "upload expires by itself and no video is published."
            ),
        }

    def _is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    async def shutdown(self) -> None:
        """Stop in-flight uploads cleanly when the server exits."""
        for task in list(self._tasks.values()):
            task.cancel()
        for task in list(self._tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


# ---------------------------------------------------------------------------


def _idempotency_key(file_id: str, metadata: ValidatedMetadata) -> str:
    """Identify "the same upload request".

    Source file plus title plus privacy. Deliberately not the description or
    tags: re-asking with a tweaked description is a correction, not a second
    video, and we would rather suppress that than publish twice.
    """
    raw = f"{file_id}|{metadata.title}|{metadata.privacy_status}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _temp_path(settings: Settings, job_id: str) -> Path:
    staging = settings.state_dir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=f"{job_id}-", suffix=".video", dir=str(staging))
    import os

    os.close(handle)
    return Path(name)


def _cleanup(path: Path) -> None:
    """Remove a staged file. Never raises: cleanup must not mask the real error."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover
        logger.warning("upload.cleanup_failed", path=str(path), error=str(exc))


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Shape a job row for the model.

    Includes a human-readable `progress` string because "bytes_sent:
    402653184" is not something anyone wants read aloud, and a model given
    only raw numbers tends to either recite them or compute the percentage
    incorrectly.
    """
    total = job.get("bytes_total")
    sent = job.get("bytes_sent") or 0
    percent = round(sent * 100 / total) if total else None

    state = job["state"]
    stage_text = {
        JobState.PENDING: "Queued, not started yet.",
        JobState.STAGING: "Copying the file out of storage onto local disk.",
        JobState.UPLOADING: "Sending the file to YouTube.",
        JobState.PROCESSING: "YouTube has all the bytes and is processing the video.",
        JobState.COMPLETED: "Finished.",
        JobState.FAILED: "Failed.",
        JobState.CANCELLED: "Cancelled.",
    }.get(JobState(state), state)

    view: dict[str, Any] = {
        "job_id": job["id"],
        "state": state,
        "stage": stage_text,
        "title": job.get("title"),
        "source_file": job.get("source_file_name"),
        "requested_privacy": job.get("requested_privacy"),
        "applied_privacy": job.get("applied_privacy"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "attempts": job.get("attempts"),
    }

    if percent is not None and state in {JobState.STAGING, JobState.UPLOADING}:
        view["progress"] = f"{percent}% ({sent:,} of {total:,} bytes)"
    if job.get("video_url"):
        view["video_url"] = job["video_url"]
        view["video_id"] = job.get("video_id")
    if job.get("error_code"):
        view["problem"] = {
            "code": job["error_code"],
            "message": job.get("error_message"),
            "remediation": job.get("error_remediation"),
        }
    if job.get("notes"):
        view["notes"] = job["notes"]
    if job.get("resume_uri") and state == JobState.UPLOADING:
        view["resumable"] = True

    return view
