"""Configuration, resolved once at startup and validated loudly.

Philosophy: the server should refuse to start with a clear message rather than
start and fail on the tenth tool call. Anything that can be checked without a
network round trip is checked here — file existence, directory writability,
numeric sanity — so that a misconfigured install produces one legible error at
launch instead of six confusing ones later.

Secrets are never *values* in this file. The client secret lives in a JSON file
on disk whose path we store; the OAuth tokens live in a separate store. That
keeps secrets out of the process environment, out of `ps` output, and out of
any crash dump that includes the settings object.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ErrorCode, ToolError, config_error

#: Scopes requested at authorisation time. Keep this minimal and keep it in
#: sync with docs/SETUP_GOOGLE_CLOUD.md — a mismatch produces a confusing
#: "insufficient scope" error much later, at the first API call.
GOOGLE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    # Requested explicitly because Google grants it anyway: asking for
    # userinfo.email implies OpenID Connect, and the token response comes back
    # carrying `openid` whether or not it was requested. Listing it here keeps
    # the requested and granted sets equal, which several layers compare.
    "openid",
)

#: Scopes Google may add to a grant on its own. Tolerated when they come back,
#: never required. Kept separate from GOOGLE_SCOPES so that "what we need" and
#: "what we will accept" stay distinguishable.
GOOGLE_IMPLICIT_SCOPES: frozenset[str] = frozenset({"openid"})


def _default_state_dir() -> Path:
    """Where tokens and the job database live.

    Outside the repo by default, so that no amount of `git add -A` can sweep a
    token into a commit. Overridable for tests and for reviewers who want
    everything in one place.
    """
    if override := os.environ.get("CLOUDTUBE_STATE_DIR"):
        return Path(override).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base).expanduser() / "cloudtube-mcp"
    return Path.home() / ".cloudtube-mcp"


class Settings(BaseSettings):
    """Runtime configuration. Populated from the environment and `.env`."""

    model_config = SettingsConfigDict(
        env_prefix="CLOUDTUBE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Credentials -----------------------------------------------------
    client_secret_file: Path = Field(
        default=Path(".secrets/client_secret.json"),
        description="OAuth desktop-client JSON downloaded from Google Cloud Console.",
    )
    state_dir: Path = Field(default_factory=_default_state_dir)

    # --- Storage provider -------------------------------------------------
    storage_provider: Literal["gdrive"] = Field(
        default="gdrive",
        description=(
            "Which storage backend to serve. Only Google Drive ships today; the value exists "
            "so that adding a provider is a config change rather than a code change at the "
            "call sites."
        ),
    )

    # --- Limits -----------------------------------------------------------
    max_read_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        le=50 * 1024 * 1024,
        description="Hard ceiling on bytes returned by read_file, before truncation notice.",
    )
    max_list_page_size: int = Field(default=100, ge=1, le=1000)
    max_upload_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=1024,
        description="Refuse uploads larger than this before streaming a single byte.",
    )
    upload_chunk_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=256 * 1024,
        description=(
            "Resumable-upload chunk size. Must be a multiple of 256 KiB per Google's API. "
            "Smaller chunks mean more round trips but less work lost to a dropped connection."
        ),
    )

    # --- Timeouts and retries ---------------------------------------------
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    upload_deadline_seconds: float = Field(default=3600.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)

    # --- Circuit breaker ---------------------------------------------------
    breaker_failure_threshold: int = Field(default=5, ge=1)
    breaker_reset_seconds: float = Field(default=60.0, gt=0)

    # --- Transport ---------------------------------------------------------
    http_host: str = Field(default="127.0.0.1")
    http_port: int = Field(default=8077, ge=1, le=65535)
    oauth_redirect_port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="0 means pick a free ephemeral port for the loopback OAuth redirect.",
    )

    # --- Behaviour switches -------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    fake_youtube: bool = Field(
        default=False,
        description=(
            "Route uploads to an in-process fake instead of YouTube. Used by the failure-"
            "scenario suite so that testing error handling does not burn the 10k/day quota."
        ),
    )

    @field_validator("upload_chunk_bytes")
    @classmethod
    def _chunk_must_be_256kib_multiple(cls, value: int) -> int:
        # Google's resumable protocol rejects chunks that are not a multiple of
        # 256 KiB, with an error that does not say so. Catch it here instead.
        if value % (256 * 1024) != 0:
            raise ValueError("upload_chunk_bytes must be a multiple of 262144 (256 KiB)")
        return value

    @model_validator(mode="after")
    def _backoff_ordering(self) -> Settings:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self

    # --- Derived paths ------------------------------------------------------
    def candidate_client_secret_paths(self) -> list[Path]:
        """Where to look for the OAuth client file, in order of preference.

        The configured path wins. The fallbacks exist so that the Claude
        Desktop extension can ask for nothing at all when the file is in a
        conventional place: an extension launches with an unpredictable
        working directory, so a relative default is no use to it, but the
        state directory is somewhere both the server and the user can name.
        """
        seen: list[Path] = []
        configured = self.client_secret_file
        # An unset extension field arrives as an empty string, and Path("") is
        # the current directory -- which exists, and would pass a naive check.
        if str(configured) in ("", "."):
            configured = None  # type: ignore[assignment]

        for candidate in (
            configured,
            self.state_dir / "client_secret.json",
            Path.cwd() / ".secrets" / "client_secret.json",
            Path.home() / ".cloudtube-mcp" / "client_secret.json",
        ):
            if candidate is None:
                continue
            resolved = candidate.expanduser()
            if resolved not in seen:
                seen.append(resolved)
        return seen

    def resolve_client_secret(self) -> Path:
        """First existing candidate, or an error naming every place tried."""
        candidates = self.candidate_client_secret_paths()
        for candidate in candidates:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate

        listed = "\n".join(f"  - {c}" for c in candidates)
        raise ToolError(
            ErrorCode.CONFIG_ERROR,
            "No Google OAuth client secrets file was found.",
            remediation=(
                "Download it from Google Cloud Console (APIs & Services -> Clients -> your "
                "Desktop client -> Download JSON) and save it to any of these paths:\n"
                f"{listed}\n"
                "Or set CLOUDTUBE_CLIENT_SECRET_FILE to wherever you put it. Full "
                "walkthrough: docs/SETUP_GOOGLE_CLOUD.md"
            ),
            details={"searched": [str(c) for c in candidates]},
        )

    @property
    def token_file(self) -> Path:
        return self.state_dir / "google_token.json"

    @property
    def jobs_db(self) -> Path:
        return self.state_dir / "uploads.sqlite3"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "server.log"

    def ensure_ready(self) -> None:
        """Fail fast, with an actionable message, before serving traffic.

        Deliberately does *not* check that the user has authorised — that is a
        normal runtime state handled by AUTH_REQUIRED, not a configuration
        fault. This only checks what the operator controls.
        """
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise config_error(
                f"Cannot create the state directory at {self.state_dir}: {exc.strerror}. "
                "Set CLOUDTUBE_STATE_DIR to a writable location.",
                state_dir=str(self.state_dir),
            ) from exc

        probe = self.state_dir / ".write-probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise config_error(
                f"The state directory {self.state_dir} is not writable: {exc.strerror}. "
                "The server stores OAuth tokens and upload history there and cannot run "
                "without it.",
                state_dir=str(self.state_dir),
            ) from exc

        # Resolution, not a single fixed path: see resolve_client_secret.
        # CONFIG_ERROR is the one failure class whose audience is the person
        # setting the server up rather than the model, so its remediation is
        # written for a terminal, not a chat.
        self.resolve_client_secret()


_settings: Settings | None = None


def get_settings(*, refresh: bool = False) -> Settings:
    """Process-wide settings singleton.

    Cached because reading `.env` on every tool call would let configuration
    drift mid-session; `refresh=True` exists for tests.
    """
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings
