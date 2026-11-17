"""Prove the HTTP transport works as a remote MCP endpoint.

Why this script exists
----------------------
The assignment asks for the server to work from both Claude and ChatGPT.
ChatGPT reaches MCP servers through custom connectors, which are available
only on paid plans -- so on a free account the client cannot be pointed at
this server no matter how correct the server is.

That makes the ChatGPT path impossible to *demonstrate* but perfectly
possible to *verify*. This script does what ChatGPT's connector would do:
opens a session, completes the MCP initialize handshake, lists the tools,
and calls one. If it passes, the endpoint is a working remote MCP server and
the only remaining requirement is a client permitted to connect to it.

Run against a local server:
    python scripts/verify_http_endpoint.py

Or against a tunnel, which is what a connector would use:
    python scripts/verify_http_endpoint.py https://example.trycloudflare.com/mcp

Standard library only, so a reviewer can run it without installing anything.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8077/mcp"
PROTOCOL_VERSION = "2025-06-18"

NEWLINE = chr(10)
TICK = "  [ok]  "
CROSS = "  [FAIL]"


class Session:
    """Minimal MCP-over-HTTP client, built the way a connector talks."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.session_id: str | None = None

    def _post(self, payload: dict, *, expect_response: bool = True) -> dict | None:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, method="POST")  # noqa: S310
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json, text/event-stream")
        if self.session_id:
            request.add_header("mcp-session-id", self.session_id)

        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            returned = response.headers.get("mcp-session-id")
            if returned:
                self.session_id = returned
            raw = response.read().decode("utf-8")

        if not expect_response:
            return None

        # A streamable-HTTP server may answer as JSON or as a single SSE event.
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{"):
                return json.loads(line)
        raise RuntimeError(f"no JSON-RPC object in response: {raw[:200]}")

    def initialize(self) -> dict:
        result = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "connector-verification", "version": "1.0"},
            },
        })
        assert result is not None
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"},
                   expect_response=False)
        return result["result"]

    def list_tools(self) -> list[dict]:
        result = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert result is not None
        return result["result"]["tools"]

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        result = self._post({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        assert result is not None
        payload = result["result"]
        if "structuredContent" in payload:
            return payload["structuredContent"]
        return json.loads(payload["content"][0]["text"])


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    print(f"Verifying MCP endpoint: {url}")
    print("-" * 60)

    session = Session(url)
    failures = 0

    # 1. handshake -------------------------------------------------------
    try:
        info = session.initialize()
        server = info.get("serverInfo", {})
        print(f"{TICK}initialize       {server.get('name')} v{server.get('version')}")
        print(f"{TICK}session id       {session.session_id}")
    except urllib.error.URLError as exc:
        print(f"{CROSS}initialize       cannot reach the endpoint: {exc.reason}")
        print()
        print("  Start the server first:")
        print("    python -m mcp_cloudtube --transport http")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"{CROSS}initialize       {type(exc).__name__}: {exc}")
        return 1

    # 2. tool discovery ----------------------------------------------------
    try:
        tools = session.list_tools()
        print(f"{TICK}tools/list       {len(tools)} tools advertised")
        for tool in tools:
            print(f"           - {tool['name']}")
        if len(tools) < 6:
            print(f"{CROSS}expected at least the six tools the brief names")
            failures += 1
    except Exception as exc:  # noqa: BLE001
        print(f"{CROSS}tools/list       {type(exc).__name__}: {exc}")
        return 1

    # 3. a real call, and a structured answer ------------------------------
    try:
        result = session.call_tool("auth_status")
        if "ok" not in result:
            print(f"{CROSS}tools/call       result has no 'ok' field")
            failures += 1
        else:
            connected = result.get("connected")
            print(f"{TICK}tools/call       auth_status returned ok={result['ok']}, "
                  f"connected={connected}")
    except Exception as exc:  # noqa: BLE001
        print(f"{CROSS}tools/call       {type(exc).__name__}: {exc}")
        failures += 1

    # 4. errors arrive as values, not transport faults ----------------------
    try:
        result = session.call_tool("get_file_metadata", {"file_id": "definitely-not-a-real-id"})
        if result.get("ok") is False and result.get("error", {}).get("code"):
            print(f"{TICK}error handling   bad input -> "
                  f"{result['error']['code']}, not a protocol error")
        else:
            print(f"{CROSS}error handling   expected a structured failure, got: {result}")
            failures += 1
    except Exception as exc:  # noqa: BLE001
        print(f"{CROSS}error handling   a tool error escaped as {type(exc).__name__}: {exc}")
        failures += 1

    print("-" * 60)
    if failures:
        print(f"FAILED  ({failures} check(s))")
        return 1

    print("PASS - this is a working remote MCP endpoint.")
    print()
    print("  To connect it to ChatGPT (requires Plus, Pro, Business or Enterprise;")
    print("  custom connectors are not available on the free tier):")
    print("    1. expose it over HTTPS, e.g.  cloudflared tunnel --url http://127.0.0.1:8077")
    print("    2. ChatGPT -> Settings -> Connectors -> Developer mode")
    print("    3. Add a custom connector pointing at  <https-url>/mcp , no authentication")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
