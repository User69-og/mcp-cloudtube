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

        # These two carry an explicit operator-facing remediation rather than
        # the taxonomy's default. A CONFIG_ERROR is the one class of failure
        # whose audience is the person setting the server up, not the model --
        # and "report this to the user verbatim" is useless advice at a
        # terminal.
        if not self.client_secret_file.exists():
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                "OAuth client secrets file not found at "
                f"{self.client_secret_file.resolve()}.",
                remediation=(
                    "In Google Cloud Console, go to APIs & Services -> Clients, open your "
                    "Desktop client and choose Download JSON. Save it to "
                    f"{self.client_secret_file}, or point CLOUDTUBE_CLIENT_SECRET_FILE at "
                    "wherever you put it. Full walkthrough: docs/SETUP_GOOGLE_CLOUD.md"
                ),
                details={"expected_path": str(self.client_secret_file.resolve())},
            )

        if self.client_secret_file.stat().st_size == 0:
            raise ToolError(
                ErrorCode.CONFIG_ERROR,
                f"The OAuth client secrets file at {self.client_secret_file.resolve()} "
                "is empty.",
                remediation=(
                    "The download produced a zero-byte file. Re-download it from Google "
                    "Cloud Console (APIs & Services -> Clients -> Download JSON)."
                ),
            )


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
