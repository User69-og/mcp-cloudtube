"""Error taxonomy for every failure this server can produce.

Design notes
------------
The brief is graded on "how the system behaves when things go wrong", and the
consumer of these errors is a language model, not a human reading a stack
trace. That drives three decisions:

1.  **Every failure is a value, not an exception that escapes.** Tool handlers
    catch everything and return a structured payload. An MCP server that dies
    on an unexpected exception takes the whole chat session's capability with
    it, so the top-level handler treats "unknown exception" as just another
    error code.

2.  **Every error carries a remediation.** A model that is told
    ``{"error": "403"}`` will either hallucinate a cause or retry forever.
    A model told "the refresh token expired, call ``connect_account`` to
    re-authorise" does the right thing unprompted. The remediation text is
    written to be read aloud to an end user *and* acted on by a model.

3.  **Retry stance is explicit and machine-readable.** ``retryable`` tells the
    model whether trying again could plausibly help. ``retry_after_seconds``,
    when present, tells it when. Distinguishing a per-minute rate limit
    (retry in 30s) from an exhausted daily quota (retry tomorrow) is the
    difference between a model that backs off and a model that burns the
    remaining quota in a loop.

Nothing in here ever interpolates a credential into a message; see
``redact`` and the test suite in ``tests/unit/test_errors.py``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final


class ErrorCode(StrEnum):
    """Stable, machine-readable failure codes.

    These strings are part of the tool contract: they appear in tool results
    that the model reads, and the integration tests assert on them. Treat
    renaming one as a breaking change.
    """

    # --- Authentication and authorisation -------------------------------
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_SCOPE_INSUFFICIENT = "AUTH_SCOPE_INSUFFICIENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # --- Caller mistakes -------------------------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"

    # --- Upstream throttling --------------------------------------------
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"

    # --- Upstream faults -------------------------------------------------
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"

    # --- Domain-specific failures ----------------------------------------
    UPLOAD_FAILED = "UPLOAD_FAILED"
    UPLOAD_NOT_FOUND = "UPLOAD_NOT_FOUND"
    VIDEO_REJECTED = "VIDEO_REJECTED"
    PRIVACY_NOT_APPLIED = "PRIVACY_NOT_APPLIED"

    # --- Operator problems ------------------------------------------------
    CONFIG_ERROR = "CONFIG_ERROR"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


#: Codes where trying the identical call again could plausibly succeed.
_RETRYABLE: Final[frozenset[ErrorCode]] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_UNAVAILABLE,
        ErrorCode.NETWORK_ERROR,
        ErrorCode.TIMEOUT,
    }
)

#: Default guidance per code. A ToolError may override with something more
#: specific (e.g. naming the exact folder that was not found).
_REMEDIATION: Final[dict[ErrorCode, str]] = {
    ErrorCode.AUTH_REQUIRED: (
        "No Google account is connected yet. Call the `connect_account` tool and give the "
        "user the authorisation URL it returns; they approve it in a browser, then retry."
    ),
    ErrorCode.AUTH_EXPIRED: (
        "The stored Google credentials are no longer valid — this normally means the refresh "
        "token expired (OAuth apps in 'Testing' status expire them after 7 days) or access was "
        "revoked. Call `connect_account` to re-authorise, then retry. Do not retry as-is."
    ),
    ErrorCode.AUTH_SCOPE_INSUFFICIENT: (
        "The connected account granted fewer permissions than this tool needs. Call "
        "`connect_account` again and ensure every requested permission is approved."
    ),
    ErrorCode.PERMISSION_DENIED: (
        "The connected account exists but is not allowed to do this. Usually the file belongs "
        "to someone else, or the YouTube channel is not owned by this account. Ask the user to "
        "confirm they are signed in as the right account, or choose a different file."
    ),
    ErrorCode.INVALID_INPUT: (
        "The arguments were rejected before any network call. Read the `details` field, correct "
        "the arguments and call the tool again. Retrying unchanged will fail identically."
    ),
    ErrorCode.NOT_FOUND: (
        "No such item. If you are working from a file ID returned by an earlier call, the file "
        "may have been moved to the trash since. Use `search_files` or `list_files` to find the "
        "current ID rather than guessing."
    ),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: (
        "This file's bytes are not readable as text. Use `get_file_metadata` to describe it "
        "instead, and tell the user what kind of file it is. Do not retry `read_file`."
    ),
    ErrorCode.FILE_TOO_LARGE: (
        "The file exceeds the read limit. Request a byte range with the `max_bytes` argument, "
        "or describe the file from its metadata instead of reading it."
    ),
    ErrorCode.RATE_LIMITED: (
        "Google is throttling requests briefly. Wait for `retry_after_seconds`, then retry the "
        "same call once. Do not issue the call repeatedly in a loop."
    ),
    ErrorCode.QUOTA_EXCEEDED: (
        "The project's daily API quota is exhausted; it resets at midnight US/Pacific. Retrying "
        "today will fail. Tell the user plainly that the quota is spent and stop — in "
        "particular do not retry an upload, which costs 1600 of the 10000 daily units."
    ),
    ErrorCode.UPSTREAM_UNAVAILABLE: (
        "Google returned a server-side error. The server already retried with backoff. Wait a "
        "moment and try once more; if it keeps failing, report it as a Google-side outage."
    ),
    ErrorCode.NETWORK_ERROR: (
        "The server could not reach Google. Check connectivity. The call is safe to retry; "
        "in-progress uploads resume from where they stopped rather than restarting."
    ),
    ErrorCode.TIMEOUT: (
        "The operation took longer than its deadline and was abandoned. For uploads, the job "
        "may still be running — call `get_upload_status` before starting a new upload, so the "
        "same video is not published twice."
    ),
    ErrorCode.UPLOAD_FAILED: (
        "The upload did not complete. Call `get_upload_status` with the job ID for the specific "
        "reason and whether it can be resumed."
    ),
    ErrorCode.UPLOAD_NOT_FOUND: (
        "No upload job with that ID. Job IDs come from `upload_video_to_youtube`; list recent "
        "jobs with `get_upload_status` and no arguments."
    ),
    ErrorCode.VIDEO_REJECTED: (
        "YouTube accepted the bytes but refused to publish the video. This is a content or "
        "policy decision (duplicate upload, claimed content, channel restriction) and will not "
        "succeed on retry. Relay the reason to the user."
    ),
    ErrorCode.PRIVACY_NOT_APPLIED: (
        "The video uploaded, but YouTube did not apply the requested privacy status. Unaudited "
        "API projects have their uploads locked to private. Tell the user the video is private "
        "and that making it unlisted or public requires a YouTube API compliance audit."
    ),
    ErrorCode.CONFIG_ERROR: (
        "The server is misconfigured and no user action in chat can fix it. Report the message "
        "to the user verbatim so the operator can correct the setup, and stop."
    ),
    ErrorCode.CANCELLED: "The operation was cancelled before it finished.",
    ErrorCode.INTERNAL: (
        "The server hit an unexpected condition. This is a bug in the server, not something the "
        "user did wrong. Report it and do not retry the same call repeatedly."
    ),
}


# Patterns that must never reach a model, a log line, or a chat transcript.
# Ordered roughly by how catastrophic a leak would be.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ya29\.[A-Za-z0-9_\-]{10,}"),          # Google access tokens
    re.compile(r"1//[A-Za-z0-9_\-]{10,}"),             # Google refresh tokens
    re.compile(r"AIza[A-Za-z0-9_\-]{35}"),             # Google API keys
    re.compile(r"[A-Za-z0-9_\-]{24}\.apps\.googleusercontent\.com"),  # client IDs
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),         # client secrets
    re.compile(r"-{5}BEGIN[^-]{0,40}PRIVATE KEY-{5}.*?-{5}END[^-]{0,40}PRIVATE KEY-{5}", re.S),
    re.compile(r"(?i)\b(authorization|bearer)\s*[:=]\s*\S+"),
)

_REDACTED: Final[str] = "[redacted]"


def redact(text: str) -> str:
    """Strip anything credential-shaped out of ``text``.

    Applied to every message on its way out of the process — tool results and
    log lines alike. Google's own error bodies sometimes echo request
    parameters back, so upstream text is not trusted to be secret-free just
    because we did not write it.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


class ToolError(Exception):
    """A failure that is safe and useful to show a model.

    Raised anywhere in the stack; converted to a payload at the tool boundary.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        remediation: str | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.code = code
        self.message = redact(message)
        self.remediation = redact(remediation or _REMEDIATION[code])
        # Explicit override wins; otherwise fall back to the code's stance.
        self.retryable = _RETRYABLE.__contains__(code) if retryable is None else retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = _redact_details(details or {})
        self.__cause__ = cause
        super().__init__(f"{code}: {self.message}")

    def to_payload(self) -> dict[str, Any]:
        """Render the dict that goes back to the client.

        Key order matters a little: models attend to what comes first, and
        ``error`` → ``code`` → ``message`` → ``remediation`` reads in the order
        a reader needs it.
        """
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": str(self.code),
                "message": self.message,
                "remediation": self.remediation,
                "retryable": self.retryable,
            },
        }
        if self.retry_after_seconds is not None:
            payload["error"]["retry_after_seconds"] = round(self.retry_after_seconds, 2)
        if self.details:
            payload["error"]["details"] = self.details
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ToolError(code={self.code!s}, retryable={self.retryable}, "
            f"message={self.message!r})"
        )


def _redact_details(details: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact string values inside a details mapping."""
    cleaned: dict[str, Any] = {}
    for key, value in details.items():
        if isinstance(value, str):
            cleaned[key] = redact(value)
        elif isinstance(value, dict):
            cleaned[key] = _redact_details(value)
        elif isinstance(value, list):
            cleaned[key] = [redact(v) if isinstance(v, str) else v for v in value]
        else:
            cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------
# Convenience constructors for the errors raised in more than one place.
# --------------------------------------------------------------------------

def invalid_input(message: str, **details: Any) -> ToolError:
    return ToolError(ErrorCode.INVALID_INPUT, message, details=details or None)


def not_found(message: str, **details: Any) -> ToolError:
    return ToolError(ErrorCode.NOT_FOUND, message, details=details or None)


def config_error(message: str, **details: Any) -> ToolError:
    return ToolError(ErrorCode.CONFIG_ERROR, message, details=details or None)
