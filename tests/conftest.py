"""Shared fixtures.

Every test runs against a temporary state directory so that nothing touches a
real token file, a real job database, or a real Google account. `anyio_backend`
pins the async backend to asyncio, matching how the server actually runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_cloudtube.config import Settings
from mcp_cloudtube.context import ServerContext
from mcp_cloudtube.resilience import reset_breakers
from mcp_cloudtube.youtube.fake import FakeYouTube

from .support.fake_storage import FakeStorage


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_breakers() -> None:
    """Circuit breakers are process-global; a tripped one must not leak."""
    reset_breakers()
    yield
    reset_breakers()


@pytest.fixture
def client_secret_file(tmp_path: Path) -> Path:
    """A structurally valid desktop-client file. Contains no real credential."""
    path = tmp_path / "client_secret_fixture.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test-client-id.apps.googleusercontent.invalid",
                    "client_secret": "test-not-a-real-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(tmp_path: Path, client_secret_file: Path) -> Settings:
    return Settings(
        _env_file=None,
        client_secret_file=client_secret_file,
        state_dir=tmp_path / "state",
        max_retries=2,
        backoff_base_seconds=0.001,
        backoff_max_seconds=0.01,
        request_timeout_seconds=5.0,
        upload_deadline_seconds=30.0,
        breaker_failure_threshold=3,
        breaker_reset_seconds=0.2,
    )


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def youtube() -> FakeYouTube:
    return FakeYouTube()


@pytest.fixture
def context(settings: Settings, storage: FakeStorage, youtube: FakeYouTube) -> ServerContext:
    """A fully wired server context with both externals faked."""
    settings.ensure_ready()
    ctx = ServerContext.build(settings, youtube=youtube)
    ctx.storage = storage  # type: ignore[assignment]
    ctx.uploads._storage = storage
    return ctx
