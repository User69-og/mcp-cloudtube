# Connecting Claude and ChatGPT

The server is the same for both clients. Only the transport differs.

| | Claude Desktop | Claude Code | ChatGPT |
|---|---|---|---|
| How it connects | installed extension | `claude_desktop_config.json` | custom connector (HTTPS URL) |
| Transport | stdio | stdio | streamable HTTP |
| Listening socket | **none** | **none** | `127.0.0.1:8077`, behind a tunnel |
| Status | **working, verified** | **working, verified** | **verified at the protocol level**; see below |

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
      "command": "E:\Internship Project\.venv\Scripts\python.exe",
      "args": ["-m", "mcp_cloudtube", "--transport", "stdio"],
      "env": {
        "CLOUDTUBE_CLIENT_SECRET_FILE": "E:\Internship Project\.secrets\client_secret.json"
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

**The server supports this and it has been verified. The blocker is the
account tier, not the code.**

ChatGPT reaches MCP servers through custom connectors, which require **Plus,
Pro, Business or Enterprise**. On a free account, Developer Mode is not
available and no connector can be added — so the path cannot be
*demonstrated* on a free plan, however correct the server is.

What *can* be shown is that the endpoint is a working remote MCP server:

```
python -m mcp_cloudtube --transport http        # in one terminal
python scripts/verify_http_endpoint.py          # in another
```

That script uses the standard library only and does exactly what a connector
does: opens a session, completes the `initialize` handshake, lists the tools,
calls one, and checks that a bad argument comes back as a structured error
rather than a transport fault. Sample output:

```
  [ok]  initialize       cloudtube v0.1.0
  [ok]  session id       99e297d50e874121b1ceeb3ce53b491b
  [ok]  tools/list       10 tools advertised
  [ok]  tools/call       auth_status returned ok=True, connected=True
  [ok]  error handling   bad input -> NOT_FOUND, not a protocol error
PASS - this is a working remote MCP endpoint.
```

### Connecting it on a paid plan

1. **Start the HTTP transport:**

   ```
   .venv\Scripts\python.exe -m mcp_cloudtube --transport http
   ```

   It binds `127.0.0.1:8077` and serves MCP at `/mcp`. Loopback is
   deliberate: this process holds a live Google OAuth token, so the tunnel
   should be the only way in.

2. **Expose it over HTTPS.** `cloudflared` needs no account:

   ```
   cloudflared tunnel --url http://127.0.0.1:8077
   ```

3. **Add the connector:** ChatGPT → Settings → Connectors → Advanced →
   Developer mode → *Create*. Paste the tunnel URL **with `/mcp` appended**,
   and choose no authentication — the server authenticates to *Google*, not to
   the client.

4. Enable it for a conversation and try the same prompts as Claude.

### Things that will bite you

- **The tunnel URL changes** on every restart of free `cloudflared`. Update
  the connector when it does.
- **Anyone with that URL can reach your server**, and through it your Drive
  and YouTube channel. Stop the tunnel when you are not using it. For anything
  beyond a demo this needs real authentication in front of it — see *Known
  limitations* in the README.
- **The server must stay running.** Closing the terminal kills it.

---

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
