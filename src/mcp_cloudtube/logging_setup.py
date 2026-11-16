"""Structured logging that cannot corrupt the MCP stdio transport.

The single most important rule in this file: **nothing is ever written to
stdout.** Under the stdio transport, stdout *is* the JSON-RPC channel between
the client and this process. One stray `print()` or a logging handler left on
its default stream injects a non-JSON line into that channel, and the client
drops the connection with a parse error that looks nothing like its cause.
Every handler here is pinned to stderr or to a file.

Logs are structured (key-value) rather than formatted prose because the
interesting questions during failure testing are "which operation, which error
code, how many attempts" — and those are fields, not sentences.

Every rendered log line is passed through the same `redact` used for tool
results, so a token cannot leak into a log file that a reviewer later reads or
that gets captured in a screen recording.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from .errors import redact

_configured = False


def _redact_processor(
    _logger: Any, _name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Scrub credential-shaped text from every field before it is rendered."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact(value)
    return event_dict


def configure_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Install the logging configuration. Safe to call more than once."""
    global _configured
    if _configured:
        return

    handlers: list[logging.Handler] = []

    # stderr: visible in the client's server log pane while developing.
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers.append(stderr_handler)

    # file: survives the client restarting the server, which is how most
    # interesting failures are actually diagnosed.
    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            handlers.append(file_handler)
        except OSError as exc:
            # Losing the log file is not worth refusing to start over.
            print(f"cloudtube: could not open log file {log_file}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    # Google's client libraries are chatty at INFO and log request URLs, which
    # can contain identifiers we would rather not echo. Quiet them down.
    for noisy in ("googleapiclient", "googleapiclient.discovery", "google_auth_httplib2",
                  "google.auth", "urllib3", "httpx", "httpcore", "oauthlib",
                  "requests_oauthlib", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # `googleapiclient.discovery_cache` logs a benign oauth2client warning on
    # every start; it confuses anyone reading the server log for real errors.
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "event"],
                drop_missing=True,
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Module-level logger. Configuration is lazy, so import order is free."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
