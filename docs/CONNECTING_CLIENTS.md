# Connecting Claude and ChatGPT

The server is the same for both. Only the transport differs, because the two
clients connect in fundamentally different ways:

| | Claude Desktop | ChatGPT |
|---|---|---|
| How it connects | launches the server as a subprocess | connects to an HTTPS URL |
| Transport | stdio (JSON-RPC over stdin/stdout) | streamable HTTP |
| Listening socket | **none** | `127.0.0.1:8077`, behind a tunnel |
| Needs a tunnel | no | yes |

Claude is the simpler and more secure of the two: nothing is listening on a
port at all, so there is nothing on the machine to reach.

---

## Prerequisite

Both paths assume setup is done:

```bash
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

and that `docs/SETUP_GOOGLE_CLOUD.md` has been worked through, so
`.secrets/client_secret.json` exists. Confirm with:

```bash
.venv\Scripts\python.exe -m mcp_cloudtube --check
```

---

## Claude Desktop

1. Open the config file. On Windows:

   ```
   %APPDATA%\Claude\claude_desktop_config.json
   ```

   On macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`.
   Create it if it does not exist.

2. Add the server. **Use absolute paths** — Claude launches the subprocess
   from an unspecified working directory, so a relative path will not resolve:

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

   On macOS or Linux the command is `/path/to/project/.venv/bin/python` and
   the paths use forward slashes.

3. Restart Claude Desktop completely — quit it, do not just close the window.

4. The tools appear under the tools icon in the chat input. Try:

   > What's in my Google Drive?

   The first call returns `AUTH_REQUIRED`, Claude calls `connect_account`, and
   you get a link to approve. That is the intended first-run experience.

### If the server does not appear

Claude logs MCP server output to `%APPDATA%\Claude\logs\`. The most common
causes, in order:

- a relative path in `command` (use absolute);
- the venv python not existing at that path;
- JSON syntax errors in the config file — a trailing comma is the usual one;
- Claude not fully restarted.

Run the command from the config by hand to see the real error:

```bash
"E:\Internship Project\.venv\Scripts\python.exe" -m mcp_cloudtube --check
```

---

## ChatGPT

ChatGPT cannot launch a local subprocess. A custom connector is a URL, and it
must be HTTPS and publicly reachable, so a local server needs a tunnel in
front of it. This is a constraint of the client, not a design choice here —
see *Decisions* in the README.

1. **Start the server over HTTP:**

   ```bash
   .venv\Scripts\python.exe -m mcp_cloudtube --transport http
   ```

   It binds `127.0.0.1:8077` and serves the MCP endpoint at `/mcp`. It binds
   loopback deliberately: this process holds a live Google OAuth token, and
   the tunnel should be the only thing that can reach it.

2. **Open a tunnel.** Either works; `cloudflared` needs no account:

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8077
   ```

   ```bash
   ngrok http 8077
   ```

   Both print an HTTPS URL. Your connector URL is that address with `/mcp`
   on the end, for example
   `https://some-name.trycloudflare.com/mcp`.

3. **Add the connector in ChatGPT:** Settings → Connectors → Advanced →
   Developer mode, then *Create* / *Add custom connector*. Give it a name,
   paste the `/mcp` URL, and choose "No authentication" — the server
   authenticates to *Google*, not to the client, and it is only reachable
   through your tunnel.

4. In a chat, enable the connector for the conversation and try the same
   prompt as above.

### Things that will bite you

- **The tunnel URL changes** every time you restart `cloudflared` on the free
  tier. Update the connector URL when it does.
- **Anyone with the tunnel URL can reach your server**, and through it your
  Drive and YouTube channel. Stop the tunnel when you are not using it. For
  anything beyond a demo this needs real authentication in front of it; see
  *Known limitations* in the README.
- **Leave the server running.** Closing the terminal kills it and the
  connector stops responding.

---

## Verifying either client end to end

A good five-minute pass that exercises the real paths:

1. "What's in my Google Drive?" → `connect_account`, approve, then a listing.
2. "Find my product demo videos." → `search_files` with `videos_only`.
3. "What's in the release notes file?" → `read_file`.
4. "What's in assets.zip?" → `UNSUPPORTED_MEDIA_TYPE`, handled gracefully: it
   should describe the file instead of failing.
5. "Upload the Q4 demo as unlisted, titled 'Q4 Demo'." → a job ID.
6. "Did it finish?" → `get_upload_status`, with the privacy caveat if the
   project is unaudited.

To demonstrate failure handling without spending quota, set
`CLOUDTUBE_FAKE_YOUTUBE=true` and restart. Nothing is published, and the
failure paths can be exercised as many times as you like.
