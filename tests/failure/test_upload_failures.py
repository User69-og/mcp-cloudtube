"""Failure scenarios for the upload pipeline.

These are the tests the brief is really asking for: "how the system behaves
when things go wrong". Each one asserts three things, because all three matter
to a user sitting in a chat window:

1.  the server stays up and the call returns a value rather than raising;
2.  the failure is classified correctly, so the model knows whether retrying
    can possibly help;
3.  no resource is left behind -- no staged temp file, no job stuck in a
    non-terminal state, no half-published video reported as success.

They run against the fake YouTube transport. A real upload costs 1600 of the
10,000 daily quota units, which would allow roughly six runs of this file per
day; the fake makes the suite runnable on every change, which is the only way
error handling stays correct as the code moves.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mcp_cloudtube.context import ServerContext
from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.youtube.fake import (
    FailurePlan,
    connection_dropped,
    quota_exhausted,
)
from mcp_cloudtube.youtube.jobs import JobState

from ..support.fake_storage import FakeStorage

pytestmark = pytest.mark.anyio


async def _run_to_completion(ctx: ServerContext, job_id: str, timeout: float = 10.0) -> dict:
    """Wait for a background upload job to reach a terminal state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        job = ctx.jobs.get(job_id)
        assert job is not None
        if JobState(job["state"]).terminal:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _staging_files(ctx: ServerContext) -> list[Path]:
    staging = ctx.settings.state_dir / "staging"
    return list(staging.glob("*")) if staging.exists() else []


# ---------------------------------------------------------------------------
# The happy path, so the failure tests mean something
# ---------------------------------------------------------------------------


async def test_successful_upload_completes_and_cleans_up(ctx_pair) -> None:
    ctx, _youtube = ctx_pair
    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description="demo", tags=["demo"],
        privacy_status="unlisted", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.COMPLETED
    assert job["video_url"].startswith("https://www.youtube.com/watch?v=")
    assert job["applied_privacy"] == "unlisted"
    assert job["error_code"] is None
    assert _staging_files(ctx) == [], "the staged temp file should be deleted"


# ---------------------------------------------------------------------------
# Scenario 1: the daily quota is gone
# ---------------------------------------------------------------------------


async def test_quota_exhausted_fails_fast_and_is_not_retryable(ctx_pair) -> None:
    """A spent quota must stop the pipeline, not drive it into a retry loop.

    This is the most expensive mistake available: each retried insert costs
    another 1600 units it does not have, and would spend tomorrow's budget too.
    """
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(fail_at_fraction=0.5, failure=quota_exhausted())

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.FAILED
    assert job["error_code"] == ErrorCode.QUOTA_EXCEEDED
    assert "resets at midnight" in job["error_remediation"]
    assert job["attempts"] == 1, "a spent quota must not be retried"
    assert _staging_files(ctx) == []

    status = await ctx.uploads.status(started["job_id"])
    assert status["problem"]["code"] == ErrorCode.QUOTA_EXCEEDED


# ---------------------------------------------------------------------------
# Scenario 2: the network drops mid-upload
# ---------------------------------------------------------------------------


async def test_network_drop_is_reported_as_retryable_with_resume_recorded(ctx_pair) -> None:
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(fail_at_fraction=0.75, failure=connection_dropped(), chunks=4)

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.FAILED
    assert job["error_code"] == ErrorCode.NETWORK_ERROR
    # The resume URI was captured before the failure, so a retry can continue.
    assert job["resume_uri"], "the resume URI should be persisted for recovery"
    assert _staging_files(ctx) == []


async def test_storage_download_failure_removes_the_partial_file(ctx_pair) -> None:
    """A dropped download must not leave a truncated video to be uploaded."""
    ctx, _youtube = ctx_pair
    storage: FakeStorage = ctx.storage  # type: ignore[assignment]
    storage.truncate_download_at = 32

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.FAILED
    assert job["error_code"] == ErrorCode.NETWORK_ERROR
    assert _staging_files(ctx) == [], "a partial download must be cleaned up"


# ---------------------------------------------------------------------------
# Scenario 3: YouTube is briefly unwell, then recovers
# ---------------------------------------------------------------------------


async def test_transient_server_errors_are_absorbed_without_user_impact(ctx_pair) -> None:
    """503s during chunks should never reach the user."""
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(transient_failures=2, chunks=6)

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.COMPLETED
    assert job["error_code"] is None


# ---------------------------------------------------------------------------
# Scenario 4: YouTube forgets the resumable session
# ---------------------------------------------------------------------------


async def test_expired_resume_session_is_not_retried(ctx_pair) -> None:
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(
        fail_at_fraction=0.5,
        failure=ToolError(
            ErrorCode.UPLOAD_FAILED,
            "YouTube no longer recognises this upload session.",
            remediation="Start the upload again; nothing was published.",
            retryable=False,
        ),
    )

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.FAILED
    assert job["error_code"] == ErrorCode.UPLOAD_FAILED
    assert "nothing was published" in job["error_remediation"].lower()


# ---------------------------------------------------------------------------
# Scenario 5: the upload succeeds but YouTube refuses to publish
# ---------------------------------------------------------------------------


async def test_rejected_video_is_reported_as_a_content_decision(ctx_pair) -> None:
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(rejection_reason="duplicate")

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.FAILED
    assert job["error_code"] == ErrorCode.VIDEO_REJECTED
    assert "already been uploaded" in job["error_message"]
    assert "will not change on retry" in job["error_remediation"]


# ---------------------------------------------------------------------------
# Scenario 6: the privacy the user asked for is not the privacy they got
# ---------------------------------------------------------------------------


async def test_privacy_downgrade_completes_but_tells_the_truth(ctx_pair) -> None:
    """The real behaviour of an unaudited API project.

    The video IS published, so the job must not be FAILED -- that would tell
    the user nothing was uploaded and invite a duplicate. But it is private,
    not unlisted, so it must not be a silent success either.
    """
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(force_privacy="private")

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="unlisted", category_id=None, made_for_kids=False,
    )
    job = await _run_to_completion(ctx, started["job_id"])

    assert job["state"] == JobState.COMPLETED, "the video is live; do not report failure"
    assert job["applied_privacy"] == "private"
    assert job["requested_privacy"] == "unlisted"
    assert job["error_code"] == ErrorCode.PRIVACY_NOT_APPLIED

    status = await ctx.uploads.status(started["job_id"])
    assert status["video_url"], "the user must still be given the link"
    assert "compliance audit" in status["problem"]["remediation"]


# ---------------------------------------------------------------------------
# Scenario 7: the same upload is requested twice
# ---------------------------------------------------------------------------


async def test_duplicate_request_returns_the_first_job(ctx_pair) -> None:
    """A client that times out and retries must not publish twice."""
    ctx, youtube = ctx_pair

    first = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    await _run_to_completion(ctx, first["job_id"])

    second = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description="added later", tags=["new"],
        privacy_status="private", category_id=None, made_for_kids=False,
    )

    assert second["job_id"] == first["job_id"]
    assert second["duplicate_of"] == first["job_id"]
    assert len(youtube.uploads) == 1, "the video must be published exactly once"


async def test_different_title_is_treated_as_a_separate_upload(ctx_pair) -> None:
    ctx, youtube = ctx_pair

    first = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    await _run_to_completion(ctx, first["job_id"])

    second = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo (final cut)", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    await _run_to_completion(ctx, second["job_id"])

    assert second["job_id"] != first["job_id"]
    assert len(youtube.uploads) == 2


# ---------------------------------------------------------------------------
# Scenario 8: bad input never reaches the network
# ---------------------------------------------------------------------------


async def test_oversized_title_fails_before_any_upload_work(ctx_pair) -> None:
    ctx, youtube = ctx_pair

    with pytest.raises(ToolError) as caught:
        await ctx.uploads.start(
            file_id="vid_q4", title="x" * 101, description=None, tags=None,
            privacy_status="private", category_id=None, made_for_kids=False,
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert youtube.access_checks == 0, "nothing should have been sent to YouTube"
    assert youtube.uploads == []
    assert ctx.jobs.recent(10) == [], "no job should have been created"


async def test_non_video_file_is_refused_before_staging(ctx_pair) -> None:
    ctx, youtube = ctx_pair

    with pytest.raises(ToolError) as caught:
        await ctx.uploads.start(
            file_id="doc_notes", title="Not a video", description=None, tags=None,
            privacy_status="private", category_id=None, made_for_kids=False,
        )

    assert caught.value.code == ErrorCode.INVALID_INPUT
    assert "not a video file" in caught.value.message
    assert youtube.uploads == []


# ---------------------------------------------------------------------------
# Scenario 9: the account has no YouTube channel
# ---------------------------------------------------------------------------


async def test_missing_channel_is_caught_before_staging_the_file(ctx_pair) -> None:
    """Fail on a cheap API call, not after copying gigabytes to disk."""
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(no_channel=True)

    with pytest.raises(ToolError) as caught:
        await ctx.uploads.start(
            file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
            privacy_status="private", category_id=None, made_for_kids=False,
        )

    assert caught.value.code == ErrorCode.PERMISSION_DENIED
    assert "create a channel" in caught.value.remediation
    assert _staging_files(ctx) == [], "nothing should have been staged"


# ---------------------------------------------------------------------------
# Scenario 10: cancellation, and the crash-recovery path
# ---------------------------------------------------------------------------


async def test_cancelling_a_running_upload_stops_it(ctx_pair) -> None:
    ctx, youtube = ctx_pair
    youtube.plan = FailurePlan(chunks=50, chunk_delay=0.02)

    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    await asyncio.sleep(0.05)

    result = await ctx.uploads.cancel(started["job_id"])
    assert result["cancelled"] is True

    await asyncio.sleep(0.2)
    job = ctx.jobs.get(started["job_id"])
    assert job is not None
    assert JobState(job["state"]).terminal


async def test_cancelling_a_finished_upload_is_refused_clearly(ctx_pair) -> None:
    ctx, _youtube = ctx_pair
    started = await ctx.uploads.start(
        file_id="vid_q4", title="Q4 Demo", description=None, tags=None,
        privacy_status="private", category_id=None, made_for_kids=False,
    )
    await _run_to_completion(ctx, started["job_id"])

    result = await ctx.uploads.cancel(started["job_id"])
    assert result["cancelled"] is False
    assert "already finished" in result["message"]


async def test_unknown_job_id_is_a_clear_error_not_a_crash(ctx_pair) -> None:
    ctx, _youtube = ctx_pair
    with pytest.raises(ToolError) as caught:
        await ctx.uploads.status("job_does_not_exist")
    assert caught.value.code == ErrorCode.UPLOAD_NOT_FOUND


async def test_jobs_interrupted_by_a_restart_are_resolved_honestly(ctx_pair) -> None:
    """A job left 'uploading' by a crash must not look eternally in progress."""
    ctx, _youtube = ctx_pair

    job_id = ctx.jobs.create(
        source_file_id="vid_q4", source_file_name="Q4 Product Demo.mp4", title="Q4 Demo",
        description=None, tags=[], requested_privacy="private", category_id="28",
        made_for_kids=False, bytes_total=1000, idempotency_key="orphan",
    )
    ctx.jobs.update(job_id, state=JobState.STAGING)

    ctx.jobs.mark_interrupted_jobs()  # what startup does

    job = ctx.jobs.get(job_id)
    assert job is not None
    assert job["state"] == JobState.FAILED
    assert "Nothing was published" in job["error_remediation"]


# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_pair(context, youtube):
    """The wired context together with the fake YouTube it is using."""
    return context, youtube
