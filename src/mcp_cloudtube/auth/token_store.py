"""Persistence for OAuth credentials.

Stance on multi-user, stated here because this file is where it bites:

    **This server is single-user by design.** It runs locally, on one
    person's machine, as a subprocess of that person's AI client. There is no
    network listener to authenticate, no session concept in the MCP stdio
    transport, and no second human in the loop. Introducing per-user token
    partitioning would add a user-identity concept that nothing upstream can
    populate — the MCP protocol does not carry an authenticated principal to a
    stdio server — so it would be ceremony, not security.

    The store is nonetheless keyed by account rather than storing a single
    blob, because the HTTP transport (used for ChatGPT) *can* in principle
    serve more than one caller. Keying costs nothing now and means the
    single-user assumption lives in one place, enforced by
    ``DEFAULT_ACCOUNT``, instead of being spread across the codebase. The
    README explains what would have to change to lift it.

Tokens at rest are a real secret: a refresh token is a long-lived grant over
the user's Drive and YouTube channel. They are therefore stored outside the
repository, in the per-user state directory, with the tightest file
permissions the platform offers.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Final

from ..errors import ErrorCode, ToolError
from ..logging_setup import get_logger

logger = get_logger(__name__)

#: The only account key used while the server is single-user.
DEFAULT_ACCOUNT: Final[str] = "default"


def _harden_permissions(path: Path) -> None:
    """Restrict a credential file to the current user.

    POSIX gets chmod 600. Windows has no usable chmod, so the ACL is rewritten
    with icacls to remove inheritance and grant only the current user. Failure
    is logged, not fatal: refusing to run because an ACL could not be tightened
    would be worse than running with the default (already user-profile scoped)
    permissions, but the operator should know.
    """
    try:
        if os.name == "posix":
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            return

        # Windows: drop inherited ACEs, grant the current user full control.
        user = os.environ.get("USERNAME")
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "token_store.permissions_not_hardened",
            path=str(path),
            error=str(exc),
            hint="The token file is readable by anyone who can read your user profile.",
        )


class TokenStore:
    """Reads and writes the OAuth token file atomically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def exists(self, account: str = DEFAULT_ACCOUNT) -> bool:
        return account in self._read_all()

    def load(self, account: str = DEFAULT_ACCOUNT) -> dict[str, Any] | None:
        return self._read_all().get(account)

    def save(self, data: dict[str, Any], account: str = DEFAULT_ACCOUNT) -> None:
        payload = self._read_all()
        payload[account] = data
        self._write_all(payload)
        logger.info("token_store.saved", account=account, path=str(self._path))

    def delete(self, account: str = DEFAULT_ACCOUNT) -> bool:
        payload = self._read_all()
        if account not in payload:
            return False
        del payload[account]
        self._write_all(payload)
        logger.info("token_store.deleted", account=account)
        return True

    def accounts(self) -> list[str]:
        return sorted(self._read_all())

    # -- internals ---------------------------------------------------------

    def _read_all(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                f"Could not read the saved credentials at {self._path}: {exc.strerror}.",
                remediation=(
                    "Check the file's permissions, or delete it and re-authorise with "
                    "`connect_account`."
                ),
            ) from exc

        if not raw.strip():
            return {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A truncated write (power loss mid-save, or an edited file). This
            # is recoverable by re-authorising, so say so rather than crashing
            # the server on every subsequent call.
            logger.warning("token_store.corrupt", path=str(self._path), error=str(exc))
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                f"The saved credentials file at {self._path} is not valid JSON and cannot "
                "be used.",
                remediation=(
                    "The stored credentials are corrupt. Call `connect_account` to "
                    "authorise again; that rewrites the file."
                ),
            ) from exc

        if not isinstance(parsed, dict):
            raise ToolError(
                ErrorCode.AUTH_REQUIRED,
                f"The saved credentials file at {self._path} has an unexpected shape.",
                remediation="Call `connect_account` to authorise again and rewrite it.",
            )

        # Tolerate the pre-keying single-account layout, so an upgrade does not
        # silently log the user out.
        if "token" in parsed or "refresh_token" in parsed:
            return {DEFAULT_ACCOUNT: parsed}

        return {k: v for k, v in parsed.items() if isinstance(v, dict)}

    def _write_all(self, payload: dict[str, dict[str, Any]]) -> None:
        """Write atomically so a crash cannot leave a half-written token file."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".token-", suffix=".tmp"
            )
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                _harden_permissions(tmp_path)
                os.replace(tmp_path, self._path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            _harden_permissions(self._path)
        except OSError as exc:
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                f"Could not save credentials to {self._path}: {exc.strerror}.",
                remediation=(
                    "The server needs a writable state directory to remember your "
                    "authorisation. Set CLOUDTUBE_STATE_DIR to a writable path and restart."
                ),
            ) from exc


def describe_storage_location(path: Path) -> str:
    """Human-readable note about where tokens live, for the auth_status tool."""
    inside_repo = False
    with contextlib.suppress(OSError):
        inside_repo = Path.cwd() in path.resolve().parents
    suffix = (
        "  WARNING: this is inside the project directory; make sure it is gitignored."
        if inside_repo
        else ""
    )
    return f"{path}{suffix}"


__all__ = ["DEFAULT_ACCOUNT", "TokenStore", "describe_storage_location"]
