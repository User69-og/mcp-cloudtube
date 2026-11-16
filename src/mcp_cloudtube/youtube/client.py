"""YouTube Data API client, with the resumable upload loop written by hand.

Why the chunk loop is explicit rather than `next_chunk(num_retries=...)`
-----------------------------------------------------------------------
`googleapiclient` can retry chunks for you, but it treats every failure the
same and gives no way to observe progress, persist the resume URI, or
distinguish "YouTube is briefly unwell" from "the quota is gone". For a
1600-unit operation those distinctions are the whole game, so the loop is
written out:

*   **5xx and connection drops** retry the *same chunk* with backoff. The
    bytes already acknowledged stay acknowledged; only the failed chunk is
    re-sent.
*   **404/410 on the resume URI** means YouTube has forgotten the session --
    they expire after about a week, and are also dropped on some errors. The
    only recovery is a fresh upload, so that is reported distinctly rather
    than retried into the ground.
*   **403 with a quota reason** stops immediately. Retrying a video insert
    against a spent quota does not succeed and does not become free.
*   **The resume URI is persisted after the first chunk**, so a server restart
    mid-upload can continue instead of re-sending the file.

The client is also fronted by a small protocol so the failure-scenario suite
can substitute a fake that produces each of these conditions on demand,
without a network and without spending quota.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..auth.google_oauth import GoogleAuthManager
from ..config import Settings
from ..errors import ErrorCode, ToolError
from ..google_errors import translate
from ..logging_setup import get_logger
from ..resilience import call_google
from .metadata import ValidatedMetadata

logger = get_logger(__name__)

SERVICE = "youtube"

#: Statuses YouTube reports for an uploaded video.
TERMINAL_FAILURE_STATUSES = {"failed", "rejected", "deleted"}

#: `processingDetails.processingStatus` / `status.uploadStatus` explanations,
#: rewritten as something worth saying to a user.
FAILURE_REASONS: dict[str, str] = {
    "codec": "YouTube could not decode the video codec.",
    "conversion": "YouTube failed while converting the video.",
    "emptyFile": "The uploaded file was empty.",
    "invalidFile": "The file is not a video format YouTube accepts.",
    "tooSmall": "The file is too small to be a valid video.",
    "uploadAborted": "The upload was aborted before it completed.",
    "duplicate": "This exact video has already been uploaded to the channel.",
    "inappropriate": "The video was rejected as violating YouTube's community guidelines.",
    "claim": "The video was rejected because of a copyright claim.",
    "copyright": "The video was rejected because of a copyright claim.",
    "duplicateDescription": "The description duplicates another video's.",
    "legal": "The video was rejected for legal reasons.",
    "length": "The video is longer than this channel is allowed to upload. "
              "Verify the channel's phone number to raise the limit past 15 minutes.",
    "termsOfUse": "The video was rejected as violating YouTube's terms of use.",
    "trademark": "The video was rejected because of a trademark complaint.",
    "uploaderAccountSuspended": "The channel is suspended and cannot upload.",
    "uploaderAccountClosed": "The channel is closed and cannot upload.",
}


@dataclass
class UploadOutcome:
    """Result of a completed upload."""

    video_id: str
    video_url: str
    upload_status: str
    applied_privacy: str | None
    failure_reason: str | None = None
    rejection_reason: str | None = None


ProgressCallback = Callable[[int, int | None], None]
ResumeUriCallback = Callable[[str], None]


@runtime_checkable
class YouTubeTransport(Protocol):
    """What the uploader needs from YouTube. Faked in the failure suite."""

    async def check_access(self) -> dict[str, Any]: ...

    async def upload(
        self,
        path: Path,
        metadata: ValidatedMetadata,
        *,
        on_progress: ProgressCallback | None = None,
        on_resume_uri: ResumeUriCallback | None = None,
        resume_uri: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> UploadOutcome: ...

    async def get_video(self, video_id: str) -> dict[str, Any] | None: ...


class YouTubeClient:
    """Real YouTube Data API v3 transport."""

    def __init__(self, auth: GoogleAuthManager, settings: Settings) -> None:
        self._auth = auth
        self._settings = settings

    def _service(self) -> Any:
        return build(
            "youtube", "v3", credentials=self._auth.get_credentials(), cache_discovery=False
        )

    # -- health ---------------------------------------------------------------

    async def check_access(self) -> dict[str, Any]:
        """Confirm the account has a YouTube channel that can be uploaded to.

        Worth doing before an upload: an account with no channel returns
        `youtubeSignupRequired`, which is an obscure way of saying "this Google
        account has never created a YouTube channel". Catching it early saves
        staging a large file first.
        """

        def execute() -> dict[str, Any]:
            return (
                self._service()
                .channels()
                .list(part="snippet,contentDetails,status", mine=True)
                .execute()
            )

        payload = await call_google(
            execute,
            operation="look up the connected YouTube channel",
            service=SERVICE,
            settings=self._settings,
            deadline_seconds=45.0,
            attempt_timeout=15.0,
        )

        items = payload.get("items") or []
        if not items:
            raise ToolError(
                ErrorCode.PERMISSION_DENIED,
                "The connected Google account does not have a YouTube channel.",
                remediation=(
                    "Sign in to youtube.com with this account and create a channel, then try "
                    "again. A Google account without a channel cannot upload videos."
                ),
            )

        channel = items[0]
        snippet = channel.get("snippet") or {}
        status = channel.get("status") or {}
        return {
            "channel_id": channel.get("id"),
            "channel_title": snippet.get("title"),
            "privacy_status": status.get("privacyStatus"),
            "is_linked": status.get("isLinked"),
            "long_uploads_allowed": (channel.get("contentDetails") or {})
            .get("relatedPlaylists", {})
            .get("uploads")
            is not None,
        }

    async def get_video(self, video_id: str) -> dict[str, Any] | None:
        def execute() -> dict[str, Any]:
            return (
                self._service()
                .videos()
                .list(part="status,processingDetails,snippet", id=video_id)
                .execute()
            )

        payload = await call_google(
            execute,
            operation=f"read back video {video_id}",
            service=SERVICE,
            settings=self._settings,
            deadline_seconds=45.0,
            attempt_timeout=15.0,
        )
        items = payload.get("items") or []
        return items[0] if items else None

    # -- upload ------------------------------------------------------------------

    async def upload(
        self,
        path: Path,
        metadata: ValidatedMetadata,
        *,
        on_progress: ProgressCallback | None = None,
        on_resume_uri: ResumeUriCallback | None = None,
        resume_uri: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> UploadOutcome:
        """Send the staged file to YouTube, chunk by chunk.

        Runs the whole loop on a worker thread: `googleapiclient` is
        synchronous, and the loop needs to stay alive across many chunks.
        """
        if not path.exists():
            raise ToolError(
                ErrorCode.UPLOAD_FAILED,
                f"The staged video file at {path} is missing, so the upload cannot start.",
                remediation=(
                    "The temporary copy was removed before the upload ran. Start the upload "
                    "again; nothing was published."
                ),
            )

        size = path.stat().st_size
        if size == 0:
            raise ToolError(
                ErrorCode.INVALID_INPUT,
                "The video file is empty (0 bytes), and YouTube rejects empty uploads.",
                remediation=(
                    "Check the source file in storage. Nothing was uploaded and no quota was "
                    "spent."
                ),
            )

        settings = self._settings

        def run() -> UploadOutcome:
            media = MediaFileUpload(
                str(path),
                chunksize=settings.upload_chunk_bytes,
                resumable=True,
                mimetype="video/*",
            )
            request = (
                self._service()
                .videos()
                .insert(
                    part="snippet,status",
                    body=metadata.to_request_body(),
                    media_body=media,
                    notifySubscribers=False,
                )
            )

            if resume_uri:
                # Continue a session a previous process started.
                request.resumable_uri = resume_uri
                logger.info("youtube.resuming", uri_known=True)

            response: dict[str, Any] | None = None
            consecutive_errors = 0
            seen_uri = bool(resume_uri)

            while response is None:
                if should_cancel is not None and should_cancel():
                    raise ToolError(
                        ErrorCode.CANCELLED,
                        "The upload was cancelled before it finished.",
                        remediation=(
                            "The partial upload on YouTube's side expires on its own. Nothing "
                            "was published."
                        ),
                    )

                try:
                    status, response = request.next_chunk()
                    consecutive_errors = 0
                except HttpError as exc:
                    error = _classify_chunk_error(exc)
                    consecutive_errors += 1

                    if not error.retryable or consecutive_errors > settings.max_retries:
                        raise error from exc

                    delay = min(
                        settings.backoff_base_seconds * (2**consecutive_errors),
                        settings.backoff_max_seconds,
                    )
                    logger.info(
                        "youtube.chunk_retry",
                        attempt=consecutive_errors,
                        code=str(error.code),
                        sleeping=round(delay, 2),
                    )
                    time.sleep(delay)
                    continue
                except (OSError, ConnectionError) as exc:
                    consecutive_errors += 1
                    if consecutive_errors > settings.max_retries:
                        raise ToolError(
                            ErrorCode.NETWORK_ERROR,
                            f"The connection to YouTube kept dropping: {exc}",
                            remediation=(
                                "Check connectivity and retry. The upload resumes from the "
                                "last acknowledged byte rather than restarting."
                            ),
                            retryable=True,
                            cause=exc,
                        ) from exc
                    delay = min(
                        settings.backoff_base_seconds * (2**consecutive_errors),
                        settings.backoff_max_seconds,
                    )
                    logger.info("youtube.chunk_retry_network", sleeping=round(delay, 2))
                    time.sleep(delay)
                    continue

                # Capture the resume URI as soon as YouTube issues it, so an
                # interrupted upload is recoverable from the very first chunk.
                if not seen_uri and request.resumable_uri and on_resume_uri is not None:
                    seen_uri = True
                    on_resume_uri(str(request.resumable_uri))

                if status is not None and on_progress is not None:
                    on_progress(int(status.resumable_progress), size)

            if on_progress is not None:
                on_progress(size, size)
            return _outcome_from_response(response, metadata)

        return await call_google(
            run,
            operation="upload the video to YouTube",
            service=SERVICE,
            settings=settings,
            deadline_seconds=settings.upload_deadline_seconds,
            attempt_timeout=settings.upload_deadline_seconds,
            # The chunk loop does its own retrying; a second layer here would
            # restart the whole upload and multiply the quota cost.
            retry_budget=0,
        )


def _classify_chunk_error(exc: HttpError) -> ToolError:
    """Interpret a failure of one resumable chunk."""
    status = getattr(getattr(exc, "resp", None), "status", None)

    if status in (404, 410):
        return ToolError(
            ErrorCode.UPLOAD_FAILED,
            "YouTube no longer recognises this upload session, so the partially sent bytes "
            "cannot be continued.",
            remediation=(
                "Resumable upload sessions expire. Start the upload again with "
                "upload_video_to_youtube -- it will re-send from the beginning. Nothing was "
                "published, so there is no risk of a duplicate."
            ),
            retryable=False,
            details={"http_status": status},
            cause=exc,
        )

    error = translate(exc, operation="send a chunk of the video")

    # Treat server-side wobbles during a chunk as retryable even when the
    # generic mapping is less sure; the resumable protocol is designed for it.
    if status in (500, 502, 503, 504):
        error.retryable = True

    return error


def _outcome_from_response(response: dict[str, Any], metadata: ValidatedMetadata) -> UploadOutcome:
    video_id = response.get("id")
    if not video_id:
        raise ToolError(
            ErrorCode.UPLOAD_FAILED,
            "YouTube accepted the upload but returned no video ID.",
            remediation=(
                "Check the channel in YouTube Studio before uploading again, so the same "
                "video is not published twice."
            ),
            details={"response_keys": sorted(response)},
        )

    status = response.get("status") or {}
    upload_status = status.get("uploadStatus", "unknown")
    applied_privacy = status.get("privacyStatus")

    failure_code = status.get("failureReason")
    rejection_code = status.get("rejectionReason")

    return UploadOutcome(
        video_id=str(video_id),
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        upload_status=str(upload_status),
        applied_privacy=applied_privacy,
        failure_reason=FAILURE_REASONS.get(str(failure_code), failure_code),
        rejection_reason=FAILURE_REASONS.get(str(rejection_code), rejection_code),
    )


def describe_video_status(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a videos.list response into something a model can read."""
    status = payload.get("status") or {}
    processing = payload.get("processingDetails") or {}
    snippet = payload.get("snippet") or {}

    upload_status = status.get("uploadStatus")
    failure = status.get("failureReason")
    rejection = status.get("rejectionReason")

    return {
        "title": snippet.get("title"),
        "upload_status": upload_status,
        "privacy_status": status.get("privacyStatus"),
        "processing_status": processing.get("processingStatus"),
        "processing_progress_percent": _progress_percent(processing),
        "failure_reason": FAILURE_REASONS.get(str(failure), failure),
        "rejection_reason": FAILURE_REASONS.get(str(rejection), rejection),
        "is_terminal_failure": upload_status in TERMINAL_FAILURE_STATUSES,
    }


def _progress_percent(processing: dict[str, Any]) -> int | None:
    progress = processing.get("processingProgress") or {}
    done = progress.get("partsProcessed")
    total = progress.get("partsTotal")
    try:
        done_i, total_i = int(done), int(total)
    except (TypeError, ValueError):
        return None
    return round(done_i * 100 / total_i) if total_i else None
