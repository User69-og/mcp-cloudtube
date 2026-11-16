"""Builds the MCP server and serves it over either transport.

Two transports, one server
--------------------------
The brief asks for a locally-run server that works with both Claude and
ChatGPT. Those clients connect differently:

*   **Claude Desktop** launches the server as a subprocess and speaks
    JSON-RPC over stdin/stdout. Nothing is listening on a port, which is the
    most secure arrangement available -- there is no socket to reach.
*   **ChatGPT** connects to custom connectors over HTTPS. It cannot launch a
    local subprocess, so a local server has to be reachable at a URL, which in
    practice means a tunnel in front of the HTTP transport.

Both are the same `MCPServer` with the same tools; only `run()` differs. That
keeps the two clients from drifting apart, which is the usual failure mode
when someone writes a second entry point.

The HTTP transport binds 127.0.0.1 by default, deliberately. Binding 0.0.0.0
would expose a server holding a live Google OAuth token to the local network.
The tunnel is the only thing that should be able to reach it, and that is an
explicit, visible step rather than a default.
"""

from __future__ import annotations

import contextlib
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import Settings, get_settings
from .context import ServerContext
from .logging_setup import get_logger
from .tools import register_all
from .youtube.client import YouTubeTransport

logger = get_logger(__name__)

INSTRUCTIONS = """\
This server connects to the user's Google Drive and their YouTube channel.

Typical flow:
  1. If any tool returns AUTH_REQUIRED, call `connect_account` and give the user
     the link it returns.
  2. Find a file with `search_files` (or browse with `list_files`).
  3. Read documents with `read_file`; describe videos and binaries with
     `get_file_metadata`.
  4. Publish a video with `upload_video_to_youtube`, then follow it with
     `get_upload_status`.

Things worth knowing before you act:
  - Every tool returns `ok: true/false`. On failure, read `error.remediation`
    and follow it; it says whether retrying can help. Do not retry an error
    whose `retryable` is false.
  - Never invent a file ID. They come from search and list results.
  - Uploading publishes to a real channel. If there is any ambiguity about
    which video, which title, or what privacy, ask the user first.
  - Uploads run in the background. Report them as started, and only call them
    published once `get_upload_status` says `completed`.
"""


def create_server(
    settings: Settings | None = None,
    *,
    youtube: YouTubeTransport | None = None,
) -> tuple[MCPServer, ServerContext]:
    """Assemble the server. Validates configuration before registering anything."""
    settings = settings or get_settings()
    settings.ensure_ready()

    ctx = ServerContext.build(settings, youtube=youtube)

    server = MCPServer(
        name="cloudtube",
        title="Google Drive + YouTube",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        warn_on_duplicate_tools=True,
    )

    register_all(server, ctx)
    logger.info(
        "server.created",
        provider=ctx.storage.display_name,
        fake_youtube=settings.fake_youtube,
    )
    return server, ctx


async def serve_stdio(settings: Settings | None = None) -> None:
    """Run for Claude Desktop and any other subprocess-launching client."""
    server, ctx = create_server(settings)
    try:
        await server.run_stdio_async()
    finally:
        with contextlib.suppress(Exception):
            await ctx.shutdown()


async def serve_http(settings: Settings | None = None) -> None:
    """Run for ChatGPT and any other client that connects to a URL."""
    settings = settings or get_settings()
    server, ctx = create_server(settings)
    try:
        await server.run_streamable_http_async(
            host=settings.http_host,
            port=settings.http_port,
            streamable_http_path="/mcp",
        )
    finally:
        with contextlib.suppress(Exception):
            await ctx.shutdown()


def describe_tools(server: MCPServer) -> list[dict[str, Any]]:
    """Used by tests and by `--list-tools` to show the registered surface."""
    tools = server._tool_manager.list_tools()
    return [
        {
            "name": tool.name,
            "title": getattr(tool, "title", None),
            "description": (tool.description or "").split("\n", 1)[0],
        }
        for tool in tools
    ]
