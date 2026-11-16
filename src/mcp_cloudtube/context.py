"""The object graph, assembled once and shared by every tool.

Kept separate from `server.py` so that tests can build a context with a fake
YouTube transport and a temporary state directory without starting a server,
and so the wiring is readable in one place rather than spread through tool
definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth.google_oauth import GoogleAuthManager
from .config import Settings, get_settings
from .logging_setup import configure_logging, get_logger
from .resilience import summarise_breakers
from .storage.base import StorageProvider
from .storage.registry import build_provider
from .youtube.client import YouTubeClient, YouTubeTransport
from .youtube.jobs import JobStore
from .youtube.uploader import UploadCoordinator

logger = get_logger(__name__)


@dataclass
class ServerContext:
    settings: Settings
    auth: GoogleAuthManager
    storage: StorageProvider
    youtube: YouTubeTransport
    jobs: JobStore
    uploads: UploadCoordinator

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        youtube: YouTubeTransport | None = None,
    ) -> ServerContext:
        settings = settings or get_settings()
        configure_logging(settings.log_level, settings.log_file)

        auth = GoogleAuthManager(settings)
        storage = build_provider(settings, auth)

        transport: YouTubeTransport
        if youtube is not None:
            transport = youtube
        elif settings.fake_youtube:
            from .youtube.fake import FakeYouTube

            logger.warning(
                "context.fake_youtube_enabled",
                note="Uploads will NOT reach YouTube. Unset CLOUDTUBE_FAKE_YOUTUBE to publish.",
            )
            transport = FakeYouTube()
        else:
            transport = YouTubeClient(auth, settings)

        jobs = JobStore(settings.jobs_db)
        jobs.connect()
        # Any job still marked in-flight belongs to a previous process that
        # died. Resolve it now so status answers are truthful from the start.
        jobs.mark_interrupted_jobs()

        uploads = UploadCoordinator(
            storage=storage, youtube=transport, jobs=jobs, settings=settings
        )

        return cls(
            settings=settings,
            auth=auth,
            storage=storage,
            youtube=transport,
            jobs=jobs,
            uploads=uploads,
        )

    async def shutdown(self) -> None:
        await self.uploads.shutdown()
        self.jobs.close()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "storage_provider": self.storage.display_name,
            "state_directory": str(self.settings.state_dir),
            "upload_mode": "FAKE (nothing is published)"
            if self.settings.fake_youtube
            else "live YouTube",
            "circuit_breakers": summarise_breakers() or "all closed",
        }
