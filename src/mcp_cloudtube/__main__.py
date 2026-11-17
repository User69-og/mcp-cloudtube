"""Command-line entry point.

Only ever run to *start* the server. The brief requires that normal use
happens entirely from the chat client, so there are no commands here for
listing files, authorising, or uploading -- those are tools, not subcommands.
The flags that exist are for starting it, inspecting what it exposes, and
checking configuration.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

# This must run before the imports below. Those imports are exactly what fails
# when the interpreter lacks the project's dependencies -- the most likely
# setup mistake, since the Claude Desktop extension defaults to whatever
# `python` is on PATH. Checking first turns a bare ImportError, which the app
# can only report as "couldn't start", into instructions.
from ._preflight import check_dependencies

check_dependencies()

from .config import get_settings  # noqa: E402
from .errors import ToolError  # noqa: E402
from .logging_setup import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-cloudtube",
        description="MCP server exposing Google Drive and YouTube publishing to an AI client.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help=(
            "stdio for Claude Desktop (the client launches this process); "
            "http for ChatGPT, which connects to a URL. Default: stdio."
        ),
    )
    parser.add_argument("--host", help="Override the HTTP bind address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, help="Override the HTTP port (default 8077).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and credentials, print a report, and exit.",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the registered tools as JSON and exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()

    if args.host:
        settings.http_host = args.host
    if args.port:
        settings.http_port = args.port

    configure_logging(settings.log_level, settings.log_file)

    try:
        if args.check:
            return _run_check(settings)

        if args.list_tools:
            from .server import create_server, describe_tools

            server, _ctx = create_server(settings)
            print(json.dumps(describe_tools(server), indent=2))
            return 0

        from .server import serve_http, serve_stdio

        if args.transport == "http":
            # Printing to stdout is safe here: under HTTP, stdout is not the
            # protocol channel. Under stdio it would corrupt the stream, which
            # is why nothing is printed on that path.
            print(
                f"cloudtube MCP server listening on "
                f"http://{settings.http_host}:{settings.http_port}/mcp",
                file=sys.stderr,
            )
            asyncio.run(serve_http(settings))
        else:
            asyncio.run(serve_stdio(settings))
        return 0

    except ToolError as error:
        # Configuration problems arrive here. Print the remediation, not a
        # traceback: the person reading this is setting the server up.
        print(f"\n  {error.code}: {error.message}\n", file=sys.stderr)
        print(f"  {error.remediation}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _run_check(settings: object) -> int:
    """Validate setup and report, without starting a server."""
    from .config import Settings

    assert isinstance(settings, Settings)
    problems: list[str] = []

    try:
        settings.ensure_ready()
        print("config         OK")
    except ToolError as error:
        print(f"config         FAILED: {error.message}")
        print(f"               -> {error.remediation}")
        problems.append("config")

    if not problems:
        from .auth.google_oauth import GoogleAuthManager

        auth = GoogleAuthManager(settings)
        status = auth.status()
        if status["connected"]:
            print("google account CONNECTED")
            print(f"               scopes: {len(status.get('scopes', []))} granted")
        else:
            print("google account NOT CONNECTED")
            print("               -> start the server, then run connect_account from chat")

    print(f"state dir      {settings.state_dir}")
    try:
        print(f"client secret  {settings.resolve_client_secret()}")
    except ToolError:
        print("client secret  NOT FOUND")
    print(f"upload mode    {'FAKE' if settings.fake_youtube else 'live YouTube'}")

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
