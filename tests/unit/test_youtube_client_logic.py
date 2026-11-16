"""The YouTube client's decision logic, isolated from the network.

`_classify_chunk_error` decides whether a failed upload chunk is retried.
Getting it wrong is expensive in both directions: retrying a dead session
wastes the whole transfer, and *not* retrying a transient 503 throws away an
upload that was about to succeed. So it is tested directly rather than only
through the fake.
"""

from __future__ import annotations

import json

import pytest
from googleapiclient.errors import HttpError

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.youtube.client import (
    FAILURE_REASONS,
    _classify_chunk_error,
    _outcome_from_response,
    _progress_percent,
    describe_video_status,
)
from mcp_cloudtube.youtube.metadata import validate


class _Resp(dict):
    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status
        self.reason = "test"


def _http(status: int, reason: str | None = None) -> HttpError:
    body: dict = {"error": {"code": status, "message": "boom"}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": "boom"}]
    return HttpError(_Resp(status), json.dumps(body).encode(), uri="https://x")


class TestChunkErrorClassification:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_server_errors_are_retryable(self, status: int) -> None:
        """The resumable protocol is designed to survive these."""
        error = _classify_chunk_error(_http(status))
        assert error.retryable is True

    @pytest.mark.parametrize("status", [404, 410])
    def test_a_lost_session_is_terminal_and_says_so(self, status: int) -> None:
        """Retrying a forgotten session can never work."""
        error = _classify_chunk_error(_http(status))
        assert error.code == ErrorCode.UPLOAD_FAILED
        assert error.retryable is False
        assert "no risk of a duplicate" in error.remediation

    def test_spent_quota_is_terminal(self) -> None:
        error = _classify_chunk_error(_http(403, "quotaExceeded"))
        assert error.code == ErrorCode.QUOTA_EXCEEDED
        assert error.retryable is False

    def test_throttling_is_retryable(self) -> None:
        error = _classify_chunk_error(_http(403, "rateLimitExceeded"))
        assert error.code == ErrorCode.RATE_LIMITED
        assert error.retryable is True

    def test_a_genuine_denial_is_terminal(self) -> None:
        error = _classify_chunk_error(_http(403, "forbidden"))
        assert error.code == ErrorCode.PERMISSION_DENIED
        assert error.retryable is False


class TestOutcomeMapping:
    def test_a_successful_insert_yields_id_and_url(self) -> None:
        outcome = _outcome_from_response(
            {"id": "abc123", "status": {"uploadStatus": "uploaded", "privacyStatus": "unlisted"}},
            validate(title="t", privacy_status="unlisted"),
        )
        assert outcome.video_id == "abc123"
        assert outcome.video_url == "https://www.youtube.com/watch?v=abc123"
        assert outcome.applied_privacy == "unlisted"

    def test_a_response_without_an_id_is_an_error_not_a_crash(self) -> None:
        with pytest.raises(ToolError) as caught:
            _outcome_from_response({"status": {}}, validate(title="t"))
        assert caught.value.code == ErrorCode.UPLOAD_FAILED
        assert "before uploading again" in caught.value.remediation

    def test_reason_codes_become_readable_messages(self) -> None:
        outcome = _outcome_from_response(
            {"id": "x", "status": {"uploadStatus": "rejected", "rejectionReason": "duplicate"}},
            validate(title="t"),
        )
        assert outcome.rejection_reason == FAILURE_REASONS["duplicate"]

    def test_an_unknown_reason_code_is_passed_through_not_dropped(self) -> None:
        """Better an opaque code than silently losing why it failed."""
        outcome = _outcome_from_response(
            {"id": "x", "status": {"rejectionReason": "somethingNew"}}, validate(title="t")
        )
        assert outcome.rejection_reason == "somethingNew"

    def test_a_privacy_downgrade_is_visible_in_the_outcome(self) -> None:
        outcome = _outcome_from_response(
            {"id": "x", "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}},
            validate(title="t", privacy_status="unlisted"),
        )
        assert outcome.applied_privacy == "private"


class TestVideoStatusDescription:
    def test_a_terminal_failure_is_flagged(self) -> None:
        described = describe_video_status(
            {"status": {"uploadStatus": "rejected", "rejectionReason": "claim"}}
        )
        assert described["is_terminal_failure"] is True
        assert "copyright" in described["rejection_reason"]

    def test_processing_is_not_a_failure(self) -> None:
        described = describe_video_status(
            {
                "status": {"uploadStatus": "uploaded"},
                "processingDetails": {"processingStatus": "processing"},
            }
        )
        assert described["is_terminal_failure"] is False

    def test_progress_is_reported_as_a_percentage(self) -> None:
        progress = {"processingProgress": {"partsProcessed": 3, "partsTotal": 4}}
        assert _progress_percent(progress) == 75

    def test_missing_progress_is_none_not_zero(self) -> None:
        """Zero would be read as 'no progress'; None means 'not reported'."""
        assert _progress_percent({}) is None

    def test_a_zero_total_does_not_divide_by_zero(self) -> None:
        progress = {"processingProgress": {"partsProcessed": 0, "partsTotal": 0}}
        assert _progress_percent(progress) is None

    def test_an_empty_payload_does_not_crash(self) -> None:
        described = describe_video_status({})
        assert described["upload_status"] is None
        assert described["is_terminal_failure"] is False
