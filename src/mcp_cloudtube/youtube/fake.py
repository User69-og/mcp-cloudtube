"""A scriptable stand-in for YouTube, used to test failure handling.

This exists because of an awkward constraint in the real system: the YouTube
Data API allows 10,000 quota units a day and a single `videos.insert` costs
1,600. That is roughly six uploads per day *including failed ones*, which
makes "test what happens when the upload fails at 60%" an expensive
experiment and "test it twenty times" impossible.

So the failure-scenario suite runs against this fake. It implements the same
`YouTubeTransport` protocol as the real client and can be told to fail in
specific, realistic ways: drop the connection mid-chunk, return 503 twice then
succeed, expire the resume session, exhaust the quota, reject the video as a
duplicate, or apply a different privacy status than requested (which is what
an unaudited project really does).

Real uploads are still exercised end to end -- see `tests/integration` and the
demo recording -- but they are spent deliberately, not on regression runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, ToolError
from ..logging_setup import get_logger
from .client import FAILURE_REASONS, UploadOutcome
from .metadata import ValidatedMetadata

logger = get_logger(__name__)


@dataclass
class FailurePlan:
    """How the fake should misbehave.

    Each field models a failure that really happens in production, and each
    maps to a scenario in `tests/failure/`.
    """

    #: Raise this many retryable 503s before succeeding.
    transient_failures: int = 0
    #: Fail permanently once this fraction of the file has been "sent".
    fail_at_fraction: float | None = None
    #: The error to raise at `fail_at_fraction`.
    failure: ToolError | None = None
    #: Pretend YouTube forgot the resume session.
    expire_resume_session: bool = False
    #: Report a privacy status different from the one requested.
    force_privacy: str | None = None
    #: Report the video as rejected after a successful byte transfer.
    rejection_reason: str | None = None
    #: Report the video as unprocessable after a successful byte transfer.
    failure_reason: str | None = None
    #: Fail `check_access`, e.g. to model an account with no channel.
    no_channel: bool = False
    #: Seconds to sleep per chunk, to make progress observable in tests.
    chunk_delay: float = 0.0
    #: Number of chunks to split the file into.
    chunks: int = 4


@dataclass
class FakeYouTube:
    """In-process YouTube. Records what it was asked to do."""

    plan: FailurePlan = field(default_factory=FailurePlan)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    videos: dict[str, dict[str, Any]] = field(default_factory=dict)
    access_checks: int = 0
    _counter: int = 0
    _transient_remaining: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._transient_remaining = self.plan.transient_failures

    async def check_access(self) -> dict[str, Any]:
        self.access_checks += 1
        if self.plan.no_channel:
            raise ToolError(
                ErrorCode.PERMISSION_DENIED,
                "The connected Google account does not have a YouTube channel.",
                remediation=(
                    "Sign in to youtube.com with this account and create a channel, then try "
                    "again."
                ),
            )
        return {
            "channel_id": "UC_fake_channel",
            "channel_title": "Test Channel",
            "privacy_status": "public",
            "is_linked": True,
        }

    async def upload(
        self,
        path: Path,
        metadata: ValidatedMetadata,
        *,
        on_progress: Callable[[int, int | None], None] | None = None,
        on_resume_uri: Callable[[str], None] | None = None,
        resume_uri: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> UploadOutcome:
        size = path.stat().st_size if path.exists() else 0
        if size == 0:
            raise ToolError(
                ErrorCode.INVALID_INPUT,
                "The video file is empty (0 bytes), and YouTube rejects empty uploads.",
                remediation="Check the source file in storage. No quota was spent.",
            )

        if self.plan.expire_resume_session and resume_uri:
            raise ToolError(
                ErrorCode.UPLOAD_FAILED,
                "YouTube no longer recognises this upload session, so the partially sent "
                "bytes cannot be continued.",
                remediation=(
                    "Resumable upload sessions expire. Start the upload again; nothing was "
                    "published, so there is no risk of a duplicate."
                ),
                retryable=False,
            )

        if on_resume_uri is not None:
            on_resume_uri("https://fake.googleapis.com/resumable/session-123")

        chunks = max(1, self.plan.chunks)
        for index in range(1, chunks + 1):
            if should_cancel is not None and should_cancel():
                raise ToolError(
                    ErrorCode.CANCELLED,
                    "The upload was cancelled before it finished.",
                    remediation="Nothing was published.",
                )

            if self.plan.chunk_delay:
                await asyncio.sleep(self.plan.chunk_delay)

            sent = round(size * index / chunks)
            fraction = index / chunks

            if (
                self.plan.fail_at_fraction is not None
                and fraction >= self.plan.fail_at_fraction
                and self.plan.failure is not None
            ):
                logger.info("fake_youtube.failing", at_fraction=fraction)
                raise self.plan.failure

            if self._transient_remaining > 0:
                self._transient_remaining -= 1
                # The real client retries the chunk internally, so model that
                # here rather than aborting: the caller should never see it.
                logger.info(
                    "fake_youtube.transient", remaining=self._transient_remaining
                )
                continue

            if on_progress is not None:
                on_progress(sent, size)

        if on_progress is not None:
            on_progress(size, size)

        self._counter += 1
        video_id = f"fakevid{self._counter:04d}"
        applied = self.plan.force_privacy or metadata.privacy_status

        record = {
            "file": str(path),
            "size": size,
            "title": metadata.title,
            "description": metadata.description,
            "tags": list(metadata.tags),
            "requested_privacy": metadata.privacy_status,
            "applied_privacy": applied,
            "category_id": metadata.category_id,
        }
        self.uploads.append(record)

        self.videos[video_id] = {
            "snippet": {"title": metadata.title},
            "status": {
                "uploadStatus": "rejected" if self.plan.rejection_reason else "uploaded",
                "privacyStatus": applied,
                "rejectionReason": self.plan.rejection_reason,
                "failureReason": self.plan.failure_reason,
            },
            "processingDetails": {
                "processingStatus": "terminated" if self.plan.rejection_reason else "succeeded",
            },
        }

        # Apply the same reason-code -> human-message mapping the real client
        # does. A fake that reports raw codes where production reports prose
        # would let a test pass against behaviour the real system never has.
        return UploadOutcome(
            video_id=video_id,
            video_url=f"https://www.youtube.com/watch?v={video_id}",
            upload_status="rejected" if self.plan.rejection_reason else "uploaded",
            applied_privacy=applied,
            rejection_reason=_humanise(self.plan.rejection_reason),
            failure_reason=_humanise(self.plan.failure_reason),
        )

    async def get_video(self, video_id: str) -> dict[str, Any] | None:
        return self.videos.get(video_id)


def _humanise(reason: str | None) -> str | None:
    """Match the real client's reason-code translation."""
    if reason is None:
        return None
    return FAILURE_REASONS.get(reason, reason)


# -- ready-made failures used across the scenario suite ---------------------


def quota_exhausted() -> ToolError:
    return ToolError(
        ErrorCode.QUOTA_EXCEEDED,
        "Google refused the request to upload the video to YouTube (HTTP 403). The request "
        "cannot be completed because you have exceeded your quota.",
        details={"http_status": 403, "google_reason": "quotaExceeded"},
    )


def connection_dropped() -> ToolError:
    return ToolError(
        ErrorCode.NETWORK_ERROR,
        "The connection to YouTube was reset while sending the video.",
    )


def service_unavailable() -> ToolError:
    return ToolError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        "Google refused the request to upload the video to YouTube (HTTP 503). Backend Error.",
        details={"http_status": 503, "google_reason": "backendError"},
    )


def token_expired() -> ToolError:
    return ToolError(
        ErrorCode.AUTH_EXPIRED,
        "The saved Google authorisation could not be renewed.",
    )


__all__ = [
    "FailurePlan",
    "FakeYouTube",
    "connection_dropped",
    "quota_exhausted",
    "service_unavailable",
    "token_expired",
]
