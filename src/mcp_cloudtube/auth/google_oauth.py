"""OAuth 2.0 authorisation code flow, driven from the chat window.

The brief requires that "every action happens from Claude or ChatGPT. No
separate web UI, CLI or manual script steps during normal use." That rules out
the obvious approach — `InstalledAppFlow.run_local_server()` — because it
blocks the calling thread until the user finishes in the browser and opens the
browser itself. Blocking inside a tool call would freeze the MCP session for
however long the user takes to find their password.

So the flow is split:

1.  ``begin()`` binds a loopback listener, builds the Google consent URL and
    returns it **immediately**. The tool hands that URL to the model, which
    shows it to the user as a link.
2.  A background thread waits for Google to redirect the browser back to the
    loopback listener, exchanges the code for tokens and saves them.
3.  The user's next tool call — or ``auth_status`` — sees the saved
    credentials. Nothing blocks, and nothing is typed at a terminal.

A loopback redirect (rather than a hosted callback) is what Google's "Desktop
app" client type is designed for, keeps the whole exchange on the user's
machine, and means no part of this project ever needs a public URL or a
client secret deployed anywhere.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import secrets
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from ..config import GOOGLE_IMPLICIT_SCOPES, GOOGLE_SCOPES, Settings
from ..errors import ErrorCode, ToolError
from ..google_errors import translate
from ..logging_setup import get_logger
from .token_store import DEFAULT_ACCOUNT, TokenStore

logger = get_logger(__name__)

# oauthlib refuses a token response whose granted scopes are not an exact set
# match for the requested ones, and raises a bare `Warning` to say so. Google
# routinely returns a superset -- it adds `openid` to any grant involving
# userinfo.email -- so that check fires on a perfectly good authorisation and
# surfaces as an unhandled error from deep inside the library.
#
# Relaxing it is safe here precisely because it is not the real check:
# `_assert_scopes_granted` below verifies that every scope the server needs was
# actually granted, which is the property that matters. oauthlib's version is
# both stricter (it rejects harmless extras) and weaker (it says nothing about
# which scope is missing), so ours replaces it rather than relying on it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

#: How long a started authorisation stays valid before the listener gives up.
AUTH_WINDOW_SECONDS = 600.0

_SUCCESS_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Connected</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e8eaed;
      display:grid;place-items:center;height:100vh;margin:0}
 .card{max-width:30rem;padding:2.5rem;text-align:center;background:#171a21;border-radius:14px;
       border:1px solid #272b35}
 h1{font-size:1.3rem;margin:0 0 .6rem}
 p{color:#9aa0ac;line-height:1.6;margin:0}
 .tick{font-size:2.6rem;line-height:1;margin-bottom:1rem}
</style></head>
<body><div class="card"><div class="tick">&#10003;</div>
<h1>Account connected</h1>
<p>You can close this tab and go back to your chat. Nothing was stored in the browser.</p>
</div></body></html>"""

_FAILURE_PAGE = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>Not connected</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e8eaed;
      display:grid;place-items:center;height:100vh;margin:0}
 .card{max-width:32rem;padding:2.5rem;text-align:center;background:#171a21;border-radius:14px;
       border:1px solid #3a2228}
 h1{font-size:1.3rem;margin:0 0 .6rem}
 p{color:#9aa0ac;line-height:1.6;margin:0}
 code{color:#e8eaed}
</style></head>
<body><div class="card">
<h1>Authorisation did not complete</h1>
<p>Go back to your chat and run <code>connect_account</code> again. The reason is in the
server log; nothing was saved.</p>
</div></body></html>"""


@dataclass
class PendingAuth:
    """An authorisation that has been started but not yet completed."""

    state: str
    flow: Flow
    redirect_uri: str
    server: http.server.HTTPServer
    thread: threading.Thread
    started_at: float = field(default_factory=time.monotonic)
    #: Wall-clock twin of started_at, for comparing against saved credentials.
    started_wall: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed: threading.Event = field(default_factory=threading.Event)
    error: ToolError | None = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.started_at > AUTH_WINDOW_SECONDS


def _free_port(preferred: int) -> int:
    """Bind a port for the loopback redirect.

    `preferred == 0` asks the OS for any free port, which is what we want by
    default: a fixed port is one more thing that can already be in use. A
    configured port is honoured, but if it is taken we say so clearly rather
    than failing with a bare OSError.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", preferred))
            return int(sock.getsockname()[1])
    except OSError as exc:
        raise ToolError(
            ErrorCode.CONFIG_ERROR,
            f"Could not bind 127.0.0.1:{preferred} for the OAuth redirect: {exc.strerror}.",
            remediation=(
                "Another process is using that port. Either stop it, or set "
                "CLOUDTUBE_OAUTH_REDIRECT_PORT=0 to let the server pick a free port "
                "automatically, then restart the server."
            ),
            details={"port": preferred},
        ) from exc


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler for Google's redirect back to the loopback address."""

    pending: PendingAuth  # injected via a subclass created per flow
    store: TokenStore
    account: str

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # Browsers request /favicon.ico unprompted; answering 404 keeps it from
        # being mistaken for the callback and tearing the listener down early.
        if parsed.path not in ("/", "/callback"):
            self.send_error(404)
            return

        try:
            self._handle(params)
        except ToolError as exc:
            self.pending.error = exc
            self._respond(400, _FAILURE_PAGE)
        except Exception as exc:
            self.pending.error = translate(exc, operation="complete authorisation")
            self._respond(500, _FAILURE_PAGE)
        finally:
            self.pending.completed.set()

    def _handle(self, params: dict[str, list[str]]) -> None:
        if error := params.get("error", [None])[0]:
            # The user clicked "Cancel", or the app is not approved for them.
            description = params.get("error_description", [""])[0]
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                f"Google did not grant access: {error}. {description}".strip(),
                remediation=(
                    "The authorisation was declined or refused. If you clicked Cancel, run "
                    "`connect_account` again and approve. If Google said the app is not "
                    "verified, add this Google account under Test users in the Cloud "
                    "Console's OAuth consent screen."
                ),
                details={"oauth_error": error},
            )

        returned_state = params.get("state", [None])[0]
        if not returned_state or not secrets.compare_digest(returned_state, self.pending.state):
            # Either a stale tab from a previous attempt, or a cross-site
            # request forgery. Both are refused identically.
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                "The authorisation response did not match the request that started it.",
                remediation=(
                    "This usually means an old authorisation tab was reused. Close any "
                    "leftover Google consent tabs and run `connect_account` again."
                ),
            )

        code = params.get("code", [None])[0]
        if not code:
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                "Google's redirect contained no authorisation code.",
                remediation="Run `connect_account` again.",
            )

        self.pending.flow.fetch_token(code=code)
        credentials = self.pending.flow.credentials

        if not credentials.refresh_token:
            # Google only issues a refresh token on first consent unless
            # prompt=consent is forced. We do force it, so this means the
            # response was genuinely unusable.
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                "Google returned an access token with no refresh token, so the connection "
                "would stop working within the hour.",
                remediation=(
                    "Revoke this app's access at https://myaccount.google.com/permissions "
                    "and run `connect_account` again."
                ),
            )

        _assert_scopes_granted(credentials)
        self.store.save(_credentials_to_dict(credentials), self.account)
        logger.info("oauth.completed", account=self.account)
        self._respond(200, _SUCCESS_PAGE)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # The callback URL contains an authorisation code; keep it out of any
        # referrer sent to a page the user visits next.
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """Silence BaseHTTPRequestHandler's stderr logging.

        Its default writes the full request line — which contains the
        authorisation code — to stderr. That is exactly the kind of line that
        ends up in a screen recording.
        """
        return


def _assert_scopes_granted(credentials: Credentials) -> None:
    """Fail early if the user unticked a permission on the consent screen.

    Checks that the granted set *covers* what we need, rather than that it
    matches exactly. Google adds scopes of its own accord (see
    GOOGLE_IMPLICIT_SCOPES), and a grant carrying more than we asked for is
    not a problem -- a grant carrying less is.
    """
    granted = set(credentials.scopes or ())
    required = set(GOOGLE_SCOPES) - GOOGLE_IMPLICIT_SCOPES
    missing = required - granted
    if missing:
        raise ToolError(
            ErrorCode.AUTH_SCOPE_INSUFFICIENT,
            "Authorisation finished, but some required permissions were not granted: "
            + ", ".join(sorted(s.rsplit("/", 1)[-1] for s in missing)),
            remediation=(
                "Run `connect_account` again and leave every permission ticked on Google's "
                "consent screen. Reading Drive and uploading to YouTube are both required; "
                "the server cannot work with a partial grant."
            ),
            details={"missing_scopes": sorted(missing)},
        )


def _credentials_to_dict(credentials: Credentials) -> dict[str, Any]:
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or ()),
        "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        "saved_at": datetime.now(UTC).isoformat(),
    }


class GoogleAuthManager:
    """Owns the OAuth lifecycle and hands out refreshed credentials."""

    def __init__(self, settings: Settings, store: TokenStore | None = None) -> None:
        self._settings = settings
        self._store = store or TokenStore(settings.token_file)
        self._pending: PendingAuth | None = None
        self._lock = threading.Lock()

    @property
    def store(self) -> TokenStore:
        return self._store

    # -- starting authorisation -------------------------------------------

    def begin(self, account: str = DEFAULT_ACCOUNT) -> str:
        """Start a flow and return the consent URL. Does not block."""
        with self._lock:
            self._discard_pending_if_stale()

            if self._pending is not None and not self._pending.completed.is_set():
                # Re-issuing the same URL is friendlier than refusing, and
                # avoids orphaning a listener if the user lost the first link.
                logger.info("oauth.reusing_pending")
                return self._authorization_url(self._pending)

            port = _free_port(self._settings.oauth_redirect_port)
            redirect_uri = f"http://127.0.0.1:{port}/"

            secrets_path = self._settings.resolve_client_secret()
            try:
                flow = Flow.from_client_secrets_file(
                    str(secrets_path),
                    scopes=list(GOOGLE_SCOPES),
                    redirect_uri=redirect_uri,
                )
            except (ValueError, KeyError, OSError) as exc:
                raise ToolError(
                    ErrorCode.CONFIG_ERROR,
                    f"The OAuth client secrets file at {secrets_path} "
                    f"could not be read as a desktop-client credential: {exc}",
                    remediation=(
                        "Re-download it from Google Cloud Console (APIs & Services -> "
                        "Clients). Make sure the client's type is 'Desktop app'; a 'Web "
                        "application' client produces a file with a different shape and "
                        "will not work with a loopback redirect."
                    ),
                ) from exc

            state = secrets.token_urlsafe(32)
            pending = self._start_listener(flow, state, redirect_uri, port, account)
            self._pending = pending
            logger.info("oauth.started", port=port, account=account)
            return self._authorization_url(pending)

    def _authorization_url(self, pending: PendingAuth) -> str:
        url, _ = pending.flow.authorization_url(
            access_type="offline",      # we need a refresh token
            prompt="consent",           # force one even on re-authorisation
            include_granted_scopes="true",
            state=pending.state,
        )
        return str(url)

    def _start_listener(
        self, flow: Flow, state: str, redirect_uri: str, port: int, account: str
    ) -> PendingAuth:
        store = self._store

        class Handler(_CallbackHandler):
            pass

        try:
            server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        except OSError as exc:
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                f"Could not start the local callback listener on port {port}: {exc.strerror}.",
                remediation=(
                    "Something bound the port between checking and using it. Run "
                    "`connect_account` again."
                ),
            ) from exc

        server.timeout = 1.0
        pending = PendingAuth(
            state=state, flow=flow, redirect_uri=redirect_uri, server=server, thread=None  # type: ignore[arg-type]
        )
        Handler.pending = pending
        Handler.store = store
        Handler.account = account

        def serve() -> None:
            """Serve until the callback lands or the window closes."""
            try:
                deadline = time.monotonic() + AUTH_WINDOW_SECONDS
                while not pending.completed.is_set() and time.monotonic() < deadline:
                    server.handle_request()  # returns after `timeout` if idle
                if not pending.completed.is_set():
                    pending.error = ToolError(
                        ErrorCode.AUTH_REQUIRED,
                        "The authorisation window expired before the browser came back.",
                        remediation=(
                            "The link is valid for 10 minutes. Run `connect_account` again "
                            "and approve it promptly."
                        ),
                    )
                    pending.completed.set()
                    logger.info("oauth.window_expired")
            except Exception as exc:
                pending.error = translate(exc, operation="wait for the authorisation redirect")
                pending.completed.set()
                logger.warning("oauth.listener_failed", error=str(exc))
            finally:
                with contextlib.suppress(OSError):
                    server.server_close()

        thread = threading.Thread(target=serve, name="cloudtube-oauth", daemon=True)
        pending.thread = thread
        thread.start()
        return pending

    def _discard_pending_if_stale(self) -> None:
        pending = self._pending
        if pending is None:
            return
        if pending.completed.is_set() or pending.expired:
            with contextlib.suppress(OSError):
                pending.server.server_close()
            self._pending = None

    def pending_error(self, account: str = DEFAULT_ACCOUNT) -> ToolError | None:
        """The failure from the last completed attempt, if it failed.

        Suppressed once credentials exist that were saved after the attempt
        began. A failed attempt followed by a successful one -- which is what
        a retry looks like -- would otherwise keep reporting the old failure
        next to a working connection, and a model reading that will tell the
        user something is broken when nothing is.
        """
        pending = self._pending
        if pending is None or not pending.completed.is_set() or pending.error is None:
            return None

        saved = self._store.load(account)
        if saved and (saved_at := saved.get("saved_at")):
            try:
                if datetime.fromisoformat(saved_at) >= pending.started_wall:
                    return None
            except ValueError:  # pragma: no cover - malformed timestamp
                pass
        return pending.error

    def authorization_in_progress(self) -> bool:
        pending = self._pending
        return pending is not None and not pending.completed.is_set() and not pending.expired

    # -- using credentials --------------------------------------------------

    def get_credentials(self, account: str = DEFAULT_ACCOUNT) -> Credentials:
        """Return valid credentials, refreshing them if needed.

        Raises `AUTH_REQUIRED` when nothing is stored and `AUTH_EXPIRED` when
        the stored grant can no longer be refreshed. The distinction matters:
        the first is the normal first-run state, the second means the user has
        to re-approve and any retry loop must stop.
        """
        data = self._store.load(account)
        if data is None:
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                "No Google account is connected to this server yet.",
            )

        try:
            credentials = Credentials.from_authorized_user_info(data, list(GOOGLE_SCOPES))
        except (ValueError, KeyError) as exc:
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                "The stored credentials could not be loaded; they look incomplete.",
                remediation="Call `connect_account` to authorise again.",
                cause=exc,
            ) from exc

        if credentials.valid:
            return credentials

        if not credentials.refresh_token:
            raise ToolError(
                ErrorCode.AUTH_EXPIRED,
                "The stored access token has expired and there is no refresh token to renew "
                "it with.",
            )

        try:
            credentials.refresh(GoogleRequest())
        except RefreshError as exc:
            # The overwhelmingly common cause in this project: an OAuth app in
            # "Testing" publishing status expires refresh tokens after 7 days.
            raise ToolError(
                ErrorCode.AUTH_EXPIRED,
                "The saved Google authorisation could not be renewed.",
                remediation=(
                    "Call `connect_account` to authorise again. This happens roughly weekly "
                    "while the Google Cloud OAuth app is in 'Testing' status, because Google "
                    "expires refresh tokens for unpublished apps after 7 days. It also "
                    "happens if access was revoked at myaccount.google.com/permissions."
                ),
                details={"account": account},
                cause=exc,
            ) from exc
        except Exception as exc:
            raise translate(exc, operation="refresh the Google credentials") from exc

        self._store.save(_credentials_to_dict(credentials), account)
        logger.info("oauth.refreshed", account=account)
        return credentials

    def status(self, account: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """Describe the connection without throwing. Used by `auth_status`."""
        data = self._store.load(account)
        if data is None:
            return {
                "connected": False,
                "authorization_in_progress": self.authorization_in_progress(),
                "reason": "No account has been connected yet.",
            }

        scopes = data.get("scopes") or []
        expiry_raw = data.get("expiry")
        expires_at = None
        access_token_expired = None
        if expiry_raw:
            try:
                parsed = datetime.fromisoformat(expiry_raw)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                expires_at = parsed.isoformat()
                access_token_expired = parsed <= datetime.now(UTC)
            except ValueError:
                expires_at = expiry_raw

        return {
            "connected": True,
            "authorization_in_progress": self.authorization_in_progress(),
            "scopes": scopes,
            "has_refresh_token": bool(data.get("refresh_token")),
            "access_token_expires_at": expires_at,
            "access_token_expired": access_token_expired,
            "authorized_at": data.get("saved_at"),
            "note": (
                "An expired access token is normal and is renewed automatically on the next "
                "call. Only a failed refresh requires re-authorising."
            ),
        }

    def disconnect(self, account: str = DEFAULT_ACCOUNT) -> dict[str, Any]:
        """Revoke the grant with Google, then forget it locally.

        Revocation is best-effort: if Google is unreachable we still delete the
        local copy, because the user asked to disconnect and leaving a usable
        token on disk would be the wrong way to fail.
        """
        data = self._store.load(account)
        if data is None:
            return {"was_connected": False, "revoked_remotely": False}

        revoked = False
        detail = None
        token = data.get("refresh_token") or data.get("token")
        if token:
            try:
                import urllib.request

                request = urllib.request.Request(
                    "https://oauth2.googleapis.com/revoke",
                    data=urllib.parse.urlencode({"token": token}).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(request, timeout=10):
                    revoked = True
            except Exception as exc:
                detail = (
                    "The local credentials were deleted, but Google could not be reached to "
                    "revoke them. Revoke manually at https://myaccount.google.com/permissions "
                    "if that matters to you."
                )
                logger.warning("oauth.revoke_failed", error=str(exc))

        self._store.delete(account)
        result: dict[str, Any] = {"was_connected": True, "revoked_remotely": revoked}
        if detail:
            result["warning"] = detail
        return result
