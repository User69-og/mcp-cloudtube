"""The error taxonomy's contract: shape, retry stance, and redaction."""

from __future__ import annotations

import pytest

from mcp_cloudtube.errors import ErrorCode, ToolError, redact


class TestRedaction:
    """Nothing credential-shaped may leave the process."""

    # Every literal below is a fabricated, structurally-valid sample -- none has
    # ever been a live credential. They carry the allow marker so the
    # pre-commit secret scan does not block the very tests that prove secrets
    # are stripped.
    @pytest.mark.parametrize(
        "secret",
        [
            "ya29.a0AfH6SMBexampleexampleexample",  # access token  # cloudtube:allow-secret-pattern
            "1//0gExampleRefreshTokenValueHere12345",  # refresh  # cloudtube:allow-secret-pattern
            "AIzaSyA1234567890123456789012345678901234",  # API key  # cloudtube:allow-secret-pattern
            "GOCSPX-abcdefghijklmnopqrstuvwx",  # client secret  # cloudtube:allow-secret-pattern
            "Authorization: Bearer abc.def.ghi",  # header echo
        ],
    )
    def test_secret_shapes_are_stripped(self, secret: str) -> None:
        assert secret not in redact(f"upstream said: {secret} <-")

    def test_private_keys_are_stripped(self) -> None:
        pem = "-----BEGIN PRIVATE KEY-----\nMIIabc\n-----END PRIVATE KEY-----"  # cloudtube:allow-secret-pattern
        assert "MIIabc" not in redact(pem)

    def test_ordinary_text_is_untouched(self) -> None:
        text = "File 'Q4 Demo.mp4' was not found in folder 1a2b3c."
        assert redact(text) == text

    def test_redaction_applies_to_message_and_remediation(self) -> None:
        error = ToolError(
            ErrorCode.INTERNAL,
            "token ya29.a0AfH6SMBexampleexampleexample failed",  # cloudtube:allow-secret-pattern
            remediation="retry ya29.a0AfH6SMBexampleexampleexample",  # cloudtube:allow-secret-pattern
        )
        assert "ya29" not in error.message
        assert "ya29" not in error.remediation

    def test_redaction_reaches_nested_details(self) -> None:
        error = ToolError(
            ErrorCode.INTERNAL,
            "boom",
            details={"outer": {"token": "ya29.a0AfH6SMBexampleexampleexample"}},  # cloudtube:allow-secret-pattern
        )
        assert "ya29" not in str(error.details)


class TestRetryStance:
    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.RATE_LIMITED,
            ErrorCode.UPSTREAM_UNAVAILABLE,
            ErrorCode.NETWORK_ERROR,
            ErrorCode.TIMEOUT,
        ],
    )
    def test_transient_codes_are_retryable(self, code: ErrorCode) -> None:
        assert ToolError(code, "x").retryable is True

    @pytest.mark.parametrize(
        "code",
        [
            ErrorCode.QUOTA_EXCEEDED,
            ErrorCode.INVALID_INPUT,
            ErrorCode.NOT_FOUND,
            ErrorCode.AUTH_EXPIRED,
            ErrorCode.VIDEO_REJECTED,
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.UNSUPPORTED_MEDIA_TYPE,
        ],
    )
    def test_permanent_codes_are_not_retryable(self, code: ErrorCode) -> None:
        assert ToolError(code, "x").retryable is False

    def test_quota_is_not_retryable_even_though_rate_limit_is(self) -> None:
        """The distinction that stops a retry loop eating tomorrow's quota."""
        assert ToolError(ErrorCode.RATE_LIMITED, "x").retryable is True
        assert ToolError(ErrorCode.QUOTA_EXCEEDED, "x").retryable is False

    def test_explicit_override_wins(self) -> None:
        assert ToolError(ErrorCode.NOT_FOUND, "x", retryable=True).retryable is True


class TestPayload:
    def test_every_code_has_a_remediation(self) -> None:
        """A model given a bare code has nothing to act on."""
        for code in ErrorCode:
            error = ToolError(code, "something happened")
            assert error.remediation, f"{code} has no remediation"
            assert len(error.remediation) > 20, f"{code}'s remediation is too thin"

    def test_payload_shape_is_stable(self) -> None:
        payload = ToolError(ErrorCode.NOT_FOUND, "missing", details={"id": "x"}).to_payload()
        assert payload["ok"] is False
        assert set(payload["error"]) >= {"code", "message", "remediation", "retryable"}
        assert payload["error"]["code"] == "NOT_FOUND"

    def test_retry_after_is_included_when_known(self) -> None:
        payload = ToolError(
            ErrorCode.RATE_LIMITED, "slow down", retry_after_seconds=12.345
        ).to_payload()
        assert payload["error"]["retry_after_seconds"] == 12.35

    def test_retry_after_is_omitted_when_unknown(self) -> None:
        payload = ToolError(ErrorCode.NOT_FOUND, "missing").to_payload()
        assert "retry_after_seconds" not in payload["error"]
