"""Translation of Google's exceptions into the taxonomy.

The 403 cases carry the most weight: Google uses one status for throttling,
exhausted quota and outright denial, and those need opposite responses.
"""

from __future__ import annotations

import json
import socket

import pytest
from googleapiclient.errors import HttpError

from mcp_cloudtube.errors import ErrorCode, ToolError
from mcp_cloudtube.google_errors import translate


class _Resp(dict):
    def __init__(self, status: int, **headers: str) -> None:
        super().__init__(**headers)
        self.status = status
        self.reason = "test"


def _http_error(status: int, reason: str | None = None, message: str = "boom", **headers: str):
    body = {"error": {"code": status, "message": message}}
    if reason:
        body["error"]["errors"] = [{"reason": reason, "message": message}]
    return HttpError(_Resp(status, **headers), json.dumps(body).encode(), uri="https://x")


class TestStatusMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (400, ErrorCode.INVALID_INPUT),
            (401, ErrorCode.AUTH_EXPIRED),
            (404, ErrorCode.NOT_FOUND),
            (413, ErrorCode.FILE_TOO_LARGE),
            (429, ErrorCode.RATE_LIMITED),
            (500, ErrorCode.UPSTREAM_UNAVAILABLE),
            (503, ErrorCode.UPSTREAM_UNAVAILABLE),
            (504, ErrorCode.TIMEOUT),
        ],
    )
    def test_status_codes(self, status: int, expected: ErrorCode) -> None:
        assert translate(_http_error(status), operation="test").code == expected


class TestTheOverloadedForbidden:
    """403 means three different things; conflating them is the expensive bug."""

    def test_rate_limit_is_retryable(self) -> None:
        error = translate(_http_error(403, "rateLimitExceeded"), operation="test")
        assert error.code == ErrorCode.RATE_LIMITED
        assert error.retryable is True

    def test_spent_quota_is_not_retryable(self) -> None:
        error = translate(_http_error(403, "quotaExceeded"), operation="test")
        assert error.code == ErrorCode.QUOTA_EXCEEDED
        assert error.retryable is False
        assert "midnight" in error.remediation

    def test_youtube_daily_upload_limit_is_quota_not_denial(self) -> None:
        error = translate(_http_error(403, "uploadLimitExceeded"), operation="test")
        assert error.code == ErrorCode.QUOTA_EXCEEDED

    def test_genuine_denial_is_permission_denied(self) -> None:
        error = translate(_http_error(403, "forbidden"), operation="test")
        assert error.code == ErrorCode.PERMISSION_DENIED
        assert error.retryable is False

    def test_missing_channel_is_permission_denied(self) -> None:
        error = translate(_http_error(403, "youtubeSignupRequired"), operation="test")
        assert error.code == ErrorCode.PERMISSION_DENIED


class TestRetryAfter:
    def test_header_is_honoured(self) -> None:
        error = translate(
            _http_error(429, "rateLimitExceeded", **{"retry-after": "42"}), operation="test"
        )
        assert error.retry_after_seconds == 42.0

    def test_unparseable_header_does_not_crash(self) -> None:
        error = translate(
            _http_error(429, **{"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            operation="test",
        )
        assert error.retry_after_seconds is None
        assert error.code == ErrorCode.RATE_LIMITED


class TestTransportFailures:
    def test_timeout(self) -> None:
        assert translate(TimeoutError(), operation="test").code == ErrorCode.TIMEOUT

    def test_connection_error(self) -> None:
        assert (
            translate(ConnectionResetError("reset"), operation="test").code
            == ErrorCode.NETWORK_ERROR
        )

    def test_dns_failure(self) -> None:
        assert (
            translate(socket.gaierror("no such host"), operation="test").code
            == ErrorCode.NETWORK_ERROR
        )


class TestRobustness:
    def test_an_existing_tool_error_passes_through_unchanged(self) -> None:
        original = ToolError(ErrorCode.NOT_FOUND, "already classified")
        assert translate(original, operation="test") is original

    def test_unknown_exception_becomes_internal_not_a_crash(self) -> None:
        error = translate(RuntimeError("something odd"), operation="test")
        assert error.code == ErrorCode.INTERNAL
        assert "RuntimeError" in error.message

    def test_html_error_body_does_not_break_parsing(self) -> None:
        """A proxy sometimes returns HTML where Google would send JSON."""
        exc = HttpError(_Resp(503), b"<html>Service Unavailable</html>", uri="https://x")
        error = translate(exc, operation="test")
        assert error.code == ErrorCode.UPSTREAM_UNAVAILABLE

    def test_operation_name_reaches_the_message(self) -> None:
        error = translate(_http_error(404), operation="find the demo video")
        assert "find the demo video" in error.message
