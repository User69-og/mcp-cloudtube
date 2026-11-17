# mcp-cloudtube

An MCP server that gives an AI client read access to a user's **Google Drive**
and the ability to publish a video from it to their **YouTube** channel —
driven entirely from chat, with no second interface.

```
"Find the latest product demo video in my Drive and upload it
 to YouTube as unlisted, titled 'Q4 Demo'"
```

Works with **Claude Desktop** and **ChatGPT**. Runs locally; nothing is
deployed.

---

## Quick start

```bash
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
```

(POSIX: `make setup`.) That creates a virtual environment, installs the
server, arms a git hook that blocks credential commits, runs the tests, and
tells you what is still missing.

Then two things, in order:

1. **[docs/SETUP_GOOGLE_CLOUD.md](docs/SETUP_GOOGLE_CLOUD.md)** — the one part
   that cannot be automated, because Google requires a human in the Cloud
   Console. About 40 minutes.
2. **[docs/CONNECTING_CLIENTS.md](docs/CONNECTING_CLIENTS.md)** — wiring it to
   Claude Desktop or ChatGPT.

Check your setup at any point:

```bash
.venv\Scripts\python.exe -m mcp_cloudtube --check
```

---

## Tools

| Tool | What it does | Example prompt |
|---|---|---|
| `connect_account` | Returns a Google consent URL to show the user | *"Connect my Google account"* |
| `auth_status` | Which account is connected, and whether its credentials still work | *"Am I connected?"* |
| `disconnect_account` | Revokes access and deletes local credentials | *"Disconnect my Google account"* |
| `list_files` | Lists one folder's contents | *"What's in my Drive?"* |
| `search_files` | Finds files by name, content, type or date | *"Find the latest product demo video"* |
| `get_file_metadata` | Full details: size, type, duration, owner, link | *"How long is that video?"* |
| `read_file` | Readable text — handles Docs, Sheets, Slides, PDFs, code, CSV | *"Summarise the release notes"* |
| `upload_video_to_youtube` | Publishes a stored video; returns a job ID | *"Upload it as unlisted, titled 'Q4 Demo'"* |
| `get_upload_status` | Progress, or how it ended | *"Did my video upload?"* |
| `cancel_upload` | Stops an upload in flight | *"Cancel that upload"* |

Tool descriptions are written for the model that reads them, not as parameter
documentation. They say which tool to reach for, what not to do (*"never
invent a file ID"*, *"you never need to read a video before uploading it"*),
and when to stop and ask the user. An empty search result carries explicit
guidance, because that is where a model most often starts inventing IDs.

---

## Architecture

See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the diagrams: the
whole system, the upload pipeline, and the auth flow.

```
src/mcp_cloudtube/
├── errors.py           failure taxonomy · remediation per code · redaction
├── google_errors.py    the one place Google exceptions are interpreted
├── resilience.py       retry · full-jitter backoff · deadlines · circuit breaker
├── config.py           settings, validated at startup
├── logging_setup.py    structured logs, never on stdout
├── auth/               OAuth loopback flow · token store
├── storage/            StorageProvider interface + Google Drive
├── youtube/            metadata validation · resumable upload · job store · fake
├── tools/              the 10 tools, each wrapped so it cannot raise
└── server.py           one server, two transports
```

Dependencies point one way: `tools → domain → core → config`. Nothing above
`storage/` imports `googleapiclient`, which is what makes a second provider a
new module rather than edits at six call sites.

---

## Decisions, and why

**Google Drive, not Dropbox or S3.** Drive and YouTube live in the same Google
Cloud project and share one OAuth consent screen, so the user authorises
*once* and gets both capabilities. Any other provider means two separate
consent flows and a visibly more complicated auth story. The provider sits
behind a `StorageProvider` interface anyway, so the bonus second provider is a
new module, not a refactor.

**Uploads are background jobs, not blocking calls.** A video upload takes
minutes. A tool call that blocks for minutes is a chat that looks frozen and a
client that may time out and retry — which, for an upload, means publishing
the same video twice. `upload_video_to_youtube` returns a job ID immediately;
`get_upload_status` reports on it. The brief's tool list implies this shape by
asking for both.

**Job state is on disk (SQLite), not in memory.** The client owns this
process's lifetime — Claude Desktop restarts its MCP servers freely. With an
in-memory table, the answer to *"did my upload finish?"* after a restart is
"no idea", and uploading again becomes the only safe move. The resume URI is
persisted too, so an interrupted upload continues from the last acknowledged
byte instead of re-spending 1600 quota units.

**The video is staged to local disk before uploading.** A resumable upload
that loses its connection must re-send from the last acknowledged byte, so the
source has to be seekable. Streaming Drive→YouTube directly is not, so a
dropped connection would restart the whole transfer. Disk is cheap; quota
is not.

**The pipeline is ordered cheapest-check-first.** Validate metadata, suppress
duplicates, confirm the channel exists, *then* stage bytes, *then* spend
quota. Uploading 500 MB before discovering a 101-character title is exactly
the failure this ordering removes.

**`private` is the default privacy.** A video can always be made more public
later; something published publicly by mistake cannot be unseen.

**Tool argument limits are enforced in code, not as schema constraints.** A
pydantic `max_length` raises *before* the handler runs, producing a
protocol-level error with no code, no `retryable` flag and no remediation —
categorically different from every other failure. Limits are documented in the
tool description and validated (or clamped) in code, so every tool answers in
one shape.

**You bring your own Google Cloud project, and that is not "deployment".**
Running this needs a Google Cloud project with the two APIs enabled and an
OAuth client downloaded. That is not a gap in "runs locally, no deployment
needed" — it is what *"the user connects their own storage account and YouTube
channel"* requires. Nothing of this project is hosted anywhere: the process
runs on your machine, binds loopback only, and writes tokens into your own
user profile. Provisioning your access to your own data is not deploying
software.

The alternative — shipping a shared OAuth client — is worse and partly
prohibited. It would mean committing a credential (an explicit rule breach),
sharing one 10,000-unit daily quota across everyone who runs it (about six
uploads a day, total), and adding every user to the Testing-mode test-user
list by hand. `docs/SETUP_GOOGLE_CLOUD.md` makes the one-time setup about
forty minutes, and `--check` verifies it.

**`drive.readonly`, not `drive`.** The server never writes to Drive, so it
should not be able to. A bug here cannot delete anyone's documents.

**Secrets never enter the environment.** The client secret stays in its own
JSON file; tokens live in a state directory outside the repository, written
atomically with `chmod 600` or a restricted Windows ACL. Nothing secret is an
environment variable, so nothing secret appears in `ps` output or a crash
dump. A pre-commit hook rejects credential-shaped filenames and greps staged
diffs for live token patterns.

**Multi-user: single-user by design.** The server runs locally, as a
subprocess of one person's AI client. The MCP stdio transport carries no
authenticated principal, so there is no identity to partition tokens by — per-user
storage would be ceremony, not security. The token store *is* keyed by account
anyway, so the assumption lives in one constant (`DEFAULT_ACCOUNT`) rather
than spread through the codebase. Lifting it would mean: real authentication
on the HTTP transport, mapping an authenticated caller to an account key, and
per-account rate limiting. That is a different product, and deploying it would
mean the server no longer "runs locally" as the brief requires.

---

## Failure handling

The brief grades *"how the system behaves when things go wrong"*, so this is
the part with the most design in it.

**Every failure is a value, not an exception.** Tool handlers cannot raise: an
exception escaping into the MCP layer becomes a generic protocol error with
nothing for the model to act on, and in some servers takes the whole
connection down. Unexpected exceptions are logged in full locally and returned
redacted as `INTERNAL`.

**Every error carries a remediation written for a model.** A model told
`{"error": "403"}` will hallucinate a cause or retry forever. A model told
*"the refresh token expired, call `connect_account` to re-authorise"* does the
right thing unprompted.

**Retry stance is explicit.** `retryable` says whether trying again could
help; `retry_after_seconds` says when. The distinction that matters most is
inside HTTP 403, which Google overloads across three situations needing
opposite responses:

| Google `reason` | Code | Retryable | Why |
|---|---|---|---|
| `rateLimitExceeded` | `RATE_LIMITED` | yes | brief throttle, back off and retry |
| `quotaExceeded` | `QUOTA_EXCEEDED` | **no** | the day's budget is gone; retrying spends tomorrow's |
| `forbidden` | `PERMISSION_DENIED` | no | not allowed at all |

**Three layers of protection**, each for a different failure shape: bounded
retry with full-jitter backoff (a 503 that succeeds a moment later); per-attempt
and overall deadlines (a retry loop must not hang a chat); and a circuit
breaker per upstream so the eighth call fails in a millisecond rather than
spending 30 seconds rediscovering an outage. The breaker counts only
*infrastructure* failures — a 404 means the user asked for a missing file and
says nothing about Google's health, so ordinary mistakes cannot disable the
server.

**Secrets cannot leak into an error.** Every outbound message and log line
passes through the same redaction, including text Google wrote — its error
bodies sometimes echo request parameters back.

### Tested, not asserted

212 tests, including 17 failure scenarios:

```bash
.venv\Scripts\python.exe -m pytest tests/failure -v
```

Spent quota · dropped connection mid-upload · truncated download · transient
503s absorbed invisibly · expired resume session · video rejected as duplicate
· privacy silently downgraded · duplicate upload request · oversized metadata ·
non-video file · account with no YouTube channel · cancellation mid-flight ·
jobs orphaned by a server restart.

Each asserts the server stays up, the failure is classified correctly, *and*
nothing is left behind — no staged temp file, no job stuck non-terminal, no
half-published video reported as success.

These run against an injectable fake YouTube transport. A real upload costs
1600 of 10,000 daily quota units — about six per day including failures — so
testing against production would mean testing error handling roughly never.

---

## Known limitations

**Privacy may not be applied, though in testing it was.** Google documents
that an API project which has not passed YouTube's compliance audit has its
uploads restricted to private, whatever `privacyStatus` the request asks for.
**That was not observed here**: uploading to the project owner's own channel
from a Testing-status OAuth app, a request for `unlisted` was applied as
`unlisted`, confirmed both in the job record and by reading the video back
through `videos.list`.

The handling remains, because the documented restriction is real and may apply
to other projects, at other scales, or after a policy change. The server sends
the requested privacy, reads back what YouTube actually applied, and reports
`PRIVACY_NOT_APPLIED` when they differ — with the job still marked *completed*,
because the video genuinely is published. Reporting failure would be false and
would invite a duplicate upload; reporting plain success would tell the user
the wrong thing about who can see their video. That path is covered by a
failure-scenario test rather than by a live occurrence.

**Quota allows about six uploads a day.** 10,000 units, 1,600 per insert,
reset at midnight US/Pacific.

**Refresh tokens expire weekly.** While the OAuth app's publishing status is
"Testing", Google expires refresh tokens after 7 days. Handled as
`AUTH_EXPIRED` with a re-authorise instruction, not a stack trace.

**ChatGPT needs a tunnel, and the tunnel is unauthenticated.** ChatGPT
connectors must be an HTTPS URL, so a local server needs `cloudflared` or
`ngrok` in front. Anyone with that URL can reach the server and, through it,
the connected Drive and YouTube channel. Fine for a demo on a tunnel you stop
afterwards; not fine for anything else.

**Read limits.** `read_file` returns at most 1 MB and truncates with an
explicit notice. Scanned PDFs have no text layer and are reported as needing
OCR rather than as empty.

**No Drive writes.** By design — the scope is `drive.readonly`.

**Resume across restarts is recorded but not yet automatic.** The resume URI
is persisted and the job is marked resumable; continuing it still requires
starting the upload again. The plumbing is there, the trigger is not.

---

## With another week

In the order I would actually do them:

1. **Finish automatic resume.** The resume URI is already persisted and
   interrupted jobs are already marked; what is missing is a startup pass that
   picks them up and continues. It is the highest-value loose end, because it
   is the difference between "recoverable in principle" and "recovers".
2. **Real authentication on the HTTP transport.** OAuth client credentials or
   a bearer token, so the ChatGPT path is not secured only by an obscure URL.
   This is the one limitation I would be uncomfortable shipping.
3. **A second provider, to prove the interface earns its keep.** Dropbox is
   the honest test: path-based rather than ID-based, which is where a storage
   abstraction usually turns out to be Drive-shaped. If `StorageProvider`
   survives it unchanged, it was the right seam.
4. **Chunk-level progress to the client.** MCP supports progress
   notifications; right now the user has to ask. Streaming progress during a
   long upload would remove most `get_upload_status` calls.
5. **Property-based tests on the error taxonomy.** Generate arbitrary Google
   error bodies and assert the invariants hold: every classification has a
   remediation, nothing credential-shaped survives redaction, and
   `QUOTA_EXCEEDED` is never retryable. The hand-written cases cover what I
   thought of.
6. **A quota budget the server enforces.** It knows an insert costs 1600; it
   could track spend and refuse an upload it predicts will fail, rather than
   discovering it at 90%.

Deliberately *not* on this list: more providers, a web UI, or multi-user. The
first is breadth over depth, and the other two contradict the brief.

---

## Installing as a Claude Desktop extension

`dist/cloudtube-0.1.0.mcpb` is a double-click install — no hand-editing JSON
in an application support directory, which is the step that actually goes
wrong. It asks for two things, both optional:

| Field | Leave blank if… |
|---|---|
| Python interpreter | the `python` on your PATH has this project's dependencies |
| OAuth client secrets | you saved `client_secret.json` into the server's state folder |

If it cannot find a credentials file it names every path it looked in, so the
fix is never a guess.

Rebuild it with `make extension`. The bundle carries 83 KB of this project's
own source and vendors nothing: a full dependency tree would be 196 MB of
platform-specific compiled binaries, useless to anyone on a different OS.

## Development

```bash
make test           # full suite
make test-failures  # failure scenarios, verbose
make lint           # ruff
make check          # what CI would run
make doctor         # configuration and credential status
```

To exercise the failure paths without spending quota, set
`CLOUDTUBE_FAKE_YOUTUBE=true`. Nothing is published.
