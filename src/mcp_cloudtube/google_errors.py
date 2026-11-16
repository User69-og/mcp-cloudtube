"""Translate Google API and transport failures into the local error taxonomy.

This module is the single place where `googleapiclient` / `google.auth` /
socket exceptions are interpreted. Everything above it deals in `ToolError`
only, which keeps the Google SDK from leaking into tool handlers and makes the
second storage provider (the stated bonus) a matter of writing a sibling
translator rather than touching call sites.

The interesting work is in HTTP 403. Google overloads that one status across
"you are going too fast" (retry in a moment), "you have spent your day's quota"
(retry tomorrow, and definitely do not retry an upload) and "you are not
allowed to do this at all" (never retry). Treating them alike is how a server
burns an entire day's quota in a retry loop, so the `reason` field in the error
body is parsed out and mapped individually.
"""

from __future__ import annotations

import json
import socket
import ssl
from typing import Any, Final

from .errors import ErrorCode, ToolError

try:  # pragma: no cover - import shape differs by install
    from googleapiclient.errors import HttpError, ResumableUploadError
except ImportError:  # pragma: no cover
    HttpError = ResumableUploadError = ()  # type: ignore[assignment,misc]

try:  # pragma: no cover
    from google.auth.exceptions import GoogleAuthError, RefreshError, TransportError
except ImportError:  # pragma: no cover
    GoogleAuthError = RefreshError = TransportError = ()  # type: ignore[assignment,misc]


#: Google's `reason` strings mapped to our codes. Anything unlisted falls back
#: to the status-code mapping below.
_REASON_MAP: Final[dict[str, ErrorCode]] = {
    # Throttling: short-lived, worth retrying.
    "rateLimitExceeded": ErrorCode.RATE_LIMITED,
    "userRateLimitExceeded": ErrorCode.RATE_LIMITED,
    "sharingRateLimitExceeded": ErrorCode.RATE_LIMITED,
    "backendError": ErrorCode.UPSTREAM_UNAVAILABLE,
    "internalError": ErrorCode.UPSTREAM_UNAVAILABLE,
    # Quota: gone for the day. Retrying is actively harmful.
    "quotaExceeded": ErrorCode.QUOTA_EXCEEDED,
    "dailyLimitExceeded": ErrorCode.QUOTA_EXCEEDED,
    "uploadLimitExceeded": ErrorCode.QUOTA_EXCEEDED,
    "storageQuotaExceeded": ErrorCode.QUOTA_EXCEEDED,
    # Authorisation.
    "authError": ErrorCode.AUTH_EXPIRED,
    "insufficientPermissions": ErrorCode.AUTH_SCOPE_INSUFFICIENT,
    "insufficientFilePermissions": ErrorCode.PERMISSION_DENIED,
    "forbidden": ErrorCode.PERMISSION_DENIED,
    "appNotAuthorizedToFile": ErrorCode.PERMISSION_DENIED,
    "youtubeSignupRequired": ErrorCode.PERMISSION_DENIED,
    "accountDelegationForbidden": ErrorCode.PERMISSION_DENIED,
    # Caller mistakes.
    "notFound": ErrorCode.NOT_FOUND,
    "fileNotFound": ErrorCode.NOT_FOUND,
    "videoNotFound": ErrorCode.NOT_FOUND,
    "badRequest": ErrorCode.INVALID_INPUT,
    "invalid": ErrorCode.INVALID_INPUT,
    "invalidParameter": ErrorCode.INVALID_INPUT,
    "invalidVideoMetadata": ErrorCode.INVALID_INPUT,
    "invalidTitle": ErrorCode.INVALID_INPUT,
    "invalidDescription": ErrorCode.INVALID_INPUT,
    "invalidTags": ErrorCode.INVALID_INPUT,
    "invalidFilename": ErrorCode.INVALID_INPUT,
    "mediaBodyRequired": ErrorCode.INVALID_INPUT,
    "cannotDownloadAbusiveFile": ErrorCode.PERMISSION_DENIED,
    # YouTube publishing refusals: the bytes arrived, the video will not go up.
    "failedPrecondition": ErrorCode.VIDEO_REJECTED,
    "forbiddenLicenseSetting": ErrorCode.VIDEO_REJECTED,
    "invalidCategoryId": ErrorCode.INVALID_INPUT,
    "defaultLanguageNotSet": ErrorCode.INVALID_INPUT,
}

_STATUS_MAP: Final[dict[int, ErrorCode]] = {
    400: ErrorCode.INVALID_INPUT,
    401: ErrorCode.AUTH_EXPIRED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    408: ErrorCode.TIMEOUT,
    409: ErrorCode.INVALID_INPUT,
    410: ErrorCode.NOT_FOUND,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.UPSTREAM_UNAVAILABLE,
    502: ErrorCode.UPSTREAM_UNAVAILABLE,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
    504: ErrorCode.TIMEOUT,
}


def _parse_error_body(exc: Any) -> tuple[str | None, str | None]:
    """Pull (reason, message) out of a Google JSON error body.

    Google returns at least three different body shapes depending on which
    front end answered. Rather than guess, try each and give up quietly — a
    missing reason just means we fall back to the status code.
    """
    raw = getattr(exc, "content", None)
    if not raw:
        return None, None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        # Occasionally an HTML error page from a proxy. Not useful; not fatal.
        return None, None

    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None, None

    message = error.get("message")
    errors = error.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        return errors[0].get("reason"), errors[0].get("message") or message

    status = error.get("status")  # newer shape, e.g. "RESOURCE_EXHAUSTED"
    if isinstance(status, str):
        return status, message
    return None, message


def _retry_after(exc: Any) -> float | None:
    """Honour a Retry-After header when Google sends one."""
    resp = getattr(exc, "resp", None)
    if resp is None:
        return None
    raw = None
    try:
        raw = resp.get("retry-after") or resp.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None  # HTTP-date form; backoff handles it well enough.


def _status_of(exc: Any) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def translate(exc: BaseException, *, operation: str) -> ToolError:
    """Convert any exception from a Google call into a `ToolError`.

    `operation` is a short human label ("list Drive files", "upload video")
    used to build a message that makes sense in a chat transcript.
    """
    if isinstance(exc, ToolError):
        return exc

    # --- Credential refresh failures --------------------------------------
    if RefreshError and isinstance(exc, RefreshError):
        text = str(exc)
        if "invalid_grant" in text or "expired" in text.lower() or "revoked" in text.lower():
            return ToolError(
                ErrorCode.AUTH_EXPIRED,
                "The saved Google authorisation is no longer valid, so the server could not "
                f"refresh it to {operation}.",
                details={"operation": operation},
                cause=exc,
            )
        return ToolError(
            ErrorCode.AUTH_EXPIRED,
            f"Could not refresh Google credentials to {operation}: {text}",
            details={"operation": operation},
            cause=exc,
        )

    # --- Transport-level failures ------------------------------------------
    if TransportError and isinstance(exc, TransportError):
        return ToolError(
            ErrorCode.NETWORK_ERROR,
            f"Network error while trying to {operation}: {exc}",
            details={"operation": operation},
            cause=exc,
        )
    if isinstance(exc, socket.timeout | TimeoutError):
        return ToolError(
            ErrorCode.TIMEOUT,
            f"Timed out trying to {operation}.",
            details={"operation": operation},
            cause=exc,
        )
    if isinstance(exc, ssl.SSLError):
        return ToolError(
            ErrorCode.NETWORK_ERROR,
            f"TLS error while trying to {operation}: {exc}. If this machine is behind a "
            "corporate proxy that intercepts HTTPS, its certificate must be trusted.",
            details={"operation": operation},
            cause=exc,
        )
    if isinstance(exc, ConnectionError | socket.gaierror | OSError) and not isinstance(
        exc, HttpError if HttpError else ()
    ):
        # OSError is broad; it is last so the narrower cases above win.
        return ToolError(
            ErrorCode.NETWORK_ERROR,
            f"Could not reach Google to {operation}: {exc}",
            details={"operation": operation},
            cause=exc,
        )

    # --- HTTP errors --------------------------------------------------------
    if HttpError and isinstance(exc, HttpError):
        status = _status_of(exc)
        reason, upstream_message = _parse_error_body(exc)
        code = _REASON_MAP.get(reason or "") or _STATUS_MAP.get(status or 0)

        if code is None:
            code = (
                ErrorCode.UPSTREAM_UNAVAILABLE
                if status and status >= 500
                else ErrorCode.INTERNAL
            )

        detail = upstream_message or str(exc)
        message = f"Google refused the request to {operation} (HTTP {status}). {detail}".strip()

        return ToolError(
            code,
            message,
            retry_after_seconds=_retry_after(exc),
            details={
                "operation": operation,
                "http_status": status,
                "google_reason": reason,
            },
            cause=exc,
        )

    if ResumableUploadError and isinstance(exc, ResumableUploadError):
        return ToolError(
            ErrorCode.UPLOAD_FAILED,
            f"The resumable upload could not {operation}: {exc}",
            details={"operation": operation},
            cause=exc,
        )

    if GoogleAuthError and isinstance(exc, GoogleAuthError):
        return ToolError(
            ErrorCode.AUTH_REQUIRED,
            f"Google authentication failed while trying to {operation}: {exc}",
            details={"operation": operation},
            cause=exc,
        )

    # --- Anything else is our bug ------------------------------------------
    return ToolError(
        ErrorCode.INTERNAL,
        f"Unexpected {type(exc).__name__} while trying to {operation}: {exc}",
        details={"operation": operation, "exception_type": type(exc).__name__},
        cause=exc,
    )
