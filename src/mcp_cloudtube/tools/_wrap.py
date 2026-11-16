"""The boundary where every failure becomes a value.

A tool handler must not raise. An exception escaping into the MCP layer
becomes a protocol-level error, which clients render as a generic "the tool
failed" with no code, no remediation and nothing the model can act on. Worse,
an unhandled exception in some server implementations kills the connection and
takes every other tool with it.

So `tool_handler` wraps each one: `ToolError` becomes its structured payload,
and anything unexpected becomes a redacted INTERNAL error that is logged in
full locally. The model always gets the same shape back, and the server stays
up.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

from ..errors import ErrorCode, ToolError, redact
from ..logging_setup import get_logger

P = ParamSpec("P")
R = TypeVar("R", bound=dict[str, Any])

logger = get_logger(__name__)


def tool_handler(
    name: str,
) -> Callable[[Callable[P, Awaitable[dict[str, Any]]]], Callable[P, Awaitable[dict[str, Any]]]]:
    """Wrap a tool so it always returns a payload, never an exception."""

    def decorate(
        fn: Callable[P, Awaitable[dict[str, Any]]],
    ) -> Callable[P, Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> dict[str, Any]:
            started = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except ToolError as error:
                logger.info(
                    "tool.error",
                    tool=name,
                    code=str(error.code),
                    retryable=error.retryable,
                    ms=round((time.monotonic() - started) * 1000),
                )
                return error.to_payload()
            except Exception as exc:
                # Full detail to the local log, a safe summary to the model.
                logger.exception("tool.unexpected", tool=name)
                return ToolError(
                    ErrorCode.INTERNAL,
                    f"The {name} tool hit an unexpected {type(exc).__name__}: "
                    f"{redact(str(exc))}",
                    details={"tool": name},
                ).to_payload()

            logger.info(
                "tool.ok", tool=name, ms=round((time.monotonic() - started) * 1000)
            )
            if isinstance(result, dict) and "ok" not in result:
                # Uniform success marker so the model can branch on one key
                # regardless of which tool it called.
                return {"ok": True, **result}
            return result

        return wrapper

    return decorate
