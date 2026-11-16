"""Tool registration.

Tools are grouped by the thing they act on rather than listed in one file, so
that the storage tools stay readable next to each other and the YouTube ones
next to each other. Registration order is also the order most clients display
them, which is why auth comes first: a user reading the tool list sees how to
connect before seeing what they could do once connected.
"""

from __future__ import annotations

from typing import Any

from ..context import ServerContext
from . import auth_tools, storage_tools, youtube_tools


def register_all(server: Any, ctx: ServerContext) -> None:
    auth_tools.register(server, ctx)
    storage_tools.register(server, ctx)
    youtube_tools.register(server, ctx)


__all__ = ["register_all"]
