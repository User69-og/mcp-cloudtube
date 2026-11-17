# Connecting Claude and ChatGPT

The server is the same for both clients. Only the transport differs.

| | Claude Desktop | Claude Code | ChatGPT |
|---|---|---|---|
| How it connects | installed extension | `claude_desktop_config.json` | custom connector (HTTPS URL) |
| Transport | stdio | stdio | streamable HTTP |
| Listening socket | **none** | **none** | `127.0.0.1:8077`, behind OpenAI's relay |
| Status | **working, verified** | **working, verified** | **working, verified** |

---

## Prerequisites

Both paths need the project set up and a Google Cloud project of your own:

```
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

(POSIX: `make setup`.) Then work through
[SETUP_GOOGLE_CLOUD.md](SETUP_GOOGLE_CLOUD.md) — about 40 minutes, and the one
part that cannot be automated because Google requires a human in the console.

Confirm before going further:

```
.venv\Scripts\python.exe -m mcp_cloudtube --check
```

---

## Claude Desktop (recommended)

Install `dist/cloudtube-0.1.0.mcpb` — double-click it, or
**Settings → Extensions → Install extension**.

It asks for two things, **both optional**:

| Field | Leave blank if… |
|---|---|
| **Python interpreter** | the `python` on your PATH has this project's dependencies |
| **OAuth client secrets** | you saved `client_secret.json` into the server's state folder |

> **If the extension says "couldn't start"**, the interpreter is almost
> certainly the cause: the default `python` on PATH usually does not have the
> dependencies. Click **View logs** — the server prints which interpreter ran,
> exactly which packages are missing, and what to set instead. Point the field
> at the project's virtual environment:
>
> - Windows: `<project>\.venv\Scripts\python.exe`
> - macOS / Linux: `<project>/.venv/bin/python`

Then enable it and set tool permissions. A sensible split:

- **Read-only tools** → *Always allow*. Browsing and reading are safe, and a
  prompt on every search makes the thing tedious to use.
- **Write/delete tools** → *Needs approval*. You want a prompt before
  publishing to a channel.

Claude groups them that way automatically, from the `readOnlyHint` and
`destructiveHint` annotations each tool declares.

### Rebuilding the extension

```
make extension
```

The bundle carries 85 KB of this project's own source and vendors nothing. A
full dependency tree would be 196 MB of platform-specific compiled binaries —
`pydantic_core`, `cryptography`, protobuf's `_upb` — useless to anyone on a
different OS or Python version.

---

## Claude Code

Add the server to `claude_desktop_config.json`. On Windows that file may live
in a custom data directory rather than `%APPDATA%\Claude` — search for it if
the obvious path has no `mcpServers` key.

```json
{
  "mcpServers": {
    "cloudtube": {
      "command": "E:\\Internship Project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_cloudtube", "--transport", "stdio"],
      "env": {
        "CLOUDTUBE_CLIENT_SECRET_FILE": "E:\\Internship Project\\.secrets\\client_secret.json"
      }
    }
  }
}
```

**Use absolute paths.** The client launches the subprocess from an unspecified
working directory, so a relative path will not resolve. Restart fully
afterwards — a background process keeps the old server list alive.

---

## ChatGPT

ChatGPT's model runs on OpenAI's servers, so a `localhost` URL cannot reach
your machine. The obvious fix — a public tunnel such as `cloudflared` — would
put a server holding live Google credentials on the open internet, protected
only by an unguessable hostname.

OpenAI's **Secure MCP Tunnel** avoids that. A `tunnel-client` process runs on
your machine, opens an *outbound* connection to `api.openai.com:443`, and
relays MCP calls to your local server. There is no inbound port, no public
hostname, and the tunnel is bound to one ChatGPT workspace.

### Setup

**1. Create a tunnel** at
[platform.openai.com/settings/organization/tunnels](https://platform.openai.com/settings/organization/tunnels).
Give it a name and select your ChatGPT workspace. Note the `tunnel_…` ID.

**2. Create a runtime API key** under *API keys*. Use **Restricted**
permissions with **Tunnels: Read + Use** and nothing else — the client needs
no other access, and a key scoped this narrowly cannot spend money or read
data if it leaks. Set an expiry.

**3. Download `tunnel-client`** from the Tunnels page, or from
[github.com/openai/tunnel-client/releases](https://github.com/openai/tunnel-client/releases/latest).
Verify it against the published `SHA256SUMS.txt` before running it.

**4. Start the MCP server over HTTP:**

```
.venv\Scripts\python.exe -m mcp_cloudtube --transport http
```

**5. Point the client at it.** The API key goes in the environment, never into
a config file:

```
$env:CONTROL_PLANE_API_KEY = "sk-..."

tunnel-client init --profile cloudtube --tunnel-id tunnel_xxx --mcp-server-url http://127.0.0.1:8077/mcp
tunnel-client doctor --profile cloudtube --explain
tunnel-client run --profile cloudtube
```

`doctor` should print `RESULT ok`. `run` should log `mcp session initialized`
with `server_name: cloudtube`, confirming the relay reached your server. A
local admin UI is served at `http://127.0.0.1:8080/ui`.

**6. Add the connector:** ChatGPT → **Plugins** → **Add** → **Add custom MCP
server**. Under *Connection* choose the **Tunnel** tab and select your tunnel.
Authentication: **No authentication**. Accept the risk notice, then **Create as
a plugin** and install it.

### Things worth knowing

- **`tunnel-client` must stay running.** Stop it and the connector goes dead.
  This is the one place ChatGPT is more work than Claude, whose extension
  needs nothing beyond the client itself.
- **The server authenticates no one.** Any client that reaches it acts as the
  connected Google account. That is coherent for a single-user tool on one
  machine — and it is precisely why a public tunnel would be the wrong choice.
- **Credentials are shared between clients.** Authorise once in Claude and
  ChatGPT is already connected, because both talk to the same server and the
  same token store. To demonstrate the first-run flow in ChatGPT, run
  `disconnect_account` first.

### Verifying without a client

`scripts/verify_http_endpoint.py` does what a connector does — opens a
session, completes the `initialize` handshake, lists the tools, calls one, and
checks that a bad argument returns a structured error rather than a transport
fault. Standard library only:

```
python scripts/verify_http_endpoint.py
```

## A five-minute pass that exercises the real paths

1. *"What's in my MCP Test folder?"* → first run returns `AUTH_REQUIRED` with
   a link; approve it in the browser, then ask again.
2. *"Find my product demo videos."* → `search_files` with `videos_only`.
3. *"What's in the release notes file?"* → `read_file`.
4. *"Summarise the product spec PDF."* → PDF text extraction.
5. *"What's in brand-assets.zip?"* → **error case**: `UNSUPPORTED_MEDIA_TYPE`,
   handled gracefully — it should describe the file rather than fail.
6. *"Upload the Q4 product demo as unlisted, titled 'Q4 Demo'."* → a job ID.
7. *"Did it finish?"* → `get_upload_status`.

Prefer naming a folder over *"what's in my Drive"*, which lists everything you
own — fine in private, less so on a screen recording.

### Exercising failures without spending quota

A real upload costs 1,600 of the 10,000 daily YouTube quota units — about six
a day including failures. To demonstrate the failure paths repeatedly, set:

```
CLOUDTUBE_FAKE_YOUTUBE=true
```

and restart. Uploads are routed to an in-process fake that reproduces dropped
connections, 503 storms, expired resume sessions, spent quota, duplicate
rejection and privacy overrides. Nothing is published.
