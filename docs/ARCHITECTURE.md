# Architecture

## The whole system

```mermaid
flowchart TB
    subgraph clients["AI clients"]
        claude["Claude Desktop<br/><i>.mcpb extension</i><br/><i>launches a subprocess</i>"]
        code["Claude Code<br/><i>mcpServers config</i><br/><i>launches a subprocess</i>"]
        chatgpt["ChatGPT<br/><i>custom MCP server plugin</i><br/><i>via Secure MCP Tunnel</i>"]
    end

    tunnel["OpenAI Secure MCP Tunnel<br/><i>tunnel-client runs locally</i><br/><i>outbound only; no public URL</i>"]

    subgraph server["mcp-cloudtube  ·  runs locally on the user's machine"]
        direction TB
        transport["Transport layer<br/>stdio  |  streamable HTTP"]
        tools["Tool layer<br/><i>10 tools · every failure returned as a value</i>"]

        subgraph core["Core"]
            direction LR
            resil["resilience<br/>retry · backoff · deadlines<br/>circuit breaker"]
            errs["errors<br/>taxonomy · remediation<br/>redaction"]
        end

        subgraph domain["Domain"]
            direction LR
            storage["storage<br/><i>StorageProvider interface</i><br/>Google Drive impl"]
            yt["youtube<br/>metadata validation<br/>resumable upload"]
        end

        auth["auth<br/>OAuth loopback flow<br/>token store"]
        jobs[("uploads.sqlite3<br/><i>durable job state</i>")]
        tokens[("google_token.json<br/><i>chmod 600 / restricted ACL</i>")]
    end

    subgraph google["Google Cloud  ·  one project, one consent"]
        drive["Google Drive API<br/><i>drive.readonly</i>"]
        ytapi["YouTube Data API v3<br/><i>youtube.upload</i>"]
        oauth["Google OAuth 2.0"]
    end

    browser["User's browser<br/><i>consent screen</i>"]

    claude -->|"JSON-RPC over stdin/stdout"| transport
    code -->|"JSON-RPC over stdin/stdout"| transport
    chatgpt -.->|"relayed by OpenAI"| tunnel -->|"127.0.0.1:8077/mcp"| transport

    transport --> tools
    tools --> core
    tools --> domain
    domain --> core
    domain --> auth
    auth -.->|reads / writes| tokens
    yt -.->|reads / writes| jobs

    storage -->|REST| drive
    yt -->|"resumable upload"| ytapi
    auth -->|"token exchange & refresh"| oauth

    auth -.->|"1 . returns consent URL"| tools
    tools -.->|"2 . shown as a link in chat"| claude
    browser -->|"3 . user approves"| oauth
    oauth -->|"4 . redirect with code"| auth

    classDef store fill:#2d333b,stroke:#546,color:#e8eaed
    class jobs,tokens store
```

## The upload pipeline

Ordered cheapest-check-first, so the failures a user is most likely to hit
cost the least.

```mermaid
flowchart LR
    A["validate<br/>metadata"] --> B["check for<br/>duplicate"]
    B --> C["confirm<br/>channel"]
    C --> D["stage from<br/>Drive to disk"]
    D --> E["resumable<br/>upload"]
    E --> F["read the<br/>video back"]
    F --> G{"privacy<br/>applied?"}
    G -->|yes| H["completed"]
    G -->|no| I["completed<br/>+ PRIVACY_NOT_APPLIED"]

    A -.->|"INVALID_INPUT<br/>0 bytes, 0 quota"| X["failed"]
    C -.->|"PERMISSION_DENIED<br/>0 bytes staged"| X
    D -.->|"NETWORK_ERROR<br/>temp file removed"| X
    E -.->|"QUOTA_EXCEEDED<br/>not retried"| X
    F -.->|"VIDEO_REJECTED<br/>content decision"| X

    classDef bad fill:#3a2228,stroke:#a44,color:#e8eaed
    classDef warn fill:#3a3322,stroke:#a94,color:#e8eaed
    class X bad
    class I warn
```

Each stage writes its outcome to the job record before moving on, so a crash
at any point leaves a job whose state is true rather than one stuck at
"uploading" forever.

## The auth flow

The constraint is that it must be driven from chat — no CLI step, and the
session must not block while the user finds their password.

```mermaid
sequenceDiagram
    actor User
    participant Client as Claude / ChatGPT
    participant Server as mcp-cloudtube
    participant Loop as loopback listener<br/>127.0.0.1:ephemeral
    participant Google

    User->>Client: "upload my demo to YouTube"
    Client->>Server: search_files(...)
    Server-->>Client: ok:false · AUTH_REQUIRED<br/>remediation: call connect_account
    Client->>Server: connect_account()
    Server->>Loop: bind an ephemeral port
    Server-->>Client: authorization_url  (returns at once)
    Client-->>User: "open this link to connect"

    User->>Google: approves in the browser
    Google->>Loop: redirect with code + state
    Loop->>Loop: compare state (constant-time)
    Loop->>Google: exchange code for tokens
    Google-->>Loop: access + refresh token
    Loop->>Loop: verify every scope was granted
    Loop-->>User: "Account connected" page

    User->>Client: "done"
    Client->>Server: search_files(...)
    Server-->>Client: ok:true · results
```

Why a loopback redirect rather than a hosted callback: it is what Google's
"Desktop app" client type is designed for, the whole exchange stays on the
user's machine, and no part of this project ever needs a public URL or a
client secret deployed anywhere.

The same reasoning drives the ChatGPT path. Its model runs on OpenAI's
servers, so `localhost` is unreachable from there — but rather than publish the
server behind a public tunnel, `tunnel-client` runs locally and dials *out*,
so the server stays bound to `127.0.0.1` and no inbound port is ever opened.

## Layering rule

Dependencies point one way:

```
tools  ->  domain (storage, youtube)  ->  core (resilience, errors)  ->  config
                     |
                     +->  auth
```

Nothing above `storage/` imports `googleapiclient`. That is what makes the
second storage provider a new module rather than edits at six call sites, and
it is why the Drive-specific awkwardness — native-document exports, `q`
expression escaping, shared-drive flags — stays in one file.
